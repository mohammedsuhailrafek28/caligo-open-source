"""Slice 5: the reviewer-ready evidence report. Facts, seller claims and AI judgments are kept apart; output is deterministic."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models import AuditEvent
from app.services import scope as scope_service
from tests.helpers import login, register
from tests.test_analysis import HOME, FORM, artifact, run
from tests.test_buyer_approvals import BUYER, decide, open_session, request_approval, token_from, TOKEN_RE, outbox
from tests.test_scope_baselines import freeze, make_project, propose

INVOICE = {"currency": "USD", "amount_minor": 80000, "recipient_email": BUYER}


@pytest.fixture()
def rapp(make_app):
    return make_app(buyer_mail_mode="fake", ai_mode="demo")


@pytest.fixture()
def rseller(rapp):
    client = TestClient(rapp)
    assert register(client, "seller@example.com").status_code == 201
    return login(client, "seller@example.com")


@pytest.fixture()
def buyer(rapp):
    return TestClient(rapp)


@pytest.fixture()
def pid(rseller):
    pid = make_project(rseller)
    assert freeze(rseller, pid, reqs=[HOME, FORM]).status_code == 201
    return pid


def report(seller, pid):
    response = seller.get(f"/api/v1/projects/{pid}/evidence-report")
    assert response.status_code == 200, response.text
    return response.json()


def markdown(seller, pid):
    return seller.get(f"/api/v1/projects/{pid}/evidence-report/markdown")


def recomputed_hash(body):
    return scope_service.canonical_sha256({k: v for k, v in body.items() if k != "report_sha256"})


def review(rapp, seller, buyer, pid, art, decision, statement=None):
    seller.post(f"/api/v1/projects/{pid}/buyer-requests",
                json={"kind": "DELIVERY_REVIEW", "buyer_email": BUYER, "artifact_ids": [art["id"]]})
    token = TOKEN_RE.search([m for m in outbox(rapp) if "token=" in m.body][-1].body).group(1)
    return decide(buyer, open_session(rapp, buyer, token)["session_token"], decision, statement).json()["decision"]


# --- access ------------------------------------------------------------------------------------------------------

def test_the_report_needs_authentication_and_ownership(rapp, rseller, pid):
    assert TestClient(rapp).get(f"/api/v1/projects/{pid}/evidence-report").status_code == 401
    other_client = TestClient(rapp)
    register(other_client, "other@example.com")
    other = login(other_client, "other@example.com")
    assert other.get(f"/api/v1/projects/{pid}/evidence-report").status_code == 404
    assert other.get(f"/api/v1/projects/{pid}/evidence-report/markdown").status_code == 404


def test_a_report_needs_a_frozen_agreement(rseller):
    pid = make_project(rseller)
    response = rseller.get(f"/api/v1/projects/{pid}/evidence-report")
    assert response.status_code == 409 and response.json()["error"]["code"] == "baseline_required"


def test_generating_a_report_changes_nothing(rapp, rseller, pid):
    def count():
        with DbSession(rapp.state.engine) as db:
            return db.scalar(select(func.count()).select_from(AuditEvent))

    before = count()
    report(rseller, pid)
    markdown(rseller, pid)
    assert count() == before


# --- determinism and integrity ---------------------------------------------------------------------------------------

def test_the_report_is_deterministic_and_carries_a_verifiable_hash(rseller, pid):
    artifact(rseller, pid, content_text="Responsive home page with hero section and navigation menu.", requirement_keys=["R1"])
    first, second = report(rseller, pid), report(rseller, pid)
    assert first == second and first["report_sha256"] == recomputed_hash(first)
    assert markdown(rseller, pid).text == markdown(rseller, pid).text


def test_new_evidence_changes_the_report_hash(rseller, pid):
    before = report(rseller, pid)["report_sha256"]
    artifact(rseller, pid, content_text="anything", requirement_keys=["R1"])
    assert report(rseller, pid)["report_sha256"] != before


def test_report_contains_no_secrets_tokens_full_buyer_addresses_or_raw_artifact_content(rapp, rseller, buyer, pid):
    change = propose(rseller, pid).json()
    request_approval(rseller, pid, change["id"])
    token = token_from(rapp)
    session = open_session(rapp, buyer, token)["session_token"]
    decide(buyer, session, "APPROVE_CHANGE", "Fine by me.")
    artifact(rseller, pid, content_text="SECRET-BODY-TEXT unique marker", requirement_keys=["R1"])
    text = json.dumps(report(rseller, pid)) + markdown(rseller, pid).text
    for forbidden in (token, session, BUYER, "SECRET-BODY-TEXT", "token_hash", "session_hash", "challenge_hash"):
        assert forbidden not in text
    assert "b***@example.org" in text


# --- facts, claims and judgments stay separate --------------------------------------------------------------------------

def test_the_report_is_labelled_synthetic_evidence_not_legal_proof(rseller, pid):
    body = report(rseller, pid)
    assert body["project"]["id"] == pid and body["limitations"]
    assert "not legal advice" in body["disclaimer"].lower() and "reviewer" in body["disclaimer"].lower()


def test_seller_asserted_items_are_claims_never_confirmed_observations(rseller, pid):
    art = artifact(rseller, pid, content_text="x", requirement_keys=["R1"], asserted_at="2020-01-01T00:00:00Z")
    rseller.post(f"/api/v1/projects/{pid}/scope-records", json={"kind": "APPROVAL", "source_label": "Chat", "content": "Client said yes."})
    body = report(rseller, pid)
    claims = body["seller_claims"]
    kinds = {c["type"] for c in claims}
    assert {"ARTIFACT_REQUIREMENT_LINK", "ASSERTED_TIME", "APPROVAL_NOTE"} <= kinds
    assert all(c["basis"] == "SELLER_ASSERTED" and c["verified"] is False for c in claims)
    observed = json.dumps(body["confirmed_observations"])
    assert "Client said yes" not in observed and "2020-01-01" not in observed
    assert all(o["basis"] == "SERVER_RECORDED" and o["refs"] for o in body["confirmed_observations"])
    assert any(art["sha256"] in json.dumps(o) for o in body["confirmed_observations"])


def test_ai_judgments_are_separate_labelled_and_absent_until_an_analysis_is_run(rseller, pid):
    assert report(rseller, pid)["ai_judgments"] is None
    run(rseller, pid)
    judgments = report(rseller, pid)["ai_judgments"]
    assert judgments["production_grade"] is False and "NOT PRODUCTION" in judgments["label"].upper()
    assert judgments["human_review_required"] is True and judgments["findings"]
    assert judgments["input_sha256"] and judgments["analysis_run_id"]


def test_only_the_latest_analysis_run_is_reported(rseller, pid):
    run(rseller, pid)
    latest = run(rseller, pid).json()
    assert report(rseller, pid)["ai_judgments"]["analysis_run_id"] == latest["id"]


def test_unresolved_matters_are_listed_not_hidden(rseller, pid):
    propose(rseller, pid)
    artifact(rseller, pid, declared_sha256="b" * 64, requirement_keys=["R1"])
    items = {u["type"]: u for u in report(rseller, pid)["unresolved"]}
    assert {"REQUIREMENT_WITHOUT_VERIFIED_EVIDENCE", "CHANGE_AWAITING_BUYER", "ARTIFACT_NOT_HASHED_BY_SERVER",
            "NO_BUYER_DELIVERY_DECISION"} <= set(items)
    assert all(u["refs"] for u in report(rseller, pid)["unresolved"])


def test_requirement_mapping_shows_unevidenced_requirements(rseller, pid):
    artifact(rseller, pid, content_text="x", requirement_keys=["R1"])
    mapping = {m["key"]: m for m in report(rseller, pid)["requirement_mapping"]}
    assert len(mapping["R1"]["artifacts"]) == 1 and mapping["R2"]["artifacts"] == []
    assert mapping["R1"]["artifacts"][0]["link_basis"] == "SELLER_ASSERTED"


# --- changes, buyer evidence and payments -----------------------------------------------------------------------------------

def test_a_pending_and_an_approved_change_are_distinguished(rapp, rseller, buyer, pid):
    pending = propose(rseller, pid, title="Pending extra").json()
    request_approval(rseller, pid, pending["id"])
    decide(buyer, open_session(rapp, buyer, token_from(rapp))["session_token"], "APPROVE_CHANGE")
    other = propose(rseller, pid, reqs=[{"key": "R9", "title": "Extra feature", "description": "Loyalty points."}], title="Unapproved extra").json()
    changes = {c["id"]: c for c in report(rseller, pid)["changes"]}
    assert changes[pending["id"]]["status"] == "APPROVED" and changes[pending["id"]]["authorization"] == "VERIFIED_BUYER_APPROVAL"
    assert changes[pending["id"]]["buyer_decision_id"]
    assert changes[other["id"]]["status"] == "PROPOSED" and changes[other["id"]]["authorization"] == "NONE"
    assert [b["version"] for b in report(rseller, pid)["agreement"]["baselines"]] == [1, 2]


def test_buyer_evidence_states_what_a_decision_does_and_does_not_prove(rapp, rseller, buyer, pid):
    art = artifact(rseller, pid, content_text="x", requirement_keys=["R1"])
    decision = review(rapp, rseller, buyer, pid, art, "ACKNOWLEDGE_DELIVERY")
    evidence = report(rseller, pid)["buyer_evidence"]
    assert evidence["delivery_status"] == "ACKNOWLEDGED_ONLY"
    row = evidence["decisions"][0]
    assert row["id"] == decision["id"] and row["verification_method"] == "EMAIL_OTP" and row["buyer_email_masked"] == "b***@example.org"
    assert "mailbox" in evidence["caveat"].lower() and "not legal identity" in evidence["caveat"].lower()
    assert row["artifact_bindings"][0]["sha256"] == art["sha256"]


@pytest.mark.parametrize("decision,status", [("ACCEPT_DELIVERY", "ACCEPTED"), ("DISPUTE_DELIVERY", "DISPUTED")])
def test_delivery_status_follows_the_latest_buyer_decision(rapp, rseller, buyer, pid, decision, status):
    art = artifact(rseller, pid, content_text="x", requirement_keys=["R1"])
    review(rapp, rseller, buyer, pid, art, decision)
    assert report(rseller, pid)["buyer_evidence"]["delivery_status"] == status
    assert report(rseller, pid)["buyer_evidence"]["delivery_status"] != "NONE"


def test_a_seller_note_never_appears_as_buyer_evidence(rseller, pid):
    rseller.post(f"/api/v1/projects/{pid}/scope-records", json={"kind": "APPROVAL", "source_label": "Chat", "content": "They agreed."})
    evidence = report(rseller, pid)["buyer_evidence"]
    assert evidence["decisions"] == [] and evidence["delivery_status"] == "NONE"


def test_mock_payments_are_labelled_as_not_real_money(rseller, pid):
    created = rseller.post(f"/api/v1/projects/{pid}/invoices", json=INVOICE, headers={"Idempotency-Key": "report-key-0001"})
    assert created.status_code == 201, created.text
    payments = report(rseller, pid)["payments"]
    assert len(payments["records"]) == 1
    record = payments["records"][0]
    assert record["provider"] == "MOCK_PAYPAL" and record["is_simulated"] is True and record["amount_paid_minor"] == 0
    assert "no real" in payments["note"].lower()
    assert "SIMULATED" in markdown(rseller, pid).text


def test_without_any_invoice_payments_are_reported_as_none(rseller, pid):
    assert report(rseller, pid)["payments"]["records"] == []


# --- timeline and markdown -----------------------------------------------------------------------------------------------------

def test_the_timeline_is_server_ordered_and_included(rseller, pid):
    artifact(rseller, pid, content_text="x", requirement_keys=["R1"])
    timeline = report(rseller, pid)["timeline"]
    assert timeline and [t["at"] for t in timeline] == sorted(t["at"] for t in timeline)
    assert {t["time_basis"] for t in timeline} == {"SERVER_RECORDED"}


def test_markdown_renders_every_section_with_the_same_hash(rseller, pid):
    run(rseller, pid)
    response = markdown(rseller, pid)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/markdown")
    text = response.text
    for heading in ("# Evidence report", "## Agreement", "## Changes", "## Requirement mapping", "## Timeline",
                    "## Confirmed observations", "## Seller claims", "## AI judgments", "## Buyer evidence", "## Payments",
                    "## Unresolved", "## Limitations"):
        assert heading in text
    assert report(rseller, pid)["report_sha256"] in text and "NOT PRODUCTION" in text.upper()


def test_markdown_escapes_user_supplied_text(rseller):
    pid = make_project(rseller, "Evil <script>alert(1)</script> | title")
    freeze(rseller, pid, reqs=[{"key": "R1", "title": "<b>Bold</b>", "description": "x"}])
    text = markdown(rseller, pid).text
    assert "<script>" not in text and "<b>" not in text


# --- security review follow-ups ---------------------------------------------------------------------------------------------------

def test_a_decision_from_the_sellers_own_email_domain_is_flagged_for_the_reviewer(rapp, rseller, buyer, pid):
    change = propose(rseller, pid).json()
    request_approval(rseller, pid, change["id"], email="colleague@example.com")      # seller is seller@example.com
    decide(buyer, open_session(rapp, buyer, token_from(rapp))["session_token"], "APPROVE_CHANGE")
    body = report(rseller, pid)
    assert body["buyer_evidence"]["decisions"][0]["shares_seller_email_domain"] is True
    assert "BUYER_SHARES_SELLER_DOMAIN" in {u["type"] for u in body["unresolved"]}


def test_a_decision_from_a_different_domain_is_not_flagged(rapp, rseller, buyer, pid):
    change = propose(rseller, pid).json()
    request_approval(rseller, pid, change["id"])
    decide(buyer, open_session(rapp, buyer, token_from(rapp))["session_token"], "APPROVE_CHANGE")
    body = report(rseller, pid)
    assert body["buyer_evidence"]["decisions"][0]["shares_seller_email_domain"] is False
    assert "BUYER_SHARES_SELLER_DOMAIN" not in {u["type"] for u in body["unresolved"]}


def test_a_change_decision_is_bound_to_the_exact_change_content_the_buyer_saw(rapp, rseller, buyer, pid):
    change = propose(rseller, pid).json()
    request_approval(rseller, pid, change["id"])
    decide(buyer, open_session(rapp, buyer, token_from(rapp))["session_token"], "APPROVE_CHANGE")
    expected = scope_service.canonical_sha256({"title": change["title"], "description": change["description"],
                                               "requirements": change["proposed_requirements"]})
    assert report(rseller, pid)["buyer_evidence"]["decisions"][0]["change_sha256"] == expected


def test_markdown_strips_control_and_direction_override_characters(rseller):
    pid = make_project(rseller, "Title‮evil\u0007 end")
    freeze(rseller, pid)
    text = markdown(rseller, pid).text
    assert "‮" not in text and "\u0007" not in text
