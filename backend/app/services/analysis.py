"""AI scope intelligence boundary (Stage 3D).

A provider turns an evidence pack into findings. Whatever it returns is untrusted: every finding is schema-validated, every
citation must resolve to a record of THIS project, and SUPPORTED claims must cite the kind of record that could actually support
them (an accepted-delivery claim needs a verified buyer decision, never just an artifact). Invalid output is rejected as a whole and
nothing is stored. The bundled provider is a deterministic keyword heuristic and is labelled NOT PRODUCTION."""
import logging
import re
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas
from app.errors import ApiError
from app.models import (
    AnalysisRun, ArtifactLink, BuyerDecisionRecord, EvidenceArtifact, Project, Requirement, ScopeChange, ScopeRecord,
)
from app.schemas import (
    MAX_FINDINGS, AnalysisFinding, BuyerDecision, ChangeStatus, ContentVerification, FindingCategory, FindingStatus,
)
from app.services import scope as scope_service

SUPPORT_OVERLAP = 0.6
MIN_TERM_LENGTH = 4
LIMITATIONS = [
    "Findings are opinions, not facts; each cites stored records and must be reviewed by a person.",
    "The demo provider compares keywords only. It does not understand meaning, quality or completeness.",
    "Seller-asserted links and notes are claims. Only verified buyer decisions count as buyer evidence.",
    "A buyer decision proves control of a mailbox, not legal identity.",
]
_LOG = logging.getLogger("app.analysis")
_TERM = re.compile(r"[a-z0-9]+")


class AnalysisProvider(Protocol):
    name: str
    production_grade: bool

    def analyze(self, pack: dict[str, Any]) -> list[dict[str, Any]]: ...


# --- evidence pack and reference registry ------------------------------------------------------------

def allowed_references(db: Session, project_id: str) -> dict[str, dict[str, Any]]:
    """Every citable record of the project, as 'type:id' -> the facts needed to check a citation's RELATIONSHIP to a claim."""
    refs: dict[str, dict[str, Any]] = {}
    for model, label in ((ScopeRecord, "scope_record"),):
        for record_id in db.scalars(select(model.id).where(model.project_id == project_id)):
            refs[f"{label}:{record_id}"] = {}
    for record_id, key in db.execute(select(Requirement.id, Requirement.key).where(Requirement.project_id == project_id)):
        refs[f"requirement:{record_id}"] = {"key": key}
    for record_id, status in db.execute(select(ScopeChange.id, ScopeChange.status).where(ScopeChange.project_id == project_id)):
        refs[f"change:{record_id}"] = {"status": status}
    artifacts = list(db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.project_id == project_id)))
    superseded = {a.supersedes_artifact_id for a in artifacts if a.supersedes_artifact_id}
    keys: dict[str, set[str]] = {}
    for artifact_id, key in db.execute(select(ArtifactLink.artifact_id, ArtifactLink.requirement_key)
                                       .where(ArtifactLink.project_id == project_id)):
        keys.setdefault(artifact_id, set()).add(key)
    for artifact in artifacts:
        refs[f"artifact:{artifact.id}"] = {"keys": keys.get(artifact.id, set()), "latest": artifact.id not in superseded}
    decisions = list(db.scalars(select(BuyerDecisionRecord).where(BuyerDecisionRecord.project_id == project_id)
                                .order_by(BuyerDecisionRecord.decided_at, BuyerDecisionRecord.id)))
    reviews = [d for d in decisions if d.kind == "DELIVERY_REVIEW"]
    for d in decisions:
        refs[f"buyer_decision:{d.id}"] = {"decision": d.decision, "change_id": d.change_id,
                                          "latest_delivery": bool(reviews) and reviews[-1].id == d.id}
    return refs


def build_pack(db: Session, project: Project) -> dict[str, Any]:
    baseline = scope_service.latest_baseline(db, project.id)
    keys: dict[str, list[str]] = {}
    for artifact_id, key in db.execute(select(ArtifactLink.artifact_id, ArtifactLink.requirement_key)
                                       .where(ArtifactLink.project_id == project.id).order_by(ArtifactLink.requirement_key)):
        keys.setdefault(artifact_id, []).append(key)
    rows = list(db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.project_id == project.id)
                           .order_by(EvidenceArtifact.ingested_at, EvidenceArtifact.id)))
    superseded = {row.supersedes_artifact_id for row in rows if row.supersedes_artifact_id}
    verified = ContentVerification.VERIFIED_SERVER_HASH.value
    return {
        "baseline": {"id": baseline.id, "version": baseline.version, "content_sha256": baseline.content_sha256},
        "requirements": [{"id": r.id, "key": r.key, "title": r.title, "description": r.description}
                         for r in baseline.requirements],
        "artifacts": [{
            "id": a.id, "name": a.name, "version": a.version, "sha256": a.sha256, "verification": a.content_verification,
            "is_latest": a.id not in superseded, "keys": keys.get(a.id, []),
            "content_text": a.content_text if a.content_verification == verified else None,
        } for a in rows],
        "changes": [{"id": c.id, "title": c.title, "status": c.status, "keys": [r["key"] for r in c.proposed_requirements]}
                    for c in db.scalars(select(ScopeChange).where(ScopeChange.project_id == project.id)
                                        .order_by(ScopeChange.created_at, ScopeChange.id))],
        "decisions": [{"id": d.id, "kind": d.kind, "decision": d.decision, "change_id": d.change_id,
                       "artifact_ids": [b["artifact_id"] for b in d.artifact_bindings or []]}
                      for d in db.scalars(select(BuyerDecisionRecord).where(BuyerDecisionRecord.project_id == project.id)
                                          .order_by(BuyerDecisionRecord.decided_at, BuyerDecisionRecord.id))],
        "scope_records": [{"id": s.id, "kind": s.kind}
                          for s in db.scalars(select(ScopeRecord).where(ScopeRecord.project_id == project.id)
                                              .order_by(ScopeRecord.created_at, ScopeRecord.id))],
    }


# --- the deterministic demo provider -----------------------------------------------------------------

def _terms(text: str) -> set[str]:
    return {t for t in _TERM.findall(text.lower()) if len(t) >= MIN_TERM_LENGTH}


def _overlap(requirement_text: str, content: str) -> float:
    wanted = _terms(requirement_text)
    return len(wanted & _terms(content)) / len(wanted) if wanted else 0.0


class _Builder:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, category: str, claim: str, status: str, refs: list[str], explanation: str) -> None:
        self.items.append({
            "id": f"F{len(self.items) + 1:03d}", "category": category, "claim": claim, "status": status, "confidence": "LOW",
            "basis": "RULE_BASED_DEMO", "source_refs": refs, "explanation": explanation, "human_review_required": True})


class DemoAnalysisProvider:
    """Rule-based and fully offline. NOT a language model; it exists to exercise the boundary deterministically."""

    name = "DEMO_RULES_V1"
    production_grade = False

    def analyze(self, pack: dict[str, Any]) -> list[dict[str, Any]]:
        out = _Builder()
        latest = [a for a in pack["artifacts"] if a["is_latest"]]
        for requirement in pack["requirements"]:
            self._coverage(out, requirement, [a for a in latest if requirement["key"] in a["keys"]])
        self._changes(out, pack, latest)
        for artifact in latest:
            if not artifact["keys"]:
                out.add("MISSING_EVIDENCE", f"Artifact {artifact['name']} is linked to a requirement", "UNSUPPORTED",
                        [f"artifact:{artifact['id']}"], "The seller linked this artifact to no requirement, so it supports nothing yet.")
        for record in pack["scope_records"]:
            if record["kind"] == "APPROVAL":
                out.add("CONFLICTING_EVIDENCE", "A seller-recorded note claims buyer approval", "AMBIGUOUS",
                        [f"scope_record:{record['id']}"],
                        "This note is a seller-side claim. It is not treated as buyer approval; only a verified buyer decision is.")
        for decision in pack["decisions"]:
            if decision["decision"] == BuyerDecision.DISPUTE_DELIVERY.value:
                out.add("CONFLICTING_EVIDENCE", "The buyer raised a dispute about the delivery", "AMBIGUOUS",
                        [f"buyer_decision:{decision['id']}"], "A verified buyer decision disputes the delivery; the merits are not judged here.")
        self._acceptance(out, pack, latest)
        return out.items

    @staticmethod
    def _coverage(out: _Builder, requirement: dict, linked: list[dict]) -> None:
        claim = f"Requirement {requirement['key']} ({requirement['title']}) is covered by delivered evidence"
        refs = [f"requirement:{requirement['id']}"]
        if not linked:
            out.add("REQUIREMENT_COVERAGE", claim, "UNSUPPORTED", refs, "No latest-version artifact is linked to this requirement.")
            return
        text = f"{requirement['title']} {requirement['description']}"
        best, reasons = None, []
        for artifact in linked:
            refs.append(f"artifact:{artifact['id']}")
            if artifact["content_text"] is None:
                reasons.append(f"{artifact['name']}: " + ("content was never seen by the server (declared hash only)"
                                                          if artifact["sha256"] else "no content or hash available"))
                continue
            share = _overlap(text, artifact["content_text"])
            if share >= SUPPORT_OVERLAP:
                best = (artifact, share)
                break
            reasons.append(f"{artifact['name']}: low keyword overlap ({share:.0%})")
        if best:
            out.add("REQUIREMENT_COVERAGE", claim, "SUPPORTED", [refs[0], f"artifact:{best[0]['id']}"],
                    f"Heuristic keyword overlap of {best[1]:.0%} with {best[0]['name']} (not semantic review).")
            return
        out.add("REQUIREMENT_COVERAGE", claim, "AMBIGUOUS", refs, "Not shown to be covered. " + "; ".join(reasons) + ".")

    @staticmethod
    def _changes(out: _Builder, pack: dict, latest: list[dict]) -> None:
        for change in pack["changes"]:
            mine = [d for d in pack["decisions"] if d["change_id"] == change["id"]]
            approvals = [d for d in mine if d["decision"] == "APPROVE_CHANGE"]
            rejections = [d for d in mine if d["decision"] == "REJECT_CHANGE"]
            change_ref = f"change:{change['id']}"
            authorised = change["status"] == ChangeStatus.APPROVED.value and bool(approvals)
            claim = f"Change '{change['title']}' was authorized by the buyer"
            if authorised:
                out.add("CHANGE_AUTHORIZATION", claim, "SUPPORTED", [change_ref, f"buyer_decision:{approvals[0]['id']}"],
                        "A verified buyer decision approved this change.")
                continue
            reject_refs = [f"buyer_decision:{d['id']}" for d in rejections]
            out.add("CHANGE_AUTHORIZATION", claim, "UNSUPPORTED", [change_ref] + reject_refs,
                    f"The change is {change['status'].lower()} and no verified buyer approval exists for it.")
            for key in change["keys"]:
                delivered = [f"artifact:{a['id']}" for a in latest if key in a["keys"]]
                if delivered:
                    out.add("SCOPE_MISMATCH", f"Delivered work for {key} is outside the agreement", "UNSUPPORTED",
                            delivered + [change_ref] + reject_refs,
                            f"Linked artifacts deliver {key}, which is not part of the agreement: its change is "
                            f"{change['status'].lower()}.")

    @staticmethod
    def _acceptance(out: _Builder, pack: dict, latest: list[dict]) -> None:
        claim = "The buyer accepted the delivered work"
        latest_refs = [f"artifact:{a['id']}" for a in latest]
        reviews = [d for d in pack["decisions"] if d["kind"] == "DELIVERY_REVIEW"]
        if not reviews:
            out.add("DELIVERY_ACCEPTANCE", claim, "UNSUPPORTED", latest_refs, "No verified buyer decision about delivery exists.")
            return
        last = reviews[-1]
        ref = f"buyer_decision:{last['id']}"
        if last["decision"] == "DISPUTE_DELIVERY":
            out.add("DELIVERY_ACCEPTANCE", claim, "UNSUPPORTED", [ref], "The buyer's most recent decision disputes the delivery.")
        elif last["decision"] == "ACKNOWLEDGE_DELIVERY":
            out.add("DELIVERY_ACCEPTANCE", claim, "AMBIGUOUS", [ref], "The buyer acknowledged receipt only; that is not acceptance.")
        elif {a["id"] for a in latest} - set(last["artifact_ids"]):
            out.add("DELIVERY_ACCEPTANCE", claim, "AMBIGUOUS", [ref] + latest_refs,
                    "Some current artifacts are not covered by the buyer's acceptance, which predates them.")
        else:
            out.add("DELIVERY_ACCEPTANCE", claim, "SUPPORTED", [ref] + latest_refs,
                    "A verified buyer decision accepted every current artifact.")


# --- validation: the provider's output is untrusted --------------------------------------------------

def _cited(finding: AnalysisFinding, registry: dict[str, dict[str, Any]], kind: str) -> list[tuple[str, dict[str, Any]]]:
    return [(ref.split(":", 1)[1], registry[ref]) for ref in finding.source_refs if ref.startswith(f"{kind}:")]


def _coverage_problem(finding: AnalysisFinding, registry: dict[str, dict[str, Any]]) -> str | None:
    wanted = {meta["key"] for _, meta in _cited(finding, registry, "requirement")}
    if not any(meta["latest"] and meta["keys"] & wanted for _, meta in _cited(finding, registry, "artifact")):
        return "a SUPPORTED coverage finding must cite a requirement and a current artifact linked to that requirement"
    return None


def _acceptance_problem(finding: AnalysisFinding, registry: dict[str, dict[str, Any]]) -> str | None:
    if not any(m["decision"] == BuyerDecision.ACCEPT_DELIVERY.value and m["latest_delivery"]
               for _, m in _cited(finding, registry, "buyer_decision")):
        return "a SUPPORTED acceptance finding must cite the buyer's latest delivery decision, and it must accept delivery"
    return None


def _authorization_problem(finding: AnalysisFinding, registry: dict[str, dict[str, Any]]) -> str | None:
    approved = {cid for cid, meta in _cited(finding, registry, "change") if meta["status"] == ChangeStatus.APPROVED.value}
    if not any(m["decision"] == BuyerDecision.APPROVE_CHANGE.value and m["change_id"] in approved
               for _, m in _cited(finding, registry, "buyer_decision")):
        return "a SUPPORTED authorization finding must cite an approved change and the buyer decision that approved that change"
    return None


_SUPPORT_CHECKS = {
    FindingCategory.REQUIREMENT_COVERAGE: _coverage_problem,
    FindingCategory.DELIVERY_ACCEPTANCE: _acceptance_problem,
    FindingCategory.CHANGE_AUTHORIZATION: _authorization_problem,
}


def _semantic_problem(finding: AnalysisFinding, registry: dict[str, dict[str, Any]]) -> str | None:
    if finding.status is not FindingStatus.SUPPORTED:
        return None
    check = _SUPPORT_CHECKS.get(finding.category)
    if check is None:
        return f"{finding.category.value} findings can not be SUPPORTED; only coverage, authorization and acceptance have a verifiable basis"
    return check(finding, registry)


def _invalid(problems: list[dict]) -> ApiError:
    return ApiError(502, "analysis_invalid", "The analysis provider returned invalid output; nothing was stored.", details=problems)


def validate_findings(raw: Any, registry: dict[str, dict[str, Any]]) -> list[AnalysisFinding]:
    if not isinstance(raw, list) or len(raw) > MAX_FINDINGS:
        raise _invalid([{"finding": None, "problem": f"output must be a list of at most {MAX_FINDINGS} findings"}])
    problems, findings, seen = [], [], set()
    for index, item in enumerate(raw):
        label = item["id"] if isinstance(item, dict) and isinstance(item.get("id"), str) else f"#{index}"
        try:
            finding = AnalysisFinding.model_validate(item)
        except ValidationError as exc:
            detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors())
            problems.append({"finding": label, "problem": detail})
            continue
        unknown = [ref for ref in finding.source_refs if ref not in registry]
        if unknown:
            problem = f"{len(unknown)} citation(s) do not resolve to a record of this project"
        elif finding.id in seen:
            problem = "duplicate finding id"
        else:
            problem = _semantic_problem(finding, registry)
        seen.add(finding.id)
        if problem:
            problems.append({"finding": label, "problem": problem})
        findings.append(finding)
    if problems:
        raise _invalid(problems)
    return findings


# --- orchestration -----------------------------------------------------------------------------------

def label_for(provider_name: str, production_grade: bool) -> str:
    if production_grade:
        return f"AI-assisted analysis ({provider_name}). Every finding requires human review."
    return (f"DEMONSTRATION ANALYSIS ({provider_name}) - NOT PRODUCTION. Heuristic output; "
            "every finding requires human review.")


def run_view(row: AnalysisRun) -> schemas.AnalysisRunView:
    return schemas.AnalysisRunView(
        id=row.id, project_id=row.project_id, baseline_id=row.baseline_id, baseline_version=row.baseline_version,
        provider_name=row.provider_name, production_grade=row.production_grade,
        label=label_for(row.provider_name, row.production_grade), input_sha256=row.input_sha256,
        limitations=LIMITATIONS, findings=[AnalysisFinding.model_validate(f) for f in row.findings], created_at=row.created_at)


def list_runs(db: Session, project: Project) -> list[schemas.AnalysisRunView]:
    rows = db.scalars(select(AnalysisRun).where(AnalysisRun.project_id == project.id)
                      .order_by(AnalysisRun.created_at, AnalysisRun.id))
    return [run_view(row) for row in rows]


def run_analysis(db: Session, provider: AnalysisProvider | None, project: Project) -> schemas.AnalysisRunView:
    if provider is None:
        raise ApiError(503, "ai_disabled", "Scope analysis is disabled.")
    if scope_service.latest_baseline(db, project.id) is None:
        raise ApiError(409, "baseline_required", "Freeze the original agreement before running an analysis.")
    pack = build_pack(db, project)
    registry = allowed_references(db, project.id)
    try:
        raw = provider.analyze(pack)
    except Exception as exc:  # a provider failure must fail closed and never leak its message
        _LOG.error("analysis_provider_failed provider=%s error_type=%s", getattr(provider, "name", "?"), type(exc).__name__)
        raise ApiError(502, "analysis_failed", "The analysis provider failed; nothing was stored.") from None
    findings = validate_findings(raw, registry)
    row = AnalysisRun(
        project_id=project.id, baseline_id=pack["baseline"]["id"], baseline_version=pack["baseline"]["version"],
        provider_name=str(provider.name)[:48], production_grade=bool(provider.production_grade),
        input_sha256=scope_service.canonical_sha256(pack), findings=[f.model_dump(mode="json") for f in findings])
    db.add(row)
    db.commit()
    return run_view(row)
