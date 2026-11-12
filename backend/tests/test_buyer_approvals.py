"""Slice 3: buyer acknowledgment and authorization. Only an email-verified buyer can approve; the seller cannot forge it."""
import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app import models
from app.config import Settings
from app.main import create_app
from app.models import AppendOnlyError, BuyerDecisionRecord, utcnow
from tests.helpers import login, register
from tests.test_scope_baselines import REQS, freeze, make_project, propose

BUYER = "buyer@example.org"
TOKEN_RE = re.compile(r"token=([A-Za-z0-9_-]{20,})")
CODE_RE = re.compile(r"code: (\d{6})")


@pytest.fixture()
def bapp(make_app):
    return make_app(buyer_mail_mode="fake")


@pytest.fixture()
def bseller(bapp):
    client = TestClient(bapp)
    assert register(client, "seller@example.com").status_code == 201
    return login(client, "seller@example.com")


@pytest.fixture()
def buyer_client(bapp):
    return TestClient(bapp)                      # no seller cookies: a different person in a different browser


@pytest.fixture()
def world(bseller):
    pid = make_project(bseller)
    freeze(bseller, pid)
    change = propose(bseller, pid).json()
    return {"pid": pid, "change": change}


def outbox(app):
    return app.state.mailer.outbox


def request_approval(seller, pid, change_id, email=BUYER):
    return seller.post(f"/api/v1/projects/{pid}/buyer-requests",
                       json={"kind": "SCOPE_CHANGE_APPROVAL", "buyer_email": email, "change_id": change_id})


def token_from(app):
    return TOKEN_RE.search(outbox(app)[-1].body).group(1)


def challenge(client, token):
    return client.post("/api/v1/buyer/challenge", json={"token": token})


def verify(client, token, code):
    return client.post("/api/v1/buyer/verify", json={"token": token, "code": code})


def decide(client, session, decision, statement=None):
    body = {"session_token": session, "decision": decision}
    if statement is not None:
        body["statement"] = statement
    return client.post("/api/v1/buyer/decide", json=body)


def open_session(app, client, token):
    assert challenge(client, token).status_code == 202
    code = CODE_RE.search(outbox(app)[-1].body).group(1)
    response = verify(client, token, code)
    assert response.status_code == 200, response.text
    return response.json()


def approve_flow(app, seller, client, world, decision="APPROVE_CHANGE", statement=None):
    assert request_approval(seller, world["pid"], world["change"]["id"]).status_code == 201
    session = open_session(app, client, token_from(app))["session_token"]
    return decide(client, session, decision, statement)


# --- the happy path: only here does the agreement change --------------------------------------------------------------------

def test_a_verified_buyer_approval_amends_the_agreement(bapp, bseller, buyer_client, world):
    response = approve_flow(bapp, bseller, buyer_client, world, statement="Yes, please add the blog.")
    assert response.status_code == 200
    body = response.json()
    assert body["scope_applied"] is True and body["decision"]["decision"] == "APPROVE_CHANGE"
    assert body["decision"]["verification_method"] == "EMAIL_OTP" and body["decision"]["statement"] == "Yes, please add the blog."
    pid = world["pid"]
    baselines = bseller.get(f"/api/v1/projects/{pid}/baselines").json()
    assert [b["version"] for b in baselines] == [1, 2] and "R3" in [r["key"] for r in baselines[1]["requirements"]]
    assert bseller.get(f"/api/v1/projects/{pid}/scope-changes").json()[0]["status"] == "APPROVED"
    audit = bseller.get(f"/api/v1/projects/{pid}/audit-events").json()
    assert {"BUYER_REQUEST_OPENED", "BUYER_DECISION_RECORDED", "CHANGE_APPROVED"} <= {e["event_type"] for e in audit}
    assert [e["actor_role"] for e in audit if e["event_type"] == "CHANGE_APPROVED"] == ["BUYER_EMAIL_VERIFIED"]


def test_a_rejection_leaves_the_agreement_unchanged(bapp, bseller, buyer_client, world):
    body = approve_flow(bapp, bseller, buyer_client, world, "REJECT_CHANGE").json()
    assert body["scope_applied"] is False
    pid = world["pid"]
    assert len(bseller.get(f"/api/v1/projects/{pid}/baselines").json()) == 1
    assert bseller.get(f"/api/v1/projects/{pid}/scope-changes").json()[0]["status"] == "REJECTED"


def test_the_decision_is_visible_to_the_seller_with_a_masked_buyer_address(bapp, bseller, buyer_client, world):
    approve_flow(bapp, bseller, buyer_client, world)
    decisions = bseller.get(f"/api/v1/projects/{world['pid']}/buyer-decisions").json()
    assert len(decisions) == 1 and decisions[0]["buyer_email_masked"] == "b***@example.org"
    assert BUYER not in json.dumps(decisions)


# --- the seller cannot forge buyer approval -----------------------------------------------------------------------------------

def test_nothing_the_seller_can_read_contains_the_link_token_or_the_code(bapp, bseller, buyer_client, world):
    created = request_approval(bseller, world["pid"], world["change"]["id"])
    token = token_from(bapp)
    assert created.status_code == 201 and token not in created.text
    challenge(buyer_client, token)
    code = CODE_RE.search(outbox(bapp)[-1].body).group(1)
    pid = world["pid"]
    seen = [created.text, bseller.get(f"/api/v1/projects/{pid}/buyer-requests").text,
            bseller.get(f"/api/v1/projects/{pid}/buyer-decisions").text, bseller.get(f"/api/v1/projects/{pid}/audit-events").text,
            bseller.get(f"/api/v1/projects/{pid}/timeline").text, bseller.get(f"/api/v1/projects/{pid}/scope-changes").text,
            bseller.get(f"/api/v1/projects/{pid}").text]
    for text in seen:
        assert token not in text and code not in text and "token_hash" not in text and "challenge_hash" not in text


def test_the_seller_has_no_route_that_records_or_applies_a_buyer_decision(bseller, world):
    pid, cid = world["pid"], world["change"]["id"]
    for path in (f"/api/v1/projects/{pid}/buyer-decisions", f"/api/v1/projects/{pid}/buyer-requests/x/decide",
                 f"/api/v1/projects/{pid}/scope-changes/{cid}/approve", "/api/v1/buyer/approve"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert bseller.client.request(method, path, headers=bseller.headers, json={"decision": "APPROVE_CHANGE"}).status_code in (404, 405)


def test_a_seller_note_of_type_approval_does_not_approve_anything(bseller, world):
    pid = world["pid"]
    bseller.post(f"/api/v1/projects/{pid}/scope-records",
                 json={"kind": "APPROVAL", "source_label": "Email", "content": "Client said yes to the blog."})
    assert bseller.get(f"/api/v1/projects/{pid}/scope-changes").json()[0]["status"] == "PROPOSED"
    assert len(bseller.get(f"/api/v1/projects/{pid}/baselines").json()) == 1


def test_guessing_tokens_codes_or_sessions_fails(bapp, bseller, buyer_client, world):
    request_approval(bseller, world["pid"], world["change"]["id"])
    for guess in ("x" * 43, "A" * 20, "../etc/passwd" + "a" * 10):
        assert challenge(buyer_client, guess).status_code == 202            # same answer as for a real token: no oracle
        assert verify(buyer_client, guess, "123456").status_code == 401
    assert len(outbox(bapp)) == 1                                           # only the seller's genuine request sent mail
    assert decide(buyer_client, "forged-session-token-" + "a" * 30, "APPROVE_CHANGE").status_code == 401


def test_the_sellers_own_session_cookie_gives_no_buyer_powers(bapp, bseller, world):
    request_approval(bseller, world["pid"], world["change"]["id"])
    assert decide(bseller.client, "some-made-up-session-token-value-0000", "APPROVE_CHANGE").status_code == 401
    assert bseller.get(f"/api/v1/projects/{world['pid']}/scope-changes").json()[0]["status"] == "PROPOSED"


def test_five_wrong_codes_lock_the_challenge_even_for_the_right_code(bapp, bseller, buyer_client, world):
    request_approval(bseller, world["pid"], world["change"]["id"])
    token = token_from(bapp)
    challenge(buyer_client, token)
    code = CODE_RE.search(outbox(bapp)[-1].body).group(1)
    wrong = "000000" if code != "000000" else "111111"
    assert [verify(buyer_client, token, wrong).status_code for _ in range(5)] == [401] * 5
    assert verify(buyer_client, token, code).status_code == 401
    challenge(buyer_client, token)                                          # a fresh challenge issues a new code
    fresh = CODE_RE.search(outbox(bapp)[-1].body).group(1)
    assert verify(buyer_client, token, fresh).status_code == 200


def test_a_session_only_works_for_its_own_request(bapp, bseller, buyer_client, world):
    pid = world["pid"]
    other = propose(bseller, pid, reqs=[{"key": "R7", "title": "Gallery", "description": "d"}]).json()
    request_approval(bseller, pid, world["change"]["id"])
    first = open_session(bapp, buyer_client, token_from(bapp))["session_token"]
    request_approval(bseller, pid, other["id"])
    open_session(bapp, buyer_client, token_from(bapp))
    assert decide(buyer_client, first, "APPROVE_CHANGE").status_code == 200
    states = {c["id"]: c["status"] for c in bseller.get(f"/api/v1/projects/{pid}/scope-changes").json()}
    assert states[world["change"]["id"]] == "APPROVED" and states[other["id"]] == "PROPOSED"


# --- expiry, single use, and request lifecycle ---------------------------------------------------------------------------------

def test_the_code_expires(bapp, bseller, buyer_client, world, monkeypatch):
    request_approval(bseller, world["pid"], world["change"]["id"])
    token = token_from(bapp)
    challenge(buyer_client, token)
    code = CODE_RE.search(outbox(bapp)[-1].body).group(1)
    later = utcnow() + timedelta(minutes=11)
    monkeypatch.setattr("app.services.buyer.utcnow", lambda: later)
    assert verify(buyer_client, token, code).status_code == 401


def test_the_session_expires(bapp, bseller, buyer_client, world, monkeypatch):
    request_approval(bseller, world["pid"], world["change"]["id"])
    session = open_session(bapp, buyer_client, token_from(bapp))["session_token"]
    later = utcnow() + timedelta(minutes=45)
    monkeypatch.setattr("app.services.buyer.utcnow", lambda: later)
    assert decide(buyer_client, session, "APPROVE_CHANGE").status_code == 401


def test_the_request_expires_after_its_lifetime(bapp, bseller, buyer_client, world, monkeypatch):
    request_approval(bseller, world["pid"], world["change"]["id"])
    token = token_from(bapp)
    monkeypatch.setattr("app.services.buyer.utcnow", lambda: utcnow() + timedelta(days=8))
    sent = len(outbox(bapp))
    assert challenge(buyer_client, token).status_code == 202 and len(outbox(bapp)) == sent


def test_a_decision_is_single_use(bapp, bseller, buyer_client, world):
    request_approval(bseller, world["pid"], world["change"]["id"])
    token = token_from(bapp)
    session = open_session(bapp, buyer_client, token)["session_token"]
    assert decide(buyer_client, session, "APPROVE_CHANGE").status_code == 200
    assert decide(buyer_client, session, "REJECT_CHANGE").status_code == 401
    sent = len(outbox(bapp))
    assert challenge(buyer_client, token).status_code == 202 and len(outbox(bapp)) == sent   # no new code for a decided request
    assert len(bseller.get(f"/api/v1/projects/{world['pid']}/buyer-decisions").json()) == 1


def test_a_new_request_cancels_the_old_pending_one(bapp, bseller, buyer_client, world):
    request_approval(bseller, world["pid"], world["change"]["id"])
    old = token_from(bapp)
    request_approval(bseller, world["pid"], world["change"]["id"])
    assert challenge(buyer_client, old).status_code == 202 and len(outbox(bapp)) == 2
    statuses = [r["status"] for r in bseller.get(f"/api/v1/projects/{world['pid']}/buyer-requests").json()]
    assert statuses == ["CANCELLED", "PENDING"]


def test_the_seller_can_cancel_a_pending_request(bapp, bseller, buyer_client, world):
    created = request_approval(bseller, world["pid"], world["change"]["id"]).json()
    token = token_from(bapp)
    cancelled = bseller.post(f"/api/v1/projects/{world['pid']}/buyer-requests/{created['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    challenge(buyer_client, token)
    assert len(outbox(bapp)) == 1
    assert bseller.post(f"/api/v1/projects/{world['pid']}/buyer-requests/{created['id']}/cancel").status_code == 409


def test_a_change_decided_elsewhere_cannot_be_approved_again(bapp, bseller, buyer_client, world):
    approve_flow(bapp, bseller, buyer_client, world)
    again = request_approval(bseller, world["pid"], world["change"]["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "change_not_pending"


# --- request validation ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"kind": "SCOPE_CHANGE_APPROVAL", "buyer_email": BUYER},
    {"kind": "SCOPE_CHANGE_APPROVAL", "buyer_email": "not-an-email", "change_id": "x"},
    {"kind": "DELIVERY_REVIEW", "buyer_email": BUYER},
    {"kind": "DELIVERY_REVIEW", "buyer_email": BUYER, "artifact_ids": ["a"], "change_id": "x"},
    {"kind": "SCOPE_CHANGE_APPROVAL", "buyer_email": BUYER, "change_id": "x", "artifact_ids": ["a"]},
    {"kind": "BOGUS", "buyer_email": BUYER, "change_id": "x"},
    {"kind": "SCOPE_CHANGE_APPROVAL", "buyer_email": BUYER, "change_id": "x", "status": "DECIDED"},
])
def test_invalid_requests_are_rejected(bseller, world, body):
    assert bseller.post(f"/api/v1/projects/{world['pid']}/buyer-requests", json=body).status_code == 422


def test_requests_for_unknown_or_foreign_subjects_are_not_found(bapp, bseller, world):
    pid = world["pid"]
    assert request_approval(bseller, pid, "no-such-change").status_code == 404
    other = make_project(bseller, "Other")
    freeze(bseller, other)
    assert request_approval(bseller, other, world["change"]["id"]).status_code == 404       # a change of another project
    delivery = bseller.post(f"/api/v1/projects/{pid}/buyer-requests",
                            json={"kind": "DELIVERY_REVIEW", "buyer_email": BUYER, "artifact_ids": ["nope"]})
    assert delivery.status_code == 404 and outbox(bapp) == []


def test_other_sellers_cannot_use_or_see_buyer_requests(bapp, bseller, world):
    other_client = TestClient(bapp)
    register(other_client, "other@example.com")
    other = login(other_client, "other@example.com")
    pid = world["pid"]
    assert request_approval(other, pid, world["change"]["id"]).status_code == 404
    for suffix in ("buyer-requests", "buyer-decisions"):
        assert other.get(f"/api/v1/projects/{pid}/{suffix}").status_code == 404
    request_approval(bseller, pid, world["change"]["id"])
    created = bseller.get(f"/api/v1/projects/{pid}/buyer-requests").json()[0]["id"]
    assert other.post(f"/api/v1/projects/{pid}/buyer-requests/{created}/cancel").status_code == 404


def test_seller_routes_need_authentication_and_csrf(bapp, bseller, world):
    anonymous = TestClient(bapp)
    path = f"/api/v1/projects/{world['pid']}/buyer-requests"
    body = {"kind": "SCOPE_CHANGE_APPROVAL", "buyer_email": BUYER, "change_id": world["change"]["id"]}
    assert anonymous.post(path, json=body).status_code == 401 and anonymous.get(path).status_code == 401
    assert bseller.client.post(path, json=body).status_code == 403
    assert outbox(bapp) == []


def test_the_buyer_sees_exactly_what_they_are_deciding_on(bapp, bseller, buyer_client, world):
    request_approval(bseller, world["pid"], world["change"]["id"])
    summary = open_session(bapp, buyer_client, token_from(bapp))
    assert summary["kind"] == "SCOPE_CHANGE_APPROVAL" and summary["allowed_decisions"] == ["APPROVE_CHANGE", "REJECT_CHANGE"]
    assert summary["subject"]["title"] == "Add a blog" and summary["subject"]["requirements"][0]["key"] == "R3"
    assert summary["project_title"] == "Website build"


# --- delivery: acknowledgment, acceptance and dispute are different things ------------------------------------------------------

@pytest.fixture()
def delivered(bseller, world):
    pid = world["pid"]
    artifact = bseller.post(f"/api/v1/projects/{pid}/artifacts",
                            json={"name": "site.zip", "content_text": "the site", "requirement_keys": ["R1"]}).json()
    return {**world, "artifact": artifact}


def review_request(seller, delivered):
    return seller.post(f"/api/v1/projects/{delivered['pid']}/buyer-requests",
                       json={"kind": "DELIVERY_REVIEW", "buyer_email": BUYER, "artifact_ids": [delivered["artifact"]["id"]]})


@pytest.mark.parametrize("decision,event", [("ACKNOWLEDGE_DELIVERY", "DELIVERY_ACKNOWLEDGED"), ("ACCEPT_DELIVERY", "DELIVERY_ACCEPTED"),
                                            ("DISPUTE_DELIVERY", "DELIVERY_DISPUTED")])
def test_delivery_decisions_are_distinct_and_bind_the_artifact_hashes(bapp, bseller, buyer_client, delivered, decision, event):
    assert review_request(bseller, delivered).status_code == 201
    session = open_session(bapp, buyer_client, token_from(bapp))
    assert session["allowed_decisions"] == ["ACKNOWLEDGE_DELIVERY", "ACCEPT_DELIVERY", "DISPUTE_DELIVERY"]
    assert session["subject"]["artifacts"][0]["sha256"] == delivered["artifact"]["sha256"]
    response = decide(buyer_client, session["session_token"], decision, "Looks fine." if "ACCEPT" in decision else None)
    assert response.status_code == 200 and response.json()["scope_applied"] is None
    bound = response.json()["decision"]["artifact_bindings"]
    assert bound == [{"artifact_id": delivered["artifact"]["id"], "version": 1, "sha256": delivered["artifact"]["sha256"]}]
    events = [e["event_type"] for e in bseller.get(f"/api/v1/projects/{delivered['pid']}/audit-events").json()]
    assert event in events and {"DELIVERY_ACKNOWLEDGED", "DELIVERY_ACCEPTED", "DELIVERY_DISPUTED"} & set(events) == {event}
    assert bseller.get(f"/api/v1/projects/{delivered['pid']}").json()["status"] == "DRAFT"     # a decision never edits money or status


def test_a_decision_must_match_the_request_kind(bapp, bseller, buyer_client, delivered, world):
    review_request(bseller, delivered)
    session = open_session(bapp, buyer_client, token_from(bapp))["session_token"]
    wrong = decide(buyer_client, session, "APPROVE_CHANGE")
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "invalid_decision"
    assert decide(buyer_client, session, "ACKNOWLEDGE_DELIVERY").status_code == 200           # the session survives a rejected attempt


def test_opening_the_link_is_recorded_as_access_not_as_approval(bapp, bseller, buyer_client, world):
    request_approval(bseller, world["pid"], world["change"]["id"])
    open_session(bapp, buyer_client, token_from(bapp))
    events = [e["event_type"] for e in bseller.get(f"/api/v1/projects/{world['pid']}/audit-events").json()]
    assert "BUYER_REQUEST_OPENED" in events and "BUYER_DECISION_RECORDED" not in events
    assert bseller.get(f"/api/v1/projects/{world['pid']}/scope-changes").json()[0]["status"] == "PROPOSED"
    assert bseller.get(f"/api/v1/projects/{world['pid']}/buyer-decisions").json() == []


def test_buyer_decisions_are_append_only(bapp, bseller, buyer_client, world):
    approve_flow(bapp, bseller, buyer_client, world)
    with DbSession(bapp.state.engine) as db:
        row = db.scalars(select(BuyerDecisionRecord)).one()
        row.decision = "REJECT_CHANGE"
        with pytest.raises(AppendOnlyError):
            db.flush()
        db.rollback()
        db.delete(db.scalars(select(BuyerDecisionRecord)).one())
        with pytest.raises(AppendOnlyError):
            db.flush()


# --- configuration and abuse limits ------------------------------------------------------------------------------------------------------

def test_without_buyer_mail_the_feature_is_off_and_fails_safe(make_app):
    app = make_app()                                           # default: buyer_mail_mode = disabled
    client = TestClient(app)
    register(client, "s@example.com")
    seller = login(client, "s@example.com")
    pid = make_project(seller)
    freeze(seller, pid)
    change = propose(seller, pid).json()
    assert request_approval(seller, pid, change["id"]).status_code == 503
    for call in (lambda: challenge(client, "x" * 30), lambda: verify(client, "x" * 30, "123456"), lambda: decide(client, "x" * 30, "APPROVE_CHANGE")):
        assert call().status_code == 503
    assert app.state.mailer is None


def test_the_fake_mailer_cannot_be_enabled_in_production():
    with pytest.raises(ValidationError):
        Settings(app_env="production", cookie_secure=True, buyer_mail_mode="fake")


def test_buyer_endpoints_are_rate_limited(make_app):
    app = make_app(buyer_mail_mode="fake", buyer_ops_per_minute=5)
    client = TestClient(app)
    codes = [challenge(client, "x" * 30).status_code for _ in range(8)]
    assert codes[:5] == [202] * 5 and codes[5:] == [429] * 3


@pytest.mark.parametrize("path,body", [
    ("/api/v1/buyer/challenge", {"token": "short"}), ("/api/v1/buyer/verify", {"token": "x" * 30, "code": "12345"}),
    ("/api/v1/buyer/verify", {"token": "x" * 30, "code": "abcdef"}), ("/api/v1/buyer/decide", {"session_token": "x" * 30, "decision": "NOPE"}),
    ("/api/v1/buyer/decide", {"session_token": "x" * 30, "decision": "ACCEPT_DELIVERY", "statement": "s" * 501}),
    ("/api/v1/buyer/challenge", {"token": "x" * 30, "extra": 1}),
])
def test_buyer_input_is_validated(bapp, buyer_client, path, body):
    assert buyer_client.post(path, json=body).status_code == 422


def test_the_buyer_statement_is_stored_verbatim_and_never_trusted_as_markup(bapp, bseller, buyer_client, delivered):
    review_request(bseller, delivered)
    session = open_session(bapp, buyer_client, token_from(bapp))["session_token"]
    decide(buyer_client, session, "ACCEPT_DELIVERY", "<script>alert(1)</script>")
    stored = bseller.get(f"/api/v1/projects/{delivered['pid']}/buyer-decisions").json()[0]["statement"]
    assert stored == "<script>alert(1)</script>"                 # JSON API: returned as data, labelled as the buyer's own words


def test_a_wrong_code_never_opens_a_session_and_no_code_means_no_session(bapp, bseller, buyer_client, world):
    request_approval(bseller, world["pid"], world["change"]["id"])
    token = token_from(bapp)
    assert verify(buyer_client, token, "123456").status_code == 401           # no challenge was ever requested
    challenge(buyer_client, token)
    code = CODE_RE.search(outbox(bapp)[-1].body).group(1)
    wrong = "000000" if code != "000000" else "111111"
    refused = verify(buyer_client, token, wrong)
    assert refused.status_code == 401 and "session_token" not in refused.text
    assert refused.json()["error"]["code"] == "challenge_failed"
    assert bseller.get(f"/api/v1/projects/{world['pid']}/buyer-requests").json()[0]["link_opened"] is False
    assert verify(buyer_client, token, code).status_code == 200               # the right code still works afterwards


# --- security review follow-ups ---------------------------------------------------------------------------------------------------

def test_a_seller_cannot_address_a_buyer_request_to_their_own_mailbox(bseller, world):
    response = request_approval(bseller, world["pid"], world["change"]["id"], email="Seller@Example.com")
    assert response.status_code == 422 and response.json()["error"]["code"] == "buyer_is_seller"
    assert bseller.get(f"/api/v1/projects/{world['pid']}/buyer-requests").json() == []


def test_every_code_attempt_is_charged_before_it_is_compared(bapp, bseller, world, monkeypatch):
    """A crash or a race during comparison must not leave attempts uncounted (parallel guesses cannot dodge the lock)."""
    from app.services import buyer as buyer_service

    request_approval(bseller, world["pid"], world["change"]["id"])
    token = token_from(bapp)
    client = TestClient(bapp, raise_server_exceptions=False)
    challenge(client, token)
    code = CODE_RE.search(outbox(bapp)[-1].body).group(1)
    real = buyer_service.tokens_match
    monkeypatch.setattr(buyer_service, "tokens_match", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    for _ in range(buyer_service.MAX_VERIFY_FAILURES):
        assert verify(client, token, "000000").status_code == 500
    monkeypatch.setattr(buyer_service, "tokens_match", real)
    assert verify(client, token, code).status_code == 401                       # locked even for the right code


def test_a_failing_mailer_does_not_reveal_that_a_link_is_valid(bapp, bseller, buyer_client, world, monkeypatch):
    request_approval(bseller, world["pid"], world["change"]["id"])
    token = token_from(bapp)
    monkeypatch.setattr(bapp.state.mailer, "send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp down")))
    assert challenge(buyer_client, token).status_code == 202
    assert challenge(buyer_client, "x" * 40).status_code == 202
