"""R5: the unified, deterministic, read-only evidence report over scope/delivery, instruction integrity/authorization and payments."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as DbSession

from app.models import Base, ProviderEvidenceGap
from app.services import report_citations, scope as scope_service, unified_report
from tests.test_analysis import FORM, HOME, artifact
from tests.test_instruction_integrity import drop_triggers, pid, propose, ready, record  # noqa: F401 (fixtures)
from tests.test_instruction_verification import APPROVE, World, clock, db_of, keys, r2app, r2seller, seller_factory_for, world  # noqa: F401
from tests.test_payment_observations import claim, csv_of, ledger, line, observations, paid, unpaid  # noqa: F401 (fixtures)
from tests.test_payments import INVOICE, invoice, make_project, sent_invoice, sync
from tests.test_scope_baselines import freeze, propose as scope_propose
from app.services.report_markdown import esc
from app.payment_provider import ProviderInvoiceState as P


def url(pid, suffix=""):
    return f"/api/v1/projects/{pid}/unified-report{suffix}"


def report(seller, pid, **params):
    response = seller.client.get(url(pid), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def markdown(seller, pid, **params):
    response = seller.client.get(url(pid, "/markdown"), params=params)
    assert response.status_code == 200, response.text
    return response.text


def recomputed(body):
    return scope_service.canonical_sha256({k: v for k, v in body.items() if k != "report_sha256"})


def row_counts(app):
    with DbSession(app.state.engine) as db:
        return {name: db.scalar(select(func.count()).select_from(table)) for name, table in Base.metadata.tables.items()}


# --- access, read-only, shape -------------------------------------------------------------------------------------------------

def test_the_report_needs_authentication_and_ownership(app, seller, seller_factory, paid):
    pid, _, _ = paid
    assert TestClient(app).get(url(pid)).status_code in (401, 403)
    intruder = seller_factory("intruder@example.com")
    assert intruder.client.get(url(pid)).status_code == 404
    assert intruder.client.get(url(pid, "/markdown")).status_code == 404
    assert intruder.client.get(url(make_project(intruder))).json()["payments"]["payments"] == []  # nothing of another project leaks in


def test_generating_the_report_writes_nothing_and_offers_no_way_to_write(app, seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    before = row_counts(app)
    report(seller, pid)
    markdown(seller, pid)
    assert row_counts(app) == before
    paths = {p: set(v) for p, v in app.openapi()["paths"].items() if "unified-report" in p}
    assert len(paths) == 2 and all(methods == {"get"} for methods in paths.values())
    for method in ("post", "put", "patch", "delete"):
        assert getattr(seller.client, method)(url(pid)).status_code in (404, 405, 403, 401)


def test_the_report_has_every_required_section_and_legend(seller, paid):
    pid, _, _ = paid
    body = report(seller, pid)
    assert body["kind"] == "UNIFIED_EVIDENCE_REPORT" and body["project"]["id"] == pid
    assert set(body) >= {"evidence_classes", "summary", "scope_and_delivery", "payment_instructions", "payments", "citation_check",
                         "truncation", "limitations", "report_sha256"}
    assert set(body["evidence_classes"]) == {"PROVIDER_OBSERVATION", "SERVER_RECORDED", "SELLER_CLAIM", "OPERATOR_ESTABLISHED_VERIFICATION",
                                             "AI_INTERPRETATION", "DERIVED_ADVISORY"}
    assert body["payments"]["recovery_cases"]["advisory_only"] is True


def test_parameters_are_bounded(seller, paid):
    pid, _, _ = paid
    for bad in ({"payments_limit": 51}, {"payments_limit": 0}, {"payments_offset": -1}, {"payments_offset": 100_001}, {"payments_limit": "x"}):
        assert seller.client.get(url(pid), params=bad).status_code == 422


def test_reads_are_throttled(app, seller, paid):
    from app.security import LoginThrottle

    pid, _, _ = paid
    app.state.observation_read_throttle = LoginThrottle(1, 60)
    assert seller.client.get(url(pid)).status_code == 200
    assert seller.client.get(url(pid)).status_code == 429


# --- claims vs provider; incomplete evidence is preserved ----------------------------------------------------------------------

def test_seller_claims_and_provider_observations_stay_separate_and_the_status_is_not_rewritten(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, amount_minor=70000, transaction_id="TXN-PROVIDER-1").status_code == 201
    [row] = report(seller, pid)["payments"]["payments"]
    assert row["authoritative_status"] == "PAID" and row["authoritative_status_source"] == "PROVIDER_RECONCILIATION"
    assert row["provider_observations"]["evidence_class"] == "PROVIDER_OBSERVATION" and row["provider_observations"]["paid_minor"] == 80000
    assert row["seller_claims"]["evidence_class"] == "SELLER_CLAIM" and row["seller_claims"]["claimed_paid_minor"] == 70000
    assert row["claim_vs_provider"] == "CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER"
    sources = row["observation_counts_by_source"]
    assert sources.get("PROVIDER_API") and sources.get("SELLER_ENTRY") == 1
    mismatch = [f for f in row["findings"] if f["code"] == "CLAIM_AMOUNT_MISMATCH"]
    assert mismatch and all(f["citations"] for f in mismatch)


def test_incomplete_provider_evidence_is_preserved_and_never_becomes_a_missing_payment(app, seller, unpaid):
    pid, payment, number = unpaid
    with DbSession(app.state.engine) as db:
        db.add(ProviderEvidenceGap(project_id=pid, payment_id=payment["id"], kind="PAYMENT", reason="OVERSIZED_AMOUNT",
                                   raw_sha256="a" * 64, dedupe_key="b" * 64))
        db.commit()
    assert claim(seller, pid, number, transaction_id="TXN-NOT-STORED-1").status_code == 201
    body = report(seller, pid)
    [row] = body["payments"]["payments"]
    assert row["provider_observations"]["evidence_complete"] is False and row["claim_vs_provider"] == "NOT_COMPARABLE_EVIDENCE_INCOMPLETE"
    assert body["summary"]["payments_with_incomplete_provider_evidence"] == [payment["id"]]
    assert body["payments"]["evidence_gaps"]["total"] == 1 and body["payments"]["evidence_gaps"]["items"][0]["reason"] == "OVERSIZED_AMOUNT"
    categories = {c["category"] for c in body["payments"]["recovery_cases"]["items"]}
    assert "EVIDENCE_INCOMPLETE" in categories and not categories & {"CLAIM_NOT_CORROBORATED", "LEDGER_MISSING_PROVIDER_PAYMENT"}
    assert "Absence of evidence is then not absence of payment" in markdown(seller, pid)


def test_conflicting_observations_are_listed_not_reconciled_away(seller, paid):
    pid, _, number = paid
    assert ledger(seller, pid, csv_of(line(number, txn="TXN-OTHER-0001", row_id="R1"))).status_code == 201
    body = report(seller, pid)
    codes = {f["code"] for f in body["payments"]["payments"][0]["findings"]}
    assert "PROVIDER_PAYMENT_NOT_IN_LEDGER" in codes
    cases = body["payments"]["recovery_cases"]["items"]
    assert cases and all(c["advisory_only"] is True and c["prohibited_automated_actions"] and c["recommended_next_action"] for c in cases)
    assert body["payments"]["payments"][0]["authoritative_status"] == "PAID"  # reported beside the conflict, not changed by it


# --- scope and delivery ------------------------------------------------------------------------------------------------------

def test_a_project_without_an_agreement_still_reports_payments_and_says_scope_is_unavailable(seller, paid):
    pid, _, _ = paid
    body = report(seller, pid)
    assert body["scope_and_delivery"]["available"] is False and "No original agreement" in body["scope_and_delivery"]["reason"]
    assert body["payments"]["payments"] and body["summary"]["scope_delivery_available"] is False


def test_scope_and_delivery_disagreements_surface_and_reuse_the_stage3_report(seller):
    pid = make_project(seller)
    assert freeze(seller, pid, reqs=[HOME, FORM]).status_code == 201
    assert scope_propose(seller, pid).status_code == 201  # R3 is proposed, not approved
    artifact(seller, pid, declared_sha256="b" * 64, requirement_keys=["R1", "R3"])
    body = report(seller, pid)
    scope = body["scope_and_delivery"]
    types = {u["type"] for u in scope["unresolved"]}
    assert {"UNAPPROVED_WORK_DELIVERED", "ARTIFACT_NOT_HASHED_BY_SERVER", "NO_BUYER_DELIVERY_DECISION"} <= types
    assert scope["evidence_report_sha256"] == seller.get(f"/api/v1/projects/{pid}/evidence-report").json()["report_sha256"]
    assert scope["buyer_evidence"]["delivery_status"] and "not legal identity" in scope["buyer_evidence"]["caveat"]
    assert body["citation_check"]["all_resolved"] is True and body["citation_check"]["distinct_checked"] > 0
    assert body["summary"]["scope_unresolved_items"] == len(scope["unresolved"])


# --- citations ---------------------------------------------------------------------------------------------------------------

def test_every_citation_in_a_real_report_resolves_to_this_project_and_payment(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    check = report(seller, pid)["citation_check"]
    assert check["all_resolved"] is True and check["problem_count"] == 0 and check["distinct_checked"] >= 2


def test_validation_rejects_other_projects_other_payments_and_unknown_kinds(app, seller, paid):
    pid, payment, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-X1").status_code == 201
    mine = next(o["id"] for o in observations(seller, pid) if o["source"] == "SELLER_ENTRY")
    other = make_project(seller)
    other_payment = sent_invoice(seller, other)
    with DbSession(app.state.engine) as db:
        stranger = db.scalar(select(ProviderEvidenceGap.id)) or "00000000-0000-0000-0000-000000000000"
        result = report_citations.validate(db, pid, [
            ("OBSERVATION", mine, payment["id"]),                    # right project, right payment: fine
            ("OBSERVATION", mine, other_payment["id"]),              # right project, WRONG payment
            ("OBSERVATION", "no-such-observation", payment["id"]),   # not found
            ("EVIDENCE_GAP", stranger, payment["id"]),               # not found / not this project's gap
            ("PAYMENT_RECORD", other_payment["id"], other_payment["id"]),  # another project's payment
            ("made_up_kind", "x", None)])
        db.rollback()
    reasons = {(p["kind"], p["reason"]) for p in result["problems"]}
    assert result["all_resolved"] is False
    assert ("OBSERVATION", "PAYMENT_MISMATCH") in reasons and ("OBSERVATION", "NOT_FOUND_IN_PROJECT") in reasons
    assert ("PAYMENT_RECORD", "NOT_FOUND_IN_PROJECT") in reasons and ("made_up_kind", "UNKNOWN_CITATION_KIND") in reasons
    assert not any(p["id"] == mine and p["reason"] == "NOT_FOUND_IN_PROJECT" for p in result["problems"])


def test_a_forged_citation_is_reported_in_json_and_markdown_and_the_report_still_builds(seller, paid, monkeypatch):
    pid, _, _ = paid
    real = unified_report._payment_citations
    monkeypatch.setattr(unified_report, "_payment_citations", lambda *a: [*real(*a), ("OBSERVATION", "forged-id-0001", "nope")])
    body = report(seller, pid)
    assert body["citation_check"]["all_resolved"] is False and body["summary"]["citations_all_resolved"] is False
    assert {"kind": "OBSERVATION", "id": "forged-id-0001", "reason": "NOT_FOUND_IN_PROJECT"} in body["citation_check"]["problems"]
    assert "Unresolved citations were found" in markdown(seller, pid)


def test_scope_reference_strings_are_validated_and_malformed_ones_are_flagged():
    assert report_citations.parse_ref("artifact:abc-123") == ("artifact", "abc-123")
    for bad in ("", "artifact", "evil:abc", "artifact:", "<script>:x"):
        assert report_citations.parse_ref(bad) is None


# --- payment-instruction integrity and authorization trust labels ------------------------------------------------------------

def test_a_pending_change_is_shown_as_pending_with_an_active_hold(seller, ready):
    pid = ready
    assert propose(seller, pid).status_code == 201
    instr = report(seller, pid)["payment_instructions"]
    assert instr["hold"]["active"] is True and instr["hold"]["reasons"] == ["UNVERIFIED_INSTRUCTION_CHANGE"]
    assert instr["changes"][0]["state"] == "PENDING_BUYER_SIDE_VERIFICATION" and instr["changes"][0]["reason_evidence_class"] == "SELLER_CLAIM"
    assert instr["versions"][0]["evidence_class"] == "SELLER_CLAIM" and instr["versions"][0]["verification_status"] == "PENDING_VERIFICATION"
    assert instr["authorizations"] == [] and "never freezes" in instr["hold"]["note"]


def test_authorization_records_are_operator_established_and_never_identity_proof(world):
    ch = world.fresh()
    assert world.decide(ch).status_code == 200
    body = world.seller.client.get(url(world.pid)).json()
    instr = body["payment_instructions"]
    [auth_row] = instr["authorizations"]
    assert auth_row["label"] == "VERIFIED_BY_ENROLLED_SECOND_CHANNEL" and auth_row["evidence_class"] == "OPERATOR_ESTABLISHED_VERIFICATION"
    assert auth_row["real_world_identity_verified"] is False and auth_row["independent_of_seller"] == "NOT_ESTABLISHED"
    assert auth_row["record_consistent"] is True and auth_row["record_problems"] == []
    assert "NOT prove the real-world identity" in auth_row["trust_limitation"] and auth_row["approver_email_masked"].startswith("b***@")
    v1, v2 = instr["versions"]
    assert (v1["evidence_class"], v2["evidence_class"]) == ("SELLER_CLAIM", "OPERATOR_ESTABLISHED_VERIFICATION")
    assert v2["verification_status"] == "VERIFIED_BY_ENROLLED_SECOND_CHANNEL" and v2["fingerprint"] == world.change["proposed_fingerprint"]
    assert v2["previous_instruction_id"] == v1["id"] and instr["hash_chain_intact"] is True and instr["hold"]["active"] is False
    assert instr["changes"][0]["state"] == "APPLIED"
    [authenticator] = instr["authenticators"]
    assert authenticator["trust_basis"] == "OPERATOR_ESTABLISHED" and authenticator["status"] == "ACTIVE"
    raw = json.dumps(body)
    assert '"real_world_identity_verified": true' not in raw and "secret_ciphertext" not in raw and "approver_email\"" not in raw
    md = world.seller.client.get(url(world.pid, "/markdown")).text
    assert "real-world identity verified: **False**" in md and "NOT prove the real-world identity" in md


def test_a_rejected_change_is_reported_as_rejected_with_the_same_trust_limits(world):
    assert world.decide(world.fresh(), decision="REJECT_INSTRUCTION_CHANGE").status_code == 200
    instr = world.seller.client.get(url(world.pid)).json()["payment_instructions"]
    assert instr["changes"][0]["state"] == "REJECTED" and instr["authorizations"][0]["real_world_identity_verified"] is False
    assert len(instr["versions"]) == 1


def test_an_inconsistent_authorization_record_is_flagged_not_hidden(world):
    assert world.decide(world.fresh()).status_code == 200
    drop_triggers(world.app, "instruction_change_verifications")
    with db_of(world.app) as db:
        db.execute(text("UPDATE instruction_change_verifications SET proposed_fingerprint = :f"), {"f": "f" * 64})
        db.commit()
    body = world.seller.client.get(url(world.pid)).json()
    [auth_row] = body["payment_instructions"]["authorizations"]
    assert auth_row["record_consistent"] is False and "FINGERPRINT_MISMATCH_WITH_CHANGE" in auth_row["record_problems"]
    assert body["summary"]["authorization_records_inconsistent"] == 1


def test_a_tampered_instruction_history_is_reported_and_the_report_still_builds(world):
    drop_triggers(world.app, "payment_instructions")
    with db_of(world.app) as db:
        db.execute(text("UPDATE payment_instructions SET amount_minor = 1"))
        db.commit()
    body = world.seller.client.get(url(world.pid)).json()
    instr = body["payment_instructions"]
    assert instr["hash_chain_intact"] is False and instr["versions"][0]["fingerprint_recomputes"] is False
    assert "INSTRUCTION_INTEGRITY_FAILURE" in instr["hold"]["reasons"] and body["summary"]["instruction_hash_chain_intact"] is False


# --- determinism, hashes, markdown -------------------------------------------------------------------------------------------

def test_the_report_is_deterministic_and_its_hash_is_verifiable(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    first, second = report(seller, pid), report(seller, pid)
    assert first == second and first["report_sha256"] == recomputed(first)
    assert markdown(seller, pid) == markdown(seller, pid) and f"`{first['report_sha256']}`" in markdown(seller, pid)


def test_new_evidence_changes_the_hash_and_a_reordered_copy_does_not(seller, paid):
    pid, _, number = paid
    before = report(seller, pid)
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    after = report(seller, pid)
    assert after["report_sha256"] != before["report_sha256"]
    shuffled = json.loads(json.dumps(after, sort_keys=True))
    assert recomputed(shuffled) == after["report_sha256"]  # canonical: key order cannot move the hash


def test_the_two_representations_agree(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    body, md = report(seller, pid), markdown(seller, pid)
    for case in body["payments"]["recovery_cases"]["items"]:
        assert case["case_id"] in md and esc(case["category"]) in md
    assert body["payments"]["payments"][0]["payment_id"] in md and esc("SELLER_CLAIM") in md and esc("PROVIDER_OBSERVATION") in md


def test_markdown_escapes_stored_text(seller, ready):
    pid = ready
    hostile = {"recipient_email": "buyer@example.org", "currency": "USD", "amount_minor": 90000, "reason": "<script>alert(1)</script> | **x** [l](javascript:1)"}
    assert propose(seller, pid, hostile).status_code == 201
    md = markdown(seller, pid)
    assert "<script>" not in md and "&lt;script&gt;" in md and "\\|" in md and "javascript:1)" not in md.replace("\\]", "]") or "\\[l\\]" in md


# --- truncation and size -----------------------------------------------------------------------------------------------------

def test_the_payment_page_is_bounded_and_says_so(app, seller):
    pid = make_project(seller)
    first = sent_invoice(seller, pid)
    app.state.payment_provider.simulate_state(first["paypal_invoice_id"], P.CANCELLED)
    assert sync(seller, pid).status_code == 200
    second = invoice(seller, pid, key="invoice-key-0002", body={**INVOICE, "replaces_payment_id": first["id"]}).json()
    body = report(seller, pid, payments_limit=1)
    assert body["payments"]["page"] == {"total_payments": 2, "offset": 0, "limit": 1, "returned": 1, "more": True}
    assert body["truncation"]["payments_page"]["more"] is True
    next_page = report(seller, pid, payments_limit=1, payments_offset=1)
    ids = {body["payments"]["payments"][0]["payment_id"], next_page["payments"]["payments"][0]["payment_id"]}
    assert ids == {first["id"], second["id"]} and next_page["payments"]["page"]["more"] is False


def test_instruction_versions_report_truncation(world, monkeypatch):
    assert world.decide(world.fresh()).status_code == 200
    monkeypatch.setattr(unified_report, "MAX_INSTRUCTION_VERSIONS", 1)
    body = world.seller.client.get(url(world.pid)).json()
    assert body["truncation"]["instruction_versions_truncated"] is True and len(body["payment_instructions"]["versions"]) == 1
    assert body["payment_instructions"]["version_count"] == 2


def test_evidence_gaps_report_truncation(app, seller, unpaid, monkeypatch):
    pid, payment, _ = unpaid
    monkeypatch.setattr(unified_report, "MAX_GAPS", 1)
    with DbSession(app.state.engine) as db:
        for n in range(2):
            db.add(ProviderEvidenceGap(project_id=pid, payment_id=payment["id"], kind="PAYMENT", reason="OVERSIZED_AMOUNT",
                                       raw_sha256="a" * 64, dedupe_key=f"{n}" * 64))
        db.commit()
    gaps = report(seller, pid)["payments"]["evidence_gaps"]
    assert gaps["truncated"] is True and gaps["total"] == 2 and gaps["shown"] == 1


def test_a_project_with_many_claims_stays_bounded(seller, paid):
    pid, _, number = paid
    rows = [line(number, amount=100 + i, txn=f"TXN-BULK-{i:04d}", row_id=f"R{i}") for i in range(120)]
    assert ledger(seller, pid, csv_of(*rows)).status_code == 201
    body = report(seller, pid)
    assert all(c["citations_total"] >= len(c["citations"]) and len(c["citations"]) <= 20 for c in body["payments"]["recovery_cases"]["items"])
    assert all(len(p["findings"]) <= 200 for p in body["payments"]["payments"])
    assert body["citation_check"]["all_resolved"] is True and body["report_sha256"] == recomputed(body)


# --- security-review fixes ---------------------------------------------------------------------------------------------------

def test_an_inconsistent_verification_never_promotes_the_applied_version_or_keeps_its_label(world):
    assert world.decide(world.fresh()).status_code == 200
    drop_triggers(world.app, "instruction_change_verifications")
    with db_of(world.app) as db:
        db.execute(text("UPDATE instruction_change_verifications SET proposed_fingerprint = :f"), {"f": "f" * 64})
        db.commit()
    instr = world.seller.client.get(url(world.pid)).json()["payment_instructions"]
    [auth_row] = instr["authorizations"]
    assert auth_row["label"] == "UNVERIFIED_INCONSISTENT_RECORD" and auth_row["evidence_class"] == "SERVER_RECORDED"
    assert instr["versions"][1]["verification_status"] == "PENDING_VERIFICATION" and instr["versions"][1]["evidence_class"] == "SELLER_CLAIM"
    assert instr["authorization_records_inconsistent"] == 1


def test_a_rejected_authorization_is_not_labelled_verified(world):
    assert world.decide(world.fresh(), decision="REJECT_INSTRUCTION_CHANGE").status_code == 200
    [auth_row] = world.seller.client.get(url(world.pid)).json()["payment_instructions"]["authorizations"]
    assert auth_row["outcome"] == "REJECTED" and auth_row["label"] == "REJECTED_BY_ENROLLED_SECOND_CHANNEL"
    assert "VERIFIED_BY" not in auth_row["label"]


def test_truncated_claims_are_not_compared_to_the_provider_total(seller, paid, monkeypatch):
    from app.services import observation_compare

    pid, _, number = paid
    rows = [line(number, amount=100 + i, txn=f"TXN-TR-{i:04d}", row_id=f"R{i}") for i in range(6)]
    assert ledger(seller, pid, csv_of(*rows)).status_code == 201
    monkeypatch.setattr(observation_compare, "MAX_OBSERVATIONS_PER_PAYMENT", 2)
    row = report(seller, pid)["payments"]["payments"][0]
    assert row["seller_claims"]["claims_truncated"] is True and row["claim_vs_provider"] in (
        "NOT_COMPARABLE_CLAIMS_TRUNCATED", "NOT_COMPARABLE_EVIDENCE_INCOMPLETE")


def test_markdown_defuses_autolinks_and_never_breaks_a_code_span(seller, ready):
    pid = ready
    hostile = {"recipient_email": "buyer@example.org", "currency": "USD", "amount_minor": 90000,
               "reason": "pay at http://evil.example now, mail a@evil.example or see www.evil.example `` x"}
    assert propose(seller, pid, hostile).status_code == 201
    md = markdown(seller, pid)
    assert "http://" not in md and "www.evil" not in md and "a@evil" not in md and "&#58;//" in md


def test_the_markdown_survives_malformed_numbers():
    from app.services.unified_report_markdown import num

    assert num(5) == "5" and num(None) == "" and num("x<b>") == "x&lt;b&gt;"


def test_the_citation_check_states_its_coverage_and_flags_truncated_scope_references(seller, paid):
    pid, _, _ = paid
    check = report(seller, pid)["citation_check"]
    assert "only what was checked" in check["coverage"] and check["scope_refs_truncated"] is False


def test_one_report_costs_three_read_tokens(app, seller, paid):
    from app.security import LoginThrottle

    pid, _, _ = paid
    app.state.observation_read_throttle = LoginThrottle(3, 60)
    assert seller.client.get(url(pid)).status_code == 200
    assert seller.client.get(url(pid)).status_code == 429


# --- R6: authorization record consistency --------------------------------------------------------------------------------------

def _tamper(world, sql, **params):
    drop_triggers(world.app, "instruction_change_verifications")
    with db_of(world.app) as db:
        db.execute(text(sql), params)
        db.commit()


def _auth_and_versions(world):
    instr = world.seller.client.get(url(world.pid)).json()["payment_instructions"]
    return instr["authorizations"][0], instr


def test_an_approver_who_is_not_the_enrolled_buyer_is_flagged_and_never_verified(world):
    assert world.decide(world.fresh()).status_code == 200
    _tamper(world, "UPDATE instruction_change_verifications SET approver_email = :e", e="someone-else@example.org")
    auth_row, instr = _auth_and_versions(world)
    assert "APPROVER_EMAIL_DOES_NOT_MATCH_AUTHENTICATOR" in auth_row["record_problems"] and auth_row["record_consistent"] is False
    assert auth_row["label"] == "UNVERIFIED_INCONSISTENT_RECORD"
    assert instr["versions"][1]["verification_status"] == "PENDING_VERIFICATION" and instr["changes"][0]["state"] == "VERIFICATION_RECORD_INCONSISTENT"


def test_an_authenticator_of_another_project_counts_as_a_missing_authenticator(world, r2app):
    from tests.test_instruction_verification import enroll

    assert world.decide(world.fresh()).status_code == 200
    other = make_project(world.seller)
    assert record(world.seller, other).status_code == 201
    foreign_id, _ = enroll(r2app, other)
    _tamper(world, "UPDATE instruction_change_verifications SET authenticator_id = :a", a=foreign_id)
    auth_row, instr = _auth_and_versions(world)
    assert "AUTHENTICATOR_NOT_IN_PROJECT" in auth_row["record_problems"] and auth_row["label"] == "UNVERIFIED_INCONSISTENT_RECORD"
    assert instr["versions"][1]["verification_status"] == "PENDING_VERIFICATION"


def test_a_verification_pointing_at_another_projects_change_is_flagged(world, r2app):
    assert world.decide(world.fresh()).status_code == 200
    other = make_project(world.seller)
    assert record(world.seller, other).status_code == 201
    foreign_change = propose(world.seller, other).json()["id"]
    _tamper(world, "UPDATE instruction_change_verifications SET change_id = :c", c=foreign_change)
    auth_row, instr = _auth_and_versions(world)
    assert "CHANGE_NOT_IN_PROJECT" in auth_row["record_problems"] and auth_row["label"] == "UNVERIFIED_INCONSISTENT_RECORD"
    assert instr["versions"][1]["verification_status"] == "PENDING_VERIFICATION"


def test_a_malformed_verification_record_is_reported_not_fatal():
    from types import SimpleNamespace

    row = SimpleNamespace(id="v1", change_id="c1", outcome="BOGUS", authenticator_id=None, approver_email=None, from_fingerprint=None,
                          proposed_fingerprint=None, applied_instruction_id=None, trust_basis=None, created_at=None)
    problems = unified_report._verification_problems(row, None, {}, {})
    assert "UNKNOWN_OUTCOME" in problems and "CHANGE_NOT_IN_PROJECT" in problems
    shown = unified_report._safe_verification_row(row, problems)
    assert shown["record_consistent"] is False and shown["label"] == "UNVERIFIED_INCONSISTENT_RECORD"
    assert shown["real_world_identity_verified"] is False
    assert unified_report._label("BOGUS", problems) == "UNVERIFIED_INCONSISTENT_RECORD"


def test_the_one_verification_per_change_constraint_exists_and_rejects_a_second_row(world):
    from sqlalchemy import inspect
    from sqlalchemy.exc import IntegrityError

    assert world.decide(world.fresh()).status_code == 200
    unique_sets = [c["column_names"] for c in inspect(world.app.state.engine).get_unique_constraints("instruction_change_verifications")]
    unique_sets += [i["column_names"] for i in inspect(world.app.state.engine).get_indexes("instruction_change_verifications") if i["unique"]]
    assert ["change_id"] in unique_sets
    with db_of(world.app) as db:
        challenge_cols = [r[1] for r in db.execute(text("PRAGMA table_info(instruction_verification_challenges)"))]
        listed = ", ".join(challenge_cols)
        selected = ", ".join("'second-challenge-id'" if c == "id" else c for c in challenge_cols)
        db.execute(text(f"INSERT INTO instruction_verification_challenges ({listed}) SELECT {selected} FROM instruction_verification_challenges"))
        verification_cols = [r[1] for r in db.execute(text("PRAGMA table_info(instruction_change_verifications)"))]
        listed = ", ".join(verification_cols)
        selected = ", ".join("'second-verification-id'" if c == "id" else "'second-challenge-id'" if c == "challenge_id" else c
                             for c in verification_cols)
        with pytest.raises(IntegrityError):
            db.execute(text(f"INSERT INTO instruction_change_verifications ({listed}) SELECT {selected} FROM instruction_change_verifications"))
        db.rollback()
