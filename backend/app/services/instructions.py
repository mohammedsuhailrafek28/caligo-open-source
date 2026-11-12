"""Payment instruction integrity (R1).

A payment instruction states WHERE (merchant/payee, buyer recipient) and HOW MUCH (currency, exact minor-unit amount) a project is
to be paid. It is an append-only, versioned, fingerprinted record. This module only RECORDS and ENFORCES; it never contacts the
payment provider and never moves money.

Trust model (honest limits):
* Every instruction is `PENDING_VERIFICATION`: it was typed by the authenticated seller (or taken from the first invoice request)
  and nobody independent has confirmed it. Buyer-side verification of CHANGES is R2 (an enrolled second channel, not proof of independence).
* A change to the instruction in force is only a *proposal*. While any proposal is unresolved a HOLD is active. There is no
  approve, override or break-glass route. The only terminal outcome R1 can record is WITHDRAWN (the proposer discards the proposal;
  the instruction in force never changed).
* A HOLD blocks operations performed through Caligo (create invoice, send invoice, accept provider evidence in a review). It cannot
  and does not freeze, redirect or block funds or operations that happen inside PayPal.
* WITHDRAWN is history, not a release. A withdrawal is recorded immutably but the hold stays effective: only a resolution listed in
  HOLD_RELEASING_RESOLUTIONS lifts it, and R1 lists none (R2 adds independently verified outcomes). Until then a pending or withdrawn
  change keeps the project held; there is deliberately no seller-only override or break-glass path.
* In-flight rule: an invoice create that was durably RESERVED (committed, bound to the exact instruction fingerprint) before a hold was
  established may complete against that bound instruction. The linearization point is the commit of the reservation under the project
  lock; for sends it is the commit of the claim. A hold never changes the recipient, amount or currency of a reserved operation: the
  claim's UPDATE requires the stored values to equal the bound ones.
* Reconciliation of what the provider reports is NOT blocked by a hold: recording the truth about money must never stop.

Concurrency: every mutation, every create-invoice reservation and every claim first takes a write lock on the project row
(`lock_project`), so proposals, reservations and claims are serialized. Integrity, merchant and provider conditions are evaluated under
that lock in the same transaction as the claim UPDATE (they need hashing, so they cannot sit inside the UPDATE itself; the instruction
rows are append-only). A SEND claim additionally carries the "no unreleased change" condition inside its UPDATE.
"""
import hashlib
import json

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app import schemas
from app.errors import ApiError
from app.models import (
    BuyerAuthenticator, InstructionChangeRequest, InstructionChangeResolution, InstructionChangeVerification, InstructionVerificationChallenge,
    PaymentInstruction, PaymentRecord, Project, ScopeBaseline, ScopeRecord, new_id,
)
from app.payment_provider import PaymentProvider
from app.schemas import AuditActor, ChangeResolution, HoldReason, InstructionProvenance, PaymentProviderName, ScopeKind
from app.security import normalize_email
from app.services.scope import canonical_sha256, record_audit

FINGERPRINT_VERSION = 1
_COMPARED = ("recipient_email", "currency", "amount_minor")


# --- fingerprints ---------------------------------------------------------------------------------------------------

def compute_fingerprint(project_id: str, provider: str, merchant_email: str | None, recipient_email: str, currency: str,
                        amount_minor: int, context: dict) -> str:
    """Hash of the canonical content. Key order and whitespace cannot change it; any field change does."""
    return canonical_sha256({
        "v": FINGERPRINT_VERSION, "project_id": project_id, "provider": provider, "merchant_email": merchant_email,
        "recipient_email": recipient_email, "currency": currency, "amount_minor": amount_minor, "context": context})


def compute_chain(previous_chain: str | None, fingerprint: str, version: int, project_id: str) -> str:
    return canonical_sha256({"previous": previous_chain, "fingerprint": fingerprint, "version": version, "project_id": project_id})


def instruction_fingerprint(row: PaymentInstruction) -> str:
    return compute_fingerprint(row.project_id, row.provider, row.merchant_email, row.recipient_email, row.currency,
                               row.amount_minor, row.context or {})


def chain_intact(versions: list[PaymentInstruction]) -> bool:
    """Recompute every fingerprint and chain link from stored content; any stored value that no longer matches is tampering."""
    previous: PaymentInstruction | None = None
    for position, row in enumerate(versions, start=1):
        if row.version != position or instruction_fingerprint(row) != row.fingerprint:
            return False
        if row.previous_instruction_id != (previous.id if previous else None):
            return False
        if row.chain_sha256 != compute_chain(previous.chain_sha256 if previous else None, row.fingerprint, row.version, row.project_id):
            return False
        previous = row
    return True


def _request_sha(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf8")).hexdigest()


# --- reads ----------------------------------------------------------------------------------------------------------

def versions_of(db: Session, project_id: str) -> list[PaymentInstruction]:
    return list(db.scalars(select(PaymentInstruction).where(PaymentInstruction.project_id == project_id)
                           .order_by(PaymentInstruction.version)))


def effective_instruction(db: Session, project_id: str) -> PaymentInstruction | None:
    versions = versions_of(db, project_id)
    return versions[-1] if versions else None


def _resolution_of(db: Session, change_id: str) -> InstructionChangeResolution | None:
    return db.scalar(select(InstructionChangeResolution).where(InstructionChangeResolution.change_id == change_id))


# Seller-side resolutions never lift a hold: stays EMPTY. WITHDRAWN is history only. (R2: the only releasing record is an
# InstructionChangeVerification row, written by app.services.instruction_verification after a buyer-side authenticator check.)
HOLD_RELEASING_RESOLUTIONS: frozenset[str] = frozenset()


_OPERATOR_TRUST = schemas.AuthenticatorTrustBasis.OPERATOR_ESTABLISHED.value


def _released() -> ColumnElement[bool]:
    """SQL predicate: a VALID buyer-side verification exists for the change. Nothing else releases a hold, and a verification row that merely
    exists does not: it must be bound to THIS change, project and fingerprints, come from an enrolled operator-established authenticator of
    this project whose buyer is the recorded approver, rest on a consumed challenge for exactly this change and approver, and (for APPLIED) point
    at the version that continues the instruction the change was proposed against. Anything missing, malformed or inconsistent fails closed:
    the change stays pending and the hold stays active."""
    V, A, C = InstructionChangeVerification, BuyerAuthenticator, InstructionVerificationChallenge
    applied = aliased(PaymentInstruction)
    approver = func.lower(V.approver_email)
    return exists().where(
        V.change_id == InstructionChangeRequest.id, V.project_id == InstructionChangeRequest.project_id,
        V.from_fingerprint == InstructionChangeRequest.from_fingerprint, V.proposed_fingerprint == InstructionChangeRequest.proposed_fingerprint,
        V.outcome.in_((schemas.VerificationOutcome.APPLIED.value, schemas.VerificationOutcome.REJECTED.value)), V.trust_basis == _OPERATOR_TRUST,
        exists().where(A.id == V.authenticator_id, A.project_id == V.project_id, A.trust_basis == V.trust_basis, func.lower(A.buyer_email) == approver),
        exists().where(C.id == V.challenge_id, C.change_id == V.change_id, C.project_id == V.project_id, C.authenticator_id == V.authenticator_id,
                       func.lower(C.approver_email) == approver, C.from_fingerprint == V.from_fingerprint,
                       C.proposed_fingerprint == V.proposed_fingerprint, C.consumed_at.is_not(None)),
        or_(and_(V.outcome == schemas.VerificationOutcome.REJECTED.value, V.applied_instruction_id.is_(None)),
            and_(V.outcome == schemas.VerificationOutcome.APPLIED.value,
                 exists().where(applied.id == V.applied_instruction_id, applied.project_id == V.project_id, applied.fingerprint == V.proposed_fingerprint,
                                applied.previous_instruction_id == InstructionChangeRequest.from_instruction_id))))


def verification_releases(db: Session, change_id: str) -> bool:
    """True only when the change's stored verification passes every binding check in `_released`."""
    return db.scalar(select(exists().where(InstructionChangeRequest.id == change_id, _released()))) is True


def verification_of(db: Session, change_id: str) -> InstructionChangeVerification | None:
    return db.scalar(select(InstructionChangeVerification).where(InstructionChangeVerification.change_id == change_id))


def verification_status_of(db: Session, instruction_id: str) -> schemas.InstructionVerification:
    """VERIFIED only when a verification that passes every binding check applied this version; a bare row never counts."""
    applied = db.scalar(select(InstructionChangeVerification.change_id).where(
        InstructionChangeVerification.applied_instruction_id == instruction_id))
    return (schemas.InstructionVerification.VERIFIED_BY_ENROLLED_SECOND_CHANNEL if applied and verification_releases(db, applied)
            else schemas.InstructionVerification.PENDING_VERIFICATION)


def instruction_view(db: Session, row: PaymentInstruction) -> schemas.PaymentInstructionView:
    view = schemas.PaymentInstructionView.model_validate(row)
    return view.model_copy(update={"verification_status": verification_status_of(db, row.id)})


def no_pending_change_clause(project_id: str) -> ColumnElement[bool]:
    """SQL predicate: the project has no change request whose hold has been released. Usable inside a compare-and-set UPDATE."""
    return ~exists().where(InstructionChangeRequest.project_id == project_id, ~_released())


def pending_changes(db: Session, project_id: str) -> list[InstructionChangeRequest]:
    return list(db.scalars(select(InstructionChangeRequest).where(InstructionChangeRequest.project_id == project_id, ~_released())
                           .order_by(InstructionChangeRequest.created_at, InstructionChangeRequest.id)))


def all_changes(db: Session, project_id: str) -> list[InstructionChangeRequest]:
    return list(db.scalars(select(InstructionChangeRequest).where(InstructionChangeRequest.project_id == project_id)
                           .order_by(InstructionChangeRequest.created_at, InstructionChangeRequest.id)))


def change_view(db: Session, change: InstructionChangeRequest) -> schemas.InstructionChangeView:
    view = schemas.InstructionChangeView.model_validate(change)
    resolution = _resolution_of(db, change.id)
    verification = verification_of(db, change.id)
    return view.model_copy(update={
        "resolution": ChangeResolution(resolution.resolution) if resolution else None,
        "holds_payment_operations": verification is None or not verification_releases(db, change.id),
        "verification": schemas.InstructionChangeVerificationView.model_validate(verification) if verification else None})


def compute_hold(db: Session, project_id: str, provider: PaymentProvider | None, *, required: bool = False,
                 ignore_pending: bool = False) -> schemas.HoldStatus:
    """`ignore_pending` is only for completing an operation that was reserved before the hold (the in-flight rule); every other
    reason (integrity, merchant, provider, required) still applies to it."""
    reasons: list[HoldReason] = []
    pending = pending_changes(db, project_id)
    if pending and not ignore_pending:
        reasons.append(HoldReason.UNVERIFIED_INSTRUCTION_CHANGE)
    versions = versions_of(db, project_id)
    if versions and not chain_intact(versions):
        reasons.append(HoldReason.INSTRUCTION_INTEGRITY_FAILURE)
    current = versions[-1] if versions else None
    if current is not None and provider is not None:
        if current.provider != provider.name.value:
            reasons.append(HoldReason.PROVIDER_MISMATCH)
        elif current.merchant_email != provider.expected_merchant_email:
            reasons.append(HoldReason.MERCHANT_MISMATCH)
    if current is None and required:
        reasons.append(HoldReason.INSTRUCTION_REQUIRED)
    return schemas.HoldStatus(active=bool(reasons), reasons=reasons, pending_change_ids=[c.id for c in pending])


def state(db: Session, project: Project, provider: PaymentProvider | None, *, required: bool) -> schemas.PaymentInstructionState:
    versions = versions_of(db, project.id)
    return schemas.PaymentInstructionState(
        instruction=instruction_view(db, versions[-1]) if versions else None,
        versions=len(versions), pending_changes=[change_view(db, c) for c in pending_changes(db, project.id)],
        hold=compute_hold(db, project.id, provider, required=required))


# --- concurrency ----------------------------------------------------------------------------------------------------

def lock_project(db: Session, project_id: str) -> None:
    """Take the write lock on the project row (SQLite: database write lock; Postgres: row lock) so that proposals and invoice
    reservations for the same project are serialized. The lock lasts until the caller commits or rolls back."""
    db.execute(update(Project).where(Project.id == project_id).values(id=Project.id)
               .execution_options(synchronize_session=False))


# --- audit of blocked attempts --------------------------------------------------------------------------------------

def _block(db: Session, project_id: str, operation: str, code: str, reasons: list[str]) -> ApiError:
    """Record that an operation was refused (evidence of attempts), release the lock, and build the error.
    Anything the caller staged is DISCARDED first: a refusal must never commit half of a rejected operation."""
    db.rollback()
    record_audit(db, project_id, "INSTRUCTION_GATE_BLOCKED", AuditActor.SYSTEM, "project", project_id,
                 {"operation": operation, "reasons": reasons})
    db.commit()
    if code == "payment_hold_active":
        return ApiError(409, code, "Payment operations through this service are on hold for this project. "
                                   "Funds at PayPal are unaffected.", details=[{"reason": r} for r in reasons])
    return ApiError(409, code, "The request does not match the payment instruction recorded for this project.",
                    details=[{"field": r} for r in reasons])


# --- agreement context ----------------------------------------------------------------------------------------------

def agreement_context(db: Session, project_id: str) -> dict:
    original = db.scalar(select(ScopeRecord).where(ScopeRecord.project_id == project_id, ScopeRecord.kind == ScopeKind.ORIGINAL.value)
                         .order_by(ScopeRecord.created_at, ScopeRecord.id).limit(1))
    baseline = db.scalar(select(ScopeBaseline).where(ScopeBaseline.project_id == project_id)
                         .order_by(ScopeBaseline.version.desc()).limit(1))
    return {"original_scope_record_id": original.id if original else None,
            "original_scope_sha256": original.source_sha256 if original else None,
            "baseline_id": baseline.id if baseline else None, "baseline_version": baseline.version if baseline else None,
            "baseline_content_sha256": baseline.content_sha256 if baseline else None}


# --- recording ------------------------------------------------------------------------------------------------------

def _provider_merchant(provider: PaymentProvider, supplied: str | None) -> str | None:
    """The payee is the provider's configured merchant. A supplied address may only confirm it, never replace it."""
    configured = provider.expected_merchant_email
    if supplied is not None and normalize_email(supplied) != configured:
        raise ApiError(422, "merchant_mismatch", "The merchant account is fixed by the payment provider configuration.")
    if configured is None and provider.name is not PaymentProviderName.MOCK_PAYPAL:
        raise ApiError(409, "merchant_evidence_missing", "The provider has no configured merchant account to verify payments against.")
    return configured


def _insert_version(db: Session, project: Project, provider: PaymentProvider, merchant: str | None, recipient: str, currency: str,
                    amount: int, provenance: InstructionProvenance, key: str | None, request_sha: str | None) -> PaymentInstruction:
    versions = versions_of(db, project.id)
    previous = versions[-1] if versions else None
    context = agreement_context(db, project.id)
    version = len(versions) + 1
    fingerprint = compute_fingerprint(project.id, provider.name.value, merchant, recipient, currency, amount, context)
    row = PaymentInstruction(
        id=new_id(), project_id=project.id, version=version, provider=provider.name.value, merchant_email=merchant,
        recipient_email=recipient, currency=currency, amount_minor=amount, context=context, fingerprint=fingerprint,
        previous_instruction_id=previous.id if previous else None,
        chain_sha256=compute_chain(previous.chain_sha256 if previous else None, fingerprint, version, project.id),
        provenance=provenance.value, idempotency_key=key, request_sha256=request_sha)
    db.add(row)
    db.flush()
    record_audit(db, project.id, "INSTRUCTION_RECORDED", AuditActor.SELLER if key else AuditActor.SYSTEM, "instruction", row.id,
                 {"version": version, "fingerprint": fingerprint, "provenance": provenance.value,
                  "verification": "PENDING_VERIFICATION"})
    return row


def record_initial(db: Session, project: Project, provider: PaymentProvider, body: schemas.PaymentInstructionCreate,
                   key: str) -> tuple[PaymentInstruction, bool]:
    """Returns (instruction, created). Same key + same body replays; a second instruction needs a change proposal."""
    recipient, currency = normalize_email(body.recipient_email), body.currency
    request_sha = _request_sha({"recipient": recipient, "currency": currency, "amount": body.amount_minor,
                                "merchant": normalize_email(body.merchant_email) if body.merchant_email else None})
    lock_project(db, project.id)
    replay = db.scalar(select(PaymentInstruction).where(PaymentInstruction.project_id == project.id,
                                                        PaymentInstruction.idempotency_key == key))
    if replay is not None:
        if replay.request_sha256 != request_sha:
            raise ApiError(409, "idempotency_key_conflict", "This Idempotency-Key was already used with a different request.")
        db.rollback()
        return replay, False
    if versions_of(db, project.id):
        db.rollback()
        raise ApiError(409, "instruction_exists", "This project already has a payment instruction; propose a change instead.")
    merchant = _provider_merchant(provider, body.merchant_email)
    try:
        row = _insert_version(db, project, provider, merchant, recipient, currency, body.amount_minor,
                              InstructionProvenance.SELLER_ENTERED, key, request_sha)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "instruction_exists", "This project already has a payment instruction; propose a change instead.") from None
    return row, True


def propose_change(db: Session, project: Project, provider: PaymentProvider, body: schemas.InstructionChangeCreate,
                   key: str) -> tuple[InstructionChangeRequest, bool]:
    recipient, currency = normalize_email(body.recipient_email), body.currency
    proposed_merchant = normalize_email(body.merchant_email) if body.merchant_email else None
    if proposed_merchant is not None and proposed_merchant != provider.expected_merchant_email:
        # the payee is always the provider's configured merchant; a different account is not something this service can accept
        raise ApiError(422, "merchant_mismatch", "The merchant account is fixed by the payment provider configuration.")
    request_sha = _request_sha({"recipient": recipient, "currency": currency, "amount": body.amount_minor,
                                "merchant": proposed_merchant, "reason": body.reason.strip()})
    lock_project(db, project.id)
    replay = db.scalar(select(InstructionChangeRequest).where(InstructionChangeRequest.project_id == project.id,
                                                              InstructionChangeRequest.idempotency_key == key))
    if replay is not None:
        db.rollback()
        if replay.request_sha256 != request_sha:
            raise ApiError(409, "idempotency_key_conflict", "This Idempotency-Key was already used with a different request.")
        return replay, False
    versions = versions_of(db, project.id)
    if not versions:
        db.rollback()
        raise ApiError(409, "instruction_required", "Record the payment instruction before proposing a change.")
    current = versions[-1]
    if pending_changes(db, project.id):
        db.rollback()
        raise ApiError(409, "change_pending", "A change is already pending and holds payment operations. It must be resolved first.")
    merchant = proposed_merchant or current.merchant_email
    changed = [name for name, new, old in (("merchant_email", merchant, current.merchant_email), ("recipient_email", recipient, current.recipient_email),
                                           ("currency", currency, current.currency), ("amount_minor", body.amount_minor, current.amount_minor))
               if new != old]
    if not changed:
        db.rollback()
        raise ApiError(422, "no_change", "The proposal is identical to the payment instruction in force.")
    fingerprint = compute_fingerprint(project.id, current.provider, merchant, recipient, currency, body.amount_minor, current.context or {})
    change = InstructionChangeRequest(
        id=new_id(), project_id=project.id, from_instruction_id=current.id, from_fingerprint=current.fingerprint,
        merchant_email=merchant, recipient_email=recipient, currency=currency, amount_minor=body.amount_minor,
        proposed_fingerprint=fingerprint, reason=body.reason.strip(), provenance=InstructionProvenance.SELLER_ENTERED.value,
        idempotency_key=key, request_sha256=request_sha)
    try:
        db.add(change)
        db.flush()
        record_audit(db, project.id, "INSTRUCTION_CHANGE_PROPOSED", AuditActor.SELLER, "instruction_change", change.id,
                     {"from_fingerprint": current.fingerprint, "proposed_fingerprint": fingerprint, "changed_fields": changed,
                      "effect": "HOLD_UNTIL_BUYER_SIDE_VERIFICATION"})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "idempotency_key_conflict", "This Idempotency-Key was already used.") from None
    return change, True


def withdraw_change(db: Session, project: Project, change_id: str, body: schemas.InstructionChangeWithdraw) -> InstructionChangeRequest:
    """Discards a proposal. This does NOT apply it: the instruction in force is unchanged. Safe to repeat."""
    lock_project(db, project.id)
    change = db.scalar(select(InstructionChangeRequest).where(InstructionChangeRequest.id == change_id,
                                                              InstructionChangeRequest.project_id == project.id))
    if change is None:
        db.rollback()
        raise ApiError(404, "not_found", "Change not found.")
    if _resolution_of(db, change.id) is not None:
        db.rollback()
        return change
    db.add(InstructionChangeResolution(change_id=change.id, project_id=project.id, resolution=ChangeResolution.WITHDRAWN.value,
                                       actor_role=AuditActor.SELLER.value, note=body.note.strip()))
    record_audit(db, project.id, "INSTRUCTION_CHANGE_WITHDRAWN", AuditActor.SELLER, "instruction_change", change.id,
                 {"instruction_in_force_unchanged": True})
    db.commit()
    return change


# --- enforcement ----------------------------------------------------------------------------------------------------

def _mismatched_fields(row, recipient: str, currency: str, amount: int) -> list[str]:
    given = {"recipient_email": recipient, "currency": currency, "amount_minor": amount}
    return [name for name in _COMPARED if getattr(row, name) != given[name]]


def raise_if_held(db: Session, project_id: str, provider: PaymentProvider, operation: str, *, required: bool,
                  ignore_pending: bool = False) -> None:
    hold = compute_hold(db, project_id, provider, required=required, ignore_pending=ignore_pending)
    if hold.active:
        raise _block(db, project_id, operation, "payment_hold_active", [r.value for r in hold.reasons])


def gate_create(db: Session, provider: PaymentProvider, project: Project, *, recipient: str, currency: str, amount: int,
                required: bool) -> PaymentInstruction | None:
    """Called inside the reservation transaction. Takes the project lock, refuses on a hold or on any deviation from the
    recorded instruction, and records an implicit instruction when none exists and none is required."""
    lock_project(db, project.id)
    raise_if_held(db, project.id, provider, "CREATE_INVOICE", required=required)
    current = effective_instruction(db, project.id)
    if current is not None:
        diff = _mismatched_fields(current, recipient, currency, amount)
        if diff:
            raise _block(db, project.id, "CREATE_INVOICE", "instruction_mismatch", diff)
        return current
    merchant = _provider_merchant(provider, None)
    return _insert_version(db, project, provider, merchant, recipient, currency, amount, InstructionProvenance.INVOICE_REQUEST, None, None)


def gate_existing(db: Session, provider: PaymentProvider, project: Project, rec: PaymentRecord, operation: str, *, required: bool,
                  ignore_pending: bool = False) -> None:
    """Pre-check for an operation on an existing payment (send, resume): hold, record/instruction consistency, supersession.
    Call under `lock_project`, in the same transaction as the claim."""
    raise_if_held(db, project.id, provider, operation, required=required, ignore_pending=ignore_pending)
    current = effective_instruction(db, project.id)
    diff: list[str] = []
    if rec.instruction_id is not None:
        bound = db.get(PaymentInstruction, rec.instruction_id)
        if bound is None or bound.project_id != project.id or bound.fingerprint != rec.instruction_fingerprint:
            diff.append("instruction_fingerprint")
    if current is not None:
        diff += _mismatched_fields(current, rec.recipient_email, rec.currency, rec.amount_minor)
        if rec.instruction_id is not None and rec.instruction_id != current.id:
            diff.append("instruction_superseded")
    if diff:
        raise _block(db, project.id, operation, "instruction_mismatch", diff)


def reconciliation_inputs(db: Session, rec: PaymentRecord, provider: PaymentProvider) -> tuple[str | None, tuple[str, ...]]:
    """(expected merchant, extra anomaly codes) for `reconcile.evaluate`. The payee provider evidence is judged against is the one
    recorded in the instruction, so a later change of the configured merchant cannot make a foreign payee look expected."""
    if rec.instruction_id is None:
        return provider.expected_merchant_email, ()
    bound = db.get(PaymentInstruction, rec.instruction_id)
    versions = versions_of(db, rec.project_id)
    if (bound is None or bound.project_id != rec.project_id or instruction_fingerprint(bound) != bound.fingerprint
            or bound.fingerprint != rec.instruction_fingerprint or not chain_intact(versions)
            or _mismatched_fields(bound, rec.recipient_email, rec.currency, rec.amount_minor)):
        return provider.expected_merchant_email, ("INSTRUCTION_TAMPERED",)
    return bound.merchant_email or provider.expected_merchant_email, ()
