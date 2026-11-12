"""Reviewer-ready evidence report (Stage 3E).

The report is a pure function of stored records: no clock, no randomness, no network, no writes. It keeps three kinds of statement
apart so a reviewer cannot mistake one for another:
  * confirmed_observations - things the SERVER recorded (hashes, server times, verified buyer decisions);
  * seller_claims          - things the seller asserted that nothing independent confirms;
  * ai_judgments           - opinions of the analysis provider, labelled and never promoted to fact.
`report_sha256` covers everything else in the document, so a copy can be checked against a re-generated report."""
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import (
    AnalysisRun, ArtifactLink, BuyerDecisionRecord, EvidenceArtifact, Project, ScopeBaseline, ScopeChange, ScopeRecord,
)
from app.sandbox_attestation import provider_label
from app.schemas import AnalysisFinding, BuyerDecision, ChangeStatus, ContentVerification, PaymentProviderName
from app.services import analysis as analysis_service
from app.services import buyer as buyer_service
from app.services import evidence as evidence_service
from app.services import payments as payment_service
from app.services import scope as scope_service

REPORT_VERSION = "1"
VERIFIED = ContentVerification.VERIFIED_SERVER_HASH.value
DISCLAIMER = ("This report summarises recorded evidence for a human reviewer. It is not legal advice, it does not decide any "
              "dispute, and nothing in it has been checked outside the system that produced it.")
BUYER_CAVEAT = ("A buyer decision shows that whoever controlled the verified mailbox made it at that time. It is not legal "
                "identity verification, and anyone who controls that mailbox could have made it.")
PAYMENT_NOTE = ("Payment data comes from the configured provider. MOCK_PAYPAL records are simulated: no real money moved and no "
                "real PayPal system was contacted. PayPal Sandbox records are test money.")
LIMITATIONS = analysis_service.LIMITATIONS + [
    "Artifact content is only as trustworthy as the hash the server computed when it was submitted.",
    "Seller-asserted times are shown as claims; ordering uses the server's recording time.",
]


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


class _Data:
    """Everything the report needs, loaded once."""

    def __init__(self, db: Session, project: Project):
        self.project = project
        self.baselines = list(db.scalars(select(ScopeBaseline).where(ScopeBaseline.project_id == project.id)
                                         .order_by(ScopeBaseline.version)))
        self.latest = self.baselines[-1]
        self.changes = list(db.scalars(select(ScopeChange).where(ScopeChange.project_id == project.id)
                                       .order_by(ScopeChange.created_at, ScopeChange.id)))
        self.decisions = list(db.scalars(select(BuyerDecisionRecord).where(BuyerDecisionRecord.project_id == project.id)
                                         .order_by(BuyerDecisionRecord.decided_at, BuyerDecisionRecord.id)))
        self.artifacts = list(db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.project_id == project.id)
                                         .order_by(EvidenceArtifact.ingested_at, EvidenceArtifact.id)))
        superseded = {a.supersedes_artifact_id for a in self.artifacts if a.supersedes_artifact_id}
        self.latest_artifacts = [a for a in self.artifacts if a.id not in superseded]
        self.keys: dict[str, list[str]] = {}
        for artifact_id, key in db.execute(select(ArtifactLink.artifact_id, ArtifactLink.requirement_key)
                                           .where(ArtifactLink.project_id == project.id).order_by(ArtifactLink.requirement_key)):
            self.keys.setdefault(artifact_id, []).append(key)
        self.notes = list(db.scalars(select(ScopeRecord).where(ScopeRecord.project_id == project.id)
                                     .order_by(ScopeRecord.created_at, ScopeRecord.id)))
        self.payments = payment_service.list_payments(db, project)

    def decision_for(self, change: ScopeChange, decision: BuyerDecision):
        return next((d for d in self.decisions if d.change_id == change.id and d.decision == decision.value), None)

    def delivery_decisions(self) -> list[BuyerDecisionRecord]:
        return [d for d in self.decisions if d.kind == "DELIVERY_REVIEW"]


def _agreement(data: _Data) -> dict[str, Any]:
    return {"baselines": [{
        "id": b.id, "version": b.version, "kind": b.kind, "content_sha256": b.content_sha256, "change_id": b.change_id,
        "created_at": _iso(b.created_at),
        "requirements": [{"id": r.id, "key": r.key, "title": r.title, "description": r.description, "origin": r.origin}
                         for r in b.requirements]} for b in data.baselines]}


def _changes(data: _Data) -> list[dict[str, Any]]:
    rows = []
    for change in data.changes:
        approval = data.decision_for(change, BuyerDecision.APPROVE_CHANGE)
        rejection = data.decision_for(change, BuyerDecision.REJECT_CHANGE)
        decision = approval or rejection
        rows.append({
            "id": change.id, "title": change.title, "status": change.status,
            "requirement_keys": [r["key"] for r in change.proposed_requirements], "created_at": _iso(change.created_at),
            "resolved_at": _iso(change.resolved_at),
            "authorization": "VERIFIED_BUYER_APPROVAL" if change.status == ChangeStatus.APPROVED.value and approval else "NONE",
            "buyer_decision_id": decision.id if decision else None, "buyer_decision": decision.decision if decision else None})
    return rows


def _artifact_row(data: _Data, artifact: EvidenceArtifact) -> dict[str, Any]:
    return {"artifact_id": artifact.id, "name": artifact.name, "version": artifact.version, "sha256": artifact.sha256,
            "content_verification": artifact.content_verification, "link_basis": "SELLER_ASSERTED"}


def _mapping(data: _Data) -> list[dict[str, Any]]:
    return [{"requirement_id": r.id, "key": r.key, "title": r.title,
             "artifacts": [_artifact_row(data, a) for a in data.latest_artifacts if r.key in data.keys.get(a.id, [])]}
            for r in data.latest.requirements]


def _observations(data: _Data) -> list[dict[str, Any]]:
    out = [{"basis": "SERVER_RECORDED", "at": _iso(b.created_at), "refs": [f"baseline:{b.id}"],
            "text": f"Agreement version {b.version} ({b.kind}) was frozen with content hash {b.content_sha256}."}
           for b in data.baselines]
    out += [{"basis": "SERVER_RECORDED", "at": _iso(a.ingested_at), "refs": [f"artifact:{a.id}"],
             "text": f"Artifact '{a.name}' v{a.version} was registered; {a.content_verification}; sha256 {a.sha256 or 'none'}."}
            for a in data.artifacts]
    out += [{"basis": "SERVER_RECORDED", "at": _iso(c.created_at), "refs": [f"change:{c.id}"],
             "text": f"Change '{c.title}' is {c.status}."} for c in data.changes]
    out += [{"basis": "SERVER_RECORDED", "at": _iso(d.decided_at), "refs": [f"buyer_decision:{d.id}"],
             "text": f"A verified mailbox holder ({buyer_service.mask_email(d.buyer_email)}) recorded {d.decision}."}
            for d in data.decisions]
    out += [{"basis": "SERVER_RECORDED", "at": _iso(p.created_at), "refs": [f"payment:{p.id}"],
             "text": f"Payment record {p.provider} is {p.provider_status}"
                     f"{' (SIMULATED)' if p.provider == PaymentProviderName.MOCK_PAYPAL.value else ''}."}
            for p in data.payments]
    return sorted(out, key=lambda row: (row["at"] or "", row["refs"][0]))


def _claims(data: _Data) -> list[dict[str, Any]]:
    out = []
    for artifact in data.artifacts:
        for key in data.keys.get(artifact.id, []):
            out.append({"type": "ARTIFACT_REQUIREMENT_LINK", "refs": [f"artifact:{artifact.id}"],
                        "text": f"The seller states that '{artifact.name}' v{artifact.version} relates to requirement {key}."})
        if artifact.asserted_at is not None:
            out.append({"type": "ASSERTED_TIME", "refs": [f"artifact:{artifact.id}"],
                        "text": f"The seller states '{artifact.name}' v{artifact.version} dates from {_iso(artifact.asserted_at)}."})
    for note in data.notes:
        if note.kind == "APPROVAL":
            out.append({"type": "APPROVAL_NOTE", "refs": [f"scope_record:{note.id}"],
                        "text": f"The seller recorded a note labelled '{note.source_label}' claiming approval (content hash "
                                f"{note.source_sha256}). It is not buyer approval."})
    return [{**c, "basis": "SELLER_ASSERTED", "verified": False} for c in out]


def _ai(db: Session, project: Project, data: _Data) -> dict[str, Any] | None:
    row = db.scalar(select(AnalysisRun).where(AnalysisRun.project_id == project.id)
                    .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc()).limit(1))
    if row is None:
        return None
    current = scope_service.canonical_sha256(analysis_service.build_pack(db, project))
    return {
        "analysis_run_id": row.id, "provider_name": row.provider_name, "production_grade": row.production_grade,
        "label": analysis_service.label_for(row.provider_name, row.production_grade), "human_review_required": True,
        "input_sha256": row.input_sha256, "matches_current_evidence": row.input_sha256 == current,
        "baseline_version": row.baseline_version, "created_at": _iso(row.created_at),
        "findings": [AnalysisFinding.model_validate(f).model_dump(mode="json") for f in row.findings],
        "limitations": analysis_service.LIMITATIONS}


def _buyer(data: _Data) -> dict[str, Any]:
    return {"caveat": BUYER_CAVEAT, "delivery_status": buyer_service.delivery_status_of(data.decisions).value, "decisions": [{
        "id": d.id, "kind": d.kind, "decision": d.decision, "buyer_email_masked": buyer_service.mask_email(d.buyer_email),
        "statement": d.statement, "verification_method": d.verification_method, "change_id": d.change_id,
        "artifact_bindings": d.artifact_bindings or [], "change_sha256": d.change_sha256,
        "shares_seller_email_domain": bool(d.shares_seller_domain), "verified_at": _iso(d.verified_at), "decided_at": _iso(d.decided_at)}
        for d in data.decisions]}


def _payments(data: _Data) -> dict[str, Any]:
    return {"note": PAYMENT_NOTE, "records": [{
        "id": p.id, "provider": p.provider, "is_simulated": p.provider == PaymentProviderName.MOCK_PAYPAL.value,
        "provider_provenance": provider_label(p.provider, p.id),
        "currency": p.currency, "amount_minor": p.amount_minor, "provider_status": p.provider_status,
        "invoice_state": p.invoice_state, "amount_paid_minor": p.amount_paid_minor,
        "amount_refunded_minor": p.amount_refunded_minor, "scope_record_id": p.scope_record_id, "scope_sha256": p.scope_sha256,
        "created_at": _iso(p.created_at), "verification_note": p.verification_note} for p in data.payments]}


def _unresolved(data: _Data) -> list[dict[str, Any]]:
    out = []
    for requirement in data.latest.requirements:
        proof = [a for a in data.latest_artifacts if requirement.key in data.keys.get(a.id, []) and a.content_verification == VERIFIED]
        if not proof:
            out.append({"type": "REQUIREMENT_WITHOUT_VERIFIED_EVIDENCE", "refs": [f"requirement:{requirement.id}"],
                        "text": f"Requirement {requirement.key} has no linked artifact whose content the server hashed."})
    for change in data.changes:
        if change.status == ChangeStatus.PROPOSED.value:
            out.append({"type": "CHANGE_AWAITING_BUYER", "refs": [f"change:{change.id}"],
                        "text": f"Change '{change.title}' has no verified buyer decision; it is not part of the agreement."})
        elif change.status == ChangeStatus.REJECTED.value:
            out.append({"type": "CHANGE_REJECTED", "refs": [f"change:{change.id}"], "text": f"Change '{change.title}' was rejected."})
    approved = {k for c in data.changes if c.status == ChangeStatus.APPROVED.value for r in c.proposed_requirements for k in [r["key"]]}
    baseline_keys = {r.key for r in data.latest.requirements}
    for artifact in data.latest_artifacts:
        if artifact.content_verification != VERIFIED:
            out.append({"type": "ARTIFACT_NOT_HASHED_BY_SERVER", "refs": [f"artifact:{artifact.id}"],
                        "text": f"Artifact '{artifact.name}' content was not seen by the server ({artifact.content_verification})."})
        outside = [k for k in data.keys.get(artifact.id, []) if k not in baseline_keys and k not in approved]
        if outside:
            out.append({"type": "UNAPPROVED_WORK_DELIVERED", "refs": [f"artifact:{artifact.id}"],
                        "text": f"Artifact '{artifact.name}' is linked to {', '.join(outside)}, which is not in the agreement."})
    out += [{"type": "BUYER_SHARES_SELLER_DOMAIN", "refs": [f"buyer_decision:{d.id}"],
             "text": f"The {d.decision} decision came from a mailbox on the seller's own e-mail domain; treat it with caution."}
            for d in data.decisions if d.shares_seller_domain]
    out += _delivery_gaps(data)
    return out


def _delivery_gaps(data: _Data) -> list[dict[str, Any]]:
    reviews = data.delivery_decisions()
    refs = [f"artifact:{a.id}" for a in data.latest_artifacts] or [f"project:{data.project.id}"]
    if not reviews:
        return [{"type": "NO_BUYER_DELIVERY_DECISION", "refs": refs, "text": "No verified buyer decision about delivery exists."}]
    last = reviews[-1]
    if last.decision == BuyerDecision.DISPUTE_DELIVERY.value:
        return [{"type": "DELIVERY_DISPUTED", "refs": [f"buyer_decision:{last.id}"], "text": "The buyer's latest decision disputes delivery."}]
    if last.decision == BuyerDecision.ACKNOWLEDGE_DELIVERY.value:
        return [{"type": "DELIVERY_NOT_ACCEPTED", "refs": [f"buyer_decision:{last.id}"],
                 "text": "The buyer acknowledged receipt only; acceptance was not recorded."}]
    bound = {b["artifact_id"] for b in last.artifact_bindings or []}
    late = [a for a in data.latest_artifacts if a.id not in bound]
    if late:
        return [{"type": "ARTIFACT_AFTER_ACCEPTANCE", "refs": [f"artifact:{a.id}" for a in late],
                 "text": "Some artifacts were registered after, or were not part of, the buyer's acceptance."}]
    return []


def build_report(db: Session, project: Project) -> dict[str, Any]:
    if scope_service.latest_baseline(db, project.id) is None:
        raise ApiError(409, "baseline_required", "Freeze the original agreement before generating a report.")
    data = _Data(db, project)
    body = {
        "report_version": REPORT_VERSION, "disclaimer": DISCLAIMER, "project": {"id": project.id, "title": project.title},
        "agreement": _agreement(data), "changes": _changes(data), "requirement_mapping": _mapping(data),
        "timeline": [t.model_dump(mode="json") for t in evidence_service.build_timeline(db, project)],
        "confirmed_observations": _observations(data), "seller_claims": _claims(data), "ai_judgments": _ai(db, project, data),
        "buyer_evidence": _buyer(data), "payments": _payments(data), "unresolved": _unresolved(data), "limitations": LIMITATIONS}
    return {**body, "report_sha256": scope_service.canonical_sha256(body)}
