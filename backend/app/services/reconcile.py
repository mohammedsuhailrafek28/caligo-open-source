"""Payment reconciliation: the ONLY code that derives a financial state from provider evidence.

`evaluate()` is a pure function (no I/O, no database): it judges provider evidence against what we stored and returns
either a target financial status or a list of anomaly codes. `apply()` is the single writer: it applies the explicit
transition table, appends to the immutable payment history, and updates project status. Anything unexpected fails
closed: the verified state is never advanced on contradictory evidence and never silently downgraded.

Project status semantics: `PAID` means "a verified payment was received at some point". A refund changes the payment
record (REFUNDED / PARTIALLY_REFUNDED), never the project: refunding money does not reverse delivered work.
"""
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PaymentEvent, PaymentRecord, Project, utcnow
from app.payment_provider import ProviderInvoice
from app.services import observations
from app.schemas import (
    OperationState, PaymentEventType, PaymentProviderStatus as S, ProjectStatus, ProviderInvoiceState as P,
)

# Provider lifecycle -> financial status, valid only when the evidence is internally consistent.
_TARGET = {
    P.DRAFT: S.UNVERIFIED, P.SENT: S.PENDING, P.SCHEDULED: S.PENDING, P.UNPAID: S.PENDING,
    P.PAYMENT_PENDING: S.PENDING, P.PARTIALLY_PAID: S.PARTIALLY_PAID, P.PAID: S.PAID,
    P.PARTIALLY_REFUNDED: S.PARTIALLY_REFUNDED, P.REFUNDED: S.REFUNDED, P.CANCELLED: S.CANCELLED,
}
_UNPAID_STATES = {P.DRAFT, P.SENT, P.SCHEDULED, P.UNPAID, P.PAYMENT_PENDING}
_SETTLED_FORWARD = {S.PARTIALLY_PAID, S.PAID, S.PARTIALLY_REFUNDED, S.REFUNDED}

# Explicit transition table. Anything not listed is an UNEXPECTED_TRANSITION (flagged, never applied).
TRANSITIONS: dict[S, frozenset[S]] = {
    S.UNVERIFIED: frozenset({S.PENDING, S.CANCELLED} | _SETTLED_FORWARD),
    S.PENDING: frozenset({S.CANCELLED} | _SETTLED_FORWARD),
    S.PARTIALLY_PAID: frozenset({S.PAID, S.PARTIALLY_REFUNDED, S.REFUNDED}),
    S.PAID: frozenset({S.PARTIALLY_REFUNDED, S.REFUNDED}),
    S.PARTIALLY_REFUNDED: frozenset({S.REFUNDED}),
    S.NEEDS_REVIEW: frozenset({S.UNVERIFIED, S.PENDING, S.CANCELLED} | _SETTLED_FORWARD),
    S.CANCELLED: frozenset(),
    S.REFUNDED: frozenset(),
}
TERMINAL = frozenset({S.CANCELLED, S.REFUNDED})
# States that may be moved to NEEDS_REVIEW by contradictory evidence. Verified money states are never downgraded.
# A send whose outcome is unknown is only declared "not sent" after the provider has kept reporting DRAFT for this long
# (PayPal may still be processing it); this prevents a second email from a stale read.
SEND_SETTLE_AFTER = timedelta(minutes=5)
_REVIEWABLE = frozenset({S.UNVERIFIED, S.PENDING, S.PARTIALLY_PAID, S.NEEDS_REVIEW})


@dataclass(frozen=True)
class Evaluation:
    target: S | None  # None when the evidence is contradictory
    anomalies: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()  # informational, non-blocking
    paid_minor: int = 0  # PayPal-verified payments only
    refunded_minor: int = 0
    transaction_id: str | None = None
    state: P | None = None


def evaluate(rec: PaymentRecord, ev: ProviderInvoice, expected_merchant: str | None = None,
             extra_anomalies: tuple[str, ...] = ()) -> Evaluation:
    anomalies: list[str] = list(extra_anomalies)
    notes: list[str] = []
    paid, ext_paid = ev.paypal_paid_minor, ev.external_paid_minor
    refunded, ext_refunded = ev.paypal_refunded_minor, ev.external_refunded_minor

    if ev.invoice_id != rec.paypal_invoice_id:
        anomalies.append("INVOICE_ID_MISMATCH")
    if rec.invoice_number and ev.invoice_number != rec.invoice_number:
        anomalies.append("INVOICE_NUMBER_MISMATCH")
    if ev.currency != rec.currency or ev.total_minor != rec.amount_minor:
        anomalies.append("AMOUNT_OR_CURRENCY_MISMATCH")
    if any(t.currency != rec.currency for t in (*ev.payments, *ev.refunds)):
        anomalies.append("TRANSACTION_CURRENCY_MISMATCH")
    if (ev.recipient_email or "").strip().lower() != rec.recipient_email:  # a missing recipient is a mismatch
        anomalies.append("RECIPIENT_MISMATCH")
    if expected_merchant and (ev.merchant_email or "").strip().lower() != expected_merchant:
        anomalies.append("MERCHANT_MISMATCH")
    if any(t.kind == "PAYPAL" and not t.transaction_id for t in ev.payments):
        anomalies.append("MISSING_TRANSACTION_ID")
    if paid < rec.amount_paid_minor or refunded < rec.amount_refunded_minor:
        anomalies.append("AMOUNT_DECREASED")  # recorded money never shrinks: stale or contradictory evidence
    if paid + ext_paid > rec.amount_minor:
        anomalies.append("OVERPAYMENT")
    if refunded + ext_refunded > paid + ext_paid:
        anomalies.append("REFUND_EXCEEDS_PAYMENT")

    state, total = ev.state, rec.amount_minor
    if state is P.MARKED_AS_PAID:
        anomalies.append("EXTERNALLY_MARKED_PAID")  # recorded outside PayPal: never proof of a PayPal payment
    elif state is P.MARKED_AS_REFUNDED:
        anomalies.append("EXTERNALLY_MARKED_REFUNDED")
    elif state is P.PAID:
        if ext_paid or paid != total:
            anomalies.append("PAID_STATE_WITHOUT_MATCHING_PAYPAL_PAYMENTS")
        if refunded or ext_refunded:
            anomalies.append("PAID_STATE_WITH_REFUNDS")
    elif state is P.PARTIALLY_PAID:
        if ext_paid or not 0 < paid < total:
            anomalies.append("PARTIAL_PAYMENT_INCONSISTENT")
        if refunded or ext_refunded:
            anomalies.append("PARTIAL_STATE_WITH_REFUNDS")
    elif state is P.REFUNDED:
        if ext_paid or ext_refunded or paid <= 0 or refunded != paid:
            anomalies.append("REFUNDED_STATE_NOT_A_FULL_PAYPAL_REFUND")
    elif state is P.PARTIALLY_REFUNDED:
        if ext_paid or ext_refunded or paid <= 0 or not 0 < refunded < paid:
            anomalies.append("PARTIAL_REFUND_INCONSISTENT")
    elif state is P.CANCELLED:
        if paid or ext_paid:
            anomalies.append("CANCELLED_WITH_PAYMENTS")
    elif state in _UNPAID_STATES:
        if paid or ext_paid or refunded or ext_refunded:
            anomalies.append("PAYMENT_ACTIVITY_IN_UNPAID_STATE")
        if state is P.PAYMENT_PENDING:
            notes.append("PAYMENT_PENDING_AT_PROVIDER")

    first_txn = next((t.transaction_id for t in ev.payments if t.kind == "PAYPAL" and t.transaction_id), None)
    target = None if anomalies else _TARGET.get(state)
    return Evaluation(target, tuple(dict.fromkeys(anomalies)), tuple(notes), paid, refunded, first_txn, state)


def _event(db: Session, rec: PaymentRecord, kind: PaymentEventType, *, from_status: str | None = None,
           to_status: str | None = None, actor: str = "SYSTEM", note: str | None = None) -> None:
    db.add(PaymentEvent(
        payment_id=rec.id, project_id=rec.project_id, event_type=kind.value, from_status=from_status,
        to_status=to_status, actor=actor, note=note[:255] if note else None,
        amount_paid_minor=rec.amount_paid_minor, amount_refunded_minor=rec.amount_refunded_minor))


def record_event(db: Session, rec: PaymentRecord, kind: PaymentEventType, **kwargs) -> None:
    _event(db, rec, kind, **kwargs)


def _no_active_invoice_left(db: Session, rec: PaymentRecord) -> bool:
    return db.scalar(select(func.count()).select_from(PaymentRecord).where(
        PaymentRecord.project_id == rec.project_id, PaymentRecord.id != rec.id,
        PaymentRecord.provider_status.notin_([S.CANCELLED.value, S.REFUNDED.value]))) == 0


def apply(db: Session, rec: PaymentRecord, project: Project, ev: ProviderInvoice, evaluation: Evaluation,
          *, actor: str = "SYSTEM", resolving: bool = False) -> None:
    """Apply one evaluation to one record inside the caller's transaction. The caller commits.

    NEEDS_REVIEW is sticky: automatic reconciliation never leaves it, even when the evidence becomes consistent. Only
    an explicit, audited seller resolution (`resolving=True`, evidence already verified consistent) may move it."""
    now = utcnow()
    rec.checked_at = now
    # Keep what the provider just said (even if it is about to be judged contradictory) as an append-only observation. This only
    # RECORDS: the judgement below is unchanged and never reads any merchant claim.
    observations.record_provider_evidence(db, rec, ev)
    current = S(rec.provider_status)
    note: str | None = None

    def flag(code: str, new_status: S | None = None) -> None:
        nonlocal note
        note = code
        if rec.verification_note != code[:255]:
            _event(db, rec, PaymentEventType.ANOMALY, from_status=rec.provider_status,
                   to_status=(new_status or current).value, actor=actor, note=code)
        if new_status is not None and new_status is not current:
            rec.provider_status = new_status.value

    if current is S.NEEDS_REVIEW and not resolving and not evaluation.anomalies:
        flag("EVIDENCE_NOW_CONSISTENT_AWAITING_RESOLUTION")  # informational: a human must resolve it explicitly
        rec.verification_note = note
        return
    if evaluation.anomalies:
        code = ",".join(evaluation.anomalies)
        if current is S.CANCELLED and evaluation.state in (P.PAID, P.PARTIALLY_PAID, P.MARKED_AS_PAID):
            code = "PAID_AFTER_CANCEL_MANUAL_REVIEW," + code
        flag(code, S.NEEDS_REVIEW if current in _REVIEWABLE else None)
        rec.verification_note = note[:255] if note else None
        return

    target = evaluation.target
    if target is None:  # defensive: evaluate() always yields a target when there are no anomalies
        flag("NO_TARGET_STATE_REVIEW", S.NEEDS_REVIEW if current in _REVIEWABLE else None)
        rec.verification_note = note
        return
    if current is S.CANCELLED and (evaluation.paid_minor > 0 or target not in (S.CANCELLED,)):
        if evaluation.paid_minor > 0:
            flag("PAID_AFTER_CANCEL_MANUAL_REVIEW")
        else:
            flag("UNEXPECTED_AFTER_CANCEL")
        rec.verification_note = note
        return
    if current is S.REFUNDED and target is not S.REFUNDED:
        flag("UNEXPECTED_AFTER_REFUND")
        rec.verification_note = note
        return
    if target is not current and target not in TRANSITIONS[current]:
        flag(f"UNEXPECTED_TRANSITION:{current.value}->{target.value}")
        rec.verification_note = note
        return
    if rec.paypal_transaction_id not in (None, evaluation.transaction_id) and evaluation.transaction_id:
        flag("TRANSACTION_ID_CHANGED_REVIEW")  # a recorded transaction id is immutable
        rec.verification_note = note
        return

    # --- consistent evidence: record it -------------------------------------------------------
    amounts_changed = (rec.amount_paid_minor, rec.amount_refunded_minor) != (evaluation.paid_minor, evaluation.refunded_minor)
    rec.amount_paid_minor, rec.amount_refunded_minor = evaluation.paid_minor, evaluation.refunded_minor
    if evaluation.transaction_id and rec.paypal_transaction_id is None:
        rec.paypal_transaction_id = evaluation.transaction_id
    if target is not current:
        rec.provider_status = target.value
        _event(db, rec, PaymentEventType.STATUS_CHANGED, from_status=current.value, to_status=target.value, actor=actor,
               note=",".join(evaluation.notes) or None)
    elif amounts_changed:
        _event(db, rec, PaymentEventType.AMOUNTS_UPDATED, from_status=current.value, to_status=target.value, actor=actor)
    rec.invoice_state = ev.state.value
    rec.verification_note = ",".join(evaluation.notes) or None

    # An interrupted send is resolved by what the provider actually reports.
    if rec.operation_state in (OperationState.SEND_UNCERTAIN.value, OperationState.SEND_PENDING.value):
        if ev.state is not P.DRAFT:
            rec.operation_state = OperationState.READY.value
            _event(db, rec, PaymentEventType.INVOICE_SENT, from_status=current.value, to_status=target.value,
                   actor=actor, note="SEND_CONFIRMED_BY_PROVIDER")
        elif rec.operation_started_at is None or utcnow() - rec.operation_started_at >= SEND_SETTLE_AFTER:
            rec.operation_state = OperationState.READY.value  # still DRAFT after the settle window: nothing was sent
            _event(db, rec, PaymentEventType.OPERATION_FAILED, from_status=current.value, to_status=target.value,
                   actor=actor, note="SEND_NOT_CONFIRMED_BY_PROVIDER")
        else:
            rec.verification_note = "SEND_OUTCOME_UNCONFIRMED_RETRY_SYNC_LATER"

    # Project status moves forward on verified evidence only. Refunds never move it.
    if target in (S.PAID, S.PARTIALLY_REFUNDED, S.REFUNDED) and evaluation.paid_minor >= rec.amount_minor \
            and project.status in (ProjectStatus.DRAFT.value, ProjectStatus.INVOICED.value):
        project.status = ProjectStatus.PAID.value
    elif target is S.PENDING and project.status == ProjectStatus.DRAFT.value:
        project.status = ProjectStatus.INVOICED.value
    elif target is S.CANCELLED and project.status == ProjectStatus.INVOICED.value and _no_active_invoice_left(db, rec):
        project.status = ProjectStatus.DRAFT.value
