"""Merchant-side primitives for the simulation: orders, inventory, fulfillment, incoming-event receipts and the audit trail.

These functions only flush; the caller (a processor or a scenario) owns the transaction. They never touch a payment provider and never
write provider evidence. Every business operation has a unique key, so repeating it is recognised (and recorded) instead of repeated.
"""
import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PaymentRecord, monotonic_utcnow
from app.sim_enums import (
    AuditAction, EventSource, EventType, FulfillmentState, MovementKind, ORDER_TRANSITIONS, OrderState, Processor, ReplayStatus,
)
from app.sim_models import SimAuditRecord, SimFulfillment, SimIncomingEvent, SimInventoryItem, SimInventoryMovement, SimOrder


class SimulationError(Exception):
    """A programming/usage error in the simulation (unknown or cross-project reference, illegal transition)."""


def operation_key(*parts: str) -> str:
    """Deterministic business-operation identity: the same inputs always yield the same key."""
    return hashlib.sha256("|".join(parts).encode("utf8")).hexdigest()


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf8")).hexdigest()


def audit(db: Session, project_id: str, action: AuditAction, *, entity_type: str, entity_id: str | None, processor: Processor,
          detail: dict[str, Any], event_id: str | None = None, outcome: str | None = None) -> SimAuditRecord:
    row = SimAuditRecord(project_id=project_id, event_id=event_id, entity_type=entity_type, entity_id=entity_id, action=action.value,
                         outcome=outcome, processor=processor.value, detail=detail)
    db.add(row)
    db.flush()
    return row


def _own(db: Session, model, row_id: str, project_id: str):
    row = db.get(model, row_id)
    if row is None or row.project_id != project_id:
        raise SimulationError(f"{model.__tablename__} {row_id} not found in this project")
    return row


# --- inventory ----------------------------------------------------------------------------------------------------------------

def add_inventory_item(db: Session, project_id: str, sku: str, initial_stock: int) -> SimInventoryItem:
    item = SimInventoryItem(project_id=project_id, sku=sku, initial_stock=initial_stock)
    db.add(item)
    db.flush()
    return item


def stock_balance(db: Session, item_id: str) -> int:
    """initial stock minus reservations and deductions plus releases: derived, never stored."""
    item = db.get(SimInventoryItem, item_id)
    if item is None:
        raise SimulationError("unknown inventory item")
    moved = dict(db.execute(select(SimInventoryMovement.kind, func.coalesce(func.sum(SimInventoryMovement.quantity), 0))
                            .where(SimInventoryMovement.item_id == item_id).group_by(SimInventoryMovement.kind)).all())
    return (item.initial_stock - moved.get(MovementKind.RESERVE.value, 0) - moved.get(MovementKind.DEDUCT.value, 0)
            + moved.get(MovementKind.RELEASE.value, 0))


def record_movement(db: Session, project_id: str, item: SimInventoryItem, kind: MovementKind, quantity: int, key: str, *,
                    order_id: str | None = None, event_id: str | None = None, processor: Processor = Processor.SAFE,
                    note: str | None = None) -> tuple[SimInventoryMovement, bool]:
    """Record a stock movement once per business operation key. Returns (movement, created); a repeat records nothing but a
    suppression note in the audit trail."""
    existing = db.scalar(select(SimInventoryMovement).where(SimInventoryMovement.project_id == project_id,
                                                           SimInventoryMovement.operation_key == key))
    if existing is not None:
        audit(db, project_id, AuditAction.OPERATION_DUPLICATE_SUPPRESSED, entity_type="inventory_movement", entity_id=existing.id,
              processor=processor, detail={"operation_key": key, "kind": kind.value}, event_id=event_id)
        return existing, False
    movement = SimInventoryMovement(project_id=project_id, item_id=item.id, order_id=order_id, kind=kind.value, quantity=quantity,
                                    operation_key=key, source_event_id=event_id)
    db.add(movement)
    db.flush()
    audit(db, project_id, AuditAction.INVENTORY_MOVEMENT, entity_type="inventory_movement", entity_id=movement.id, processor=processor,
          detail={"kind": kind.value, "quantity": quantity, "item_id": item.id, "order_id": order_id, **({"note": note} if note else {})},
          event_id=event_id)
    return movement, True


# --- orders -------------------------------------------------------------------------------------------------------------------

def order_key_for(payment_id: str) -> str:
    """The safe, deterministic order identity: ONE order per provider payment."""
    return operation_key("order", payment_id)


def create_order(db: Session, project_id: str, payment: PaymentRecord, item: SimInventoryItem, quantity: int, *, key: str | None = None,
                 processor: Processor = Processor.SAFE, event_id: str | None = None) -> tuple[SimOrder, bool]:
    """Create the order for a payment, once per business key (default: one order per payment). Amount and currency are copied from the
    provider-side payment record, never from the caller."""
    if payment.project_id != project_id or item.project_id != project_id:
        raise SimulationError("payment and item must belong to the order's project")
    key = key or order_key_for(payment.id)
    existing = db.scalar(select(SimOrder).where(SimOrder.project_id == project_id, SimOrder.business_key == key))
    if existing is not None:
        audit(db, project_id, AuditAction.OPERATION_DUPLICATE_SUPPRESSED, entity_type="order", entity_id=existing.id, processor=processor,
              detail={"operation_key": key}, event_id=event_id)
        return existing, False
    order = SimOrder(project_id=project_id, payment_id=payment.id, business_key=key, item_id=item.id, quantity=quantity,
                     amount_minor=payment.amount_minor, currency=payment.currency, state=OrderState.PENDING.value)
    db.add(order)
    db.flush()
    audit(db, project_id, AuditAction.ORDER_CREATED, entity_type="order", entity_id=order.id, processor=processor,
          detail={"payment_id": payment.id, "quantity": quantity, "amount_minor": payment.amount_minor, "currency": payment.currency},
          event_id=event_id)
    return order, True


def transition_order(db: Session, order: SimOrder, to: OrderState, *, processor: Processor = Processor.SAFE, event_id: str | None = None,
                     reason: str = "") -> None:
    current = OrderState(order.state)
    if to not in ORDER_TRANSITIONS[current]:
        raise SimulationError(f"illegal order transition {current.value} -> {to.value}")
    _set_state(db, order, current, to, processor, event_id, reason, regression=False)


def force_order_state(db: Session, order: SimOrder, to: OrderState, *, processor: Processor, event_id: str | None, reason: str) -> None:
    """Bypasses the transition table. ONLY for deliberately faulty fixtures (it refuses any processor but FAULTY_FIXTURE)."""
    if processor is not Processor.FAULTY_FIXTURE:
        raise SimulationError("the transition table can only be bypassed by a faulty fixture")
    current = OrderState(order.state)
    _set_state(db, order, current, to, processor, event_id, reason, regression=to not in ORDER_TRANSITIONS[current])


def _set_state(db, order, current, to, processor, event_id, reason, *, regression: bool) -> None:
    order.state = to.value
    order.updated_at = monotonic_utcnow()
    db.flush()
    audit(db, order.project_id, AuditAction.ORDER_STATE_CHANGED, entity_type="order", entity_id=order.id, processor=processor,
          detail={"from": current.value, "to": to.value, "reason": reason, "illegal_regression": regression}, event_id=event_id)


# --- fulfillment --------------------------------------------------------------------------------------------------------------

def create_fulfillment(db: Session, order: SimOrder, *, key: str | None = None, processor: Processor = Processor.SAFE,
                       event_id: str | None = None) -> tuple[SimFulfillment, bool]:
    key = key or operation_key("fulfil", order.id)
    existing = db.scalar(select(SimFulfillment).where(SimFulfillment.project_id == order.project_id, SimFulfillment.operation_key == key))
    if existing is not None:
        audit(db, order.project_id, AuditAction.OPERATION_DUPLICATE_SUPPRESSED, entity_type="fulfillment", entity_id=existing.id,
              processor=processor, detail={"operation_key": key}, event_id=event_id)
        return existing, False
    fulfillment = SimFulfillment(project_id=order.project_id, order_id=order.id, operation_key=key, state=FulfillmentState.PENDING.value)
    db.add(fulfillment)
    db.flush()
    audit(db, order.project_id, AuditAction.FULFILLMENT_CREATED, entity_type="fulfillment", entity_id=fulfillment.id, processor=processor,
          detail={"order_id": order.id}, event_id=event_id)
    return fulfillment, True


def dispatch_fulfillment(db: Session, fulfillment: SimFulfillment, *, processor: Processor = Processor.SAFE) -> bool:
    """Mark a parcel as left. Irreversible. Returns False (and records nothing new) if it was already dispatched."""
    if fulfillment.state == FulfillmentState.DISPATCHED.value:
        return False
    if fulfillment.state != FulfillmentState.PENDING.value:
        raise SimulationError("only a pending fulfillment can be dispatched")
    fulfillment.state = FulfillmentState.DISPATCHED.value
    fulfillment.dispatched_at = monotonic_utcnow()
    db.flush()
    audit(db, fulfillment.project_id, AuditAction.FULFILLMENT_DISPATCHED, entity_type="fulfillment", entity_id=fulfillment.id,
          processor=processor, detail={"order_id": fulfillment.order_id, "dispatched_at": fulfillment.dispatched_at.isoformat()})
    order = db.get(SimOrder, fulfillment.order_id)
    if order is not None and order.state == OrderState.PAID.value:
        transition_order(db, order, OrderState.FULFILLED, processor=processor, reason="dispatched")
    return True


def cancel_fulfillment(db: Session, fulfillment: SimFulfillment, *, reason: str, processor: Processor = Processor.SAFE,
                       event_id: str | None = None) -> bool:
    """Cancel a pending shipment. A shipment that already left cannot be cancelled: that is recorded, never forced."""
    if fulfillment.state == FulfillmentState.DISPATCHED.value:
        audit(db, fulfillment.project_id, AuditAction.DISPATCH_PROTECTED, entity_type="fulfillment", entity_id=fulfillment.id,
              processor=processor, detail={"reason": reason, "dispatched_at": fulfillment.dispatched_at.isoformat()}, event_id=event_id)
        return False
    if fulfillment.state == FulfillmentState.CANCELLED.value:
        return False
    fulfillment.state = FulfillmentState.CANCELLED.value
    db.flush()
    audit(db, fulfillment.project_id, AuditAction.FULFILLMENT_CANCELLED, entity_type="fulfillment", entity_id=fulfillment.id,
          processor=processor, detail={"reason": reason}, event_id=event_id)
    return True


def dispatch_evidence(db: Session, project_id: str, order_id: str) -> dict[str, Any]:
    """Whether goods for this order actually left, from the fulfillment rows AND their immutable audit records."""
    rows = list(db.scalars(select(SimFulfillment).where(SimFulfillment.project_id == project_id, SimFulfillment.order_id == order_id)))
    ids = [r.id for r in rows]
    dispatch_records = list(db.scalars(select(SimAuditRecord).where(
        SimAuditRecord.project_id == project_id, SimAuditRecord.action == AuditAction.FULFILLMENT_DISPATCHED.value,
        SimAuditRecord.entity_id.in_(ids)))) if ids else []
    return {"order_id": order_id, "fulfillment_ids": ids, "dispatched": any(r.state == FulfillmentState.DISPATCHED.value for r in rows),
            "dispatch_audit_record_ids": [r.id for r in dispatch_records]}


# --- incoming events ----------------------------------------------------------------------------------------------------------

def receive_event(db: Session, project_id: str, *, source: EventSource, event_key: str, event_type: EventType,
                  provider_txn_ref: str | None, amount_minor: int | None, currency: str | None, asserted_at: datetime | None,
                  payload: dict[str, Any] | None = None) -> SimIncomingEvent:
    """Store one delivery. Duplicates are kept (as REPLAY_*), never merged; a delivery is stored even if it will later be rejected."""
    body = {"event_key": event_key, "event_type": event_type.value, "provider_txn_ref": provider_txn_ref, "amount_minor": amount_minor,
            "currency": currency, "asserted_at": asserted_at.isoformat() if asserted_at else None, **(payload or {})}
    sha = digest(body)
    first = db.scalar(select(SimIncomingEvent).where(
        SimIncomingEvent.project_id == project_id, SimIncomingEvent.source == source.value,
        SimIncomingEvent.event_key == event_key).order_by(SimIncomingEvent.received_at, SimIncomingEvent.id).limit(1))
    status, replay_of = ReplayStatus.FIRST, None
    if first is not None:
        status = ReplayStatus.REPLAY_IDENTICAL if first.payload_sha256 == sha else ReplayStatus.REPLAY_DIFFERENT_PAYLOAD
        replay_of = first.id
    payment_id = db.scalar(select(PaymentRecord.id).where(PaymentRecord.project_id == project_id,
                                                         PaymentRecord.paypal_transaction_id == provider_txn_ref)) if provider_txn_ref else None
    row = SimIncomingEvent(project_id=project_id, payment_id=payment_id, source=source.value, event_key=event_key,
                           event_type=event_type.value, provider_txn_ref=provider_txn_ref, amount_minor=amount_minor, currency=currency,
                           asserted_at=asserted_at, payload=body, payload_sha256=sha, replay_status=status.value, replay_of_id=replay_of)
    db.add(row)
    db.flush()
    return row


def get_event(db: Session, project_id: str, event_id: str) -> SimIncomingEvent:
    return _own(db, SimIncomingEvent, event_id, project_id)


def unprocessed_events(db: Session, project_id: str) -> list[SimIncomingEvent]:
    """Received events with no EVENT_PROCESSED record: what a crash before persist leaves behind."""
    done = select(SimAuditRecord.event_id).where(SimAuditRecord.project_id == project_id,
                                                 SimAuditRecord.action == AuditAction.EVENT_PROCESSED.value,
                                                 SimAuditRecord.event_id.is_not(None))
    return list(db.scalars(select(SimIncomingEvent).where(SimIncomingEvent.project_id == project_id, SimIncomingEvent.id.not_in(done))
                           .order_by(SimIncomingEvent.received_at, SimIncomingEvent.id)))


def is_unique_violation(exc: IntegrityError) -> bool:
    return "UNIQUE" in str(exc.orig).upper()
