"""Unified evidence report (R5): ONE deterministic, reviewer-ready document over scope/delivery (Stage 3), payment-instruction integrity and
buyer-side authorization (R1/R2) and payment observations, divergences and recovery cases (R3/R4).

A thin composition layer. It reads stored records and calls existing services; it stores nothing, calls no provider, has no clock and no
randomness, and decides nothing. Every statement is tagged with an evidence class so a reader cannot confuse a provider observation with a
seller claim, an operator-established verification or an AI interpretation. Incomplete or conflicting evidence is reported as such; this
module never computes a "resolved" payment state of its own."""
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import schemas
from app.models import (
    InstructionChangeResolution, InstructionChangeVerification, PaymentObservation, PaymentRecord, Project,
    ProviderEvidenceGap,
)
from app.payment_provider import PaymentProvider
from app.sandbox_attestation import provider_label
from app.services import authenticators, instructions, observation_compare, recovery
from app.services import buyer as buyer_service
from app.services import report as scope_report
from app.services.report_citations import Citation, parse_ref, validate
from app.services.scope import canonical_sha256, latest_baseline

REPORT_VERSION = "R5.1"
MAX_INSTRUCTION_VERSIONS = 200
MAX_CHANGES = 200
MAX_AUTHENTICATORS = 100
MAX_GAPS = 500
MAX_REFS = 5000  # scope/delivery references collected for validation

EVIDENCE_CLASSES = {
    "PROVIDER_OBSERVATION": "What the payment provider returned to Caligo's last sync, stored immutably. It is not independently audited and is only as complete as the sync.",
    "SERVER_RECORDED": "A fact Caligo's server recorded itself (a hash, a server time, a hash-chained instruction version, a mailbox-verified buyer action).",
    "SELLER_CLAIM": "Asserted by the seller or in a seller-supplied file. Nothing independent confirms it; it is never proof of a payment or approval.",
    "OPERATOR_ESTABLISHED_VERIFICATION": ("A check by an authenticator an OPERATOR created on the server host. It shows that whoever held that authenticator "
                                          "answered; it does NOT prove the real-world identity of any buyer and is not independent of the seller if the "
                                          "seller is also the operator."),
    "AI_INTERPRETATION": "An opinion of an analysis provider: labelled, schema-validated, never a fact, and always needs human review.",
    "DERIVED_ADVISORY": "Deterministic advice derived from the records above (recovery cases). It recommends human checks and never acts.",
}
AUTHORIZATION_LIMIT = EVIDENCE_CLASSES["OPERATOR_ESTABLISHED_VERIFICATION"]
LIMITATIONS = [
    "This report summarises stored records for a human reviewer. It is not legal advice and it decides no dispute.",
    "Payment state is decided only by provider reconciliation. This report shows conflicts and gaps beside that state and never resolves them.",
    "A verification by an operator-established authenticator is not proof of any person's real-world identity.",
    "Provider observations carry no provider timestamps, a transaction that disappears from a later provider reading is not detected, and there is no webhook ingestion.",
    "Evidence gaps are permanent: a payment that ever had one stays marked incomplete, and absence of evidence is then never read as absence of payment.",
    "Scope and delivery sections reuse the Stage 3 evidence report and are bounded only by the limits of that report.",
]


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# --- payment instructions and buyer-side authorization (R1/R2) ----------------------------------------------------------------

def _version_row(v, applied_ids: set[str]) -> dict[str, Any]:
    try:
        recomputes = instructions.instruction_fingerprint(v) == v.fingerprint
    except Exception:  # malformed historical row: report it, never fail the whole report
        recomputes = False
    verified = v.id in applied_ids
    return {
        "id": v.id, "version": v.version, "provider": v.provider, "merchant_email": v.merchant_email, "recipient_email": v.recipient_email,
        "currency": v.currency, "amount_minor": v.amount_minor, "fingerprint": v.fingerprint, "fingerprint_recomputes": recomputes,
        "chain_sha256": v.chain_sha256, "previous_instruction_id": v.previous_instruction_id, "provenance": v.provenance,
        "verification_status": (schemas.InstructionVerification.VERIFIED_BY_ENROLLED_SECOND_CHANNEL if verified
                                else schemas.InstructionVerification.PENDING_VERIFICATION).value,
        "evidence_class": "OPERATOR_ESTABLISHED_VERIFICATION" if verified else "SELLER_CLAIM", "created_at": _iso(v.created_at)}


def _same_address(a, b) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.strip().lower() == b.strip().lower()


def _verification_problems(row, change, versions_by_id: dict, authenticators_by_id: dict) -> list[str]:
    """Every way a stored verification record can disagree with the records it points to. Never raises on a malformed row."""
    problems = []
    if row.outcome not in {o.value for o in schemas.VerificationOutcome}:
        problems.append("UNKNOWN_OUTCOME")
    if change is None:
        return [*problems, "CHANGE_NOT_IN_PROJECT"]
    if (row.from_fingerprint, row.proposed_fingerprint) != (change.from_fingerprint, change.proposed_fingerprint):
        problems.append("FINGERPRINT_MISMATCH_WITH_CHANGE")
    auth_row = authenticators_by_id.get(row.authenticator_id)
    if auth_row is None:
        problems.append("AUTHENTICATOR_NOT_IN_PROJECT")
    else:
        if auth_row.trust_basis != row.trust_basis:
            problems.append("TRUST_BASIS_MISMATCH")
        if not _same_address(row.approver_email, auth_row.buyer_email):
            problems.append("APPROVER_EMAIL_DOES_NOT_MATCH_AUTHENTICATOR")
    applied = versions_by_id.get(row.applied_instruction_id) if row.applied_instruction_id else None
    if row.outcome == schemas.VerificationOutcome.APPLIED.value:
        if applied is None:
            problems.append("APPLIED_VERSION_NOT_IN_PROJECT")
        elif applied.fingerprint != row.proposed_fingerprint or applied.previous_instruction_id != change.from_instruction_id:
            problems.append("APPLIED_VERSION_DOES_NOT_MATCH_CHANGE")
    elif row.applied_instruction_id:
        problems.append("REJECTED_BUT_VERSION_APPLIED")
    return problems


def _label(outcome: str, problems: list[str]) -> str:
    """Only a CONSISTENT record earns its trust label; an inconsistent one is never shown as verified."""
    if problems:
        return "UNVERIFIED_INCONSISTENT_RECORD"
    return ("VERIFIED_BY_ENROLLED_SECOND_CHANNEL" if outcome == schemas.VerificationOutcome.APPLIED.value
            else "REJECTED_BY_ENROLLED_SECOND_CHANNEL")


def _safe_verification_row(row, problems: list[str]) -> dict[str, Any]:
    """A malformed historical row is reported as inconsistent, never allowed to fail the whole report."""
    try:
        return _verification_row(row, problems)
    except Exception:
        return {"id": getattr(row, "id", None), "change_id": getattr(row, "change_id", None), "outcome": None,
                "label": "UNVERIFIED_INCONSISTENT_RECORD", "evidence_class": "SERVER_RECORDED", "real_world_identity_verified": False,
                "independent_of_seller": "NOT_ESTABLISHED", "trust_limitation": AUTHORIZATION_LIMIT, "record_consistent": False,
                "record_problems": [*problems, "RECORD_UNREADABLE"]}


def _verification_row(row, problems: list[str]) -> dict[str, Any]:
    return {
        "id": row.id, "change_id": row.change_id, "outcome": row.outcome, "authenticator_id": row.authenticator_id,
        "approver_email_masked": buyer_service.mask_email(row.approver_email), "trust_basis": row.trust_basis,
        "from_fingerprint": row.from_fingerprint, "proposed_fingerprint": row.proposed_fingerprint,
        "applied_instruction_id": row.applied_instruction_id, "created_at": _iso(row.created_at),
        "evidence_class": "OPERATOR_ESTABLISHED_VERIFICATION" if not problems else "SERVER_RECORDED", "label": _label(row.outcome, problems),
        "real_world_identity_verified": False, "independent_of_seller": "NOT_ESTABLISHED", "trust_limitation": AUTHORIZATION_LIMIT,
        "record_consistent": not problems, "record_problems": problems}


def _change_state(change_id: str, verification, withdrawn: set[str], consistent: bool = True) -> str:
    if verification is not None:
        return verification.outcome if consistent else "VERIFICATION_RECORD_INCONSISTENT"
    return "WITHDRAWN_HOLD_NOT_RELEASED" if change_id in withdrawn else "PENDING_BUYER_SIDE_VERIFICATION"


def _payment_instructions(db: Session, project_id: str, provider: PaymentProvider | None, required: bool) -> tuple[dict, list[Citation]]:
    versions = instructions.versions_of(db, project_id)
    changes = instructions.all_changes(db, project_id)
    verifications = {v.change_id: v for v in db.scalars(select(InstructionChangeVerification).where(
        InstructionChangeVerification.project_id == project_id).order_by(InstructionChangeVerification.created_at, InstructionChangeVerification.id))}
    withdrawn = set(db.scalars(select(InstructionChangeResolution.change_id).where(InstructionChangeResolution.project_id == project_id)))
    auth_rows = authenticators.list_for_project(db, project_id)
    try:
        chain_ok = instructions.chain_intact(versions)
    except Exception:
        chain_ok = False
    hold, _ = recovery.hold_state(db, project_id, provider, required)
    versions_by_id, changes_by_id, auth_by_id = {v.id: v for v in versions}, {c.id: c for c in changes}, {a.id: a for a in auth_rows}
    problems_of = {v.id: _verification_problems(v, changes_by_id.get(v.change_id), versions_by_id, auth_by_id) for v in verifications.values()}
    all_rows = [_safe_verification_row(v, problems_of[v.id]) for v in verifications.values()]
    applied_ids = {v.applied_instruction_id for v in verifications.values()
                   if v.outcome == schemas.VerificationOutcome.APPLIED.value and v.applied_instruction_id and not problems_of[v.id]}
    ver_rows = all_rows[:MAX_CHANGES]
    section = {
        "hash_chain_intact": chain_ok, "version_count": len(versions), "versions_truncated": len(versions) > MAX_INSTRUCTION_VERSIONS,
        "versions": [_version_row(v, applied_ids) for v in versions[:MAX_INSTRUCTION_VERSIONS]],
        "changes_truncated": len(changes) > MAX_CHANGES, "changes": [{
            "id": c.id, "from_instruction_id": c.from_instruction_id, "from_fingerprint": c.from_fingerprint,
            "proposed_fingerprint": c.proposed_fingerprint, "recipient_email": c.recipient_email, "currency": c.currency,
            "amount_minor": c.amount_minor, "reason": c.reason, "reason_evidence_class": "SELLER_CLAIM", "provenance": c.provenance,
            "state": _change_state(c.id, verifications.get(c.id), withdrawn, not problems_of.get(getattr(verifications.get(c.id), "id", None))),
            "created_at": _iso(c.created_at)} for c in changes[:MAX_CHANGES]],
        "authorizations": ver_rows, "authorizations_truncated": len(all_rows) > MAX_CHANGES,
        "authorization_records_inconsistent": sum(1 for r in all_rows if not r["record_consistent"]),
        "authorization_limitation": AUTHORIZATION_LIMIT,
        "authenticators": [authenticators.view_of(db, a).model_dump(mode="json") for a in auth_rows[:MAX_AUTHENTICATORS]],
        "authenticators_truncated": len(auth_rows) > MAX_AUTHENTICATORS,
        "hold": {"active": hold.active, "reasons": [r.value for r in hold.reasons], "pending_change_ids": hold.pending_change_ids,
                 "evaluated_with_provider": provider is not None,
                 "note": "A hold blocks operations through Caligo only; it never freezes funds held by PayPal. Only buyer-side verification releases it."}}
    refs: list[Citation] = [("INSTRUCTION_VERSION", v.id, None) for v in versions] + [("INSTRUCTION_CHANGE", c.id, None) for c in changes]
    for v in verifications.values():
        refs += [("INSTRUCTION_CHANGE", v.change_id, None), ("AUTHENTICATOR", v.authenticator_id, None)]
        refs += [("INSTRUCTION_VERSION", v.applied_instruction_id, None)] if v.applied_instruction_id else []
    return section, refs


# --- scope and delivery (Stage 3) ----------------------------------------------------------------------------------------------

_SCOPE_KEYS = ("agreement", "changes", "requirement_mapping", "buyer_evidence", "seller_claims", "ai_judgments", "unresolved")


def _scope_delivery(db: Session, project: Project) -> tuple[dict, list[Citation]]:
    if latest_baseline(db, project.id) is None:
        return {"available": False, "reason": "No original agreement has been frozen, so there is no scope or delivery evidence to report."}, []
    evidence = scope_report.build_report(db, project)
    section = {"available": True, "evidence_report_sha256": evidence["report_sha256"], "evidence_classes_used": {
        "agreement_and_changes": "SERVER_RECORDED", "buyer_evidence": "SERVER_RECORDED (mailbox-verified; not legal identity)",
        "seller_claims": "SELLER_CLAIM", "ai_judgments": "AI_INTERPRETATION"}, **{k: evidence[k] for k in _SCOPE_KEYS}}
    refs: list[str] = []
    for group in (evidence["unresolved"], evidence["seller_claims"]):
        refs += [r for item in group for r in item.get("refs", [])]
    if evidence["ai_judgments"]:
        refs += [r for f in evidence["ai_judgments"]["findings"] for r in f.get("source_refs", [])]
    cites: list[Citation] = []
    section["refs_truncated"] = len(refs) > MAX_REFS
    for ref in refs[:MAX_REFS]:
        parsed = parse_ref(ref)
        cites.append((parsed[0], parsed[1], None) if parsed else ("malformed_reference", str(ref)[:60], None))
    return section, cites


# --- payments: claims vs provider observations, gaps, recovery cases (R3/R4) --------------------------------------------------

def _agreement_label(pc: schemas.PaymentComparison) -> str:
    if not pc.provider_evidence_complete:
        return "NOT_COMPARABLE_EVIDENCE_INCOMPLETE"
    if pc.claims_truncated:
        return "NOT_COMPARABLE_CLAIMS_TRUNCATED"  # only the newest claims were summed
    if pc.claimed_paid_minor == 0:
        return "NO_CLAIMS"
    return "CLAIMED_TOTAL_MATCHES_PROVIDER" if pc.claimed_paid_minor == pc.provider_paid_minor else "CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER"


def _observation_counts(db: Session, project_id: str, ids: list[str]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    if not ids:
        return counts
    for payment_id, source, n in db.execute(select(PaymentObservation.payment_id, PaymentObservation.source, func.count()).where(
            PaymentObservation.project_id == project_id, PaymentObservation.payment_id.in_(ids))
            .group_by(PaymentObservation.payment_id, PaymentObservation.source)):
        counts.setdefault(payment_id, {})[source] = n
    return counts


def _gaps(db: Session, project_id: str, ids: list[str]) -> tuple[list[dict], int]:
    if not ids:
        return [], 0
    where = (ProviderEvidenceGap.project_id == project_id, ProviderEvidenceGap.payment_id.in_(ids))
    total = db.scalar(select(func.count()).select_from(ProviderEvidenceGap).where(*where)) or 0
    rows = db.scalars(select(ProviderEvidenceGap).where(*where).order_by(ProviderEvidenceGap.observed_at, ProviderEvidenceGap.id).limit(MAX_GAPS))
    return [{"id": g.id, "payment_id": g.payment_id, "kind": g.kind, "reason": g.reason, "raw_sha256": g.raw_sha256,
             "observed_at": _iso(g.observed_at), "evidence_class": "SERVER_RECORDED"} for g in rows], total


def _payment_row(pc: schemas.PaymentComparison, rec: PaymentRecord | None, counts: dict[str, int]) -> dict[str, Any]:
    return {
        "payment_id": pc.payment_id, "invoice_number": pc.invoice_number, "provider": rec.provider if rec else None,
        "is_simulated": bool(rec and rec.provider == schemas.PaymentProviderName.MOCK_PAYPAL.value),
        "provider_provenance": provider_label(rec.provider, rec.id) if rec else "UNLABELLED_PROVIDER",
        "invoice_amount_minor": rec.amount_minor if rec else None, "currency": rec.currency if rec else None,
        "authoritative_status": pc.authoritative_status.value, "authoritative_status_source": "PROVIDER_RECONCILIATION",
        "provider_observations": {"evidence_class": "PROVIDER_OBSERVATION", "paid_minor": pc.provider_paid_minor,
                                  "evidence_complete": pc.provider_evidence_complete, "evidence_gap_count": pc.evidence_gap_count},
        "seller_claims": {"evidence_class": "SELLER_CLAIM", "claimed_paid_minor": pc.claimed_paid_minor, "claims_truncated": pc.claims_truncated},
        "claim_vs_provider": _agreement_label(pc), "observation_counts_by_source": dict(sorted(counts.items())),
        "findings": [f.model_dump(mode="json") for f in pc.findings]}


def _payment_citations(comparison: schemas.ObservationComparison, cases: list[schemas.RecoveryCase]) -> list[Citation]:
    cites: list[Citation] = []
    for pc in comparison.payments:
        for f in pc.findings:
            cites += [("OBSERVATION", c.observation_id, pc.payment_id) for c in f.citations]
            cites += [("EVIDENCE_GAP", g, pc.payment_id) for g in f.gap_ids]
    for case in cases:
        cites += [(c.kind.value, c.id, case.payment_id) for c in case.citations]
    return cites


def _payments(db: Session, project: Project, provider: PaymentProvider | None, required: bool, limit: int, offset: int) -> tuple[dict, list[Citation]]:
    comparison = observation_compare.compare_project(db, project.id, limit=limit, offset=offset)
    case_list = recovery.build_cases(db, project, provider, required=required, limit=limit, offset=offset, comparison=comparison)
    ids = [p.payment_id for p in comparison.payments]
    records = {r.id: r for r in db.scalars(select(PaymentRecord).where(PaymentRecord.project_id == project.id, PaymentRecord.id.in_(ids)))} if ids else {}
    counts = _observation_counts(db, project.id, ids)
    gaps, gap_total = _gaps(db, project.id, ids)
    cases = [c.model_dump(mode="json") | {"evidence_class": "DERIVED_ADVISORY"} for c in case_list.cases]
    section = {
        "authority": comparison.authority, "page": {"total_payments": comparison.total_payments, "offset": comparison.offset,
                                                    "limit": comparison.limit, "returned": len(ids),
                                                    "more": comparison.offset + len(ids) < comparison.total_payments},
        "payments": [_payment_row(pc, records.get(pc.payment_id), counts.get(pc.payment_id, {})) for pc in comparison.payments],
        "evidence_gaps": {"total": gap_total, "shown": len(gaps), "truncated": gap_total > len(gaps), "items": gaps,
                          "warning": "A gap means part of what the provider reported could not be stored. Absence of evidence is then not absence of payment."},
        "recovery_cases": {"evidence_class": "DERIVED_ADVISORY", "advisory_only": True, "cases_sha256": case_list.cases_sha256, "items": cases}}
    return section, _payment_citations(comparison, case_list.cases)


# --- assembly -----------------------------------------------------------------------------------------------------------------

def _summary(scope: dict, instr: dict, pay: dict, citation_check: dict) -> dict[str, Any]:
    cases = pay["recovery_cases"]["items"]
    incomplete = [p["payment_id"] for p in pay["payments"] if not p["provider_observations"]["evidence_complete"]]
    return {
        "payments_on_page": len(pay["payments"]), "total_payments": pay["page"]["total_payments"],
        "payments_with_incomplete_provider_evidence": incomplete, "recovery_cases_on_page": len(cases),
        "recovery_cases_by_severity": {s: sum(1 for c in cases if c["severity"] == s) for s in ("CONFLICT", "WARNING", "INFO")},
        "scope_delivery_available": scope["available"], "delivery_status": scope["buyer_evidence"]["delivery_status"] if scope["available"] else None,
        "scope_unresolved_items": len(scope["unresolved"]) if scope["available"] else None,
        "instruction_versions": instr["version_count"], "instruction_hash_chain_intact": instr["hash_chain_intact"],
        "payment_hold_active": instr["hold"]["active"], "pending_instruction_changes": len(instr["hold"]["pending_change_ids"]),
        "authorization_records": len(instr["authorizations"]),
        "authorization_records_inconsistent": instr["authorization_records_inconsistent"],
        "citations_all_resolved": citation_check["all_resolved"],
        "no_conclusion_is_drawn": "Where evidence is incomplete, conflicting or unverified the report says so and does not resolve it."}


def build_unified_report(db: Session, project: Project, provider: PaymentProvider | None, *, required: bool, payments_limit: int = 25,
                         payments_offset: int = 0) -> dict[str, Any]:
    scope, scope_cites = _scope_delivery(db, project)
    instr, instr_cites = _payment_instructions(db, project.id, provider, required)
    pay, pay_cites = _payments(db, project, provider, required, payments_limit, payments_offset)
    citation_check = {**validate(db, project.id, [*scope_cites, *instr_cites, *pay_cites]),
                      "scope_refs_truncated": bool(scope.get("refs_truncated")),
                      "coverage": ("Checked: observation, gap and event citations against project AND payment; instruction, change and authenticator "
                                   "references on authorization records; references in scope unresolved items, seller claims and AI findings. "
                                   "Instruction versions and changes listed from the project's own rows are existence-only. 'all_resolved' covers "
                                   "only what was checked.")}
    citation_check["all_resolved"] = citation_check["all_resolved"] and not citation_check["scope_refs_truncated"]
    body = {
        "report_version": REPORT_VERSION, "kind": "UNIFIED_EVIDENCE_REPORT", "project": {"id": project.id, "title": project.title},
        "evidence_classes": EVIDENCE_CLASSES, "summary": _summary(scope, instr, pay, citation_check), "scope_and_delivery": scope,
        "payment_instructions": instr, "payments": pay, "citation_check": citation_check,
        "truncation": {"payments_page": pay["page"], "evidence_gaps_truncated": pay["evidence_gaps"]["truncated"],
                       "instruction_versions_truncated": instr["versions_truncated"], "instruction_changes_truncated": instr["changes_truncated"],
                       "authenticators_truncated": instr["authenticators_truncated"], "authorizations_truncated": instr["authorizations_truncated"]},
        "limitations": LIMITATIONS}
    return {**body, "report_sha256": canonical_sha256(body)}
