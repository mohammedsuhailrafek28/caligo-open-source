"""The four failure simulations (A-D), as plain functions over a session, a project, a provider-side PaymentRecord and an inventory item.

The provider side is never simulated here: callers pass a PaymentRecord produced by the existing fake/MOCK provider flow, so provider
evidence stays explicitly simulated (MOCK_PAYPAL) and untouched. Every event is a SIMULATED_* source. These functions use the faulty
fixtures on purpose; they are for tests and demos, never for an application route.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaymentRecord
from app.sim_enums import AuditAction, EventSource, EventType, FulfillmentState, OrderState
from app.sim_models import SimAuditRecord, SimFulfillment, SimInventoryItem, SimOrder
from app.simulation import faulty, store
from app.simulation.divergence import Divergence, detect_divergences
from app.simulation.health import HealthAssessment, assess_integration_health
from app.simulation.processor import preview_event, process_event

SOURCE = EventSource.SIMULATED_PROVIDER_WEBHOOK


class SimulatedCrash(RuntimeError):
    """Raised by a failure-injection hook to model the process dying before its work was persisted."""


@dataclass(frozen=True)
class ScenarioOutcome:
    name: str
    facts: dict[str, Any]
    divergences: tuple[Divergence, ...] = field(default_factory=tuple)

    @property
    def divergence_codes(self) -> list[str]:
        return [d.code.value for d in self.divergences]


def _deliver(db: Session, project_id: str, payment: PaymentRecord, item: SimInventoryItem, *, key: str, kind: EventType, quantity: int,
             asserted_at: datetime, amount_minor: int | None = None):
    event = store.receive_event(
        db, project_id, source=SOURCE, event_key=key, event_type=kind, provider_txn_ref=payment.paypal_transaction_id,
        amount_minor=payment.amount_minor if amount_minor is None else amount_minor, currency=payment.currency, asserted_at=asserted_at,
        payload={"sku": item.sku, "quantity": quantity, "simulated": True})
    db.commit()  # a receipt is durable before any processing starts
    return event


def _live_fulfillments(db: Session, project_id: str, order_ids: list[str]) -> list[SimFulfillment]:
    return list(db.scalars(select(SimFulfillment).where(SimFulfillment.project_id == project_id, SimFulfillment.order_id.in_(order_ids))
                           .order_by(SimFulfillment.created_at)))


def scenario_a_one_payment_three_orders(db: Session, project_id: str, payment: PaymentRecord, item: SimInventoryItem, *,
                                        deliveries: int = 3, dispatched: int = 2, quantity: int = 1) -> ScenarioOutcome:
    """One paid event delivered `deliveries` times to a non-idempotent merchant: duplicate orders, deductions and fulfillments. The
    first `dispatched` parcels leave before anyone notices; they are evidence, not state that can be rolled back."""
    when = datetime.now(timezone.utc)
    events = [_deliver(db, project_id, payment, item, key="evt-one-payment", kind=EventType.PAYMENT_COMPLETED, quantity=quantity,
                       asserted_at=when) for _ in range(deliveries)]
    order_ids = [oid for e in events if (oid := faulty.process_event_naively(db, project_id, e.id))]
    for fulfillment in _live_fulfillments(db, project_id, order_ids)[:dispatched]:
        store.dispatch_fulfillment(db, fulfillment, processor=faulty.FAULTY)
    db.commit()
    shipped = [f.order_id for f in _live_fulfillments(db, project_id, order_ids) if f.state == FulfillmentState.DISPATCHED.value]
    facts = {"replay_statuses": [e.replay_status for e in events], "order_ids": order_ids, "provider_status": payment.provider_status,
             "stock_balance": store.stock_balance(db, item.id), "expected_stock_balance": item.initial_stock - quantity,
             "dispatched_order_ids": shipped, "dispatch_evidence": [store.dispatch_evidence(db, project_id, o) for o in order_ids]}
    return ScenarioOutcome("A_ONE_PAYMENT_THREE_ORDERS", facts, tuple(detect_divergences(db, project_id)))


def scenario_b_payment_success_merchant_failed(db: Session, project_id: str, payment: PaymentRecord, item: SimInventoryItem, *,
                                               quantity: int = 1) -> ScenarioOutcome:
    """The provider evidence says PAID; the merchant crashed while handling the paid event, before persisting anything."""
    order, _ = store.create_order(db, project_id, payment, item, quantity)
    db.commit()
    event = _deliver(db, project_id, payment, item, key="evt-crash", kind=EventType.PAYMENT_COMPLETED, quantity=quantity,
                     asserted_at=datetime.now(timezone.utc))

    def crash(step: str) -> None:
        if step == "after_inventory":
            raise SimulatedCrash("process died before commit")

    crashed = False
    try:
        process_event(db, project_id, event.id, fail_at=crash)
    except SimulatedCrash:
        crashed = True
    db.expire_all()
    facts = {"crashed": crashed, "provider_status": payment.provider_status, "order_state": db.get(SimOrder, order.id).state,
             "unprocessed_event_ids": [e.id for e in store.unprocessed_events(db, project_id)],
             "stock_balance": store.stock_balance(db, item.id), "initial_stock": item.initial_stock}
    return ScenarioOutcome("B_PAYMENT_SUCCESS_MERCHANT_FAILED", facts, tuple(detect_divergences(db, project_id)))


def scenario_c_stale_paid_after_refund(db: Session, project_id: str, payment: PaymentRecord, item: SimInventoryItem, *,
                                       quantity: int = 1) -> ScenarioOutcome:
    """Precondition: the provider evidence shows REFUNDED and the merchant order is PAID (applied earlier by the safe processor).
    A refund event is processed safely; then an OLDER paid event arrives late. The safe processor's verdict is previewed first, then a
    deliberately faulty handler regresses the order to PAID."""
    order = db.scalar(select(SimOrder).where(SimOrder.project_id == project_id, SimOrder.payment_id == payment.id))
    if order is None or order.state != OrderState.PAID.value:
        raise store.SimulationError("scenario C needs an order the safe processor already applied as PAID")
    now = datetime.now(timezone.utc)
    refund = _deliver(db, project_id, payment, item, key="evt-refund", kind=EventType.PAYMENT_REFUNDED, quantity=quantity, asserted_at=now,
                      amount_minor=payment.amount_paid_minor)
    refund_result = process_event(db, project_id, refund.id)
    stale = _deliver(db, project_id, payment, item, key="evt-paid-old", kind=EventType.PAYMENT_COMPLETED, quantity=quantity,
                     asserted_at=now - timedelta(minutes=10))  # asserted EARLIER, received LATER
    verdict = preview_event(db, project_id, stale.id)
    db.expire_all()
    before = db.get(SimOrder, order.id).state
    faulty.process_event_naively(db, project_id, stale.id)
    db.expire_all()
    regressions = [r.detail for r in db.scalars(select(SimAuditRecord).where(
        SimAuditRecord.project_id == project_id, SimAuditRecord.action == AuditAction.ORDER_STATE_CHANGED.value,
        SimAuditRecord.processor == "FAULTY_FIXTURE"))]
    facts = {"refund_outcome": refund_result.outcome.value, "safe_verdict_on_stale_event": verdict.outcome.value,
             "safe_verdict_detail": verdict.detail, "order_state_before_faulty": before, "order_state_after_faulty": db.get(SimOrder, order.id).state,
             "provider_status": payment.provider_status, "stock_balance": store.stock_balance(db, item.id), "faulty_state_changes": regressions}
    return ScenarioOutcome("C_REFUND_THEN_STALE_PAID", facts, tuple(detect_divergences(db, project_id)))


def scenario_d_false_healthy(db: Session, project_id: str, *, credentials_configured: bool = True, heartbeat_ok: bool = True) -> ScenarioOutcome:
    """Same inputs, two reports: the naive one says HEALTHY from configuration and heartbeat; the honest one reports what was proven."""
    naive = faulty.naive_health_report(credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
    honest: HealthAssessment = assess_integration_health(db, project_id, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
    facts = {"naive_report": naive, "honest": {"configuration": honest.configuration, "heartbeat": honest.heartbeat,
                                               "end_to_end": honest.end_to_end, "overall": honest.overall, "reasons": list(honest.reasons)}}
    return ScenarioOutcome("D_FALSE_HEALTHY", facts, tuple(detect_divergences(db, project_id)))
