import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as DbSession

from app.models import ScopeRecord, ScopeRecordImmutableError

ROUTES = [
    ("get", "/api/v1/projects"),
    ("post", "/api/v1/projects"),
    ("get", "/api/v1/projects/{pid}"),
    ("get", "/api/v1/projects/{pid}/scope-records"),
    ("post", "/api/v1/projects/{pid}/scope-records"),
    ("post", "/api/v1/projects/{pid}/invoices"),
    ("get", "/api/v1/projects/{pid}/payments"),
]


def make_project(seller, title="Landing page") -> dict:
    response = seller.post("/api/v1/projects", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("method,url", ROUTES)
def test_unauthenticated_access_is_rejected_everywhere(app, method, url):
    response = getattr(TestClient(app), method)(url.format(pid="any-id"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_create_and_read_project(seller):
    project = make_project(seller)
    assert project["status"] == "DRAFT" and project["title"] == "Landing page"
    detail = seller.get(f"/api/v1/projects/{project['id']}").json()
    assert detail["id"] == project["id"] and detail["payments"] == []
    assert [p["id"] for p in seller.get("/api/v1/projects").json()] == [project["id"]]


def test_client_supplied_owner_is_ignored(app, seller_factory):
    a, b = seller_factory("a@example.com"), seller_factory("b@example.com")
    victim_id = b.get("/api/v1/auth/me").json()["seller"]["id"]
    own_id = a.get("/api/v1/auth/me").json()["seller"]["id"]
    response = a.post("/api/v1/projects", json={"title": "mine", "seller_id": victim_id, "status": "PAID"})
    assert response.status_code == 201
    assert response.json()["seller_id"] == own_id
    assert response.json()["status"] == "DRAFT"
    assert b.get("/api/v1/projects").json() == []


def test_other_sellers_resources_return_404_not_403(seller_factory):
    a, b = seller_factory("a@example.com"), seller_factory("b@example.com")
    pid = make_project(a)["id"]
    unknown = b.get("/api/v1/projects/00000000-0000-0000-0000-000000000000")
    foreign = b.get(f"/api/v1/projects/{pid}")
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["error"]["message"] == unknown.json()["error"]["message"]
    scope = {"kind": "ORIGINAL", "source_label": "email", "content": "text"}
    invoice = {"currency": "USD", "amount_minor": 1000, "recipient_email": "buyer@example.org"}
    assert b.get(f"/api/v1/projects/{pid}/scope-records").status_code == 404
    assert b.post(f"/api/v1/projects/{pid}/scope-records", json=scope).status_code == 404
    assert b.post(f"/api/v1/projects/{pid}/invoices", json=invoice,
                  headers={"Idempotency-Key": "key-0123456789"}).status_code == 404
    assert b.get(f"/api/v1/projects/{pid}/payments").status_code == 404
    assert b.get("/api/v1/projects").json() == []


@pytest.mark.parametrize("body", [{}, {"title": ""}, {"title": "x" * 161}])
def test_project_title_is_validated(seller, body):
    assert seller.post("/api/v1/projects", json=body).status_code == 422


# --- scope records ----------------------------------------------------------------------

def add_scope(seller, pid, kind="ORIGINAL", content="Build a 5 page site.", label="signed-agreement.pdf"):
    return seller.post(f"/api/v1/projects/{pid}/scope-records",
                       json={"kind": kind, "source_label": label, "content": content})


def test_scope_records_are_stored_with_server_computed_hash_and_ordered(seller):
    pid = make_project(seller)["id"]
    first = add_scope(seller, pid, "ORIGINAL", "Build a 5 page site.")
    second = add_scope(seller, pid, "CHANGE_REQUEST", "Add a blog page.", "email 2026-03-02")
    assert first.status_code == second.status_code == 201
    body = first.json()
    assert body["source_sha256"] == hashlib.sha256(b"Build a 5 page site.").hexdigest()
    assert body["kind"] == "ORIGINAL" and body["project_id"] == pid and body["created_at"].endswith(("Z", "+00:00"))
    listed = seller.get(f"/api/v1/projects/{pid}/scope-records").json()
    assert [r["kind"] for r in listed] == ["ORIGINAL", "CHANGE_REQUEST"]


def test_client_cannot_supply_the_hash_or_id(seller):
    pid = make_project(seller)["id"]
    forged = {"kind": "ORIGINAL", "source_label": "x", "content": "text", "source_sha256": "0" * 64, "id": "chosen"}
    assert seller.post(f"/api/v1/projects/{pid}/scope-records", json=forged).status_code == 422


@pytest.mark.parametrize("payload", [
    {"kind": "BOGUS", "source_label": "x", "content": "c"},
    {"kind": "ORIGINAL", "source_label": "", "content": "c"},
    {"kind": "ORIGINAL", "source_label": "x", "content": ""},
    {"kind": "ORIGINAL", "source_label": "x", "content": "a" * 100_001},
    {"kind": "ORIGINAL", "source_label": "x", "content": "é" * 60_000},  # under 100k chars, over 100k bytes
])
def test_scope_record_input_is_validated(seller, payload):
    pid = make_project(seller)["id"]
    assert seller.post(f"/api/v1/projects/{pid}/scope-records", json=payload).status_code == 422


def test_no_route_can_modify_or_delete_scope_records(seller):
    pid = make_project(seller)["id"]
    rid = add_scope(seller, pid).json()["id"]
    for method in ("put", "patch", "delete"):
        response = getattr(seller.client, method)(f"/api/v1/projects/{pid}/scope-records/{rid}", headers=seller.headers)
        assert response.status_code in (404, 405)


def test_orm_refuses_to_update_or_delete_a_scope_record(app, seller):
    pid = make_project(seller)["id"]
    rid = add_scope(seller, pid).json()["id"]
    with DbSession(app.state.engine) as db:
        record = db.get(ScopeRecord, rid)
        record.content = "silently rewritten"
        with pytest.raises(ScopeRecordImmutableError):
            db.flush()
        db.rollback()
        db.delete(db.get(ScopeRecord, rid))
        with pytest.raises(ScopeRecordImmutableError):
            db.flush()


def test_database_triggers_block_raw_sql_history_rewrites(app, seller):
    pid = make_project(seller)["id"]
    rid = add_scope(seller, pid).json()["id"]
    with app.state.engine.connect() as conn:
        with pytest.raises(DBAPIError):
            conn.execute(text("UPDATE scope_records SET content = 'tampered' WHERE id = :i"), {"i": rid})
        conn.rollback()
        with pytest.raises(DBAPIError):
            conn.execute(text("DELETE FROM scope_records WHERE id = :i"), {"i": rid})
    assert seller.get(f"/api/v1/projects/{pid}/scope-records").json()[0]["content"] == "Build a 5 page site."
