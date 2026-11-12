"""R3 acceptance hardening: integrity failures, provider evidence gaps, resource limits, duplicate claims, trust boundaries."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.models import AppendOnlyError, AuditEvent, PaymentEvent, PaymentObservation, PaymentRecord, ProviderEvidenceGap
from app.payment_provider import ProviderInvoice, ProviderInvoiceState, ProviderTransaction
from app.security import LoginThrottle
from app.services import observation_compare, observations as obs_service, payments as payments_service
from tests.test_payment_observations import (
    claim, codes, comparison, csv_of, finding, ledger, line, observations, paid, snapshot, unpaid,  # noqa: F401 (fixtures)
)
from tests.test_payments import sync

BIG = 3_000_000_000  # above MAX_AMOUNT_MINOR


def counts(app):
    with DbSession(app.state.engine) as db:
        return (db.scalar(select(func.count()).select_from(PaymentObservation)), db.scalar(select(func.count()).select_from(ProviderEvidenceGap)),
                db.scalar(select(func.count()).select_from(PaymentEvent)))


# --- 1. integrity failures are not "duplicate evidence" -------------------------------------------------------------------

def test_an_unexpected_observation_integrity_failure_fails_closed_and_is_not_called_a_duplicate(app, seller, paid, monkeypatch):
    pid, payment, _ = paid
    before = snapshot(app, seller, pid)
    events_before = counts(app)
    monkeypatch.setattr(obs_service, "_existing_keys", lambda *a, **k: set())  # make the dedupe check miss so the UNIQUE constraint fires
    response = sync(seller, pid)
    assert response.status_code == 500 and response.json()["error"]["code"] == "evidence_storage_failed"
    monkeypatch.undo()
    with DbSession(app.state.engine) as db:
        rec = db.get(PaymentRecord, payment["id"])
        assert rec.verification_note != "DUPLICATE_TRANSACTION_ID"
    assert counts(app) == events_before  # nothing half-written: no anomaly event, no extra rows
    assert snapshot(app, seller, pid) == before


def test_an_unrelated_integrity_error_during_sync_is_not_labelled_a_duplicate_transaction(app, seller, paid, monkeypatch):
    pid, payment, _ = paid
    from app.services import reconcile

    def boom(*args, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("CHECK constraint failed: something_else"))

    before = counts(app)
    monkeypatch.setattr(reconcile, "apply", boom)
    response = sync(seller, pid)
    assert response.status_code == 500 and response.json()["error"]["code"] == "evidence_storage_failed"
    monkeypatch.undo()
    with DbSession(app.state.engine) as db:
        assert db.get(PaymentRecord, payment["id"]).verification_note != "DUPLICATE_TRANSACTION_ID"
    assert counts(app) == before


def test_only_the_transaction_id_unique_constraint_counts_as_a_duplicate_transaction():
    dup = IntegrityError("x", {}, Exception("UNIQUE constraint failed: payment_records.paypal_transaction_id"))
    other = IntegrityError("x", {}, Exception("UNIQUE constraint failed: payment_observations.project_id, payment_observations.dedupe_key"))
    assert payments_service._is_duplicate_transaction(dup) and not payments_service._is_duplicate_transaction(other)


# --- 2. skipped provider values are surfaced as incomplete evidence ---------------------------------------------------------

def test_an_unstorable_provider_amount_becomes_a_visible_gap_and_never_a_clean_absence(app, seller, unpaid):
    pid, payment, number = unpaid
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"], amount_minor=BIG, transaction_id="TXN-HUGE-1")
    assert sync(seller, pid).status_code == 200
    with DbSession(app.state.engine) as db:
        gaps = list(db.scalars(select(ProviderEvidenceGap)))
        status = db.get(PaymentRecord, payment["id"]).provider_status
        assert gaps and {g.reason for g in gaps} == {"OVERSIZED_AMOUNT"} and all(len(g.raw_sha256) == 64 for g in gaps)
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "PROVIDER_EVIDENCE_INCOMPLETE")) == 1
    assert status != "PAID"  # reconciliation judged the (impossible) evidence itself; incompleteness never verifies anything
    assert claim(seller, pid, number, transaction_id="TXN-HUGE-1", amount_minor=80000).status_code == 201
    body = comparison(seller, pid)
    pay = body["payments"][0]
    assert pay["provider_evidence_complete"] is False and pay["evidence_gap_count"] == len(gaps)
    incomplete = finding(body, "PROVIDER_EVIDENCE_INCOMPLETE")
    assert incomplete and incomplete[0]["gap_ids"] and incomplete[0]["severity"] == "WARNING"
    assert finding(body, "CLAIM_PROVIDER_EVIDENCE_INCOMPLETE") and not finding(body, "CLAIM_NOT_IN_PROVIDER_EVIDENCE")
    assert not finding(body, "CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER")
    assert snapshot(app, seller, pid)["payments"][0][2] == status


def test_each_unparseable_provider_value_kind_is_recorded_once(app, seller, unpaid):
    pid, payment, _ = unpaid
    with DbSession(app.state.engine) as db:
        rec = db.get(PaymentRecord, payment["id"])
        evidence = ProviderInvoice(
            invoice_id="INV-X", invoice_number=None, state=ProviderInvoiceState.PAID, currency="USD", total_minor=100,
            payments=(ProviderTransaction("T" * 101, "PAYPAL", 100, "USD"), ProviderTransaction("TXN-NEG", "PAYPAL", -5, "USD"),
                      ProviderTransaction("TXN-CUR", "PAYPAL", 5, "usd"), ProviderTransaction("TXN-BIG", "PAYPAL", BIG, "USD")),
            refunds=(ProviderTransaction("TXN-RBIG", "PAYPAL", BIG, "USD"),))
        obs_service.record_provider_evidence(db, rec, evidence)
        obs_service.record_provider_evidence(db, rec, evidence)  # same evidence again: no new gaps
        db.commit()
        reasons = sorted(g.reason for g in db.scalars(select(ProviderEvidenceGap)))
    assert reasons == ["INVALID_CURRENCY", "NEGATIVE_AMOUNT", "OVERLONG_TRANSACTION_ID", "OVERSIZED_AMOUNT", "OVERSIZED_AMOUNT"]


def test_gaps_are_append_only(app, unpaid):
    with DbSession(app.state.engine) as db:
        rec = db.scalar(select(PaymentRecord))
        obs_service.record_provider_evidence(db, rec, ProviderInvoice("I", None, ProviderInvoiceState.PAID, "USD", 100,
                                                                      payments=(ProviderTransaction("TXN-BIG", "PAYPAL", BIG, "USD"),)))
        db.commit()
        gap = db.scalar(select(ProviderEvidenceGap))
        gap.reason = "NEGATIVE_AMOUNT"
        with pytest.raises(AppendOnlyError):
            db.commit()
        db.rollback()
        db.delete(db.scalar(select(ProviderEvidenceGap)))
        with pytest.raises(AppendOnlyError):
            db.commit()


def test_a_provider_transaction_listed_twice_is_not_double_counted(app, seller, paid):
    pid, payment, _ = paid
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"], transaction_id="TXN-PROVIDER-1")
    sync(seller, pid)
    assert comparison(seller, pid)["payments"][0]["provider_paid_minor"] == 80000


# --- 3. resource limits ---------------------------------------------------------------------------------------------------

def many_rows(number, count, txn=None):
    return csv_of(*[line(number, amount=100, txn=txn or "", row_id=f"R{i:04d}") for i in range(count)])


def test_observation_and_import_listings_are_bounded_pages_with_totals(seller, paid):
    pid, _, number = paid
    assert ledger(seller, pid, many_rows(number, 30)).status_code == 201
    assert ledger(seller, pid, many_rows(number, 31)).status_code == 201
    first = seller.client.get(f"/api/v1/projects/{pid}/payment-observations", params={"source": "MERCHANT_IMPORT", "limit": 10})
    assert len(first.json()) == 10 and first.headers["X-Total-Count"] == "31"  # file 2 repeats rows 0-29 (counted duplicates, not stored) and adds one
    second = seller.client.get(f"/api/v1/projects/{pid}/payment-observations", params={"source": "MERCHANT_IMPORT", "limit": 10, "offset": 10})
    ids = [o["id"] for o in first.json()] + [o["id"] for o in second.json()]
    assert len(set(ids)) == 20
    assert [o["id"] for o in first.json() + second.json()] == [o["id"] for o in observations(seller, pid, source="MERCHANT_IMPORT", limit=20)]
    for bad in ({"limit": 201}, {"limit": 0}, {"offset": -1}, {"offset": 100_001}):
        assert seller.client.get(f"/api/v1/projects/{pid}/payment-observations", params=bad).status_code == 422
    imports = seller.client.get(f"/api/v1/projects/{pid}/merchant-ledger/imports", params={"limit": 1})
    assert len(imports.json()) == 1 and imports.headers["X-Total-Count"] == "2"
    assert seller.client.get(f"/api/v1/projects/{pid}/merchant-ledger/imports", params={"limit": 101}).status_code == 422


def test_the_comparison_is_paged_caps_citations_and_stays_deterministic(seller, paid):
    pid, _, number = paid
    assert ledger(seller, pid, many_rows(number, 25, txn="TXN-SAME-1")).status_code == 201
    body = comparison(seller, pid)
    assert body["total_payments"] == 1 and body["limit"] == 25 and body["offset"] == 0
    duplicate = finding(body, "DUPLICATE_CLAIM")[0]
    assert duplicate["citations_total"] == 25 and len(duplicate["citations"]) == observation_compare.MAX_CITATIONS
    assert comparison(seller, pid) == body
    empty = seller.get(f"/api/v1/projects/{pid}/observation-comparison?offset=5").json()
    assert empty["payments"] == [] and empty["total_payments"] == 1
    for bad in ("limit=51", "limit=0", "offset=-1"):
        assert seller.get(f"/api/v1/projects/{pid}/observation-comparison?{bad}").status_code == 422


def test_comparison_work_per_payment_is_bounded_and_says_so(seller, paid, monkeypatch):
    pid, _, number = paid
    assert ledger(seller, pid, many_rows(number, 6)).status_code == 201
    monkeypatch.setattr(observation_compare, "MAX_OBSERVATIONS_PER_PAYMENT", 3)
    body = comparison(seller, pid)
    assert finding(body, "COMPARISON_INPUT_TRUNCATED")


def test_reads_are_throttled_separately_from_writes(app, seller, paid):
    pid, _, _ = paid
    app.state.observation_read_throttle = LoginThrottle(2, 60)
    assert seller.get(f"/api/v1/projects/{pid}/observation-comparison").status_code == 200
    assert seller.client.get(f"/api/v1/projects/{pid}/payment-observations").status_code == 200
    blocked = seller.get(f"/api/v1/projects/{pid}/observation-comparison")
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "60"


def test_stored_claims_per_project_are_capped_and_a_refused_import_records_nothing(app, seller, paid, monkeypatch):
    pid, _, number = paid
    monkeypatch.setattr(obs_service, "MAX_CLAIMS_PER_PROJECT", 3)
    assert claim(seller, pid, number, claim_id="C1").status_code == 201
    over = ledger(seller, pid, many_rows(number, 3))
    assert over.status_code == 409 and over.json()["error"]["code"] == "observation_limit_reached"
    assert observations(seller, pid, source="MERCHANT_IMPORT") == []
    assert claim(seller, pid, number, claim_id="C2").status_code == 201 and claim(seller, pid, number, claim_id="C3").status_code == 201
    assert claim(seller, pid, number, claim_id="C4").status_code == 409


# --- 4. duplicate claims ---------------------------------------------------------------------------------------------------

def test_a_retry_is_one_claim_but_distinct_claim_ids_are_independent_claims(seller, paid):
    pid, _, number = paid
    kw = dict(transaction_id=None, amount_minor=30000)
    one = claim(seller, pid, number, claim_id="A-1", **kw)
    retry = claim(seller, pid, number, claim_id="A-1", **kw)
    other = claim(seller, pid, number, claim_id="A-2", **kw)
    assert (one.status_code, retry.status_code, other.status_code) == (201, 200, 201)
    assert retry.json()["created"] is False and retry.json()["observation"]["id"] == one.json()["observation"]["id"]
    assert other.json()["observation"]["id"] != one.json()["observation"]["id"]
    body = comparison(seller, pid)
    assert body["payments"][0]["claimed_paid_minor"] == 60000  # two independent claims are both counted
    duplicate = finding(body, "DUPLICATE_CLAIM")
    assert duplicate and "separate submissions" in duplicate[0]["summary"] and duplicate[0]["citations_total"] == 2


def test_a_reused_claim_id_with_different_content_is_refused(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, claim_id="A-1", amount_minor=100).status_code == 201
    conflict = claim(seller, pid, number, claim_id="A-1", amount_minor=200)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "claim_id_conflict"


def test_identical_claims_without_a_claim_id_are_a_retry_and_the_response_says_so(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number).status_code == 201
    again = claim(seller, pid, number)
    assert again.status_code == 200 and again.json()["created"] is False
    assert len(observations(seller, pid, source="SELLER_ENTRY")) == 1


def test_the_same_transaction_claimed_by_an_import_and_by_hand_is_kept_twice_but_counted_once(seller, paid):
    pid, _, number = paid
    assert ledger(seller, pid, csv_of(line(number, txn="TXN-PROVIDER-1", row_id="R1"))).status_code == 201
    assert claim(seller, pid, number, transaction_id="TXN-PROVIDER-1").status_code == 201
    assert len([o for o in observations(seller, pid) if o["source"] != "PROVIDER_API"]) == 2
    body = comparison(seller, pid)
    duplicate = finding(body, "DUPLICATE_CLAIM")[0]
    assert "MERCHANT_IMPORT" in duplicate["summary"] and "SELLER_ENTRY" in duplicate["summary"]
    assert body["payments"][0]["claimed_paid_minor"] == 80000


def test_claim_ids_do_not_cross_projects(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, claim_id="SAME").status_code == 201
    from tests.test_payments import make_project, sent_invoice
    from app.services.payments import invoice_number_for

    other = make_project(seller)
    payment = sent_invoice(seller, other, )
    assert claim(seller, other, invoice_number_for(payment["id"]), claim_id="SAME", amount_minor=1).status_code == 201


# --- 5. trust boundaries ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["source", "confidence", "actor", "import_id", "instrument", "provider_state", "observed_at", "reason", "gap"])
def test_no_request_can_choose_provenance_or_forge_provider_evidence(seller, paid, field):
    pid, _, number = paid
    body = {"invoice_number": number, "kind": "PAYMENT", "currency": "USD", "amount_minor": 1, field: "PROVIDER_API"}
    assert seller.post(f"/api/v1/projects/{pid}/payment-observations", json=body).status_code == 422
    assert seller.post(f"/api/v1/projects/{pid}/merchant-ledger/imports", json={"csv": csv_of(line(number)), field: "x"}).status_code == 422


def test_there_is_no_route_that_writes_evidence_gaps_or_webhooks(app):
    paths = set(app.openapi()["paths"])
    assert not any("gap" in p or "webhook" in p for p in paths)
    with pytest.raises(NotImplementedError):
        obs_service.record_webhook_observation(None, None)


def test_claims_and_gaps_leave_status_and_holds_alone(app, seller, unpaid):
    pid, payment, number = unpaid
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"], amount_minor=BIG, transaction_id="TXN-HUGE-2")
    sync(seller, pid)
    before = snapshot(app, seller, pid)
    assert ledger(seller, pid, csv_of(line(number, txn="TXN-HUGE-2", row_id="R1"))).status_code == 201
    comparison(seller, pid)
    assert snapshot(app, seller, pid) == before


def test_another_seller_cannot_page_or_compare_this_project(seller, seller_factory, paid):
    pid, _, _ = paid
    intruder = seller_factory("intruder@example.com")
    assert intruder.client.get(f"/api/v1/projects/{pid}/payment-observations?limit=5").status_code == 404
    assert intruder.client.get(f"/api/v1/projects/{pid}/merchant-ledger/imports?limit=5").status_code == 404
    assert intruder.get(f"/api/v1/projects/{pid}/observation-comparison?limit=5").status_code == 404


# --- review round: window flooding, duplicate-transaction evidence, retries ------------------------------------------------

def test_a_flood_of_claims_cannot_push_provider_evidence_out_of_the_comparison(seller, paid, monkeypatch):
    pid, _, number = paid
    monkeypatch.setattr(observation_compare, "MAX_OBSERVATIONS_PER_PAYMENT", 3)
    assert ledger(seller, pid, csv_of(*[line(number, amount=100, txn="", row_id=f"R{i}") for i in range(8)])).status_code == 201
    pay = comparison(seller, pid)["payments"][0]
    assert pay["provider_paid_minor"] == 80000 and pay["claims_truncated"] is True and pay["provider_evidence_complete"] is True
    codes_now = [f["code"] for f in pay["findings"]]
    assert "COMPARISON_INPUT_TRUNCATED" in codes_now
    assert "PROVIDER_PAYMENT_NOT_IN_LEDGER" not in codes_now and "CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER" not in codes_now


def test_a_provider_transaction_id_reused_on_another_payment_is_flagged_and_its_evidence_is_kept(app, seller, unpaid, seller_factory):
    from tests.test_payments import make_project, sent_invoice

    pid, first, _ = unpaid
    second_project = make_project(seller)
    second = sent_invoice(seller, second_project)
    for payment in (first, second):
        app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"], transaction_id="TXN-REUSED-1")
    assert sync(seller, pid).status_code == 200
    assert sync(seller, second_project).status_code == 200
    with DbSession(app.state.engine) as db:
        rec = db.get(PaymentRecord, second["id"])
        assert rec.verification_note == "DUPLICATE_TRANSACTION_ID" and rec.paypal_transaction_id is None
        stored = db.scalar(select(func.count()).select_from(PaymentObservation).where(
            PaymentObservation.payment_id == second["id"], PaymentObservation.source == "PROVIDER_API"))
        events = db.scalar(select(func.count()).select_from(PaymentEvent).where(
            PaymentEvent.payment_id == second["id"], PaymentEvent.note == "DUPLICATE_TRANSACTION_ID"))
    assert stored >= 2 and events == 1  # the contradictory provider readings are kept beside the anomaly
    assert sync(seller, second_project).status_code == 200  # idempotent: no second anomaly event
    with DbSession(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(PaymentEvent).where(
            PaymentEvent.payment_id == second["id"], PaymentEvent.note == "DUPLICATE_TRANSACTION_ID")) == 1


def test_a_transient_observation_race_is_retried_once_but_a_persistent_failure_still_fails_closed(app, seller, paid, monkeypatch):
    pid, _, _ = paid
    real = obs_service._existing_keys
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        return set() if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(obs_service, "_existing_keys", flaky)
    assert sync(seller, pid).status_code == 200  # first attempt collides, the retry sees the committed rows
    monkeypatch.setattr(obs_service, "_existing_keys", lambda *a, **k: set())
    assert sync(seller, pid).status_code == 500


def test_identical_unstorable_provider_records_each_leave_their_own_gap(app, unpaid):
    with DbSession(app.state.engine) as db:
        rec = db.scalar(select(PaymentRecord))
        obs_service.record_provider_evidence(db, rec, ProviderInvoice("I", None, ProviderInvoiceState.PAID, "USD", 100, payments=(
            ProviderTransaction("TXN-A", "PAYPAL", BIG, "USD"), ProviderTransaction("TXN-A", "PAYPAL", BIG, "USD"))))
        db.commit()
        assert db.scalar(select(func.count()).select_from(ProviderEvidenceGap)) == 2


def test_the_snapshot_hash_depends_on_the_page(seller, paid):
    pid, _, _ = paid
    first = seller.get(f"/api/v1/projects/{pid}/observation-comparison").json()
    shifted = seller.get(f"/api/v1/projects/{pid}/observation-comparison?offset=1").json()
    assert first["comparison_sha256"] != shifted["comparison_sha256"]


# --- final fixes: finding suppression, truncated refunds, settlement storage errors, downgrade guard ---------------------------

def test_a_flood_of_claims_cannot_hide_provider_findings_gaps_or_external_payments(app, seller, paid):
    pid, payment, number = paid
    provider = app.state.payment_provider
    provider.simulate_external_payment(payment["paypal_invoice_id"])
    provider.simulate_state(payment["paypal_invoice_id"], ProviderInvoiceState.SENT)  # the provider "un-pays" the invoice
    sync(seller, pid)
    with DbSession(app.state.engine) as db:
        db.add(ProviderEvidenceGap(project_id=pid, payment_id=payment["id"], kind="PAYMENT", reason="OVERSIZED_AMOUNT",
                                   raw_sha256="a" * 64, dedupe_key="b" * 64))
        db.commit()
    for batch in range(3):  # 1,500 reference-less claims, each its own row
        rows = [line(number, amount=100 + batch * 500 + i, txn="", row_id=f"B{batch}-{i:04d}") for i in range(500)]
        assert ledger(seller, pid, csv_of(*rows)).status_code == 201
    body = comparison(seller, pid)
    pay = body["payments"][0]
    shown = [f["code"] for f in pay["findings"]]
    for code in ("PROVIDER_EVIDENCE_INCOMPLETE", "PROVIDER_STATE_REGRESSION", "PROVIDER_REPORTS_EXTERNAL_PAYMENT"):
        assert code in shown, code
    assert len(pay["findings"]) <= observation_compare.MAX_FINDINGS_PER_PAYMENT
    aggregate = finding(body, "CLAIM_WITHOUT_TRANSACTION_REFERENCE")
    assert len(aggregate) == 1 and aggregate[0]["citations_total"] == 1500 and len(aggregate[0]["citations"]) == observation_compare.MAX_CITATIONS
    assert aggregate[0]["summary"].startswith("1500 findings of this kind")
    severities = [observation_compare._SEVERITY_RANK[observation_compare.Sev(f["severity"])] for f in pay["findings"]]
    assert severities == sorted(severities)  # CONFLICT, then WARNING, then INFO
    assert comparison(seller, pid) == body  # still deterministic


def test_refunds_are_not_declared_larger_than_payments_when_older_claims_were_left_out(seller, paid, monkeypatch):
    pid, _, number = paid
    rows = [line(number, kind="PAYMENT", amount=1000, txn=f"PAY-{i:03d}", row_id=f"P{i}") for i in range(3)]
    rows.append(line(number, kind="REFUND", amount=2500, txn="REF-001", row_id="R1"))
    assert ledger(seller, pid, csv_of(*rows)).status_code == 201
    assert finding(comparison(seller, pid), "CLAIMED_REFUNDS_EXCEED_PAYMENTS") == []  # 3000 paid, 2500 refunded: fine
    monkeypatch.setattr(observation_compare, "MAX_OBSERVATIONS_PER_PAYMENT", 1)  # only the newest claim (the refund) is compared
    body = comparison(seller, pid)
    assert body["payments"][0]["claims_truncated"] is True
    assert finding(body, "CLAIMED_REFUNDS_EXCEED_PAYMENTS") == []


def test_a_storage_failure_while_settling_a_new_invoice_is_reported_and_recoverable_without_a_second_provider_call(app, seller, monkeypatch):
    from tests.test_payments import INVOICE, invoice, make_project

    pid = make_project(seller)

    def broken(*args, **kwargs):
        raise obs_service.ObservationStorageError("simulated")

    monkeypatch.setattr(obs_service, "record_provider_evidence", broken)
    failed = invoice(seller, pid, INVOICE, key="settle-key-0001")
    assert failed.status_code == 500 and failed.json()["error"]["code"] == "evidence_storage_failed"
    provider = app.state.payment_provider
    assert len(provider._invoices) == 1  # the provider was called once and never retried automatically
    with DbSession(app.state.engine) as db:
        rec = db.scalar(select(PaymentRecord))
        assert rec.operation_state == "CREATE_UNCERTAIN" and rec.paypal_invoice_id is None
    monkeypatch.undo()
    retried = invoice(seller, pid, INVOICE, key="settle-key-0001")  # the client's retry with the SAME key resumes
    assert retried.status_code in (200, 201) and retried.json()["paypal_invoice_id"]
    assert len(provider._invoices) == 1  # the provider deduplicated on its request id: still a single invoice


def test_downgrades_refuse_while_evidence_records_exist(tmp_path):
    from alembic import command
    from sqlalchemy import inspect

    from app.db import make_engine
    from tests.test_migrations import config_for

    url = f"sqlite:///{tmp_path / 'guard.sqlite3'}"
    command.upgrade(config_for(url), "head")
    engine = make_engine(url)
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sellers (id, email, password_hash, display_name, created_at) VALUES ('s1','g@example.com','x','G','2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO projects (id, seller_id, title, status, created_at) VALUES ('p1','s1','T','DRAFT','2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO payment_records (id, project_id, provider, idempotency_key, request_fingerprint, recipient_email, currency, amount_minor, "
                          "amount_paid_minor, amount_refunded_minor, provider_status, operation_state, created_at, version) VALUES "
                          "('r1','p1','MOCK_PAYPAL','k','f','b@example.org','USD',500,0,0,'UNVERIFIED','READY','2026-01-01 00:00:00',1)"))
        conn.execute(text("INSERT INTO provider_evidence_gaps (id, project_id, payment_id, kind, reason, raw_sha256, observed_at, dedupe_key) "
                          "VALUES ('g1','p1','r1','PAYMENT','OVERSIZED_AMOUNT','" + "a" * 64 + "','2026-01-01 00:00:00','" + "b" * 64 + "')"))
    with pytest.raises(Exception, match="refusing to downgrade"):
        command.downgrade(config_for(url), "0004")
    assert "provider_evidence_gaps" in set(inspect(make_engine(url)).get_table_names())


def test_the_observation_tables_are_also_protected_from_a_destructive_downgrade(tmp_path):
    from alembic import command
    from sqlalchemy import inspect, text

    from app.db import make_engine
    from tests.test_migrations import config_for

    url = f"sqlite:///{tmp_path / 'guard2.sqlite3'}"
    command.upgrade(config_for(url), "head")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sellers (id, email, password_hash, display_name, created_at) VALUES ('s1','g@example.com','x','G','2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO projects (id, seller_id, title, status, created_at) VALUES ('p1','s1','T','DRAFT','2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO merchant_ledger_imports (id, project_id, file_sha256, row_count, recorded_count, duplicate_count, created_at) "
                          "VALUES ('i1','p1','" + "c" * 64 + "',1,1,0,'2026-01-01 00:00:00')"))
    command.downgrade(config_for(url), "0004")  # the (empty) gaps table may go
    with pytest.raises(Exception, match="refusing to downgrade"):
        command.downgrade(config_for(url), "0003")
    assert "merchant_ledger_imports" in set(inspect(make_engine(url)).get_table_names())
