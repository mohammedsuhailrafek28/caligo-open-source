"""The SAFE incoming-event processor for the simulated merchant.

Rules (none of them trusts arrival order or the event's own claims):
* an event is only a CLAIM: money-affecting effects require the project's own PaymentRecord (provider evidence) to agree;
* identity is the business operation key (one order / deduction / fulfillment per payment), never the delivery id, so a redelivery
  cannot repeat an effect;
* order states move only along ORDER_TRANSITIONS; REFUNDED/CANCELLED are terminal, so a stale "paid" cannot regress a refund;
* every attempt, including rejected and duplicate ones, is written to the append-only audit trail; a crash writes nothing and leaves the
  event detectable via store.unprocessed_events.
This module only reads PaymentRecord; it never calls a provider and never changes provider evidence.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PaymentRecord, ProviderEvidenceGap
from app.schemas import PaymentProviderStatus as P
from app.sim_enums import (
    AuditAction, EventType, FulfillmentState, MovementKind, OrderState, ProcessingOutcome as Out, Processor, ReplayStatus,
)
from app.sim_models import SimAuditRecord, SimFulfillment, SimIncomingEvent, SimInventoryItem, SimOrder
from app.simulation import store

FailPoint = Callable[[str], None]
_REFUND_STATES = (P.REFUNDED.value, P.PARTIALLY_REFUNDED.value)


@dataclass(frozen=True)
class ProcessingResult:
    outcome: Out
    detail: dict[str, Any] = field(default_factory=dict)
    order_id: str | None = None


def _payment_for(db: Session, event: SimIncomingEvent) -> PaymentRecord | None:
    if not event.provider_txn_ref:
        return None
    return db.scalar(select(PaymentRecord).where(PaymentRecord.project_id == event.project_id,
                                                 PaymentRecord.paypal_transaction_id == event.provider_txn_ref))


def _already_applied(db: Session, event: SimIncomingEvent) -> bool:
    """True if this event, or an earlier delivery of the same event key, already took effect."""
    sibling_ids = select(SimIncomingEvent.id).where(
        SimIncomingEvent.project_id == event.project_id, SimIncomingEvent.source == event.source, SimIncomingEvent.event_key == event.event_key)
    return db.scalar(select(SimAuditRecord.id).where(
        SimAuditRecord.project_id == event.project_id, SimAuditRecord.action == AuditAction.EVENT_PROCESSED.value,
        SimAuditRecord.outcome == Out.APPLIED.value, SimAuditRecord.event_id.in_(sibling_ids)).limit(1)) is not None


def _check_evidence(db: Session, event: SimIncomingEvent) -> tuple[PaymentRecord | None, ProcessingResult | None]:
    """Shared gates. Returns (payment, None) to continue or (None, rejection)."""
    if event.replay_status == ReplayStatus.REPLAY_DIFFERENT_PAYLOAD.value:
        return None, ProcessingResult(Out.REJECTED_CONFLICTING_REPLAY, {"reason": "same event key was delivered with different content"})
    if _already_applied(db, event):
        return None, ProcessingResult(Out.DUPLICATE_NO_EFFECT, {"reason": "an earlier delivery of this event already took effect"})
    if event.amount_minor is None or event.currency is None or not event.provider_txn_ref:
        return None, ProcessingResult(Out.REJECTED_INCOMPLETE_EVIDENCE, {"reason": "amount, currency and provider transaction are required"})
    payment = _payment_for(db, event)
    if payment is None or (event.payment_id is not None and event.payment_id != payment.id):
        return None, ProcessingResult(Out.REJECTED_UNKNOWN_PAYMENT, {"reason": "no payment of this project carries that provider transaction"})
    if event.currency != payment.currency:
        return None, ProcessingResult(Out.REJECTED_CURRENCY_MISMATCH, {"event": event.currency, "payment": payment.currency})
    expected = payment.amount_paid_minor if event.event_type == EventType.PAYMENT_REFUNDED.value else payment.amount_minor
    if event.amount_minor != expected:
        return None, ProcessingResult(Out.REJECTED_AMOUNT_MISMATCH, {"event": event.amount_minor, "expected": expected})
    gap = db.scalar(select(ProviderEvidenceGap.id).where(ProviderEvidenceGap.payment_id == payment.id).limit(1))
    if gap is not None:
        return None, ProcessingResult(Out.HELD_AWAITING_PROVIDER_EVIDENCE, {"reason": "provider evidence for this payment is incomplete"})
    return payment, None


def _completed(db: Session, event: SimIncomingEvent, payment: PaymentRecord, fail_at: FailPoint) -> ProcessingResult:
    if payment.provider_status in _REFUND_STATES:
        return ProcessingResult(Out.REJECTED_STALE, {"reason": "provider evidence shows a refund; a paid event cannot regress it"})
    if payment.provider_status != P.PAID.value:
        return ProcessingResult(Out.HELD_AWAITING_PROVIDER_EVIDENCE, {"provider_status": payment.provider_status})
    order = db.scalar(select(SimOrder).where(SimOrder.project_id == event.project_id, SimOrder.business_key == store.order_key_for(payment.id)))
    if order is None:
        item = db.scalar(select(SimInventoryItem).where(SimInventoryItem.project_id == event.project_id,
                                                       SimInventoryItem.sku == event.payload.get("sku")))
        quantity = event.payload.get("quantity")
        if item is None or not isinstance(quantity, int) or quantity < 1:
            return ProcessingResult(Out.REJECTED_INCOMPLETE_EVIDENCE, {"reason": "no merchant order and no usable sku/quantity"})
        order, _ = store.create_order(db, event.project_id, payment, item, quantity, event_id=event.id)
    fail_at("after_order")
    if order.state in (OrderState.REFUNDED.value, OrderState.CANCELLED.value):
        return ProcessingResult(Out.REJECTED_STALE, {"reason": f"order is already {order.state}"}, order.id)
    if order.state in (OrderState.PAID.value, OrderState.FULFILLED.value):
        return ProcessingResult(Out.DUPLICATE_NO_EFFECT, {"reason": f"order is already {order.state}"}, order.id)
    store.transition_order(db, order, OrderState.PAID, event_id=event.id, reason="provider evidence shows PAID")
    item = db.get(SimInventoryItem, order.item_id)
    store.record_movement(db, event.project_id, item, MovementKind.DEDUCT, order.quantity, store.operation_key("deduct", order.id),
                          order_id=order.id, event_id=event.id)
    fail_at("after_inventory")
    store.create_fulfillment(db, order, event_id=event.id)
    return ProcessingResult(Out.APPLIED, {"order_state": OrderState.PAID.value}, order.id)


def _refunded(db: Session, event: SimIncomingEvent, payment: PaymentRecord, fail_at: FailPoint) -> ProcessingResult:
    if payment.provider_status != P.REFUNDED.value:
        return ProcessingResult(Out.HELD_AWAITING_PROVIDER_EVIDENCE, {"provider_status": payment.provider_status})
    order = db.scalar(select(SimOrder).where(SimOrder.project_id == event.project_id, SimOrder.business_key == store.order_key_for(payment.id)))
    if order is None:
        return ProcessingResult(Out.REJECTED_NO_MERCHANT_ORDER, {"reason": "nothing to reverse"})
    if order.state == OrderState.REFUNDED.value:
        return ProcessingResult(Out.DUPLICATE_NO_EFFECT, {"reason": "order is already REFUNDED"}, order.id)
    if order.state not in (OrderState.PAID.value, OrderState.FULFILLED.value):
        return ProcessingResult(Out.HELD_AWAITING_PROVIDER_EVIDENCE, {"reason": f"refund arrived while the order is {order.state}; "
                                                                                 "the paid event has not been applied"}, order.id)
    fulfillment = db.scalar(select(SimFulfillment).where(SimFulfillment.project_id == event.project_id, SimFulfillment.order_id == order.id))
    shipped = fulfillment is not None and fulfillment.state == FulfillmentState.DISPATCHED.value
    store.transition_order(db, order, OrderState.REFUNDED, event_id=event.id, reason="provider evidence shows REFUNDED")
    fail_at("after_order")
    if fulfillment is not None:
        store.cancel_fulfillment(db, fulfillment, reason="payment refunded", event_id=event.id)  # records DISPATCH_PROTECTED if shipped
    if not shipped:  # stock only returns for goods that did not ship
        store.record_movement(db, event.project_id, db.get(SimInventoryItem, order.item_id), MovementKind.RELEASE, order.quantity,
                              store.operation_key("release", order.id), order_id=order.id, event_id=event.id)
    return ProcessingResult(Out.APPLIED, {"order_state": OrderState.REFUNDED.value, "goods_already_shipped": shipped}, order.id)


def _failed(db: Session, event: SimIncomingEvent, payment: PaymentRecord) -> ProcessingResult:
    if payment.provider_status in (P.PAID.value, P.PARTIALLY_PAID.value, *_REFUND_STATES):
        return ProcessingResult(Out.REJECTED_CONTRADICTED_BY_PROVIDER, {"provider_status": payment.provider_status})
    order = db.scalar(select(SimOrder).where(SimOrder.project_id == event.project_id, SimOrder.business_key == store.order_key_for(payment.id)))
    if order is None:
        return ProcessingResult(Out.REJECTED_NO_MERCHANT_ORDER, {"reason": "nothing to fail"})
    if order.state == OrderState.FAILED.value:
        return ProcessingResult(Out.DUPLICATE_NO_EFFECT, {"reason": "order is already FAILED"}, order.id)
    if order.state != OrderState.PENDING.value:
        return ProcessingResult(Out.REJECTED_STALE, {"reason": f"order is already {order.state}"}, order.id)
    store.transition_order(db, order, OrderState.FAILED, event_id=event.id, reason="payment failure reported")
    return ProcessingResult(Out.APPLIED, {"order_state": OrderState.FAILED.value}, order.id)


def _decide(db: Session, event: SimIncomingEvent, fail_at: FailPoint) -> ProcessingResult:
    payment, rejection = _check_evidence(db, event)
    if rejection is not None:
        return rejection
    if event.event_type == EventType.PAYMENT_COMPLETED.value:
        return _completed(db, event, payment, fail_at)
    if event.event_type == EventType.PAYMENT_REFUNDED.value:
        return _refunded(db, event, payment, fail_at)
    return _failed(db, event, payment)


def process_event(db: Session, project_id: str, event_id: str, *, fail_at: FailPoint | None = None) -> ProcessingResult:
    """Process one stored delivery exactly as described in the module docstring. Commits on success or on a recorded rejection.

    `fail_at` is a test/demo failure-injection hook called at named steps; if it raises, EVERYTHING of this attempt is rolled back and
    the exception propagates (a crash before persist): the event stays received-but-unprocessed."""
    hook = fail_at or (lambda _step: None)
    event = store.get_event(db, project_id, event_id)
    try:
        result = _decide(db, event, hook)
        hook("before_commit")
        store.audit(db, project_id, AuditAction.EVENT_PROCESSED, entity_type="event", entity_id=event.id, processor=Processor.SAFE,
                    detail={**result.detail, "order_id": result.order_id}, event_id=event.id, outcome=result.outcome.value)
        db.commit()
        return result
    except IntegrityError as exc:  # two workers raced on the same business operation: the unique key made one of them lose
        db.rollback()
        if not store.is_unique_violation(exc):
            raise
        event = store.get_event(db, project_id, event_id)
        result = ProcessingResult(Out.DUPLICATE_NO_EFFECT, {"reason": "a concurrent attempt performed this operation"})
        store.audit(db, project_id, AuditAction.EVENT_PROCESSED, entity_type="event", entity_id=event.id, processor=Processor.SAFE,
                    detail=result.detail, event_id=event.id, outcome=result.outcome.value)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def preview_event(db: Session, project_id: str, event_id: str) -> ProcessingResult:
    """What the safe processor WOULD decide, without persisting anything (a savepoint that is always rolled back). Used to prepare
    evidence, for example that a stale event would be rejected, before any other handler touches the state."""
    event = store.get_event(db, project_id, event_id)
    savepoint = db.begin_nested()
    try:
        return _decide(db, event, lambda _step: None)
    finally:
        savepoint.rollback()
