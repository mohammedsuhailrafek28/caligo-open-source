"""Read-only detection of divergence between provider payment state (PaymentRecord, from verified provider evidence) and the simulated
merchant state. It does not reconcile, repair or duplicate the payment state machine: PaymentRecord is only READ. Merchant state is
never treated as provider truth; a divergence is a finding for a human, not an instruction to change either side."""
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PaymentRecord
from app.schemas import PaymentProviderStatus as P
from app.sim_enums import DivergenceCode as D, FulfillmentState, MovementKind, OrderState
from app.sim_models import SimFulfillment, SimInventoryMovement, SimOrder

_PROVIDER_PAID = (P.PAID.value,)
_PROVIDER_REFUNDED = (P.REFUNDED.value,)
_MERCHANT_PAID = (OrderState.PAID.value, OrderState.FULFILLED.value)
_NOT_PAID = (OrderState.PENDING.value, OrderState.FAILED.value)


@dataclass(frozen=True)
class Divergence:
    code: D
    payment_id: str
    order_ids: tuple[str, ...]
    detail: dict = field(default_factory=dict)


def detect_divergences(db: Session, project_id: str) -> list[Divergence]:
    payments = list(db.scalars(select(PaymentRecord).where(PaymentRecord.project_id == project_id).order_by(PaymentRecord.created_at, PaymentRecord.id)))
    orders: dict[str, list[SimOrder]] = {}
    for order in db.scalars(select(SimOrder).where(SimOrder.project_id == project_id).order_by(SimOrder.created_at, SimOrder.id)):
        orders.setdefault(order.payment_id, []).append(order)
    found: list[Divergence] = []
    for payment in payments:
        found.extend(_for_payment(db, project_id, payment, orders.get(payment.id, [])))
    return found


def _for_payment(db: Session, project_id: str, payment: PaymentRecord, orders: list[SimOrder]) -> list[Divergence]:
    ids = tuple(o.id for o in orders)
    out: list[Divergence] = []
    if payment.provider_status in _PROVIDER_PAID and not any(o.state in _MERCHANT_PAID for o in orders):
        out.append(Divergence(D.PROVIDER_PAID_MERCHANT_NOT_PAID, payment.id, ids,
                              {"order_states": [o.state for o in orders] or ["NO_ORDER"], "provider_status": payment.provider_status}))
    if payment.provider_status in _PROVIDER_REFUNDED and any(o.state in _MERCHANT_PAID for o in orders):
        out.append(Divergence(D.PROVIDER_REFUNDED_MERCHANT_STILL_PAID, payment.id, tuple(o.id for o in orders if o.state in _MERCHANT_PAID),
                              {"goods_shipped": _shipped(db, project_id, [o.id for o in orders])}))
    if orders and payment.provider_status not in (*_PROVIDER_PAID, P.PARTIALLY_REFUNDED.value, *_PROVIDER_REFUNDED) \
            and any(o.state in _MERCHANT_PAID for o in orders):
        out.append(Divergence(D.MERCHANT_PAID_PROVIDER_NOT_PAID, payment.id, ids, {"provider_status": payment.provider_status}))
    live = [o for o in orders if o.state != OrderState.CANCELLED.value]  # a cancelled duplicate no longer counts as a second order
    if len(live) > 1:
        out.append(Divergence(D.MULTIPLE_ORDERS_FOR_ONE_PAYMENT, payment.id, tuple(o.id for o in live), {"order_count": len(live)}))
    if len(orders) > 1:
        out.extend(_duplicate_effects(db, project_id, payment, orders))
    for order in orders:
        if order.amount_minor != payment.amount_minor or order.currency != payment.currency:
            out.append(Divergence(D.ORDER_AMOUNT_OR_CURRENCY_MISMATCH, payment.id, (order.id,),
                                  {"order": [order.amount_minor, order.currency], "payment": [payment.amount_minor, payment.currency]}))
    return out


def _duplicate_effects(db: Session, project_id: str, payment: PaymentRecord, orders: list[SimOrder]) -> list[Divergence]:
    ids = [o.id for o in orders]
    deductions = db.scalar(select(func.count()).select_from(SimInventoryMovement).where(
        SimInventoryMovement.project_id == project_id, SimInventoryMovement.order_id.in_(ids), SimInventoryMovement.kind == MovementKind.DEDUCT.value))
    holding = _orders_holding_stock(db, project_id, ids)
    fulfillments = db.scalar(select(func.count()).select_from(SimFulfillment).where(
        SimFulfillment.project_id == project_id, SimFulfillment.order_id.in_(ids), SimFulfillment.state != FulfillmentState.CANCELLED.value))
    out = []
    if holding > 1:  # more than one order of this payment still holds deducted stock (net of releases), even if some are cancelled
        out.append(Divergence(D.DUPLICATE_INVENTORY_DEDUCTION, payment.id, tuple(ids), {"deduction_movements": deductions, "orders_holding_stock": holding}))
    if fulfillments > 1:
        out.append(Divergence(D.DUPLICATE_FULFILLMENT, payment.id, tuple(ids),
                              {"live_fulfillments": fulfillments, "dispatched": _shipped(db, project_id, ids)}))
    return out


def _orders_holding_stock(db: Session, project_id: str, order_ids: list[str]) -> int:
    net: dict[str, int] = {}
    for row in db.scalars(select(SimInventoryMovement).where(SimInventoryMovement.project_id == project_id,
                                                              SimInventoryMovement.order_id.in_(order_ids))):
        sign = -1 if row.kind == MovementKind.RELEASE.value else 1
        net[row.order_id] = net.get(row.order_id, 0) + sign * row.quantity
    return sum(1 for units in net.values() if units > 0)


def _shipped(db: Session, project_id: str, order_ids: list[str]) -> int:
    """How many parcels actually left (evidence that cannot be assumed away or undone)."""
    return db.scalar(select(func.count()).select_from(SimFulfillment).where(
        SimFulfillment.project_id == project_id, SimFulfillment.order_id.in_(order_ids),
        SimFulfillment.state == FulfillmentState.DISPATCHED.value)) or 0
