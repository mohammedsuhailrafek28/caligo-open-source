"""End-to-end sandbox flows: API -> services -> PayPalSandboxProvider -> fake PayPal (MockTransport, no network)."""
import logging
import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import Settings
from app.models import PaymentEvent, PaymentRecord
from tests.fake_paypal import CLIENT_SECRET, MERCHANT, SETTINGS_CREDENTIALS
from tests.helpers import login
from tests.test_payments import INVOICE, KEY, SEND, events, invoice, payments, project_status, send, sync

BUYER_INVOICE = {**INVOICE, "recipient_email": "buyer@example.org"}


def sb_project(seller, with_scope=True) -> str:
    pid = seller.post("/api/v1/projects", json={"title": "Website build"}).json()["id"]
    if with_scope:
        seller.post(f"/api/v1/projects/{pid}/scope-records",
                    json={"kind": "ORIGINAL", "source_label": "Agreed email", "content": "Five-page marketing site."})
    return pid


def create(seller, pid, key=KEY, body=None):
    return invoice(seller, pid, body=body or BUYER_INVOICE, key=key)


def sent(seller, pid, key=KEY):
    created = create(seller, pid, key).json()
    assert send(seller, pid, created["id"]).status_code == 200
    return created


# --- configuration -----------------------------------------------------------------------------

def test_sandbox_mode_requires_credentials_and_an_allowlist():
    with pytest.raises(ValidationError):
        Settings(paypal_mode="sandbox")
    with pytest.raises(ValidationError):
        Settings(paypal_mode="sandbox", **SETTINGS_CREDENTIALS, paypal_sandbox_merchant_email=MERCHANT)  # no allow-list
    with pytest.raises(ValidationError):
        Settings(paypal_mode="sandbox", **SETTINGS_CREDENTIALS, paypal_sandbox_recipient_allowlist="buyer@example.org")  # no merchant


def test_production_still_refuses_any_paypal_mode():
    with pytest.raises(ValidationError):
        Settings(app_env="production", cookie_secure=True, paypal_mode="sandbox", **SETTINGS_CREDENTIALS,
                 paypal_sandbox_recipient_allowlist="buyer@example.org")


def test_live_mode_is_not_representable():
    with pytest.raises(ValidationError):
        Settings(paypal_mode="live")


def test_capabilities_distinguish_mock_and_sandbox(sb, sandbox_app, app):
    sandbox = TestClient(sandbox_app).get("/api/v1/capabilities").json()
    mock = TestClient(app).get("/api/v1/capabilities").json()
    assert sandbox["paypal_sandbox_invoicing"] and not sandbox["mock_paypal_invoicing"]
    assert mock["mock_paypal_invoicing"] and not mock["paypal_sandbox_invoicing"]
    assert not sandbox["paypal_invoicing"]  # live PayPal is not implemented
    assert TestClient(sandbox_app).get("/health").json()["paypal_mode"] == "sandbox"


# --- 3. creation -------------------------------------------------------------------------------

def test_create_makes_a_sandbox_draft_with_scope_reference_and_emails_nobody(sb, fake):
    pid = sb_project(sb)
    response = create(sb, pid)
    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "PAYPAL_SANDBOX" and body["paypal_invoice_id"].startswith("INV2-")
    assert (body["provider_status"], body["invoice_state"], body["operation_state"]) == ("UNVERIFIED", "DRAFT", "READY")
    scope_id = sb.get(f"/api/v1/projects/{pid}/scope-records").json()[0]["id"]
    assert body["scope_record_id"] == scope_id
    assert fake.count("POST", "/send") == 0 and fake.only()["status"] == "DRAFT"
    assert project_status(sb, pid) == "DRAFT"


def test_sandbox_invoice_requires_the_original_scope_record(sb, fake):
    response = create(sb, sb_project(sb, with_scope=False))
    assert response.status_code == 409 and response.json()["error"]["code"] == "original_scope_required"
    assert fake.calls == []


def test_sandbox_recipient_must_be_an_approved_test_identity(sb, fake):
    response = create(sb, sb_project(sb), body={**BUYER_INVOICE, "recipient_email": "real.customer@example.com"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "recipient_not_allowed"
    assert fake.calls == []  # nothing reached the provider


def test_unsupported_currency_is_rejected_before_any_provider_call(sb, fake):
    response = create(sb, sb_project(sb), body={**BUYER_INVOICE, "currency": "XYZ"})
    assert response.status_code == 422 and fake.calls == []


def test_replayed_create_returns_the_same_invoice_without_a_second_provider_invoice(sb, fake):
    pid = sb_project(sb)
    first, again = create(sb, pid), create(sb, pid)
    assert (first.status_code, again.status_code) == (201, 200) and len(fake.invoices) == 1
    assert first.json()["id"] == again.json()["id"]


# --- 4. ambiguous outcomes and resumability -------------------------------------------------------

def test_ambiguous_create_never_duplicates_and_resumes_with_the_same_key(sb, fake, sandbox_app):
    pid = sb_project(sb)
    fake.fault("POST", "/v2/invoicing/invoices", "create_then_timeout")
    failed = create(sb, pid)
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "provider_outcome_uncertain"
    with DbSession(sandbox_app.state.engine) as db:
        record = db.scalars(select(PaymentRecord)).one()
        assert (record.operation_state, record.paypal_invoice_id) == ("CREATE_UNCERTAIN", None)
    resumed = create(sb, pid)
    assert resumed.status_code == 201 and resumed.json()["operation_state"] == "READY"
    assert len(fake.invoices) == 1 and resumed.json()["paypal_invoice_id"] == fake.only()["id"]
    assert len(payments(sb, pid)) == 1


def test_created_but_unverifiable_invoice_is_kept_and_verified_by_sync(sb, fake):
    pid = sb_project(sb)
    fake.fault("GET", "", 500, 500, 500)
    created = create(sb, pid)
    assert created.status_code == 201
    assert created.json()["paypal_invoice_id"] == fake.only()["id"]
    assert created.json()["verification_note"] == "CREATED_UNVERIFIED_SYNC_REQUIRED"
    synced = sync(sb, pid).json()[0]
    assert synced["invoice_state"] == "DRAFT" and synced["verification_note"] is None


def test_rate_limit_on_create_is_retried_then_reported_when_persistent(sb, fake):
    pid = sb_project(sb)
    fake.fault("POST", "/v2/invoicing/invoices", 429)
    assert create(sb, pid).status_code == 201 and len(fake.invoices) == 1
    pid2 = sb_project(sb)
    fake.fault("POST", "/v2/invoicing/invoices", 429, 429, 429)
    limited = create(sb, pid2, key="invoice-key-0002")
    assert limited.status_code == 503 and limited.json()["error"]["code"] == "provider_rate_limited"
    assert limited.headers["Retry-After"] == "30"
    retry = create(sb, pid2, key="invoice-key-0002")  # same key, nothing was created: simply proceeds
    assert retry.status_code == 201 and len(fake.invoices) == 2


def test_server_error_on_create_is_uncertain_and_the_retry_creates_exactly_one(sb, fake):
    pid = sb_project(sb)
    fake.fault("POST", "/v2/invoicing/invoices", 500)
    assert create(sb, pid).status_code == 502
    assert create(sb, pid).status_code == 201
    assert len(fake.invoices) == 1


def test_provider_auth_failure_is_reported_without_details(sandbox_app, sandbox_provider, fake):
    sandbox_provider._client_secret = "rejected-example-credential"
    sb = TestClient(sandbox_app)
    from tests.helpers import register
    register(sb, "auth@example.com")
    seller = login(sb, "auth@example.com")
    response = create(seller, sb_project(seller))
    assert response.status_code == 502 and response.json()["error"]["code"] == "provider_auth_failed"
    assert "rejected-example-credential" not in response.text and CLIENT_SECRET not in response.text


# --- 5. sending ------------------------------------------------------------------------------------

def test_send_is_a_separate_explicit_confirmed_action(sb, fake):
    pid = sb_project(sb)
    payment = create(sb, pid).json()
    assert send(sb, pid, payment["id"], {"confirm_recipient_email": "other@example.org"}).status_code == 422
    assert fake.count("POST", "/send") == 0
    ok = send(sb, pid, payment["id"])
    assert ok.status_code == 200 and (ok.json()["provider_status"], ok.json()["invoice_state"]) == ("PENDING", "SENT")
    assert fake.count("POST", "/send") == 1 and project_status(sb, pid) == "INVOICED"
    assert send(sb, pid, payment["id"]).status_code == 409 and fake.count("POST", "/send") == 1


def test_send_authorization_and_cross_seller_protection(sandbox_seller_factory, fake):
    owner, other = sandbox_seller_factory("owner@example.com"), sandbox_seller_factory("other@example.com")
    pid = sb_project(owner)
    payment = create(owner, pid).json()
    assert send(other, pid, payment["id"]).status_code == 404
    assert sync(other, pid).status_code == 404
    assert other.get(f"/api/v1/projects/{pid}/payments").status_code == 404
    assert other.get(f"/api/v1/projects/{pid}/payments/{payment['id']}/events").status_code == 404
    other_project = sb_project(other)
    assert send(other, other_project, payment["id"]).status_code == 404  # payment id of someone else's project
    owner.csrf = None
    assert send(owner, pid, payment["id"]).status_code == 403
    assert fake.count("POST", "/send") == 0 and fake.only()["status"] == "DRAFT"


def test_send_timeout_is_uncertain_and_sync_learns_the_truth(sb, fake):
    pid = sb_project(sb)
    payment = create(sb, pid).json()
    fake.fault("POST", "/send", "timeout")
    failed = send(sb, pid, payment["id"])
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "provider_outcome_uncertain"
    assert fake.count("POST", "/send") == 1  # not retried blindly
    assert send(sb, pid, payment["id"]).status_code == 409  # blocked until the outcome is known
    fake.only()["status"] = "SENT"  # the provider had actually sent it
    body = sync(sb, pid).json()[0]
    assert (body["provider_status"], body["operation_state"], body["invoice_state"]) == ("PENDING", "READY", "SENT")


def test_send_rejected_by_the_provider_leaves_the_invoice_sendable(sb, fake):
    pid = sb_project(sb)
    payment = create(sb, pid).json()
    fake.fault("POST", "/send", 422)
    rejected = send(sb, pid, payment["id"])
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "provider_rejected"
    assert send(sb, pid, payment["id"]).status_code == 200


@pytest.mark.parametrize("status", [406, 415])
def test_send_406_is_never_assumed_to_have_had_no_effect(sb, fake, status):
    pid = sb_project(sb)
    payment = create(sb, pid).json()
    fake.fault("POST", "/send", status)
    failed = send(sb, pid, payment["id"])
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "provider_outcome_uncertain"
    assert send(sb, pid, payment["id"]).status_code == 409 and fake.count("POST", "/send") == 1  # no blind retry
    shown = sb.get(f"/api/v1/projects/{pid}/payments").json()[0]
    assert shown["operation_state"] == "SEND_UNCERTAIN" and shown["provider_status"] == "UNVERIFIED"  # nothing advanced, nothing assumed
    fake.only()["status"] = "SENT"  # the provider processed it despite the error status
    body = sync(sb, pid).json()[0]
    assert (body["provider_status"], body["operation_state"], body["invoice_state"]) == ("PENDING", "READY", "SENT")


def test_send_406_followed_by_a_draft_read_stays_uncertain_until_the_settle_window_passes(sb, fake, monkeypatch):
    from datetime import timedelta
    pid = sb_project(sb)
    payment = create(sb, pid).json()
    fake.fault("POST", "/send", 406)
    assert send(sb, pid, payment["id"]).status_code == 502
    body = sync(sb, pid).json()[0]  # an immediate DRAFT read does not prove the send failed (it may still be in flight)
    assert (body["provider_status"], body["invoice_state"], body["amount_paid_minor"]) == ("UNVERIFIED", "DRAFT", 0)
    assert body["operation_state"] == "SEND_UNCERTAIN" and send(sb, pid, payment["id"]).status_code == 409
    monkeypatch.setattr("app.services.reconcile.SEND_SETTLE_AFTER", timedelta(0))  # the settle window has passed and the provider still says DRAFT
    assert sync(sb, pid).json()[0]["operation_state"] == "READY" and fake.count("POST", "/send") == 1


# --- 7/8/9/10. reconciliation -------------------------------------------------------------------------

def test_verified_payment_marks_paid_with_the_paypal_transaction_id(sb, fake):
    pid = sb_project(sb)
    payment = sent(sb, pid)
    assert sync(sb, pid).json()[0]["provider_status"] == "PENDING"
    fake.pay(fake.only())
    body = sync(sb, pid).json()[0]
    assert (body["provider_status"], body["amount_paid_minor"], body["invoice_state"]) == ("PAID", 80000, "PAID")
    assert body["paypal_transaction_id"].startswith("PAY-") and project_status(sb, pid) == "PAID"
    assert events(sb, pid, payment["id"])[-1]["to_status"] == "PAID"


def test_get_endpoints_do_not_call_paypal(sb, fake):
    pid = sb_project(sb)
    payment = sent(sb, pid)
    fake.pay(fake.only())
    calls = len(fake.calls)
    assert payments(sb, pid)[0]["provider_status"] == "PENDING"
    sb.get(f"/api/v1/projects/{pid}")
    events(sb, pid, payment["id"])
    assert len(fake.calls) == calls


@pytest.mark.parametrize("tamper", ["amount", "currency", "recipient", "merchant", "invoice_number", "no_recipient", "no_merchant"])
def test_mismatching_provider_evidence_is_never_accepted_as_paid(sb, fake, tamper):
    pid = sb_project(sb)
    sent(sb, pid)
    invoice_data = fake.only()
    fake.pay(invoice_data)
    if tamper == "amount":
        invoice_data["amount"]["value"] = "799.99"
    elif tamper == "currency":
        invoice_data["amount"]["currency_code"] = "EUR"
        invoice_data["payments"]["transactions"][0]["amount"]["currency_code"] = "EUR"
    elif tamper == "recipient":
        invoice_data["primary_recipients"] = [{"billing_info": {"email_address": "attacker@example.net"}}]
    elif tamper == "no_recipient":
        invoice_data["primary_recipients"] = []
    elif tamper == "no_merchant":
        invoice_data.pop("invoicer")
    elif tamper == "merchant":
        invoice_data["invoicer"]["email_address"] = "someone-else@example.com"
    else:
        invoice_data["detail"]["invoice_number"] = "PEM-TAMPERED"
    body = sync(sb, pid).json()[0]
    assert body["provider_status"] == "NEEDS_REVIEW" and body["amount_paid_minor"] == 0
    assert body["paypal_transaction_id"] is None and project_status(sb, pid) == "INVOICED"


def test_payment_with_a_wrong_transaction_currency_fails_closed(sb, fake):
    pid = sb_project(sb)
    sent(sb, pid)
    fake.pay(fake.only())
    fake.only()["payments"]["transactions"][0]["amount"]["currency_code"] = "EUR"
    assert sync(sb, pid).json()[0]["provider_status"] == "NEEDS_REVIEW"


def test_marked_as_paid_is_not_proof_of_payment(sb, fake):
    pid = sb_project(sb)
    sent(sb, pid)
    fake.pay(fake.only(), kind="EXTERNAL", status="MARKED_AS_PAID")
    body = sync(sb, pid).json()[0]
    assert body["provider_status"] == "NEEDS_REVIEW" and "EXTERNALLY_MARKED_PAID" in body["verification_note"]
    assert body["amount_paid_minor"] == 0 and body["paypal_transaction_id"] is None
    assert project_status(sb, pid) == "INVOICED"


def test_paid_status_without_a_matching_paypal_payment_is_rejected(sb, fake):
    pid = sb_project(sb)
    sent(sb, pid)
    fake.only()["status"] = "PAID"  # status claims paid, but no transaction exists
    body = sync(sb, pid).json()[0]
    assert body["provider_status"] == "NEEDS_REVIEW" and "PAID_STATE_WITHOUT_MATCHING_PAYPAL_PAYMENTS" in body["verification_note"]


def test_payment_pending_and_partial_payment_are_not_paid(sb, fake):
    pid = sb_project(sb)
    sent(sb, pid)
    fake.only()["status"] = "PAYMENT_PENDING"
    body = sync(sb, pid).json()[0]
    assert body["provider_status"] == "PENDING" and body["verification_note"] == "PAYMENT_PENDING_AT_PROVIDER"
    fake.pay(fake.only(), amount_minor=30000)
    body = sync(sb, pid).json()[0]
    assert (body["provider_status"], body["amount_paid_minor"]) == ("PARTIALLY_PAID", 30000)
    assert project_status(sb, pid) == "INVOICED"
    fake.pay(fake.only(), amount_minor=50000)
    assert sync(sb, pid).json()[0]["provider_status"] == "PAID" and project_status(sb, pid) == "PAID"


def test_refund_transitions_keep_history_and_the_project_stays_paid(sb, fake):
    pid = sb_project(sb)
    payment = sent(sb, pid)
    fake.pay(fake.only())
    sync(sb, pid)
    fake.refund(fake.only(), 20000)
    assert sync(sb, pid).json()[0]["provider_status"] == "PARTIALLY_REFUNDED"
    fake.refund(fake.only(), 60000)
    body = sync(sb, pid).json()[0]
    assert (body["provider_status"], body["amount_refunded_minor"], body["amount_paid_minor"]) == ("REFUNDED", 80000, 80000)
    assert project_status(sb, pid) == "PAID"
    transitions = [(e["from_status"], e["to_status"]) for e in events(sb, pid, payment["id"]) if e["event_type"] == "STATUS_CHANGED"]
    assert transitions == [("PENDING", "PAID"), ("PAID", "PARTIALLY_REFUNDED"), ("PARTIALLY_REFUNDED", "REFUNDED")]
    calls = len(fake.calls)
    sync(sb, pid)  # a fully refunded invoice is final: no further provider traffic
    assert len(fake.calls) == calls


def test_replacement_after_a_provider_refund_creates_a_new_sandbox_invoice(sb, fake):
    pid = sb_project(sb)
    first = sent(sb, pid)
    fake.pay(fake.only())
    sync(sb, pid)
    fake.refund(fake.only())
    sync(sb, pid)
    assert create(sb, pid, key="invoice-key-0002").status_code == 409
    second = create(sb, pid, key="invoice-key-0002", body={**BUYER_INVOICE, "replaces_payment_id": first["id"]})
    assert second.status_code == 201 and len(fake.invoices) == 2
    assert [p["provider_status"] for p in payments(sb, pid)] == ["REFUNDED", "UNVERIFIED"]


def test_provider_cancellation_is_observed_by_sync(sb, fake):
    pid = sb_project(sb)
    sent(sb, pid)
    fake.only()["status"] = "CANCELLED"
    assert sync(sb, pid).json()[0]["provider_status"] == "CANCELLED" and project_status(sb, pid) == "DRAFT"


# --- 11. provider failures during sync -----------------------------------------------------------------

@pytest.mark.parametrize("fault", [("GET", "", 500, 500, 500), ("GET", "", "timeout", "timeout", "timeout"),
                                   ("GET", "", 429, 429, 429), ("GET", "", 404)])
def test_provider_failures_during_sync_leave_financial_state_unchanged(sb, fake, fault):
    pid = sb_project(sb)
    sent(sb, pid)
    fake.fault(*fault)
    assert sync(sb, pid).status_code >= 500
    body = payments(sb, pid)[0]
    assert body["provider_status"] == "PENDING" and "UNCHANGED" in body["verification_note"]
    assert body["amount_paid_minor"] == 0 and project_status(sb, pid) == "INVOICED"
    assert sync(sb, pid).json()[0]["verification_note"] is None  # recovers on the next sync


def test_oauth_failure_during_sync_leaves_financial_state_unchanged(sb, fake, clock):
    pid = sb_project(sb)
    sent(sb, pid)
    clock.t += 7200  # cached token expired, so sync must authenticate again
    fake.fault("POST", "/v1/oauth2/token", 401)
    assert sync(sb, pid).status_code == 502
    body = payments(sb, pid)[0]
    assert body["provider_status"] == "PENDING" and "AUTH_FAILED_STATUS_UNCHANGED" in body["verification_note"]
    assert sync(sb, pid).json()[0]["verification_note"] is None


def test_sync_after_failure_still_applies_a_real_payment(sb, fake):
    pid = sb_project(sb)
    sent(sb, pid)
    fake.pay(fake.only())
    fake.fault("GET", "", 500, 500, 500)
    assert sync(sb, pid).status_code == 502
    assert payments(sb, pid)[0]["provider_status"] == "PENDING"
    assert sync(sb, pid).json()[0]["provider_status"] == "PAID"


# --- 12. concurrency ---------------------------------------------------------------------------------------

def test_concurrent_sandbox_syncs_apply_the_payment_once(sandbox_app, fake):
    from tests.helpers import register
    first_client = TestClient(sandbox_app)
    register(first_client, "conc@example.com")
    one = login(first_client, "conc@example.com")
    two = login(TestClient(sandbox_app), "conc@example.com")
    pid = sb_project(one)
    payment = sent(one, pid)
    fake.pay(fake.only())
    results, barrier = [], threading.Barrier(2)

    def run(session):
        barrier.wait()
        results.append(sync(session, pid))
    threads = [threading.Thread(target=run, args=(s,)) for s in (one, two)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert payments(one, pid)[0]["provider_status"] == "PAID"
    changes = [e for e in events(one, pid, payment["id"]) if e["event_type"] == "STATUS_CHANGED" and e["to_status"] == "PAID"]
    assert len(changes) == 1
    with DbSession(sandbox_app.state.engine) as db:
        assert db.scalars(select(PaymentRecord)).one().amount_paid_minor == 80000


# --- 13. no credential or token exposure --------------------------------------------------------------------

def test_no_secret_or_token_appears_in_responses_database_or_logs(sb, fake, sandbox_app, caplog):
    caplog.set_level(logging.DEBUG)
    pid = sb_project(sb)
    responses = []
    payment = create(sb, pid)
    responses.append(payment)
    responses.append(send(sb, pid, payment.json()["id"]))
    fake.pay(fake.only())
    responses.append(sync(sb, pid))
    responses.append(sb.get(f"/api/v1/projects/{pid}"))
    responses.append(sb.get(f"/api/v1/projects/{pid}/payments/{payment.json()['id']}/events"))
    responses.append(TestClient(sandbox_app).get("/health"))
    token = fake.valid_token
    blob = "".join(r.text + str(dict(r.headers)) for r in responses)
    with DbSession(sandbox_app.state.engine) as db:
        blob += repr([vars(r) for r in db.scalars(select(PaymentRecord))]) + repr([vars(e) for e in db.scalars(select(PaymentEvent))])
    blob += caplog.text
    for secret in (CLIENT_SECRET, token, SETTINGS_CREDENTIALS["paypal_client_id"], "Bearer fake-token"):
        assert secret not in blob
    assert CLIENT_SECRET not in repr(sandbox_app.state.settings) and CLIENT_SECRET not in str(sandbox_app.state.settings)
    assert MERCHANT not in "".join(r.text for r in responses)  # merchant identity is not echoed to clients


def test_provider_errors_return_generic_messages(sb, fake):
    pid = sb_project(sb)
    fake.fault("POST", "/v2/invoicing/invoices", 500)
    body = create(sb, pid).text
    assert "INTERNAL_SERVER_ERROR" not in body and "SIMULATED" not in body and "Traceback" not in body


# --- 14. the sandbox path never touches the real network -------------------------------------------------

def test_every_provider_request_goes_through_the_fake_transport(sb, fake):
    pid = sb_project(sb)
    sent(sb, pid)
    fake.pay(fake.only())
    sync(sb, pid)
    assert fake.calls and all(path.startswith(("/v1/oauth2/", "/v2/invoicing/")) for _, path in fake.calls)


# --- explicit invoicer: end to end ----------------------------------------------------------------------------------------

def test_an_invoice_created_through_the_api_carries_and_returns_the_asserted_merchant(sb, fake):
    pid = sb_project(sb)
    response = create(sb, pid)
    assert response.status_code == 201
    assert fake.create_bodies[0]["invoicer"] == {"email_address": MERCHANT}
    assert fake.only()["invoicer"] == {"email_address": MERCHANT} and project_status(sb, pid) != "NEEDS_REVIEW"


@pytest.mark.parametrize("mode", ["omit", "empty", "other"])
def test_missing_or_different_merchant_evidence_still_needs_review(sb, fake, mode):
    fake.invoicer_mode = mode
    pid = sb_project(sb)
    response = create(sb, pid)
    assert response.status_code == 502 and response.json()["error"]["code"] == "provider_response_mismatch"
    assert [p["provider_status"] for p in payments(sb, pid)] == ["NEEDS_REVIEW"]  # kept for review, never accepted
    assert sync(sb, pid).json()[0]["provider_status"] == "NEEDS_REVIEW"             # and sync does not clear it
    assert "someone-else@example.com" not in response.text and MERCHANT not in response.text


def test_paypal_rejecting_the_invoicer_is_a_provider_rejection_and_makes_no_invoice(sb, fake):
    fake.merchant = "the-token-account@example.com"   # the configured merchant is not the account behind the token
    pid = sb_project(sb)
    response = create(sb, pid)
    assert response.status_code == 422 and response.json()["error"]["code"] == "provider_rejected"
    assert fake.invoices == {} and fake.count("POST", "/send") == 0
    assert MERCHANT not in response.text and "the-token-account@example.com" not in response.text
