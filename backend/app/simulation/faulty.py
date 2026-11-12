"""DELIBERATELY FAULTY merchant behavior, for explicit test/demo fixtures ONLY.

Nothing in the application (routers, main, services) may import this module; a test enforces that. Every row these functions write is
marked FAULTY_FIXTURE in the audit trail, so simulated corruption can never be mistaken for normal behavior or for evidence of what a
real provider did. They exist to reproduce the failure patterns the product must detect and, later, safely recover from.
"""
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaymentRecord
from app.sim_enums import AuditAction, EventType, MovementKind, OrderState, ProcessingOutcome, Processor
from app.sim_models import SimInventoryItem, SimOrder
from app.simulation import store

FAULTY = Processor.FAULTY_FIXTURE


def process_event_naively(db: Session, project_id: str, event_id: str) -> str | None:
    """A merchant handler that is NOT idempotent and trusts arrival order:
    * identity is the DELIVERY id, so every redelivery of a paid event creates another order, deduction and fulfillment;
    * a paid event always sets the order to PAID, even over a refund (state regression);
    * a refund event blindly sets PAID/FULFILLED orders of the payment to REFUNDED.
    Returns the id of the order it touched (if any). Commits."""
    event = store.get_event(db, project_id, event_id)
    payment = db.scalar(select(PaymentRecord).where(PaymentRecord.project_id == project_id,
                                                    PaymentRecord.paypal_transaction_id == event.provider_txn_ref))
    touched: str | None = None
    if payment is not None and event.event_type == EventType.PAYMENT_COMPLETED.value:
        touched = _naive_paid(db, event, payment)
    elif payment is not None and event.event_type == EventType.PAYMENT_REFUNDED.value:
        for order in db.scalars(select(SimOrder).where(SimOrder.payment_id == payment.id, SimOrder.project_id == project_id)):
            if order.state in (OrderState.PAID.value, OrderState.FULFILLED.value):
                store.force_order_state(db, order, OrderState.REFUNDED, processor=FAULTY, event_id=event.id, reason="naive refund handler")
                touched = order.id
    store.audit(db, project_id, AuditAction.EVENT_PROCESSED, entity_type="event", entity_id=event.id, processor=FAULTY,
                detail={"order_id": touched, "note": "FAULTY FIXTURE: non-idempotent, arrival-order handler"}, event_id=event.id,
                outcome=ProcessingOutcome.FAULTY_APPLIED.value)
    db.commit()
    return touched


def _naive_paid(db: Session, event, payment: PaymentRecord) -> str | None:
    item = db.scalar(select(SimInventoryItem).where(SimInventoryItem.project_id == event.project_id,
                                                   SimInventoryItem.sku == event.payload.get("sku")))
    quantity = event.payload.get("quantity")
    if item is None or not isinstance(quantity, int):
        return None
    existing = db.scalar(select(SimOrder).where(SimOrder.project_id == event.project_id, SimOrder.payment_id == payment.id,
                                                SimOrder.state == OrderState.REFUNDED.value))
    if existing is not None:  # stale paid event after a refund: regress the state and deduct stock again
        store.force_order_state(db, existing, OrderState.PAID, processor=FAULTY, event_id=event.id, reason="naive paid handler (stale event)")
        store.record_movement(db, event.project_id, item, MovementKind.DEDUCT, existing.quantity,
                              store.operation_key("naive-deduct", event.id), order_id=existing.id, event_id=event.id, processor=FAULTY)
        return existing.id
    order, _ = store.create_order(db, event.project_id, payment, item, quantity, key=store.operation_key("order-per-delivery", event.id),
                                  processor=FAULTY, event_id=event.id)
    store.force_order_state(db, order, OrderState.PAID, processor=FAULTY, event_id=event.id, reason="naive paid handler")
    store.record_movement(db, event.project_id, item, MovementKind.DEDUCT, quantity, store.operation_key("naive-deduct", event.id),
                          order_id=order.id, event_id=event.id, processor=FAULTY)
    store.create_fulfillment(db, order, key=store.operation_key("naive-fulfil", event.id), processor=FAULTY, event_id=event.id)
    return order.id


def naive_health_report(*, credentials_configured: bool, heartbeat_ok: bool) -> dict[str, Any]:
    """A health check that reports HEALTHY from configuration and a heartbeat alone. It never observes a payment reaching the merchant,
    which is exactly why it cannot see a broken integration."""
    healthy = credentials_configured and heartbeat_ok
    return {"status": "HEALTHY" if healthy else "UNHEALTHY", "basis": ["configuration", "heartbeat"],
            "observed_end_to_end_payment": False, "faulty_fixture": True}
