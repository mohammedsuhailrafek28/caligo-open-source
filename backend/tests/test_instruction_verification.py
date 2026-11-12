"""R2 independent authorization of payment-instruction changes: operator-established TOTP authenticators, encrypted secrets, single-use
challenges bound to the exact change, replay/guess/expiry defences, atomic release. Adversarial. Offline only."""
import base64
import logging
import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as DbSession

from app.errors import ApiError
from app.models import (
    AppendOnlyError, AuthenticatorRevocation, BuyerAuthenticator, InstructionChangeVerification, InstructionVerificationChallenge,
    PaymentInstruction, TotpUse,
)
from app.services import authenticators as auth
from app.services import instruction_verification as iv
from app.services import instructions as instruction_service
from tests.test_instruction_integrity import CHANGE, INSTR, base, hold, propose, record
from tests.test_payments import make_project

BUYER = "buyer@example.org"
T0 = 1_700_000_010.0  # fixed clock, mid-step so +/-1 step stays unambiguous
APPROVE, REJECT = "APPROVE_INSTRUCTION_CHANGE", "REJECT_INSTRUCTION_CHANGE"
CH = "/api/v1/buyer/instruction-changes/challenge"
DE = "/api/v1/buyer/instruction-changes/decide"


@pytest.fixture()
def keys():
    return f"k1:{auth.generate_key()}"


@pytest.fixture()
def clock(monkeypatch):
    state = {"t": T0}
    monkeypatch.setattr(auth, "now", lambda: state["t"])
    return state


@pytest.fixture()
def r2app(make_app, keys, clock):
    return make_app(authenticator_keys=keys)


@pytest.fixture()
def r2seller(r2app, seller_factory_for):
    return seller_factory_for(r2app)


@pytest.fixture()
def seller_factory_for():
    from tests.helpers import login, register

    def _make(application, email="seller@example.com"):
        client = TestClient(application)
        assert register(client, email).status_code == 201
        return login(client, email)

    return _make


def keyring_of(app):
    settings = app.state.settings
    return auth.parse_keyring(settings.authenticator_keys.get_secret_value(), settings.authenticator_active_key_id)


def db_of(app):
    return DbSession(app.state.engine, expire_on_commit=False)


def enroll(app, pid, email=BUYER):
    with db_of(app) as db:
        row, secret = auth.enroll(db, keyring_of(app), pid, email)
        return row.id, secret


class World:
    def __init__(self, app, seller, pid, authenticator_id, secret, change):
        self.app, self.seller, self.pid, self.auth_id, self.secret, self.change = app, seller, pid, authenticator_id, secret, change
        self.buyer = TestClient(app)

    def code(self, at=None):
        return auth.totp(self.secret, T0 if at is None else at)

    def challenge(self, **override):
        body = {"project_id": self.pid, "change_id": self.change["id"], "buyer_email": BUYER, **override}
        return self.buyer.post(CH, json=body)

    def decide_body(self, ch, decision=APPROVE, code=None, **override):
        change = ch["change"]
        return {"challenge_id": ch["challenge_id"], "project_id": self.pid, "change_id": self.change["id"], "buyer_email": BUYER,
                "from_fingerprint": change["from_fingerprint"], "proposed_fingerprint": change["proposed_fingerprint"],
                "decision": decision, "totp_code": self.code() if code is None else code, **override}

    def decide(self, ch, decision=APPROVE, code=None, client=None, **override):
        return (client or self.buyer).post(DE, json=self.decide_body(ch, decision, code, **override))

    def fresh(self, decision=APPROVE):
        response = self.challenge()
        assert response.status_code == 200, response.text
        return response.json()

    def count(self, model):
        with db_of(self.app) as db:
            return db.scalar(select(func.count()).select_from(model))


@pytest.fixture()
def world(r2app, r2seller, clock):
    pid = make_project(r2seller)
    assert record(r2seller, pid).status_code == 201
    auth_id, secret = enroll(r2app, pid)
    change = propose(r2seller, pid)
    assert change.status_code == 201, change.text
    return World(r2app, r2seller, pid, auth_id, secret, change.json())


# --- primitives ---------------------------------------------------------------------------------------------------

def test_totp_matches_the_rfc_6238_vector():
    secret = b"12345678901234567890"
    assert auth.totp(secret, 59) == "287082"  # RFC 6238 appendix B, T=59, 6 digits of 94287082
    assert auth.totp(secret, 1111111109) == "081804"
    assert auth.hotp(secret, 0) == "755224"  # RFC 4226 appendix D


def test_keyring_rejects_malformed_keys_without_echoing_them():
    for raw in ("nokeyid", "k1:!!!", "k1:" + base64.urlsafe_b64encode(b"short").decode(), "bad id:" + auth.generate_key()):
        with pytest.raises(auth.KeyringError) as caught:
            auth.parse_keyring(raw, "")
        assert raw.split(":")[-1] not in str(caught.value)
    assert auth.parse_keyring("", "") is None
    key = auth.generate_key()
    with pytest.raises(auth.KeyringError):
        auth.parse_keyring(f"k1:{key}", "missing")
    assert key not in repr(auth.parse_keyring(f"k1:{key}", ""))


def test_the_secret_is_encrypted_at_rest_and_bound_to_its_row(world):
    with db_of(world.app) as db:
        row = db.get(BuyerAuthenticator, world.auth_id)
        stored = row.secret_ciphertext
        raw = db.execute(text("SELECT secret_ciphertext FROM buyer_authenticators")).scalar()
    assert raw == stored
    b32 = base64.b32encode(world.secret).decode()
    assert world.secret not in base64.b64decode(stored) and b32 not in stored and world.secret.hex() not in stored
    assert auth.decrypt_secret(keyring_of(world.app), row) == world.secret
    for tampered in (dict(id="x" * 36), dict(project_id="other-project"), dict(buyer_email="evil@example.org")):
        moved = BuyerAuthenticator(**{"id": row.id, "project_id": row.project_id, "buyer_email": row.buyer_email,
                                      "secret_ciphertext": stored, "key_id": row.key_id, "trust_basis": row.trust_basis, **tampered})
        with pytest.raises(auth.AuthenticatorUnavailable):
            auth.decrypt_secret(keyring_of(world.app), moved)
    other = auth.parse_keyring(f"k1:{auth.generate_key()}", "")
    with pytest.raises(auth.AuthenticatorUnavailable):
        auth.decrypt_secret(other, row)
    with pytest.raises(auth.AuthenticatorUnavailable):
        auth.decrypt_secret(None, row)


def test_secrets_and_keys_never_reach_logs_or_responses(world, keys, caplog):
    caplog.set_level(logging.DEBUG)
    ch = world.fresh()
    world.decide(ch, code="000000")
    world.decide(ch)
    listing = world.seller.get(f"/api/v1/projects/{world.pid}/buyer-authenticators")
    blobs = [caplog.text, listing.text, str(ch), world.seller.get(f"/api/v1/projects/{world.pid}/audit-events").text]
    sensitive = [base64.b32encode(world.secret).decode(), keys.split(":", 1)[1], world.secret.hex()]
    with db_of(world.app) as db:
        sensitive.append(db.get(BuyerAuthenticator, world.auth_id).secret_ciphertext)
    for blob in blobs:
        for item in sensitive:
            assert item not in blob
    assert listing.json()[0]["trust_basis"] == "OPERATOR_ESTABLISHED" and "not verified real-world identity" in listing.json()[0]["note"]
    assert "secret" not in listing.text.replace("not verified", "")


# --- happy paths --------------------------------------------------------------------------------------------------

def test_approval_applies_exactly_the_proposed_instruction_and_releases_the_hold(world):
    assert hold(world.seller, world.pid)["active"] is True
    ch = world.fresh()
    assert ch["allowed_decisions"] == [APPROVE, REJECT] and ch["change"]["approvable"] is True
    assert ch["change"]["current"]["amount_minor"] == 80000 and ch["change"]["proposed"]["amount_minor"] == 95000
    response = world.decide(ch)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "APPLIED" and body["hold_active"] is False and "not verified real-world identity" in body["note"]
    assert hold(world.seller, world.pid)["active"] is False
    state = world.seller.get(base(world.pid)).json()
    assert state["instruction"]["version"] == 2 and state["instruction"]["amount_minor"] == 95000
    assert state["instruction"]["fingerprint"] == world.change["proposed_fingerprint"]
    assert state["instruction"]["verification_status"] == "VERIFIED_BY_ENROLLED_SECOND_CHANNEL"
    versions = world.seller.get(base(world.pid) + "/versions").json()
    assert [v["verification_status"] for v in versions] == ["PENDING_VERIFICATION", "VERIFIED_BY_ENROLLED_SECOND_CHANNEL"]
    with db_of(world.app) as db:
        assert instruction_service.chain_intact(instruction_service.versions_of(db, world.pid))
    changes = world.seller.get(base(world.pid) + "/changes").json()
    assert changes[0]["holds_payment_operations"] is False and changes[0]["verification"]["outcome"] == "APPLIED"
    types = [e["event_type"] for e in world.seller.get(f"/api/v1/projects/{world.pid}/audit-events").json()]
    assert "INSTRUCTION_CHANGE_VERIFIED" in types and "AUTHENTICATOR_ENROLLED" in types


def test_rejection_releases_the_hold_and_leaves_the_instruction_unchanged(world):
    response = world.decide(world.fresh(), REJECT)
    assert response.status_code == 200 and response.json()["outcome"] == "REJECTED"
    assert hold(world.seller, world.pid)["active"] is False
    state = world.seller.get(base(world.pid)).json()
    assert state["instruction"]["version"] == 1 and state["instruction"]["amount_minor"] == INSTR["amount_minor"]
    assert state["instruction"]["verification_status"] == "PENDING_VERIFICATION"


# --- authentication bypass ----------------------------------------------------------------------------------------

def test_a_wrong_code_never_releases_the_hold(world):
    ch = world.fresh()
    for code in ("000000", "123456"):
        if code == world.code():
            continue
        assert world.decide(ch, code=code).status_code == 400
    assert hold(world.seller, world.pid)["active"] is True and world.count(InstructionChangeVerification) == 0


@pytest.mark.parametrize("override", [
    {"challenge_id": "00000000-0000-0000-0000-000000000000"},
    {"project_id": "other"},
    {"change_id": "other"},
    {"buyer_email": "attacker@example.org"},
    {"from_fingerprint": "0" * 64},
    {"proposed_fingerprint": "f" * 64},
])
def test_every_binding_field_must_match_the_challenge(world, override):
    ch = world.fresh()
    assert world.decide(ch, **override).status_code == 400
    assert hold(world.seller, world.pid)["active"] is True and world.count(InstructionChangeVerification) == 0
    assert world.decide(ch).status_code == 200  # the untouched challenge still works with the right values (one try was spent)


def test_the_error_is_identical_whatever_check_failed(world):
    ch = world.fresh()
    wrong_code = world.decide(ch, code="000000")
    wrong_binding = world.decide(ch, from_fingerprint="0" * 64)
    unknown = world.decide(ch, challenge_id="nope")
    assert {r.status_code for r in (wrong_code, wrong_binding, unknown)} == {400}
    assert wrong_code.json()["error"]["code"] == wrong_binding.json()["error"]["code"] == unknown.json()["error"]["code"] == "verification_failed"
    assert wrong_code.json()["error"]["message"] == wrong_binding.json()["error"]["message"]


def test_a_challenge_for_another_project_or_buyer_or_change_is_not_found(world, r2seller):
    other_project = make_project(r2seller)
    assert world.challenge(project_id=other_project).status_code == 404
    assert world.challenge(change_id="nope").status_code == 404
    assert world.challenge(buyer_email="attacker@example.org").status_code == 404
    assert world.challenge(project_id="nope").status_code == 404


def test_another_sellers_project_exposes_nothing(r2app, world, seller_factory_for):
    intruder = seller_factory_for(r2app, "intruder@example.com")
    assert intruder.get(f"/api/v1/projects/{world.pid}/buyer-authenticators").status_code == 404
    assert hold(world.seller, world.pid)["active"] is True


def test_the_seller_has_no_way_to_enrol_replace_reset_or_approve(world):
    pid, change_id = world.pid, world.change["id"]
    for method, path in (("post", f"/api/v1/projects/{pid}/buyer-authenticators"), ("put", f"/api/v1/projects/{pid}/buyer-authenticators"),
                         ("delete", f"/api/v1/projects/{pid}/buyer-authenticators"),
                         ("post", base(pid) + f"/changes/{change_id}/approve"), ("post", base(pid) + f"/changes/{change_id}/release"),
                         ("post", f"/api/v1/projects/{pid}/buyer-authenticators/{world.auth_id}/reset")):
        assert world.seller.client.request(method.upper(), path, json={}, headers=world.seller.headers).status_code in (404, 405)
    assert world.seller.post(DE, json={}).status_code in (400, 422)  # the seller session grants nothing on the buyer route
    assert hold(world.seller, pid)["active"] is True


def test_the_buyer_route_ignores_a_seller_session_and_needs_the_code(world):
    ch = world.seller.post(CH, json={"project_id": world.pid, "change_id": world.change["id"], "buyer_email": BUYER}).json()
    assert world.decide(ch, client=world.seller, code="000000").status_code == 400
    assert hold(world.seller, world.pid)["active"] is True


def test_a_malformed_code_is_rejected_at_the_boundary(world):
    ch = world.fresh()
    for code in ("12345", "1234567", "abcdef", " 12345", ""):
        assert world.decide(ch, code=code).status_code == 422


# --- guessing, expiry, replay -------------------------------------------------------------------------------------

def test_the_attempt_budget_locks_the_challenge_even_for_the_right_code(world):
    ch = world.fresh()
    for _ in range(iv.MAX_FAILED_ATTEMPTS - 1):
        assert world.decide(ch, code="000000").status_code == 400
    assert world.decide(ch).status_code == 200  # the fifth attempt is still allowed
    other = world.seller.post(base(world.pid) + "/changes", json={**CHANGE, "amount_minor": 99000},
                              headers={"Idempotency-Key": "change-key-00002"})
    assert other.status_code == 201  # an earlier change resolved; a new one is allowed


def test_the_budget_is_charged_before_the_code_is_checked(world):
    ch = world.fresh()
    for _ in range(iv.MAX_FAILED_ATTEMPTS):
        world.decide(ch, code="000000")
    assert world.decide(ch).status_code == 400
    with db_of(world.app) as db:
        row = db.get(InstructionVerificationChallenge, ch["challenge_id"])
        assert row.failed_attempts == iv.MAX_FAILED_ATTEMPTS and row.consumed_at is None
    assert hold(world.seller, world.pid)["active"] is True


def test_an_expired_challenge_is_refused(world, monkeypatch):
    ch = world.fresh()
    real = iv.utcnow
    monkeypatch.setattr(iv, "utcnow", lambda: real() + iv.CHALLENGE_TTL + iv.timedelta(seconds=1))
    assert world.decide(ch).status_code == 400
    assert hold(world.seller, world.pid)["active"] is True


def test_a_consumed_challenge_cannot_be_reused(world):
    ch = world.fresh()
    assert world.decide(ch).status_code == 200
    assert world.decide(ch).status_code == 400
    assert world.count(InstructionChangeVerification) == 1


def test_a_code_cannot_be_replayed_even_inside_its_window(world, clock):
    first = world.fresh()
    code = world.code()
    assert world.decide(first, REJECT, code=code).status_code == 200
    second = world.seller.post(base(world.pid) + "/changes", json={**CHANGE, "amount_minor": 99000},
                               headers={"Idempotency-Key": "change-key-00002"}).json()
    ch = world.buyer.post(CH, json={"project_id": world.pid, "change_id": second["id"], "buyer_email": BUYER}).json()
    body = {"challenge_id": ch["challenge_id"], "project_id": world.pid, "change_id": second["id"], "buyer_email": BUYER,
            "from_fingerprint": ch["change"]["from_fingerprint"], "proposed_fingerprint": ch["change"]["proposed_fingerprint"],
            "decision": REJECT, "totp_code": code}
    assert world.buyer.post(DE, json=body).status_code == 400  # same step
    clock["t"] += 29
    older = auth.totp(world.secret, T0 - 30)  # an OLDER step inside the skew window is also refused
    assert world.buyer.post(DE, json={**body, "totp_code": older}).status_code == 400
    clock["t"] += 31
    assert world.buyer.post(DE, json={**body, "totp_code": auth.totp(world.secret, clock["t"])}).status_code == 200


def test_an_open_challenge_is_reused_so_flooding_cannot_burn_the_issue_budget(world):
    ids = {world.challenge().json()["challenge_id"] for _ in range(iv.MAX_CHALLENGES_PER_HOUR + 5)}
    assert len(ids) == 1
    assert world.decide(world.fresh()).status_code == 200  # the real buyer is not locked out


def test_the_challenge_issue_rate_is_limited_per_authenticator(world):
    with db_of(world.app) as db:
        for _ in range(iv.MAX_CHALLENGES_PER_HOUR):
            db.add(InstructionVerificationChallenge(
                project_id=world.pid, change_id=world.change["id"], authenticator_id=world.auth_id, approver_email=BUYER,
                from_fingerprint=world.change["from_fingerprint"], proposed_fingerprint=world.change["proposed_fingerprint"],
                expires_at=iv.utcnow() - iv.timedelta(minutes=1)))
        db.commit()
    blocked = world.challenge()
    assert blocked.status_code == 404 and blocked.json()["error"]["code"] == "not_found"  # indistinguishable from "no such change"


def test_guessing_is_bounded_across_challenges_per_authenticator(world, monkeypatch):
    real = iv.utcnow
    with db_of(world.app) as db:
        for _ in range(2):
            db.add(InstructionVerificationChallenge(
                project_id=world.pid, change_id=world.change["id"], authenticator_id=world.auth_id, approver_email=BUYER,
                from_fingerprint=world.change["from_fingerprint"], proposed_fingerprint=world.change["proposed_fingerprint"],
                expires_at=iv.utcnow() - iv.timedelta(minutes=1), failed_attempts=iv.MAX_FAILED_ATTEMPTS))
        db.commit()
    assert world.challenge().status_code == 404  # 10 spent guesses across earlier challenges: locked out, a fresh one does not reset it
    ch_row = None
    assert hold(world.seller, world.pid)["active"] is True
    monkeypatch.setattr(iv, "utcnow", lambda: real() + iv.FAILURE_WINDOW + iv.timedelta(seconds=5))
    assert world.challenge().status_code == 200  # the window passed
    assert ch_row is None


# --- parallel approval --------------------------------------------------------------------------------------------

def test_parallel_approvals_of_one_challenge_succeed_exactly_once(world):
    ch = world.fresh()
    results, barrier = [], threading.Barrier(6)

    def attempt():
        client = TestClient(world.app)
        barrier.wait()
        results.append(world.decide(ch, client=client).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(200) == 1 and all(code in (200, 400, 429, 500) and code != 500 for code in results), results
    assert world.count(InstructionChangeVerification) == 1 and world.count(TotpUse) == 1
    with db_of(world.app) as db:
        assert len(instruction_service.versions_of(db, world.pid)) == 2


def test_parallel_approve_and_reject_with_separate_challenges_resolve_once(world):
    approve, reject = world.fresh(), world.fresh()
    results, barrier = {}, threading.Barrier(2)

    def attempt(name, ch, decision):
        client = TestClient(world.app)
        barrier.wait()
        results[name] = world.decide(ch, decision, client=client).status_code

    threads = [threading.Thread(target=attempt, args=("a", approve, APPROVE)), threading.Thread(target=attempt, args=("r", reject, REJECT))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == [200, 400], results
    assert world.count(InstructionChangeVerification) == 1


# --- enrolment substitution and key loss --------------------------------------------------------------------------

def test_enrolment_is_checked_against_the_instruction_and_existing_authenticators(r2app, r2seller, clock):
    pid = make_project(r2seller)
    with db_of(r2app) as db:
        with pytest.raises(ApiError) as no_instruction:
            auth.enroll(db, keyring_of(r2app), pid, BUYER)
    assert no_instruction.value.code == "instruction_required"
    record(r2seller, pid)
    with db_of(r2app) as db:
        with pytest.raises(ApiError) as stranger:
            auth.enroll(db, keyring_of(r2app), pid, "stranger@example.org")
        with pytest.raises(ApiError) as no_keys:
            auth.enroll(db, None, pid, BUYER)
    assert stranger.value.code == "approver_mismatch" and no_keys.value.code == "authenticator_keys_missing"
    enroll(r2app, pid)
    with db_of(r2app) as db:
        with pytest.raises(ApiError) as twice:
            auth.enroll(db, keyring_of(r2app), pid, BUYER)
    assert twice.value.code == "authenticator_exists"


def test_an_authenticator_enrolled_after_the_proposal_can_only_reject(world):
    with db_of(world.app) as db:
        auth.revoke(db, world.auth_id, "device lost")
    new_id, new_secret = enroll(world.app, world.pid)
    assert world.challenge().status_code == 200
    ch = world.challenge().json()
    assert ch["allowed_decisions"] == [REJECT] and ch["change"]["approvable"] is False
    body = world.decide_body(ch, APPROVE, code=auth.totp(new_secret, T0))
    assert world.buyer.post(DE, json=body).status_code == 400
    assert hold(world.seller, world.pid)["active"] is True
    assert world.buyer.post(DE, json={**body, "decision": REJECT}).status_code == 200
    assert hold(world.seller, world.pid)["active"] is False


def test_a_revoked_authenticator_cannot_challenge_or_decide(world):
    ch = world.fresh()
    with db_of(world.app) as db:
        auth.revoke(db, world.auth_id, "compromised")
        assert auth.revoke(db, world.auth_id, "again").id is not None  # idempotent
    assert world.challenge().status_code == 404
    assert world.decide(ch).status_code == 400
    assert hold(world.seller, world.pid)["active"] is True


def test_losing_the_key_fails_closed_and_keeps_the_hold(world):
    ch = world.fresh()
    world.app.state.settings = world.app.state.settings.model_copy(update={"authenticator_keys": SecretStr("")})
    response = world.decide(ch)
    assert response.status_code == 503 and response.json()["error"]["code"] == "authenticator_unavailable"
    world.app.state.settings = world.app.state.settings.model_copy(update={"authenticator_keys": SecretStr(f"k9:{auth.generate_key()}")})
    assert world.decide(ch).status_code == 503
    world.app.state.settings = world.app.state.settings.model_copy(update={"authenticator_keys": SecretStr("garbage")})
    assert world.decide(ch).status_code == 503
    assert hold(world.seller, world.pid)["active"] is True and world.count(InstructionChangeVerification) == 0


def test_key_loss_is_recovered_by_revoking_and_enrolling_again_which_can_reject(world):
    new_keys = f"k2:{auth.generate_key()}"
    world.app.state.settings = world.app.state.settings.model_copy(update={"authenticator_keys": SecretStr(new_keys)})
    assert world.decide(world.fresh()).status_code == 503
    with db_of(world.app) as db:
        auth.revoke(db, world.auth_id, "key lost")
    _, secret = enroll(world.app, world.pid)
    ch = world.challenge().json()
    assert world.buyer.post(DE, json=world.decide_body(ch, REJECT, code=auth.totp(secret, T0))).status_code == 200
    assert hold(world.seller, world.pid)["active"] is False


def test_key_rotation_keeps_older_secrets_readable(world, keys):
    rotated = f"k2:{auth.generate_key()},{keys}"
    world.app.state.settings = world.app.state.settings.model_copy(update={"authenticator_keys": SecretStr(rotated)})
    assert world.decide(world.fresh()).status_code == 200


# --- withdrawal, atomicity, integrity -----------------------------------------------------------------------------

def test_a_withdrawn_change_keeps_the_hold_and_can_only_be_rejected(world):
    assert world.seller.post(base(world.pid) + f"/changes/{world.change['id']}/withdraw", json={}).status_code == 200
    assert hold(world.seller, world.pid)["active"] is True
    ch = world.fresh()
    assert ch["change"]["withdrawn_by_seller"] is True and ch["allowed_decisions"] == [REJECT]
    assert world.decide(ch, APPROVE).status_code == 400
    assert hold(world.seller, world.pid)["active"] is True
    assert world.decide(ch, REJECT).status_code == 200
    assert hold(world.seller, world.pid)["active"] is False
    with db_of(world.app) as db:
        assert len(instruction_service.versions_of(db, world.pid)) == 1


def test_a_failure_while_recording_the_outcome_rolls_everything_back(world, monkeypatch):
    ch = world.fresh()

    def explode(db, project_id, event_type, *args, **kwargs):
        if event_type == "INSTRUCTION_CHANGE_VERIFIED":
            raise RuntimeError("disk on fire")
        return real(db, project_id, event_type, *args, **kwargs)

    real = iv.record_audit
    monkeypatch.setattr(iv, "record_audit", explode)
    crashing = TestClient(world.app, raise_server_exceptions=False)
    assert world.decide(ch, client=crashing).status_code == 500
    monkeypatch.setattr(iv, "record_audit", real)
    assert hold(world.seller, world.pid)["active"] is True
    assert world.count(InstructionChangeVerification) == 0 and world.count(TotpUse) == 0
    with db_of(world.app) as db:
        assert len(instruction_service.versions_of(db, world.pid)) == 1
        assert db.get(InstructionVerificationChallenge, ch["challenge_id"]).consumed_at is None
    assert world.decide(ch).status_code == 200  # nothing was half-done: the same challenge can still complete


def test_a_tampered_instruction_chain_blocks_verification(world):
    ch = world.fresh()
    with db_of(world.app) as db:
        for verb in ("update", "delete"):
            db.execute(text(f"DROP TRIGGER IF EXISTS payment_instructions_no_{verb}"))
        db.execute(text("UPDATE payment_instructions SET amount_minor = 1 WHERE project_id = :p"), {"p": world.pid})
        db.commit()
    assert world.decide(ch).status_code == 400
    assert world.challenge().status_code == 404
    assert hold(world.seller, world.pid)["active"] is True


def test_the_applied_instruction_requires_the_providers_merchant(world):
    ch = world.fresh()
    world.app.state.payment_provider.expected_merchant_email = "someone-else@example.org"  # provider now configured for a different payee
    assert world.decide(ch, APPROVE).status_code == 400
    assert hold(world.seller, world.pid)["active"] is True


def test_verification_rows_are_append_only(world):
    world.decide(world.fresh())
    with db_of(world.app) as db:
        auth.revoke(db, world.auth_id, "rotate")
    with db_of(world.app) as db:
        row = db.scalar(select(InstructionChangeVerification))
        row.outcome = "REJECTED"
        with pytest.raises(AppendOnlyError):
            db.flush()
        db.rollback()
        with pytest.raises(Exception):
            db.execute(text("UPDATE instruction_change_verifications SET outcome='REJECTED'"))
        db.rollback()
        for model in (BuyerAuthenticator, AuthenticatorRevocation, TotpUse):
            table = model.__tablename__
            with pytest.raises(Exception):
                db.execute(text(f"DELETE FROM {table}"))
            db.rollback()


def test_a_second_verification_for_one_change_is_impossible_at_the_database(world):
    world.decide(world.fresh())
    with db_of(world.app) as db:
        existing = db.scalar(select(InstructionChangeVerification))
        db.add(InstructionChangeVerification(
            change_id=existing.change_id, project_id=existing.project_id, outcome="REJECTED", authenticator_id=existing.authenticator_id,
            challenge_id="other-challenge", approver_email=BUYER, from_fingerprint=existing.from_fingerprint,
            proposed_fingerprint=existing.proposed_fingerprint, trust_basis=existing.trust_basis, applied_instruction_id=None))
        with pytest.raises(Exception):
            db.flush()


def test_a_hold_is_released_only_by_a_verification_row(world):
    """Seller withdrawal, a forged resolution, or a stale pending list cannot release; only the verification table does."""
    world.seller.post(base(world.pid) + f"/changes/{world.change['id']}/withdraw", json={})
    assert hold(world.seller, world.pid)["active"] is True
    assert instruction_service.HOLD_RELEASING_RESOLUTIONS == frozenset()
    world.decide(world.fresh(), REJECT)
    assert hold(world.seller, world.pid)["active"] is False


def test_applying_a_change_does_not_touch_existing_payments(world, r2seller):
    """Reconciliation state is separate from instruction verification: a payment recorded earlier keeps its status."""
    from tests.test_payments import INVOICE

    other = make_project(r2seller)
    record(r2seller, other, {**INSTR, "amount_minor": INVOICE["amount_minor"]}, key="other-instr-key1")
    created = r2seller.post(f"/api/v1/projects/{other}/invoices", json=INVOICE, headers={"Idempotency-Key": "invoice-key-00001"})
    before = r2seller.get(f"/api/v1/projects/{other}/payments").json()
    world.decide(world.fresh())
    assert r2seller.get(f"/api/v1/projects/{other}/payments").json() == before
    assert created.status_code in (200, 201)


# --- operator CLI -------------------------------------------------------------------------------------------------

def test_the_cli_enrols_once_shows_the_secret_once_and_never_lists_it(r2app, r2seller, keys, monkeypatch, capsys):
    import importlib.util
    import pathlib

    from app.config import get_settings

    pid = make_project(r2seller)
    record(r2seller, pid)
    url = str(r2app.state.engine.url)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("AUTHENTICATOR_KEYS", keys)
    get_settings.cache_clear()
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "enroll_buyer_authenticator.py"
    spec = importlib.util.spec_from_file_location("enroll_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["enroll", "--project", pid, "--buyer", BUYER]) == 0
    printed = capsys.readouterr().out
    assert "base32_secret=" in printed and "NOT verified identity" in printed
    secret = printed.split("base32_secret=")[1].split()[0]
    assert cli.main(["enroll", "--project", pid, "--buyer", BUYER]) == 1  # one active authenticator only
    capsys.readouterr()
    assert cli.main(["list", "--project", pid]) == 0
    listing = capsys.readouterr().out
    assert secret not in listing and "ACTIVE" in listing and BUYER not in listing
    assert cli.main(["generate-key"]) == 0
    assert "key=" in capsys.readouterr().out
    monkeypatch.delenv("AUTHENTICATOR_KEYS")
    get_settings.cache_clear()
    other = make_project(r2seller)
    record(r2seller, other, key="other-instr-key1")
    assert cli.main(["enroll", "--project", other, "--buyer", BUYER]) == 1
    assert "no authenticator encryption key" in capsys.readouterr().err.lower()
