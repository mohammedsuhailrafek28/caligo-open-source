"""Recovery cases (R4): evidence-cited, ADVISORY explanations of where records disagree. Pure read path.

Everything here is derived on every request from stored, immutable records: R3 provider observations, evidence gaps and the R3 comparison,
the reconciliation record (`PaymentRecord`, `PaymentEvent`) and the R1/R2 instruction records. Nothing is stored, nothing is written,
no provider is called, and no recommendation is executed by Caligo: the module imports no writer, no provider port and no
reconciliation state machine. Merchant claims stay claims: a claim is only ever shown beside provider evidence, never as it."""
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import schemas
from app.models import (
    InstructionChangeRequest, PaymentEvent, PaymentInstruction, PaymentObservation, PaymentRecord, Project, ProviderEvidenceGap,
)
from app.payment_provider import PaymentProvider
from app.schemas import (
    CaseCitation, CaseCitationKind as Kind, FindingSeverity as Sev, RecoveryCategory as C, RecoveryConfidence as Conf,
)
from app.services import instructions, observation_compare
from app.services.recovery_catalog import (
    ABSENCE_DEPENDENT, ABSENCE_DEPENDENT_NOTES, CATEGORY_TEXT, FINDING_CATEGORY, GENERIC_HOLD, HOLD_TEXT, entry_for,
)
from app.services.scope import canonical_sha256

MAX_CASE_CITATIONS = 20
MAX_NOTE_CODES = 20
MAX_DETAILS = 3
MAX_LOOKUP_PAYMENTS = 5000  # single-case lookup matches ids over at most this many payments, then computes ONE payment
MAX_EVENTS_PER_PAYMENT = 3
VERIFY_CHUNK = 500
NO_PROVIDER_LIMIT = "No payment provider is configured, so the hold state was not evaluated in full."
_SEVERITY_RANK = {Sev.CONFLICT: 0, Sev.WARNING: 1, Sev.INFO: 2}
_INSTRUCTION_CATEGORIES = frozenset({C.INSTRUCTION_INTEGRITY, C.INSTRUCTION_HOLD, C.PENDING_BUYER_AUTHORIZATION})


@dataclass
class _Part:
    """What one source (a finding, an anomaly code, a hold reason) contributes to a case."""

    severity: Sev
    code: str
    detail: str
    action: str | None = None
    citations: list[CaseCitation] = field(default_factory=list)


def _worst(severities: list[Sev]) -> Sev:
    return min(severities, key=lambda s: _SEVERITY_RANK[s])


def _obs_citation(c: schemas.ObservationCitation) -> CaseCitation:
    return CaseCitation(kind=Kind.OBSERVATION, id=c.observation_id, source=c.source, confidence=c.confidence, payload_sha256=c.payload_sha256)


def _case_id(project_id: str, payment_id: str | None, category: C, key: str = "") -> str:
    return "rc_" + canonical_sha256(["recovery-case", project_id, payment_id, category.value, key])[:24]


def _assemble(project_id: str, payment_id: str | None, category: C, parts: list[_Part], state: dict, *, evidence_complete: bool,
              extra_limits: list[str], key: str = "") -> schemas.RecoveryCase:
    severity_default, confidence, explanation, action, limits = CATEGORY_TEXT[category]
    if not evidence_complete and category not in _INSTRUCTION_CATEGORIES:
        confidence = Conf.INCOMPLETE_EVIDENCE
        extra_limits = [*extra_limits, "The provider evidence for this payment is incomplete, so no conclusion about a missing payment is drawn."]
    parts = sorted(parts, key=lambda p: (_SEVERITY_RANK[p.severity], p.code, p.detail))
    details = list(dict.fromkeys(p.detail for p in parts if p.detail))
    if details:
        explanation = f"{explanation} Details: {'; '.join(details[:MAX_DETAILS])}{' …' if len(details) > MAX_DETAILS else ''}"
    actions = list(dict.fromkeys([action, *(p.action for p in parts if p.action)]))
    cited = {(c.kind.value, c.id): c for p in parts for c in p.citations}
    ordered = [cited[k] for k in sorted(cited)]
    shown = ordered if len(ordered) <= MAX_CASE_CITATIONS else ordered[:MAX_CASE_CITATIONS // 2] + ordered[-(MAX_CASE_CITATIONS // 2):]
    derived = sorted({p.code for p in parts})
    severity = _worst([p.severity for p in parts]) if parts else severity_default
    text = " ".join(actions)
    basis = canonical_sha256({"category": category.value, "severity": severity.value, "derived": derived, "state": state,
                              "citations": [list(k) for k in sorted(cited)], "explanation": explanation})
    return schemas.RecoveryCase(
        case_id=_case_id(project_id, payment_id, category, key), project_id=project_id, payment_id=payment_id, category=category,
        severity=severity, confidence=confidence, confidence_limits=[*limits, *extra_limits], current_state=state,
        explanation=explanation, recommended_next_action=text, citations=shown, citations_total=len(ordered), citations_verified=True,
        derived_from=derived, evidence_complete=evidence_complete, basis_sha256=basis)


def _payment_state(pc: schemas.PaymentComparison, rec: PaymentRecord, hold_active: bool) -> dict:
    return {"invoice_number": pc.invoice_number, "authoritative_status": pc.authoritative_status.value,
            "invoice_amount_minor": rec.amount_minor, "currency": rec.currency, "provider_paid_minor": pc.provider_paid_minor,
            "claimed_paid_minor": pc.claimed_paid_minor, "evidence_complete": pc.provider_evidence_complete,
            "evidence_gap_count": pc.evidence_gap_count, "claims_truncated": pc.claims_truncated, "hold_active": hold_active}


def _finding_parts(pc: schemas.PaymentComparison) -> dict[C, list[_Part]]:
    by_category: dict[C, list[_Part]] = {}
    for f in pc.findings:
        category = FINDING_CATEGORY.get(f.code.value)
        if category is None:  # corroboration (good news) or a code this version does not map
            continue
        if category in ABSENCE_DEPENDENT and not pc.provider_evidence_complete:
            continue  # belt and braces: an absence is never asserted from incomplete evidence
        citations = [_obs_citation(c) for c in f.citations] + [CaseCitation(kind=Kind.EVIDENCE_GAP, id=g) for g in f.gap_ids]
        by_category.setdefault(category, []).append(_Part(f.severity, f.code.value, f.summary, None, citations))
    return by_category


def _anomaly_parts(rec: PaymentRecord, events: list[PaymentEvent], evidence_complete: bool) -> dict[C, list[_Part]]:
    note = rec.verification_note
    if not note:
        return {}
    matching = [e for e in events if e.note == note][:3]
    cites = [CaseCitation(kind=Kind.PAYMENT_RECORD, id=rec.id), *(CaseCitation(kind=Kind.PAYMENT_EVENT, id=e.id) for e in matching)]
    by_category: dict[C, list[_Part]] = {}
    for raw in note.split(",")[:MAX_NOTE_CODES]:
        code, entry = entry_for(raw.strip())
        if entry.category is None or (code in ABSENCE_DEPENDENT_NOTES and not evidence_complete):
            continue
        by_category.setdefault(entry.category, []).append(_Part(entry.severity, code, entry.explanation, entry.action, list(cites)))
    return by_category


def _recent_anomaly_events(db: Session, project_id: str, ids: list[str]) -> dict[str, list[PaymentEvent]]:
    """Newest few ANOMALY events per payment (a per-payment window, so one noisy payment cannot starve another's citations)."""
    rank = func.row_number().over(partition_by=PaymentEvent.payment_id,
                                  order_by=(PaymentEvent.created_at.desc(), PaymentEvent.id.desc())).label("rn")
    ranked = (select(PaymentEvent.id, rank).where(PaymentEvent.project_id == project_id, PaymentEvent.payment_id.in_(ids),
                                                   PaymentEvent.event_type == schemas.PaymentEventType.ANOMALY.value).subquery())
    events: dict[str, list[PaymentEvent]] = {}
    for e in db.scalars(select(PaymentEvent).join(ranked, PaymentEvent.id == ranked.c.id).where(ranked.c.rn <= MAX_EVENTS_PER_PAYMENT)
                        .order_by(PaymentEvent.created_at.desc(), PaymentEvent.id.desc())):
        events.setdefault(e.payment_id, []).append(e)
    return events


def _payment_cases(db: Session, project_id: str, comparison: schemas.ObservationComparison,
                   hold_active: bool | None) -> list[schemas.RecoveryCase]:
    ids = [p.payment_id for p in comparison.payments]
    if not ids:
        return []
    records = {r.id: r for r in db.scalars(select(PaymentRecord).where(PaymentRecord.project_id == project_id, PaymentRecord.id.in_(ids)))}
    events = _recent_anomaly_events(db, project_id, ids)
    cases: list[schemas.RecoveryCase] = []
    for pc in comparison.payments:
        rec = records.get(pc.payment_id)
        if rec is None:
            continue
        state = _payment_state(pc, rec, hold_active)
        merged = _finding_parts(pc)
        for category, parts in _anomaly_parts(rec, events.get(rec.id, []), pc.provider_evidence_complete).items():
            merged.setdefault(category, []).extend(parts)
        limits = ["Claims were truncated: only the newest claims were compared."] if pc.claims_truncated else []
        if hold_active is None:
            limits.append(NO_PROVIDER_LIMIT)
        for category, parts in merged.items():
            cases.append(_assemble(project_id, rec.id, category, parts, state, evidence_complete=pc.provider_evidence_complete, extra_limits=limits))
    return cases


def _hold_cases(db: Session, project_id: str, hold) -> list[schemas.RecoveryCase]:
    if not hold.active:
        return []
    versions = instructions.versions_of(db, project_id)
    current = versions[-1] if versions else None
    pending = {c.id: c for c in instructions.pending_changes(db, project_id)}
    cases = []
    for reason in hold.reasons:
        entry = HOLD_TEXT.get(reason.value, GENERIC_HOLD)
        cites: list[CaseCitation] = []
        if current is not None:
            cites.append(CaseCitation(kind=Kind.INSTRUCTION_VERSION, id=current.id))
        if reason.value == "UNVERIFIED_INSTRUCTION_CHANGE":
            for change in pending.values():
                cites += [CaseCitation(kind=Kind.INSTRUCTION_CHANGE, id=change.id),
                          CaseCitation(kind=Kind.INSTRUCTION_VERSION, id=change.from_instruction_id)]
        state = {"hold_active": True, "hold_reasons": ",".join(r.value for r in hold.reasons), "pending_changes": len(pending),
                 "current_instruction_version": current.version if current is not None else None,
                 "hold_scope": hold.scope, "release_requires": hold.release_requires}
        part = _Part(entry.severity, reason.value, entry.explanation, entry.action, cites)
        cases.append(_assemble(project_id, None, entry.category, [part], state, evidence_complete=True, extra_limits=[], key=reason.value))
    return cases


def _verify(db: Session, project_id: str, cases: list[schemas.RecoveryCase]) -> list[schemas.RecoveryCase]:
    """Re-resolve every shown citation to a stored record of THIS project; a case with an unresolved citation says so."""
    wanted: dict[Kind, set[str]] = {}
    for case in cases:
        for c in case.citations:
            wanted.setdefault(c.kind, set()).add(c.id)
    models = {Kind.OBSERVATION: PaymentObservation, Kind.EVIDENCE_GAP: ProviderEvidenceGap, Kind.PAYMENT_RECORD: PaymentRecord,
              Kind.PAYMENT_EVENT: PaymentEvent, Kind.INSTRUCTION_VERSION: PaymentInstruction, Kind.INSTRUCTION_CHANGE: InstructionChangeRequest}
    found: dict[Kind, set[str]] = {}
    for kind, ids in wanted.items():
        model = models[kind]
        listed = sorted(ids)
        found[kind] = set()
        for i in range(0, len(listed), VERIFY_CHUNK):
            found[kind].update(db.scalars(select(model.id).where(model.project_id == project_id, model.id.in_(listed[i:i + VERIFY_CHUNK]))))
    out = []
    for case in cases:
        ok = all(c.id in found.get(c.kind, set()) for c in case.citations)
        out.append(case if ok else case.model_copy(update={
            "citations_verified": False,
            "confidence_limits": [*case.confidence_limits, "Some cited records could not be re-resolved to this project's stored records."]}))
    return out


def _sorted(cases: list[schemas.RecoveryCase]) -> list[schemas.RecoveryCase]:
    return sorted(cases, key=lambda c: (_SEVERITY_RANK[c.severity], c.category.value, c.payment_id or "", c.case_id))


def hold_state(db: Session, project_id: str, provider: PaymentProvider | None, required: bool):
    """Compute the hold ONCE per request. With no provider configured the full evaluation is skipped, so 'active' is not claimed either way."""
    hold = instructions.compute_hold(db, project_id, provider, required=required)
    return hold, (hold.active if provider is not None else (True if hold.active else None))


def _finish(project_id: str, cases: list[schemas.RecoveryCase], category: C | None, severity: Sev | None) -> list[schemas.RecoveryCase]:
    if category is not None:
        cases = [c for c in cases if c.category is category]
    if severity is not None:
        cases = [c for c in cases if c.severity is severity]
    return _sorted(cases)


def build_cases(db: Session, project: Project, provider: PaymentProvider | None, *, required: bool, limit: int = 25, offset: int = 0,
                category: C | None = None, severity: Sev | None = None,
                comparison: schemas.ObservationComparison | None = None) -> schemas.RecoveryCaseList:
    """One bounded page of payments (project-level hold cases ride on the first page). Read-only and deterministic.
    A caller that already holds the R3 comparison for this exact page may pass it to avoid computing it twice."""
    comparison = comparison or observation_compare.compare_project(db, project.id, limit=limit, offset=offset)
    hold, hold_active = hold_state(db, project.id, provider, required)
    holds = _hold_cases(db, project.id, hold) if comparison.offset == 0 else []
    cases = _finish(project.id, _verify(db, project.id, _payment_cases(db, project.id, comparison, hold_active) + holds), category, severity)
    digest = canonical_sha256({"offset": comparison.offset, "limit": comparison.limit, "total": comparison.total_payments,
                               "cases": [[c.case_id, c.basis_sha256, c.citations_verified] for c in cases]})
    return schemas.RecoveryCaseList(project_id=project.id, cases=cases, total_payments=comparison.total_payments,
                                    offset=comparison.offset, limit=comparison.limit, cases_sha256=digest)


def _candidate_payment(db: Session, project_id: str, case_id: str) -> str | None:
    """Match the id against every (payment, category) hash without computing any case: cheap, bounded, no evidence reads."""
    ids = db.scalars(select(PaymentRecord.id).where(PaymentRecord.project_id == project_id)
                     .order_by(PaymentRecord.created_at, PaymentRecord.id).limit(MAX_LOOKUP_PAYMENTS))
    categories = [c for c in C if c not in _INSTRUCTION_CATEGORIES]
    for pid in ids:
        if any(_case_id(project_id, pid, c) == case_id for c in categories):
            return pid
    return None


def find_case(db: Session, project: Project, provider: PaymentProvider | None, *, required: bool, case_id: str) -> schemas.RecoveryCase | None:
    """Look one case up by id. The id is matched against derived hashes first, then ONLY the matching payment (or the holds) is computed,
    so an unknown id costs a hash scan, not an evidence scan. Unknown and other projects' ids are indistinguishable."""
    hold_ids = {_case_id(project.id, None, cat, reason) for reason in [r.value for r in schemas.HoldReason]
                for cat in _INSTRUCTION_CATEGORIES}
    if case_id in hold_ids:
        hold, _ = hold_state(db, project.id, provider, required)
        found = _verify(db, project.id, _hold_cases(db, project.id, hold))
        return next((c for c in found if c.case_id == case_id), None)
    payment_id = _candidate_payment(db, project.id, case_id)
    if payment_id is None:
        return None
    comparison = observation_compare.compare_project(db, project.id, limit=1, offset=0, payment_id=payment_id)
    _, hold_active = hold_state(db, project.id, provider, required)
    found = _verify(db, project.id, _payment_cases(db, project.id, comparison, hold_active))
    return next((c for c in found if c.case_id == case_id), None)
