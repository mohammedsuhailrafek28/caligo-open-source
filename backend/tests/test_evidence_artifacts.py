"""Slice 2: delivery evidence artifacts: hashing, honest verification labels, versions, duplicates, links and the timeline."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session as DbSession

from app import models
from app.models import AppendOnlyError, ArtifactLink, EvidenceArtifact
from tests.test_scope_baselines import BLOG, REQS, freeze, make_project, propose

SHA = lambda s: hashlib.sha256(s.encode("utf8")).hexdigest()  # noqa: E731


@pytest.fixture()
def pid(seller):
    pid = make_project(seller)
    assert freeze(seller, pid).status_code == 201
    return pid


def art(seller, pid, **body):
    payload = {"name": "site.zip", "description": "First delivery build", **body}
    return seller.post(f"/api/v1/projects/{pid}/artifacts", json=payload)


def utc(minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


# --- registration and honest integrity labels --------------------------------------------------------------------------------

def test_content_is_hashed_by_the_server_and_labelled_as_verified(seller, pid):
    response = art(seller, pid, content_text="<html>home</html>", media_type="text/html")
    assert response.status_code == 201
    body = response.json()
    assert body["sha256"] == SHA("<html>home</html>") and body["content_verification"] == "VERIFIED_SERVER_HASH"
    assert body["version"] == 1 and body["is_latest"] and body["size_bytes"] == len("<html>home</html>")
    assert body["supersedes_artifact_id"] is None and body["requirement_keys"] == []


def test_without_content_nothing_is_claimed_about_integrity(seller, pid):
    body = art(seller, pid).json()
    assert body["sha256"] is None and body["content_verification"] == "UNAVAILABLE" and body["size_bytes"] is None


def test_a_declared_hash_alone_is_labelled_declared_only(seller, pid):
    body = art(seller, pid, declared_sha256=SHA("external file")).json()
    assert body["sha256"] == SHA("external file") and body["content_verification"] == "DECLARED_ONLY"


def test_a_declared_hash_that_contradicts_the_content_is_rejected(seller, pid):
    response = art(seller, pid, content_text="real", declared_sha256=SHA("other"))
    assert response.status_code == 422 and response.json()["error"]["code"] == "hash_mismatch"
    assert seller.get(f"/api/v1/projects/{pid}/artifacts").json() == []
    assert art(seller, pid, content_text="real", declared_sha256=SHA("real")).json()["content_verification"] == "VERIFIED_SERVER_HASH"


def test_server_time_is_recorded_separately_from_the_asserted_time(seller, pid):
    asserted = "2020-01-02T03:04:05+00:00"
    body = art(seller, pid, content_text="x", asserted_at=asserted).json()
    ingested = datetime.fromisoformat(body["ingested_at"])
    assert datetime.fromisoformat(body["asserted_at"]) == datetime.fromisoformat(asserted)
    assert abs(datetime.now(timezone.utc) - ingested) < timedelta(minutes=1)      # the server's clock, not the seller's
    assert art(seller, pid, name="b.zip", content_text="y").json()["asserted_at"] is None


@pytest.mark.parametrize("asserted", ["2999-01-01T00:00:00+00:00", "2020-01-01T00:00:00", "yesterday"])
def test_future_naive_or_unparsable_asserted_times_are_rejected(seller, pid, asserted):
    assert art(seller, pid, asserted_at=asserted).status_code == 422


@pytest.mark.parametrize("extra", [
    {"sha256": "a" * 64}, {"ingested_at": "2020-01-01T00:00:00+00:00"}, {"project_id": "x"}, {"version": 7},
    {"content_verification": "VERIFIED_SERVER_HASH"}, {"is_latest": True}, {"seller_id": "x"},
])
def test_clients_cannot_forge_server_decided_fields(seller, pid, extra):
    assert art(seller, pid, **extra).status_code == 422


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"name": "x" * 256}, {"declared_sha256": "ABC"}, {"declared_sha256": "A" * 64}, {"declared_sha256": "a" * 63},
    {"content_text": "é" * 40_000}, {"media_type": "x" * 101},
])
def test_invalid_artifact_input_is_rejected(seller, pid, bad):
    assert art(seller, pid, **bad).status_code == 422


# --- duplicates and versions ----------------------------------------------------------------------------------------------------

def test_the_same_content_cannot_be_registered_twice(seller, pid):
    first = art(seller, pid, content_text="same bytes").json()
    for body in ({"content_text": "same bytes"}, {"name": "renamed.zip", "content_text": "same bytes"},
                 {"name": "other.zip", "declared_sha256": SHA("same bytes")}):
        response = art(seller, pid, **body)
        assert response.status_code == 409 and response.json()["error"]["code"] == "duplicate_artifact"
        assert first["id"] in str(response.json()["error"]["details"])
    assert len(seller.get(f"/api/v1/projects/{pid}/artifacts").json()) == 1


def test_a_replacement_is_a_new_version_and_the_old_one_is_kept_untouched(seller, pid):
    v1 = art(seller, pid, content_text="build one").json()
    v2 = art(seller, pid, content_text="build two", supersedes_artifact_id=v1["id"])
    assert v2.status_code == 201 and v2.json()["version"] == 2 and v2.json()["supersedes_artifact_id"] == v1["id"]
    listed = {a["version"]: a for a in seller.get(f"/api/v1/projects/{pid}/artifacts").json()}
    assert listed[1]["sha256"] == v1["sha256"] == SHA("build one") and listed[1]["is_latest"] is False
    assert listed[2]["is_latest"] is True
    stale = art(seller, pid, content_text="build three", supersedes_artifact_id=v1["id"])
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "artifact_not_latest"
    same = art(seller, pid, content_text="build two again?", supersedes_artifact_id=listed[2]["id"])
    assert same.status_code == 201 and same.json()["version"] == 3


def test_a_replacement_must_keep_the_name_and_belong_to_the_project(seller, pid):
    v1 = art(seller, pid, content_text="one").json()
    renamed = art(seller, pid, name="different.zip", content_text="two", supersedes_artifact_id=v1["id"])
    assert renamed.status_code == 422 and renamed.json()["error"]["code"] == "artifact_name_mismatch"
    other = make_project(seller, "Other")
    assert art(seller, other, content_text="three", supersedes_artifact_id=v1["id"]).status_code == 404
    assert art(seller, pid, content_text="four", supersedes_artifact_id="no-such").status_code == 404


# --- links to requirements ------------------------------------------------------------------------------------------------------------

def test_artifacts_link_to_baseline_requirements(seller, pid):
    body = art(seller, pid, content_text="home", requirement_keys=["R2", "R1"]).json()
    assert body["requirement_keys"] == ["R1", "R2"]
    more = seller.post(f"/api/v1/projects/{pid}/artifacts/{body['id']}/links", json={"requirement_keys": ["R1"]})
    assert more.status_code == 200 and more.json()["requirement_keys"] == ["R1", "R2"]        # idempotent


def test_links_to_unknown_requirements_are_rejected_and_nothing_is_stored(seller, pid):
    response = art(seller, pid, content_text="x", requirement_keys=["R1", "R99"])
    assert response.status_code == 422 and response.json()["error"]["code"] == "unknown_requirement"
    assert seller.get(f"/api/v1/projects/{pid}/artifacts").json() == []
    body = art(seller, pid, content_text="y").json()
    bad = seller.post(f"/api/v1/projects/{pid}/artifacts/{body['id']}/links", json={"requirement_keys": ["nope"]})
    assert bad.status_code == 422


def test_a_pending_changes_requirement_can_be_linked_but_the_agreement_does_not_grow(seller, pid):
    propose(seller, pid)
    body = art(seller, pid, content_text="blog code", requirement_keys=["R3"]).json()
    assert body["requirement_keys"] == ["R3"]
    baselines = seller.get(f"/api/v1/projects/{pid}/baselines").json()
    assert len(baselines) == 1 and "R3" not in [r["key"] for r in baselines[0]["requirements"]]


def test_requirement_keys_of_another_project_do_not_resolve(seller):
    a, b = make_project(seller, "A"), make_project(seller, "B")
    freeze(seller, a)
    freeze(seller, b, reqs=[{"key": "ONLYB", "title": "t", "description": "d"}])
    assert art(seller, a, content_text="x", requirement_keys=["ONLYB"]).status_code == 422


def test_without_a_frozen_baseline_no_link_can_be_made(seller):
    pid = make_project(seller)
    assert art(seller, pid, content_text="x", requirement_keys=["R1"]).status_code == 422
    assert art(seller, pid, content_text="x").status_code == 201                                  # unlinked evidence is allowed


# --- auth and isolation ------------------------------------------------------------------------------------------------------------------

def test_artifact_routes_need_authentication_csrf_and_ownership(app, seller_factory):
    owner, intruder = seller_factory("owner@example.com"), seller_factory("intruder@example.com")
    pid = make_project(owner)
    freeze(owner, pid)
    artifact = art(owner, pid, content_text="secret build").json()
    anonymous = TestClient(app)
    base = f"/api/v1/projects/{pid}"
    assert anonymous.get(f"{base}/artifacts").status_code == 401
    assert anonymous.post(f"{base}/artifacts", json={"name": "x"}).status_code == 401
    assert owner.client.post(f"{base}/artifacts", json={"name": "x"}).status_code == 403          # no CSRF
    assert intruder.get(f"{base}/artifacts").status_code == 404 and intruder.get(f"{base}/timeline").status_code == 404
    assert art(intruder, pid, content_text="forged").status_code == 404
    assert intruder.post(f"{base}/artifacts/{artifact['id']}/links", json={"requirement_keys": ["R1"]}).status_code == 404
    own = make_project(intruder)
    freeze(intruder, own)
    assert intruder.post(f"/api/v1/projects/{own}/artifacts/{artifact['id']}/links",
                         json={"requirement_keys": ["R1"]}).status_code == 404                    # another project's artifact


# --- immutability --------------------------------------------------------------------------------------------------------------------------

def test_artifacts_and_links_are_append_only(app, seller, pid):
    art(seller, pid, content_text="x", requirement_keys=["R1"])
    models.Base.metadata.create_all(app.state.engine)
    with DbSession(app.state.engine) as db:
        for model, field in ((EvidenceArtifact, "sha256"), (ArtifactLink, "requirement_key")):
            setattr(db.scalars(select(model)).first(), field, "tampered")
            with pytest.raises(AppendOnlyError):
                db.flush()
            db.rollback()
            db.delete(db.scalars(select(model)).first())
            with pytest.raises(AppendOnlyError):
                db.flush()
            db.rollback()
        for table in ("evidence_artifacts", "artifact_links"):
            with pytest.raises(Exception):
                db.execute(text(f"UPDATE {table} SET id = id || 'x'"))
            db.rollback()
            with pytest.raises(Exception):
                db.execute(text(f"DELETE FROM {table}"))
            db.rollback()


# --- timeline --------------------------------------------------------------------------------------------------------------------------------

def test_the_timeline_is_chronological_by_server_time_and_keeps_both_clocks(seller):
    pid = make_project(seller)
    freeze(seller, pid)
    propose(seller, pid)
    art(seller, pid, content_text="one", asserted_at="2001-01-01T00:00:00+00:00", requirement_keys=["R1"])
    art(seller, pid, name="b.zip", content_text="two")
    items = seller.get(f"/api/v1/projects/{pid}/timeline").json()
    assert [i["at"] for i in items] == sorted(i["at"] for i in items)
    types = [i["event_type"] for i in items]
    assert types.index("BASELINE_FROZEN") < types.index("CHANGE_PROPOSED") < types.index("ARTIFACT_REGISTERED")
    first = [i for i in items if i["event_type"] == "ARTIFACT_REGISTERED"][0]
    assert first["time_basis"] == "SERVER_RECORDED" and first["asserted_at"].startswith("2001-01-01")
    assert items.index(first) > types.index("CHANGE_PROPOSED")                 # a back-dated assertion does not reorder the timeline
    assert all(i["actor_role"] in ("SELLER", "SYSTEM", "BUYER_EMAIL_VERIFIED") for i in items)


def test_the_timeline_includes_scope_records_and_payments_and_never_another_projects_events(seller):
    pid, other = make_project(seller), make_project(seller, "Other")
    freeze(seller, pid)
    freeze(seller, other)
    art(seller, other, content_text="foreign")
    seller.post(f"/api/v1/projects/{pid}/invoices", json={"currency": "USD", "amount_minor": 80000, "recipient_email": "buyer@example.org"},
                headers={"Idempotency-Key": "timeline-key-1"})
    types = [i["event_type"] for i in seller.get(f"/api/v1/projects/{pid}/timeline").json()]
    assert "SCOPE_RECORD_ADDED" in types and "ARTIFACT_REGISTERED" not in types
    assert any(t.startswith("PAYMENT_") for t in types)


def test_linking_is_audited(seller, pid):
    body = art(seller, pid, content_text="x").json()
    seller.post(f"/api/v1/projects/{pid}/artifacts/{body['id']}/links", json={"requirement_keys": ["R1"]})
    events = [e["event_type"] for e in seller.get(f"/api/v1/projects/{pid}/audit-events").json()]
    assert events.count("ARTIFACT_REGISTERED") == 1 and events.count("ARTIFACT_LINKED") == 1
