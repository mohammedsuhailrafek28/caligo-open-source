"""Invoice creation, explicit sending, and server-side synchronization with the payment provider.

Invariants:
* No database transaction is open while a provider network call is in flight: every provider operation follows
  reserve -> COMMIT -> call -> record. State needed to resume after a crash or an ambiguous outcome is committed first.
* A state-changing provider call is never retried blindly. Ambiguous outcomes are persisted: CREATE_UNCERTAIN is
  resolved by resuming with the same Idempotency-Key (same provider request id); SEND_PENDING / SEND_UNCERTAIN are
  resolved by synchronization once the provider has reported DRAFT for the settle window.
* Financial state (PAID, refunds, ...) is derived only by `reconcile.evaluate/apply` from provider evidence.
* Payment rows and payment_events are never deleted.
"""
import hashlib
import json
import logging
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.errors import ApiError
from app.models import PaymentRecord, Project, ScopeRecord, new_id, utcnow
from app.payment_provider import (
    PaymentProvider, ProviderAmbiguousOutcome, ProviderAuthError, ProviderError, ProviderInvoice,
    ProviderInvoiceRequest, ProviderRateLimited, ProviderRejected,
)
from app.schemas import (
    InvoiceRequest, OperationState as Op, PaymentEventType as Ev, PaymentProviderName,
    PaymentProviderStatus as S, ProjectStatus, ProviderInvoiceState, ReviewResolution, ReviewResolutionRequest,
    ScopeKind, SendInvoiceRequest,
)
from app.security import normalize_email
from app.services import instructions, reconcile
from app.services import observations
from app.services.observations import ObservationStorageError

log = logging.getLogger("app.audit")
IN_FLIGHT_STALE_AFTER = timedelta(seconds=300)  # above the worst-case provider call time (bounded retries)  # a CREATING row older than this was abandoned (crash) and may be resumed
_INACTIVE = (S.CANCELLED.value, S.REFUNDED.value)


# --- helpers ---------------------------------------------------------------------------------

def request_fingerprint(req: InvoiceRequest) -> str:
    canonical = json.dumps(
        {"currency": req.currency, "amount_minor": req.amount_minor,
         "recipient_email": normalize_email(req.recipient_email), "replaces": req.replaces_payment_id},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf8")).hexdigest()


def invoice_number_for(payment_id: str) -> str:
    """Deterministic and unique per payment record, so a retried create can never produce a second invoice."""
    return "PEM-" + hashlib.sha256(payment_id.encode("utf8")).hexdigest()[:20].upper()


def _api_error(exc: ProviderError) -> ApiError:
    """Map provider failures to generic, non-leaking API errors."""
    if isinstance(exc, ProviderAuthError):
        return ApiError(502, "provider_auth_failed", "Authentication with the payment provider failed.")
    if isinstance(exc, ProviderRateLimited):
        return ApiError(503, "provider_rate_limited", "The payment provider is rate limiting requests. Try again shortly.",
                        headers={"Retry-After": "30"})
    if isinstance(exc, ProviderRejected):
        return ApiError(422, "provider_rejected", f"The payment provider rejected the request ({exc.code}).")
    return ApiError(502, "provider_unavailable", "The payment provider could not be reached.")


def _active_invoice_id(db: Session, project_id: str) -> str | None:
    return db.scalar(select(PaymentRecord.id).where(
        PaymentRecord.project_id == project_id, PaymentRecord.provider_status.notin_(_INACTIVE)))


def _claim(db: Session, rec: PaymentRecord, from_states: tuple[str, ...], to_state: Op, *,
           also: list | None = None, stale_state: Op | None = None, enforce_hold: bool = True) -> bool:
    """Atomically move the operation state (compare-and-set) and COMMIT, so no transaction stays open.
    The UPDATE only matches if the stored recipient, currency, amount and instruction binding still equal the values this operation
    was checked against (a reserved operation can never be redirected). `enforce_hold` (sends) also requires that no instruction
    change holds the project; a create that was durably reserved before the hold passes enforce_hold=False and completes against
    its bound instruction (the in-flight rule)."""
    # NOTE: `rec` must hold the values the operation was CHECKED against. Never refresh()/expire() it between the gate and this
    # claim, or the bound-value comparison below would become trivially true.
    bound = [PaymentRecord.recipient_email == rec.recipient_email, PaymentRecord.currency == rec.currency,
             PaymentRecord.amount_minor == rec.amount_minor, PaymentRecord.instruction_id == rec.instruction_id,
             PaymentRecord.instruction_fingerprint == rec.instruction_fingerprint]
    also = [*(also or []), *bound, *([instructions.no_pending_change_clause(rec.project_id)] if enforce_hold else [])]
    now = utcnow()
    allowed = PaymentRecord.operation_state.in_(from_states)
    if stale_state is not None:
        allowed = or_(allowed, and_(PaymentRecord.operation_state == stale_state.value,
                                    PaymentRecord.operation_started_at < now - IN_FLIGHT_STALE_AFTER))
    result = db.execute(
        update(PaymentRecord).where(PaymentRecord.id == rec.id, allowed, *(also or []))
        .values(operation_state=to_state.value, operation_started_at=now, version=PaymentRecord.version + 1)
        .execution_options(synchronize_session=False))
    db.commit()
    if result.rowcount != 1:
        return False
    db.refresh(rec)
    db.commit()  # refresh opened a transaction; end it before any network call
    return True


def _settle(db: Session, rec_id: str, mutate, attempts: int = 3) -> PaymentRecord:
    """Re-read the record, apply `mutate(rec)`, commit; retry on a concurrent version conflict."""
    for _ in range(attempts):
        try:
            rec = db.get(PaymentRecord, rec_id)
            db.refresh(rec)
            mutate(rec)
            db.commit()
            return rec
        except StaleDataError:
            db.rollback()
        except ObservationStorageError:  # evidence could not be stored: fail closed, keep the stored state
            db.rollback()
            log.error("evidence_storage_failed payment_id=%s", rec_id)
            raise ApiError(500, "evidence_storage_failed", "The provider evidence could not be stored; the payment's stored state is unchanged.") from None
    raise ApiError(409, "sync_conflict", "The payment changed concurrently. Retry.")


# --- create -----------------------------------------------------------------------------------

def create_invoice(db: Session, provider: PaymentProvider, project: Project, req: InvoiceRequest, key: str, *,
                   recipient_allowlist: list[str], instruction_required: bool = False) -> tuple[PaymentRecord, bool]:
    """Returns (record, created). Same key + same payload replays the stored record, resuming it if an earlier
    attempt was interrupted. The invoice is created as a DRAFT and nobody is emailed."""
    fingerprint = request_fingerprint(req)
    found = db.scalar(select(PaymentRecord).where(
        PaymentRecord.project_id == project.id, PaymentRecord.idempotency_key == key))
    if found is not None:
        if found.request_fingerprint != fingerprint:
            raise ApiError(409, "idempotency_key_conflict", "This Idempotency-Key was already used with a different request.")
        return _resume_create(db, provider, project, found, recipient_allowlist, instruction_required)

    sandbox = provider.name is PaymentProviderName.PAYPAL_SANDBOX
    recipient = normalize_email(req.recipient_email)
    if sandbox:
        from app.paypal_sandbox import SUPPORTED_CURRENCIES
        if req.currency not in SUPPORTED_CURRENCIES:
            raise ApiError(422, "unsupported_currency", "This currency is not supported for PayPal invoicing.")
        if recipient not in recipient_allowlist:
            raise ApiError(422, "recipient_not_allowed",
                           "Sandbox invoices may only be addressed to approved test identities (PAYPAL_SANDBOX_RECIPIENT_ALLOWLIST).")

    prior = list(db.scalars(select(PaymentRecord).where(PaymentRecord.project_id == project.id)))
    if any(r.provider_status not in _INACTIVE for r in prior):
        raise ApiError(409, "project_already_invoiced", "This project already has an active invoice.")
    provider_backed = {r.id for r in prior if r.paypal_invoice_id is not None}
    if provider_backed and req.replaces_payment_id is None:
        raise ApiError(409, "replacement_required",
                       "The previous invoice is cancelled or refunded. Create a replacement explicitly with replaces_payment_id.")
    if req.replaces_payment_id is not None and req.replaces_payment_id not in provider_backed:
        raise ApiError(404, "replaced_payment_not_found", "No such previous invoice to replace on this project.")
    if any(r.id == req.replaces_payment_id and "PAID_AFTER_CANCEL" in (r.verification_note or "") for r in prior):
        raise ApiError(409, "replacement_blocked_review",
                       "The cancelled invoice shows payment activity. Resolve it with the provider before replacing it.")

    scope = db.scalar(select(ScopeRecord).where(
        ScopeRecord.project_id == project.id, ScopeRecord.kind == ScopeKind.ORIGINAL.value)
        .order_by(ScopeRecord.created_at, ScopeRecord.id).limit(1))
    if sandbox and scope is None:
        raise ApiError(409, "original_scope_required", "Record the original agreement (scope kind ORIGINAL) before invoicing.")

    instruction = instructions.gate_create(db, provider, project, recipient=recipient, currency=req.currency,
                                           amount=req.amount_minor, required=instruction_required)
    rec_id = new_id()
    record = PaymentRecord(
        id=rec_id, project_id=project.id, provider=provider.name.value, idempotency_key=key,
        request_fingerprint=fingerprint, recipient_email=recipient, invoice_number=invoice_number_for(rec_id),
        currency=req.currency, amount_minor=req.amount_minor, provider_status=S.UNVERIFIED.value,
        operation_state=Op.RESERVED.value, replaces_payment_id=req.replaces_payment_id,
        instruction_id=instruction.id if instruction else None,
        instruction_fingerprint=instruction.fingerprint if instruction else None,
        scope_record_id=scope.id if scope else None, scope_sha256=scope.source_sha256 if scope else None)
    db.add(record)
    try:
        db.flush()
        db.commit()  # reservation is durable BEFORE the provider is contacted
    except IntegrityError:
        db.rollback()
        again = db.scalar(select(PaymentRecord).where(
            PaymentRecord.project_id == project.id, PaymentRecord.idempotency_key == key))
        if again is not None:
            if again.request_fingerprint != fingerprint:
                raise ApiError(409, "idempotency_key_conflict", "This Idempotency-Key was already used with a different request.")
            return again, False
        if _active_invoice_id(db, project.id) is not None:
            raise ApiError(409, "project_already_invoiced", "This project already has an active invoice.")
        raise
    request, record_id = _provider_request(project, record), record.id
    # Linearization point of the create is the reservation commit above. From here the operation may complete against its bound
    # instruction even if a hold was placed since; integrity, merchant and provider conditions are still re-checked under the lock.
    instructions.lock_project(db, project.id)
    instructions.gate_existing(db, provider, project, record, "CREATE_INVOICE", required=instruction_required, ignore_pending=True)
    if not _claim(db, record, (Op.RESERVED.value,), Op.CREATING, enforce_hold=False):
        raise ApiError(409, "operation_in_progress", "This invoice is already being created.")
    return _execute_create(db, provider, project, record_id, request), True


def _resume_create(db: Session, provider: PaymentProvider, project: Project, rec: PaymentRecord,
                   recipient_allowlist: list[str], instruction_required: bool = False):
    if rec.provider_status == S.NEEDS_REVIEW.value:
        raise ApiError(409, "review_required", "This payment needs an explicit review resolution before it can continue.")
    if rec.operation_state == Op.CREATE_FAILED.value:
        raise ApiError(409, "idempotency_key_failed",
                       "The provider rejected the original request for this Idempotency-Key. Use a new key.")
    if rec.operation_state == Op.CREATING.value and rec.operation_started_at is not None             and rec.operation_started_at >= utcnow() - IN_FLIGHT_STALE_AFTER:
        raise ApiError(409, "operation_in_progress", "This invoice is already being created.")
    resumable = rec.operation_state in (Op.RESERVED.value, Op.CREATE_UNCERTAIN.value) or (
        rec.operation_state == Op.CREATING.value and rec.operation_started_at is not None
        and rec.operation_started_at < utcnow() - IN_FLIGHT_STALE_AFTER)
    if not resumable or rec.paypal_invoice_id is not None:
        return rec, False
    if provider.name is not PaymentProviderName(rec.provider):
        raise ApiError(409, "provider_mismatch", "This payment belongs to a different payment provider mode.")
    if provider.name is PaymentProviderName.PAYPAL_SANDBOX and rec.recipient_email not in recipient_allowlist:
        raise ApiError(422, "recipient_not_allowed", "Sandbox invoices may only be addressed to approved test identities.")
    instructions.lock_project(db, project.id)
    instructions.gate_existing(db, provider, project, rec, "CREATE_INVOICE", required=instruction_required, ignore_pending=True)
    uncertain = rec.operation_state != Op.RESERVED.value
    request = _provider_request(project, rec)
    rec_id = rec.id
    if not _claim(db, rec, (Op.RESERVED.value, Op.CREATE_UNCERTAIN.value), Op.CREATING, stale_state=Op.CREATING, enforce_hold=False):
        raise ApiError(409, "operation_in_progress", "This invoice is already being created.")
    return _execute_create(db, provider, project, rec_id, request, resumed=uncertain), True


def _provider_request(project: Project, rec: PaymentRecord) -> ProviderInvoiceRequest:
    """Plain values only: ORM objects expire on commit and must not be touched during the provider call."""
    return ProviderInvoiceRequest(
        idempotency_key=f"pem-{rec.id}",  # stable across every resume of this payment record
        invoice_number=rec.invoice_number or invoice_number_for(rec.id), currency=rec.currency,
        amount_minor=rec.amount_minor, recipient_email=rec.recipient_email, item_name=project.title,
        memo=f"Project {project.id[:8]}" + (f"; scope sha256 {rec.scope_sha256[:12]}" if rec.scope_sha256 else ""),
        reference=f"PEM-{project.id[:8]}")


def _execute_create(db: Session, provider: PaymentProvider, project: Project, rec_id: str,
                    request: ProviderInvoiceRequest, resumed: bool = False) -> PaymentRecord:
    try:
        invoice = provider.create_invoice(request)  # NOTE: no database transaction is open here
    except ProviderRejected as exc:
        # After an earlier ambiguous attempt ANY rejection may mean "it already exists": never treat it as a clean failure.
        if resumed or exc.code == "DUPLICATE_INVOICE_NUMBER":  # the first attempt probably succeeded: a human must reconcile
            _fail(db, rec_id, Op.CREATE_UNCERTAIN, S.NEEDS_REVIEW, "REJECTED_AFTER_UNCERTAIN_CREATE_REVIEW", Ev.OPERATION_UNCERTAIN)
            raise ApiError(502, "provider_outcome_uncertain",
                           "The provider's answer is inconsistent with an earlier uncertain attempt. Review the invoice in the provider dashboard.")
        _fail(db, rec_id, Op.CREATE_FAILED, S.CANCELLED, f"PROVIDER_REJECTED:{exc.code}", Ev.OPERATION_FAILED)
        raise _api_error(exc)
    except ProviderAmbiguousOutcome as exc:
        if exc.invoice_id:  # created, but not yet verified: never lose the provider id
            def keep(r: PaymentRecord) -> None:
                r.paypal_invoice_id, r.operation_state = exc.invoice_id, Op.READY.value
                r.verification_note = "CREATED_UNVERIFIED_SYNC_REQUIRED"
                reconcile.record_event(db, r, Ev.OPERATION_UNCERTAIN, actor="SELLER", note=r.verification_note)
            return _settle(db, rec_id, keep)
        _fail(db, rec_id, Op.CREATE_UNCERTAIN, None, "CREATE_OUTCOME_UNKNOWN", Ev.OPERATION_UNCERTAIN)
        raise ApiError(502, "provider_outcome_uncertain",
                       "The provider did not confirm the invoice. Retry with the same Idempotency-Key to resume safely.")
    except ProviderError as exc:  # auth / rate limit / unreachable: the request provably did not run
        _fail(db, rec_id, Op.RESERVED, None, f"PROVIDER_{type(exc).__name__.upper()}", Ev.OPERATION_FAILED)
        raise _api_error(exc)
    except AssertionError:
        raise
    except Exception as exc:
        log.error("provider_call_failed error_type=%s payment_id=%s", type(exc).__name__, rec_id)
        _fail(db, rec_id, Op.CREATE_UNCERTAIN, None, "CREATE_OUTCOME_UNKNOWN", Ev.OPERATION_UNCERTAIN)
        raise ApiError(502, "provider_outcome_uncertain",
                       "The provider did not confirm the invoice. Retry with the same Idempotency-Key to resume safely.")

    def record(r: PaymentRecord) -> None:
        r.paypal_invoice_id = invoice.invoice_id
        r.operation_state = Op.READY.value
        if invoice.state is ProviderInvoiceState.DRAFT:
            evaluation = reconcile.evaluate(r, invoice, *instructions.reconciliation_inputs(db, r, provider))
        else:  # a freshly created invoice must be a draft; anything else may already have emailed someone
            evaluation = reconcile.Evaluation(None, ("CREATED_NOT_IN_DRAFT",), state=invoice.state)
        reconcile.apply(db, r, project, invoice, evaluation, actor="SELLER")
        reconcile.record_event(db, r, Ev.INVOICE_CREATED, to_status=r.provider_status, actor="SELLER",
                               note=r.verification_note)

    try:
        created = _settle(db, rec_id, record)
    except ApiError as exc:
        if exc.code == "evidence_storage_failed":
            # The provider already created the invoice but we could not record it. Mark the outcome uncertain so a client retry with the
            # SAME Idempotency-Key resumes safely (the provider deduplicates on its request id). Nothing is retried automatically.
            _fail(db, rec_id, Op.CREATE_UNCERTAIN, None, "CREATE_OUTCOME_UNKNOWN", Ev.OPERATION_UNCERTAIN)
        raise
    if created.provider_status == S.NEEDS_REVIEW.value:
        raise ApiError(502, "provider_response_mismatch",
                       "The provider returned data that does not match the request. The invoice was kept for review.")
    log.info("invoice_created payment_id=%s provider=%s", created.id, created.provider)
    return created


def _fail(db: Session, rec_id: str, op: Op, status: S | None, note: str, event: Ev) -> None:
    def mutate(r: PaymentRecord) -> None:
        from_status = r.provider_status
        r.operation_state = op.value
        if status is not None:
            r.provider_status = status.value
        r.verification_note = note[:255]
        reconcile.record_event(db, r, event, from_status=from_status, to_status=r.provider_status, actor="SELLER", note=note)
    _settle(db, rec_id, mutate)


# --- send -------------------------------------------------------------------------------------

def send_invoice(db: Session, provider: PaymentProvider, project: Project, rec: PaymentRecord,
                 req: SendInvoiceRequest, *, recipient_allowlist: list[str], instruction_required: bool = False) -> PaymentRecord:
    """Explicit seller action: this is the only code path that can make the provider email a person."""
    if normalize_email(req.confirm_recipient_email) != rec.recipient_email:
        raise ApiError(422, "recipient_confirmation_mismatch", "The confirmation address does not match the invoice recipient.")
    if provider.name is not PaymentProviderName(rec.provider):
        raise ApiError(409, "provider_mismatch", "This payment belongs to a different payment provider mode.")
    if provider.name is PaymentProviderName.PAYPAL_SANDBOX and rec.recipient_email not in recipient_allowlist:
        raise ApiError(422, "recipient_not_allowed", "Sandbox invoices may only be sent to approved test identities.")
    if rec.paypal_invoice_id is None or rec.operation_state != Op.READY.value \
            or rec.invoice_state != ProviderInvoiceState.DRAFT.value or rec.provider_status != S.UNVERIFIED.value:
        raise ApiError(409, "invoice_not_sendable",
                       "Only a verified DRAFT invoice with no pending operation can be sent. Synchronize first.")
    instructions.lock_project(db, project.id)  # serialize with proposals; released by the claim's commit or the gate's block
    instructions.gate_existing(db, provider, project, rec, "SEND_INVOICE", required=instruction_required)
    rec_id, invoice_id = rec.id, rec.paypal_invoice_id
    if not _claim(db, rec, (Op.READY.value,), Op.SEND_PENDING, also=[
            PaymentRecord.invoice_state == ProviderInvoiceState.DRAFT.value,
            PaymentRecord.provider_status == S.UNVERIFIED.value]):
        instructions.raise_if_held(db, project.id, provider, "SEND_INVOICE", required=instruction_required)
        raise ApiError(409, "operation_in_progress", "This invoice is already being sent.")
    try:
        provider.send_invoice(invoice_id)  # NOTE: no database transaction is open here
    except ProviderAmbiguousOutcome:
        _fail(db, rec_id, Op.SEND_UNCERTAIN, None, "SEND_OUTCOME_UNKNOWN", Ev.OPERATION_UNCERTAIN)
        raise ApiError(502, "provider_outcome_uncertain",
                       "The provider did not confirm the send. Synchronize payments to learn whether it was sent.")
    except ProviderError as exc:  # definitively not sent
        note = f"SEND_REJECTED:{exc.code}" if isinstance(exc, ProviderRejected) else f"SEND_FAILED:{type(exc).__name__.upper()}"
        _fail(db, rec_id, Op.READY, None, note, Ev.OPERATION_FAILED)
        raise _api_error(exc)
    except AssertionError:
        raise
    except Exception as exc:
        log.error("provider_call_failed error_type=%s payment_id=%s", type(exc).__name__, rec_id)
        _fail(db, rec_id, Op.SEND_UNCERTAIN, None, "SEND_OUTCOME_UNKNOWN", Ev.OPERATION_UNCERTAIN)
        raise ApiError(502, "provider_outcome_uncertain",
                       "The provider did not confirm the send. Synchronize payments to learn whether it was sent.")

    def sent(r: PaymentRecord) -> None:
        if r.invoice_state == ProviderInvoiceState.SENT.value or r.provider_status != S.UNVERIFIED.value:
            r.operation_state = Op.READY.value  # a concurrent sync already recorded the send: never overwrite its result
            return
        before = r.provider_status
        r.operation_state, r.invoice_state, r.provider_status = Op.READY.value, ProviderInvoiceState.SENT.value, S.PENDING.value
        r.verification_note = None
        reconcile.record_event(db, r, Ev.INVOICE_SENT, from_status=before, to_status=r.provider_status, actor="SELLER")
        project_row = db.get(Project, r.project_id)
        if project_row.status == ProjectStatus.DRAFT.value:
            project_row.status = ProjectStatus.INVOICED.value

    result = _settle(db, rec_id, sent)
    log.info("invoice_sent payment_id=%s provider=%s", result.id, result.provider)
    return result


# --- synchronization --------------------------------------------------------------------------

_FAILURE_PRIORITY = ("AUTH", "RATE_LIMITED", "UNAVAILABLE", "REJECTED", "CONFLICT")


def sync_project_payments(db: Session, provider: PaymentProvider, project: Project) -> list[PaymentRecord]:
    """Fetch each provider invoice directly and reconcile it. Safe to run concurrently and repeatedly.

    Everything that could be reconciled is persisted. If any record could not be synchronized the call then fails with an
    error status (never a 200), listing the affected payments; the stored state of those records is unchanged."""
    targets = db.execute(
        select(PaymentRecord.id, PaymentRecord.paypal_invoice_id)
        .where(PaymentRecord.project_id == project.id, PaymentRecord.paypal_invoice_id.is_not(None),
               PaymentRecord.provider_status != S.REFUNDED.value, PaymentRecord.provider == provider.name.value)
        .order_by(PaymentRecord.created_at, PaymentRecord.id)).all()
    project_id = project.id
    db.commit()  # close the read transaction before any network call
    failures: list[tuple[str, str]] = []
    for rec_id, invoice_id in targets:
        failure: str | None = None
        kind: str | None = None
        invoice: ProviderInvoice | None = None
        try:
            invoice = provider.get_invoice(invoice_id)
        except ProviderAuthError:
            failure, kind = "PROVIDER_AUTH_FAILED_STATUS_UNCHANGED", "AUTH"
        except ProviderRateLimited:
            failure, kind = "PROVIDER_RATE_LIMITED_STATUS_UNCHANGED", "RATE_LIMITED"
        except ProviderRejected as exc:
            failure, kind = f"PROVIDER_REJECTED_{exc.code}_STATUS_UNCHANGED"[:255], "REJECTED"
        except AssertionError:
            raise
        except Exception as exc:  # unreachable, timeout, unreadable response: fail closed
            log.error("provider_sync_failed error_type=%s payment_id=%s", type(exc).__name__, rec_id)
            failure, kind = "PROVIDER_UNAVAILABLE_STATUS_UNCHANGED", "UNAVAILABLE"
        try:
            _write_sync(db, provider, rec_id, invoice, failure)
        except ApiError as exc:  # one contended record must not block reconciliation of the others
            db.rollback()
            kind = kind or ("STORAGE" if exc.code == "evidence_storage_failed" else "CONFLICT")
        if kind:
            failures.append((rec_id, kind))
    if failures:
        raise _sync_failure(failures, project_id)
    return list_payments(db, db.get(Project, project_id))


def _sync_failure(failures: list[tuple[str, str]], project_id: str) -> ApiError:
    kinds = {kind for _, kind in failures}
    details = [{"payment_id": rec_id, "outcome": kind} for rec_id, kind in failures]
    message = "Some payments could not be synchronized; their stored state is unchanged."
    if "STORAGE" in kinds:  # evidence could not be stored: never reported as a conflict or a duplicate
        return ApiError(500, "evidence_storage_failed", message, details=details)
    worst = next(k for k in _FAILURE_PRIORITY if k in kinds)
    if worst == "AUTH":
        return ApiError(502, "provider_auth_failed", message, details=details)
    if worst == "RATE_LIMITED":
        return ApiError(503, "provider_rate_limited", message, headers={"Retry-After": "30"}, details=details)
    if worst == "UNAVAILABLE":
        return ApiError(502, "provider_unavailable", message, details=details)
    if worst == "REJECTED":
        return ApiError(502, "provider_sync_failed", message, details=details)
    return ApiError(409, "sync_conflict", "A payment changed concurrently. Retry.", details=details)


def _is_duplicate_transaction(exc: IntegrityError) -> bool:
    """True only for the unique constraint on the PayPal transaction id of a payment (a provider reusing an id)."""
    return "paypal_transaction_id" in str(getattr(exc, "orig", exc))


def _collides_with_another_payment(db: Session, rec_id: str, invoice: ProviderInvoice | None) -> bool:
    """Semantic confirmation of a duplicate: a PayPal transaction id in this evidence is already recorded on a DIFFERENT payment."""
    if invoice is None:
        return False
    ids = {t.transaction_id for t in invoice.payments if t.kind == "PAYPAL" and t.transaction_id}
    if not ids:
        return False
    return db.scalar(select(PaymentRecord.id).where(PaymentRecord.paypal_transaction_id.in_(ids), PaymentRecord.id != rec_id).limit(1)) is not None


def _write_sync(db: Session, provider: PaymentProvider, rec_id: str, invoice: ProviderInvoice | None,
                failure: str | None) -> None:
    storage_retries = 1  # a benign race on the observation dedupe key gets one more attempt before failing closed
    for _ in range(4):
        try:
            rec = db.get(PaymentRecord, rec_id)
            db.refresh(rec)
            project = db.get(Project, rec.project_id)
            db.refresh(project)
            if invoice is None:
                note = rec.verification_note
                if note != failure and (note is None or note.endswith("_STATUS_UNCHANGED")):  # keep review flags
                    rec.verification_note = failure
            else:
                evaluation = reconcile.evaluate(rec, invoice, *instructions.reconciliation_inputs(db, rec, provider))
                reconcile.apply(db, rec, project, invoice, evaluation)
            db.commit()
            return
        except StaleDataError:  # another sync won the race: re-read and re-judge the same evidence
            db.rollback()
        except ObservationStorageError:  # evidence could not be stored: fail closed, keep the stored state, never call it a duplicate
            db.rollback()
            if storage_retries > 0:
                storage_retries -= 1
                continue
            log.error("evidence_storage_failed payment_id=%s", rec_id)
            raise ApiError(500, "evidence_storage_failed", "The provider evidence could not be stored; the payment's stored state is unchanged.") from None
        except IntegrityError as exc:
            db.rollback()
            if not (_is_duplicate_transaction(exc) and _collides_with_another_payment(db, rec_id, invoice)):
                # any other integrity failure is unexpected: fail closed, label nothing
                log.error("unexpected_integrity_error payment_id=%s", rec_id)
                raise ApiError(500, "evidence_storage_failed", "The sync could not be stored; the payment's stored state is unchanged.") from None
            rec = db.get(PaymentRecord, rec_id)
            db.refresh(rec)
            if rec.verification_note != "DUPLICATE_TRANSACTION_ID":
                rec.verification_note = "DUPLICATE_TRANSACTION_ID"
                reconcile.record_event(db, rec, Ev.ANOMALY, from_status=rec.provider_status,
                                       to_status=rec.provider_status, note="DUPLICATE_TRANSACTION_ID")
            try:  # the contradictory provider evidence is exactly what must not be lost: keep it beside the anomaly
                observations.record_provider_evidence(db, rec, invoice)
                db.commit()
            except ObservationStorageError:
                db.rollback()
                log.error("evidence_storage_failed payment_id=%s", rec_id)
                raise ApiError(500, "evidence_storage_failed", "The provider evidence could not be stored; the payment's stored state is unchanged.") from None
            return
    raise ApiError(409, "sync_conflict", "The payment changed concurrently. Retry.")


def list_payments(db: Session, project: Project) -> list[PaymentRecord]:
    return list(db.scalars(select(PaymentRecord).where(PaymentRecord.project_id == project.id)
                           .order_by(PaymentRecord.created_at, PaymentRecord.id)))


# --- explicit resolution of NEEDS_REVIEW ----------------------------------------------------------

def resolve_review(db: Session, provider: PaymentProvider, project: Project, rec: PaymentRecord,
                   req: ReviewResolutionRequest, *, instruction_required: bool = False) -> PaymentRecord:
    """The only way out of NEEDS_REVIEW. Always an authenticated seller decision with a recorded reason."""
    if rec.provider_status != S.NEEDS_REVIEW.value:
        raise ApiError(409, "not_in_review", "Only a payment in NEEDS_REVIEW can be resolved.")
    rec_id, invoice_id, provider_name = rec.id, rec.paypal_invoice_id, rec.provider
    reason = f"{req.resolution.value}: {req.note.strip()}"
    if req.resolution is ReviewResolution.ABANDON_UNCREATED_INVOICE:
        if not req.confirm_no_provider_invoice:
            raise ApiError(422, "abandon_confirmation_required",
                           "Set confirm_no_provider_invoice=true after checking the provider dashboard that no invoice exists.")
        if invoice_id is not None:
            raise ApiError(409, "abandon_not_applicable", "A provider invoice exists; accept or cancel it at the provider.")

        def abandon(r: PaymentRecord) -> None:
            if r.provider_status != S.NEEDS_REVIEW.value or r.paypal_invoice_id is not None:
                raise ApiError(409, "not_in_review", "The payment changed concurrently.")
            if r.operation_state != Op.CREATE_UNCERTAIN.value:  # e.g. a create is in flight: it could still produce an invoice
                raise ApiError(409, "operation_in_progress", "A provider operation is still in flight for this payment.")
            before = r.provider_status
            r.provider_status, r.operation_state = S.CANCELLED.value, Op.CREATE_FAILED.value
            r.verification_note = "ABANDONED_BY_SELLER"
            reconcile.record_event(db, r, Ev.STATUS_CHANGED, from_status=before, to_status=r.provider_status, actor="SELLER",
                                   note="ABANDONED_BY_SELLER")
            reconcile.record_event(db, r, Ev.REVIEW_RESOLVED, from_status=before, to_status=r.provider_status,
                                   actor="SELLER", note=reason)
        return _settle(db, rec_id, abandon)

    if invoice_id is None:
        raise ApiError(409, "no_provider_invoice", "There is no provider invoice to verify; abandon it instead.")
    if provider.name is not PaymentProviderName(provider_name):
        raise ApiError(409, "provider_mismatch", "This payment belongs to a different payment provider mode.")
    instructions.raise_if_held(db, project.id, provider, "ACCEPT_REVIEW_EVIDENCE", required=instruction_required)
    db.commit()  # no transaction open during the provider call
    try:
        invoice = provider.get_invoice(invoice_id)
    except ProviderError as exc:
        raise _api_error(exc)
    except AssertionError:
        raise
    except Exception as exc:
        log.error("provider_resolve_failed error_type=%s payment_id=%s", type(exc).__name__, rec_id)
        raise ApiError(502, "provider_unavailable", "The payment provider could not be reached.")

    def accept(r: PaymentRecord) -> None:
        if r.provider_status != S.NEEDS_REVIEW.value:
            raise ApiError(409, "not_in_review", "The payment changed concurrently.")
        instructions.lock_project(db, r.project_id)
        instructions.raise_if_held(db, r.project_id, provider, "ACCEPT_REVIEW_EVIDENCE", required=instruction_required)
        evaluation = reconcile.evaluate(r, invoice, *instructions.reconciliation_inputs(db, r, provider))
        if evaluation.anomalies:
            raise ApiError(409, "review_evidence_inconsistent",
                           "The provider evidence is still inconsistent: " + ",".join(evaluation.anomalies))
        before = r.provider_status
        reconcile.apply(db, r, db.get(Project, r.project_id), invoice, evaluation, actor="SELLER", resolving=True)
        if r.provider_status == S.NEEDS_REVIEW.value:  # apply declined (e.g. changed transaction id): not resolved
            db.commit()  # keep the anomaly it recorded, but never claim a resolution
            raise ApiError(409, "review_not_resolved",
                           "The evidence could not be applied safely; the payment remains in review: " + (r.verification_note or ""))
        reconcile.record_event(db, r, Ev.REVIEW_RESOLVED, from_status=before, to_status=r.provider_status,
                               actor="SELLER", note=reason)
    return _settle(db, rec_id, accept)
