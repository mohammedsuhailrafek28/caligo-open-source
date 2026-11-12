"""Stage 2D hardening: explicit audited review resolution, honest sync failures, clean shutdown."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.payment_provider import ProviderInvoiceState as P
from app.payment_provider import ProviderTransaction
from tests.fake_paypal import FakePayPal
from tests.helpers import login, register
from tests.test_payments import (
    INVOICE, events, invoice, make_project, payments, project_status, rows, sent_invoice, sync,
)
from tests.test_sandbox_flow import create, sb_project, sent

NOTE = "Checked the provider dashboard and the evidence matches the agreement."


def resolve(seller, pid, payment_id, resolution="ACCEPT_PROVIDER_EVIDENCE", note=NOTE, confirm=False):
    return seller.post(f"/api/v1/projects/{pid}/payments/{payment_id}/resolve-review",
                       json={"resolution": resolution, "note": note, "confirm_no_provider_invoice": confirm})


def into_review(app, seller):
    """A paid invoice whose provider total disagrees with ours -> NEEDS_REVIEW; then the provider 'fixes' itself."""
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    mock, iid = app.state.payment_provider, payment["paypal_invoice_id"]
    mock.simulate_payment(iid, amount_minor=79999)
    assert sync(seller, pid).json()[0]["provider_status"] == "NEEDS_REVIEW"
    current = mock._invoices[iid]
    mock._invoices[iid] = replace(current, state=P.PAID, total_minor=80000,
                                  payments=(ProviderTransaction("MOCK-TXN-CONSISTENT", "PAYPAL", 80000, "USD"),))
    return pid, payment


# --- 1. NEEDS_REVIEW never clears silently --------------------------------------------------------------

def test_consistent_evidence_does_not_silently_clear_needs_review(app, seller):
    pid, payment = into_review(app, seller)
    for _ in range(2):
        body = sync(seller, pid).json()[0]
        assert body["provider_status"] == "NEEDS_REVIEW" and "AWAITING_RESOLUTION" in body["verification_note"]
        assert body["amount_paid_minor"] == 0 and body["paypal_transaction_id"] is None
    assert project_status(seller, pid) == "INVOICED"
    assert not [e for e in events(seller, pid, payment["id"]) if e["to_status"] == "PAID"]


def test_explicit_resolution_applies_verified_evidence_and_is_audited(app, seller):
    pid, payment = into_review(app, seller)
    response = resolve(seller, pid, payment["id"])
    assert response.status_code == 200
    body = response.json()
    assert (body["provider_status"], body["amount_paid_minor"]) == ("PAID", 80000)
    assert project_status(seller, pid) == "PAID"
    history = events(seller, pid, payment["id"])
    resolved = [e for e in history if e["event_type"] == "REVIEW_RESOLVED"]
    assert len(resolved) == 1 and resolved[0]["actor"] == "SELLER"
    assert resolved[0]["from_status"] == "NEEDS_REVIEW" and NOTE[:40] in resolved[0]["note"]
    assert any(e["from_status"] == "NEEDS_REVIEW" and e["to_status"] == "PAID" for e in history)


def test_resolution_is_refused_while_evidence_is_still_inconsistent(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"], amount_minor=79999)
    sync(seller, pid)
    refused = resolve(seller, pid, payment["id"])
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "review_evidence_inconsistent"
    assert payments(seller, pid)[0]["provider_status"] == "NEEDS_REVIEW"
    assert not [e for e in events(seller, pid, payment["id"]) if e["event_type"] == "REVIEW_RESOLVED"]


def test_resolution_input_ownership_and_state_are_enforced(app, seller_factory):
    owner, other = seller_factory("owner@example.com"), seller_factory("other@example.com")
    pid, payment = into_review(app, owner)
    assert resolve(other, pid, payment["id"]).status_code == 404
    assert resolve(owner, pid, payment["id"], note="too short").status_code == 422
    assert resolve(owner, pid, payment["id"], resolution="MARK_PAID").status_code == 422
    assert owner.post(f"/api/v1/projects/{pid}/payments/{payment['id']}/resolve-review",
                      json={"resolution": "ACCEPT_PROVIDER_EVIDENCE", "note": NOTE, "provider_status": "PAID"}).status_code == 422
    owner.csrf = None
    assert resolve(owner, pid, payment["id"]).status_code == 403
    assert payments(login(TestClient(app), "owner@example.com"), pid)[0]["provider_status"] == "NEEDS_REVIEW"


def test_only_records_in_review_can_be_resolved(app, seller):
    pid = make_project(seller)
    payment = sent_invoice(seller, pid)
    refused = resolve(seller, pid, payment["id"])
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_in_review"


def test_a_stuck_review_without_a_provider_invoice_can_be_abandoned_with_an_audit_trail(app, seller, monkeypatch):
    from app.payment_provider import ProviderRejected
    pid = make_project(seller)
    mock = app.state.payment_provider
    monkeypatch.setattr(mock, "create_invoice", lambda _r: (_ for _ in ()).throw(RuntimeError("lost")))
    assert invoice(seller, pid).status_code == 502
    monkeypatch.setattr(mock, "create_invoice", lambda _r: (_ for _ in ()).throw(ProviderRejected("ODD")))
    assert invoice(seller, pid).status_code == 502
    stuck = rows(app)[0]
    assert stuck.provider_status == "NEEDS_REVIEW" and stuck.paypal_invoice_id is None
    assert resolve(seller, pid, stuck.id).status_code == 409  # nothing at the provider to accept
    done = resolve(seller, pid, stuck.id, "ABANDON_UNCREATED_INVOICE", "Confirmed in the sandbox dashboard that no invoice exists.",
                   confirm=True)
    assert done.status_code == 200 and done.json()["provider_status"] == "CANCELLED"
    assert [e["event_type"] for e in events(seller, pid, stuck.id)][-2:] == ["STATUS_CHANGED", "REVIEW_RESOLVED"] \
        or any(e["event_type"] == "REVIEW_RESOLVED" for e in events(seller, pid, stuck.id))
    monkeypatch.undo()
    fresh = invoice(seller, pid, key="invoice-key-0002")
    assert fresh.status_code == 201 and len(payments(seller, pid)) == 2  # history preserved


def test_abandon_is_refused_when_a_provider_invoice_exists(app, seller):
    pid, payment = into_review(app, seller)
    refused = resolve(seller, pid, payment["id"], "ABANDON_UNCREATED_INVOICE", "I believe it was never created at all.",
                      confirm=True)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "abandon_not_applicable"


def test_sandbox_review_resolution_uses_a_fresh_provider_fetch(sb, fake):
    pid = sb_project(sb)
    payment = sent(sb, pid)
    fake.pay(fake.only())
    fake.only()["amount"]["value"] = "799.99"
    assert sync(sb, pid).json()[0]["provider_status"] == "NEEDS_REVIEW"
    fake.only()["amount"]["value"] = "800.00"
    assert sync(sb, pid).json()[0]["provider_status"] == "NEEDS_REVIEW"  # still not silent
    gets = fake.count("GET", fake.only()["id"])
    done = resolve(sb, pid, payment["id"])
    assert done.status_code == 200 and done.json()["provider_status"] == "PAID"
    assert fake.count("GET", fake.only()["id"]) == gets + 1


# --- 2. provider synchronization failures are not a successful 200 ------------------------------------------

@pytest.mark.parametrize("fault,status,code", [
    (("GET", "", 500, 500, 500), 502, "provider_unavailable"),
    (("GET", "", "timeout", "timeout", "timeout"), 502, "provider_unavailable"),
    (("GET", "", 429, 429, 429), 503, "provider_rate_limited"),
    (("GET", "", 404), 502, "provider_sync_failed"),
])
def test_sync_provider_failure_is_an_error_status_with_details_and_persisted_notes(sb, fake, fault, status, code):
    pid = sb_project(sb)
    payment = sent(sb, pid)
    fake.fault(*fault)
    response = sync(sb, pid)
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code and error["details"][0]["payment_id"] == payment["id"]
    assert "Traceback" not in response.text
    if status == 503:
        assert response.headers["Retry-After"]
    stored = payments(sb, pid)[0]  # state is unchanged and the failure is visible without another provider call
    assert stored["provider_status"] == "PENDING" and "UNCHANGED" in stored["verification_note"]
    assert sync(sb, pid).status_code == 200  # recovers


def test_sync_auth_failure_is_reported_as_such(sb, fake, clock):
    pid = sb_project(sb)
    sent(sb, pid)
    clock.t += 7200
    fake.fault("POST", "/v1/oauth2/token", 401)
    response = sync(sb, pid)
    assert response.status_code == 502 and response.json()["error"]["code"] == "provider_auth_failed"


def test_sync_still_reconciles_other_records_when_one_fails(app, seller, monkeypatch):
    pid = make_project(seller)
    first = sent_invoice(seller, pid)
    mock = app.state.payment_provider
    mock.simulate_state(first["paypal_invoice_id"], P.CANCELLED)
    sync(seller, pid)
    second = sent_invoice(seller, pid, key="invoice-key-0002", body={**INVOICE, "replaces_payment_id": first["id"]})
    real = mock.get_invoice

    def flaky(invoice_id):
        if invoice_id == first["paypal_invoice_id"]:
            raise RuntimeError("down")
        return real(invoice_id)
    monkeypatch.setattr(mock, "get_invoice", flaky)
    mock.simulate_payment(second["paypal_invoice_id"])
    response = sync(seller, pid)
    assert response.status_code == 502 and [d["payment_id"] for d in response.json()["error"]["details"]] == [first["id"]]
    assert {p["id"]: p["provider_status"] for p in payments(seller, pid)}[second["id"]] == "PAID"


# --- 3. clean shutdown -------------------------------------------------------------------------------------------

def test_shutdown_closes_the_paypal_http_client_and_disposes_the_engine(sandbox_app, sandbox_provider, monkeypatch):
    disposed = []
    monkeypatch.setattr(sandbox_app.state.engine, "dispose", lambda *a, **k: disposed.append(1))
    with TestClient(sandbox_app) as client:
        assert client.get("/health").status_code == 200
        assert not sandbox_provider._client.is_closed
    assert sandbox_provider._client.is_closed and disposed == [1]


def test_shutdown_tolerates_providers_without_a_close_method(app):
    with TestClient(app) as client:  # the in-process mock has nothing to close
        assert client.get("/health").status_code == 200


def test_shutdown_survives_a_failing_close(sandbox_app, sandbox_provider, monkeypatch):
    monkeypatch.setattr(sandbox_provider, "close", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(sandbox_app) as client:
        client.get("/health")


# --- review-driven hardening (Stage 2D security review) -------------------------------------------------------

def _stuck_review(app, seller, monkeypatch):
    from app.payment_provider import ProviderRejected
    pid = make_project(seller)
    mock = app.state.payment_provider
    monkeypatch.setattr(mock, "create_invoice", lambda _r: (_ for _ in ()).throw(RuntimeError("lost")))
    invoice(seller, pid)
    monkeypatch.setattr(mock, "create_invoice", lambda _r: (_ for _ in ()).throw(ProviderRejected("ODD")))
    invoice(seller, pid)
    return pid, rows(app)[0]


ABANDON_NOTE = "Confirmed in the sandbox dashboard that no invoice exists."


def test_abandon_requires_an_explicit_attestation(app, seller, monkeypatch):
    pid, stuck = _stuck_review(app, seller, monkeypatch)
    refused = resolve(seller, pid, stuck.id, "ABANDON_UNCREATED_INVOICE", ABANDON_NOTE, confirm=False)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "abandon_confirmation_required"
    assert rows(app)[0].provider_status == "NEEDS_REVIEW"


def test_abandon_is_refused_while_a_provider_operation_is_in_flight(app, seller, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.orm import Session as DbSession
    pid, stuck = _stuck_review(app, seller, monkeypatch)
    with DbSession(app.state.engine) as db:
        db.execute(text("UPDATE payment_records SET operation_state='CREATING' WHERE id=:i"), {"i": stuck.id})
        db.commit()
    refused = resolve(seller, pid, stuck.id, "ABANDON_UNCREATED_INVOICE", ABANDON_NOTE, confirm=True)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "operation_in_progress"


def test_replaying_a_create_that_is_in_review_does_not_resume_it(app, seller, monkeypatch):
    pid, stuck = _stuck_review(app, seller, monkeypatch)
    monkeypatch.undo()
    replay = invoice(seller, pid)
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "review_required"
    assert len(app.state.payment_provider._invoices) == 0


def test_a_declined_acceptance_is_not_recorded_as_resolved(app, seller):
    from sqlalchemy import text
    from sqlalchemy.orm import Session as DbSession
    pid, payment = into_review(app, seller)
    with DbSession(app.state.engine) as db:  # a different transaction id was already recorded
        db.execute(text("UPDATE payment_records SET paypal_transaction_id='MOCK-TXN-OLD' WHERE id=:i"), {"i": payment["id"]})
        db.commit()
    refused = resolve(seller, pid, payment["id"])
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "review_not_resolved"
    assert payments(seller, pid)[0]["provider_status"] == "NEEDS_REVIEW"
    assert not [e for e in events(seller, pid, payment["id"]) if e["event_type"] == "REVIEW_RESOLVED"]
    assert any("TRANSACTION_ID_CHANGED_REVIEW" in (e["note"] or "") for e in events(seller, pid, payment["id"]))


def test_a_blank_reason_is_rejected(app, seller):
    pid, payment = into_review(app, seller)
    assert resolve(seller, pid, payment["id"], note=" " * 12).status_code == 422
