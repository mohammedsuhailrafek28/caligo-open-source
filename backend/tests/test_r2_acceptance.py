"""R2 acceptance: SQLite contention, cross-transaction invariants, enumeration, fail-closed behaviour, trust labelling, and the
R1 -> 0003 migration of a populated database. Offline only."""
import json
import sqlite3
import threading
import time

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as DbSession

from app.models import (
    AuthenticatorRevocation, BuyerAuthenticator, InstructionChangeVerification, InstructionVerificationChallenge, PaymentInstruction,
    Project, Seller, TotpUse,
)
from app.services import authenticators as auth
from app.services import instruction_verification as iv
from app.services import instructions as instruction_service
from tests.test_instruction_integrity import CHANGE, INSTR, base, hold, propose, record
from tests.test_instruction_verification import (  # noqa: F401  (fixtures)
    APPROVE, BUYER, CH, DE, REJECT, T0, World, clock, db_of, enroll, keys, keyring_of, r2app, r2seller, seller_factory_for, world,
)
from tests.test_migrations import config_for
from tests.test_payments import make_project


def busy(*_a, **_k):
    raise OperationalError("UPDATE", {}, Exception("database is locked"))


def assert_hold_intact(world):
    assert hold(world.seller, world.pid)["active"] is True
    assert world.count(InstructionChangeVerification) == 0 and world.count(TotpUse) == 0
    with db_of(world.app) as db:
        assert len(instruction_service.versions_of(db, world.pid)) == 1


# --- 1. SQLite OperationalError ----------------------------------------------------------------------------------

def test_a_database_error_after_the_checks_rolls_back_and_the_same_challenge_still_completes(world, monkeypatch):
    ch = world.fresh()
    real = iv.record_audit
    monkeypatch.setattr(iv, "record_audit", lambda db, pid, event, *a, **k: busy() if event == "INSTRUCTION_CHANGE_VERIFIED" else real(db, pid, event, *a, **k))
    response = world.decide(ch)
    assert response.status_code == 503 and response.json()["error"]["code"] == "verification_busy"
    assert response.headers["Retry-After"] == "2"
    monkeypatch.setattr(iv, "record_audit", real)
    assert_hold_intact(world)
    with db_of(world.app) as db:
        row = db.get(InstructionVerificationChallenge, ch["challenge_id"])
        assert row.consumed_at is None  # the consumption was rolled back with everything else
    assert world.decide(ch).status_code == 200


def test_a_database_error_while_charging_the_attempt_charges_nothing(world, monkeypatch):
    ch = world.fresh()
    monkeypatch.setattr(iv, "_locked_out", busy)
    response = world.decide(ch)
    assert response.status_code == 503 and response.json()["error"]["code"] == "verification_busy"
    monkeypatch.undo()
    with db_of(world.app) as db:
        assert db.get(InstructionVerificationChallenge, ch["challenge_id"]).failed_attempts == 0
    assert_hold_intact(world)


def test_a_database_error_while_issuing_a_challenge_is_busy_not_a_500(world, monkeypatch):
    monkeypatch.setattr(iv, "_locked_out", busy)
    response = world.challenge()
    assert response.status_code == 503 and response.json()["error"]["code"] == "verification_busy"
    monkeypatch.undo()
    assert_hold_intact(world)
    assert world.challenge().status_code == 200


def test_the_busy_answer_does_not_depend_on_the_code_or_the_binding(world, monkeypatch):
    ch = world.fresh()
    monkeypatch.setattr(iv, "_locked_out", busy)
    answers = [world.decide(ch, code="000000"), world.decide(ch), world.decide(ch, from_fingerprint="0" * 64)]
    assert {a.status_code for a in answers} == {503}
    assert len({json.dumps(a.json()["error"], sort_keys=True).replace(a.json()["error"].get("request_id", ""), "") for a in answers}) == 1


def test_real_sqlite_write_lock_contention_gives_a_closed_input_independent_answer(world):
    ch = world.fresh()
    path = world.app.state.engine.url.database
    blocker = sqlite3.connect(path, timeout=0.1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # another writer holds the database write lock
    try:
        started = time.monotonic()
        wrong = world.decide(ch, code="000000")
        right = world.decide(ch)
        issue = world.challenge()
        assert time.monotonic() - started < 60
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    for response in (wrong, right, issue):
        assert response.status_code == 503 and response.json()["error"]["code"] == "verification_busy", response.text
    assert wrong.json()["error"]["message"] == right.json()["error"]["message"]
    assert_hold_intact(world)
    assert world.decide(ch).status_code == 200  # once the lock is gone the right code works: nothing was spent or half-written
    assert hold(world.seller, world.pid)["active"] is False


# --- 2. cross-transaction behaviour --------------------------------------------------------------------------------

def test_parallel_challenge_requests_converge_on_one_open_challenge(world):
    ids, barrier = [], threading.Barrier(8)

    def request():
        barrier.wait()
        response = world.challenge()
        ids.append((response.status_code, response.json().get("challenge_id")))

    threads = [threading.Thread(target=request) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {code for code, _ in ids} <= {200, 503}
    issued = {cid for code, cid in ids if code == 200}
    assert len(issued) == 1 and world.count(InstructionVerificationChallenge) == 1


def test_a_mixed_storm_keeps_the_release_invariants(world):
    """Concurrent approve / reject / wrong-code attempts across several challenges: at most one release, and the rows agree."""
    challenges = [world.fresh()]
    results, barrier = [], threading.Barrier(9)
    plan = [(APPROVE, None)] * 3 + [(REJECT, None)] * 2 + [(APPROVE, "000000")] * 4

    def attempt(decision, code):
        barrier.wait()
        results.append(world.decide(challenges[0], decision, code=code).status_code)

    threads = [threading.Thread(target=attempt, args=item) for item in plan]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 500 not in results and results.count(200) <= 1
    with db_of(world.app) as db:
        verifications = list(db.scalars(select(InstructionChangeVerification)))
        versions = instruction_service.versions_of(db, world.pid)
        consumed = db.scalar(select(func.count()).select_from(InstructionVerificationChallenge).where(InstructionVerificationChallenge.consumed_at.is_not(None)))
        uses = db.scalar(select(func.count()).select_from(TotpUse))
        assert len(verifications) == consumed == uses == results.count(200)
        if verifications and verifications[0].outcome == "APPLIED":
            assert len(versions) == 2 and verifications[0].applied_instruction_id == versions[1].id
        else:
            assert len(versions) == 1
        assert instruction_service.chain_intact(versions)


def test_the_lockout_survives_across_requests_and_blocks_the_right_code(world):
    last = None
    for _ in range(2):  # a spent challenge is never reused, so the second round gets a fresh one
        last = world.fresh()
        for _ in range(iv.MAX_FAILED_ATTEMPTS):
            assert world.decide(last, code="000000").status_code == 400
    assert world.decide(last).status_code == 400  # right code, but the authenticator is locked out for the window
    assert world.challenge().status_code == 404
    assert_hold_intact(world)


def test_a_used_step_cannot_be_replayed_by_a_second_authenticator_row_after_re_enrolment(world, clock):
    """Re-enrolment creates a new secret and a new step history; the OLD secret's codes do not work against it."""
    old_code = world.code()
    with db_of(world.app) as db:
        auth.revoke(db, world.auth_id, "rotate")
    _, new_secret = enroll(world.app, world.pid)
    ch = world.challenge().json()
    assert world.decide(ch, REJECT, code=old_code).status_code == 400
    assert world.decide(ch, REJECT, code=auth.totp(new_secret, clock["t"])).status_code == 200


# --- 3. enumeration ------------------------------------------------------------------------------------------------

def shape(response):
    error = response.json()["error"]
    return response.status_code, error["code"], error["message"]


def test_challenge_refusals_are_indistinguishable(world, r2seller, monkeypatch):
    unknown_change = shape(world.challenge(change_id="nope"))
    wrong_email = shape(world.challenge(buyer_email="attacker@example.org"))
    other_project = shape(world.challenge(project_id=make_project(r2seller)))
    assert unknown_change == wrong_email == other_project and unknown_change[0] == 404

    # exhausted issue budget
    with db_of(world.app) as db:
        for _ in range(iv.MAX_CHALLENGES_PER_HOUR):
            db.add(InstructionVerificationChallenge(
                project_id=world.pid, change_id=world.change["id"], authenticator_id=world.auth_id, approver_email=BUYER,
                from_fingerprint=world.change["from_fingerprint"], proposed_fingerprint=world.change["proposed_fingerprint"],
                expires_at=iv.utcnow() - iv.timedelta(minutes=1), failed_attempts=0))
        db.commit()
    assert shape(world.challenge()) == unknown_change

    # revoked authenticator
    with db_of(world.app) as db:
        auth.revoke(db, world.auth_id, "x")
    assert shape(world.challenge()) == unknown_change


def test_a_locked_out_authenticator_looks_like_a_missing_one(world):
    with db_of(world.app) as db:
        for _ in range(2):
            db.add(InstructionVerificationChallenge(
                project_id=world.pid, change_id=world.change["id"], authenticator_id=world.auth_id, approver_email=BUYER,
                from_fingerprint=world.change["from_fingerprint"], proposed_fingerprint=world.change["proposed_fingerprint"],
                expires_at=iv.utcnow() - iv.timedelta(minutes=1), failed_attempts=iv.MAX_FAILED_ATTEMPTS))
        db.commit()
    assert shape(world.challenge()) == shape(world.challenge(change_id="nope"))


def test_operational_signals_are_logged_without_the_buyer_address(world, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="app.audit")
    with db_of(world.app) as db:
        for _ in range(2):
            db.add(InstructionVerificationChallenge(
                project_id=world.pid, change_id=world.change["id"], authenticator_id=world.auth_id, approver_email=BUYER,
                from_fingerprint=world.change["from_fingerprint"], proposed_fingerprint=world.change["proposed_fingerprint"],
                expires_at=iv.utcnow() - iv.timedelta(minutes=1), failed_attempts=iv.MAX_FAILED_ATTEMPTS))
        db.commit()
    world.challenge()
    text_ = caplog.text
    assert "verification_authenticator_locked_out" in text_ and world.auth_id in text_ and BUYER not in text_


# --- 4. fail closed: missing enrolment, lost key, revoked, corrupted -------------------------------------------------

def test_a_project_without_any_enrolment_can_never_be_released(r2app, r2seller, clock):
    pid = make_project(r2seller)
    assert record(r2seller, pid).status_code == 201
    change = propose(r2seller, pid).json()
    ghost = World(r2app, r2seller, pid, "none", b"x" * 20, change)
    assert ghost.challenge().status_code == 404
    forged = {"challenge_id": "x", "project_id": pid, "change_id": change["id"], "buyer_email": BUYER,
              "from_fingerprint": change["from_fingerprint"], "proposed_fingerprint": change["proposed_fingerprint"],
              "decision": REJECT, "totp_code": "123456"}
    assert ghost.buyer.post(DE, json=forged).status_code == 400
    assert r2seller.post(base(pid) + f"/changes/{change['id']}/withdraw", json={}).status_code == 200
    assert hold(r2seller, pid)["active"] is True  # still held: withdrawal never releases and nobody can verify


def test_a_corrupted_ciphertext_fails_closed(world):
    ch = world.fresh()
    with db_of(world.app) as db:
        for verb in ("update", "delete"):
            db.execute(text(f"DROP TRIGGER IF EXISTS buyer_authenticators_no_{verb}"))
        db.execute(text("UPDATE buyer_authenticators SET secret_ciphertext = 'AAAA' || substr(secret_ciphertext, 5)"))
        db.commit()
    response = world.decide(ch)
    assert response.status_code == 503 and response.json()["error"]["code"] == "authenticator_unavailable"
    assert_hold_intact(world)


def test_revoked_lost_key_and_unknown_key_id_all_fail_closed_together(world):
    ch = world.fresh()
    from pydantic import SecretStr

    world.app.state.settings = world.app.state.settings.model_copy(update={"authenticator_keys": SecretStr(f"other:{auth.generate_key()}")})
    assert world.decide(ch).status_code == 503  # the row's key id is not in the keyring
    with db_of(world.app) as db:
        auth.revoke(db, world.auth_id, "also revoked")
    assert world.decide(ch).status_code == 400  # revocation is checked before the key is touched
    assert world.challenge().status_code == 404
    assert_hold_intact(world)


# --- 6/7. trust labelling ---------------------------------------------------------------------------------------------

def test_a_seller_who_enrols_themselves_is_never_labelled_independent(r2app, r2seller, clock):
    """Seller == operator == buyer: the system cannot tell, so every surface keeps the OPERATOR_ESTABLISHED, not-independent wording."""
    pid = make_project(r2seller)
    self_mail = "seller@example.com"
    assert record(r2seller, pid, {**INSTR, "recipient_email": self_mail}).status_code == 201
    _, secret = enroll(r2app, pid, self_mail)
    change = propose(r2seller, pid, {**CHANGE, "recipient_email": self_mail}).json()
    me = World(r2app, r2seller, pid, "-", secret, change)
    ch = me.buyer.post(CH, json={"project_id": pid, "change_id": change["id"], "buyer_email": self_mail}).json()
    body = {"challenge_id": ch["challenge_id"], "project_id": pid, "change_id": change["id"], "buyer_email": self_mail,
            "from_fingerprint": ch["change"]["from_fingerprint"], "proposed_fingerprint": ch["change"]["proposed_fingerprint"],
            "decision": APPROVE, "totp_code": auth.totp(secret, T0)}
    decided = me.buyer.post(DE, json=body)
    assert decided.status_code == 200
    surfaces = [json.dumps(ch), decided.text, r2seller.get(f"/api/v1/projects/{pid}/buyer-authenticators").text,
                r2seller.get(base(pid)).text, r2seller.get(base(pid) + "/changes").text, r2seller.get(f"/api/v1/projects/{pid}/payment-hold").text]
    joined = " ".join(surfaces)
    assert "OPERATOR_ESTABLISHED" in joined and "not independent of the seller" in joined.replace("\\u2019", "'")
    assert "VERIFIED_BY_ENROLLED_SECOND_CHANNEL" in joined
    import re

    assert not re.search(r'"(?:INDEPENDENT|VERIFIED_INDEPENDENT[A-Z_]*|IDENTITY_VERIFIED)"', joined)
    assert "INDEPENDENT_VERIFICATION" not in joined
    audit = r2seller.get(f"/api/v1/projects/{pid}/audit-events").json()
    verified = [e for e in audit if e["event_type"] == "INSTRUCTION_CHANGE_VERIFIED"][0]
    assert verified["detail"]["real_world_identity_verified"] is False and verified["detail"]["trust_basis"] == "OPERATOR_ESTABLISHED"


def test_version_one_is_never_independently_verified_even_after_several_authorized_changes(world, clock):
    assert world.decide(world.fresh()).status_code == 200
    clock["t"] += 90
    second = world.seller.post(base(world.pid) + "/changes", json={**CHANGE, "amount_minor": 99000}, headers={"Idempotency-Key": "change-key-00002"}).json()
    ch = world.buyer.post(CH, json={"project_id": world.pid, "change_id": second["id"], "buyer_email": BUYER}).json()
    body = {"challenge_id": ch["challenge_id"], "project_id": world.pid, "change_id": second["id"], "buyer_email": BUYER,
            "from_fingerprint": ch["change"]["from_fingerprint"], "proposed_fingerprint": ch["change"]["proposed_fingerprint"],
            "decision": APPROVE, "totp_code": auth.totp(world.secret, clock["t"])}
    assert world.buyer.post(DE, json=body).status_code == 200
    versions = world.seller.get(base(world.pid) + "/versions").json()
    assert [v["verification_status"] for v in versions] == ["PENDING_VERIFICATION", "VERIFIED_BY_ENROLLED_SECOND_CHANNEL", "VERIFIED_BY_ENROLLED_SECOND_CHANNEL"]
    assert versions[0]["provenance"] == "SELLER_ENTERED"
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "R2_AUTHORIZATION.md").read_text(encoding="utf8")
    assert "Version 1 of every instruction is NOT independently verified" in doc


# --- 5/4. migration from a populated R1 database --------------------------------------------------------------------------

def test_a_populated_r1_database_migrates_to_0003_preserving_data_and_append_only_guarantees(tmp_path):
    from app.db import current_revision, head_revision, init_db, make_engine

    url = f"sqlite:///{tmp_path / 'r1.sqlite3'}"
    command.upgrade(config_for(url), "0002")
    engine = make_engine(url)
    assert current_revision(engine) == "0002"
    with DbSession(engine) as db:
        seller = Seller(email="keep@example.com", password_hash="x", display_name="Keep")
        db.add(seller)
        db.flush()
        project = Project(seller_id=seller.id, title="Populated")
        db.add(project)
        db.flush()
        context = {"original_scope_record_id": None, "original_scope_sha256": None, "baseline_id": None, "baseline_version": None,
                   "baseline_content_sha256": None}
        fingerprint = instruction_service.compute_fingerprint(project.id, "MOCK_PAYPAL", None, "buyer@example.org", "USD", 80000, context)
        db.add(PaymentInstruction(
            id="i1", project_id=project.id, version=1, provider="MOCK_PAYPAL", merchant_email=None, recipient_email="buyer@example.org",
            currency="USD", amount_minor=80000, context=context, fingerprint=fingerprint, previous_instruction_id=None,
            chain_sha256=instruction_service.compute_chain(None, fingerprint, 1, project.id), provenance="SELLER_ENTERED",
            idempotency_key="k" * 12, request_sha256="r" * 64))
        db.commit()
        project_id = project.id
    with engine.connect() as conn:
        before = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("sellers", "projects", "payment_instructions")}
        old_triggers = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    command.upgrade(config_for(url), "head")
    engine = make_engine(url)
    assert current_revision(engine) == head_revision() == "0008"
    new_tables = {"buyer_authenticators", "authenticator_revocations", "instruction_verification_challenges", "totp_uses",
                  "instruction_change_verifications"}
    assert new_tables <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        after = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in before}
        triggers = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    assert after == before and old_triggers <= triggers
    for table in ("buyer_authenticators", "authenticator_revocations", "totp_uses", "instruction_change_verifications"):
        assert {f"{table}_no_update", f"{table}_no_delete"} <= triggers
    assert "instruction_verification_challenges_no_update" not in triggers  # mutable by design (counters only)
    with DbSession(engine) as db:
        versions = instruction_service.versions_of(db, project_id)
        assert instruction_service.chain_intact(versions) and versions[0].fingerprint
        assert instruction_service.instruction_view(db, versions[0]).verification_status.value == "PENDING_VERIFICATION"
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("UPDATE payment_instructions SET amount_minor = 1"))
        db.rollback()
    init_db(engine)  # the migrated database is accepted as current
    # the migrated tables behave: enrolment, then the append-only guarantee on the new tables
    from app.services.authenticators import enroll, parse_keyring

    with DbSession(engine, expire_on_commit=False) as db:
        row, _ = enroll(db, parse_keyring(f"k1:{auth.generate_key()}", ""), project_id, "buyer@example.org")
        with pytest.raises(Exception, match="append-only"):
            db.execute(text("DELETE FROM buyer_authenticators"))
        db.rollback()
        assert db.get(BuyerAuthenticator, row.id) is not None


def test_downgrade_from_0003_removes_only_the_r2_objects(tmp_path):
    from app.db import make_engine

    url = f"sqlite:///{tmp_path / 'd.sqlite3'}"
    command.upgrade(config_for(url), "head")
    command.downgrade(config_for(url), "0002")
    engine = make_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert "payment_instructions" in tables and "buyer_authenticators" not in tables and "totp_uses" not in tables
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert not [n for n in names if n.startswith(("buyer_authenticators", "totp_uses", "instruction_change_verifications", "authenticator_revocations"))]
    assert "payment_instructions_no_update" in names


# --- environment verification script -----------------------------------------------------------------------------------

def test_the_environment_checker_flags_missing_and_borrowed_packages(tmp_path):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "verify_environment.py"
    spec = importlib.util.spec_from_file_location("verify_environment", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = {m: s for m, s, _ in module.check(prefix=tmp_path)}  # nothing lives under an empty prefix -> everything is OUTSIDE_ENV or MISSING
    assert rows["pytest"] == "OUTSIDE_ENV" and all(status in ("OUTSIDE_ENV", "MISSING") for status in rows.values())
    assert module.is_inside(Path(module.__file__).resolve(), Path(module.__file__).resolve().parent)


def test_a_successful_decision_is_not_counted_as_a_failed_guess(world, clock):
    assert world.decide(world.fresh(), REJECT).status_code == 200
    with db_of(world.app) as db:
        assert db.scalar(select(func.sum(InstructionVerificationChallenge.failed_attempts))) == 0


def test_a_database_error_while_handling_an_integrity_backstop_is_busy_not_a_500(world, monkeypatch):
    ch = world.fresh()
    monkeypatch.setattr(iv, "_record_outcome", lambda *a, **k: (_ for _ in ()).throw(__import__("sqlalchemy.exc", fromlist=["IntegrityError"]).IntegrityError("x", {}, Exception("dup"))))
    monkeypatch.setattr(iv, "_fail", lambda *a, **k: busy())
    response = world.decide(ch)
    assert response.status_code == 503 and response.json()["error"]["code"] == "verification_busy"
    monkeypatch.undo()
    assert_hold_intact(world)
