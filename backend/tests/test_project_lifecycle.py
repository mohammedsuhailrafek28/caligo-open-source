"""R0: delivery state is DERIVED from verified buyer decisions; the stored project status stays financial-only.

Why: `Project.status` is written only by payment reconciliation (DRAFT -> INVOICED -> PAID). Writing DELIVERED/DISPUTED into it would
either stop a later payment from ever reaching PAID (acceptance before payment) or erase the fact that payment was verified (dispute).
`delivery_status` is read-only, comes from the append-only buyer decisions and always reflects the buyer's LATEST delivery decision."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import login, register
from tests.test_buyer_approvals import BUYER, TOKEN_RE, decide, open_session, outbox
from tests.test_scope_baselines import freeze, make_project

APP_DIR = Path(__file__).resolve().parents[1] / "app"
INVOICE = {"currency": "USD", "amount_minor": 80000, "recipient_email": BUYER}


@pytest.fixture()
def lapp(make_app):
    return make_app(buyer_mail_mode="fake")


@pytest.fixture()
def lseller(lapp):
    client = TestClient(lapp)
    assert register(client, "seller@example.com").status_code == 201
    return login(client, "seller@example.com")


@pytest.fixture()
def buyer(lapp):
    return TestClient(lapp)


@pytest.fixture()
def pid(lseller):
    pid = make_project(lseller)
    assert freeze(lseller, pid).status_code == 201
    return pid


def detail(seller, pid):
    response = seller.get(f"/api/v1/projects/{pid}")
    assert response.status_code == 200, response.text
    return response.json()


def deliver_and_decide(app, seller, buyer, pid, decision):
    existing = len(seller.get(f"/api/v1/projects/{pid}/artifacts").json())      # unique content: the server rejects duplicate hashes
    artifact = seller.post(f"/api/v1/projects/{pid}/artifacts", json={"name": f"site-{existing}.zip", "content_text": f"build {existing}"}).json()
    seller.post(f"/api/v1/projects/{pid}/buyer-requests",
                json={"kind": "DELIVERY_REVIEW", "buyer_email": BUYER, "artifact_ids": [artifact["id"]]})
    token = TOKEN_RE.search([m for m in outbox(app) if "token=" in m.body][-1].body).group(1)
    return decide(buyer, open_session(app, buyer, token)["session_token"], decision)


def pay(app, seller, pid):
    created = seller.post(f"/api/v1/projects/{pid}/invoices", json=INVOICE, headers={"Idempotency-Key": f"life-key-{pid[:8]}"})
    assert created.status_code == 201, created.text
    app.state.payment_provider.simulate_payment(created.json()["paypal_invoice_id"])
    assert seller.post(f"/api/v1/projects/{pid}/payments/sync").status_code == 200


def test_a_new_project_has_no_delivery_status(lseller, pid):
    body = detail(lseller, pid)
    assert body["delivery_status"] == "NONE" and body["status"] == "DRAFT"


@pytest.mark.parametrize("decision,expected", [("ACKNOWLEDGE_DELIVERY", "ACKNOWLEDGED_ONLY"), ("ACCEPT_DELIVERY", "ACCEPTED"),
                                               ("DISPUTE_DELIVERY", "DISPUTED")])
def test_delivery_status_follows_the_verified_buyer_decision(lapp, lseller, buyer, pid, decision, expected):
    assert deliver_and_decide(lapp, lseller, buyer, pid, decision).status_code == 200
    assert detail(lseller, pid)["delivery_status"] == expected


def test_the_latest_delivery_decision_wins_in_both_directions(lapp, lseller, buyer, pid):
    deliver_and_decide(lapp, lseller, buyer, pid, "ACCEPT_DELIVERY")
    deliver_and_decide(lapp, lseller, buyer, pid, "DISPUTE_DELIVERY")
    assert detail(lseller, pid)["delivery_status"] == "DISPUTED"
    deliver_and_decide(lapp, lseller, buyer, pid, "ACCEPT_DELIVERY")
    assert detail(lseller, pid)["delivery_status"] == "ACCEPTED"


def test_a_seller_note_or_artifact_never_changes_delivery_status(lseller, pid):
    lseller.post(f"/api/v1/projects/{pid}/scope-records", json={"kind": "DELIVERY_NOTE", "source_label": "Chat", "content": "Client accepted."})
    lseller.post(f"/api/v1/projects/{pid}/artifacts", json={"name": "site.zip", "content_text": "x"})
    assert detail(lseller, pid)["delivery_status"] == "NONE"


def test_a_scope_change_decision_is_not_a_delivery_decision(lapp, lseller, buyer, pid):
    from tests.test_scope_baselines import propose
    from tests.test_buyer_approvals import request_approval, token_from

    change = propose(lseller, pid).json()
    request_approval(lseller, pid, change["id"])
    decide(buyer, open_session(lapp, buyer, token_from(lapp))["session_token"], "APPROVE_CHANGE")
    assert detail(lseller, pid)["delivery_status"] == "NONE"


# --- the stored status stays financial ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["ACKNOWLEDGE_DELIVERY", "ACCEPT_DELIVERY", "DISPUTE_DELIVERY"])
def test_delivery_decisions_never_change_a_verified_paid_status(lapp, lseller, buyer, pid, decision):
    pay(lapp, lseller, pid)
    assert detail(lseller, pid)["status"] == "PAID"
    deliver_and_decide(lapp, lseller, buyer, pid, decision)
    body = detail(lseller, pid)
    assert body["status"] == "PAID" and body["payments"][0]["provider_status"] == "PAID"


def test_acceptance_before_payment_does_not_block_a_later_verified_payment(lapp, lseller, buyer, pid):
    deliver_and_decide(lapp, lseller, buyer, pid, "ACCEPT_DELIVERY")
    assert detail(lseller, pid)["status"] == "DRAFT"
    pay(lapp, lseller, pid)
    body = detail(lseller, pid)
    assert body["status"] == "PAID" and body["delivery_status"] == "ACCEPTED"


def test_the_report_and_the_project_agree_on_delivery_status(lapp, lseller, buyer, pid):
    deliver_and_decide(lapp, lseller, buyer, pid, "DISPUTE_DELIVERY")
    report = lseller.get(f"/api/v1/projects/{pid}/evidence-report").json()
    assert report["buyer_evidence"]["delivery_status"] == detail(lseller, pid)["delivery_status"] == "DISPUTED"


def test_the_delivery_status_cannot_be_supplied_by_a_client(lseller):
    response = lseller.post("/api/v1/projects", json={"title": "x", "delivery_status": "ACCEPTED", "status": "DELIVERED"})
    created = lseller.get(f"/api/v1/projects/{response.json()['id']}").json() if response.status_code == 201 else None
    assert response.status_code == 422 or (created["delivery_status"] == "NONE" and created["status"] == "DRAFT")


def test_no_code_assigns_the_reserved_project_statuses():
    """DELIVERED and DISPUTED are reserved enum values. Assigning them needs a documented, tested transition (see docs/ARCHITECTURE.md)."""
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if path.name == "schemas.py":
            continue
        text = path.read_text(encoding="utf8")
        if "ProjectStatus.DELIVERED" in text or "ProjectStatus.DISPUTED" in text:
            offenders.append(path.name)
    assert offenders == []
