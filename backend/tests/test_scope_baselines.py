"""Slice 1: immutable, versioned scope baselines, requirement ids, change proposals and the audit trail."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session as DbSession

from app import models
from app.errors import ApiError
from app.models import AppendOnlyError, AuditEvent, Requirement, ScopeBaseline, ScopeChange
from app.services import scope as scope_service
from tests.helpers import login, register

REQS = [
    {"key": "R1", "title": "Home page", "description": "Responsive home page with hero section."},
    {"key": "R2", "title": "Contact form", "description": "Contact form that emails the owner."},
]
BLOG = [{"key": "R3", "title": "Blog", "description": "A blog with five posts and an RSS feed."}]


def make_project(seller, title="Website build"):
    return seller.post("/api/v1/projects", json={"title": title}).json()["id"]


def original_record(seller, pid, kind="ORIGINAL"):
    response = seller.post(f"/api/v1/projects/{pid}/scope-records",
                           json={"kind": kind, "source_label": "Signed proposal", "content": "Home page and contact form."})
    assert response.status_code == 201
    return response.json()["id"]


def freeze(seller, pid, record_id=None, reqs=None):
    record_id = record_id or original_record(seller, pid)
    return seller.post(f"/api/v1/projects/{pid}/baselines",
                       json={"source_scope_record_id": record_id, "requirements": REQS if reqs is None else reqs})


def propose(seller, pid, reqs=None, title="Add a blog", **extra):
    return seller.post(f"/api/v1/projects/{pid}/scope-changes",
                       json={"title": title, "description": "Client asked for a blog.", "requirements": reqs or BLOG, **extra})


@pytest.fixture()
def pid(seller):
    return make_project(seller)


@pytest.fixture()
def frozen(seller, pid):
    response = freeze(seller, pid)
    assert response.status_code == 201, response.text
    return response.json()


# --- freezing the original agreement ---------------------------------------------------------------------------------------

def test_freezing_creates_version_1_with_stable_requirement_ids(seller, pid, frozen):
    assert frozen["version"] == 1 and frozen["kind"] == "ORIGINAL" and frozen["previous_baseline_id"] is None
    assert [r["key"] for r in frozen["requirements"]] == ["R1", "R2"]
    assert all(r["origin"] == "ORIGINAL" and r["id"] for r in frozen["requirements"])
    assert len(frozen["content_sha256"]) == 64


def test_the_baseline_hash_does_not_depend_on_input_order(seller_factory, app):
    first, second = seller_factory("a@example.com"), seller_factory("b@example.com")
    a = freeze(first, make_project(first), reqs=REQS).json()
    b = freeze(second, make_project(second), reqs=list(reversed(REQS))).json()
    assert a["content_sha256"] == b["content_sha256"]


def test_freezing_needs_authentication_and_csrf(app, seller, pid):
    record = original_record(seller, pid)
    anonymous = TestClient(app)
    body = {"source_scope_record_id": record, "requirements": REQS}
    assert anonymous.post(f"/api/v1/projects/{pid}/baselines", json=body).status_code == 401
    assert seller.client.post(f"/api/v1/projects/{pid}/baselines", json=body).status_code == 403  # no CSRF header


@pytest.mark.parametrize("kind", ["CHANGE_REQUEST", "APPROVAL", "DELIVERY_NOTE"])
def test_only_an_original_scope_record_can_be_the_source(seller, pid, kind):
    response = freeze(seller, pid, record_id=original_record(seller, pid, kind))
    assert response.status_code == 409 and response.json()["error"]["code"] == "original_scope_required"


def test_another_projects_scope_record_cannot_be_used(seller, pid):
    other = make_project(seller, "Other")
    response = freeze(seller, pid, record_id=original_record(seller, other))
    assert response.status_code == 409 and response.json()["error"]["code"] == "original_scope_required"


def test_the_original_agreement_can_only_be_frozen_once(seller, pid, frozen):
    again = freeze(seller, pid, record_id=frozen["source_scope_record_id"], reqs=BLOG)
    assert again.status_code == 409 and again.json()["error"]["code"] == "baseline_exists"
    assert [b["version"] for b in seller.get(f"/api/v1/projects/{pid}/baselines").json()] == [1]


@pytest.mark.parametrize("reqs", [
    [], REQS + [REQS[0]],
    [{"key": "r1", "title": "x", "description": "y"}], [{"key": "1R", "title": "x", "description": "y"}],
    [{"key": "R" * 30, "title": "x", "description": "y"}], [{"key": "R1", "title": "", "description": "y"}],
    [{"key": "R1", "title": "x" * 300, "description": "y"}],
    [{"key": "R1", "title": "x", "description": "y", "id": "forged"}],
])
def test_invalid_requirement_lists_are_rejected(seller, pid, reqs):
    assert freeze(seller, pid, reqs=reqs).status_code == 422


def test_a_baseline_request_cannot_carry_ownership_or_version_fields(seller, pid):
    record = original_record(seller, pid)
    body = {"source_scope_record_id": record, "requirements": REQS}
    for extra in ({"seller_id": "x"}, {"version": 9}, {"kind": "AMENDED"}):
        assert seller.post(f"/api/v1/projects/{pid}/baselines", json={**body, **extra}).status_code == 422


# --- change proposals never rewrite the agreement ------------------------------------------------------------------------------

def test_a_change_needs_a_frozen_baseline(seller, pid):
    response = propose(seller, pid)
    assert response.status_code == 409 and response.json()["error"]["code"] == "baseline_required"


def test_a_proposed_change_is_pending_and_leaves_the_agreement_untouched(seller, pid, frozen):
    response = propose(seller, pid)
    assert response.status_code == 201
    change = response.json()
    assert change["status"] == "PROPOSED" and change["resolved_baseline_id"] is None
    assert change["base_baseline_id"] == frozen["id"] and change["proposed_requirements"][0]["key"] == "R3"
    baselines = seller.get(f"/api/v1/projects/{pid}/baselines").json()
    assert len(baselines) == 1 and [r["key"] for r in baselines[0]["requirements"]] == ["R1", "R2"]
    assert baselines[0]["content_sha256"] == frozen["content_sha256"]


def test_a_change_cannot_reuse_an_existing_requirement_key(seller, pid, frozen):
    response = propose(seller, pid, reqs=[{"key": "R1", "title": "Home v2", "description": "Rewrite the home page."}])
    assert response.status_code == 409 and response.json()["error"]["code"] == "requirement_key_conflict"


def test_the_seller_cannot_approve_reject_or_edit_a_change(seller, pid, frozen):
    change = propose(seller, pid).json()
    base = f"/api/v1/projects/{pid}/scope-changes/{change['id']}"
    for suffix in ("/approve", "/accept", "/status", "/apply", ""):
        for method in ("post", "put", "patch", "delete"):
            response = seller.client.request(method.upper(), base + suffix, headers=seller.headers, json={"status": "APPROVED"})
            assert response.status_code in (404, 405), (method, suffix)
    for forged in ({"status": "APPROVED"}, {"resolved_baseline_id": "x"}, {"approved_by": "buyer"}):
        assert propose(seller, pid, reqs=[{"key": "R9", "title": "t", "description": "d"}], **forged).status_code == 422
    assert seller.get(f"/api/v1/projects/{pid}/scope-changes").json()[0]["status"] == "PROPOSED"


def test_a_replacement_proposal_supersedes_the_pending_one(seller, pid, frozen):
    first = propose(seller, pid).json()
    second = propose(seller, pid, reqs=[{"key": "R4", "title": "Blog v2", "description": "Blog with ten posts."}],
                     supersedes_change_id=first["id"])
    assert second.status_code == 201 and second.json()["supersedes_change_id"] == first["id"]
    states = {c["id"]: c["status"] for c in seller.get(f"/api/v1/projects/{pid}/scope-changes").json()}
    assert states == {first["id"]: "SUPERSEDED", second.json()["id"]: "PROPOSED"}
    again = propose(seller, pid, reqs=[{"key": "R5", "title": "t", "description": "d"}], supersedes_change_id=first["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "change_not_pending"


def test_superseding_an_unknown_change_is_not_found(seller, pid, frozen):
    assert propose(seller, pid, supersedes_change_id="no-such-change").status_code == 404


# --- isolation -----------------------------------------------------------------------------------------------------------------

def test_other_sellers_cannot_see_or_touch_baselines_changes_or_audit(seller_factory, app):
    owner, intruder = seller_factory("owner@example.com"), seller_factory("intruder@example.com")
    pid = make_project(owner)
    record = freeze(owner, pid).json()["source_scope_record_id"]
    change = propose(owner, pid).json()
    for suffix in ("baselines", "scope-changes", "audit-events"):
        assert intruder.get(f"/api/v1/projects/{pid}/{suffix}").status_code == 404
    assert freeze(intruder, pid, record_id=record).status_code == 404
    assert propose(intruder, pid).status_code == 404
    other = make_project(intruder)
    freeze(intruder, other)
    assert propose(intruder, other, reqs=[{"key": "R7", "title": "t", "description": "d"}],
                   supersedes_change_id=change["id"]).status_code == 404   # another project's change


# --- audit trail ------------------------------------------------------------------------------------------------------------------

def test_meaningful_transitions_are_audited_in_order(seller, pid, frozen):
    first = propose(seller, pid).json()
    propose(seller, pid, reqs=[{"key": "R4", "title": "t", "description": "d"}], supersedes_change_id=first["id"])
    events = seller.get(f"/api/v1/projects/{pid}/audit-events").json()
    assert [e["event_type"] for e in events] == ["BASELINE_FROZEN", "CHANGE_PROPOSED", "CHANGE_SUPERSEDED", "CHANGE_PROPOSED"]
    assert {e["actor_role"] for e in events} == {"SELLER"}
    assert events[0]["subject_id"] == frozen["id"] and events[2]["subject_id"] == first["id"]


# --- immutability at the persistence layer ---------------------------------------------------------------------------------------

@pytest.fixture()
def db(app):
    models.Base.metadata.create_all(app.state.engine)
    with DbSession(app.state.engine) as session:
        yield session


def _first(db, model):
    return db.scalars(select(model)).first()


def test_baselines_requirements_and_audit_events_are_append_only(app, seller, pid, frozen, db):
    for model, field in ((ScopeBaseline, "content_sha256"), (Requirement, "title"), (AuditEvent, "event_type")):
        row = _first(db, model)
        setattr(row, field, "tampered")
        with pytest.raises(AppendOnlyError):
            db.flush()
        db.rollback()
        db.delete(_first(db, model))
        with pytest.raises(AppendOnlyError):
            db.flush()
        db.rollback()
    for table in ("scope_baselines", "requirements", "audit_events"):  # and below the ORM, via the database triggers
        with pytest.raises(Exception):
            db.execute(text(f"UPDATE {table} SET id = id || 'x'"))
        db.rollback()
        with pytest.raises(Exception):
            db.execute(text(f"DELETE FROM {table}"))
        db.rollback()


# --- applying a verified buyer decision (the only route to an amended baseline) ---------------------------------------------------

def _change_row(db, pid):
    return db.scalars(select(ScopeChange).where(ScopeChange.project_id == pid)).one()


def test_an_approval_creates_a_new_baseline_and_keeps_version_1_intact(app, seller, pid, frozen, db):
    propose(seller, pid)
    change = _change_row(db, pid)
    result = scope_service.apply_change_decision(db, change, approve=True, decision_ref="decision-1")
    db.commit()
    assert result.applied and result.baseline.version == 2 and result.baseline.kind == "AMENDED"
    baselines = {b["version"]: b for b in seller.get(f"/api/v1/projects/{pid}/baselines").json()}
    assert [r["key"] for r in baselines[2]["requirements"]] == ["R1", "R2", "R3"]
    assert baselines[2]["previous_baseline_id"] == baselines[1]["id"] and baselines[2]["change_id"] == change.id
    assert baselines[1] == frozen                                              # the original agreement is byte-for-byte unchanged
    assert [r["origin"] for r in baselines[2]["requirements"]] == ["ORIGINAL", "ORIGINAL", f"CHANGE:{change.id}"]
    stored = seller.get(f"/api/v1/projects/{pid}/scope-changes").json()[0]
    assert stored["status"] == "APPROVED" and stored["resolved_baseline_id"] == baselines[2]["id"]
    events = seller.get(f"/api/v1/projects/{pid}/audit-events").json()
    approved = [e for e in events if e["event_type"] == "CHANGE_APPROVED"][0]
    assert approved["actor_role"] == "BUYER_EMAIL_VERIFIED" and approved["detail"]["decision_ref"] == "decision-1"


def test_a_rejection_changes_no_baseline(app, seller, pid, frozen, db):
    propose(seller, pid)
    result = scope_service.apply_change_decision(db, _change_row(db, pid), approve=False, decision_ref="decision-2")
    db.commit()
    assert not result.applied and result.baseline is None
    assert len(seller.get(f"/api/v1/projects/{pid}/baselines").json()) == 1
    assert seller.get(f"/api/v1/projects/{pid}/scope-changes").json()[0]["status"] == "REJECTED"


def test_a_decided_change_cannot_be_decided_again(app, seller, pid, frozen, db):
    propose(seller, pid)
    change = _change_row(db, pid)
    scope_service.apply_change_decision(db, change, approve=True, decision_ref="d1")
    db.commit()
    with pytest.raises(ApiError) as caught:
        scope_service.apply_change_decision(db, change, approve=False, decision_ref="d2")
    assert caught.value.code == "change_not_pending"


def test_a_superseded_change_cannot_be_approved(app, seller, pid, frozen, db):
    first = propose(seller, pid).json()
    propose(seller, pid, reqs=[{"key": "R4", "title": "t", "description": "d"}], supersedes_change_id=first["id"])
    old = db.get(ScopeChange, first["id"])
    with pytest.raises(ApiError) as caught:
        scope_service.apply_change_decision(db, old, approve=True, decision_ref="d1")
    assert caught.value.code == "change_not_pending"


def test_an_approval_that_now_conflicts_with_the_baseline_is_superseded_not_applied(app, seller, pid, frozen, db):
    seller_change = propose(seller, pid, reqs=[{"key": "R3", "title": "Blog", "description": "A"}]).json()
    # a second pending change with the same key can only be created while the first is pending: supersede-free path
    other = propose(seller, pid, reqs=[{"key": "R3", "title": "Gallery", "description": "B"}])
    assert other.status_code == 201                                               # keys are only checked against the baseline
    scope_service.apply_change_decision(db, db.get(ScopeChange, seller_change["id"]), approve=True, decision_ref="d1")
    db.commit()
    result = scope_service.apply_change_decision(db, db.get(ScopeChange, other.json()["id"]), approve=True, decision_ref="d2")
    db.commit()
    assert not result.applied and result.reason == "REQUIREMENT_KEY_CONFLICT"
    states = {c["id"]: c["status"] for c in seller.get(f"/api/v1/projects/{pid}/scope-changes").json()}
    assert states[seller_change["id"]] == "APPROVED" and states[other.json()["id"]] == "SUPERSEDED"
    assert len(seller.get(f"/api/v1/projects/{pid}/baselines").json()) == 2     # no third baseline was created
