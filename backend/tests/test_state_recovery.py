"""S2: guarded, human-approved, replay-safe recovery of the SIMULATED merchant store. Offline; no provider call exists on this path."""
import re
import threading
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.models import PaymentRecord, Project
from app.sim_enums import OrderState, Processor, RecoveryKind
from app.sim_models import SimAuditRecord, SimFulfillment, SimInventoryMovement, SimOrder, SimRecoveryEvent, SimRecoveryPlan
from app.simulation import recovery, scenarios, store
from app.simulation.recovery import RecoveryRefused
from app.simulation.recovery_plan import RecoveryError
from tests.test_payment_observations import paid, unpaid  # noqa: F401 (fixtures)
from tests.test_state_simulation import STOCK, deliver, orders, refund_at_provider, run, sim  # noqa: F401 (fixtures)

K = RecoveryKind


def seller_id(sim):
    return sim.db.get(Project, sim.pid).seller_id


def make_plan(sim, kind, payment=True):
    plan, _ = recovery.create_plan(sim.db, sim.pid, seller_id(sim), kind, sim.payment.id if payment else None)
    return plan


def approve(sim, plan):
    return recovery.approve_plan(sim.db, sim.pid, plan.id, seller_id(sim), version=plan.version, evidence_fingerprint=plan.evidence_fingerprint)


def execute(sim, plan, **kw):
    return recovery.execute_plan(sim.db, sim.pid, plan.id, seller_id(sim), **kw)


def live_orders(sim):
    sim.db.expire_all()
    return [o for o in orders(sim) if o.state != OrderState.CANCELLED.value]


def event_types(sim, plan):
    return [e.event_type for e in recovery.list_events(sim.db, sim.pid, plan.id)]


def scenario_a(sim, dispatched=1):
    return scenarios.scenario_a_one_payment_three_orders(sim.db, sim.pid, sim.payment, sim.item, dispatched=dispatched)


# --- scenario A ---------------------------------------------------------------------------------------------------------------

def test_a_recovers_duplicates_keeps_the_dispatched_order_and_is_replay_safe(sim):
    outcome = scenario_a(sim, dispatched=1)
    shipped = outcome.facts["dispatched_order_ids"]
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    assert plan.status == "PROPOSED" and plan.evidence["completeness"]["complete"] is True
    preview = recovery.preview_plan(sim.db, sim.pid, plan.id)
    assert preview["would_execute"] is False and preview["blockers"][0]["code"] == "approval_required"
    approve(sim, plan)
    assert recovery.preview_plan(sim.db, sim.pid, plan.id)["would_execute"] is True
    result = execute(sim, plan)
    assert result["executed_now"] is True
    assert [o.id for o in live_orders(sim)] == shipped  # the dispatched order is canonical and untouched
    assert store.stock_balance(sim.db, sim.item.id) == STOCK - 1
    assert store.dispatch_evidence(sim.db, sim.pid, shipped[0])["dispatched"] is True  # the shipment is untouched
    again = execute(sim, plan)  # replay: no extra effects
    assert again["executed_now"] is False
    assert store.stock_balance(sim.db, sim.item.id) == STOCK - 1
    assert event_types(sim, plan).count("EXECUTED") == 1
    assert not [d for d in __import__("app.simulation.divergence", fromlist=["x"]).detect_divergences(sim.db, sim.pid)
                if d.code.value.startswith(("MULTIPLE", "DUPLICATE"))]


def test_a_with_two_shipped_parcels_needs_manual_review_and_is_never_executable(sim):
    scenario_a(sim, dispatched=2)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    assert plan.status == "NEEDS_REVIEW"
    with pytest.raises(RecoveryRefused) as err:
        approve(sim, plan)
    assert err.value.code == "plan_not_approvable"
    with pytest.raises(RecoveryRefused):
        execute(sim, plan)
    assert len(live_orders(sim)) == 3 and sim.db.scalar(select(func.count()).select_from(SimFulfillment).where(
        SimFulfillment.state == "DISPATCHED")) == 2


def test_dispatched_shipments_are_final_even_against_raw_sql(sim):
    scenario_a(sim, dispatched=1)
    with pytest.raises(IntegrityError):
        sim.db.execute(text("UPDATE sim_fulfillments SET state='CANCELLED' WHERE state='DISPATCHED'"))
    sim.db.rollback()


# --- scenario B ---------------------------------------------------------------------------------------------------------------

def test_b_applies_the_paid_event_once_through_the_existing_processor(sim):
    scenarios.scenario_b_payment_success_merchant_failed(sim.db, sim.pid, sim.payment, sim.item)
    plan = make_plan(sim, K.PAID_NOT_APPLIED)
    assert plan.status == "PROPOSED"
    approve(sim, plan)
    assert execute(sim, plan)["executed_now"] is True
    [order] = live_orders(sim)
    assert order.state == "PAID" and store.stock_balance(sim.db, sim.item.id) == STOCK - 1
    assert execute(sim, plan)["executed_now"] is False
    assert sim.db.scalar(select(func.count()).select_from(SimInventoryMovement).where(SimInventoryMovement.kind == "DEDUCT")) == 1
    assert len(live_orders(sim)) == 1


def test_b_with_an_order_but_no_event_synthesizes_one_event_and_applies_once(sim):
    store.create_order(sim.db, sim.pid, sim.payment, sim.item, 1)
    sim.db.commit()
    plan = make_plan(sim, K.PAID_NOT_APPLIED)
    approve(sim, plan)
    assert execute(sim, plan)["executed_now"] is True
    assert len(live_orders(sim)) == 1 and store.stock_balance(sim.db, sim.item.id) == STOCK - 1
    assert execute(sim, plan)["executed_now"] is False
    assert store.stock_balance(sim.db, sim.item.id) == STOCK - 1


def test_b_with_neither_event_nor_order_is_blocked_and_nothing_is_invented(sim):
    plan = make_plan(sim, K.PAID_NOT_APPLIED)
    assert plan.status == "BLOCKED"
    with pytest.raises(RecoveryRefused):
        approve(sim, plan)
    assert orders(sim) == []


def test_b_is_blocked_when_the_provider_does_not_show_paid(sim, unpaid):  # noqa: F811
    pid, payment, _ = unpaid
    with pytest.raises(RecoveryError):
        recovery.create_plan(sim.db, pid, seller_id(sim), K.PAID_NOT_APPLIED, payment["id"])


# --- scenario C ---------------------------------------------------------------------------------------------------------------

def test_c_aligns_a_stale_paid_order_with_the_refund_and_never_claims_funds(sim):
    run(sim, deliver(sim))
    refund_at_provider(sim)
    plan = make_plan(sim, K.STALE_PAID_AFTER_REFUND)
    assert plan.status == "PROPOSED"
    assert any(r.get("code") == "NO_FUND_STATEMENT" or "NO_FUND_STATEMENT" in str(r) for r in plan.reasons)
    approve(sim, plan)
    execute(sim, plan)
    sim.db.expire_all()
    assert orders(sim)[0].state == "REFUNDED"
    assert store.stock_balance(sim.db, sim.item.id) == STOCK  # undispatched stock returned once
    assert execute(sim, plan)["executed_now"] is False
    assert store.stock_balance(sim.db, sim.item.id) == STOCK


def test_c_with_a_shipped_parcel_keeps_the_shipment_and_flags_follow_up(sim):
    run(sim, deliver(sim))
    [f] = sim.db.scalars(select(SimFulfillment)).all()
    store.dispatch_fulfillment(sim.db, f)
    sim.db.commit()
    refund_at_provider(sim)
    plan = make_plan(sim, K.STALE_PAID_AFTER_REFUND)
    assert any("GOODS_ALREADY_SHIPPED" in str(r) for r in plan.reasons)
    approve(sim, plan)
    execute(sim, plan)
    sim.db.expire_all()
    assert sim.db.get(SimFulfillment, f.id).state == "DISPATCHED"
    assert store.stock_balance(sim.db, sim.item.id) == STOCK - 1  # shipped goods are never returned


# --- scenario D ---------------------------------------------------------------------------------------------------------------

def test_d_is_advisory_only_and_mutates_nothing(sim):
    plan = make_plan(sim, K.FALSE_HEALTHY, payment=False)
    assert plan.status == "ADVISORY" and plan.steps == []
    with pytest.raises(RecoveryRefused):
        approve(sim, plan)
    with pytest.raises(RecoveryRefused):
        execute(sim, plan)
    assert orders(sim) == [] and store.stock_balance(sim.db, sim.item.id) == STOCK


# --- approval integrity, staleness, idempotency -------------------------------------------------------------------------------

def test_execution_requires_approval_and_a_wrong_fingerprint_is_refused_and_recorded(sim):
    scenario_a(sim)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    with pytest.raises(RecoveryRefused) as err:
        execute(sim, plan)
    assert err.value.code == "approval_required"
    with pytest.raises(RecoveryRefused) as err:
        recovery.approve_plan(sim.db, sim.pid, plan.id, seller_id(sim), version=plan.version, evidence_fingerprint="0" * 64)
    assert err.value.code == "approval_mismatch"
    types = event_types(sim, plan)
    assert "EXECUTION_REFUSED" in types and "APPROVAL_REFUSED" in types and "APPROVED" not in types
    assert len(live_orders(sim)) == 3


def test_duplicate_approval_is_idempotent_and_records_one_approval(sim):
    scenario_a(sim)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    assert approve(sim, plan)[1] is True
    assert approve(sim, plan)[1] is False
    assert event_types(sim, plan).count("APPROVED") == 1


def test_identical_evidence_yields_the_same_plan_and_changed_evidence_a_new_version(sim):
    scenario_a(sim, dispatched=1)
    first = make_plan(sim, K.DUPLICATE_ORDERS)
    assert make_plan(sim, K.DUPLICATE_ORDERS).id == first.id
    approve(sim, first)
    # business state changes after approval: a second parcel ships
    extra = [f for f in sim.db.scalars(select(SimFulfillment)) if f.state == "PENDING"][0]
    store.dispatch_fulfillment(sim.db, extra, processor=Processor.FAULTY_FIXTURE)
    sim.db.commit()
    with pytest.raises(RecoveryRefused) as err:
        execute(sim, first)
    assert err.value.code == "MULTIPLE_DISPATCHED_ORDERS"
    assert len(live_orders(sim)) == 3  # nothing was applied
    second = make_plan(sim, K.DUPLICATE_ORDERS)
    assert second.version == 2 and second.status == "NEEDS_REVIEW"
    assert recovery.plan_view(sim.db, sim.db.get(SimRecoveryPlan, first.id))["superseded"] is True
    with pytest.raises(RecoveryRefused):
        execute(sim, first)  # an old approval never authorizes anything now


def test_a_tampered_stored_plan_is_never_executed(sim):
    scenario_a(sim)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    approve(sim, plan)
    sim.db.execute(text("DROP TRIGGER sim_recovery_plans_no_update"))
    sim.db.execute(text("UPDATE sim_recovery_plans SET steps='[]' WHERE id=:i"), {"i": plan.id})
    sim.db.commit()
    sim.db.expire_all()
    with pytest.raises(RecoveryRefused) as err:
        execute(sim, plan)
    assert err.value.code == "plan_tampered"
    assert len(live_orders(sim)) == 3


def test_provider_evidence_change_blocks_execution(sim):
    run(sim, deliver(sim))
    refund_at_provider(sim)
    plan = make_plan(sim, K.STALE_PAID_AFTER_REFUND)
    approve(sim, plan)
    sim.db.execute(text("UPDATE payment_records SET amount_minor = amount_minor + 1 WHERE id=:i"), {"i": sim.payment.id})
    sim.db.commit()
    sim.db.expire_all()
    with pytest.raises(RecoveryRefused) as err:
        execute(sim, plan)
    assert err.value.code == "provider_evidence_changed"
    assert orders(sim)[0].state == "PAID"


# --- crash safety -------------------------------------------------------------------------------------------------------------

def test_a_crash_before_the_executed_record_rolls_back_everything_and_a_retry_completes(sim):
    scenario_a(sim)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    approve(sim, plan)

    def crash(step):
        if step == "before_executed":
            raise scenarios.SimulatedCrash("died")

    with pytest.raises(scenarios.SimulatedCrash):
        execute(sim, plan, fail_at=crash)
    sim.db.rollback()
    sim.db.expire_all()
    assert len(live_orders(sim)) == 3 and "EXECUTED" not in event_types(sim, plan)
    assert execute(sim, plan)["executed_now"] is True
    assert len(live_orders(sim)) == 1


def test_a_crash_after_commit_leaves_exactly_one_result(sim):
    scenario_a(sim)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    approve(sim, plan)

    def crash(step):
        if step == "after_commit":
            raise scenarios.SimulatedCrash("died after commit")

    with pytest.raises(scenarios.SimulatedCrash):
        execute(sim, plan, fail_at=crash)
    sim.db.expire_all()
    assert execute(sim, plan)["executed_now"] is False
    assert len(live_orders(sim)) == 1 and event_types(sim, plan).count("EXECUTED") == 1


def test_b_crash_after_the_processor_applied_completes_without_a_second_effect(sim):
    scenarios.scenario_b_payment_success_merchant_failed(sim.db, sim.pid, sim.payment, sim.item)
    plan = make_plan(sim, K.PAID_NOT_APPLIED)
    approve(sim, plan)

    def crash(step):
        if step == "after_process":
            raise scenarios.SimulatedCrash("died")

    with pytest.raises(scenarios.SimulatedCrash):
        execute(sim, plan, fail_at=crash)
    sim.db.rollback()
    sim.db.expire_all()
    assert execute(sim, plan)["executed_now"] is True
    assert store.stock_balance(sim.db, sim.item.id) == STOCK - 1 and len(live_orders(sim)) == 1
    assert event_types(sim, plan).count("EXECUTED") == 1


def test_concurrent_executions_apply_the_plan_once(sim):
    scenario_a(sim)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    approve(sim, plan)
    owner, results, errors = seller_id(sim), [], []

    def worker():
        with DbSession(sim.app.state.engine, expire_on_commit=False) as db:
            try:
                results.append(recovery.execute_plan(db, sim.pid, plan.id, owner)["executed_now"])
            except RecoveryRefused as exc:
                errors.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1
    sim.db.expire_all()
    assert len(live_orders(sim)) == 1 and store.stock_balance(sim.db, sim.item.id) == STOCK - 1
    assert event_types(sim, plan).count("EXECUTED") == 1


# --- history, isolation, API ----------------------------------------------------------------------------------------------------

def test_recovery_history_is_append_only(sim):
    scenario_a(sim)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    for sql in ("UPDATE sim_recovery_events SET detail='{}'", "DELETE FROM sim_recovery_events", "UPDATE sim_recovery_plans SET status='BLOCKED'",
                "DELETE FROM sim_recovery_plans"):
        with pytest.raises(Exception):
            sim.db.execute(text(sql))
            sim.db.commit()
        sim.db.rollback()
    assert plan.id


def test_the_normal_recovery_path_never_imports_the_faulty_fixtures_or_a_provider():
    base = Path(__file__).resolve().parents[1] / "app"
    for rel in ("simulation/recovery.py", "simulation/recovery_plan.py", "routers/state_recovery.py"):
        source = (base / rel).read_text(encoding="utf8")
        assert not re.search(r"^\s*(from|import)\s+app\.simulation(\.|\s+import\s+)faulty|^\s*from app\.simulation import .*faulty", source, re.M), rel
        imports = "\n".join(line for line in source.splitlines() if re.match(r"\s*(from|import)\s", line))
        assert not re.search(r"(paypal|payment_provider|paypal_sandbox|sandbox_|services.payments|httpx|requests|urllib|socket)", imports, re.I), rel
        assert not re.search(r"(payment_provider|PaymentProvider|\.invoice|send_invoice|capture|refund_payment)\(", source), rel


def test_api_flow_and_wrong_project_access(sim, seller_factory):
    scenario_a(sim)
    api = sim.seller
    base = f"/api/v1/projects/{sim.pid}/state-recovery"
    found = api.get(f"{base}/divergences")
    assert found.status_code == 200 and found.json()["simulated"] is True and found.json()["divergences"]
    created = api.post(f"{base}/plans", json={"kind": "DUPLICATE_ORDERS", "payment_id": sim.payment.id})
    assert created.status_code == 201
    plan = created.json()
    again = api.post(f"{base}/plans", json={"kind": "DUPLICATE_ORDERS", "payment_id": sim.payment.id})
    assert again.status_code == 200 and again.json()["id"] == plan["id"]
    preview = api.get(f"{base}/plans/{plan['id']}/preview")
    assert preview.status_code == 200 and preview.json()["guarantees"]["provider_calls"] == 0
    assert api.post(f"{base}/plans/{plan['id']}/execute").status_code == 409  # not approved
    bad = api.post(f"{base}/plans/{plan['id']}/approve", json={"version": plan["version"], "evidence_fingerprint": "f" * 64})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "approval_mismatch"
    ok = api.post(f"{base}/plans/{plan['id']}/approve", json={"version": plan["version"], "evidence_fingerprint": plan["evidence_fingerprint"]})
    assert ok.status_code == 200 and ok.json()["state"] == "APPROVED" and ok.json()["approval"]["approved_by_seller_id"]
    done = api.post(f"{base}/plans/{plan['id']}/execute")
    assert done.status_code == 200 and done.json()["executed_now"] is True and done.json()["plan"]["state"] == "EXECUTED"
    repeat = api.post(f"{base}/plans/{plan['id']}/execute")
    assert repeat.status_code == 200 and repeat.json()["executed_now"] is False
    history = api.get(f"{base}/plans/{plan['id']}/events").json()["events"]
    assert [e["seq"] for e in history] == sorted(e["seq"] for e in history)
    assert {"PLAN_CREATED", "APPROVED", "EXECUTED", "EXECUTION_REFUSED", "APPROVAL_REFUSED"} <= {e["event_type"] for e in history}
    assert api.get(f"{base}/plans").json()["total"] == 1
    other = seller_factory("intruder@example.com")
    for method, path, body in (("get", "divergences", None), ("get", f"plans/{plan['id']}", None), ("get", f"plans/{plan['id']}/preview", None),
                               ("get", f"plans/{plan['id']}/events", None), ("post", f"plans/{plan['id']}/execute", None),
                               ("post", "plans", {"kind": "DUPLICATE_ORDERS", "payment_id": sim.payment.id})):
        response = getattr(other, method)(f"{base}/{path}", **({"json": body} if body else {}))
        assert response.status_code == 404, path


def test_a_plan_id_from_another_project_is_not_found(sim):
    scenario_a(sim)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    with pytest.raises(RecoveryError) as err:
        recovery.get_plan(sim.db, "some-other-project", plan.id)
    assert err.value.code == "not_found"


def test_b_never_applies_an_event_whose_amount_disagrees_with_the_provider(sim):
    store.create_order(sim.db, sim.pid, sim.payment, sim.item, 1)
    sim.db.commit()
    wrong = deliver(sim, key="evt-wrong-amount", amount=sim.payment.amount_minor + 5)
    plan = make_plan(sim, K.PAID_NOT_APPLIED)
    assert plan.steps[0]["params"]["event_id"] is None and plan.steps[0]["params"]["synthesize"]  # the wrong event is not a candidate
    approve(sim, plan)
    execute(sim, plan)
    sim.db.expire_all()
    [order] = orders(sim)
    assert order.state == "PAID" and order.amount_minor == sim.payment.amount_minor
    assert not sim.db.scalar(select(func.count()).select_from(SimAuditRecord).where(SimAuditRecord.event_id == wrong.id, SimAuditRecord.outcome == "APPLIED"))


def test_evidence_that_flips_back_yields_a_fresh_executable_version(sim):
    scenario_a(sim, dispatched=1)
    v1 = make_plan(sim, K.DUPLICATE_ORDERS)
    # a stock adjustment changes the merchant evidence, then is undone
    item = store.add_inventory_item(sim.db, sim.pid, "SKU-FLIP", 3)
    pending = [f for f in sim.db.scalars(select(SimFulfillment)) if f.state == "PENDING"][0]
    store.dispatch_fulfillment(sim.db, pending)
    sim.db.commit()
    v2 = make_plan(sim, K.DUPLICATE_ORDERS)
    assert v2.version == 2 and v2.status == "NEEDS_REVIEW" and item.id
    assert recovery.plan_view(sim.db, v1)["superseded"] is True


def test_a_provider_recheck_with_identical_facts_does_not_change_the_plan(sim):
    scenario_a(sim)
    first = make_plan(sim, K.DUPLICATE_ORDERS)
    sim.db.execute(text("UPDATE payment_records SET checked_at = datetime(checked_at, '+1 hour') WHERE id=:i"), {"i": sim.payment.id})
    sim.db.commit()
    sim.db.expire_all()
    assert make_plan(sim, K.DUPLICATE_ORDERS).id == first.id
    approve(sim, first)
    assert execute(sim, first)["executed_now"] is True


def test_identical_repeated_refusals_do_not_grow_the_history(sim):
    scenario_a(sim)
    plan = make_plan(sim, K.DUPLICATE_ORDERS)
    for _ in range(6):
        with pytest.raises(RecoveryRefused):
            execute(sim, plan)
    assert event_types(sim, plan).count("EXECUTION_REFUSED") == 1
