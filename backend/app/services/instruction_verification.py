"""Buyer-side verification of payment-instruction changes (R2). Trust model and limits: docs/R2_AUTHORIZATION.md.

A change proposed by the seller is released (applied or rejected) ONLY by an `InstructionChangeVerification` row, which this module writes
after a TOTP check against an operator-established authenticator. The row, the new instruction version, the consumed challenge, the consumed
TOTP step and the audit event are written in ONE transaction under the project write lock; any failure rolls all of it back, so the hold stays.
Every refusal returns the same non-informative error so a caller learns nothing about which check failed (the reason goes to the audit log)."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import schemas
from app.errors import ApiError
from app.models import (
    BuyerAuthenticator, InstructionChangeRequest, InstructionChangeVerification, InstructionVerificationChallenge, PaymentInstruction,
    TotpUse, new_id,
)
from app.payment_provider import PaymentProvider
from app.schemas import AuditActor, InstructionDecision, VerificationOutcome
from app.security import normalize_email
from app.services import authenticators as auth
from app.services import instructions
from app.services.scope import record_audit

log = logging.getLogger("app.audit")
CHALLENGE_TTL = timedelta(minutes=5)
MAX_FAILED_ATTEMPTS = 5
MAX_CHALLENGES_PER_HOUR = 10  # per authenticator, durable (counted from the database)
MAX_FAILURES_PER_WINDOW = 10  # wrong-code attempts summed over ALL of an authenticator's challenges in the window (durable lockout)
FAILURE_WINDOW = timedelta(minutes=15)


def _locked_out(db: Session, authenticator_id: str) -> bool:
    """Guessing is bounded per authenticator, not just per challenge: a fresh challenge does not reset the budget."""
    since = utcnow() - FAILURE_WINDOW
    spent = db.scalar(select(func.coalesce(func.sum(InstructionVerificationChallenge.failed_attempts), 0)).where(
        InstructionVerificationChallenge.authenticator_id == authenticator_id, InstructionVerificationChallenge.created_at > since))
    return spent >= MAX_FAILURES_PER_WINDOW


def utcnow() -> datetime:
    return datetime.now(timezone.utc)  # indirection so tests can pin the clock


def _uniform_failure() -> ApiError:
    return ApiError(400, "verification_failed", "The verification could not be completed. The payment hold is unchanged.")


def _busy() -> ApiError:
    """The database could not serve the request (e.g. SQLite write-lock contention). The answer does not depend on any input, so it is not an
    oracle; nothing was committed that changes a hold, and the caller may retry."""
    return ApiError(503, "verification_busy", "Verification is temporarily busy. The payment hold is unchanged; retry shortly.",
                    headers={"Retry-After": "2"})


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "No such instruction change.")


def _fail(db: Session, project_id: str | None, reason: str, subject_id: str) -> ApiError:
    """Discard everything staged, audit the refusal (internal reason only), and return the uniform error."""
    db.rollback()
    if project_id is not None:
        record_audit(db, project_id, "INSTRUCTION_VERIFICATION_FAILED", AuditActor.SYSTEM, "instruction_change", subject_id,
                     {"reason": reason})
        db.commit()
    return _uniform_failure()


def _values(row, merchant: str | None) -> schemas.InstructionValues:
    return schemas.InstructionValues(merchant_email=merchant, recipient_email=row.recipient_email, currency=row.currency,
                                     amount_minor=row.amount_minor)


def _proposed_fingerprint(current: PaymentInstruction, change: InstructionChangeRequest) -> str:
    return instructions.compute_fingerprint(change.project_id, current.provider, change.merchant_email, change.recipient_email,
                                            change.currency, change.amount_minor, current.context or {})


def _change_is_consistent(db: Session, current: PaymentInstruction | None, change: InstructionChangeRequest) -> bool:
    """The stored change must still describe a transition FROM the instruction in force, with an untampered chain and fingerprint."""
    if current is None or current.fingerprint != change.from_fingerprint or current.id != change.from_instruction_id:
        return False
    if not instructions.chain_intact(instructions.versions_of(db, change.project_id)):
        return False
    return _proposed_fingerprint(current, change) == change.proposed_fingerprint


def _withdrawn(db: Session, change_id: str) -> bool:
    return instructions._resolution_of(db, change_id) is not None


def _allowed(db: Session, authenticator: BuyerAuthenticator, change: InstructionChangeRequest) -> list[InstructionDecision]:
    """An authenticator enrolled after the proposal, or a withdrawn proposal, may only be rejected - never approved."""
    approvable = authenticator.created_at < change.created_at and not _withdrawn(db, change.id)
    return ([InstructionDecision.APPROVE_INSTRUCTION_CHANGE] if approvable else []) + [InstructionDecision.REJECT_INSTRUCTION_CHANGE]


# --- challenge ------------------------------------------------------------------------------------------------------

def challenge(db: Session, project_id: str, change_id: str, buyer_email: str) -> schemas.InstructionChallengeResponse:
    try:
        return _challenge(db, project_id, change_id, buyer_email)
    except OperationalError:
        db.rollback()
        log.warning("verification_db_busy stage=challenge")
        raise _busy() from None


def _challenge(db: Session, project_id: str, change_id: str, buyer_email: str) -> schemas.InstructionChallengeResponse:
    email = normalize_email(buyer_email)
    instructions.lock_project(db, project_id)
    change = db.scalar(select(InstructionChangeRequest).where(InstructionChangeRequest.id == change_id,
                                                              InstructionChangeRequest.project_id == project_id))
    current = instructions.effective_instruction(db, project_id)
    authenticator = auth.active_authenticator(db, project_id, email)
    if (change is None or authenticator is None or current is None or email != current.recipient_email
            or instructions.verification_of(db, change.id) is not None or not _change_is_consistent(db, current, change)):
        db.rollback()
        raise _not_found()
    if _locked_out(db, authenticator.id):
        db.rollback()
        log.warning("verification_authenticator_locked_out authenticator=%s stage=challenge", authenticator.id)
        raise _not_found()
    open_row = db.scalar(select(InstructionVerificationChallenge).where(
        InstructionVerificationChallenge.authenticator_id == authenticator.id, InstructionVerificationChallenge.change_id == change.id,
        InstructionVerificationChallenge.consumed_at.is_(None), InstructionVerificationChallenge.expires_at > utcnow(),
        InstructionVerificationChallenge.failed_attempts < MAX_FAILED_ATTEMPTS)
        .order_by(InstructionVerificationChallenge.created_at.desc()).limit(1))
    since = utcnow() - timedelta(hours=1)
    issued = db.scalar(select(func.count()).select_from(InstructionVerificationChallenge).where(
        InstructionVerificationChallenge.authenticator_id == authenticator.id, InstructionVerificationChallenge.created_at > since))
    if open_row is None and issued >= MAX_CHALLENGES_PER_HOUR:
        # Same answer as "no such change": a distinct 429 here would tell a caller who knows (project, change, address) that an authenticator
        # exists. The operational signal goes to the log (authenticator id only, no address).
        db.rollback()
        log.warning("verification_challenge_limited authenticator=%s", authenticator.id)
        raise _not_found()
    row = open_row  # an unexpired challenge is reused, so repeated requests cannot burn the issue budget (flooding)
    if row is None:
        row = InstructionVerificationChallenge(
            id=new_id(), project_id=project_id, change_id=change.id, authenticator_id=authenticator.id, approver_email=email,
            from_fingerprint=change.from_fingerprint, proposed_fingerprint=change.proposed_fingerprint, expires_at=utcnow() + CHALLENGE_TTL)
        db.add(row)
        db.flush()
        record_audit(db, project_id, "INSTRUCTION_VERIFICATION_CHALLENGED", AuditActor.SYSTEM, "instruction_change", change.id,
                     {"challenge_id": row.id})
    response = schemas.InstructionChallengeResponse(
        challenge_id=row.id, expires_at=row.expires_at,
        change=schemas.InstructionChangeForBuyer(
            project_id=project_id, change_id=change.id, current=_values(current, current.merchant_email),
            proposed=_values(change, change.merchant_email), from_fingerprint=change.from_fingerprint,
            proposed_fingerprint=change.proposed_fingerprint, seller_reason=change.reason,
            withdrawn_by_seller=_withdrawn(db, change.id),
            approvable=InstructionDecision.APPROVE_INSTRUCTION_CHANGE in _allowed(db, authenticator, change)),
        allowed_decisions=_allowed(db, authenticator, change))
    db.commit()
    return response


# --- decision -------------------------------------------------------------------------------------------------------

def _precharge(db: Session, body: schemas.InstructionDecideRequest) -> InstructionVerificationChallenge:
    """Charge one attempt to the challenge and COMMIT it before the code is looked at, so a crash or a concurrent guess cannot get a free try."""
    challenge_row = db.scalar(select(InstructionVerificationChallenge).where(InstructionVerificationChallenge.id == body.challenge_id))
    if challenge_row is None:
        db.rollback()
        raise _uniform_failure()
    if _locked_out(db, challenge_row.authenticator_id):
        db.rollback()
        log.warning("verification_authenticator_locked_out authenticator=%s stage=decide", challenge_row.authenticator_id)
        raise _uniform_failure()
    charged = db.execute(update(InstructionVerificationChallenge).where(
        InstructionVerificationChallenge.id == challenge_row.id, InstructionVerificationChallenge.consumed_at.is_(None),
        InstructionVerificationChallenge.expires_at > utcnow(),
        InstructionVerificationChallenge.failed_attempts < MAX_FAILED_ATTEMPTS)
        .values(failed_attempts=InstructionVerificationChallenge.failed_attempts + 1).execution_options(synchronize_session=False))
    if charged.rowcount != 1:
        db.rollback()  # not audited: a spent or expired challenge id must not be a way to grow the audit table
        raise _uniform_failure()
    db.commit()
    return challenge_row


def _challenge_matches(row: InstructionVerificationChallenge, body: schemas.InstructionDecideRequest, email: str) -> bool:
    return (row.project_id == body.project_id and row.change_id == body.change_id and row.approver_email == email
            and row.from_fingerprint == body.from_fingerprint and row.proposed_fingerprint == body.proposed_fingerprint)


def decide(db: Session, provider: PaymentProvider, keyring: auth.Keyring | None,
           body: schemas.InstructionDecideRequest) -> schemas.InstructionDecideResponse:
    email = normalize_email(body.buyer_email)
    try:
        charged = _precharge(db, body)
    except OperationalError:
        db.rollback()
        log.warning("verification_db_busy stage=precharge")
        raise _busy() from None
    project_id, change_id = charged.project_id, charged.change_id
    try:
        return _decide_locked(db, provider, keyring, body, email, project_id)
    except ApiError:
        raise
    except OperationalError:  # e.g. SQLite busy under contention: closed (nothing committed), input-independent answer
        db.rollback()
        log.warning("verification_db_busy stage=decide")
        raise _busy() from None
    except IntegrityError:  # a unique backstop (change, challenge, TOTP step) fired: someone else won the race
        try:
            raise _fail(db, project_id, "integrity_backstop", change_id) from None
        except OperationalError:
            db.rollback()
            raise _busy() from None
    except Exception:
        db.rollback()  # nothing half-written may survive; the hold stays
        raise


def _decide_locked(db: Session, provider: PaymentProvider, keyring: auth.Keyring | None, body: schemas.InstructionDecideRequest,
                   email: str, project_id: str) -> schemas.InstructionDecideResponse:
    instructions.lock_project(db, project_id)
    db.expire_all()
    row = db.get(InstructionVerificationChallenge, body.challenge_id, populate_existing=True)
    change = db.get(InstructionChangeRequest, row.change_id, populate_existing=True)
    fail = lambda reason: _fail(db, project_id, reason, row.change_id)  # noqa: E731
    if row.consumed_at is not None or row.expires_at <= utcnow():
        raise fail("challenge_consumed_or_expired")
    if not _challenge_matches(row, body, email):
        raise fail("challenge_binding_mismatch")
    authenticator = db.get(BuyerAuthenticator, row.authenticator_id)
    current = instructions.effective_instruction(db, project_id)
    if (authenticator is None or auth.revocation_of(db, authenticator.id) is not None or authenticator.buyer_email != email
            or current is None or current.recipient_email != email):
        raise fail("authenticator_or_actor_invalid")
    if instructions.verification_of(db, change.id) is not None:
        raise fail("already_resolved")
    if not _change_is_consistent(db, current, change) or change.proposed_fingerprint != body.proposed_fingerprint:
        raise fail("change_inconsistent")
    approve = body.decision is InstructionDecision.APPROVE_INSTRUCTION_CHANGE
    if body.decision not in _allowed(db, authenticator, change):
        raise fail("decision_not_allowed")
    if approve and (provider.name.value != current.provider or change.merchant_email != provider.expected_merchant_email):
        raise fail("provider_or_merchant_mismatch")
    try:
        secret = auth.decrypt_secret(keyring, authenticator)
    except auth.AuthenticatorUnavailable:
        db.rollback()
        raise ApiError(503, "authenticator_unavailable", "Verification is unavailable; the payment hold is unchanged.") from None
    step = auth.matching_step(secret, body.totp_code, auth.now())
    if step is None:
        raise fail("code_incorrect")
    last_used = db.scalar(select(func.max(TotpUse.step)).where(TotpUse.authenticator_id == authenticator.id))
    if last_used is not None and step <= last_used:
        raise fail("code_replayed")
    db.add(TotpUse(authenticator_id=authenticator.id, step=step, challenge_id=row.id))
    db.flush()
    consumed = db.execute(update(InstructionVerificationChallenge).where(
        InstructionVerificationChallenge.id == row.id, InstructionVerificationChallenge.consumed_at.is_(None),
        InstructionVerificationChallenge.expires_at > utcnow()).values(consumed_at=utcnow()).execution_options(synchronize_session=False))
    if consumed.rowcount != 1:
        raise fail("challenge_consumed_concurrently")
    # A successful decision is not a failed guess: credit back the attempt that was pre-charged, so a buyer with several changes to decide
    # is not locked out by their own successes. Same transaction as the release.
    db.execute(update(InstructionVerificationChallenge).where(InstructionVerificationChallenge.id == row.id,
                                                              InstructionVerificationChallenge.failed_attempts > 0)
               .values(failed_attempts=InstructionVerificationChallenge.failed_attempts - 1).execution_options(synchronize_session=False))
    response = _record_outcome(db, project_id, change, current, authenticator, row, approve)
    hold = instructions.compute_hold(db, project_id, provider)
    return response.model_copy(update={"hold_active": hold.active})


def _record_outcome(db: Session, project_id: str, change: InstructionChangeRequest, current: PaymentInstruction,
                    authenticator: BuyerAuthenticator, challenge_row: InstructionVerificationChallenge,
                    approve: bool) -> schemas.InstructionDecideResponse:
    applied_id: str | None = None
    if approve:
        version = current.version + 1
        applied = PaymentInstruction(
            id=new_id(), project_id=project_id, version=version, provider=current.provider, merchant_email=change.merchant_email,
            recipient_email=change.recipient_email, currency=change.currency, amount_minor=change.amount_minor,
            context=current.context, fingerprint=change.proposed_fingerprint, previous_instruction_id=current.id,
            chain_sha256=instructions.compute_chain(current.chain_sha256, change.proposed_fingerprint, version, project_id),
            provenance=schemas.InstructionProvenance.SELLER_ENTERED.value, idempotency_key=None, request_sha256=None)
        db.add(applied)
        db.flush()
        applied_id = applied.id
    outcome = VerificationOutcome.APPLIED if approve else VerificationOutcome.REJECTED
    verification = InstructionChangeVerification(
        change_id=change.id, project_id=project_id, outcome=outcome.value, authenticator_id=authenticator.id,
        challenge_id=challenge_row.id, approver_email=challenge_row.approver_email, from_fingerprint=change.from_fingerprint,
        proposed_fingerprint=change.proposed_fingerprint, trust_basis=authenticator.trust_basis, applied_instruction_id=applied_id)
    db.add(verification)
    db.flush()
    record_audit(db, project_id, "INSTRUCTION_CHANGE_VERIFIED", AuditActor.SYSTEM, "instruction_change", change.id,
                 {"outcome": outcome.value, "verification_id": verification.id, "applied_instruction_id": applied_id,
                  "from_fingerprint": change.from_fingerprint, "proposed_fingerprint": change.proposed_fingerprint,
                  "trust_basis": authenticator.trust_basis, "real_world_identity_verified": False})
    view = schemas.InstructionChangeVerificationView.model_validate(verification)
    db.commit()
    return schemas.InstructionDecideResponse(outcome=outcome, verification=view, hold_active=True)  # recomputed by the caller after the commit
