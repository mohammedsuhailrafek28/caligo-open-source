"""R4 recovery cases: deterministic, evidence-cited, advisory, read-only."""
import json
import re
import threading
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session as DbSession

from app import schemas
from app.models import Base, PaymentObservation, PaymentRecord, ProviderEvidenceGap
from app.payment_provider import ProviderInvoiceState as P
from app.services import observation_compare, recovery, recovery_catalog
from app.services.payments import invoice_number_for
from tests.test_instruction_integrity import hold, propose, ready, pid  # noqa: F401 (fixtures)
from tests.test_payment_observations import claim, csv_of, ledger, line, observations, paid, snapshot, unpaid  # noqa: F401 (fixtures)
from tests.test_payments import make_project, sent_invoice, sync


def cases(seller, pid, **params):
    response = seller.client.get(f"/api/v1/projects/{pid}/recovery-cases", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def of(body, category):
    return [c for c in body["cases"] if c["category"] == category]


def cited(case, kind):
    return [c["id"] for c in case["citations"] if c["kind"] == kind]


def row_counts(app):
    with DbSession(app.state.engine) as db:
        return {name: db.scalar(select(func.count()).select_from(table)) for name, table in Base.metadata.tables.items()}


# --- divergence classification ---------------------------------------------------------------------------------------------

def test_a_provider_payment_missing_from_the_ledger_is_a_case_citing_real_observations(seller, paid):
    pid, _, number = paid
    assert ledger(seller, pid, csv_of(line(number, txn="TXN-OTHER-0001", row_id="R1"))).status_code == 201
    body = cases(seller, pid)
    [case] = of(body, "LEDGER_MISSING_PROVIDER_PAYMENT")
    assert case["confidence"] == "PROVIDER_EVIDENCE" and case["advisory_only"] is True
    stored = {o["id"]: o for o in observations(seller, pid)}
    ids = cited(case, "OBSERVATION")
    assert ids and all(i in stored for i in ids)
    assert all(stored[i]["source"] == "PROVIDER_API" for i in ids)  # the thing it rests on is provider evidence
    assert case["current_state"]["authoritative_status"] == "PAID" and case["citations_verified"] is True


def test_a_claim_the_complete_provider_evidence_does_not_contain_is_not_corroborated(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    body = cases(seller, pid)
    [case] = of(body, "CLAIM_NOT_CORROBORATED")
    assert case["confidence"] == "CLAIM_VS_PROVIDER" and case["evidence_complete"] is True
    sources = {c["source"]: c["confidence"] for c in case["citations"] if c["kind"] == "OBSERVATION"}
    assert sources["SELLER_ENTRY"] == "UNVERIFIED_CLAIM"  # the claim stays a claim
    assert "Do not treat the claim as a received payment" in case["recommended_next_action"]


def test_amount_and_currency_disagreements_are_one_conflict_case_citing_both_sides(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-PROVIDER-1", amount_minor=70000).status_code == 201
    assert claim(seller, pid, number, transaction_id="TXN-CUR-1", currency="EUR", claim_id="C-EUR").status_code == 201
    [case] = of(cases(seller, pid), "AMOUNT_OR_CURRENCY_MISMATCH")
    assert case["severity"] == "CONFLICT"
    assert {"CLAIM_AMOUNT_MISMATCH", "CLAIM_CURRENCY_MISMATCH"} <= set(case["derived_from"])
    assert {c["source"] for c in case["citations"]} == {"SELLER_ENTRY", "PROVIDER_API"}


def test_duplicate_references_from_two_submissions_are_a_case_and_nothing_is_merged(seller, paid):
    pid, _, number = paid
    assert ledger(seller, pid, csv_of(line(number, txn="TXN-DUP-0001", row_id="R1"))).status_code == 201
    assert claim(seller, pid, number, transaction_id="TXN-DUP-0001").status_code == 201
    [case] = of(cases(seller, pid), "DUPLICATE_REFERENCE")
    assert {c["source"] for c in case["citations"]} == {"MERCHANT_IMPORT", "SELLER_ENTRY"}
    assert len([o for o in observations(seller, pid) if o["source"] != "PROVIDER_API"]) == 2


def test_a_provider_transaction_id_reused_on_another_payment_becomes_a_case_citing_the_payment_and_its_event(app, seller, unpaid):
    pid, first, _ = unpaid
    second_project = make_project(seller)
    second = sent_invoice(seller, second_project)
    for payment in (first, second):
        app.state.payment_provider.simulate_payment(payment["paypal_invoice_id"], transaction_id="TXN-REUSED-9")
    assert sync(seller, pid).status_code == 200 and sync(seller, second_project).status_code == 200
    [case] = of(cases(seller, second_project), "DUPLICATE_REFERENCE")
    assert case["derived_from"] == ["DUPLICATE_TRANSACTION_ID"] and case["severity"] == "CONFLICT"
    assert cited(case, "PAYMENT_RECORD") == [second["id"]] and len(cited(case, "PAYMENT_EVENT")) == 1
    assert case["citations_verified"] is True


def test_a_provider_state_regression_is_a_case_citing_both_state_observations(app, seller, paid):
    pid, payment, _ = paid
    app.state.payment_provider.simulate_state(payment["paypal_invoice_id"], P.SENT)
    sync(seller, pid)
    [case] = of(cases(seller, pid), "PROVIDER_STATE_REGRESSION")
    assert case["severity"] == "CONFLICT" and len(cited(case, "OBSERVATION")) >= 2
    assert "PROVIDER_STATE_REGRESSION" in case["derived_from"]


def test_a_reconciliation_anomaly_is_a_case_citing_the_payment_record(app, seller, paid):
    pid, payment, _ = paid
    app.state.payment_provider.simulate_external_payment(payment["paypal_invoice_id"])
    sync(seller, pid)
    body = cases(seller, pid)
    assert of(body, "EXTERNAL_PAYMENT") or of(body, "RECONCILIATION_ANOMALY")
    anomaly = (of(body, "EXTERNAL_PAYMENT") + of(body, "RECONCILIATION_ANOMALY"))[0]
    assert payment["id"] in cited(anomaly, "PAYMENT_RECORD")


# --- incomplete evidence never means "missing" ------------------------------------------------------------------------------

def test_a_provider_evidence_gap_gives_an_incomplete_case_and_no_missing_payment_conclusion(app, seller, unpaid):
    pid, payment, number = unpaid
    with DbSession(app.state.engine) as db:
        db.add(ProviderEvidenceGap(project_id=pid, payment_id=payment["id"], kind="PAYMENT", reason="OVERSIZED_AMOUNT",
                                   raw_sha256="a" * 64, dedupe_key="b" * 64))
        gap_id = db.scalar(select(ProviderEvidenceGap.id))
        db.commit()
    assert claim(seller, pid, number, transaction_id="TXN-NOT-STORED-1").status_code == 201
    body = cases(seller, pid)
    [case] = of(body, "EVIDENCE_INCOMPLETE")
    assert case["confidence"] == "INCOMPLETE_EVIDENCE" and case["evidence_complete"] is False
    assert "cannot tell whether a payment is missing" in case["explanation"]
    assert gap_id in cited(case, "EVIDENCE_GAP")
    assert not of(body, "CLAIM_NOT_CORROBORATED") and not of(body, "LEDGER_MISSING_PROVIDER_PAYMENT")
    assert all(c["confidence"] == "INCOMPLETE_EVIDENCE" for c in body["cases"] if c["payment_id"])


def test_truncated_claim_windows_give_an_incomplete_case_and_no_missing_ledger_line(seller, paid, monkeypatch):
    pid, _, number = paid
    rows = [line(number, amount=100 + i, txn=f"TXN-TR-{i:04d}", row_id=f"R{i}") for i in range(6)]
    assert ledger(seller, pid, csv_of(*rows)).status_code == 201
    monkeypatch.setattr(observation_compare, "MAX_OBSERVATIONS_PER_PAYMENT", 2)
    body = cases(seller, pid)
    assert of(body, "EVIDENCE_INCOMPLETE") and not of(body, "LEDGER_MISSING_PROVIDER_PAYMENT")
    assert any("truncated" in " ".join(c["confidence_limits"]) for c in body["cases"])


def test_claims_with_no_provider_evidence_at_all_are_awaiting_evidence_not_missing():
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rec = NS(id="p1", currency="USD", amount_minor=80000, invoice_number="INV1", provider_status="UNVERIFIED")
    claim_row = NS(id="o1", source="SELLER_ENTRY", kind="PAYMENT", confidence="UNVERIFIED_CLAIM", payload_sha256="h", observed_at=now,
                   source_event_id="TXN-EARLY-0001", currency="USD", amount_minor=80000, asserted_at=None, provider_state=None,
                   instrument=None, import_id=None, source_reference="r")
    comparison = observation_compare.compare_payment(rec, [claim_row], ledger_present=False)
    parts = recovery._finding_parts(comparison)
    assert list(parts) == [schemas.RecoveryCategory.AWAITING_PROVIDER_SYNC]
    case = recovery._assemble("proj", "p1", schemas.RecoveryCategory.AWAITING_PROVIDER_SYNC, parts[schemas.RecoveryCategory.AWAITING_PROVIDER_SYNC],
                              {}, evidence_complete=True, extra_limits=[])
    assert "says nothing about whether a payment was made" in case.explanation and case.confidence.value == "UNVERIFIED_CLAIM_ONLY"


# --- instruction holds and buyer authorization (R1/R2) ----------------------------------------------------------------------

def test_a_pending_instruction_change_is_a_buyer_authorization_case_and_stays_a_hold(seller, ready):
    pid = ready
    change = propose(seller, pid).json()
    held = hold(seller, pid)
    body = cases(seller, pid)
    [case] = of(body, "PENDING_BUYER_AUTHORIZATION")
    assert case["payment_id"] is None and case["confidence"] == "INSTRUCTION_RECORD"
    assert change["id"] in cited(case, "INSTRUCTION_CHANGE") and cited(case, "INSTRUCTION_VERSION")
    assert case["current_state"]["hold_active"] is True and case["current_state"]["release_requires"] == "BUYER_SIDE_VERIFICATION"
    assert "seller cannot approve it" in case["recommended_next_action"]
    assert case["citations_verified"] is True
    assert hold(seller, pid) == held and hold(seller, pid)["active"] is True  # reading cases never touches the hold


def test_payment_cases_report_that_a_hold_is_active(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    from tests.test_instruction_integrity import record

    record(seller, pid)  # first version of an instruction (not independently verified); then propose a change
    assert propose(seller, pid).status_code in (200, 201)
    body = cases(seller, pid)
    assert of(body, "PENDING_BUYER_AUTHORIZATION") and all(c["current_state"]["hold_active"] for c in body["cases"])


# --- trust, safety and read-only behaviour ----------------------------------------------------------------------------------

def test_matching_claims_never_become_provider_facts_or_change_anything(app, seller, paid):
    pid, _, number = paid
    before = snapshot(app, seller, pid)
    assert claim(seller, pid, number, transaction_id="TXN-PROVIDER-1").status_code == 201  # matches the provider exactly
    body = cases(seller, pid)
    assert not of(body, "CLAIM_NOT_CORROBORATED") and not of(body, "AMOUNT_OR_CURRENCY_MISMATCH")  # corroboration is not a case
    for case in body["cases"]:
        assert all(not (c["source"] == "SELLER_ENTRY" and c["confidence"] != "UNVERIFIED_CLAIM") for c in case["citations"])
    assert snapshot(app, seller, pid) == before


def test_reading_cases_changes_no_stored_record(app, seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    before, snap = row_counts(app), snapshot(app, seller, pid)
    listing = cases(seller, pid)
    case_id = listing["cases"][0]["case_id"]
    assert seller.client.get(f"/api/v1/projects/{pid}/recovery-cases/{case_id}").status_code == 200
    assert row_counts(app) == before and snapshot(app, seller, pid) == snap


def test_the_recovery_api_offers_no_way_to_write(app):
    paths = {p: set(v) for p, v in app.openapi()["paths"].items() if "/recovery-cases" in p}  # R4 cases only; S2 state-recovery routes are guarded writes by design
    assert paths and all(methods == {"get"} for methods in paths.values())


def test_every_case_is_advisory_and_lists_the_prohibited_automated_actions(seller, paid, ready):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    for case in cases(seller, pid)["cases"]:
        assert case["advisory_only"] is True
        assert case["prohibited_automated_actions"] == schemas.PROHIBITED_AUTOMATED_ACTIONS


def test_no_recommendation_text_instructs_an_automated_or_financial_step():
    texts = [e.action for e in recovery_catalog.ANOMALIES.values() if e.category] + [e.action for e in recovery_catalog.HOLD_TEXT.values()]
    texts += [t[3] for t in recovery_catalog.CATEGORY_TEXT.values()]
    for t in texts:
        assert t and not re.match(r"^(refund|cancel|capture|transfer|pay|void|release|send|approve)\b", t, re.I), t
        assert not re.search(r"\b(automatically (refund|cancel|release|pay|resolve)|caligo will (refund|cancel|release|pay|resolve))\b", t, re.I), t
    assert "never releases the hold automatically" in recovery_catalog.HOLD_TEXT["UNVERIFIED_INSTRUCTION_CHANGE"].action


def test_every_reconciliation_note_code_in_the_source_has_a_recovery_mapping():
    root = Path(__file__).resolve().parents[1] / "app" / "services"
    found = set()
    for name in ("reconcile.py", "payments.py", "instructions.py"):
        source = (root / name).read_text(encoding="utf8")
        found |= set(re.findall(r'anomalies\.append\("([A-Z_]+)"\)', source))
        found |= set(re.findall(r'flag\("([A-Z_]+)"', source))
        found |= set(re.findall(r'note="([A-Z_]+)"', source))
        found |= set(re.findall(r'verification_note = "([A-Z_]+)"', source))
        found |= set(re.findall(r'\("([A-Z_]+)",\)', source))
        found |= set(re.findall(r'"([A-Z_]+_REVIEW)"', source))
        found |= set(re.findall(r'_fail\(db, rec_id, [^,]+, [^,]+, "([A-Z_]+)"', source))
        found |= set(re.findall(r'"(CREATE_OUTCOME_UNKNOWN|CREATED_[A-Z_]+|SEND_[A-Z_]+)"', source))
    ignore = {"SEND_CONFIRMED_BY_PROVIDER", "SEND_NOT_CONFIRMED_BY_PROVIDER", "SEND_INVOICE"}  # event notes / an operation name, not notes
    missing = sorted(c for c in found - ignore if recovery_catalog.entry_for(c)[0] == "UNRECOGNIZED_CODE")
    assert not missing, f"unmapped reconciliation codes: {missing}"
    assert found, "the extraction pattern found nothing: the test would be vacuous"


def test_an_unrecognized_or_hostile_note_is_never_echoed(app, seller, paid):
    pid, payment, _ = paid
    with DbSession(app.state.engine) as db:
        db.execute(update(PaymentRecord).where(PaymentRecord.id == payment["id"]).values(verification_note="<script>alert(1)</script>,WEIRD_NEW_CODE"))
        db.commit()
    body = cases(seller, pid)
    [case] = of(body, "RECONCILIATION_ANOMALY")
    assert case["derived_from"] == ["UNRECOGNIZED_CODE"]
    assert "<script" not in json.dumps(body) and "WEIRD_NEW_CODE" not in json.dumps(body)


# --- corrupted references, isolation, determinism, bounds, concurrency -------------------------------------------------------

def test_a_citation_that_does_not_resolve_to_this_project_is_flagged(app, seller, seller_factory, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    other = seller_factory("other@example.com")
    other_project = make_project(other)
    other_payment = sent_invoice(other, other_project)
    assert claim(other, other_project, invoice_number_for(other_payment["id"]), transaction_id="TXN-OTHER-9999").status_code == 201
    with DbSession(app.state.engine) as db:
        foreign = db.scalar(select(PaymentObservation.id).where(PaymentObservation.project_id == other_project))
        project = db.get(__import__("app.models", fromlist=["Project"]).Project, pid)
        result = recovery.build_cases(db, project, None, required=False)
        forged = result.cases[0].model_copy(update={"citations": [*result.cases[0].citations, schemas.CaseCitation(kind="OBSERVATION", id=foreign)]})
        [checked] = recovery._verify(db, pid, [forged])
    assert checked.citations_verified is False
    assert any("could not be re-resolved" in limit for limit in checked.confidence_limits)


def test_another_seller_cannot_read_cases_or_probe_case_ids(seller, seller_factory, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    case_id = cases(seller, pid)["cases"][0]["case_id"]
    intruder = seller_factory("intruder@example.com")
    assert intruder.client.get(f"/api/v1/projects/{pid}/recovery-cases").status_code == 404
    assert intruder.client.get(f"/api/v1/projects/{pid}/recovery-cases/{case_id}").status_code == 404
    own = make_project(intruder)
    assert intruder.client.get(f"/api/v1/projects/{own}/recovery-cases/{case_id}").status_code == 404  # another project's id: same answer
    assert intruder.client.get(f"/api/v1/projects/{own}/recovery-cases/rc_{'0' * 24}").status_code == 404
    assert intruder.client.get(f"/api/v1/projects/{own}/recovery-cases/not-a-case-id").status_code == 422
    assert cases(intruder, own)["cases"] == []  # nothing of the other project's evidence leaks in


def test_cases_are_deterministic_ids_stay_stable_and_the_basis_moves_with_the_evidence(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    first = cases(seller, pid)
    assert cases(seller, pid) == first
    [before] = of(first, "CLAIM_NOT_CORROBORATED")
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER-2", claim_id="C2").status_code == 201
    [after] = of(cases(seller, pid), "CLAIM_NOT_CORROBORATED")
    assert after["case_id"] == before["case_id"] and after["basis_sha256"] != before["basis_sha256"]
    assert after["citations_total"] > before["citations_total"]
    severities = [{"CONFLICT": 0, "WARNING": 1, "INFO": 2}[c["severity"]] for c in cases(seller, pid)["cases"]]
    assert severities == sorted(severities)


def test_filters_pagination_and_bounds(seller, paid):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    assert cases(seller, pid, category="CLAIM_NOT_CORROBORATED")["cases"]
    assert cases(seller, pid, category="PROVIDER_STATE_REGRESSION")["cases"] == []
    assert cases(seller, pid, severity="CONFLICT")["cases"] == []
    page = cases(seller, pid, limit=1, offset=1)
    assert page["total_payments"] == 1 and page["cases"] == []
    for bad in ({"limit": 51}, {"limit": 0}, {"offset": -1}, {"category": "NOPE"}):
        assert seller.client.get(f"/api/v1/projects/{pid}/recovery-cases", params=bad).status_code == 422


def test_cases_can_be_read_while_claims_and_syncs_arrive_concurrently(app, seller, paid):
    pid, _, number = paid
    statuses, barrier = [], threading.Barrier(6)

    def writer(i):
        barrier.wait()
        statuses.append(claim(seller, pid, number, transaction_id=f"TXN-CONC-{i:04d}", claim_id=f"CONC-{i}").status_code)

    def reader():
        barrier.wait()
        for _ in range(3):
            statuses.append(seller.client.get(f"/api/v1/projects/{pid}/recovery-cases").status_code)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(statuses) <= {200, 201, 429, 503}
    final = cases(seller, pid)
    [case] = of(final, "CLAIM_NOT_CORROBORATED")
    stored = [o for o in observations(seller, pid, source="SELLER_ENTRY")]
    assert case["citations_total"] >= len(stored) and case["citations_verified"] is True


def test_reads_are_throttled(app, seller, paid):
    from app.security import LoginThrottle

    pid, _, _ = paid
    app.state.observation_read_throttle = LoginThrottle(2, 60)
    assert seller.client.get(f"/api/v1/projects/{pid}/recovery-cases").status_code == 200
    assert seller.client.get(f"/api/v1/projects/{pid}/recovery-cases").status_code == 200
    assert seller.client.get(f"/api/v1/projects/{pid}/recovery-cases").status_code == 429


def test_authentication_is_required(app, paid):
    from fastapi.testclient import TestClient

    pid, _, _ = paid
    assert TestClient(app).get(f"/api/v1/projects/{pid}/recovery-cases").status_code in (401, 403)


# --- review fixes: bounded lookup, single hold read, absence-flavoured notes, no-provider hold state ------------------------

def _case_get(seller, pid, case_id):
    return seller.client.get(f"/api/v1/projects/{pid}/recovery-cases/{case_id}")


def test_case_detail_computes_one_payment_and_an_unknown_id_computes_none(seller, paid, monkeypatch):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    [case] = of(cases(seller, pid), "CLAIM_NOT_CORROBORATED")
    calls = []
    real = observation_compare.compare_project
    monkeypatch.setattr(observation_compare, "compare_project", lambda *a, **k: calls.append(k) or real(*a, **k))
    found = _case_get(seller, pid, case["case_id"])
    assert found.status_code == 200 and found.json()["case_id"] == case["case_id"]
    assert len(calls) == 1 and calls[0]["payment_id"] and calls[0]["limit"] == 1
    calls.clear()
    assert _case_get(seller, pid, "rc_" + "0" * 24).status_code == 404
    assert calls == []  # an unknown id never reads evidence


def test_a_hold_case_is_found_by_id_and_the_hold_is_computed_once_per_list(seller, ready, monkeypatch):
    from app.services import instructions

    pid = ready
    propose(seller, pid)
    calls = []
    real = instructions.compute_hold
    monkeypatch.setattr(instructions, "compute_hold", lambda *a, **k: calls.append(1) or real(*a, **k))
    [case] = of(cases(seller, pid), "PENDING_BUYER_AUTHORIZATION")
    assert len(calls) == 1
    assert _case_get(seller, pid, case["case_id"]).json()["category"] == "PENDING_BUYER_AUTHORIZATION"


def test_absence_flavoured_notes_are_not_cases_while_evidence_is_incomplete(app, seller, unpaid):
    pid, payment, _ = unpaid
    with DbSession(app.state.engine) as db:
        db.add(ProviderEvidenceGap(project_id=pid, payment_id=payment["id"], kind="PAYMENT", reason="OVERSIZED_AMOUNT",
                                   raw_sha256="a" * 64, dedupe_key="b" * 64))
        rec = db.get(PaymentRecord, payment["id"])
        rec.verification_note = "PAID_STATE_WITHOUT_MATCHING_PAYPAL_PAYMENTS,CANCELLED_WITH_PAYMENTS,AMOUNT_DECREASED"
        db.commit()
    body = cases(seller, pid)
    assert not of(body, "RECONCILIATION_ANOMALY") and of(body, "EVIDENCE_INCOMPLETE")


def test_reconciliation_anomalies_rest_on_the_reconciliation_record_not_provider_evidence(app, seller, paid):
    pid, payment, _ = paid
    with DbSession(app.state.engine) as db:
        db.get(PaymentRecord, payment["id"]).verification_note = "SYNC_FAILED"
        db.commit()
    [case] = of(cases(seller, pid), "RECONCILIATION_ANOMALY")
    assert case["confidence"] == "RECONCILIATION_RECORD"


def test_recommendations_never_tell_the_seller_to_retry_an_ambiguous_outcome():
    for entry in [*recovery_catalog.ANOMALIES.values(), recovery_catalog.REJECTED]:
        assert "Idempotency-Key" not in entry.action and "retry the action" not in entry.action.lower()


def test_hold_state_is_not_claimed_when_no_provider_is_configured(app, seller, paid, monkeypatch):
    pid, _, number = paid
    assert claim(seller, pid, number, transaction_id="TXN-NOT-AT-PROVIDER").status_code == 201
    monkeypatch.setattr(app.state, "payment_provider", None)
    body = cases(seller, pid)
    payment_cases = [c for c in body["cases"] if c["payment_id"]]
    assert payment_cases and all(c["current_state"]["hold_active"] is None for c in payment_cases)
    assert all(recovery.NO_PROVIDER_LIMIT in c["confidence_limits"] for c in payment_cases)


def test_every_r3_finding_code_is_mapped_or_deliberately_informational():
    for code in schemas.FindingCode:
        assert code.value in recovery_catalog.FINDING_CATEGORY, code.value
