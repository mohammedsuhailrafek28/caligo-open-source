"""READ-ONLY construction of guarded recovery plans from the simulated merchant state and the project's provider evidence (S2).

`build_draft` never writes. It reads PaymentRecord (provider evidence as already reconciled; it makes no provider call and cannot
capture, refund or cancel anything), the S1 merchant tables and `detect_divergences`, and returns a deterministic Draft: steps, an
evidence envelope with stable ids, provenance and completeness, reasons (why blocked / needs review, manual follow-ups), and two
fingerprints. The same function is run again at approval and at execution time; a changed fingerprint means a changed plan.

Fail-closed: anything missing, incomplete, ambiguous or contradicting yields BLOCKED or NEEDS_REVIEW, never a guess.
"""
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PaymentRecord, ProviderEvidenceGap
from app.schemas import PaymentProviderStatus as P
from app.sim_enums import (
    AuditAction, DivergenceCode as D, EventType, FulfillmentState, MovementKind, OrderState, PlanStatus, RecoveryKind, StepKind,
)
from app.sim_models import SimAuditRecord, SimFulfillment, SimIncomingEvent, SimInventoryItem, SimInventoryMovement, SimOrder
from app.simulation import store
from app.simulation.divergence import detect_divergences
from app.simulation.health import assess_integration_health

EVIDENCE_SCHEMA = "caligo.recovery_evidence.v1"
MAX_ORDERS_PER_PAYMENT = 50
SIMULATED = "SIMULATED_MERCHANT_STATE"  # provenance label of every merchant-side fact
PROVIDER = "PROVIDER_EVIDENCE_AS_RECONCILED"  # provenance label of the PaymentRecord facts (MOCK_PAYPAL in this build)
_TERMINAL = (OrderState.CANCELLED.value, OrderState.REFUNDED.value)

KIND_FOR_DIVERGENCE = {D.MULTIPLE_ORDERS_FOR_ONE_PAYMENT: RecoveryKind.DUPLICATE_ORDERS, D.DUPLICATE_INVENTORY_DEDUCTION: RecoveryKind.DUPLICATE_ORDERS,
                       D.DUPLICATE_FULFILLMENT: RecoveryKind.DUPLICATE_ORDERS, D.PROVIDER_PAID_MERCHANT_NOT_PAID: RecoveryKind.PAID_NOT_APPLIED,
                       D.PROVIDER_REFUNDED_MERCHANT_STILL_PAID: RecoveryKind.STALE_PAID_AFTER_REFUND}


class RecoveryError(Exception):
    """A request the engine cannot honor (unknown payment, no divergence of that kind). Carries a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf8")).hexdigest()


def plan_key_for(project_id: str, kind: RecoveryKind, payment_id: str | None) -> str:
    return store.operation_key("recovery-plan", project_id, kind.value, payment_id or "-")


def content_fingerprint(kind: str, payment_id: str | None, status: str, steps: list, evidence: dict, reasons: list) -> str:
    """The fingerprint an approval is bound to: everything that decides what executing the plan would do."""
    return fingerprint({"kind": kind, "payment_id": payment_id, "status": status, "steps": steps, "evidence": evidence, "reasons": reasons})


@dataclass(frozen=True)
class Draft:
    kind: RecoveryKind
    payment_id: str | None
    status: PlanStatus
    steps: list[dict[str, Any]]
    evidence: dict[str, Any]
    reasons: list[dict[str, Any]]
    provider_fingerprint: str
    evidence_fingerprint: str
    plan_key: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def _reason(code: str, message: str, kind: str = "BLOCKER", **detail: Any) -> dict[str, Any]:
    return {"code": code, "type": kind, "message": message, **({"detail": detail} if detail else {})}


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# --- evidence facts -------------------------------------------------------------------------------------------------------------

def payment_facts(db: Session, payment: PaymentRecord) -> dict[str, Any]:
    gaps = db.scalar(select(func.count()).select_from(ProviderEvidenceGap).where(ProviderEvidenceGap.payment_id == payment.id)) or 0
    return {"ref": f"payment:{payment.id}", "id": payment.id, "provider": payment.provider, "provider_status": payment.provider_status,
            "amount_minor": payment.amount_minor, "amount_paid_minor": payment.amount_paid_minor,
            "amount_refunded_minor": payment.amount_refunded_minor, "currency": payment.currency,
            "paypal_transaction_id": payment.paypal_transaction_id, "evidence_gap_count": gaps, "provenance": PROVIDER}


def provider_fingerprint_of(facts: dict[str, Any] | None) -> str:
    if facts is None:
        return fingerprint({"provider": None})
    return fingerprint(facts)


def _order_facts(db: Session, order: SimOrder) -> dict[str, Any]:
    fulfillments = list(db.scalars(select(SimFulfillment).where(SimFulfillment.order_id == order.id).order_by(SimFulfillment.created_at, SimFulfillment.id)))
    movements = list(db.scalars(select(SimInventoryMovement).where(SimInventoryMovement.order_id == order.id)
                                .order_by(SimInventoryMovement.created_at, SimInventoryMovement.id)))
    held = sum((-1 if m.kind == MovementKind.RELEASE.value else 1) * m.quantity for m in movements)
    created = db.scalar(select(SimAuditRecord).where(SimAuditRecord.entity_id == order.id, SimAuditRecord.action == AuditAction.ORDER_CREATED.value))
    event = db.get(SimIncomingEvent, created.event_id) if created is not None and created.event_id else None
    return {"ref": f"order:{order.id}", "id": order.id, "state": order.state, "quantity": order.quantity, "item_id": order.item_id,
            "amount_minor": order.amount_minor, "currency": order.currency, "created_at": _iso(order.created_at),
            "creating_event": None if event is None else {"ref": f"event:{event.id}", "id": event.id, "event_key": event.event_key,
                                                          "replay_status": event.replay_status, "received_at": _iso(event.received_at)},
            "fulfillments": [{"ref": f"fulfillment:{f.id}", "id": f.id, "state": f.state, "dispatched_at": _iso(f.dispatched_at)} for f in fulfillments],
            "stock_units_held": held, "movement_ids": [m.id for m in movements],
            "dispatched": any(f.state == FulfillmentState.DISPATCHED.value for f in fulfillments), "provenance": SIMULATED}


def _citations(evidence: dict[str, Any]) -> list[dict[str, str]]:
    refs = []
    if evidence.get("payment"):
        refs.append(evidence["payment"]["ref"])
    for order in evidence.get("orders", []):
        refs.append(order["ref"])
        refs.extend(f["ref"] for f in order["fulfillments"])
        if order["creating_event"]:
            refs.append(order["creating_event"]["ref"])
    refs.extend(e["ref"] for e in evidence.get("events", []))
    refs.extend(r["ref"] for r in evidence.get("regression_records", []))
    return [{"ref": ref, "kind": ref.split(":", 1)[0], "id": ref.split(":", 1)[1]} for ref in dict.fromkeys(refs)]


def _envelope(kind: RecoveryKind, payment: dict | None, orders: list[dict], *, events: list[dict] | None = None,
              regression_records: list[dict] | None = None, health: dict | None = None, missing: list[str] | None = None) -> dict[str, Any]:
    evidence = {"schema": EVIDENCE_SCHEMA, "kind": kind.value, "payment": payment, "orders": orders, "events": events or [],
                "regression_records": regression_records or [], "health": health,
                "completeness": {"complete": not missing, "missing": sorted(missing or [])},
                "provenance": {"provider": PROVIDER, "merchant": SIMULATED, "note": "Merchant state is simulated and never provider truth; "
                                                                                    "no provider call is made to build this evidence."}}
    evidence["citations"] = _citations(evidence)
    return evidence


# --- shared gates ---------------------------------------------------------------------------------------------------------------

def _provider_blockers(facts: dict[str, Any], *, require: str) -> list[dict[str, Any]]:
    out = []
    if facts["evidence_gap_count"]:
        out.append(_reason("INCOMPLETE_PROVIDER_EVIDENCE", "Provider evidence for this payment is incomplete; absence of evidence is not evidence."))
    if not facts["paypal_transaction_id"]:
        out.append(_reason("MISSING_PROVIDER_TRANSACTION", "The payment has no provider transaction reference."))
    if facts["provider_status"] != require:
        out.append(_reason("PROVIDER_STATE_MISMATCH", f"Recovery of this kind needs provider evidence of {require}.",
                           provider_status=facts["provider_status"], required=require))
    elif require == P.PAID.value and facts["amount_paid_minor"] < facts["amount_minor"]:
        out.append(_reason("PROVIDER_AMOUNT_NOT_FULLY_PAID", "Provider evidence does not show the full amount as paid."))
    return out


def _order_blockers(facts: dict[str, Any], orders: list[dict]) -> list[dict[str, Any]]:
    bad = [o["id"] for o in orders if o["amount_minor"] != facts["amount_minor"] or o["currency"] != facts["currency"]]
    return [_reason("AMOUNT_OR_CURRENCY_MISMATCH", "An order does not agree with the payment's amount and currency.", order_ids=bad)] if bad else []


def _load_orders(db: Session, project_id: str, payment_id: str) -> list[SimOrder]:
    return list(db.scalars(select(SimOrder).where(SimOrder.project_id == project_id, SimOrder.payment_id == payment_id)
                           .order_by(SimOrder.created_at, SimOrder.id).limit(MAX_ORDERS_PER_PAYMENT + 1)))


def _finish(kind: RecoveryKind, payment_id: str | None, status: PlanStatus, steps: list, evidence: dict, reasons: list,
            provider_fp: str, project_id: str, **extras: Any) -> Draft:
    return Draft(kind, payment_id, status, steps, evidence, reasons, provider_fp,
                 content_fingerprint(kind.value, payment_id, status.value, steps, evidence, reasons), plan_key_for(project_id, kind, payment_id), extras)


def _status(blockers: list[dict], review: list[dict]) -> PlanStatus:
    return PlanStatus.BLOCKED if blockers else PlanStatus.NEEDS_REVIEW if review else PlanStatus.PROPOSED


def _step(kind: StepKind, ordinal: int, **params: Any) -> dict[str, Any]:
    return {"step_id": f"{ordinal:02d}-{kind.value}", "kind": kind.value, "params": params}


# --- A: one payment, several orders ---------------------------------------------------------------------------------------------

def _canonical_order(live: list[dict], everything: list[dict]) -> tuple[dict | None, str, list[dict]]:
    """(canonical order, basis, review reasons). Deterministic: dispatched goods decide first, then the first delivery."""
    shipped = [o for o in everything if o["dispatched"]]
    if len(shipped) >= 2:
        return None, "", [_reason("MULTIPLE_DISPATCHED_ORDERS", "More than one order already shipped; which one counts cannot be decided from evidence.",
                                  "REVIEW", order_ids=[o["id"] for o in shipped])]
    if len(shipped) == 1:
        if shipped[0]["state"] in _TERMINAL:
            return None, "", [_reason("DISPATCHED_ORDER_NOT_LIVE", "The only shipped order is already cancelled or refunded.", "REVIEW", order_id=shipped[0]["id"])]
        return shipped[0], "ONLY_DISPATCHED_ORDER", []
    firsts = [o for o in live if o["creating_event"] and o["creating_event"]["replay_status"] == "FIRST"]
    if len(firsts) == 1:
        return firsts[0], "FIRST_DELIVERY_OF_THE_PAID_EVENT", []
    return None, "", [_reason("CANONICAL_ORDER_AMBIGUOUS", "No single order can be identified as the original from the recorded evidence.", "REVIEW",
                              candidates=[o["id"] for o in live])]


def _duplicate_steps(duplicate: dict, ordinal: int) -> list[dict[str, Any]]:
    steps = []
    for f in duplicate["fulfillments"]:
        if f["state"] == FulfillmentState.PENDING.value:
            steps.append(_step(StepKind.CANCEL_DUPLICATE_FULFILLMENT, ordinal + len(steps), order_id=duplicate["id"], fulfillment_id=f["id"]))
    if duplicate["stock_units_held"] > 0:
        steps.append(_step(StepKind.RELEASE_STOCK, ordinal + len(steps), order_id=duplicate["id"], item_id=duplicate["item_id"],
                           quantity=duplicate["stock_units_held"], operation_key=store.operation_key("recovery-dup-release", duplicate["id"])))
    if duplicate["state"] != OrderState.CANCELLED.value:
        steps.append(_step(StepKind.CANCEL_DUPLICATE_ORDER, ordinal + len(steps), order_id=duplicate["id"], from_state=duplicate["state"]))
    return steps


def _draft_duplicates(db: Session, project_id: str, payment: PaymentRecord) -> Draft:
    facts = payment_facts(db, payment)
    orders = _load_orders(db, project_id, payment.id)
    order_facts = [_order_facts(db, o) for o in orders[:MAX_ORDERS_PER_PAYMENT]]
    blockers = _provider_blockers(facts, require=P.PAID.value) + _order_blockers(facts, order_facts)
    review: list[dict] = []
    if len(orders) > MAX_ORDERS_PER_PAYMENT:
        review.append(_reason("TOO_MANY_ORDERS", "Too many orders for one payment to reason about safely.", "REVIEW"))
    live = [o for o in order_facts if o["state"] not in _TERMINAL]
    canonical_order, basis, why = _canonical_order(live, order_facts) if not review else (None, "", [])
    review += why
    steps: list[dict] = []
    reasons = blockers + review
    if canonical_order is not None and not blockers:
        if canonical_order["state"] not in (OrderState.PAID.value, OrderState.FULFILLED.value):
            review.append(_reason("CANONICAL_ORDER_NOT_PAID", "The canonical order is not in a paid state.", "REVIEW", order_id=canonical_order["id"]))
            reasons = blockers + review
        else:
            for duplicate in (o for o in live if o["id"] != canonical_order["id"]):
                steps += _duplicate_steps(duplicate, len(steps) + 1)
            reasons += [_reason("CANONICAL_ORDER_SELECTED", f"Canonical order chosen by rule {basis}.", "INFO", order_id=canonical_order["id"], basis=basis)]
            reasons += [_reason("NO_REVERSAL_OF_DISPATCH", "Dispatched shipments are never reversed; only undispatched duplicates are corrected.", "INFO")]
    evidence = _envelope(RecoveryKind.DUPLICATE_ORDERS, facts, order_facts,
                         missing=["provider_evidence"] if blockers else [])
    evidence["canonical_order_id"] = canonical_order["id"] if canonical_order else None
    evidence["canonical_basis"] = basis or None
    status = _status(blockers, review)
    if status is PlanStatus.PROPOSED and not steps:
        status, reasons = PlanStatus.NEEDS_REVIEW, reasons + [_reason("NOTHING_SAFE_TO_CORRECT", "No safe local correction exists for this divergence.", "REVIEW")]
    return _finish(RecoveryKind.DUPLICATE_ORDERS, payment.id, status, steps, evidence, reasons, provider_fingerprint_of(facts), project_id)


# --- B: provider paid, merchant did not apply it --------------------------------------------------------------------------------

def _paid_event_candidates(db: Session, project_id: str, payment: PaymentRecord) -> list[SimIncomingEvent]:
    rows = db.scalars(select(SimIncomingEvent).where(
        SimIncomingEvent.project_id == project_id, SimIncomingEvent.payment_id == payment.id,
        SimIncomingEvent.event_type == EventType.PAYMENT_COMPLETED.value, SimIncomingEvent.amount_minor == payment.amount_minor,
        SimIncomingEvent.currency == payment.currency, SimIncomingEvent.replay_status != "REPLAY_DIFFERENT_PAYLOAD")
        .order_by(SimIncomingEvent.received_at, SimIncomingEvent.id).limit(20))
    return list(rows)


def recovery_event_key(payment_id: str) -> str:
    return f"recovery-paid|{payment_id}"


def _draft_paid_not_applied(db: Session, project_id: str, payment: PaymentRecord) -> Draft:
    facts = payment_facts(db, payment)
    orders = _load_orders(db, project_id, payment.id)
    order_facts = [_order_facts(db, o) for o in orders[:MAX_ORDERS_PER_PAYMENT]]
    candidates = _paid_event_candidates(db, project_id, payment)
    events = [{"ref": f"event:{e.id}", "id": e.id, "event_key": e.event_key, "replay_status": e.replay_status, "received_at": _iso(e.received_at),
               "asserted_at": _iso(e.asserted_at), "payload_sha256": e.payload_sha256, "provenance": "SIMULATED_INCOMING_EVENT"} for e in candidates]
    blockers = _provider_blockers(facts, require=P.PAID.value) + _order_blockers(facts, order_facts)
    review: list[dict] = []
    if len(order_facts) > 1:
        review.append(_reason("MULTIPLE_ORDERS", "More than one order exists for this payment; resolve duplicates first.", "REVIEW"))
    order = order_facts[0] if len(order_facts) == 1 else None
    if order is not None and order["state"] not in (OrderState.PENDING.value, OrderState.FAILED.value):
        blockers.append(_reason("ORDER_NOT_AWAITING_PAYMENT", f"The order is {order['state']}, not PENDING or FAILED.", order_state=order["state"]))
    steps: list[dict] = []
    reasons = blockers + review
    if not blockers and not review:
        if candidates:
            steps.append(_step(StepKind.REPLAY_PAID_EVENT, 1, event_id=candidates[0].id, synthesize=None))
        elif order is not None:
            sku = db.scalar(select(SimInventoryItem.sku).where(SimInventoryItem.id == order["item_id"]))
            steps.append(_step(StepKind.REPLAY_PAID_EVENT, 1, event_id=None,
                               synthesize={"event_key": recovery_event_key(payment.id), "source": "SIMULATED_MERCHANT_JOB", "sku": sku,
                                           "quantity": order["quantity"]}))
        else:
            blockers.append(_reason("NO_EVENT_AND_NO_ORDER", "Neither a received paid event nor a merchant order identifies what was bought; nothing is invented."))
            reasons = blockers
    if steps:
        reasons += [_reason("USES_IDEMPOTENT_PROCESSOR", "Recovery replays the paid event through the existing safe processor; no capture is initiated.", "INFO")]
    evidence = _envelope(RecoveryKind.PAID_NOT_APPLIED, facts, order_facts, events=events, missing=["provider_evidence"] if blockers else [])
    return _finish(RecoveryKind.PAID_NOT_APPLIED, payment.id, _status(blockers, review), steps, evidence, reasons, provider_fingerprint_of(facts), project_id)


# --- C: stale paid event after a refund -----------------------------------------------------------------------------------------

def _regression_records(db: Session, project_id: str, order_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(SimAuditRecord).where(SimAuditRecord.project_id == project_id, SimAuditRecord.entity_id == order_id,
                                                   SimAuditRecord.action == AuditAction.ORDER_STATE_CHANGED.value).order_by(SimAuditRecord.created_at))
    return [{"ref": f"audit:{r.id}", "id": r.id, "from": r.detail.get("from"), "to": r.detail.get("to"), "processor": r.processor,
             "illegal_regression": bool(r.detail.get("illegal_regression")), "created_at": _iso(r.created_at), "provenance": SIMULATED}
            for r in rows if r.detail.get("illegal_regression") or (r.detail.get("from") == OrderState.REFUNDED.value)]


def _draft_stale_paid(db: Session, project_id: str, payment: PaymentRecord) -> Draft:
    facts = payment_facts(db, payment)
    orders = _load_orders(db, project_id, payment.id)
    order_facts = [_order_facts(db, o) for o in orders[:MAX_ORDERS_PER_PAYMENT]]
    blockers = _provider_blockers(facts, require=P.REFUNDED.value) + _order_blockers(facts, order_facts)
    paid = [o for o in order_facts if o["state"] in (OrderState.PAID.value, OrderState.FULFILLED.value)]
    review = [_reason("MULTIPLE_PAID_ORDERS", "More than one order still counts as paid; resolve duplicates first.", "REVIEW")] if len(paid) > 1 else []
    regression = _regression_records(db, project_id, paid[0]["id"]) if len(paid) == 1 else []
    steps: list[dict] = []
    reasons = blockers + review
    if len(paid) == 1 and not blockers:
        order = paid[0]
        steps.append(_step(StepKind.ORDER_TO_REFUNDED, 1, order_id=order["id"], from_state=order["state"]))
        pending = [f for f in order["fulfillments"] if f["state"] == FulfillmentState.PENDING.value]
        steps += [_step(StepKind.CANCEL_PENDING_FULFILLMENT, len(steps) + 1, order_id=order["id"], fulfillment_id=f["id"]) for f in pending]
        if order["dispatched"]:
            reasons.append(_reason("GOODS_ALREADY_SHIPPED", "Goods already left for a refunded payment; stock is NOT restored and the shipment is not reversed. "
                                   "The seller must decide the real-world follow-up.", "FOLLOW_UP", order_id=order["id"]))
        elif order["stock_units_held"] > 0:
            steps.append(_step(StepKind.RELEASE_STOCK, len(steps) + 1, order_id=order["id"], item_id=order["item_id"], quantity=order["stock_units_held"],
                               operation_key=store.operation_key("recovery-refund-release", order["id"], str(len(order["movement_ids"])))))
        reasons.append(_reason("NO_FUND_STATEMENT", "No refund is reversed or issued, and nothing here asserts that funds were restored; "
                               "only the local order state is aligned with the provider's refund evidence.", "INFO"))
    evidence = _envelope(RecoveryKind.STALE_PAID_AFTER_REFUND, facts, order_facts, regression_records=regression,
                         missing=["provider_evidence"] if blockers else [])
    status = _status(blockers, review)
    if status is PlanStatus.PROPOSED and not steps:
        status, reasons = PlanStatus.NEEDS_REVIEW, reasons + [_reason("NOTHING_SAFE_TO_CORRECT", "No safe local correction exists.", "REVIEW")]
    return _finish(RecoveryKind.STALE_PAID_AFTER_REFUND, payment.id, status, steps, evidence, reasons, provider_fingerprint_of(facts), project_id)


# --- D: false healthy (diagnostic only) -----------------------------------------------------------------------------------------

def _draft_false_healthy(db: Session, project_id: str, *, credentials_configured: bool, heartbeat_ok: bool) -> Draft:
    assessment = assess_integration_health(db, project_id, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
    if assessment.end_to_end == "VERIFIED":
        raise RecoveryError("no_divergence", "An end-to-end payment is already proven for this project; there is nothing to diagnose.")
    health = {"configuration": assessment.configuration, "heartbeat": assessment.heartbeat, "end_to_end": assessment.end_to_end,
              "overall": assessment.overall, "reasons": list(assessment.reasons),
              "inputs_provenance": "CALLER_REPORTED (configuration and heartbeat are claims, not observations)"}
    reasons = [_reason("END_TO_END_NOT_VERIFIED", "Configuration and a heartbeat do not prove that a payment reaches the merchant.", "DIAGNOSIS"),
               _reason("RECOMMENDED_VERIFICATION", "Run one real end-to-end test payment through the provider's own test flow (the owner's Sandbox), "
                       "let the merchant process its event, then confirm the order is applied and reconciled. Caligo has not done this and does not fabricate it.",
                       "RECOMMENDATION"),
               _reason("NO_MUTATION_NEEDED", "No financial or business-state change is made by this plan.", "INFO")]
    evidence = _envelope(RecoveryKind.FALSE_HEALTHY, None, [], health=health, missing=["end_to_end_verification"])
    return _finish(RecoveryKind.FALSE_HEALTHY, None, PlanStatus.ADVISORY, [], evidence, reasons, provider_fingerprint_of(None), project_id)


# --- entry point ----------------------------------------------------------------------------------------------------------------

_BUILDERS = {RecoveryKind.DUPLICATE_ORDERS: _draft_duplicates, RecoveryKind.PAID_NOT_APPLIED: _draft_paid_not_applied,
             RecoveryKind.STALE_PAID_AFTER_REFUND: _draft_stale_paid}


def divergence_exists(db: Session, project_id: str, kind: RecoveryKind, payment_id: str) -> bool:
    return any(d.payment_id == payment_id and KIND_FOR_DIVERGENCE.get(d.code) is kind for d in detect_divergences(db, project_id))


def build_draft(db: Session, project_id: str, kind: RecoveryKind, payment_id: str | None, *, credentials_configured: bool = True,
                heartbeat_ok: bool = True) -> Draft:
    """Deterministic, read-only. Raises RecoveryError for an unknown payment or when no divergence of this kind exists."""
    if kind is RecoveryKind.FALSE_HEALTHY:
        if payment_id is not None:
            raise RecoveryError("invalid_request", "A false-healthy diagnosis is project-wide and takes no payment.")
        return _draft_false_healthy(db, project_id, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
    if payment_id is None:
        raise RecoveryError("invalid_request", "This kind of recovery needs a payment.")
    payment = db.get(PaymentRecord, payment_id)
    if payment is None or payment.project_id != project_id:
        raise RecoveryError("not_found", "Payment not found.")
    if not divergence_exists(db, project_id, kind, payment_id):
        raise RecoveryError("no_divergence", "No divergence of this kind is currently detected for this payment.")
    return _BUILDERS[kind](db, project_id, payment)
