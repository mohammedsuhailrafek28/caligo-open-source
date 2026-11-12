import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from app.models import Seller, SellerSession, utcnow
from tests.helpers import PASSWORD, login, register


def db_of(app):
    return DbSession(app.state.engine)


def test_register_stores_argon2id_hash_and_normalized_email(app):
    client = TestClient(app)
    assert register(client, "  Mixed.Case@Example.COM ").status_code == 201
    with db_of(app) as db:
        seller = db.scalars(select(Seller)).one()
    assert seller.email == "mixed.case@example.com"
    assert seller.password_hash.startswith("$argon2id$")
    assert PASSWORD not in seller.password_hash


def test_register_does_not_reveal_whether_email_exists(app):
    client = TestClient(app)
    first = register(client, "dup@example.com")
    second = register(client, "DUP@example.com", password="a completely different password")
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() == {"status": "registered"}
    with db_of(app) as db:
        assert len(db.scalars(select(Seller)).all()) == 1
    # the second (ignored) registration must not have replaced the original password
    login(client, "dup@example.com", PASSWORD)


@pytest.mark.parametrize("payload", [
    {"email": "not-an-email", "password": PASSWORD, "display_name": "x"},
    {"email": "a@example.com", "password": "short", "display_name": "x"},
    {"email": "a@example.com", "password": PASSWORD, "display_name": ""},
    {"email": "a@example.com", "password": PASSWORD, "display_name": "x", "is_admin": True},
])
def test_register_validates_input(app, payload):
    response = TestClient(app).post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422
    assert PASSWORD not in response.text  # validation errors never echo the input


def test_login_sets_hardened_cookies_and_stores_only_hashes(app):
    client = TestClient(app)
    register(client, "a@example.com")
    response = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert response.status_code == 200
    cookies = {h.split("=", 1)[0]: h for h in response.headers.get_list("set-cookie")}
    session_cookie, csrf_cookie = cookies["pem_session"], cookies["pem_csrf"]
    assert "httponly" in session_cookie.lower() and "httponly" not in csrf_cookie.lower()
    assert "samesite=lax" in session_cookie.lower() and "samesite=lax" in csrf_cookie.lower()
    assert "secure" not in session_cookie.lower()  # local HTTP development default
    raw_session = client.cookies.get("pem_session")
    with db_of(app) as db:
        row = db.scalars(select(SellerSession)).one()
    assert raw_session not in (row.token_hash, row.csrf_hash)
    assert response.json()["csrf_token"] not in (row.token_hash, row.csrf_hash)
    assert len(row.token_hash) == 64


def test_cookie_secure_flag_is_configurable(make_app):
    app = make_app(cookie_secure=True, cookie_samesite="strict")
    client = TestClient(app, base_url="https://testserver")
    register(client, "a@example.com")
    response = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    for header in response.headers.get_list("set-cookie"):
        assert "secure" in header.lower() and "samesite=strict" in header.lower()


def test_wrong_password_and_unknown_email_look_identical(app):
    client = TestClient(app)
    register(client, "a@example.com")
    wrong = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "wrong password!!"})
    unknown = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong password!!"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert "set-cookie" not in wrong.headers


def test_login_is_throttled_after_repeated_failures(make_app):
    app = make_app(login_max_failures=3)
    client = TestClient(app)
    register(client, "a@example.com")
    for _ in range(3):
        assert client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "wrong password!!"}).status_code == 401
    blocked = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"]
    assert blocked.json()["error"]["code"] == "too_many_attempts"


def test_me_requires_authentication_and_returns_the_seller(app, seller):
    assert TestClient(app).get("/api/v1/auth/me").status_code == 401
    body = seller.get("/api/v1/auth/me").json()
    assert body["seller"]["email"] == "seller@example.com"
    assert "password" not in str(body).lower()


def test_each_login_issues_a_fresh_session_token(app):
    client = TestClient(app)
    register(client, "a@example.com")
    login(client, "a@example.com")
    first = client.cookies.get("pem_session")
    login(client, "a@example.com")
    assert client.cookies.get("pem_session") != first


def test_session_expires(app, seller):
    assert seller.get("/api/v1/auth/me").status_code == 200
    with db_of(app) as db:
        db.execute(update(SellerSession).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert seller.get("/api/v1/auth/me").status_code == 401
    assert seller.get("/api/v1/projects").status_code == 401


def test_logout_revokes_the_session_server_side(app, seller):
    stolen_cookie = seller.client.cookies.get("pem_session")
    csrf = seller.csrf
    assert seller.post("/api/v1/auth/logout").status_code == 204
    # replaying the old cookie must fail even though the client still holds it
    replay = TestClient(app)
    replay.cookies.set("pem_session", stolen_cookie)
    assert replay.get("/api/v1/auth/me").status_code == 401
    replay.cookies.set("pem_csrf", csrf)
    assert replay.post("/api/v1/projects", json={"title": "x"}, headers={"X-CSRF-Token": csrf}).status_code == 401
    with db_of(app) as db:
        assert db.scalars(select(SellerSession)).one().revoked_at is not None


def test_logout_requires_csrf(seller):
    assert seller.client.post("/api/v1/auth/logout").status_code == 403
    assert seller.get("/api/v1/auth/me").status_code == 200  # session untouched


# --- CSRF -----------------------------------------------------------------------------

def test_mutations_require_a_valid_csrf_token(seller):
    body = {"title": "Site build"}
    no_header = seller.client.post("/api/v1/projects", json=body)
    wrong_header = seller.client.post("/api/v1/projects", json=body, headers={"X-CSRF-Token": "x" * 43})
    assert no_header.status_code == wrong_header.status_code == 403
    assert no_header.json()["error"]["code"] == "csrf_failed"
    assert seller.post("/api/v1/projects", json=body).status_code == 201


def test_csrf_token_must_belong_to_this_session(app, seller_factory):
    a = seller_factory("a@example.com")
    b = seller_factory("b@example.com")
    # b's perfectly valid token, replayed with a's cookies (double-submit match, wrong session)
    a.client.cookies.set("pem_csrf", b.csrf)
    response = a.client.post("/api/v1/projects", json={"title": "x"}, headers={"X-CSRF-Token": b.csrf})
    assert response.status_code == 403


def test_cross_origin_mutations_are_rejected(seller):
    response = seller.post("/api/v1/projects", json={"title": "x"}, headers={"Origin": "https://evil.example.org"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    ok = seller.post("/api/v1/projects", json={"title": "x"}, headers={"Origin": "http://localhost:3000"})
    assert ok.status_code == 201


def test_login_rejects_cross_origin_posts(app):
    response = TestClient(app).post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD},
                                    headers={"Origin": "https://evil.example.org"})
    assert response.status_code == 403


# --- logging ---------------------------------------------------------------------------

def test_no_passwords_or_tokens_in_logs_or_error_bodies(app, caplog):
    caplog.set_level(logging.DEBUG)
    client = TestClient(app)
    secret_pw = "Unique-Test-Password-9f3a!"
    register(client, "log@example.com", password=secret_pw)
    client.post("/api/v1/auth/login", json={"email": "log@example.com", "password": "bad-guess-password-1"})
    response = client.post("/api/v1/auth/login", json={"email": "log@example.com", "password": secret_pw})
    session_token, csrf_token = client.cookies.get("pem_session"), response.json()["csrf_token"]
    client.post("/api/v1/projects", json={"title": "x"}, headers={"X-CSRF-Token": csrf_token})
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})
    logged = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records)
    for secret in (secret_pw, "bad-guess-password-1", session_token, csrf_token):
        assert secret not in logged
    assert "login_succeeded" in logged  # the audit trail itself exists


def test_redacting_filter_masks_credential_looking_pairs(caplog):
    caplog.set_level(logging.INFO, logger="app.audit")
    from app.errors import install_log_redaction
    install_log_redaction()
    logging.getLogger("app.audit").info("debug password=hunter2hunter2 token: abcdef Authorization=Bearer-xyz")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter2hunter2" not in text and "abcdef" not in text and "Bearer-xyz" not in text
    assert "[REDACTED]" in text


def test_error_responses_use_the_contract_envelope_with_request_id(app):
    response = TestClient(app).get("/api/v1/no-such-route")
    assert response.status_code == 404
    error = response.json()["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["request_id"] == response.headers["x-request-id"]


def test_unhandled_exceptions_use_the_error_envelope_without_leaking_details(app, caplog):
    @app.get("/api/v1/_boom")
    def boom():
        raise RuntimeError("super secret internal detail")

    response = TestClient(app, raise_server_exceptions=False).get("/api/v1/_boom")
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error" and set(error) == {"code", "message", "request_id"}
    assert "super secret internal detail" not in response.text
    assert "super secret internal detail" not in " ".join(r.getMessage() for r in caplog.records)


def test_registration_is_throttled_per_ip(make_app):
    client = TestClient(make_app(register_max_per_window=3))
    codes = [register(client, f"user{i}@example.com").status_code for i in range(5)]
    assert codes[:3] == [201, 201, 201] and codes[3:] == [429, 429]


def test_oversized_request_bodies_are_rejected_early(make_app):
    client = TestClient(make_app(max_body_bytes=2_000))
    response = client.post("/api/v1/auth/register", content=b"x" * 5_000, headers={"content-type": "application/json"})
    assert response.status_code == 413 and response.json()["error"]["code"] == "payload_too_large"


def test_login_purges_dead_sessions_of_that_seller(app, seller):
    seller.post("/api/v1/auth/logout")
    with db_of(app) as db:
        assert len(db.scalars(select(SellerSession)).all()) == 1  # revoked row
    login(seller.client, "seller@example.com")
    with db_of(app) as db:
        rows = db.scalars(select(SellerSession)).all()
    assert len(rows) == 1 and rows[0].revoked_at is None


def test_throttle_memory_is_bounded():
    from app.security import LoginThrottle
    throttle = LoginThrottle(5, 900)
    throttle.MAX_KEYS = 50
    for i in range(500):
        throttle.record_failure(f"email:{i}@example.com")
    assert len(throttle._failures) <= 50
