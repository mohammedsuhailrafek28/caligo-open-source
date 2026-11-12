"""R1 hardening: withdrawal is history not release, the in-flight (reserved-before-hold) rule, claim-bound values, and _block hygiene."""
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import AuditEvent, InstructionChangeResolution, PaymentRecord, Project
from app.payment_provider import ProviderRateLimited
from app.services import instructions as instruction_service
from app.services import payments as payment_service
from tests.test_instruction_integrity import (
    CK, CHANGE, INSTR, INVOICE, base, db_of, drop_triggers, hold, invoice, make_project, payments, pid, propose, ready,  # noqa: F401
    record, send, sync,
)


def withdraw(seller, pid, change_id, note=""):
    return seller.post(base(pid) + f"/changes/{change_id}/withdraw", json={"note": note})


# --- A. withdrawal is recorded history, never a release --------------------------------------------------------------

def test_a_withdrawal_is_recorded_but_the_hold_stays_effective(app, seller, ready):
    change = propose(seller, ready).json()
    done = withdraw(seller, ready, change["id"], "typo")
    assert done.status_code == 200
    assert done.json()["resolution"] == "WITHDRAWN" and done.json()["holds_payment_operations"] is True
    assert withdraw(seller, ready, change["id"]).status_code == 200  # idempotent
    state = seller.get(base(ready)).json()
    assert state["hold"]["active"] is True and state["hold"]["reasons"] == ["UNVERIFIED_INSTRUCTION_CHANGE"]
    assert state["hold"]["pending_change_ids"] == [change["id"]]
    assert state["instruction"]["amount_minor"] == 80000 and state["versions"] == 1
    with db_of(app) as db:
        assert len(db.scalars(select(InstructionChangeResolution)).all()) == 1  # immutable history, exactly once
    events = [e["event_type"] for e in seller.get(f"/api/v1/projects/{ready}/audit-events").json()]
    assert events.count("INSTRUCTION_CHANGE_WITHDRAWN") == 1


def test_after_a_withdrawal_every_protected_operation_is_still_blocked(app, seller, ready):
    created = invoice(seller, ready).json()
    change = propose(seller, ready).json()
    withdraw(seller, ready, change["id"])
    assert send(seller, ready, created["id"]).json()["error"]["code"] == "payment_hold_active"
    other = make_project(seller)
    record(seller, other, key="other-instr-key2")
    propose(seller, other, key="other-change-key")
    withdraw(seller, other, seller.get(base(other) + "/changes").json()[0]["id"])
    assert invoice(seller, other, key="after-withdraw-01").json()["error"]["code"] == "payment_hold_active"
    again = propose(seller, ready, {**CHANGE, "amount_minor": 70000}, key="change-key-00002")
    assert again.status_code == 409 and again.json()["error"]["code"] == "change_pending"  # no way around it by re-proposing
    assert all(i.state.value == "DRAFT" for i in app.state.payment_provider._invoices.values())


def test_a_withdrawal_does_not_unblock_accepting_review_evidence(app, seller, ready):
    created = invoice(seller, ready).json()
    with db_of(app) as db:
        db.execute(text("UPDATE payment_records SET provider_status='NEEDS_REVIEW' WHERE id=:i"), {"i": created["id"]})
        db.commit()
    change = propose(seller, ready).json()
    withdraw(seller, ready, change["id"])
    body = {"resolution": "ACCEPT_PROVIDER_EVIDENCE", "note": "checked the dashboard manually"}
    blocked = seller.post(f"/api/v1/projects/{ready}/payments/{created['id']}/resolve-review", json=body)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "payment_hold_active"


def test_concurrent_withdrawals_record_one_resolution_and_never_release(app, seller, ready):
    change = propose(seller, ready).json()
    statuses: list[int] = []

    def attempt() -> None:
        client = TestClient(app)
        client.cookies.update(seller.client.cookies)
        statuses.append(client.post(base(ready) + f"/changes/{change['id']}/withdraw", json={}, headers=seller.headers).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert statuses == [200] * 6
    with db_of(app) as db:
        assert len(db.scalars(select(InstructionChangeResolution)).all()) == 1
        assert len(db.scalars(select(AuditEvent).where(AuditEvent.event_type == "INSTRUCTION_CHANGE_WITHDRAWN")).all()) == 1
    assert hold(seller, ready)["active"] is True


def test_only_a_withdrawn_resolution_can_exist_and_it_never_releases(app, seller, ready):
    """The database refuses any other outcome, and the set of releasing outcomes is empty until R2 (independent verification) exists."""
    assert instruction_service.HOLD_RELEASING_RESOLUTIONS == frozenset()
    change = propose(seller, ready).json()
    with db_of(app) as db:
        with pytest.raises(IntegrityError):
            db.execute(text("INSERT INTO instruction_change_resolutions (id, change_id, project_id, resolution, actor_role, note, created_at) "
                            "VALUES ('x', :c, :p, 'APPLIED', 'SELLER', '', '2026-01-01 00:00:00')"), {"c": change["id"], "p": ready})
        db.rollback()
    assert hold(seller, ready)["active"] is True


# --- C. in-flight rule: reserved before the hold -> may complete against the exact bound instruction -------------------

def _hold_after_reservation(app, monkeypatch, amount=1):
    """Commit a hold from another connection immediately after the create has durably reserved its record."""
    real = payment_service._provider_request

    def request_then_hold(project, rec):
        built = real(project, rec)  # reads the reserved record's own committed values
        with db_of(app) as other:
            current = instruction_service.effective_instruction(other, project.id)
            other.add(instruction_service.InstructionChangeRequest(
                project_id=project.id, from_instruction_id=current.id, from_fingerprint=current.fingerprint,
                recipient_email="attacker@example.net", currency="EUR", amount_minor=amount, proposed_fingerprint="f" * 64,
                reason="hold after reservation", provenance="SELLER_ENTERED", idempotency_key="inflight-key-01", request_sha256="r" * 64))
            other.commit()
        return built

    monkeypatch.setattr(payment_service, "_provider_request", request_then_hold)


def test_a_create_reserved_before_the_hold_completes_with_exactly_its_bound_values(app, seller, ready, monkeypatch):
    _hold_after_reservation(app, monkeypatch)
    created = invoice(seller, ready)
    assert created.status_code == 201, created.text
    sent = next(iter(app.state.payment_provider._requests.values()))  # what the provider was actually asked for
    assert (sent.currency, sent.amount_minor, sent.recipient_email) == ("USD", 80000, "buyer@example.org")
    with db_of(app) as db:
        rec, instruction = db.scalar(select(PaymentRecord)), instruction_service.effective_instruction(db, ready)
        assert rec.instruction_id == instruction.id and rec.instruction_fingerprint == instruction.fingerprint
        assert (rec.currency, rec.amount_minor, rec.recipient_email) == ("USD", 80000, "buyer@example.org")
    assert hold(seller, ready)["active"] is True
    # the hold applies to everything AFTER the reservation
    assert send(seller, ready, created.json()["id"]).json()["error"]["code"] == "payment_hold_active"


def _reserved_then_failed(app, seller, ready, monkeypatch):
    """Leave a durably RESERVED, unsent create (provider rate-limited once), exactly as a retryable failure does."""
    provider = app.state.payment_provider
    real = provider.create_invoice
    monkeypatch.setattr(provider, "create_invoice", lambda request: (_ for _ in ()).throw(ProviderRateLimited("slow down")))
    assert invoice(seller, ready).status_code == 503
    assert payments(seller, ready)[0]["operation_state"] == "RESERVED" and not provider._invoices
    monkeypatch.setattr(provider, "create_invoice", real)


def test_a_reserved_create_resumed_after_a_hold_completes_against_its_bound_instruction(app, seller, ready, monkeypatch):
    _reserved_then_failed(app, seller, ready, monkeypatch)
    propose(seller, ready)  # the hold arrives while the reserved create is waiting
    resumed = invoice(seller, ready)
    assert resumed.status_code in (200, 201), resumed.text
    sent = next(iter(app.state.payment_provider._requests.values()))
    assert (sent.currency, sent.amount_minor, sent.recipient_email) == ("USD", 80000, "buyer@example.org")
    assert len(payments(seller, ready)) == 1


def test_a_reserved_create_whose_values_were_altered_is_refused_and_never_reaches_the_provider(app, seller, ready, monkeypatch):
    _reserved_then_failed(app, seller, ready, monkeypatch)
    with db_of(app) as db:
        db.execute(text("UPDATE payment_records SET amount_minor = 5"))
        db.commit()
    blocked = invoice(seller, ready)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "instruction_mismatch"
    assert not app.state.payment_provider._invoices


def test_a_reserved_create_cannot_use_an_instruction_that_fails_its_integrity_check(app, seller, ready, monkeypatch):
    _reserved_then_failed(app, seller, ready, monkeypatch)
    propose(seller, ready)
    drop_triggers(app, "payment_instructions")
    with db_of(app) as db:
        db.execute(text("UPDATE payment_instructions SET amount_minor = 1"))
        db.commit()
    blocked = invoice(seller, ready)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "payment_hold_active"
    assert "INSTRUCTION_INTEGRITY_FAILURE" in hold(seller, ready)["reasons"] and not app.state.payment_provider._invoices


def test_the_claim_only_matches_when_the_stored_values_equal_the_bound_ones(app, seller, ready, monkeypatch):
    _reserved_then_failed(app, seller, ready, monkeypatch)
    with db_of(app) as checked, db_of(app) as attacker:
        rec = checked.scalar(select(PaymentRecord))
        _ = (rec.amount_minor, rec.recipient_email, rec.currency, rec.instruction_id)  # values the operation was checked against
        attacker.execute(text("UPDATE payment_records SET amount_minor = 5"))  # altered after the check, before the claim
        attacker.commit()
        assert payment_service._claim(checked, rec, ("RESERVED",), payment_service.Op.CREATING, enforce_hold=False) is False
    with db_of(app) as db:
        assert db.scalar(select(PaymentRecord)).operation_state == "RESERVED"


def test_a_send_claim_is_still_refused_by_a_hold_committed_right_before_it(app, seller, ready):
    created = invoice(seller, ready).json()
    propose(seller, ready)
    with db_of(app) as db:
        rec = db.scalar(select(PaymentRecord))
        assert payment_service._claim(db, rec, ("READY",), payment_service.Op.SEND_PENDING) is False  # enforce_hold defaults to True
        assert created["id"] == rec.id


# --- _block must never commit staged data ----------------------------------------------------------------------------

def test_a_refusal_discards_whatever_the_caller_had_staged(app, seller, ready):
    with db_of(app) as db:
        project = db.get(Project, ready)
        project.title = "STAGED BEFORE REFUSAL"
        error = instruction_service._block(db, ready, "TEST", "payment_hold_active", ["UNVERIFIED_INSTRUCTION_CHANGE"])
        assert error.status_code == 409
    assert seller.get(f"/api/v1/projects/{ready}").json()["title"] != "STAGED BEFORE REFUSAL"
    assert "INSTRUCTION_GATE_BLOCKED" in [e["event_type"] for e in seller.get(f"/api/v1/projects/{ready}/audit-events").json()]
