"""A verification row's mere existence must never release a payment-instruction hold; every binding is checked and anything off fails closed."""
import pytest
from sqlalchemy import select, text

from app.services import instructions
from tests.test_instruction_integrity import drop_triggers, hold, make_project, pid, propose, record  # noqa: F401 (fixtures)
from tests.test_instruction_verification import World, clock, db_of, keys, r2app, r2seller, seller_factory_for, world  # noqa: F401

UNVERIFIED = "UNVERIFIED_INSTRUCTION_CHANGE"


def _tamper(world, sql, **params):
    """Simulate corruption that bypasses every DB guard (triggers, CHECK, FOREIGN KEY) so the release predicate itself is what is tested.
    Test databases only; the guards are restored on the connection afterwards."""
    drop_triggers(world.app, "instruction_change_verifications", "instruction_verification_challenges", "buyer_authenticators",
                  "payment_instructions")
    with db_of(world.app) as db:
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(text("PRAGMA ignore_check_constraints=ON"))
        try:
            db.execute(text(sql), params)
            db.commit()
        finally:
            db.rollback()
            db.execute(text("PRAGMA ignore_check_constraints=OFF"))
            db.execute(text("PRAGMA foreign_keys=ON"))


def _releases(world) -> bool:
    with db_of(world.app) as db:
        return instructions.verification_releases(db, world.change["id"])


def _gate_open(world) -> bool:
    """The compare-and-set predicate used by payment operations: True means no unreleased change blocks the project."""
    with db_of(world.app) as db:
        return db.scalar(select(instructions.no_pending_change_clause(world.pid))) is True


def _assert_still_held(world):
    assert _releases(world) is False
    assert _gate_open(world) is False
    assert UNVERIFIED in str(hold(world.seller.client, world.pid))


def test_a_legitimate_approval_releases_the_hold(world):
    assert world.decide(world.fresh()).status_code == 200
    assert _releases(world) is True and _gate_open(world) is True
    assert UNVERIFIED not in str(hold(world.seller.client, world.pid))


def test_a_legitimate_rejection_releases_the_hold_without_applying_anything(world):
    assert world.decide(world.fresh(), decision="REJECT_INSTRUCTION_CHANGE").status_code == 200
    assert _releases(world) is True


def test_an_undecided_change_holds(world):
    _assert_still_held(world)


@pytest.mark.parametrize("sql", [
    "UPDATE instruction_change_verifications SET approver_email = 'someone-else@example.org'",
    "UPDATE instruction_change_verifications SET from_fingerprint = 'f' || from_fingerprint",
    "UPDATE instruction_change_verifications SET proposed_fingerprint = 'f' || proposed_fingerprint",
    "UPDATE instruction_change_verifications SET trust_basis = 'SELF_ASSERTED'",
    "UPDATE instruction_change_verifications SET outcome = 'BOGUS'",
    "UPDATE instruction_change_verifications SET outcome = 'REJECTED'",  # applied version still referenced
    "UPDATE instruction_change_verifications SET applied_instruction_id = NULL",  # APPROVED with nothing applied
    "UPDATE instruction_change_verifications SET challenge_id = 'no-such-challenge'",
    "UPDATE instruction_verification_challenges SET consumed_at = NULL",
    "UPDATE instruction_verification_challenges SET approver_email = 'someone-else@example.org'",
    "UPDATE instruction_verification_challenges SET proposed_fingerprint = 'f' || proposed_fingerprint",
    "UPDATE buyer_authenticators SET buyer_email = 'someone-else@example.org'",
    "UPDATE buyer_authenticators SET trust_basis = 'SELF_ASSERTED'",
    "UPDATE instruction_change_verifications SET authenticator_id = 'no-such-authenticator'",
    "UPDATE instruction_change_verifications SET project_id = 'no-such-project'",
])
def test_a_tampered_approval_does_not_release_the_hold(world, sql):
    assert world.decide(world.fresh()).status_code == 200
    assert _releases(world) is True
    _tamper(world, sql)
    _assert_still_held(world)


def test_a_verification_of_another_projects_change_does_not_release_this_one(world):
    assert world.decide(world.fresh()).status_code == 200
    other = make_project(world.seller)
    assert record(world.seller, other).status_code == 201
    foreign_change = propose(world.seller, other).json()["id"]
    _tamper(world, "UPDATE instruction_change_verifications SET change_id = :c", c=foreign_change)
    assert _releases(world) is False and _gate_open(world) is False


def test_an_applied_version_that_does_not_continue_the_instruction_does_not_release(world):
    assert world.decide(world.fresh()).status_code == 200
    _tamper(world, "UPDATE payment_instructions SET previous_instruction_id = NULL WHERE previous_instruction_id IS NOT NULL")
    _assert_still_held(world)
