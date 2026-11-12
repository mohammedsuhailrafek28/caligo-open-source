"""S1: simulated merchant state (orders, inventory, fulfillment, incoming events, audit) and the four failure scenarios.
Offline: SQLite + the existing MOCK provider flow only. No provider call is possible (the network guard is active)."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.db import MIGRATIONS_DIR, make_engine
from app.models import AppendOnlyError, PaymentRecord, ProviderEvidenceGap
from app.sim_enums import (
    AuditAction, EventSource, EventType, FulfillmentState, MovementKind, OrderState, ProcessingOutcome as Out, ReplayStatus,
)
from app.sim_models import SimAuditRecord, SimFulfillment, SimIncomingEvent, SimInventoryMovement, SimOrder
from app.simulation import faulty, scenarios, store
from app.simulation.divergence import detect_divergences
from app.simulation.health import assess_integration_health
from app.simulation.processor import preview_event, process_event
from tests.test_payment_observations import paid, unpaid  # noqa: F401 (fixtures)
from tests.test_payments import make_project, sync

NOW = datetime.now(timezone.utc)
STOCK = 10


@pytest.fixture()
def sim(app, seller, paid):  # noqa: F811
    pid, payment, _ = paid
    with DbSession(app.state.engine, expire_on_commit=False) as db:
        record = db.get(PaymentRecord, payment["id"])
        item = store.add_inventory_item(db, pid, "SKU-1", STOCK)
        db.commit()
        yield SimpleNamespace(db=db, pid=pid, payment=record, item=item, app=app, seller=seller, invoice_id=payment["paypal_invoice_id"])


def deliver(sim, key="evt-1", kind=EventType.PAYMENT_COMPLETED, *, amount="payment", currency="payment", txn="payment", qty=1,
            asserted=NOW, payload=None, project=None):
    amount = sim.payment.amount_minor if amount == "payment" else amount
    currency = sim.payment.currency if currency == "payment" else currency
    txn = sim.payment.paypal_transaction_id if txn == "payment" else txn
    event = store.receive_event(sim.db, project or sim.pid, source=EventSource.SIMULATED_PROVIDER_WEBHOOK, event_key=key, event_type=kind,
                                provider_txn_ref=txn, amount_minor=amount, currency=currency, asserted_at=asserted,
                                payload=payload or {"sku": sim.item.sku, "quantity": qty})
    sim.db.commit()
    return event


def run(sim, event):
    return process_event(sim.db, sim.pid, event.id)


def orders(sim):
    return list(sim.db.scalars(select(SimOrder).where(SimOrder.project_id == sim.pid)))


def count(sim, model):
    return sim.db.scalar(select(func.count()).select_from(model).where(model.project_id == sim.pid))


def refund_at_provider(sim):
    sim.app.state.payment_provider.simulate_refund(sim.invoice_id)
    assert sync(sim.seller, sim.pid).status_code == 200
    sim.db.refresh(sim.payment)
    assert sim.payment.provider_status == "REFUNDED"


# --- normal, idempotent processing ---------------------------------------------------------------------------------------------

def test_a_paid_event_creates_one_order_one_deduction_and_one_pending_fulfillment(sim):
    result = run(sim, deliver(sim))
    assert result.outcome is Out.APPLIED
    [order] = orders(sim)
    assert (order.state, order.amount_minor, order.currency, order.payment_id) == ("PAID", 80000, "USD", sim.payment.id)
    assert store.stock_balance(sim.db, sim.item.id) == STOCK - 1
    [fulfillment] = sim.db.scalars(select(SimFulfillment)).all()
    assert fulfillment.state == "PENDING" and fulfillment.dispatched_at is None


def test_duplicate_event_delivery_has_no_second_effect_and_is_preserved(sim):
    first, second, third = (deliver(sim) for _ in range(3))
    assert [e.replay_status for e in (first, second, third)] == ["FIRST", "REPLAY_IDENTICAL", "REPLAY_IDENTICAL"]
    assert [run(sim, e).outcome for e in (first, second, third)] == [Out.APPLIED, Out.DUPLICATE_NO_EFFECT, Out.DUPLICATE_NO_EFFECT]
    assert len(orders(sim)) == 1 and store.stock_balance(sim.db, sim.item.id) == STOCK - 1
    assert count(sim, SimIncomingEvent) == 3  # every delivery is kept
    attempts = sim.db.scalars(select(SimAuditRecord).where(SimAuditRecord.action == "EVENT_PROCESSED")).all()
    assert sorted(a.outcome for a in attempts) == ["APPLIED", "DUPLICATE_NO_EFFECT", "DUPLICATE_NO_EFFECT"]


def test_reprocessing_the_same_delivery_is_a_duplicate_not_a_second_effect(sim):
    event = deliver(sim)
    assert run(sim, event).outcome is Out.APPLIED
    assert run(sim, event).outcome is Out.DUPLICATE_NO_EFFECT
    assert len(orders(sim)) == 1 and count(sim, SimInventoryMovement) == 1


def test_the_same_event_key_with_different_content_is_flagged_and_never_applied(sim):
    deliver(sim, key="evt-x")
    altered = deliver(sim, key="evt-x", amount=1)
    assert altered.replay_status == ReplayStatus.REPLAY_DIFFERENT_PAYLOAD.value
    assert run(sim, altered).outcome is Out.REJECTED_CONFLICTING_REPLAY
    assert orders(sim) == [] and count(sim, SimInventoryMovement) == 0


def test_concurrent_duplicate_operations_are_stopped_by_the_unique_operation_key(sim):
    run(sim, deliver(sim))
    [order] = orders(sim)
    again, created = store.record_movement(sim.db, sim.pid, sim.item, MovementKind.DEDUCT, 1, store.operation_key("deduct", order.id), order_id=order.id)
    assert created is False and count(sim, SimInventoryMovement) == 1
    with pytest.raises(IntegrityError):  # a raced insert that skipped the pre-check is stopped by the database itself
        sim.db.execute(text("INSERT INTO sim_inventory_movements (id, project_id, item_id, order_id, kind, quantity, operation_key, created_at) "
                            "SELECT 'dup', project_id, item_id, order_id, kind, quantity, operation_key, created_at FROM sim_inventory_movements"))
    sim.db.rollback()


# --- crash before persist; delayed and out-of-order events ---------------------------------------------------------------------

def test_a_crash_before_persist_leaves_no_partial_effect_and_the_retry_applies_exactly_once(sim):
    event = deliver(sim)

    def crash(step):
        if step == "after_inventory":
            raise scenarios.SimulatedCrash("died")

    with pytest.raises(scenarios.SimulatedCrash):
        process_event(sim.db, sim.pid, event.id, fail_at=crash)
    assert orders(sim) == [] and count(sim, SimInventoryMovement) == 0 and count(sim, SimFulfillment) == 0
    assert count(sim, SimIncomingEvent) == 1  # the receipt survived
    assert [e.id for e in store.unprocessed_events(sim.db, sim.pid)] == [event.id]
    assert run(sim, event).outcome is Out.APPLIED
    assert len(orders(sim)) == 1 and store.stock_balance(sim.db, sim.item.id) == STOCK - 1 and store.unprocessed_events(sim.db, sim.pid) == []


def test_a_refund_that_arrives_before_the_paid_event_is_held_and_changes_nothing(sim):
    store.create_order(sim.db, sim.pid, sim.payment, sim.item, 1)
    sim.db.commit()
    refund_at_provider(sim)
    result = run(sim, deliver(sim, "evt-refund", EventType.PAYMENT_REFUNDED, amount=sim.payment.amount_paid_minor))
    assert result.outcome is Out.HELD_AWAITING_PROVIDER_EVIDENCE
    assert orders(sim)[0].state == "PENDING" and count(sim, SimInventoryMovement) == 0


def test_a_late_paid_event_after_a_provider_refund_never_creates_an_order_or_deducts_stock(sim):
    refund_at_provider(sim)
    result = run(sim, deliver(sim, "evt-late-paid", asserted=NOW + timedelta(hours=1)))  # even asserted as NEWER than anything
    assert result.outcome is Out.REJECTED_STALE
    assert orders(sim) == [] and store.stock_balance(sim.db, sim.item.id) == STOCK


def test_event_arrival_order_and_asserted_time_are_not_treated_as_financial_truth(sim):
    run(sim, deliver(sim))
    refund_at_provider(sim)
    run(sim, deliver(sim, "evt-refund", EventType.PAYMENT_REFUNDED, amount=sim.payment.amount_paid_minor, asserted=NOW))
    for key, when in (("evt-old", NOW - timedelta(days=1)), ("evt-new", NOW + timedelta(days=1))):
        assert run(sim, deliver(sim, key, asserted=when)).outcome is Out.REJECTED_STALE  # neither older nor newer revives it
    assert orders(sim)[0].state == "REFUNDED"


def test_a_refund_after_payment_returns_stock_only_when_nothing_shipped(sim):
    run(sim, deliver(sim))
    refund_at_provider(sim)
    result = run(sim, deliver(sim, "evt-refund", EventType.PAYMENT_REFUNDED, amount=sim.payment.amount_paid_minor))
    assert result.outcome is Out.APPLIED and result.detail["goods_already_shipped"] is False
    assert orders(sim)[0].state == "REFUNDED" and store.stock_balance(sim.db, sim.item.id) == STOCK
    assert sim.db.scalars(select(SimFulfillment)).one().state == "CANCELLED"


def test_a_refund_after_dispatch_never_pretends_the_parcel_came_back(sim):
    run(sim, deliver(sim))
    fulfillment = sim.db.scalars(select(SimFulfillment)).one()
    store.dispatch_fulfillment(sim.db, fulfillment)
    sim.db.commit()
    refund_at_provider(sim)
    result = run(sim, deliver(sim, "evt-refund", EventType.PAYMENT_REFUNDED, amount=sim.payment.amount_paid_minor))
    assert result.outcome is Out.APPLIED and result.detail["goods_already_shipped"] is True
    sim.db.expire_all()
    assert sim.db.get(SimFulfillment, fulfillment.id).state == "DISPATCHED"
    assert store.stock_balance(sim.db, sim.item.id) == STOCK - 1  # no stock release for goods that left
    assert sim.db.scalar(select(func.count()).select_from(SimAuditRecord).where(SimAuditRecord.action == "DISPATCH_PROTECTED")) == 1
    assert [d.code.value for d in detect_divergences(sim.db, sim.pid)] == []  # merchant order REFUNDED agrees with the provider


# --- inventory, dispatch protection ---------------------------------------------------------------------------------------------

def test_the_stock_balance_is_derived_from_movements(sim):
    other = store.add_inventory_item(sim.db, sim.pid, "SKU-2", 5)
    store.record_movement(sim.db, sim.pid, other, MovementKind.RESERVE, 2, "k-reserve")
    store.record_movement(sim.db, sim.pid, other, MovementKind.DEDUCT, 1, "k-deduct")
    store.record_movement(sim.db, sim.pid, other, MovementKind.RELEASE, 1, "k-release")
    sim.db.commit()
    assert store.stock_balance(sim.db, other.id) == 5 - 2 - 1 + 1


def test_a_dispatched_fulfillment_cannot_be_cancelled_or_reverted_even_by_raw_sql(sim):
    run(sim, deliver(sim))
    fulfillment = sim.db.scalars(select(SimFulfillment)).one()
    store.dispatch_fulfillment(sim.db, fulfillment)
    sim.db.commit()
    assert store.cancel_fulfillment(sim.db, fulfillment, reason="customer asked") is False
    assert fulfillment.state == "DISPATCHED" and fulfillment.dispatched_at is not None
    sim.db.commit()
    for statement in ("UPDATE sim_fulfillments SET state = 'CANCELLED', dispatched_at = NULL", "UPDATE sim_fulfillments SET state = 'PENDING'",
                      "UPDATE sim_fulfillments SET dispatched_at = '2020-01-01 00:00:00'"):
        with pytest.raises(IntegrityError):
            sim.db.execute(text(statement))
        sim.db.rollback()
    evidence = store.dispatch_evidence(sim.db, sim.pid, fulfillment.order_id)
    assert evidence["dispatched"] is True and len(evidence["dispatch_audit_record_ids"]) == 1


def test_a_fulfillment_cannot_claim_dispatch_without_a_dispatch_time(sim):
    run(sim, deliver(sim))
    with pytest.raises(IntegrityError):
        sim.db.execute(text("UPDATE sim_fulfillments SET state = 'DISPATCHED'"))
    sim.db.rollback()


def test_order_identity_cannot_be_rewritten_and_the_transition_table_holds(sim):
    run(sim, deliver(sim))
    [order] = orders(sim)
    for statement in ("UPDATE sim_orders SET amount_minor = 1", "UPDATE sim_orders SET payment_id = 'x'", "UPDATE sim_orders SET quantity = 99"):
        with pytest.raises(IntegrityError):
            sim.db.execute(text(statement))
        sim.db.rollback()
    with pytest.raises(store.SimulationError):
        store.transition_order(sim.db, order, OrderState.PENDING)
    with pytest.raises(store.SimulationError):  # safe code cannot bypass the table
        store.force_order_state(sim.db, order, OrderState.PENDING, processor=store.Processor.SAFE, event_id=None, reason="x")


# --- cross-project isolation ----------------------------------------------------------------------------------------------------

@pytest.fixture()
def other(sim, seller):
    pid = make_project(seller)
    item = store.add_inventory_item(sim.db, pid, "SKU-1", 3)
    sim.db.commit()
    return SimpleNamespace(pid=pid, item=item)


def test_orders_cannot_reference_another_projects_payment_or_item(sim, other):
    with pytest.raises(store.SimulationError):
        store.create_order(sim.db, other.pid, sim.payment, other.item, 1)
    with pytest.raises(store.SimulationError):
        store.create_order(sim.db, sim.pid, sim.payment, other.item, 1)
    with pytest.raises(IntegrityError):  # and the database refuses it even if the service were bypassed
        sim.db.execute(text("INSERT INTO sim_orders (id, project_id, payment_id, business_key, item_id, quantity, amount_minor, currency, state, "
                            "version, created_at, updated_at) VALUES ('o', :p, :pay, 'k', :i, 1, 100, 'USD', 'PENDING', 1, '2026-01-01', '2026-01-01')"),
                        {"p": other.pid, "pay": sim.payment.id, "i": other.item.id})
    sim.db.rollback()


def test_movements_and_events_cannot_cross_projects_at_the_database_level(sim, other):
    run(sim, deliver(sim))
    [order] = orders(sim)
    with pytest.raises(IntegrityError):
        sim.db.execute(text("INSERT INTO sim_inventory_movements (id, project_id, item_id, order_id, kind, quantity, operation_key, created_at) "
                            "VALUES ('m', :p, :i, :o, 'DEDUCT', 1, 'x', '2026-01-01')"), {"p": other.pid, "i": other.item.id, "o": order.id})
    sim.db.rollback()
    with pytest.raises(IntegrityError):  # an event of project B pointing at project A's payment
        sim.db.execute(text("INSERT INTO sim_incoming_events (id, project_id, payment_id, source, event_key, event_type, payload, payload_sha256, "
                            "replay_status, received_at) VALUES ('e', :p, :pay, 'SIMULATED_PROVIDER_WEBHOOK', 'k', 'PAYMENT_COMPLETED', '{}', 'x', "
                            "'FIRST', '2026-01-01')"), {"p": other.pid, "pay": sim.payment.id})
    sim.db.rollback()


def test_an_event_carrying_another_projects_transaction_is_unknown_here(sim, other):
    event = store.receive_event(sim.db, other.pid, source=EventSource.SIMULATED_PROVIDER_WEBHOOK, event_key="x", event_type=EventType.PAYMENT_COMPLETED,
                                provider_txn_ref=sim.payment.paypal_transaction_id, amount_minor=sim.payment.amount_minor, currency="USD",
                                asserted_at=NOW, payload={"sku": "SKU-1", "quantity": 1})
    sim.db.commit()
    assert event.payment_id is None  # not resolved across projects
    assert process_event(sim.db, other.pid, event.id).outcome is Out.REJECTED_UNKNOWN_PAYMENT
    with pytest.raises(store.SimulationError):  # nor can another project process this project's event
        process_event(sim.db, other.pid, deliver(sim).id)


# --- amount/currency mismatch, missing or incomplete provider evidence --------------------------------------------------------------

@pytest.mark.parametrize("kwargs,expected", [({"amount": 79999}, Out.REJECTED_AMOUNT_MISMATCH), ({"amount": 80001}, Out.REJECTED_AMOUNT_MISMATCH),
                                             ({"currency": "EUR"}, Out.REJECTED_CURRENCY_MISMATCH)])
def test_amount_or_currency_mismatch_is_rejected_without_effect(sim, kwargs, expected):
    assert run(sim, deliver(sim, **kwargs)).outcome is expected
    assert orders(sim) == [] and count(sim, SimInventoryMovement) == 0


@pytest.mark.parametrize("kwargs", [{"amount": None}, {"currency": None}, {"txn": None}])
def test_an_event_with_missing_fields_is_incomplete_evidence_and_changes_nothing(sim, kwargs):
    assert run(sim, deliver(sim, **kwargs)).outcome is Out.REJECTED_INCOMPLETE_EVIDENCE
    assert orders(sim) == [] and count(sim, SimInventoryMovement) == 0


def test_an_unknown_provider_transaction_is_rejected(sim):
    assert run(sim, deliver(sim, txn="TXN-NOBODY")).outcome is Out.REJECTED_UNKNOWN_PAYMENT


def test_a_merchant_claim_of_payment_is_held_until_provider_evidence_shows_paid(app, seller, unpaid):  # noqa: F811
    pid, payment, _ = unpaid
    with DbSession(app.state.engine, expire_on_commit=False) as db:
        record = db.get(PaymentRecord, payment["id"])
        record.paypal_transaction_id = "TXN-CLAIMED"  # a claim only: provider status is still PENDING
        item = store.add_inventory_item(db, pid, "SKU-1", 5)
        db.commit()
        event = store.receive_event(db, pid, source=EventSource.SIMULATED_MERCHANT_JOB, event_key="claim", event_type=EventType.PAYMENT_COMPLETED,
                                    provider_txn_ref="TXN-CLAIMED", amount_minor=record.amount_minor, currency=record.currency, asserted_at=NOW,
                                    payload={"sku": "SKU-1", "quantity": 1})
        db.commit()
        result = process_event(db, pid, event.id)
        assert record.provider_status != "PAID" and result.outcome is Out.HELD_AWAITING_PROVIDER_EVIDENCE
        assert db.scalar(select(func.count()).select_from(SimOrder)) == 0 and store.stock_balance(db, item.id) == 5


def test_incomplete_provider_evidence_holds_the_event(sim):
    sim.db.add(ProviderEvidenceGap(project_id=sim.pid, payment_id=sim.payment.id, kind="PAYMENT", reason="OVERSIZED_AMOUNT", raw_sha256="0" * 64,
                                   dedupe_key="gap-1"))
    sim.db.commit()
    assert run(sim, deliver(sim)).outcome is Out.HELD_AWAITING_PROVIDER_EVIDENCE
    assert orders(sim) == []


def test_a_failed_payment_event_cannot_override_provider_evidence_of_payment(sim):
    store.create_order(sim.db, sim.pid, sim.payment, sim.item, 1)
    sim.db.commit()
    assert run(sim, deliver(sim, "evt-failed", EventType.PAYMENT_FAILED)).outcome is Out.REJECTED_CONTRADICTED_BY_PROVIDER
    assert orders(sim)[0].state == "PENDING"


def test_money_is_integer_minor_units_taken_from_the_payment_not_the_caller(sim):
    order, _ = store.create_order(sim.db, sim.pid, sim.payment, sim.item, 2)
    assert isinstance(order.amount_minor, int) and (order.amount_minor, order.currency) == (sim.payment.amount_minor, sim.payment.currency)
    with pytest.raises(IntegrityError):
        sim.db.execute(text("UPDATE sim_orders SET currency = 'usd'"))
    sim.db.rollback()


# --- append-only evidence and explicit simulation markers ----------------------------------------------------------------------

@pytest.mark.parametrize("table", ["sim_incoming_events", "sim_audit_records", "sim_inventory_movements", "sim_inventory_items"])
def test_history_tables_are_append_only_at_the_database_level(sim, table):
    run(sim, deliver(sim))
    with pytest.raises(IntegrityError):
        sim.db.execute(text(f"UPDATE {table} SET project_id = project_id"))
    sim.db.rollback()
    with pytest.raises(IntegrityError):
        sim.db.execute(text(f"DELETE FROM {table}"))
    sim.db.rollback()


def test_history_rows_cannot_be_modified_through_the_orm(sim):
    event = deliver(sim)
    event.event_key = "changed"
    with pytest.raises(AppendOnlyError):
        sim.db.flush()
    sim.db.rollback()


def test_only_simulated_sources_exist(sim):
    assert all(source.value.startswith("SIMULATED_") for source in EventSource)
    with pytest.raises(IntegrityError):
        sim.db.execute(text("INSERT INTO sim_incoming_events (id, project_id, source, event_key, event_type, payload, payload_sha256, replay_status, "
                            "received_at) VALUES ('e', :p, 'PAYPAL_LIVE', 'k', 'PAYMENT_COMPLETED', '{}', 'x', 'FIRST', '2026-01-01')"), {"p": sim.pid})
    sim.db.rollback()


def test_normal_application_code_never_reaches_the_faulty_fixtures(app):
    root = Path(__file__).resolve().parents[1] / "app"
    offenders = [str(p) for p in root.rglob("*.py") if "simulation" not in p.parts and p.name not in ("sim_models.py", "sim_enums.py")
                 and re.search(r"simulation(\.|\s+import\s+)(faulty|scenarios)|from app.simulation import[^\n]*(faulty|scenarios)", p.read_text(encoding="utf8"))]
    assert offenders == []
    assert not any("sim" in getattr(route, "path", "") for route in app.routes)  # no HTTP surface exists for the simulation


# --- the four failure scenarios -------------------------------------------------------------------------------------------------

def test_scenario_a_one_payment_three_orders(sim):
    outcome = scenarios.scenario_a_one_payment_three_orders(sim.db, sim.pid, sim.payment, sim.item)
    facts = outcome.facts
    assert facts["replay_statuses"] == ["FIRST", "REPLAY_IDENTICAL", "REPLAY_IDENTICAL"]
    assert len(facts["order_ids"]) == 3 and len(set(facts["order_ids"])) == 3
    assert facts["stock_balance"] == STOCK - 3 != facts["expected_stock_balance"]  # three deductions for one purchase
    assert len(facts["dispatched_order_ids"]) == 2  # two parcels already left: evidence, not undoable
    assert {"MULTIPLE_ORDERS_FOR_ONE_PAYMENT", "DUPLICATE_INVENTORY_DEDUCTION", "DUPLICATE_FULFILLMENT"} <= set(outcome.divergence_codes)
    assert sum(e["dispatched"] for e in facts["dispatch_evidence"]) == 2
    assert count(sim, SimIncomingEvent) == 3
    processors = {r.processor for r in sim.db.scalars(select(SimAuditRecord))}
    assert processors == {"FAULTY_FIXTURE"}  # simulated corruption is marked, never passed off as normal behavior
    shipped_ids = set(facts["dispatched_order_ids"])
    for fulfillment in sim.db.scalars(select(SimFulfillment).where(SimFulfillment.order_id.in_(shipped_ids))):
        assert store.cancel_fulfillment(sim.db, fulfillment, reason="dup") is False and fulfillment.state == "DISPATCHED"


def test_scenario_a_with_the_safe_processor_shows_what_correct_behavior_would_leave(sim):
    events = [deliver(sim, key="evt-one-payment") for _ in range(3)]
    assert [run(sim, e).outcome for e in events] == [Out.APPLIED, Out.DUPLICATE_NO_EFFECT, Out.DUPLICATE_NO_EFFECT]
    assert len(orders(sim)) == 1 and detect_divergences(sim.db, sim.pid) == []


def test_scenario_b_payment_success_merchant_failed(sim):
    outcome = scenarios.scenario_b_payment_success_merchant_failed(sim.db, sim.pid, sim.payment, sim.item)
    facts = outcome.facts
    assert facts["crashed"] is True and facts["provider_status"] == "PAID" and facts["order_state"] == "PENDING"
    assert len(facts["unprocessed_event_ids"]) == 1 and facts["stock_balance"] == STOCK
    assert outcome.divergence_codes == ["PROVIDER_PAID_MERCHANT_NOT_PAID"]
    assert outcome.divergences[0].detail["order_states"] == ["PENDING"]


def test_a_failed_merchant_order_with_a_paid_provider_payment_is_a_divergence(sim):
    order, _ = store.create_order(sim.db, sim.pid, sim.payment, sim.item, 1)
    store.transition_order(sim.db, order, OrderState.FAILED, reason="timeout")
    sim.db.commit()
    assert [d.code.value for d in detect_divergences(sim.db, sim.pid)] == ["PROVIDER_PAID_MERCHANT_NOT_PAID"]
    assert run(sim, deliver(sim)).outcome is Out.APPLIED  # the provider evidence legitimately moves FAILED -> PAID
    assert detect_divergences(sim.db, sim.pid) == []


def test_scenario_c_refund_followed_by_a_stale_paid_event(sim):
    assert run(sim, deliver(sim, "evt-first-paid")).outcome is Out.APPLIED
    refund_at_provider(sim)
    outcome = scenarios.scenario_c_stale_paid_after_refund(sim.db, sim.pid, sim.payment, sim.item)
    facts = outcome.facts
    assert facts["refund_outcome"] == "APPLIED" and facts["order_state_before_faulty"] == "REFUNDED"
    assert facts["safe_verdict_on_stale_event"] == "REJECTED_STALE"  # the evidence a safe processor rejects it
    assert facts["order_state_after_faulty"] == "PAID" and facts["provider_status"] == "REFUNDED"  # the deliberate regression
    assert facts["stock_balance"] == STOCK - 1  # stock deducted again for a refunded payment
    assert "PROVIDER_REFUNDED_MERCHANT_STILL_PAID" in outcome.divergence_codes
    assert [c["illegal_regression"] for c in facts["faulty_state_changes"]] == [True]


def test_the_preview_decides_without_persisting_anything(sim):
    event = deliver(sim)
    before = count(sim, SimAuditRecord)
    assert preview_event(sim.db, sim.pid, event.id).outcome is Out.APPLIED
    sim.db.rollback()
    assert orders(sim) == [] and count(sim, SimAuditRecord) == before and count(sim, SimInventoryMovement) == 0


def test_scenario_c_refuses_to_run_without_a_safely_applied_paid_order(sim):
    refund_at_provider(sim)
    with pytest.raises(store.SimulationError):
        scenarios.scenario_c_stale_paid_after_refund(sim.db, sim.pid, sim.payment, sim.item)


def test_scenario_d_configuration_and_heartbeat_do_not_prove_a_healthy_integration(sim):
    # a project whose provider shows a payment but whose merchant never saw it:
    outcome = scenarios.scenario_d_false_healthy(sim.db, sim.pid)
    naive, honest = outcome.facts["naive_report"], outcome.facts["honest"]
    assert naive["status"] == "HEALTHY" and naive["observed_end_to_end_payment"] is False
    assert (honest["configuration"], honest["heartbeat"]) == ("CONFIGURED", "RESPONDING")
    assert honest["end_to_end"] == "FAILING" and honest["overall"] == "DEGRADED"
    assert outcome.divergence_codes == ["PROVIDER_PAID_MERCHANT_NOT_PAID"]


def test_health_is_unverified_with_no_payment_and_healthy_only_after_a_proven_end_to_end_payment(app, seller):
    pid = make_project(seller)  # no payments at all
    with DbSession(app.state.engine, expire_on_commit=False) as db:
        empty = assess_integration_health(db, pid, credentials_configured=True, heartbeat_ok=True)
        assert (empty.end_to_end, empty.overall) == ("NOT_VERIFIED", "UNVERIFIED")
        assert faulty.naive_health_report(credentials_configured=True, heartbeat_ok=True)["status"] == "HEALTHY"


def test_health_becomes_healthy_only_when_a_safely_processed_payment_exists(sim):
    assert assess_integration_health(sim.db, sim.pid, credentials_configured=True, heartbeat_ok=True).overall == "DEGRADED"
    run(sim, deliver(sim))
    proven = assess_integration_health(sim.db, sim.pid, credentials_configured=True, heartbeat_ok=True)
    assert (proven.end_to_end, proven.overall) == ("VERIFIED", "HEALTHY")
    assert assess_integration_health(sim.db, sim.pid, credentials_configured=False, heartbeat_ok=True).overall != "HEALTHY"
    assert assess_integration_health(sim.db, sim.pid, credentials_configured=True, heartbeat_ok=False).overall != "HEALTHY"


# --- migration -------------------------------------------------------------------------------------------------------------------

def test_the_migrated_simulation_triggers_match_a_fresh_database_exactly(tmp_path):
    from alembic.config import Config

    def config(url):
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.set_main_option("sqlalchemy.url", url)
        return cfg

    migrated_url, fresh_url = f"sqlite:///{tmp_path / 'm.sqlite3'}", f"sqlite:///{tmp_path / 'f.sqlite3'}"
    command.upgrade(config(migrated_url), "head")
    from app.db import init_db

    fresh = make_engine(fresh_url)
    init_db(fresh)

    def definitions(engine):
        with engine.connect() as conn:
            return {r[0]: " ".join(r[1].split()) for r in conn.execute(text("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'sim_%'"))}

    migrated = definitions(make_engine(migrated_url))
    assert migrated == definitions(fresh) and len(migrated) >= 20


def test_downgrade_refuses_while_simulated_records_exist(tmp_path):
    from alembic.config import Config

    url = f"sqlite:///{tmp_path / 'd.sqlite3'}"
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sellers (id,email,password_hash,display_name,created_at) VALUES ('s','a@b.co','x','n','2026-01-01')"))
        conn.execute(text("INSERT INTO projects (id,seller_id,title,status,created_at) VALUES ('p','s','t','DRAFT','2026-01-01')"))
        conn.execute(text("INSERT INTO sim_inventory_items (id,project_id,sku,initial_stock,created_at) VALUES ('i','p','S',1,'2026-01-01')"))
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(cfg, "0005")
