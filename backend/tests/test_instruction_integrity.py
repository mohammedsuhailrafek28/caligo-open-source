"""R1 payment instruction integrity: versioned fingerprinted instructions, change proposals that HOLD Caligo's payment operations,
enforcement in create/send/accept-review, idempotency, isolation, tampering and races. Offline only (MOCK provider or fake PayPal)."""
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session as DbSession

from app.config import Settings
from app.main import create_app
from app.models import (
    AppendOnlyError, InstructionChangeRequest, InstructionChangeResolution, PaymentInstruction, PaymentRecord,
)
from app.paypal_sandbox import PayPalSandboxProvider
from app.services import instructions as instruction_service
from tests.fake_paypal import CLIENT_ID, CLIENT_SECRET, MERCHANT, SETTINGS_CREDENTIALS
from tests.helpers import login, register
from tests.test_payments import INVOICE, SEND, events, invoice, make_project, payments, send, sync

INSTR = {"recipient_email": "Buyer@Example.org", "currency": "USD", "amount_minor": 80000}
CHANGE = {"recipient_email": "buyer@example.org", "currency": "USD", "amount_minor": 95000, "reason": "Scope grew by agreement"}
IK = "instr-key-000001"
CK = "change-key-00001"


def base(pid):
    return f"/api/v1/projects/{pid}/payment-instruction"


def record(seller, pid, body=None, key=IK):
    headers = {"Idempotency-Key": key} if key else {}
    return seller.post(base(pid), json=body or INSTR, headers=headers)


def propose(seller, pid, body=None, key=CK):
    headers = {"Idempotency-Key": key} if key else {}
    return seller.post(base(pid) + "/changes", json=body or CHANGE, headers=headers)


def hold(seller, pid):
    response = seller.get(f"/api/v1/projects/{pid}/payment-hold")
    assert response.status_code == 200, response.text
    return response.json()


def audit_types(seller, pid):
    return [e["event_type"] for e in seller.get(f"/api/v1/projects/{pid}/audit-events").json()]


@pytest.fixture()
def pid(seller):
    return make_project(seller)


@pytest.fixture()
def ready(seller, pid):
    assert record(seller, pid).status_code == 201
    return pid


def db_of(app):
    return DbSession(app.state.engine)


def drop_triggers(app, *tables):
    with db_of(app) as db:
        for table in tables:
            for verb in ("update", "delete"):
                db.execute(text(f"DROP TRIGGER IF EXISTS {table}_no_{verb}"))
        db.commit()


# --- recording ----------------------------------------------------------------------------------------------------

def test_an_instruction_is_recorded_pending_verification_with_a_fingerprint(seller, pid):
    response = record(seller, pid)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["verification_status"] == "PENDING_VERIFICATION" and body["provenance"] == "SELLER_ENTERED"
    assert body["version"] == 1 and len(body["fingerprint"]) == 64 and len(body["chain_sha256"]) == 64
    assert body["recipient_email"] == "buyer@example.org" and body["merchant_email"] is None  # MOCK has no merchant
    assert body["context"].keys() >= {"original_scope_sha256", "baseline_content_sha256"}


def test_recording_is_idempotent_and_a_second_instruction_needs_a_change(seller, pid):
    first = record(seller, pid).json()
    replay = record(seller, pid)
    assert replay.status_code == 200 and replay.json()["id"] == first["id"]
    assert record(seller, pid, {**INSTR, "amount_minor": 1}).status_code == 409  # same key, different body
    other = record(seller, pid, key="another-key-0001")
    assert other.status_code == 409 and other.json()["error"]["code"] == "instruction_exists"
    assert len(seller.get(base(pid) + "/versions").json()) == 1


def test_an_idempotency_key_is_required_and_unknown_fields_are_rejected(seller, pid):
    assert record(seller, pid, key=None).status_code == 400
    assert record(seller, pid, key="short").status_code == 400
    assert record(seller, pid, {**INSTR, "status": "VERIFIED"}).status_code == 422
    assert record(seller, pid, {**INSTR, "verification_status": "VERIFIED"}).status_code == 422
    assert record(seller, pid, {**INSTR, "amount_minor": 0}).status_code == 422
    assert record(seller, pid, {**INSTR, "amount_minor": 10.5}).status_code == 422


def test_a_client_cannot_name_a_different_payee(seller, pid):
    response = record(seller, pid, {**INSTR, "merchant_email": "attacker@example.net"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "merchant_mismatch"
    assert seller.get(base(pid)).json()["instruction"] is None


def test_the_instruction_requires_authentication_and_csrf(app, pid, seller):
    anonymous = TestClient(app)
    assert anonymous.get(base(pid)).status_code == 401
    assert anonymous.post(base(pid), json=INSTR, headers={"Idempotency-Key": IK}).status_code == 401
    no_csrf = seller.client.post(base(pid), json=INSTR, headers={"Idempotency-Key": IK})
    assert no_csrf.status_code == 403


# --- invoices must match the instruction --------------------------------------------------------------------------

def test_an_invoice_that_deviates_from_the_instruction_is_refused_and_audited(seller, ready):
    for deviation in ({"amount_minor": 90000}, {"currency": "EUR"}, {"recipient_email": "other@example.org"}):
        response = invoice(seller, ready, {**INVOICE, **deviation}, key=f"dev-{list(deviation)[0]}-0001")
        assert response.status_code == 409 and response.json()["error"]["code"] == "instruction_mismatch", deviation
        assert payments(seller, ready) == []
    assert "INSTRUCTION_GATE_BLOCKED" in audit_types(seller, ready)


def test_a_matching_invoice_is_bound_to_the_instruction(app, seller, ready):
    created = invoice(seller, ready)
    assert created.status_code == 201, created.text
    with db_of(app) as db:
        rec = db.scalar(select(PaymentRecord))
        instruction = db.scalar(select(PaymentInstruction))
        assert rec.instruction_id == instruction.id and rec.instruction_fingerprint == instruction.fingerprint


def test_without_an_instruction_the_first_invoice_records_one_implicitly(app, seller, pid):
    assert invoice(seller, pid).status_code == 201
    state = seller.get(base(pid)).json()
    assert state["instruction"]["provenance"] == "INVOICE_REQUEST"
    assert state["instruction"]["verification_status"] == "PENDING_VERIFICATION"
    assert "INSTRUCTION_RECORDED" in audit_types(seller, pid)
    assert invoice(seller, pid, {**INVOICE, "amount_minor": 1}, key="second-key-0001").status_code in (409,)  # active invoice / mismatch


def test_when_required_no_invoice_can_be_created_without_an_instruction(make_app):
    app = make_app(payment_instruction_required=True)
    seller = login(_client(app, "req@example.com"), "req@example.com")
    pid = make_project(seller)
    blocked = invoice(seller, pid)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "payment_hold_active"
    assert hold(seller, pid)["reasons"] == ["INSTRUCTION_REQUIRED"]
    assert record(seller, pid).status_code == 201 and invoice(seller, pid).status_code == 201


def _client(app, email):
    client = TestClient(app)
    assert register(client, email).status_code == 201
    return client


# --- change proposals and the hold --------------------------------------------------------------------------------

def test_a_change_proposal_starts_a_hold_and_nothing_applies_it(seller, ready):
    assert hold(seller, ready)["active"] is False
    response = propose(seller, ready)
    assert response.status_code == 201, response.text
    assert response.json()["resolution"] is None
    state = seller.get(base(ready)).json()
    assert state["instruction"]["amount_minor"] == 80000 and state["versions"] == 1  # the instruction in force is unchanged
    assert state["hold"]["active"] and state["hold"]["reasons"] == ["UNVERIFIED_INSTRUCTION_CHANGE"]
    assert state["hold"]["scope"] == "CALIGO_OPERATIONS_ONLY" and state["hold"]["release_requires"] == "BUYER_SIDE_VERIFICATION"
    assert {"INSTRUCTION_CHANGE_PROPOSED"} <= set(audit_types(seller, ready))


def test_proposals_are_idempotent_and_only_one_may_be_pending(seller, ready):
    first = propose(seller, ready).json()
    again = propose(seller, ready)
    assert again.status_code == 200 and again.json()["id"] == first["id"]
    assert propose(seller, ready, {**CHANGE, "amount_minor": 1}).status_code == 409  # same key, different body
    pending = propose(seller, ready, {**CHANGE, "amount_minor": 99999}, key="change-key-00002")
    assert pending.status_code == 409 and pending.json()["error"]["code"] == "change_pending"


def test_a_no_op_or_instruction_less_proposal_is_refused(seller, pid):
    assert propose(seller, pid).json()["error"]["code"] == "instruction_required"
    record(seller, pid)
    same = propose(seller, pid, {**CHANGE, "amount_minor": 80000})
    assert same.status_code == 422 and same.json()["error"]["code"] == "no_change"


def test_a_pending_change_blocks_invoice_creation(app, seller, ready):
    propose(seller, ready)
    blocked = invoice(seller, ready)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "payment_hold_active"
    assert blocked.json()["error"]["details"] == [{"reason": "UNVERIFIED_INSTRUCTION_CHANGE"}]
    assert payments(seller, ready) == [] and not app.state.payment_provider._invoices  # the provider was never contacted


def test_a_pending_change_blocks_sending_and_the_provider_is_not_called(app, seller, ready):
    created = invoice(seller, ready).json()
    propose(seller, ready)
    blocked = send(seller, ready, created["id"])
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "payment_hold_active"
    stored = payments(seller, ready)[0]
    assert stored["provider_status"] == "UNVERIFIED" and stored["invoice_state"] in (None, "DRAFT")
    assert all(i.state.value == "DRAFT" for i in app.state.payment_provider._invoices.values())


def test_a_pending_change_blocks_accepting_provider_evidence_in_a_review(app, seller, ready):
    created = invoice(seller, ready).json()
    with db_of(app) as db:
        db.execute(text("UPDATE payment_records SET provider_status='NEEDS_REVIEW' WHERE id=:i"), {"i": created["id"]})
        db.commit()
    propose(seller, ready)
    body = {"resolution": "ACCEPT_PROVIDER_EVIDENCE", "note": "checked the dashboard manually"}
    blocked = seller.post(f"/api/v1/projects/{ready}/payments/{created['id']}/resolve-review", json=body)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "payment_hold_active"
    assert payments(seller, ready)[0]["provider_status"] == "NEEDS_REVIEW"


def test_reconciliation_still_records_what_the_provider_reports_during_a_hold(app, seller, ready):
    created = invoice(seller, ready).json()
    assert send(seller, ready, created["id"]).status_code == 200
    propose(seller, ready)
    app.state.payment_provider.simulate_payment(created["paypal_invoice_id"])
    assert sync(seller, ready).status_code == 200
    assert payments(seller, ready)[0]["provider_status"] == "PAID"  # a hold never hides real money


def test_there_is_no_route_that_approves_overrides_or_releases_a_hold(app, seller, ready):
    change = propose(seller, ready).json()
    paths = " ".join(p for p in app.openapi()["paths"] if "payment-instruction" in p or "payment-hold" in p)
    for word in ("approve", "release", "override", "apply", "verify", "break"):
        assert word not in paths, word  # R2 adds independent verification; R1 has no way to resolve a pending change
    for tail in ("approve", "release", "override", "apply", "resolve", "verify", "break-glass"):
        for url in (base(ready) + f"/changes/{change['id']}/{tail}", base(ready) + f"/{tail}", f"/api/v1/projects/{ready}/payment-hold/{tail}"):
            assert seller.post(url, json={}).status_code in (404, 405), url
    assert hold(seller, ready)["active"] is True
    assert seller.client.put(f"/api/v1/projects/{ready}/payment-hold", json={"active": False}).status_code in (403, 404, 405)


def test_a_resolution_cannot_be_forged_through_the_api(seller, ready):
    change = propose(seller, ready).json()
    forged = seller.post(base(ready) + f"/changes/{change['id']}/withdraw", json={"resolution": "APPLIED", "note": "x"})
    assert forged.status_code == 422 and hold(seller, ready)["active"] is True


# --- isolation ------------------------------------------------------------------------------------------------------

def test_another_seller_cannot_read_or_change_the_instruction(app, seller, ready):
    change = propose(seller, ready).json()
    other = login(_client(app, "other@example.com"), "other@example.com")
    for response in (other.get(base(ready)), other.get(f"/api/v1/projects/{ready}/payment-hold"), other.get(base(ready) + "/versions"),
                     other.get(base(ready) + "/changes"), record(other, ready, key="foreign-key-0001"),
                     propose(other, ready, key="foreign-key-0002"),
                     other.post(base(ready) + f"/changes/{change['id']}/withdraw", json={})):
        assert response.status_code == 404
    assert hold(seller, ready)["active"] is True


def test_a_hold_on_one_project_does_not_affect_another_project_of_the_same_seller(seller, ready):
    other = make_project(seller)
    propose(seller, ready)
    assert hold(seller, other)["active"] is False
    assert invoice(seller, other, key="other-proj-key01").status_code == 201


def test_a_change_id_from_another_project_cannot_be_withdrawn_here(seller, ready):
    change = propose(seller, ready).json()
    other = make_project(seller)
    assert seller.post(base(other) + f"/changes/{change['id']}/withdraw", json={}).status_code == 404
    assert hold(seller, ready)["active"] is True


def test_an_instruction_of_another_project_is_not_accepted_for_a_payment(app, seller, ready):
    created = invoice(seller, ready).json()
    other = make_project(seller)
    record(seller, other, key="other-instr-key1")
    with db_of(app) as db:
        foreign = db.scalar(select(PaymentInstruction).where(PaymentInstruction.project_id == other))
        db.execute(text("UPDATE payment_records SET instruction_id=:f, instruction_fingerprint=:p WHERE id=:i"),
                   {"f": foreign.id, "p": foreign.fingerprint, "i": created["id"]})
        db.commit()
    blocked = send(seller, ready, created["id"])
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "instruction_mismatch"


# --- tampering ------------------------------------------------------------------------------------------------------

def test_the_history_tables_are_append_only(app, seller, ready):
    change = propose(seller, ready).json()
    seller.post(base(ready) + f"/changes/{change['id']}/withdraw", json={})
    with db_of(app) as db:
        for model in (PaymentInstruction, InstructionChangeRequest, InstructionChangeResolution):
            row = db.scalar(select(model))
            row.created_at = row.created_at.replace(year=2001)
            with pytest.raises(AppendOnlyError):
                db.flush()
            db.rollback()
    with db_of(app) as db:
        for table in ("payment_instructions", "instruction_change_requests", "instruction_change_resolutions"):
            for statement in (f"UPDATE {table} SET id=id", f"DELETE FROM {table}"):
                with pytest.raises(Exception, match="append-only"):
                    db.execute(text(statement))
                db.rollback()


def test_an_altered_amount_in_the_instruction_row_is_detected_and_holds_everything(app, seller, ready):
    drop_triggers(app, "payment_instructions")
    with db_of(app) as db:
        db.execute(text("UPDATE payment_instructions SET amount_minor = 1"))
        db.commit()
    assert hold(seller, ready)["reasons"] == ["INSTRUCTION_INTEGRITY_FAILURE"]
    blocked = invoice(seller, ready, {**INVOICE, "amount_minor": 1})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "payment_hold_active"


def test_an_altered_fingerprint_or_chain_is_detected(app, seller, ready):
    drop_triggers(app, "payment_instructions")
    for column in ("fingerprint", "chain_sha256"):
        with db_of(app) as db:
            db.execute(text(f"UPDATE payment_instructions SET {column} = :v"), {"v": "0" * 64})
            db.commit()
        assert "INSTRUCTION_INTEGRITY_FAILURE" in hold(seller, ready)["reasons"], column
        with db_of(app) as db:  # restore for the next column
            row = db.scalar(select(PaymentInstruction))
            fingerprint = instruction_service.instruction_fingerprint(row)
            db.execute(text("UPDATE payment_instructions SET fingerprint=:f, chain_sha256=:c"),
                       {"f": fingerprint, "c": instruction_service.compute_chain(None, fingerprint, 1, row.project_id)})
            db.commit()
    assert hold(seller, ready)["active"] is False


def test_a_payment_record_altered_after_the_instruction_cannot_be_sent(app, seller, ready):
    created = invoice(seller, ready).json()
    with db_of(app) as db:
        db.execute(text("UPDATE payment_records SET amount_minor = 5 WHERE id=:i"), {"i": created["id"]})
        db.commit()
    blocked = send(seller, ready, created["id"])
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "instruction_mismatch"
    assert all(i.state.value == "DRAFT" for i in app.state.payment_provider._invoices.values())


def test_a_tampered_instruction_makes_reconciliation_flag_the_payment_for_review(app, seller, ready):
    created = invoice(seller, ready).json()
    assert send(seller, ready, created["id"]).status_code == 200
    drop_triggers(app, "payment_instructions")
    with db_of(app) as db:
        db.execute(text("UPDATE payment_instructions SET currency = 'EUR'"))
        db.commit()
    app.state.payment_provider.simulate_payment(created["paypal_invoice_id"])
    sync(seller, ready)
    stored = payments(seller, ready)[0]
    assert stored["provider_status"] == "NEEDS_REVIEW" and "INSTRUCTION_TAMPERED" in stored["verification_note"]


# --- merchant evidence ----------------------------------------------------------------------------------------------

def _sandbox(tmp_path, fake, provider_merchant):
    provider = PayPalSandboxProvider(CLIENT_ID, CLIENT_SECRET, merchant_email=provider_merchant, transport=fake.transport())
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'sb.sqlite3'}", paypal_mode="sandbox", **SETTINGS_CREDENTIALS,
                        paypal_sandbox_merchant_email=MERCHANT, paypal_sandbox_recipient_allowlist="buyer@example.org")
    app = create_app(settings, payment_provider=provider)
    seller = login(_client(app, "sb@example.com"), "sb@example.com")
    pid = seller.post("/api/v1/projects", json={"title": "Site"}).json()["id"]
    seller.post(f"/api/v1/projects/{pid}/scope-records", json={"kind": "ORIGINAL", "source_label": "Mail", "content": "Five pages."})
    return app, provider, seller, pid


def test_a_sandbox_instruction_records_the_configured_merchant(tmp_path, fake):
    _app, _provider, seller, pid = _sandbox(tmp_path, fake, MERCHANT)
    assert record(seller, pid, {**INSTR, "merchant_email": MERCHANT.upper()}).json()["merchant_email"] == MERCHANT
    assert invoice(seller, pid).status_code == 201


def test_missing_merchant_evidence_refuses_to_record_an_instruction_or_invoice(tmp_path, fake):
    _app, _provider, seller, pid = _sandbox(tmp_path, fake, None)
    blocked = record(seller, pid)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "merchant_evidence_missing"
    assert invoice(seller, pid).status_code == 409 and payments(seller, pid) == []


def test_a_changed_merchant_configuration_holds_operations(tmp_path, fake):
    _app, provider, seller, pid = _sandbox(tmp_path, fake, MERCHANT)
    record(seller, pid)
    provider.expected_merchant_email = "someone-else@example.net"  # the configured payee changed after the instruction
    assert hold(seller, pid)["reasons"] == ["MERCHANT_MISMATCH"]
    blocked = invoice(seller, pid)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "payment_hold_active"


def test_an_invoice_issued_to_a_different_payee_is_flagged_by_reconciliation(tmp_path, fake):
    _app, provider, seller, pid = _sandbox(tmp_path, fake, MERCHANT)
    record(seller, pid)
    created = invoice(seller, pid).json()
    provider.expected_merchant_email = "someone-else@example.net"  # even if the config drifts, judge against the INSTRUCTION's payee
    assert sync(seller, pid).status_code == 200
    assert payments(seller, pid)[0]["provider_status"] == "UNVERIFIED" and created["id"]


def test_a_mock_instruction_cannot_be_used_by_a_sandbox_provider(tmp_path, fake, make_app):
    app = make_app()
    seller = login(_client(app, "m@example.com"), "m@example.com")
    pid = make_project(seller)
    record(seller, pid)
    app.state.payment_provider = _sandbox(tmp_path, fake, MERCHANT)[1]
    assert "PROVIDER_MISMATCH" in hold(seller, pid)["reasons"]


# --- races ----------------------------------------------------------------------------------------------------------

def test_a_hold_placed_between_the_send_check_and_the_claim_still_stops_the_provider_call(app, seller, ready, monkeypatch):
    created = invoice(seller, ready).json()
    real_gate = instruction_service.gate_existing

    def gate_then_hold(db, provider, project, rec, operation, *, required):
        real_gate(db, provider, project, rec, operation, required=required)  # passes: no hold yet
        current = instruction_service.effective_instruction(db, project.id)  # a proposal lands inside the same locked transaction
        db.add(InstructionChangeRequest(
            project_id=project.id, from_instruction_id=current.id, from_fingerprint=current.fingerprint,
            recipient_email=current.recipient_email, currency="USD", amount_minor=1, proposed_fingerprint="f" * 64,
            reason="race", provenance="SELLER_ENTERED", idempotency_key="race-key-0001", request_sha256="r" * 64))
        db.flush()

    monkeypatch.setattr(instruction_service, "gate_existing", gate_then_hold)
    blocked = send(seller, ready, created["id"])
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "payment_hold_active"
    assert all(i.state.value == "DRAFT" for i in app.state.payment_provider._invoices.values())
    assert payments(seller, ready)[0]["provider_status"] == "UNVERIFIED"


def test_parallel_proposals_leave_exactly_one_pending(app, seller, ready):
    results: list[int] = []

    def attempt(n: int) -> None:
        client = TestClient(app)
        client.cookies.update(seller.client.cookies)
        response = client.post(base(ready) + "/changes", json={**CHANGE, "amount_minor": 90000 + n},
                               headers={**seller.headers, "Idempotency-Key": f"parallel-key-{n:04d}"})
        results.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(201) == 1 and results.count(409) == 5, results
    assert len(hold(seller, ready)["pending_change_ids"]) == 1


def test_parallel_invoice_creates_and_a_proposal_never_leave_an_invoice_past_a_hold(app, seller, ready):
    outcomes: dict[str, int] = {}

    def create(n: int) -> None:
        client = TestClient(app)
        client.cookies.update(seller.client.cookies)
        outcomes[f"create{n}"] = client.post(f"/api/v1/projects/{ready}/invoices", json=INVOICE,
                                       headers={**seller.headers, "Idempotency-Key": f"par-create-{n:04d}"}).status_code

    def change() -> None:
        client = TestClient(app)
        client.cookies.update(seller.client.cookies)
        outcomes["change"] = client.post(base(ready) + "/changes", json=CHANGE, headers={**seller.headers, "Idempotency-Key": CK}).status_code

    threads = [threading.Thread(target=create, args=(n,)) for n in range(3)] + [threading.Thread(target=change)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes["change"] == 201
    assert sum(1 for k, v in outcomes.items() if k.startswith("create") and v == 201) <= 1  # one active invoice, never two
    # whatever happened first, once the proposal exists every NEW operation is held (in-flight rule: tests/test_r1_hardening.py)
    assert invoice(seller, ready, key="after-hold-0001").status_code == 409
    assert hold(seller, ready)["active"] is True


def test_a_proposal_cannot_name_a_payee_other_than_the_configured_merchant(tmp_path, fake):
    _app, _provider, seller, pid = _sandbox(tmp_path, fake, MERCHANT)
    record(seller, pid)
    foreign = propose(seller, pid, {**CHANGE, "merchant_email": "attacker@example.net"})
    assert foreign.status_code == 422 and foreign.json()["error"]["code"] == "merchant_mismatch"
    assert hold(seller, pid)["active"] is False and seller.get(base(pid) + "/changes").json() == []
    assert propose(seller, pid, {**CHANGE, "merchant_email": MERCHANT}).status_code == 201


def test_a_withdrawal_is_audited_as_a_seller_action_so_a_release_by_the_seller_is_visible(seller, ready):
    change = propose(seller, ready).json()
    seller.post(base(ready) + f"/changes/{change['id']}/withdraw", json={})
    event = [e for e in seller.get(f"/api/v1/projects/{ready}/audit-events").json() if e["event_type"] == "INSTRUCTION_CHANGE_WITHDRAWN"][0]
    assert event["actor_role"] == "SELLER" and event["detail"] == {"instruction_in_force_unchanged": True}
