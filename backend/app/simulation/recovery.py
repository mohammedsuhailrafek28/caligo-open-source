"""Guarded recovery engine for the simulated merchant store (S2): create -> preview -> approve -> execute -> inspect.

Safety model
* Plans are immutable versions; a plan's STATE is derived from its append-only events (PLAN_CREATED, APPROVED, STEP_*, EXECUTED, refusals).
* An approval is bound to (plan id, version, evidence fingerprint) and to the approver and server time. It cannot authorize a changed plan:
  creating a plan again with changed evidence makes a NEW version and the older one is superseded.
* Every execution re-derives the plan from FRESH provider evidence and merchant state and compares fingerprints before touching anything;
  any difference, any missing/incomplete evidence, any dispatch evidence, a tampered plan or a missing approval REFUSES (and records why).
* Duplicate-order and stale-paid recovery (A, C) run all steps and their audit rows in ONE transaction: all or nothing. Paid-not-applied (B)
  replays the event through the existing idempotent SAFE processor. Every business effect has a deterministic operation key, so repeating
  it, or running a second plan, cannot apply it twice. EXECUTED is unique per plan (partial unique index).
* No provider call exists in this module: nothing is captured, refunded, cancelled or transferred. Dispatched shipments are never undone.
The faulty fixtures (app.simulation.faulty) are never imported here.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.sim_enums import (
    AuditAction, EventSource, EventType, FulfillmentState, MovementKind, OrderState, PlanState, PlanStatus, ProcessingOutcome, RecoveryEventType as Ev,
    RecoveryKind, StepKind,
)
from app.sim_models import SimAuditRecord, SimFulfillment, SimIncomingEvent, SimInventoryItem, SimOrder, SimRecoveryEvent, SimRecoveryPlan
from app.models import PaymentRecord
from app.simulation import store
from app.simulation.processor import process_event
from app.simulation.recovery_plan import (
    Draft, RecoveryError, build_draft, content_fingerprint, payment_facts, plan_key_for, provider_fingerprint_of, recovery_event_key,
)

FailPoint = Callable[[str], None]
MAX_LIST = 100
MAX_REFUSALS_SHOWN = 5  # identical refusals within the last N events are not appended again (bounds history growth)


class RecoveryRefused(Exception):
    """A guard stopped an approval or execution. The refusal has already been recorded (when a plan exists). `status` is the HTTP status."""

    def __init__(self, code: str, message: str, status: int = 409, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail or {}


@dataclass(frozen=True)
class Evaluation:
    state: PlanState
    mode: str  # EXECUTED | APPLY | ALREADY_APPLIED | BLOCKED
    blockers: list[dict[str, str]] = field(default_factory=list)
    fresh: Draft | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- loading and derived state --------------------------------------------------------------------------------------------------

def get_plan(db: Session, project_id: str, plan_id: str) -> SimRecoveryPlan:
    plan = db.get(SimRecoveryPlan, plan_id)
    if plan is None or plan.project_id != project_id:
        raise RecoveryError("not_found", "Recovery plan not found.")
    return plan


def _events(db: Session, plan_id: str) -> list[SimRecoveryEvent]:
    return list(db.scalars(select(SimRecoveryEvent).where(SimRecoveryEvent.plan_id == plan_id).order_by(SimRecoveryEvent.seq)))


def _latest_version(db: Session, project_id: str, plan_key: str) -> int:
    return db.scalar(select(func.coalesce(func.max(SimRecoveryPlan.version), 0)).where(
        SimRecoveryPlan.project_id == project_id, SimRecoveryPlan.plan_key == plan_key)) or 0


def derive_state(plan: SimRecoveryPlan, events: list[SimRecoveryEvent]) -> PlanState:
    kinds = {e.event_type for e in events}
    if Ev.EXECUTED.value in kinds:
        return PlanState.EXECUTED
    if Ev.APPROVED.value in kinds:
        return PlanState.APPROVED
    return PlanState(plan.status)


def _approval(events: list[SimRecoveryEvent]) -> SimRecoveryEvent | None:
    return next((e for e in events if e.event_type == Ev.APPROVED.value), None)


def _append(db: Session, plan: SimRecoveryPlan, kind: Ev, seller_id: str, detail: dict[str, Any], *, version: int | None = None,
            fingerprint: str | None = None) -> SimRecoveryEvent:
    seq = (db.scalar(select(func.coalesce(func.max(SimRecoveryEvent.seq), 0)).where(SimRecoveryEvent.plan_id == plan.id)) or 0) + 1
    row = SimRecoveryEvent(project_id=plan.project_id, plan_id=plan.id, seq=seq, event_type=kind.value, actor_seller_id=seller_id,
                           approved_version=version, approved_fingerprint=fingerprint, detail=detail)
    db.add(row)
    db.flush()
    return row


def _refuse(db: Session, plan: SimRecoveryPlan, seller_id: str, kind: Ev, code: str, message: str, *, status: int = 409,
            detail: dict[str, Any] | None = None, version: int | None = None, fingerprint: str | None = None) -> RecoveryRefused:
    """Record the refusal (append-only) in a fresh transaction and return the exception to raise."""
    db.rollback()
    try:
        last = _events(db, plan.id)[-MAX_REFUSALS_SHOWN:]
        if any(e.event_type == kind.value and e.detail.get("code") == code and e.actor_seller_id == seller_id and
               e.approved_fingerprint == fingerprint for e in last):
            return RecoveryRefused(code, message, status, detail)  # an identical recent refusal is already on record
        _append(db, plan, kind, seller_id, {"code": code, "message": message, **(detail or {})}, version=version, fingerprint=fingerprint)
        db.commit()
    except (IntegrityError, OperationalError, StaleDataError):
        db.rollback()  # a concurrent writer won the sequence number; the refusal itself still stands
    return RecoveryRefused(code, message, status, detail)


# --- creation -------------------------------------------------------------------------------------------------------------------

def create_plan(db: Session, project_id: str, seller_id: str, kind: RecoveryKind, payment_id: str | None, *, credentials_configured: bool = True,
                heartbeat_ok: bool = True) -> tuple[SimRecoveryPlan, bool]:
    """Build a plan from current evidence. The same evidence yields the SAME plan (idempotent); changed evidence yields a new version."""
    draft = build_draft(db, project_id, kind, payment_id, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
    existing = _by_fingerprint(db, project_id, draft)
    if existing is not None:
        return existing, False
    plan = SimRecoveryPlan(project_id=project_id, plan_key=draft.plan_key, version=_latest_version(db, project_id, draft.plan_key) + 1,
                           kind=draft.kind.value, payment_id=draft.payment_id, status=draft.status.value, steps=draft.steps, evidence=draft.evidence,
                           reasons=draft.reasons, provider_fingerprint=draft.provider_fingerprint, evidence_fingerprint=draft.evidence_fingerprint,
                           created_by_seller_id=seller_id)
    try:
        db.add(plan)
        db.flush()
        _append(db, plan, Ev.PLAN_CREATED, seller_id, {"status": plan.status, "version": plan.version, "step_count": len(plan.steps)})
        db.commit()
    except IntegrityError:  # a concurrent identical request created it first
        db.rollback()
        existing = _by_fingerprint(db, project_id, draft)
        if existing is None:
            raise
        return existing, False
    return plan, True


def _by_fingerprint(db: Session, project_id: str, draft: Draft) -> SimRecoveryPlan | None:
    """The latest version of this plan, if (and only if) it still matches the current evidence."""
    latest = db.scalar(select(SimRecoveryPlan).where(SimRecoveryPlan.project_id == project_id, SimRecoveryPlan.plan_key == draft.plan_key)
                       .order_by(SimRecoveryPlan.version.desc()).limit(1))
    return latest if latest is not None and latest.evidence_fingerprint == draft.evidence_fingerprint else None


# --- evaluation (shared by preview, approval and execution) ---------------------------------------------------------------------

def _tampered(plan: SimRecoveryPlan) -> bool:
    """A stored plan whose content no longer matches its fingerprints was altered after creation (it can only happen by bypassing the
    append-only guards): it is never approved or executed."""
    if content_fingerprint(plan.kind, plan.payment_id, plan.status, plan.steps, plan.evidence, plan.reasons) != plan.evidence_fingerprint:
        return True
    return provider_fingerprint_of(plan.evidence.get("payment")) != plan.provider_fingerprint


def _paid_replay_done(db: Session, plan: SimRecoveryPlan) -> bool:
    """B only: the paid event has ALREADY been applied by the safe processor (for example a crash after the durable step)."""
    order = db.scalar(select(SimOrder).where(SimOrder.project_id == plan.project_id, SimOrder.payment_id == plan.payment_id))
    if order is None or order.state not in (OrderState.PAID.value, OrderState.FULFILLED.value):
        return False
    applied = db.scalar(select(SimAuditRecord.id).join(SimIncomingEvent, SimIncomingEvent.id == SimAuditRecord.event_id).where(
        SimAuditRecord.project_id == plan.project_id, SimAuditRecord.action == AuditAction.EVENT_PROCESSED.value,
        SimAuditRecord.outcome == ProcessingOutcome.APPLIED.value, SimAuditRecord.processor == "SAFE",
        SimIncomingEvent.payment_id == plan.payment_id, SimIncomingEvent.event_type == EventType.PAYMENT_COMPLETED.value).limit(1))
    return applied is not None


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _evaluate(db: Session, plan: SimRecoveryPlan, events: list[SimRecoveryEvent], *, require_approval: bool) -> Evaluation:
    state = derive_state(plan, events)
    if state is PlanState.EXECUTED:
        return Evaluation(state, "EXECUTED")
    blockers: list[dict[str, str]] = []
    if plan.status != PlanStatus.PROPOSED.value:
        blockers.append(_blocker("plan_not_executable", f"A {plan.status} plan has nothing that may be executed."))
        return Evaluation(state, "BLOCKED", blockers)
    if plan.version < _latest_version(db, plan.project_id, plan.plan_key):
        blockers.append(_blocker("plan_superseded", "A newer version of this plan exists; approvals never carry over to it."))
    if _tampered(plan):
        blockers.append(_blocker("plan_tampered", "The stored plan no longer matches its fingerprint."))
    approval = _approval(events)
    if require_approval:
        if approval is None:
            blockers.append(_blocker("approval_required", "The plan has not been approved."))
        elif approval.approved_version != plan.version or approval.approved_fingerprint != plan.evidence_fingerprint:
            blockers.append(_blocker("approval_mismatch", "The approval does not match this plan version and evidence."))
    if blockers:
        return Evaluation(state, "BLOCKED", blockers)
    return _evaluate_fresh(db, plan, state)


def _evaluate_fresh(db: Session, plan: SimRecoveryPlan, state: PlanState) -> Evaluation:
    try:
        fresh = build_draft(db, plan.project_id, RecoveryKind(plan.kind), plan.payment_id)
    except RecoveryError as exc:
        if plan.kind == RecoveryKind.PAID_NOT_APPLIED.value and _paid_replay_done(db, plan):
            return Evaluation(state, "ALREADY_APPLIED")
        return Evaluation(state, "BLOCKED", [_blocker("divergence_resolved" if exc.code == "no_divergence" else exc.code, exc.message)])
    blockers: list[dict[str, str]] = []
    if fresh.provider_fingerprint != plan.provider_fingerprint:
        blockers.append(_blocker("provider_evidence_changed", "Provider evidence changed since the plan was made; create a new plan."))
    if fresh.status is not PlanStatus.PROPOSED:
        blockers += [_blocker(r["code"], r["message"]) for r in fresh.reasons if r["type"] in ("BLOCKER", "REVIEW")] or \
                    [_blocker("preconditions_no_longer_hold", "Safety preconditions no longer hold.")]
    elif fresh.evidence_fingerprint != plan.evidence_fingerprint:
        blockers.append(_blocker("business_state_changed", "Merchant state changed since the plan was made; create a new plan."))
    if blockers:
        return Evaluation(state, "BLOCKED", blockers, fresh)
    return Evaluation(state, "APPLY", [], fresh)


# --- preview --------------------------------------------------------------------------------------------------------------------

def describe_step(step: dict[str, Any]) -> str:
    p, kind = step["params"], step["kind"]
    return {StepKind.CANCEL_DUPLICATE_FULFILLMENT.value: f"Cancel the pending fulfillment {p.get('fulfillment_id')} of duplicate order {p.get('order_id')} (never a dispatched one).",
            StepKind.RELEASE_STOCK.value: f"Return {p.get('quantity')} unit(s) of stock held by order {p.get('order_id')} (one unique operation, never repeated).",
            StepKind.CANCEL_DUPLICATE_ORDER.value: f"Cancel duplicate order {p.get('order_id')} (from {p.get('from_state')}).",
            StepKind.REPLAY_PAID_EVENT.value: "Replay the paid event through the existing idempotent processor (no capture is initiated).",
            StepKind.ORDER_TO_REFUNDED.value: f"Align order {p.get('order_id')} with the provider's refund evidence (no refund is issued or reversed).",
            StepKind.CANCEL_PENDING_FULFILLMENT.value: f"Cancel the pending fulfillment {p.get('fulfillment_id')} of the refunded order."}[kind]


def preview_plan(db: Session, project_id: str, plan_id: str) -> dict[str, Any]:
    """Dry run: re-derives everything from fresh evidence and reports exactly what executing now would do. Writes nothing."""
    plan = get_plan(db, project_id, plan_id)
    events = _events(db, plan.id)
    approval_given = _approval(events) is not None
    result = _evaluate(db, plan, events, require_approval=False)
    mode = result.mode
    would_apply = mode == "APPLY"
    steps = [{"step_id": s["step_id"], "kind": s["kind"], "description": describe_step(s),
              "status": "ALREADY_DONE" if mode in ("EXECUTED", "ALREADY_APPLIED") else "WOULD_APPLY" if would_apply else "BLOCKED"} for s in plan.steps]
    blockers = list(result.blockers)
    if would_apply and not approval_given:
        blockers.append(_blocker("approval_required", "The plan has not been approved."))
    return {"plan_id": plan.id, "version": plan.version, "state": result.state.value, "plan_current": would_apply or mode in ("EXECUTED", "ALREADY_APPLIED"),
            "would_execute": would_apply and approval_given, "blockers": blockers, "steps": steps,
            "provider_fingerprint": plan.provider_fingerprint, "evidence_fingerprint": plan.evidence_fingerprint,
            "fresh_evidence_fingerprint": result.fresh.evidence_fingerprint if result.fresh else None,
            "guarantees": {"provider_calls": 0, "financial_mutations": 0, "dispatched_shipments_reversed": 0}}


# --- approval -------------------------------------------------------------------------------------------------------------------

def approve_plan(db: Session, project_id: str, plan_id: str, seller_id: str, *, version: int, evidence_fingerprint: str) -> tuple[SimRecoveryPlan, bool]:
    plan = get_plan(db, project_id, plan_id)
    events = _events(db, plan.id)
    existing = _approval(events)
    if existing is not None:
        if existing.approved_version == version and existing.approved_fingerprint == evidence_fingerprint:
            return plan, False  # the same approval again: idempotent
        raise _refuse(db, plan, seller_id, Ev.APPROVAL_REFUSED, "approval_mismatch", "This plan is already approved for a different version or evidence.",
                      version=version, fingerprint=evidence_fingerprint)
    if version != plan.version or evidence_fingerprint != plan.evidence_fingerprint:
        raise _refuse(db, plan, seller_id, Ev.APPROVAL_REFUSED, "approval_mismatch", "The approval does not match this plan's version and evidence fingerprint.",
                      version=version, fingerprint=evidence_fingerprint)
    if plan.status != PlanStatus.PROPOSED.value:
        raise _refuse(db, plan, seller_id, Ev.APPROVAL_REFUSED, "plan_not_approvable", f"A {plan.status} plan cannot be approved.", version=version,
                      fingerprint=evidence_fingerprint)
    evaluation = _evaluate(db, plan, events, require_approval=False)
    if evaluation.blockers:
        first = evaluation.blockers[0]
        raise _refuse(db, plan, seller_id, Ev.APPROVAL_REFUSED, first["code"], first["message"], version=version, fingerprint=evidence_fingerprint,
                      detail={"blockers": evaluation.blockers})
    try:
        _append(db, plan, Ev.APPROVED, seller_id, {"approver_seller_id": seller_id, "approved_at": _now(), "plan_version": plan.version},
                version=plan.version, fingerprint=plan.evidence_fingerprint)
        db.commit()
    except IntegrityError:  # a concurrent approval of the same plan won
        db.rollback()
        winner = _approval(_events(db, plan.id))
        if winner is not None and winner.approved_fingerprint == evidence_fingerprint and winner.approved_version == version:
            return plan, False
        raise
    return plan, True


# --- execution ------------------------------------------------------------------------------------------------------------------

def _take_write_lock(db: Session) -> None:
    """SQLite: start the write transaction BEFORE re-reading evidence, so a concurrent writer cannot slip in between the precondition
    check and the effect. Other databases serialize through the version columns and unique keys."""
    if db.get_bind().dialect.name != "sqlite":
        return
    raw = db.connection().connection.driver_connection
    if not raw.in_transaction:
        db.execute(text("BEGIN IMMEDIATE"))


def _reason(plan: SimRecoveryPlan) -> str:
    return f"recovery plan {plan.id} v{plan.version}"


def _owned(db: Session, model, row_id: str, plan: SimRecoveryPlan):
    row = db.get(model, row_id)
    if row is None or row.project_id != plan.project_id:
        raise RecoveryRefused("wrong_project_reference", "A step references a record outside this project.")
    return row


def _order_for(db: Session, plan: SimRecoveryPlan, order_id: str) -> SimOrder:
    order = _owned(db, SimOrder, order_id, plan)
    if order.payment_id != plan.payment_id:
        raise RecoveryRefused("wrong_payment_reference", "A step references an order of a different payment.")
    return order


def _shipped(db: Session, order_id: str) -> bool:
    return db.scalar(select(func.count()).select_from(SimFulfillment).where(
        SimFulfillment.order_id == order_id, SimFulfillment.state == FulfillmentState.DISPATCHED.value)) > 0


def _apply_step(db: Session, plan: SimRecoveryPlan, step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Apply one planned step. Returns (APPLIED | ALREADY_DONE, effect). Refuses (raises) on dispatch evidence or a foreign reference."""
    p, kind, note = step["params"], StepKind(step["kind"]), _reason(plan)
    if kind in (StepKind.CANCEL_DUPLICATE_FULFILLMENT, StepKind.CANCEL_PENDING_FULFILLMENT):
        order = _order_for(db, plan, p["order_id"])
        fulfillment = _owned(db, SimFulfillment, p["fulfillment_id"], plan)
        if fulfillment.order_id != order.id:
            raise RecoveryRefused("wrong_order_reference", "The fulfillment does not belong to the planned order.")
        if fulfillment.state == FulfillmentState.DISPATCHED.value:
            raise RecoveryRefused("dispatch_evidence", "A shipment has already left; it is never reversed.", detail={"fulfillment_id": fulfillment.id})
        if fulfillment.state == FulfillmentState.CANCELLED.value:
            return "ALREADY_DONE", {"fulfillment_id": fulfillment.id}
        store.cancel_fulfillment(db, fulfillment, reason=note)
        return "APPLIED", {"fulfillment_id": fulfillment.id, "state": fulfillment.state}
    if kind is StepKind.RELEASE_STOCK:
        order = _order_for(db, plan, p["order_id"])
        if _shipped(db, order.id):
            raise RecoveryRefused("dispatch_evidence", "Stock of shipped goods is never returned.", detail={"order_id": order.id})
        item = _owned(db, SimInventoryItem, p["item_id"], plan)
        movement, created = store.record_movement(db, plan.project_id, item, MovementKind.RELEASE, int(p["quantity"]), p["operation_key"],
                                                  order_id=order.id, note=note)
        return ("APPLIED" if created else "ALREADY_DONE"), {"movement_id": movement.id, "quantity": movement.quantity}
    order = _order_for(db, plan, p["order_id"])
    target = OrderState.CANCELLED if kind is StepKind.CANCEL_DUPLICATE_ORDER else OrderState.REFUNDED
    if order.state == target.value:
        return "ALREADY_DONE", {"order_id": order.id, "state": order.state}
    if kind is StepKind.CANCEL_DUPLICATE_ORDER and _shipped(db, order.id):
        raise RecoveryRefused("dispatch_evidence", "An order with a shipped parcel is never cancelled.", detail={"order_id": order.id})
    store.transition_order(db, order, target, reason=note)
    return "APPLIED", {"order_id": order.id, "state": order.state}


def _execution_result(db: Session, plan: SimRecoveryPlan, executed_now: bool) -> dict[str, Any]:
    events = _events(db, plan.id)
    done = next((e for e in events if e.event_type == Ev.EXECUTED.value), None)
    return {"plan": plan, "executed_now": executed_now, "execution": None if done is None else {"executed_at": done.created_at, **done.detail}}


def execute_plan(db: Session, project_id: str, plan_id: str, seller_id: str, *, fail_at: FailPoint | None = None) -> dict[str, Any]:
    """Execute an APPROVED, current plan at most once. Returns {"plan", "executed_now", "execution"}. A second call (or a concurrent one)
    returns the recorded result and changes nothing. `fail_at` is a test/demo crash-injection hook; it is not reachable from the API."""
    hook = fail_at or (lambda _step: None)
    plan = get_plan(db, project_id, plan_id)
    _take_write_lock_safely(db)
    events = _events(db, plan.id)
    evaluation = _evaluate(db, plan, events, require_approval=True)
    if evaluation.mode == "EXECUTED":
        db.rollback()
        return _execution_result(db, plan, False)
    if evaluation.mode == "BLOCKED":
        first = evaluation.blockers[0]
        raise _refuse(db, plan, seller_id, Ev.EXECUTION_REFUSED, first["code"], first["message"], detail={"blockers": evaluation.blockers})
    try:
        if plan.kind == RecoveryKind.PAID_NOT_APPLIED.value:
            summary = _run_paid_replay(db, plan, seller_id, evaluation, hook)
        else:
            summary = _run_atomic(db, plan, seller_id, hook)
    except RecoveryRefused as exc:
        raise _refuse(db, plan, seller_id, Ev.EXECUTION_REFUSED, exc.code, exc.message, detail=exc.detail) from exc
    except (IntegrityError, StaleDataError, OperationalError) as exc:
        db.rollback()
        db.expire_all()
        if any(e.event_type == Ev.EXECUTED.value for e in _events(db, plan.id)):
            return _execution_result(db, plan, False)  # a concurrent executor completed it: nothing further to do
        raise _refuse(db, plan, seller_id, Ev.EXECUTION_REFUSED, "concurrent_modification",
                      "The business state changed or another execution is in progress; nothing was applied. Preview again.") from exc
    hook("after_commit")
    result = _execution_result(db, plan, True)
    result["execution"] = {**(result["execution"] or {}), **summary}
    return result


def _take_write_lock_safely(db: Session) -> None:
    try:
        _take_write_lock(db)
    except OperationalError as exc:
        db.rollback()
        raise RecoveryRefused("concurrent_execution", "Another recovery operation holds the database; retry shortly.", 409) from exc


def _run_atomic(db: Session, plan: SimRecoveryPlan, seller_id: str, hook: FailPoint) -> dict[str, Any]:
    hook("before_apply")
    applied = already = 0
    for step in plan.steps:
        outcome, effect = _apply_step(db, plan, step)
        _append(db, plan, Ev.STEP_APPLIED if outcome == "APPLIED" else Ev.STEP_ALREADY_DONE, seller_id,
                {"step_id": step["step_id"], "kind": step["kind"], "effect": effect})
        applied += outcome == "APPLIED"
        already += outcome != "APPLIED"
        hook(f"after_step:{step['step_id']}")
    hook("before_executed")
    summary = {"steps_applied": applied, "steps_already_done": already, "provider_calls": 0, "financial_mutations": 0, "dispatched_shipments_reversed": 0}
    _append(db, plan, Ev.EXECUTED, seller_id, summary)
    db.commit()
    return summary


def _run_paid_replay(db: Session, plan: SimRecoveryPlan, seller_id: str, evaluation: Evaluation, hook: FailPoint) -> dict[str, Any]:
    step = plan.steps[0]
    outcome_name = "ALREADY_DONE"
    if evaluation.mode == "APPLY":
        event_id = step["params"]["event_id"] or _synthesize_event(db, plan, step["params"]["synthesize"])
        hook("before_process")
        result = process_event(db, plan.project_id, event_id)  # the existing safe processor; commits its own work
        hook("after_process")
        if result.outcome not in (ProcessingOutcome.APPLIED, ProcessingOutcome.DUPLICATE_NO_EFFECT):
            raise RecoveryRefused("processor_declined", "The safe processor declined to apply the event.",
                                  detail={"outcome": result.outcome.value, **result.detail})
        outcome_name = "APPLIED" if result.outcome is ProcessingOutcome.APPLIED else "ALREADY_DONE"
    _take_write_lock_safely(db)
    hook("before_executed")
    _append(db, plan, Ev.STEP_APPLIED if outcome_name == "APPLIED" else Ev.STEP_ALREADY_DONE, seller_id,
            {"step_id": step["step_id"], "kind": step["kind"], "effect": {"processed_by": "SAFE"}})
    summary = {"steps_applied": int(outcome_name == "APPLIED"), "steps_already_done": int(outcome_name != "APPLIED"), "provider_calls": 0,
               "financial_mutations": 0, "dispatched_shipments_reversed": 0}
    _append(db, plan, Ev.EXECUTED, seller_id, summary)
    db.commit()
    return summary


def _synthesize_event(db: Session, plan: SimRecoveryPlan, spec: dict[str, Any]) -> str:
    """A merchant reconciliation job event built ONLY from the plan's provider evidence; one per payment (deterministic key)."""
    key = spec["event_key"]
    existing = db.scalar(select(SimIncomingEvent).where(SimIncomingEvent.project_id == plan.project_id, SimIncomingEvent.source == EventSource.SIMULATED_MERCHANT_JOB.value,
                                                        SimIncomingEvent.event_key == key))
    if existing is not None:
        return existing.id
    payment = db.get(PaymentRecord, plan.payment_id)
    facts = payment_facts(db, payment)
    event = store.receive_event(db, plan.project_id, source=EventSource.SIMULATED_MERCHANT_JOB, event_key=key, event_type=EventType.PAYMENT_COMPLETED,
                                provider_txn_ref=facts["paypal_transaction_id"], amount_minor=facts["amount_minor"], currency=facts["currency"],
                                asserted_at=None, payload={"sku": spec["sku"], "quantity": spec["quantity"], "recovery_plan_key": plan.plan_key})
    db.commit()
    return event.id


# --- inspection -----------------------------------------------------------------------------------------------------------------

def plan_view(db: Session, plan: SimRecoveryPlan) -> dict[str, Any]:
    events = _events(db, plan.id)
    state = derive_state(plan, events)
    approval, done = _approval(events), next((e for e in events if e.event_type == Ev.EXECUTED.value), None)
    latest = _latest_version(db, plan.project_id, plan.plan_key)
    return {"id": plan.id, "project_id": plan.project_id, "plan_key": plan.plan_key, "version": plan.version, "latest_version": latest,
            "superseded": plan.version < latest, "kind": plan.kind, "payment_id": plan.payment_id, "status": plan.status, "state": state.value,
            "steps": [{**s, "description": describe_step(s)} for s in plan.steps], "evidence": plan.evidence, "reasons": plan.reasons,
            "provider_fingerprint": plan.provider_fingerprint, "evidence_fingerprint": plan.evidence_fingerprint,
            "created_by_seller_id": plan.created_by_seller_id, "created_at": plan.created_at,
            "approval": None if approval is None else {"approved_by_seller_id": approval.actor_seller_id, "approved_at": approval.created_at,
                                                       "plan_version": approval.approved_version, "evidence_fingerprint": approval.approved_fingerprint},
            "execution": None if done is None else {"executed_by_seller_id": done.actor_seller_id, "executed_at": done.created_at, **done.detail},
            "executable": state is PlanState.APPROVED and plan.version == latest}


def list_plans(db: Session, project_id: str, *, limit: int = 25, offset: int = 0) -> tuple[list[SimRecoveryPlan], int]:
    limit = max(1, min(limit, MAX_LIST))
    total = db.scalar(select(func.count()).select_from(SimRecoveryPlan).where(SimRecoveryPlan.project_id == project_id)) or 0
    rows = db.scalars(select(SimRecoveryPlan).where(SimRecoveryPlan.project_id == project_id)
                      .order_by(SimRecoveryPlan.created_at.desc(), SimRecoveryPlan.id).limit(limit).offset(max(0, offset)))
    return list(rows), total


def list_events(db: Session, project_id: str, plan_id: str) -> list[SimRecoveryEvent]:
    return _events(db, get_plan(db, project_id, plan_id).id)
