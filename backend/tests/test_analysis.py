"""Slice 4: the AI analysis boundary. Findings must cite real records; the demo provider is deterministic and labelled non-production."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models import AnalysisRun, AppendOnlyError
from app.services import analysis as analysis_service
from tests.helpers import login, register
from tests.test_buyer_approvals import (
    BUYER, CODE_RE, TOKEN_RE, challenge, decide, open_session, outbox, request_approval, token_from, verify,
)
from tests.test_scope_baselines import freeze, make_project, propose

HOME = {"key": "R1", "title": "Home page", "description": "Responsive home page with hero section and navigation menu."}
FORM = {"key": "R2", "title": "Contact form", "description": "Contact form that sends email to the site owner."}


@pytest.fixture()
def aapp(make_app):
    return make_app(buyer_mail_mode="fake", ai_mode="demo")


@pytest.fixture()
def aseller(aapp):
    client = TestClient(aapp)
    assert register(client, "seller@example.com").status_code == 201
    return login(client, "seller@example.com")


@pytest.fixture()
def buyer(aapp):
    return TestClient(aapp)


@pytest.fixture()
def pid(aseller):
    pid = make_project(aseller)
    assert freeze(aseller, pid, reqs=[HOME, FORM]).status_code == 201
    return pid


def artifact(seller, pid, **body):
    payload = {"name": "home.html", "description": "Home page build", **body}
    response = seller.post(f"/api/v1/projects/{pid}/artifacts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def run(seller, pid):
    return seller.post(f"/api/v1/projects/{pid}/analysis")


def findings(seller, pid):
    response = run(seller, pid)
    assert response.status_code == 201, response.text
    return response.json()["findings"]


def find(items, category, text):
    matches = [f for f in items if f["category"] == category and text in f["claim"]]
    assert len(matches) == 1, (category, text, [f["claim"] for f in items])
    return matches[0]


# --- switch and prerequisites ------------------------------------------------------------------------------------------------

def test_analysis_is_off_by_default_and_fails_safe(make_app):
    app = make_app()
    client = TestClient(app)
    register(client, "s@example.com")
    seller = login(client, "s@example.com")
    pid = make_project(seller)
    freeze(seller, pid)
    response = run(seller, pid)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ai_disabled"


def test_a_frozen_baseline_is_required(aseller):
    pid = make_project(aseller)
    response = run(aseller, pid)
    assert response.status_code == 409 and response.json()["error"]["code"] == "baseline_required"


def test_analysis_needs_authentication_csrf_and_ownership(aapp, aseller, pid):
    anonymous = TestClient(aapp)
    assert anonymous.post(f"/api/v1/projects/{pid}/analysis").status_code == 401
    assert aseller.client.post(f"/api/v1/projects/{pid}/analysis").status_code == 403
    other_client = TestClient(aapp)
    register(other_client, "other@example.com")
    other = login(other_client, "other@example.com")
    assert run(other, pid).status_code == 404 and other.get(f"/api/v1/projects/{pid}/analysis").status_code == 404


def test_the_result_is_labelled_as_non_production_demonstration_analysis(aseller, pid):
    body = run(aseller, pid).json()
    assert body["production_grade"] is False and body["provider_name"] == "DEMO_RULES_V1"
    assert "NOT PRODUCTION" in body["label"].upper() and body["human_review_required"] is True
    assert body["limitations"] and body["baseline_version"] == 1 and len(body["input_sha256"]) == 64
    assert {f["basis"] for f in body["findings"]} == {"RULE_BASED_DEMO"} and {f["confidence"] for f in body["findings"]} == {"LOW"}


# --- requirement coverage ------------------------------------------------------------------------------------------------------------

def test_a_requirement_without_linked_delivery_is_unsupported(aseller, pid):
    items = findings(aseller, pid)
    finding = find(items, "REQUIREMENT_COVERAGE", "R1")
    assert finding["status"] == "UNSUPPORTED" and finding["source_refs"][0].startswith("requirement:")
    assert all(not ref.startswith("artifact:") for ref in finding["source_refs"])


def test_verified_linked_content_that_matches_is_supported_only_as_a_low_confidence_heuristic(aseller, pid):
    art = artifact(aseller, pid, content_text="Responsive home page with hero section and navigation menu implemented.",
                   requirement_keys=["R1"])
    finding = find(findings(aseller, pid), "REQUIREMENT_COVERAGE", "R1")
    assert finding["status"] == "SUPPORTED" and finding["confidence"] == "LOW"
    assert f"artifact:{art['id']}" in finding["source_refs"]
    assert "heuristic" in finding["explanation"].lower() and "not semantic" in finding["explanation"].lower()
    assert find(findings(aseller, pid), "REQUIREMENT_COVERAGE", "R2")["status"] == "UNSUPPORTED"    # others unaffected


@pytest.mark.parametrize("body,reason", [
    ({"content_text": "Completely unrelated invoice spreadsheet totals."}, "overlap"),
    ({"declared_sha256": "a" * 64}, "never seen"),
    ({}, "no content"),
])
def test_unverifiable_or_unrelated_evidence_is_ambiguous_not_supported(aseller, pid, body, reason):
    artifact(aseller, pid, requirement_keys=["R1"], **body)
    finding = find(findings(aseller, pid), "REQUIREMENT_COVERAGE", "R1")
    assert finding["status"] == "AMBIGUOUS" and reason in finding["explanation"].lower()


def test_only_the_latest_artifact_version_counts(aseller, pid):
    v1 = artifact(aseller, pid, content_text="Responsive home page with hero section and navigation menu.", requirement_keys=["R1"])
    aseller.post(f"/api/v1/projects/{pid}/artifacts",
                 json={"name": "home.html", "content_text": "totally different words here", "supersedes_artifact_id": v1["id"]})
    finding = find(findings(aseller, pid), "REQUIREMENT_COVERAGE", "R1")
    assert finding["status"] == "UNSUPPORTED" and f"artifact:{v1['id']}" not in finding["source_refs"]


# --- scope: unapproved work and seller-asserted approval --------------------------------------------------------------------------------

def test_delivered_work_for_an_unapproved_change_is_flagged_outside_the_agreement(aseller, pid):
    change = propose(aseller, pid).json()
    art = artifact(aseller, pid, name="blog.zip", content_text="A blog with five posts and an RSS feed.", requirement_keys=["R3"])
    items = findings(aseller, pid)
    mismatch = find(items, "SCOPE_MISMATCH", "R3")
    assert mismatch["status"] == "UNSUPPORTED" and {f"artifact:{art['id']}", f"change:{change['id']}"} <= set(mismatch["source_refs"])
    assert "not part of the agreement" in mismatch["explanation"].lower()
    auth = find(items, "CHANGE_AUTHORIZATION", "Add a blog")
    assert auth["status"] == "UNSUPPORTED" and "no verified buyer approval" in auth["explanation"].lower()
    assert not [f for f in items if f["category"] == "REQUIREMENT_COVERAGE" and "R3" in f["claim"]]   # R3 is not in the baseline


def test_a_seller_note_claiming_approval_is_flagged_as_conflicting_not_believed(aseller, pid):
    propose(aseller, pid)
    note = aseller.post(f"/api/v1/projects/{pid}/scope-records",
                        json={"kind": "APPROVAL", "source_label": "Chat", "content": "Client said yes to the blog."}).json()
    conflict = find(findings(aseller, pid), "CONFLICTING_EVIDENCE", "approval")
    assert conflict["status"] == "AMBIGUOUS" and f"scope_record:{note['id']}" in conflict["source_refs"]
    assert "claim" in conflict["explanation"].lower()


def test_a_verified_approval_makes_the_change_authorised_and_its_requirements_part_of_coverage(aapp, aseller, buyer, pid):
    change = propose(aseller, pid).json()
    request_approval(aseller, pid, change["id"])
    session = open_session(aapp, buyer, token_from(aapp))["session_token"]
    decision = decide(buyer, session, "APPROVE_CHANGE", "ok").json()["decision"]
    artifact(aseller, pid, name="blog.zip", content_text="A blog with five posts and an RSS feed.", requirement_keys=["R3"])
    items = findings(aseller, pid)
    auth = find(items, "CHANGE_AUTHORIZATION", "Add a blog")
    assert auth["status"] == "SUPPORTED" and f"buyer_decision:{decision['id']}" in auth["source_refs"]
    assert find(items, "REQUIREMENT_COVERAGE", "R3")["status"] == "SUPPORTED"                  # now in baseline v2
    assert not [f for f in items if f["category"] == "SCOPE_MISMATCH"]


def test_a_rejected_change_with_delivered_work_cites_the_buyers_rejection(aapp, aseller, buyer, pid):
    change = propose(aseller, pid).json()
    request_approval(aseller, pid, change["id"])
    session = open_session(aapp, buyer, token_from(aapp))["session_token"]
    decision = decide(buyer, session, "REJECT_CHANGE").json()["decision"]
    artifact(aseller, pid, name="blog.zip", content_text="blog", requirement_keys=["R3"])
    mismatch = find(findings(aseller, pid), "SCOPE_MISMATCH", "R3")
    assert f"buyer_decision:{decision['id']}" in mismatch["source_refs"] and "rejected" in mismatch["explanation"].lower()


# --- delivery acceptance ---------------------------------------------------------------------------------------------------------------

def review(aapp, aseller, buyer, pid, art, decision, statement=None):
    aseller.post(f"/api/v1/projects/{pid}/buyer-requests",
                 json={"kind": "DELIVERY_REVIEW", "buyer_email": BUYER, "artifact_ids": [art["id"]]})
    session = open_session(aapp, buyer, TOKEN_RE.search([m for m in outbox(aapp) if "token=" in m.body][-1].body).group(1))
    return decide(buyer, session["session_token"], decision, statement).json()["decision"]


def acceptance(items):
    return find(items, "DELIVERY_ACCEPTANCE", "accepted")


def test_without_buyer_evidence_acceptance_is_unsupported(aseller, pid):
    artifact(aseller, pid, requirement_keys=["R1"], content_text="x")
    assert acceptance(findings(aseller, pid))["status"] == "UNSUPPORTED"


def test_receipt_acknowledgment_is_not_acceptance(aapp, aseller, buyer, pid):
    art = artifact(aseller, pid, requirement_keys=["R1"], content_text="x")
    review(aapp, aseller, buyer, pid, art, "ACKNOWLEDGE_DELIVERY")
    found = acceptance(findings(aseller, pid))
    assert found["status"] == "AMBIGUOUS" and "receipt" in found["explanation"].lower()


def test_an_explicit_verified_acceptance_is_supported_and_cites_the_decision(aapp, aseller, buyer, pid):
    art = artifact(aseller, pid, requirement_keys=["R1"], content_text="x")
    decision = review(aapp, aseller, buyer, pid, art, "ACCEPT_DELIVERY", "Accepted.")
    found = acceptance(findings(aseller, pid))
    assert found["status"] == "SUPPORTED" and f"buyer_decision:{decision['id']}" in found["source_refs"]


def test_acceptance_does_not_cover_artifacts_added_afterwards(aapp, aseller, buyer, pid):
    art = artifact(aseller, pid, requirement_keys=["R1"], content_text="x")
    review(aapp, aseller, buyer, pid, art, "ACCEPT_DELIVERY")
    artifact(aseller, pid, name="late.zip", requirement_keys=["R2"], content_text="added after the buyer accepted")
    found = acceptance(findings(aseller, pid))
    assert found["status"] == "AMBIGUOUS" and "not covered" in found["explanation"].lower()


def test_a_later_dispute_overrides_an_earlier_acceptance(aapp, aseller, buyer, pid):
    art = artifact(aseller, pid, requirement_keys=["R1"], content_text="x")
    review(aapp, aseller, buyer, pid, art, "ACCEPT_DELIVERY")
    disputed = review(aapp, aseller, buyer, pid, art, "DISPUTE_DELIVERY")
    found = acceptance(findings(aseller, pid))
    assert found["status"] == "UNSUPPORTED" and f"buyer_decision:{disputed['id']}" in found["source_refs"]
    assert find(findings(aseller, pid), "CONFLICTING_EVIDENCE", "dispute")["status"] == "AMBIGUOUS"


# --- the citation validator: a model cannot make things up ----------------------------------------------------------------------------------

class ScriptedProvider:
    name, production_grade = "SCRIPTED_TEST", False

    def __init__(self, raw):
        self.raw = raw

    def analyze(self, data):
        return self.raw(data) if callable(self.raw) else self.raw


def good(**over):
    base = {"id": "F001", "category": "REQUIREMENT_COVERAGE", "claim": "c", "status": "AMBIGUOUS", "confidence": "LOW",
            "basis": "LLM", "source_refs": [], "explanation": "e", "human_review_required": True}
    base.update(over)
    return base


def scripted(aapp, aseller, pid, raw):
    aapp.state.analysis_provider = ScriptedProvider(raw)
    return run(aseller, pid)


def test_a_citation_to_a_record_that_does_not_exist_is_rejected_and_nothing_is_stored(aapp, aseller, pid):
    response = scripted(aapp, aseller, pid, [good(status="SUPPORTED", source_refs=["artifact:invented-id"])])
    assert response.status_code == 502 and response.json()["error"]["code"] == "analysis_invalid"
    assert aseller.get(f"/api/v1/projects/{pid}/analysis").json() == []


def test_a_citation_to_another_projects_record_is_rejected(aapp, aseller, pid):
    other = make_project(aseller, "Other")
    freeze(aseller, other, reqs=[HOME])
    foreign = artifact(aseller, other, content_text="foreign")
    response = scripted(aapp, aseller, pid, [good(source_refs=[f"artifact:{foreign['id']}"])])
    assert response.status_code == 502 and response.json()["error"]["code"] == "analysis_invalid"


@pytest.mark.parametrize("bad", [
    good(status="SUPPORTED", source_refs=[]),
    good(source_refs=["not-a-typed-ref"]), good(source_refs=["artifact:"]), good(source_refs=["secret:abc"]),
    good(status="MAYBE"), good(confidence="CERTAIN"), good(category="PREDICTED_DISPUTE_OUTCOME"),
    good(human_review_required=False), good(extra_field="x"), good(claim=""), good(id=""),
])
def test_malformed_findings_are_rejected(aapp, aseller, pid, bad):
    response = scripted(aapp, aseller, pid, [bad])
    assert response.status_code == 502 and response.json()["error"]["code"] == "analysis_invalid"
    assert aseller.get(f"/api/v1/projects/{pid}/analysis").json() == []


def test_duplicate_ids_and_oversized_output_are_rejected(aapp, aseller, pid):
    assert scripted(aapp, aseller, pid, [good(), good()]).status_code == 502
    assert scripted(aapp, aseller, pid, [good(id=f"F{i:04d}") for i in range(analysis_service.MAX_FINDINGS + 1)]).status_code == 502


def test_acceptance_cannot_be_asserted_from_an_artifact_alone(aapp, aseller, pid):
    art = artifact(aseller, pid, content_text="x", requirement_keys=["R1"])
    raw = [good(category="DELIVERY_ACCEPTANCE", status="SUPPORTED", source_refs=[f"artifact:{art['id']}"], claim="Buyer accepted")]
    response = scripted(aapp, aseller, pid, raw)
    assert response.status_code == 502 and "acceptance" in str(response.json()["error"]["details"]).lower()


def test_coverage_cannot_be_supported_without_citing_an_artifact(aapp, aseller, pid):
    requirement = aseller.get(f"/api/v1/projects/{pid}/baselines").json()[0]["requirements"][0]["id"]
    raw = [good(status="SUPPORTED", source_refs=[f"requirement:{requirement}"])]
    assert scripted(aapp, aseller, pid, raw).status_code == 502


def test_a_valid_scripted_finding_is_accepted_and_still_requires_human_review(aapp, aseller, pid):
    art = artifact(aseller, pid, content_text="x", requirement_keys=["R1"])
    requirement = aseller.get(f"/api/v1/projects/{pid}/baselines").json()[0]["requirements"][0]["id"]
    raw = [good(status="SUPPORTED", source_refs=[f"requirement:{requirement}", f"artifact:{art['id']}"])]
    response = scripted(aapp, aseller, pid, raw)
    assert response.status_code == 201 and response.json()["human_review_required"] is True
    assert response.json()["provider_name"] == "SCRIPTED_TEST" and "NOT PRODUCTION" in response.json()["label"].upper()


def test_a_crashing_provider_fails_closed_without_leaking_its_message(aapp, aseller, pid):
    def boom(_data):
        raise RuntimeError("secret internal detail sk-should-not-appear")

    response = scripted(aapp, aseller, pid, boom)
    assert response.status_code == 502 and response.json()["error"]["code"] == "analysis_failed"
    assert "sk-should-not-appear" not in response.text and aseller.get(f"/api/v1/projects/{pid}/analysis").json() == []


# --- determinism, resolution, immutability --------------------------------------------------------------------------------------------------

def test_the_demo_analysis_is_deterministic_for_identical_evidence(aseller, pid):
    artifact(aseller, pid, content_text="Responsive home page with hero section and navigation menu.", requirement_keys=["R1"])
    first, second = run(aseller, pid).json(), run(aseller, pid).json()
    assert first["findings"] == second["findings"] and first["input_sha256"] == second["input_sha256"]
    assert first["id"] != second["id"] and len(aseller.get(f"/api/v1/projects/{pid}/analysis").json()) == 2


def test_new_evidence_changes_the_input_hash(aseller, pid):
    before = run(aseller, pid).json()["input_sha256"]
    artifact(aseller, pid, content_text="something new", requirement_keys=["R1"])
    assert run(aseller, pid).json()["input_sha256"] != before


def test_every_citation_in_the_demo_output_resolves_to_a_real_record(aapp, aseller, buyer, pid):
    change = propose(aseller, pid).json()
    art = artifact(aseller, pid, content_text="Responsive home page with hero section and navigation menu.", requirement_keys=["R1"])
    artifact(aseller, pid, name="blog.zip", content_text="blog", requirement_keys=["R3"])
    aseller.post(f"/api/v1/projects/{pid}/scope-records", json={"kind": "APPROVAL", "source_label": "x", "content": "yes"})
    review(aapp, aseller, buyer, pid, art, "ACCEPT_DELIVERY")
    items = findings(aseller, pid)
    with DbSession(aapp.state.engine) as db:
        allowed = analysis_service.allowed_references(db, pid)
    refs = [ref for f in items for ref in f["source_refs"]]
    assert refs and all(ref in allowed for ref in refs)
    assert {ref.split(":")[0] for ref in refs} >= {"requirement", "artifact", "change", "buyer_decision", "scope_record"}


def test_analysis_runs_are_append_only(aapp, aseller, pid):
    run(aseller, pid)
    with DbSession(aapp.state.engine) as db:
        row = db.scalars(select(AnalysisRun)).one()
        row.findings = []
        with pytest.raises(AppendOnlyError):
            db.flush()


# --- security review follow-ups: citations must have the right RELATIONSHIP, not just the right type -------------------------------------

def req_id(seller, pid, key):
    baseline = seller.get(f"/api/v1/projects/{pid}/baselines").json()[-1]
    return next(r["id"] for r in baseline["requirements"] if r["key"] == key)


def test_an_approval_of_one_change_cannot_authorise_another(aapp, aseller, buyer, pid):
    first = propose(aseller, pid, title="First").json()
    request_approval(aseller, pid, first["id"])
    decision = decide(buyer, open_session(aapp, buyer, token_from(aapp))["session_token"], "APPROVE_CHANGE").json()["decision"]
    second = propose(aseller, pid, reqs=[{"key": "R9", "title": "Other", "description": "Other thing."}], title="Second").json()
    raw = [good(category="CHANGE_AUTHORIZATION", status="SUPPORTED",
                source_refs=[f"change:{second['id']}", f"buyer_decision:{decision['id']}"])]
    assert scripted(aapp, aseller, pid, raw).status_code == 502


def test_coverage_cannot_cite_an_artifact_linked_to_a_different_requirement(aapp, aseller, pid):
    art = artifact(aseller, pid, content_text="x", requirement_keys=["R2"])
    raw = [good(status="SUPPORTED", source_refs=[f"requirement:{req_id(aseller, pid, 'R1')}", f"artifact:{art['id']}"])]
    assert scripted(aapp, aseller, pid, raw).status_code == 502


def test_supported_is_only_allowed_for_categories_that_have_a_relationship_check(aapp, aseller, pid):
    art = artifact(aseller, pid, content_text="x", requirement_keys=["R1"])
    for category in ("SCOPE_MISMATCH", "CONFLICTING_EVIDENCE", "MISSING_EVIDENCE"):
        raw = [good(category=category, status="SUPPORTED", source_refs=[f"artifact:{art['id']}"])]
        assert scripted(aapp, aseller, pid, raw).status_code == 502


def test_an_acceptance_that_was_later_disputed_cannot_be_cited_as_current(aapp, aseller, buyer, pid):
    art = artifact(aseller, pid, content_text="x", requirement_keys=["R1"])
    accepted = review(aapp, aseller, buyer, pid, art, "ACCEPT_DELIVERY")
    review(aapp, aseller, buyer, pid, art, "DISPUTE_DELIVERY")
    raw = [good(category="DELIVERY_ACCEPTANCE", status="SUPPORTED", source_refs=[f"buyer_decision:{accepted['id']}"])]
    assert scripted(aapp, aseller, pid, raw).status_code == 502


def test_analysis_runs_are_rate_limited(make_app):
    app = make_app(ai_mode="demo", payment_ops_per_minute=2)
    client = TestClient(app)
    register(client, "s@example.com")
    seller = login(client, "s@example.com")
    pid = make_project(seller)
    freeze(seller, pid)
    assert [run(seller, pid).status_code for _ in range(3)] == [201, 201, 429]
