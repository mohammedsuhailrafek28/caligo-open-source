"""Payment API behaviour against the in-process MOCK provider (Stage 2C: draft -> send -> explicit sync)."""
import re
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.models import AppendOnlyError, PaymentEvent, PaymentRecord
from app.payment_provider import ProviderInvoiceState as P
from tests.helpers import login, register

INVOICE = {"currency": "USD", "amount_minor": 80000, "recipient_email": "Buyer@Example.org"}
SEND = {"confirm_recipient_email": "buyer@example.org"}
KEY = "invoice-key-0001"


def make_project(seller) -> str:
    return seller.post("/api/v1/projects", json={"title": "Site"}).json()["id"]


def invoice(seller, pid, body=None, key=KEY):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return seller.post(f"/api/v1/projects/{pid}/invoices", json=body or INVOICE, headers=headers)


def send(seller, pid, payment_id, body=None):
    return seller.post(f"/api/v1/projects/{pid}/payments/{payment_id}/send", json=body or SEND)


def sync(seller, pid):
    return seller.post(f"/api/v1/projects/{pid}/payments/sync")


def payments(seller, pid):
    return seller.get(f"/api/v1/projects/{pid}/payments").json()


def events(seller, pid, payment_id):
    return seller.get(f"/api/v1/projects/{pid}/payments/{payment_id}/events").json()


def project_status(seller, pid):
    return seller.get(f"/api/v1/projects/{pid}").json()["status"]


def rows(app, model=PaymentRecord):
    with DbSession(app.state.engine) as db:
        return db.scalars(select(model)).all()


def age_operation(app, payment_id, minutes=10):
    with DbSession(app.state.engine) as db:
        db.execute(text("UPDATE payment_records SET operation_started_at = datetime('now', :d) WHERE id = :i"),
                   {"d": f"-{minutes} minutes", "i": payment_id})
        db.commit()


def sent_invoice(seller, pid, **kw):
    created = invoice(seller, pid, **kw).json()
    assert send(seller, pid, created["id"]).status_code == 200
    return created


# --- draft creation / idempotency ---------------------------------------------------------------

def test_create_invoice_makes_a_mock_draft_and_sends_nothing(app, seller):
    pid = make_project(seller)
    response = invoice(seller, pid)
    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "MOCK_PAYPAL" and body["paypal_invoice_id"].startswith("MOCK-INV-")
    assert (body["provider_status"], body["invoice_state"], body["operation_state"]) == ("UNVERIFIED", "DRAFT", "READY")
    assert body["paypal_transaction_id"] is None and body["amount_paid_minor"] == 0
    assert project_status(seller, pid) == "DRAFT"
    assert app.state.payment_provider.get_invoice(body["paypal_invoice_id"]).state is P.DRAFT
    assert rows(app)[0].recipient_email == "buyer@example.org"


def test_same_key_same_payload_is_a_safe_retry(app, seller):
    pid = make_project(seller)
    first = invoice(seller, pid)
    retry = invoice(seller, pid, body={**INVOICE, "recipient_email": "buyer@example.org"})
    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.json()["id"] == first.json()["id"]
    assert len(rows(app)) == 1 and len(app.state.payment_provider._invoices) == 1


def test_same_key_different_payload_conflicts(seller):
    pid = make_project(seller)
    invoice(seller, pid)
    clash = invoice(seller, pid, body={**INVOICE, "amount_minor": 1})
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_key_conflict"


def test_a_second_active_invoice_is_refused_even_with_a_new_key(app, seller):
    pid = make_project(seller)
    invoice(seller, pid)
    second = invoice(seller, pid, key="invoice-key-0002")
    assert second.status_code == 409 and second.json()["error"]["code"] == "project_already_invoiced"
    assert len(rows(app)) == 1


def test_idempotency_key_is_scoped_to_the_project(seller):
    a, b = make_project(seller), make_project(seller)
    assert invoice(seller, a).status_code == 201 and invoice(seller, b).status_code == 201


@pytest.mark.parametrize("key", [None, "short", "has space in it!", "x" * 65])
def test_idempotency_key_is_required_and_validated(seller, key):
    response = invoice(seller, make_project(seller), key=key)
    assert response.status_code == 400 and response.json()["error"]["code"] == "idempotency_key_invalid"


@pytest.mark.parametrize("extra", [{"provider_status": "PAID"}, {"status": "PAID"}, {"seller_id": "x"},
                                   {"amount_paid_minor": 80000}, {"invoice_state": "PAID"}])
def test_request_body_cannot_set_payment_state(app, seller, extra):
    response = invoice(seller, make_project(seller), body={**INVOICE, **extra})
    assert response.status_code == 422 and rows(app) == []


@pytest.mark.parametrize("override", [{"amount_minor": 0}, {"amount_minor": -5}, {"amount_minor": 10.5},
                                      {"amount_minor": "800"}, {"amount_minor": 2_000_000_001}, {"currency": "usd"},
                                      {"recipient_email": "not-an-email"}])
def test_invoice_input_is_validated(seller, override):
    assert invoice(seller, make_project(seller), body={**INVOICE, **override}).status_code == 422


def test_payment_writes_require_csrf(seller):
    pid = make_project(seller)
    payment_id = invoice(seller, pid).json()["id"]
    seller.csrf = None
    assert invoice(seller, pid, key="invoice-key-0009").status_code == 403
    assert send(seller, pid, payment_id).status_code == 403
    assert sync(seller, pid).status_code == 403


def test_payments_disabled_when_mode_is_disabled(make_app):
    client = TestClient(make_app(paypal_mode="disabled"))
    register(client, "d@example.com")
    s = login(client, "d@example.com")
    pid = make_project(s)
    assert invoice(s, pid).status_code == 503
    assert sync(s, pid).status_code == 503


# --- explicit send ---------------------------------------------------------------------------------

def test_send_requires_the_recipient_to_be_retyped(app, seller):
    pid = make_project(seller)
    payment = invoice(seller, pid).json()
    wrong = send(seller, pid, payment["id"], {"confirm_recipient_email": "someone-else@example.org"})
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "recipient_confirmation_mismatch"
    assert app.state.payment_provider.get_invoice(payment["paypal_invoice_id"]).state is P.DRAFT
    ok = send(seller, pid, payment["id"], {"confirm_recipient_email": "BUYER@example.org"})
    assert ok.status_code == 200
    assert (ok.json()["provider_status"], ok.json()["invoice_state"]) == ("PENDING", "SENT")
    assert project_status(seller, pid) == "INVOICED"


def test_a_sent_invoice_cannot_be_sent_twice(seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    again = send(seller, pid, payment["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "invoice_not_sendable"


def test_send_is_owner_only(seller_factory):
    owner, other = seller_factory("owner@example.com"), seller_factory("other@example.com")
    pid = make_project(owner)
    payment = invoice(owner, pid).json()
    assert send(other, pid, payment["id"]).status_code == 404
    assert send(owner, make_project(owner), payment["id"]).status_code == 404  # payment of another project
    anonymous = TestClient(owner.client.app)
    assert anonymous.post(f"/api/v1/projects/{pid}/payments/{payment['id']}/send", json=SEND).status_code == 401


# --- GET is strictly read-only; POST sync reconciles ------------------------------------------------

def test_get_payments_never_contacts_the_provider_or_writes(app, seller, monkeypatch):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"])  # paid at the provider
    before = [(r.id, r.version, r.provider_status) for r in rows(app)]
    event_count = len(rows(app, PaymentEvent))

    def forbidden(*_a, **_k):
        raise AssertionError("GET must not call the provider")
    for name in ("get_invoice", "create_invoice", "send_invoice"):
        monkeypatch.setattr(app.state.payment_provider, name, forbidden)
    assert payments(seller, pid)[0]["provider_status"] == "PENDING"  # stale until an explicit sync
    assert seller.get(f"/api/v1/projects/{pid}").json()["payments"][0]["provider_status"] == "PENDING"
    assert events(seller, pid, payment["id"])
    assert [(r.id, r.version, r.provider_status) for r in rows(app)] == before
    assert len(rows(app, PaymentEvent)) == event_count
    assert project_status(seller, pid) == "INVOICED"


def test_sync_marks_a_verified_payment_paid(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"])
    result = sync(seller, pid)
    assert result.status_code == 200
    body = result.json()[0]
    assert (body["provider_status"], body["amount_paid_minor"]) == ("PAID", 80000)
    assert body["paypal_transaction_id"].startswith("MOCK-TXN-") and body["checked_at"]
    assert project_status(seller, pid) == "PAID"
    kinds = [e["event_type"] for e in events(seller, pid, payment["id"])]
    assert kinds[-1] == "STATUS_CHANGED"


def test_an_unpaid_invoice_is_never_marked_paid(seller):
    pid = make_project(seller)
    sent_invoice(seller, pid)
    assert sync(seller, pid).json()[0]["provider_status"] == "PENDING"
    assert project_status(seller, pid) == "INVOICED"


def test_sync_is_idempotent_and_adds_no_noise(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"])
    sync(seller, pid)
    count = len(rows(app, PaymentEvent))
    sync(seller, pid)
    sync(seller, pid)
    assert len(rows(app, PaymentEvent)) == count


@pytest.mark.parametrize("wrong", [{"amount_minor": 79999}, {"currency": "EUR"}])
def test_amount_or_currency_mismatch_is_never_paid(app, seller, wrong):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"], **wrong)
    body = sync(seller, pid).json()[0]
    assert body["provider_status"] == "NEEDS_REVIEW" and "AMOUNT_OR_CURRENCY_MISMATCH" in body["verification_note"]
    assert body["amount_paid_minor"] == 0 and project_status(seller, pid) == "INVOICED"


def test_externally_marked_paid_is_not_proof_of_a_paypal_payment(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_external_payment(payment["paypal_invoice_id"])
    body = sync(seller, pid).json()[0]
    assert body["provider_status"] == "NEEDS_REVIEW" and "EXTERNALLY_MARKED_PAID" in body["verification_note"]
    assert body["amount_paid_minor"] == 0 and project_status(seller, pid) == "INVOICED"


def test_partial_payment_is_not_paid(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_partial_payment(payment["paypal_invoice_id"], 30000)
    body = sync(seller, pid).json()[0]
    assert (body["provider_status"], body["amount_paid_minor"]) == ("PARTIALLY_PAID", 30000)
    assert project_status(seller, pid) == "INVOICED"


def test_partial_then_complete_payment_becomes_paid(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, payment["paypal_invoice_id"]
    mock.simulate_partial_payment(iid, 30000)
    sync(seller, pid)
    mock.simulate_partial_payment(iid, 50000)
    mock.simulate_state(iid, P.PAID)
    body = sync(seller, pid).json()[0]
    assert (body["provider_status"], body["amount_paid_minor"]) == ("PAID", 80000)
    assert project_status(seller, pid) == "PAID"


def test_full_refund_sets_refunded_but_never_reverses_the_project(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, payment["paypal_invoice_id"]
    mock.simulate_payment(iid)
    sync(seller, pid)
    mock.simulate_refund(iid)
    body = sync(seller, pid).json()[0]
    assert (body["provider_status"], body["amount_refunded_minor"], body["amount_paid_minor"]) == ("REFUNDED", 80000, 80000)
    assert project_status(seller, pid) == "PAID"  # refunding money does not undo completed work
    history = [(e["from_status"], e["to_status"]) for e in events(seller, pid, payment["id"]) if e["event_type"] == "STATUS_CHANGED"]
    assert history == [("PENDING", "PAID"), ("PAID", "REFUNDED")]
    assert len(rows(app)) == 1  # the record is preserved


def test_partial_refund_is_recorded_without_claiming_a_full_refund(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, payment["paypal_invoice_id"]
    mock.simulate_payment(iid)
    sync(seller, pid)
    mock.simulate_refund(iid, 20000)
    body = sync(seller, pid).json()[0]
    assert (body["provider_status"], body["amount_refunded_minor"]) == ("PARTIALLY_REFUNDED", 20000)
    mock.simulate_refund(iid, 60000)  # the remainder
    assert sync(seller, pid).json()[0]["provider_status"] == "REFUNDED"


def test_contradictory_evidence_does_not_downgrade_a_verified_payment(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, payment["paypal_invoice_id"]
    mock.simulate_payment(iid)
    sync(seller, pid)
    mock.simulate_state(iid, P.SENT)  # provider claims "unpaid" while still listing a payment: contradictory
    body = sync(seller, pid).json()[0]
    assert body["provider_status"] == "PAID" and "PAYMENT_ACTIVITY_IN_UNPAID_STATE" in body["verification_note"]
    assert any(e["event_type"] == "ANOMALY" for e in events(seller, pid, payment["id"]))


def test_refund_exceeding_payment_fails_closed(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, payment["paypal_invoice_id"]
    mock.simulate_partial_payment(iid, 10000)
    sync(seller, pid)
    mock.simulate_refund(iid, 90000)
    body = sync(seller, pid).json()[0]
    assert body["provider_status"] == "NEEDS_REVIEW" and "REFUND_EXCEEDS_PAYMENT" in body["verification_note"]


def test_a_recorded_transaction_id_cannot_be_replaced(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, payment["paypal_invoice_id"]
    mock.simulate_payment(iid)
    original = sync(seller, pid).json()[0]["paypal_transaction_id"]
    current = mock._invoices[iid]
    forged = replace(current.payments[0], transaction_id="MOCK-TXN-FORGED")
    mock._invoices[iid] = replace(current, payments=(forged,))
    body = sync(seller, pid).json()[0]
    assert body["paypal_transaction_id"] == original and "TRANSACTION_ID_CHANGED_REVIEW" in body["verification_note"]


# --- cancellation and replacement -------------------------------------------------------------------

def test_cancelled_invoice_returns_project_to_draft_and_needs_explicit_replacement(app, seller):
    pid = make_project(seller)
    first = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_state(first["paypal_invoice_id"], P.CANCELLED)
    assert sync(seller, pid).json()[0]["provider_status"] == "CANCELLED"
    assert project_status(seller, pid) == "DRAFT"
    implicit = invoice(seller, pid, key="invoice-key-0002")
    assert implicit.status_code == 409 and implicit.json()["error"]["code"] == "replacement_required"
    explicit = invoice(seller, pid, key="invoice-key-0002", body={**INVOICE, "replaces_payment_id": first["id"]})
    assert explicit.status_code == 201 and explicit.json()["replaces_payment_id"] == first["id"]
    assert [p["provider_status"] for p in payments(seller, pid)] == ["CANCELLED", "UNVERIFIED"]


def test_refunded_invoice_can_be_replaced_only_explicitly_and_history_is_kept(app, seller):
    pid = make_project(seller)
    first = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, first["paypal_invoice_id"]
    mock.simulate_payment(iid)
    sync(seller, pid)
    mock.simulate_refund(iid)
    sync(seller, pid)
    assert invoice(seller, pid, key="invoice-key-0002").status_code == 409
    second = invoice(seller, pid, key="invoice-key-0002", body={**INVOICE, "replaces_payment_id": first["id"]})
    assert second.status_code == 201
    listing = payments(seller, pid)
    assert [p["provider_status"] for p in listing] == ["REFUNDED", "UNVERIFIED"]
    assert listing[0]["amount_paid_minor"] == 80000 and listing[0]["amount_refunded_minor"] == 80000
    assert project_status(seller, pid) == "PAID"


def test_replacement_is_refused_while_another_invoice_is_active(seller):
    pid = make_project(seller)
    first = sent_invoice(seller, pid)
    blocked = invoice(seller, pid, key="invoice-key-0002", body={**INVOICE, "replaces_payment_id": first["id"]})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "project_already_invoiced"


def test_replacement_must_reference_a_payment_of_the_same_project(seller):
    pid = make_project(seller)
    other_project_payment = invoice(seller, make_project(seller)).json()["id"]
    response = invoice(seller, pid, body={**INVOICE, "replaces_payment_id": other_project_payment})
    assert response.status_code == 404 and response.json()["error"]["code"] == "replaced_payment_not_found"


def test_payment_after_local_cancel_is_flagged_not_applied(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, payment["paypal_invoice_id"]
    mock.simulate_state(iid, P.CANCELLED)
    sync(seller, pid)
    mock.simulate_payment(iid)
    body = sync(seller, pid).json()[0]
    assert body["provider_status"] == "CANCELLED" and "PAID_AFTER_CANCEL_MANUAL_REVIEW" in body["verification_note"]
    assert body["amount_paid_minor"] == 0 and project_status(seller, pid) == "DRAFT"


# --- ownership --------------------------------------------------------------------------------------

def test_other_sellers_cannot_read_sync_or_create_on_a_project(seller_factory):
    owner, other = seller_factory("owner@example.com"), seller_factory("other@example.com")
    pid = make_project(owner)
    payment = sent_invoice(owner, pid)
    assert other.get(f"/api/v1/projects/{pid}/payments").status_code == 404
    assert sync(other, pid).status_code == 404
    assert other.get(f"/api/v1/projects/{pid}/payments/{payment['id']}/events").status_code == 404
    assert invoice(other, pid, key="invoice-key-0003").status_code == 404
    assert payments(owner, pid)[0]["provider_status"] == "PENDING"


def test_payments_require_authentication(app, seller):
    pid = make_project(seller)
    anonymous = TestClient(app)
    assert anonymous.get(f"/api/v1/projects/{pid}/payments").status_code == 401
    assert anonymous.post(f"/api/v1/projects/{pid}/payments/sync").status_code == 401


# --- failures and interrupted operations --------------------------------------------------------------

def test_provider_failure_during_create_leaves_a_resumable_record(app, seller, monkeypatch):
    pid = make_project(seller)
    mock = app.state.payment_provider
    real = mock.create_invoice
    monkeypatch.setattr(mock, "create_invoice", lambda _r: (_ for _ in ()).throw(RuntimeError("boom")))
    failed = invoice(seller, pid)
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "provider_outcome_uncertain"
    assert rows(app)[0].operation_state == "CREATE_UNCERTAIN" and rows(app)[0].paypal_invoice_id is None
    monkeypatch.setattr(mock, "create_invoice", real)
    resumed = invoice(seller, pid)  # same key resumes the same record
    assert resumed.status_code == 201 and resumed.json()["operation_state"] == "READY"
    assert len(rows(app)) == 1 and len(mock._invoices) == 1


def test_stale_in_flight_creation_can_be_resumed_after_a_crash(app, seller):
    pid = make_project(seller)
    first = invoice(seller, pid).json()
    with DbSession(app.state.engine) as db:  # simulate a crash that left the row mid-create with no provider id
        db.execute(text("UPDATE payment_records SET operation_state='CREATING', paypal_invoice_id=NULL, "
                        "operation_started_at='2000-01-01 00:00:00' WHERE id=:i"), {"i": first["id"]})
        db.commit()
    resumed = invoice(seller, pid)
    assert resumed.status_code == 201 and resumed.json()["paypal_invoice_id"]
    assert len(rows(app)) == 1


def test_fresh_in_flight_creation_is_not_duplicated(app, seller):
    pid = make_project(seller)
    first = invoice(seller, pid).json()
    with DbSession(app.state.engine) as db:
        db.execute(text("UPDATE payment_records SET operation_state='CREATING', paypal_invoice_id=NULL, "
                        "operation_started_at=CURRENT_TIMESTAMP WHERE id=:i"), {"i": first["id"]})
        db.commit()
    again = invoice(seller, pid)
    assert again.status_code == 409 and again.json()["error"]["code"] == "operation_in_progress"
    assert len(app.state.payment_provider._invoices) == 1


def test_provider_outage_during_sync_fails_closed(app, seller, monkeypatch):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    monkeypatch.setattr(app.state.payment_provider, "get_invoice", lambda _i: (_ for _ in ()).throw(RuntimeError("down")))
    result = sync(seller, pid)
    assert result.status_code == 502 and result.json()["error"]["code"] == "provider_unavailable"
    body = payments(seller, pid)[0]
    assert body["provider_status"] == "PENDING" and "STATUS_UNCHANGED" in body["verification_note"]
    assert body["id"] == payment["id"] and project_status(seller, pid) == "INVOICED"


def test_send_failure_is_reported_and_leaves_the_invoice_resendable(app, seller, monkeypatch):
    pid = make_project(seller)
    payment = invoice(seller, pid).json()
    mock = app.state.payment_provider
    real = mock.send_invoice
    monkeypatch.setattr(mock, "send_invoice", lambda _i: (_ for _ in ()).throw(RuntimeError("down")))
    failed = send(seller, pid, payment["id"])
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "provider_outcome_uncertain"
    assert rows(app)[0].operation_state == "SEND_UNCERTAIN"
    monkeypatch.setattr(mock, "send_invoice", real)
    early = sync(seller, pid).json()[0]  # still DRAFT, but too soon to be sure PayPal is not mid-send
    assert early["operation_state"] == "SEND_UNCERTAIN" and "UNCONFIRMED" in early["verification_note"]
    assert send(seller, pid, payment["id"]).status_code == 409  # no second email while unsure
    age_operation(app, payment["id"])
    synced = sync(seller, pid).json()[0]  # DRAFT after the settle window: nothing was sent
    assert synced["operation_state"] == "READY" and synced["invoice_state"] == "DRAFT"
    assert send(seller, pid, payment["id"]).status_code == 200


def test_uncertain_send_that_actually_went_out_is_resolved_by_sync(app, seller, monkeypatch):
    pid = make_project(seller)
    payment = invoice(seller, pid).json()
    mock = app.state.payment_provider
    real = mock.send_invoice

    def sent_then_lost(invoice_id):
        real(invoice_id)
        raise RuntimeError("connection dropped after processing")
    monkeypatch.setattr(mock, "send_invoice", sent_then_lost)
    assert send(seller, pid, payment["id"]).status_code == 502
    body = sync(seller, pid).json()[0]
    assert (body["provider_status"], body["operation_state"], body["invoice_state"]) == ("PENDING", "READY", "SENT")
    assert send(seller, pid, payment["id"]).status_code == 409  # cannot be sent a second time


def test_no_database_transaction_is_open_during_provider_calls(app, seller):
    pid = make_project(seller)
    mock = app.state.payment_provider
    observed: list[tuple[str, int]] = []
    for name in ("create_invoice", "send_invoice", "get_invoice"):
        real = getattr(mock, name)

        def wrapper(*args, _real=real, _name=name):
            observed.append((_name, app.state.engine.pool.checkedout()))
            return _real(*args)
        setattr(mock, name, wrapper)
    payment = invoice(seller, pid).json()
    send(seller, pid, payment["id"])
    mock.simulate_payment(payment["paypal_invoice_id"])
    sync(seller, pid)
    assert {n for n, _ in observed} == {"create_invoice", "send_invoice", "get_invoice"}
    assert all(open_connections == 0 for _, open_connections in observed), observed


# --- concurrency -------------------------------------------------------------------------------------

def test_concurrent_syncs_apply_a_payment_exactly_once(app, seller_factory):
    seller = seller_factory("conc@example.com")
    other_tab = login(TestClient(app), "conc@example.com")
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"])
    results, barrier = [], threading.Barrier(2, timeout=10)

    def run(session):
        barrier.wait()
        results.append(sync(session, pid))
    threads = [threading.Thread(target=run, args=(s,)) for s in (seller, other_tab)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert payments(seller, pid)[0]["provider_status"] == "PAID"
    paid_events = [e for e in events(seller, pid, payment["id"]) if e["to_status"] == "PAID" and e["event_type"] == "STATUS_CHANGED"]
    assert len(paid_events) == 1 and project_status(seller, pid) == "PAID"


def test_concurrent_creates_with_different_keys_make_one_active_invoice(app, seller_factory):
    seller = seller_factory("race@example.com")
    other_tab = login(TestClient(app), "race@example.com")
    pid = make_project(seller)
    results, barrier = [], threading.Barrier(2, timeout=10)

    def run(session, key):
        barrier.wait()
        results.append(invoice(session, pid, key=key).status_code)
    threads = [threading.Thread(target=run, args=a) for a in ((seller, "race-key-0001"), (other_tab, "race-key-0002"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2 and sorted(results) == [201, 409]
    active = [r for r in rows(app) if r.provider_status not in ("CANCELLED", "REFUNDED")]
    assert len(active) == 1


# --- history is append-only, DB invariants -------------------------------------------------------------

def test_payment_events_are_append_only(app, seller):
    pid = make_project(seller)
    sent_invoice(seller, pid)
    with DbSession(app.state.engine) as db:
        event = db.scalars(select(PaymentEvent)).first()
        event.note = "tampered"
        with pytest.raises(AppendOnlyError):
            db.flush()
        db.rollback()
        db.delete(db.scalars(select(PaymentEvent)).first())
        with pytest.raises(AppendOnlyError):
            db.flush()
        db.rollback()
    with DbSession(app.state.engine) as db:
        for sql in ("UPDATE payment_events SET note='x'", "DELETE FROM payment_events"):
            with pytest.raises(DatabaseError):
                db.execute(text(sql))
            db.rollback()


def test_database_allows_only_one_active_invoice_per_project(app, seller):
    pid = make_project(seller)
    invoice(seller, pid)
    with DbSession(app.state.engine) as db:
        first = db.scalars(select(PaymentRecord)).one()
        db.add(PaymentRecord(project_id=first.project_id, provider="MOCK_PAYPAL", idempotency_key="other-key-0001",
                             request_fingerprint="f" * 64, recipient_email="b@example.org", currency="USD", amount_minor=5))
        with pytest.raises(IntegrityError):
            db.flush()


def test_database_enforces_idempotency_uniqueness(app, seller):
    pid = make_project(seller)
    invoice(seller, pid)
    with DbSession(app.state.engine) as db:
        first = db.scalars(select(PaymentRecord)).one()
        first.provider_status = "CANCELLED"
        db.commit()
        db.add(PaymentRecord(project_id=first.project_id, provider="MOCK_PAYPAL", idempotency_key=first.idempotency_key,
                             request_fingerprint="f" * 64, recipient_email="b@example.org", currency="USD", amount_minor=5))
        with pytest.raises(IntegrityError):
            db.flush()


def test_financial_status_is_assigned_only_by_the_reconciliation_module():
    """PAID / PARTIALLY_* / REFUNDED can be written only in services/reconcile.py (single writer)."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r"provider_status\s*=\s*[^=].*(PAID|REFUND)")
    offenders = [f"{p.name}:{n}" for p in app_dir.rglob("*.py") if p.name != "reconcile.py"
                 for n, line in enumerate(p.read_text(encoding="utf8").splitlines(), 1) if pattern.search(line)]
    assert offenders == []


# --- the mock is clearly a mock and offline ---------------------------------------------------------------

def test_mock_provider_is_idempotent_distinguishable_and_offline():
    from app.payment_provider import MockPayPalProvider, ProviderInvoiceRequest
    mock = MockPayPalProvider()
    request = ProviderInvoiceRequest("pem-key-0001", "PEM-1", "USD", 100, "b@example.org")
    first, second = mock.create_invoice(request), mock.create_invoice(request)
    assert first.invoice_id == second.invoice_id and first.invoice_id.startswith("MOCK-INV-")
    assert mock.name.value == "MOCK_PAYPAL" and first.state is P.DRAFT


# --- review-driven hardening --------------------------------------------------------------------------

def test_a_crash_after_the_send_claim_is_recoverable_by_sync(app, seller):
    pid = make_project(seller)
    payment = invoice(seller, pid).json()
    with DbSession(app.state.engine) as db:  # process died after claiming SEND_PENDING, before calling the provider
        db.execute(text("UPDATE payment_records SET operation_state='SEND_PENDING' WHERE id=:i"), {"i": payment["id"]})
        db.commit()
    assert send(seller, pid, payment["id"]).status_code == 409
    assert sync(seller, pid).json()[0]["operation_state"] == "SEND_PENDING"  # too early to be sure
    age_operation(app, payment["id"])
    assert sync(seller, pid).json()[0]["operation_state"] == "READY"
    assert send(seller, pid, payment["id"]).status_code == 200


def test_recorded_money_never_decreases(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, payment["paypal_invoice_id"]
    mock.simulate_partial_payment(iid, 30000)
    sync(seller, pid)
    current = mock._invoices[iid]
    mock._invoices[iid] = replace(current, payments=(replace(current.payments[0], amount_minor=10000),))
    body = sync(seller, pid).json()[0]
    assert body["amount_paid_minor"] == 30000 and "AMOUNT_DECREASED" in body["verification_note"]


def test_rejection_after_an_uncertain_create_is_reviewed_not_cancelled(app, seller, monkeypatch):
    from app.payment_provider import ProviderRejected
    pid = make_project(seller)
    mock = app.state.payment_provider
    monkeypatch.setattr(mock, "create_invoice", lambda _r: (_ for _ in ()).throw(RuntimeError("lost")))
    assert invoice(seller, pid).status_code == 502
    monkeypatch.setattr(mock, "create_invoice", lambda _r: (_ for _ in ()).throw(ProviderRejected("SOMETHING_ELSE")))
    assert invoice(seller, pid).status_code == 502  # same key, provider now rejects: it may already exist
    assert rows(app)[0].provider_status == "NEEDS_REVIEW"
    blocked = invoice(seller, pid, key="invoice-key-0002")  # still the active invoice: no silent second one
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "project_already_invoiced"


def test_replaying_a_cleanly_rejected_create_reports_the_failure(app, seller, monkeypatch):
    from app.payment_provider import ProviderRejected
    pid = make_project(seller)
    monkeypatch.setattr(app.state.payment_provider, "create_invoice",
                        lambda _r: (_ for _ in ()).throw(ProviderRejected("INVALID_REQUEST")))
    assert invoice(seller, pid).status_code == 422
    replay = invoice(seller, pid)
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "idempotency_key_failed"


def test_replacement_is_blocked_when_a_cancelled_invoice_shows_payment(app, seller):
    pid = make_project(seller)
    first = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, first["paypal_invoice_id"]
    mock.simulate_state(iid, P.CANCELLED)
    sync(seller, pid)
    mock.simulate_payment(iid)
    sync(seller, pid)
    blocked = invoice(seller, pid, key="invoice-key-0002", body={**INVOICE, "replaces_payment_id": first["id"]})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "replacement_blocked_review"


def test_provider_touching_routes_are_rate_limited_per_seller(make_app):
    app = make_app(payment_ops_per_minute=3)
    client = TestClient(app)
    register(client, "rate@example.com")
    s = login(client, "rate@example.com")
    pid = make_project(s)
    assert [sync(s, pid).status_code for _ in range(5)] == [200, 200, 200, 429, 429]
    other_client = TestClient(app)
    register(other_client, "other-rate@example.com")
    other = login(other_client, "other-rate@example.com")
    assert sync(other, make_project(other)).status_code == 200
