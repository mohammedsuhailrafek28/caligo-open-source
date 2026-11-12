"""R3 payment observations and merchant ledger: provenance, claims vs provider evidence, strict CSV import, comparison findings,
isolation, replay, concurrency, source impersonation, append-only storage. Offline only (MOCK provider)."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as DbSession

from app.models import (
    AppendOnlyError, InstructionChangeVerification, MerchantLedgerImport, PaymentEvent, PaymentObservation, PaymentRecord, Project,
)
from app.schemas import ProviderInvoiceState as P
from app.services import observations as obs_service
from app.services.payments import invoice_number_for
from tests.test_instruction_integrity import CHANGE, hold, propose
from tests.test_payments import INVOICE, make_project, rows, sent_invoice, sync

HEADER = "kind,invoice_number,currency,amount_minor,transaction_id,asserted_at,note,merchant_row_id"
NOW = datetime.now(timezone.utc)


def stamp(delta: timedelta) -> str:
    return (NOW + delta).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def line(number, kind="PAYMENT", currency="USD", amount=80000, txn="TXN-0001", asserted="", note="", row_id=""):
    return f"{kind},{number},{currency},{amount},{txn},{asserted},{note},{row_id}"


def csv_of(*lines, header=HEADER):
    return "\n".join([header, *lines]) + "\n"


def ledger(seller, pid, text_):
    return seller.post(f"/api/v1/projects/{pid}/merchant-ledger/imports", json={"csv": text_})


def claim(seller, pid, number, **kw):
    body = {"invoice_number": number, "kind": "PAYMENT", "currency": "USD", "amount_minor": 80000, "transaction_id": "TXN-0001", **kw}
    return seller.post(f"/api/v1/projects/{pid}/payment-observations", json=body)


def observations(seller, pid, **params):
    response = seller.client.get(f"/api/v1/projects/{pid}/payment-observations", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def comparison(seller, pid):
    response = seller.get(f"/api/v1/projects/{pid}/observation-comparison")
    assert response.status_code == 200, response.text
    return response.json()


def codes(body):
    return [f["code"] for p in body["payments"] for f in p["findings"]]


def finding(body, code):
    return [f for p in body["payments"] for f in p["findings"] if f["code"] == code]


@pytest.fixture()
def paid(app, seller):
    """A project whose single invoice the provider reports as PAID with transaction TXN-PROVIDER-1, already synchronized."""
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"], transaction_id="TXN-PROVIDER-1")
    assert sync(seller, pid).status_code == 200
    return pid, payment, invoice_number_for(payment["id"])


@pytest.fixture()
def unpaid(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    assert sync(seller, pid).status_code == 200
    return pid, payment, invoice_number_for(payment["id"])


def snapshot(app, seller, pid):
    with DbSession(app.state.engine) as db:
        return {
            "payments": [(r.id, r.version, r.provider_status, r.amount_paid_minor, r.paypal_transaction_id) for r in db.scalars(select(PaymentRecord))],
            "project": db.get(Project, pid).status,
            "events": db.scalar(select(func.count()).select_from(PaymentEvent)),
            "verifications": db.scalar(select(func.count()).select_from(InstructionChangeVerification)),
            "hold": hold(seller, pid) if db.execute(text("SELECT count(*) FROM payment_instructions")).scalar() else None,
        }


# --- provider evidence is recorded by reconciliation, once, and kept when it contradicts itself ---------------

def test_synchronization_records_provider_observations_with_provider_confidence(seller, paid):
    pid, payment, number = paid
    provider = observations(seller, pid, source="PROVIDER_API")
    kinds = {o["kind"] for o in provider}
    assert kinds == {"INVOICE_STATE", "PAYMENT"}
    for o in provider:
        assert o["confidence"] == "PROVIDER_EVIDENCE" and o["asserted_at"] is None and len(o["payload_sha256"]) == 64
        assert o["payment_id"] == payment["id"] and o["observed_at"]
    pay = next(o for o in provider if o["kind"] == "PAYMENT")
    assert pay["source_event_id"] == "TXN-PROVIDER-1" and pay["instrument"] == "PAYPAL" and pay["amount_minor"] == 80000


def test_resynchronizing_the_same_evidence_adds_nothing(seller, paid):
    pid, _, _ = paid
    before = observations(seller, pid)
    assert sync(seller, pid).status_code == 200 and sync(seller, pid).status_code == 200
    assert observations(seller, pid) == before


def test_a_changed_provider_state_is_a_new_observation_and_history_is_kept(app, seller, unpaid):
    pid, payment, _ = unpaid
    iid = payment["paypal_invoice_id"]
    app.state.payment_provider.simulate_partial_payment(iid, 30000)
    sync(seller, pid)
    states = [o["provider_state"] for o in observations(seller, pid, source="PROVIDER_API") if o["kind"] == "INVOICE_STATE"]
    assert states[-2:] == ["SENT", "PARTIALLY_PAID"] or states[-1] == "PARTIALLY_PAID"
    assert len(states) >= 2


def test_provider_state_regression_is_visible_but_reconciliation_decides_status(app, seller, paid):
    pid, payment, _ = paid
    app.state.payment_provider.simulate_state(payment["paypal_invoice_id"], P.SENT)  # the provider "un-pays" the invoice
    sync(seller, pid)
    body = comparison(seller, pid)
    regression = finding(body, "PROVIDER_STATE_REGRESSION")
    assert regression and regression[0]["severity"] == "CONFLICT" and len(regression[0]["citations"]) == 2
    assert body["payments"][0]["authoritative_status"] == seller.get(f"/api/v1/projects/{pid}/payments").json()[0]["provider_status"]


def test_external_payments_are_flagged_as_not_paypal_proof(app, seller, unpaid):
    pid, payment, _ = unpaid
    app.state.payment_provider.simulate_external_payment(payment["paypal_invoice_id"])
    sync(seller, pid)
    assert finding(comparison(seller, pid), "PROVIDER_REPORTS_EXTERNAL_PAYMENT")


# --- claims are claims -----------------------------------------------------------------------------------------

def test_an_imported_row_is_an_unverified_claim_with_separate_timestamps(seller, paid):
    pid, _, number = paid
    asserted = stamp(-timedelta(hours=2))
    response = ledger(seller, pid, csv_of(line(number, asserted=asserted, note="bank deposit", row_id="R1")))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["row_count"] == 1 and body["recorded_count"] == 1 and body["duplicate_count"] == 0 and body["replay"] is False
    assert "unverified" in body["note"].lower() and "releases no payment-instruction hold" in body["note"]
    claim_row = observations(seller, pid, source="MERCHANT_IMPORT")[0]
    assert claim_row["confidence"] == "UNVERIFIED_CLAIM" and claim_row["source"] == "MERCHANT_IMPORT"
    assert claim_row["import_id"] == body["id"] and "Unverified" in claim_row["provenance"]
    assert claim_row["asserted_at"].startswith(asserted[:19]) and claim_row["observed_at"] != claim_row["asserted_at"]
    assert claim_row["note"] == "bank deposit" and claim_row["source_reference"] == "R1"


def test_a_claim_never_changes_status_confirms_a_payment_or_adds_payment_history(app, seller, unpaid):
    pid, payment, number = unpaid
    before = snapshot(app, seller, pid)
    assert ledger(seller, pid, csv_of(line(number, amount=80000, txn="TXN-FAKE-PAID"))).status_code == 201
    assert claim(seller, pid, number, transaction_id="TXN-FAKE-2").status_code == 201
    after = snapshot(app, seller, pid)
    assert after == before
    current = seller.get(f"/api/v1/projects/{pid}/payments").json()[0]
    assert current["provider_status"] == "PENDING" and current["amount_paid_minor"] == 0 and current["paypal_transaction_id"] is None
    assert seller.get(f"/api/v1/projects/{pid}").json()["status"] != "PAID"
    assert finding(comparison(seller, pid), "CLAIM_NOT_IN_PROVIDER_EVIDENCE")


def test_claims_cannot_release_or_alter_a_payment_instruction_hold(app, seller, unpaid):
    pid, _, number = unpaid
    assert hold(seller, pid)["active"] is False  # the invoice recorded the implicit instruction
    assert propose(seller, pid, {**CHANGE, "amount_minor": 90000}).status_code == 201
    assert hold(seller, pid)["active"] is True
    with DbSession(app.state.engine) as db:
        before = db.scalar(select(func.count()).select_from(InstructionChangeVerification))
    assert ledger(seller, pid, csv_of(line(number))).status_code == 201
    assert claim(seller, pid, number, transaction_id="TXN-X-9").status_code == 201
    assert hold(seller, pid)["active"] is True
    with DbSession(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(InstructionChangeVerification)) == before == 0


def test_the_import_and_seller_claims_are_audited_without_claiming_a_status_change(seller, paid):
    pid, _, number = paid
    ledger(seller, pid, csv_of(line(number)))
    claim(seller, pid, number, transaction_id="TXN-AUD-01")
    events = {e["event_type"]: e for e in seller.get(f"/api/v1/projects/{pid}/audit-events").json()}
    for name in ("MERCHANT_LEDGER_IMPORTED", "SELLER_PAYMENT_CLAIM_RECORDED"):
        assert events[name]["actor_role"] == "SELLER" and events[name]["detail"]["payment_status_changed"] is False
        assert events[name]["detail"]["hold_changed"] is False and events[name]["detail"]["confidence"] == "UNVERIFIED_CLAIM"


# --- replay, duplicates, concurrency -----------------------------------------------------------------------------

def test_the_same_file_is_not_imported_twice(app, seller, paid):
    pid, _, number = paid
    text_ = csv_of(line(number, txn="TXN-A-0001"), line(number, txn="TXN-A-0002", amount=100))
    first = ledger(seller, pid, text_)
    second = ledger(seller, pid, text_)
    assert first.status_code == 201 and second.status_code == 200 and second.json()["replay"] is True
    assert second.json()["id"] == first.json()["id"]
    assert len(observations(seller, pid, source="MERCHANT_IMPORT")) == 2
    assert len(seller.get(f"/api/v1/projects/{pid}/merchant-ledger/imports").json()) == 1


def test_rows_repeated_in_an_overlapping_export_are_counted_as_duplicates(seller, paid):
    pid, _, number = paid
    first = ledger(seller, pid, csv_of(line(number, txn="TXN-A-0001")))
    second = ledger(seller, pid, csv_of(line(number, txn="TXN-A-0001"), line(number, txn="TXN-A-0002", amount=500)))
    assert second.status_code == 201 and second.json()["recorded_count"] == 1 and second.json()["duplicate_count"] == 1
    assert first.json()["id"] != second.json()["id"]


def test_identical_lines_inside_one_file_are_reported_not_silently_merged(seller, paid):
    pid, _, number = paid
    response = ledger(seller, pid, csv_of(line(number, txn=""), line(number, txn="")))
    assert response.json()["recorded_count"] == 1 and response.json()["duplicate_count"] == 1


def test_distinct_row_ids_keep_identical_payments_apart(seller, paid):
    pid, _, number = paid
    response = ledger(seller, pid, csv_of(line(number, txn="", amount=500, row_id="A1"), line(number, txn="", amount=500, row_id="A2")))
    assert response.json()["recorded_count"] == 2


def test_concurrent_identical_imports_record_one_batch(app, seller, paid):
    pid, _, number = paid
    text_ = csv_of(line(number, txn="TXN-C-0001"), line(number, txn="TXN-C-0002", amount=7))
    results, barrier = [], threading.Barrier(6)

    def attempt():
        barrier.wait()
        results.append(ledger(seller, pid, text_).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1 and set(results) <= {200, 201, 503, 429}, results
    with DbSession(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(MerchantLedgerImport)) == 1
        assert db.scalar(select(func.count()).select_from(PaymentObservation).where(PaymentObservation.source == "MERCHANT_IMPORT")) == 2


def test_concurrent_overlapping_imports_never_double_record_a_row(app, seller, paid):
    pid, _, number = paid
    files = [csv_of(line(number, txn="TXN-D-0001"), line(number, txn=f"TXN-D-{n:04d}", amount=n)) for n in range(2, 6)]
    barrier = threading.Barrier(len(files))
    statuses = []

    def attempt(text_):
        barrier.wait()
        statuses.append(ledger(seller, pid, text_).status_code)

    threads = [threading.Thread(target=attempt, args=(f,)) for f in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 500 not in statuses
    with DbSession(app.state.engine) as db:
        keys = list(db.scalars(select(PaymentObservation.dedupe_key).where(PaymentObservation.source == "MERCHANT_IMPORT")))
        assert len(keys) == len(set(keys))
        shared = db.scalar(select(func.count()).select_from(PaymentObservation).where(PaymentObservation.source_event_id == "TXN-D-0001"))
        assert shared == 1


def test_concurrent_identical_seller_claims_record_one(app, seller, paid):
    pid, _, number = paid
    results, barrier = [], threading.Barrier(5)

    def attempt():
        barrier.wait()
        results.append(claim(seller, pid, number, transaction_id="TXN-E-0001").status_code)

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(201) == 1 and set(results) <= {200, 201, 429, 503}
    assert len(observations(seller, pid, source="SELLER_ENTRY")) == 1


# --- strict CSV validation ------------------------------------------------------------------------------------------

def bad_cases(number):
    ok = lambda **kw: csv_of(line(number, **kw))  # noqa: E731
    return {
        "empty file": "",
        "header only": HEADER + "\n",
        "unknown column": csv_of(line(number), header=HEADER + ",extra").replace(line(number) + "\n", line(number) + ",x\n"),
        "missing column": "kind,invoice_number,currency\nPAYMENT," + number + ",USD\n",
        "duplicate header": HEADER + ",kind\n" + line(number) + ",PAYMENT\n",
        "decimal amount": ok(amount="12.50"),
        "negative amount": ok(amount="-5"),
        "zero amount": ok(amount="0"),
        "exponent amount": ok(amount="1e3"),
        "text amount": ok(amount="abc"),
        "huge amount": ok(amount="9" * 13),
        "bad kind": ok(kind="GIFT"),
        "lowercase currency": ok(currency="usd"),
        "unsupported currency": ok(currency="ZZZ"),
        "bad transaction ref": ok(txn="has space"),
        "leading dash transaction": ok(txn="-abc123"),
        "naive timestamp": ok(asserted="2026-01-01T10:00:00"),
        "garbage timestamp": ok(asserted="yesterday"),
        "far future": ok(asserted=stamp(timedelta(days=30))),
        "ancient": ok(asserted="1990-01-01T00:00:00Z"),
        "formula note": ok(note="=HYPERLINK(1)"),
        "dash note": ok(note="-1+1"),
        "control character": ok(note="a\u0007b"),
        "nul byte": ok(note="a\x00b"),
        "ragged row": HEADER + "\nPAYMENT," + number + ",USD\n",
        "bad row id": ok(row_id="bad id!"),
        "repeated row id": csv_of(line(number, row_id="R1", txn="TXN-Z-0001"), line(number, row_id="R1", txn="TXN-Z-0002")),
        "unterminated quote": HEADER + '\nPAYMENT,"' + number + ",USD,5,,,,\n",
    }


def test_malformed_files_are_refused_whole_and_record_nothing(app, seller, paid):
    pid, _, number = paid
    for name, text_ in bad_cases(number).items():
        app.state.payment_throttle._failures.clear()  # this test sends many requests; the rate limit is not under test
        response = ledger(seller, pid, text_) if text_ else seller.post(f"/api/v1/projects/{pid}/merchant-ledger/imports", json={"csv": ""})
        assert response.status_code in (422,), (name, response.status_code, response.text)
        if response.status_code == 422 and "error" in response.json() and response.json()["error"]["code"] == "ledger_invalid":
            assert response.json()["error"]["details"], name
    assert observations(seller, pid, source="MERCHANT_IMPORT") == []
    assert seller.get(f"/api/v1/projects/{pid}/merchant-ledger/imports").json() == []


def test_one_bad_row_rejects_every_row(seller, paid):
    pid, _, number = paid
    response = ledger(seller, pid, csv_of(line(number, txn="TXN-G-0001"), line(number, amount="oops", txn="TXN-G-0002")))
    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert {"row": 2, "column": "amount_minor"}.items() <= details[0].items()
    assert observations(seller, pid, source="MERCHANT_IMPORT") == []


def test_size_and_row_limits_are_enforced(seller, paid):
    pid, _, number = paid
    many = csv_of(*[line(number, txn=f"TXN-{n:05d}", amount=n + 1) for n in range(501)])
    assert ledger(seller, pid, many).status_code == 422
    assert seller.post(f"/api/v1/projects/{pid}/merchant-ledger/imports", json={"csv": "x" * 300_001}).status_code == 422
    big_note = csv_of(line(number, note="n" * 201))
    assert ledger(seller, pid, big_note).status_code == 422
    assert observations(seller, pid, source="MERCHANT_IMPORT") == []


def test_a_byte_order_mark_and_crlf_line_endings_are_accepted(seller, paid):
    pid, _, number = paid
    text_ = "﻿" + csv_of(line(number)).replace("\n", "\r\n")
    assert ledger(seller, pid, text_).status_code == 201


# --- ownership, forged references, impersonation --------------------------------------------------------------------

def test_unknown_and_foreign_invoice_references_are_indistinguishable_and_record_nothing(app, seller, seller_factory, paid):
    pid, _, number = paid
    other_seller = seller_factory("other@example.com")
    other_pid = make_project(other_seller)
    foreign_number = invoice_number_for(sent_invoice(other_seller, other_pid)["id"])
    sibling_pid = make_project(seller)
    answers = []
    for ref in (foreign_number, "PEM-DOESNOTEXIST", number):  # the last belongs to ANOTHER PROJECT of the same seller
        target = sibling_pid if ref == number else pid
        response = ledger(seller, target, csv_of(line(ref)))
        assert response.status_code == 422
        answers.append(json.dumps(response.json()["error"]["details"]))
        single = claim(seller, target, ref)
        assert single.status_code == 422 and single.json()["error"]["code"] == "unknown_invoice_reference"
    assert len(set(answers)) == 1
    assert observations(seller, sibling_pid) == [] and observations(other_seller, other_pid, source="MERCHANT_IMPORT") == []


def test_another_seller_cannot_read_import_or_compare_this_project(seller, seller_factory, paid):
    pid, _, number = paid
    ledger(seller, pid, csv_of(line(number)))
    intruder = seller_factory("intruder@example.com")
    for url in (f"/api/v1/projects/{pid}/payment-observations", f"/api/v1/projects/{pid}/observation-comparison",
                f"/api/v1/projects/{pid}/merchant-ledger/imports"):
        assert intruder.get(url).status_code == 404
    assert ledger(intruder, pid, csv_of(line(number))).status_code == 404
    assert claim(intruder, pid, number).status_code == 404


def test_requests_cannot_choose_the_source_confidence_or_actor(seller, paid):
    pid, _, number = paid
    base_body = {"invoice_number": number, "kind": "PAYMENT", "currency": "USD", "amount_minor": 1, "transaction_id": "TXN-IMP-001"}
    for extra in ({"source": "PROVIDER_API"}, {"confidence": "PROVIDER_EVIDENCE"}, {"actor": "SYSTEM"}, {"source": "WEBHOOK"},
                  {"instrument": "PAYPAL"}, {"import_id": "x"}):
        assert seller.post(f"/api/v1/projects/{pid}/payment-observations", json={**base_body, **extra}).status_code == 422
    assert seller.post(f"/api/v1/projects/{pid}/merchant-ledger/imports", json={"csv": csv_of(line(number)), "source": "PROVIDER_API"}).status_code == 422
    assert seller.post(f"/api/v1/projects/{pid}/payment-observations", json={**base_body, "kind": "INVOICE_STATE"}).status_code == 422
    assert all(o["confidence"] == "PROVIDER_EVIDENCE" for o in observations(seller, pid, source="PROVIDER_API"))
    assert all(o["confidence"] == "UNVERIFIED_CLAIM" for o in observations(seller, pid) if o["source"] != "PROVIDER_API")


def test_a_claim_cannot_be_stored_as_provider_evidence_even_by_a_buggy_caller(app, paid):
    pid, payment, _ = paid
    base = dict(project_id=pid, payment_id=payment["id"], kind="PAYMENT", amount_minor=1, currency="USD", payload={}, payload_sha256="0" * 64,
                provenance="x")
    bad = [
        dict(source="MERCHANT_IMPORT", confidence="PROVIDER_EVIDENCE", actor="SELLER"),
        dict(source="SELLER_ENTRY", confidence="PROVIDER_EVIDENCE", actor="SELLER"),
        dict(source="PROVIDER_API", confidence="UNVERIFIED_CLAIM", actor="SYSTEM"),
        dict(source="PROVIDER_API", confidence="PROVIDER_EVIDENCE", actor="SELLER"),
        dict(source="SELLER_ENTRY", confidence="UNVERIFIED_CLAIM", actor="SYSTEM"),
        dict(source="MERCHANT_IMPORT", confidence="UNVERIFIED_CLAIM", actor="SELLER"),  # an import row with no import
        dict(source="WEBHOOK", confidence="PROVIDER_EVIDENCE", actor="SYSTEM"),
        dict(source="SELLER_ENTRY", confidence="UNVERIFIED_CLAIM", actor="SELLER", kind="INVOICE_STATE", provider_state="PAID"),
    ]
    for i, fields in enumerate(bad):
        with DbSession(app.state.engine) as db:
            db.add(PaymentObservation(**{**base, "dedupe_key": f"bad{i}", **fields}))
            with pytest.raises(Exception, match="(?i)constraint"):
                db.flush()


def test_there_is_no_webhook_endpoint_and_the_interface_refuses(app, seller, paid):
    pid, payment, _ = paid
    for url in ("/api/v1/webhooks/paypal", "/api/v1/webhooks", f"/api/v1/projects/{pid}/webhooks"):
        assert seller.post(url, json={}).status_code in (404, 405)
    with DbSession(app.state.engine) as db, pytest.raises(NotImplementedError):
        obs_service.record_webhook_observation(db, obs_service.WebhookObservation("E1", payment["id"], obs_service.Kind.PAYMENT, 1, "USD", None))
    assert observations(seller, pid, source="WEBHOOK") == []


def test_authentication_and_csrf_are_required(app, seller, paid):
    from fastapi.testclient import TestClient

    pid, _, number = paid
    anonymous = TestClient(app)
    assert anonymous.get(f"/api/v1/projects/{pid}/payment-observations").status_code == 401
    assert anonymous.post(f"/api/v1/projects/{pid}/merchant-ledger/imports", json={"csv": "x"}).status_code == 401
    no_csrf = seller.client.post(f"/api/v1/projects/{pid}/merchant-ledger/imports", json={"csv": csv_of(line(number))})
    assert no_csrf.status_code == 403
    assert seller.client.post(f"/api/v1/projects/{pid}/payment-observations", json={}).status_code == 403


# --- comparison findings ---------------------------------------------------------------------------------------------

def test_a_matching_claim_is_corroborated_by_provider_evidence_but_status_is_still_reconciliations(seller, paid):
    pid, _, number = paid
    claim(seller, pid, number, transaction_id="TXN-PROVIDER-1")
    body = comparison(seller, pid)
    corroborated = finding(body, "CLAIM_CORROBORATED")
    assert len(corroborated) == 1
    sources = {c["source"]: c["confidence"] for c in corroborated[0]["citations"]}
    assert sources == {"SELLER_ENTRY": "UNVERIFIED_CLAIM", "PROVIDER_API": "PROVIDER_EVIDENCE"}
    assert "never confirm a payment" in body["authority"]
    pay = body["payments"][0]
    assert pay["authoritative_status"] == "PAID" and pay["provider_paid_minor"] == 80000 and pay["claimed_paid_minor"] == 80000


def test_an_amount_that_differs_from_the_provider_is_a_visible_conflict(seller, paid):
    pid, _, number = paid
    claim(seller, pid, number, transaction_id="TXN-PROVIDER-1", amount_minor=70000)
    conflict = finding(comparison(seller, pid), "CLAIM_AMOUNT_MISMATCH")
    assert len(conflict) == 1 and conflict[0]["severity"] == "CONFLICT" and len(conflict[0]["citations"]) == 2
    assert not finding(comparison(seller, pid), "CLAIM_CORROBORATED")


def test_currency_mismatches_are_recorded_not_hidden(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, currency="EUR", transaction_id="TXN-PROVIDER-1").status_code == 201
    mismatch = finding(comparison(seller, pid), "CLAIM_CURRENCY_MISMATCH")
    assert mismatch and all(m["severity"] == "CONFLICT" for m in mismatch)


def test_a_claim_against_a_draft_invoice_is_not_in_the_providers_evidence(seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    claim(seller, pid, invoice_number_for(payment["id"]))
    assert sorted(set(codes(comparison(seller, pid)))) == ["CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER", "CLAIM_NOT_IN_PROVIDER_EVIDENCE"]


def test_with_no_provider_reading_at_all_a_claim_is_only_awaiting_evidence():
    from app.models import PaymentRecord as Rec
    from app.services.observation_compare import compare_payment

    rec = Rec(id="pay-1", project_id="p", currency="USD", amount_minor=100, provider_status="UNVERIFIED", invoice_number="PEM-X")
    claim_row = PaymentObservation(id="o1", project_id="p", payment_id="pay-1", source="SELLER_ENTRY", kind="PAYMENT", confidence="UNVERIFIED_CLAIM",
                                   actor="SELLER", amount_minor=100, currency="USD", source_event_id="TXN-1", payload={}, payload_sha256="a" * 64,
                                   dedupe_key="k", provenance="x", observed_at=NOW)
    result = compare_payment(rec, [claim_row], ledger_present=False)
    assert [f.code.value for f in result.findings] == ["CLAIM_AWAITING_PROVIDER_EVIDENCE"]


def test_a_claim_without_a_reference_is_compared_only_in_total(seller, paid):
    pid, _, number = paid
    ledger(seller, pid, csv_of(line(number, txn="", amount=80000)))
    body = comparison(seller, pid)
    assert finding(body, "CLAIM_WITHOUT_TRANSACTION_REFERENCE")
    assert not finding(body, "CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER")  # 80000 claimed == 80000 reported


def test_provider_payments_missing_from_the_ledger_are_listed_once_a_ledger_exists(seller, paid):
    pid, _, number = paid
    assert not finding(comparison(seller, pid), "PROVIDER_PAYMENT_NOT_IN_LEDGER")  # no ledger yet: not noise
    ledger(seller, pid, csv_of(line(number, txn="TXN-OTHER-001", amount=5)))
    assert finding(comparison(seller, pid), "PROVIDER_PAYMENT_NOT_IN_LEDGER")
    assert finding(comparison(seller, pid), "CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER")


def test_contradicting_and_duplicate_claims_are_detected(seller, paid):
    pid, _, number = paid
    claim(seller, pid, number, transaction_id="TXN-DUP-0001", amount_minor=500)
    ledger(seller, pid, csv_of(line(number, txn="TXN-DUP-0001", amount=500)))
    body = comparison(seller, pid)
    assert len(finding(body, "DUPLICATE_CLAIM")) == 1 and not finding(body, "CONFLICTING_CLAIMS")
    ledger(seller, pid, csv_of(line(number, txn="TXN-DUP-0001", amount=900)))
    body = comparison(seller, pid)
    assert len(finding(body, "CONFLICTING_CLAIMS")) == 1 and len(finding(body, "CONFLICTING_CLAIMS")[0]["citations"]) == 3
    assert body["payments"][0]["claimed_paid_minor"] == 500  # one transaction counted once, first claim wins; the rest is shown, not summed


def test_over_claims_are_conflicts(seller, paid):
    pid, _, number = paid
    ledger(seller, pid, csv_of(line(number, txn="TXN-OV-0001", amount=80000), line(number, txn="TXN-OV-0002", amount=1)))
    assert finding(comparison(seller, pid), "CLAIMED_PAYMENTS_EXCEED_INVOICE")
    ledger(seller, pid, csv_of(line(number, kind="REFUND", txn="TXN-RF-0001", amount=90000)))
    assert finding(comparison(seller, pid), "CLAIMED_REFUNDS_EXCEED_PAYMENTS")


def test_delayed_future_and_out_of_order_claims_are_reported(seller, paid):
    pid, _, number = paid
    ledger(seller, pid, csv_of(line(number, txn="TXN-T-0001", amount=1, asserted=stamp(timedelta(hours=-1))),
                               line(number, txn="TXN-T-0002", amount=2, asserted=stamp(timedelta(days=-3))),
                               line(number, txn="TXN-T-0003", amount=3, asserted=stamp(timedelta(days=1)))))
    body = comparison(seller, pid)
    assert len(finding(body, "DELAYED_OBSERVATION")) == 1 and len(finding(body, "CLAIM_TIMESTAMP_IN_FUTURE")) == 1
    assert finding(body, "OUT_OF_ORDER_OBSERVATION")  # the -3d row arrives after a row that asserts a later time


def test_findings_are_deterministic_and_cite_real_observations(app, seller, paid):
    pid, _, number = paid
    claim(seller, pid, number, transaction_id="TXN-PROVIDER-1", amount_minor=70000)
    ledger(seller, pid, csv_of(line(number, txn="TXN-DET-0001", amount=3)))
    first, second = comparison(seller, pid), comparison(seller, pid)
    assert first == second and len(first["comparison_sha256"]) == 64
    stored = {o["id"]: o for o in observations(seller, pid)}
    for payment in first["payments"]:
        for f in payment["findings"]:
            assert f["citations"] and f["payment_id"] == payment["payment_id"]
            for c in f["citations"]:
                assert stored[c["observation_id"]]["payload_sha256"] == c["payload_sha256"] and stored[c["observation_id"]]["source"] == c["source"]
    claim(seller, pid, number, transaction_id="TXN-DET-0002", amount_minor=4)
    assert comparison(seller, pid)["comparison_sha256"] != first["comparison_sha256"]


def test_comparison_and_reads_never_write(app, seller, paid):
    pid, _, number = paid
    ledger(seller, pid, csv_of(line(number)))
    with DbSession(app.state.engine) as db:
        before = db.scalar(select(func.count()).select_from(PaymentObservation))
    for _ in range(3):
        comparison(seller, pid)
        observations(seller, pid)
    with DbSession(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(PaymentObservation)) == before


# --- storage guarantees ---------------------------------------------------------------------------------------------

def test_observations_and_imports_are_append_only(app, seller, paid):
    pid, _, number = paid
    ledger(seller, pid, csv_of(line(number)))
    with DbSession(app.state.engine) as db:
        row = db.scalar(select(PaymentObservation))
        row.amount_minor = 1
        with pytest.raises(AppendOnlyError):
            db.flush()
        db.rollback()
        for table in ("payment_observations", "merchant_ledger_imports"):
            for statement in (f"UPDATE {table} SET project_id = project_id", f"DELETE FROM {table}"):
                with pytest.raises(Exception, match="append-only"):
                    db.execute(text(statement))
                db.rollback()


def test_the_provider_path_is_the_only_writer_of_provider_evidence(app):
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    writers = []
    for path in root.rglob("*.py"):
        body = path.read_text(encoding="utf8")
        if re.search(r"Src\.PROVIDER_API|ObservationSource\.PROVIDER_API|PROVIDER_API\b", body) and path.name not in ("schemas.py", "models.py"):
            writers.append(path.name)
    assert sorted(writers) == ["observation_compare.py", "observations.py"]  # reads/comparison and the one recorder
    routers = "".join(p.read_text(encoding="utf8") for p in (root / "routers").glob("*.py"))
    assert "record_provider_evidence" not in routers and "PROVIDER_API" not in routers.replace("ObservationSource", "")


def test_reconciliation_status_logic_does_not_read_observations(app):
    import pathlib

    text_ = (pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "reconcile.py").read_text(encoding="utf8")
    evaluate_body = text_.split("def evaluate", 1)[1].split("def _event", 1)[0]
    assert "observation" not in evaluate_body.lower()
    apply_body = text_.split("def apply", 1)[1]
    assert apply_body.count("observations.") == 1  # one append-only recording call, no reads feeding the decision


# --- migration from a populated R2 database ---------------------------------------------------------------------------

def test_a_populated_r2_database_migrates_to_head_keeping_data_and_append_only(tmp_path):
    from alembic import command
    from sqlalchemy import inspect

    from app.db import current_revision, make_engine
    from tests.test_migrations import config_for

    url = f"sqlite:///{tmp_path / 'r2.sqlite3'}"
    command.upgrade(config_for(url), "0003")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sellers (id, email, password_hash, display_name, created_at) VALUES ('s1','keep@example.com','x','Keep','2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO projects (id, seller_id, title, status, created_at) VALUES ('p1','s1','Old','DRAFT','2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO payment_records (id, project_id, provider, idempotency_key, request_fingerprint, recipient_email, currency, amount_minor, "
                          "amount_paid_minor, amount_refunded_minor, provider_status, operation_state, created_at, version) VALUES "
                          "('r1','p1','MOCK_PAYPAL','k','f','b@example.org','USD',500,0,0,'UNVERIFIED','READY','2026-01-01 00:00:00',1)"))
    command.upgrade(config_for(url), "head")
    engine = make_engine(url)
    assert current_revision(engine) == "0008"
    assert {"payment_observations", "merchant_ledger_imports", "provider_evidence_gaps"} <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM payment_records")).scalar() == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        triggers = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    for table in ("payment_observations", "merchant_ledger_imports", "provider_evidence_gaps"):
        assert {f"{table}_no_update", f"{table}_no_delete"} <= triggers
    command.downgrade(config_for(url), "0004")
    assert "provider_evidence_gaps" not in set(inspect(make_engine(url)).get_table_names())
    assert "payment_observations" in set(inspect(make_engine(url)).get_table_names())
    command.downgrade(config_for(url), "0003")
    assert "payment_observations" not in set(inspect(make_engine(url)).get_table_names())


# --- review fixes (R3 security review) ---------------------------------------------------------------------------------

def test_out_of_range_timestamps_are_a_clean_422_not_a_500(seller, paid):
    pid, _, number = paid
    for bad in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:00:00-05:00"):
        assert ledger(seller, pid, csv_of(line(number, asserted=bad))).status_code == 422
        assert claim(seller, pid, number, asserted_at=bad).status_code == 422
    assert observations(seller, pid, source="SELLER_ENTRY") == [] and observations(seller, pid, source="MERCHANT_IMPORT") == []


def test_seller_entry_notes_follow_the_same_rules_as_csv_notes(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, note="=HYPERLINK(1)").status_code == 422
    assert claim(seller, pid, number, note="ok\x07bell").status_code == 422
    assert claim(seller, pid, number, note="plain note").status_code == 201


def test_a_claim_matching_an_external_record_is_not_corroborated(app, seller, paid):
    pid, _, number = paid
    with DbSession(app.state.engine) as db:
        old = db.scalar(select(PaymentObservation).where(PaymentObservation.project_id == pid, PaymentObservation.kind == "PAYMENT"))
        db.add(PaymentObservation(
            project_id=pid, payment_id=old.payment_id, source="PROVIDER_API", kind="PAYMENT", confidence="PROVIDER_EVIDENCE", actor="SYSTEM",
            amount_minor=7000, currency=old.currency, source_event_id="EXT-0001", instrument="EXTERNAL",
            observed_at=old.observed_at + timedelta(seconds=5), payload={"x": 2}, payload_sha256="d" * 64, dedupe_key="c" * 64,
            provenance=old.provenance))
        db.commit()
    assert claim(seller, pid, number, transaction_id="EXT-0001", amount_minor=7000).status_code == 201
    body = comparison(seller, pid)
    assert finding(body, "CLAIM_MATCHES_EXTERNAL_RECORD") and not finding(body, "CLAIM_CORROBORATED")


def test_provider_totals_use_only_the_newest_reading_of_a_transaction(app, seller, paid):
    pid, payment, number = paid
    with DbSession(app.state.engine) as db:
        old = db.scalar(select(PaymentObservation).where(PaymentObservation.project_id == pid, PaymentObservation.kind == "PAYMENT"))
        newer = PaymentObservation(
            project_id=pid, payment_id=old.payment_id, source="PROVIDER_API", kind="PAYMENT", confidence="PROVIDER_EVIDENCE", actor="SYSTEM",
            amount_minor=old.amount_minor + 500, currency=old.currency, source_event_id=old.source_event_id, instrument=old.instrument,
            observed_at=old.observed_at + timedelta(seconds=5), payload={"x": 1}, payload_sha256="f" * 64, dedupe_key="e" * 64,
            provenance=old.provenance)
        db.add(newer)
        db.commit()
        expected = newer.amount_minor
    body = comparison(seller, pid)
    assert body["payments"][0]["provider_paid_minor"] == expected  # not old + new
    assert finding(body, "PROVIDER_TRANSACTION_CHANGED")
