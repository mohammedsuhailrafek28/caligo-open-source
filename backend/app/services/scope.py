"""Scope baselines, requirements and change proposals (Stage 3A).

Invariants: the original agreement is frozen once and never rewritten; a proposed change never alters any baseline; the only way a
change becomes part of the agreement is `apply_change_decision`, which the buyer-verification service calls with a recorded
decision. No seller-facing route reaches it."""
import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas
from app.errors import ApiError
from app.models import (
    AuditEvent, Project, Requirement, ScopeBaseline, ScopeChange, ScopeRecord, new_id, utcnow,
)
from app.schemas import AuditActor, BaselineKind, ChangeStatus, ScopeKind


def canonical_sha256(value: object) -> str:
    """Hash of a canonical JSON rendering: key order and whitespace cannot change it."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf8")).hexdigest()


def change_content_sha256(change) -> str:
    """Hash of exactly what a buyer is shown when asked to decide on a change (the row itself is mutable)."""
    return canonical_sha256({"title": change.title, "description": change.description,
                             "requirements": change.proposed_requirements})


def record_audit(db: Session, project_id: str, event_type: str, actor: AuditActor, subject_type: str, subject_id: str,
                 detail: dict | None = None) -> AuditEvent:
    event = AuditEvent(project_id=project_id, event_type=event_type, actor_role=actor.value, subject_type=subject_type,
                       subject_id=subject_id, detail=detail or {})
    db.add(event)
    return event


def latest_baseline(db: Session, project_id: str) -> ScopeBaseline | None:
    return db.scalar(select(ScopeBaseline).where(ScopeBaseline.project_id == project_id)
                     .order_by(ScopeBaseline.version.desc()).limit(1))


def list_baselines(db: Session, project: Project) -> list[ScopeBaseline]:
    return list(db.scalars(select(ScopeBaseline).where(ScopeBaseline.project_id == project.id).order_by(ScopeBaseline.version)))


def list_changes(db: Session, project: Project) -> list[ScopeChange]:
    return list(db.scalars(select(ScopeChange).where(ScopeChange.project_id == project.id)
                           .order_by(ScopeChange.created_at, ScopeChange.id)))


def list_audit_events(db: Session, project: Project) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.project_id == project.id)
                           .order_by(AuditEvent.created_at, AuditEvent.id)))


def _insert_baseline(db: Session, project_id: str, version: int, kind: BaselineKind, rows: list[dict],
                     source_record_id: str | None, previous: ScopeBaseline | None, change_id: str | None) -> ScopeBaseline:
    rows = sorted(rows, key=lambda r: r["key"])
    baseline = ScopeBaseline(
        id=new_id(), project_id=project_id, version=version, kind=kind.value, source_scope_record_id=source_record_id,
        previous_baseline_id=previous.id if previous else None, change_id=change_id,
        content_sha256=canonical_sha256([{k: r[k] for k in ("key", "title", "description", "origin")} for r in rows]))
    db.add(baseline)
    db.flush()
    for row in rows:
        db.add(Requirement(baseline_id=baseline.id, project_id=project_id, **row))
    db.flush()
    return baseline


def freeze_original(db: Session, project: Project, body: schemas.BaselineCreate) -> ScopeBaseline:
    record = db.scalar(select(ScopeRecord).where(ScopeRecord.id == body.source_scope_record_id,
                                                 ScopeRecord.project_id == project.id))
    if record is None or record.kind != ScopeKind.ORIGINAL.value:
        raise ApiError(409, "original_scope_required", "An ORIGINAL scope record of this project is required.")
    if latest_baseline(db, project.id) is not None:
        raise ApiError(409, "baseline_exists", "The original agreement is already frozen; propose a change instead.")
    rows = [{"key": r.key, "title": r.title.strip(), "description": r.description.strip(), "origin": "ORIGINAL"}
            for r in body.requirements]
    try:
        baseline = _insert_baseline(db, project.id, 1, BaselineKind.ORIGINAL, rows, record.id, None, None)
        record_audit(db, project.id, "BASELINE_FROZEN", AuditActor.SELLER, "baseline", baseline.id,
                     {"version": 1, "source_scope_record_id": record.id, "content_sha256": baseline.content_sha256})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "baseline_exists", "The original agreement is already frozen; propose a change instead.") from None
    return baseline


def propose_change(db: Session, project: Project, body: schemas.ScopeChangeCreate) -> ScopeChange:
    base = latest_baseline(db, project.id)
    if base is None:
        raise ApiError(409, "baseline_required", "Freeze the original agreement before proposing changes.")
    existing = {r.key for r in base.requirements}
    if existing & {r.key for r in body.requirements}:
        raise ApiError(409, "requirement_key_conflict", "A proposed requirement key already exists in the agreement.")
    previous = None
    if body.supersedes_change_id is not None:
        previous = db.scalar(select(ScopeChange).where(ScopeChange.id == body.supersedes_change_id,
                                                       ScopeChange.project_id == project.id))
        if previous is None:
            raise ApiError(404, "not_found", "Change not found.")
        if previous.status != ChangeStatus.PROPOSED.value:
            raise ApiError(409, "change_not_pending", "Only a pending change can be superseded.")
    change = ScopeChange(
        project_id=project.id, title=body.title.strip(), description=body.description.strip(),
        proposed_requirements=[{"key": r.key, "title": r.title.strip(), "description": r.description.strip()}
                               for r in body.requirements],
        status=ChangeStatus.PROPOSED.value, base_baseline_id=base.id,
        supersedes_change_id=previous.id if previous else None)
    db.add(change)
    db.flush()
    if previous is not None:
        previous.status = ChangeStatus.SUPERSEDED.value
        previous.resolved_at = utcnow()
        record_audit(db, project.id, "CHANGE_SUPERSEDED", AuditActor.SELLER, "change", previous.id,
                     {"superseded_by": change.id})
    record_audit(db, project.id, "CHANGE_PROPOSED", AuditActor.SELLER, "change", change.id,
                 {"base_baseline_id": base.id, "requirement_keys": [r["key"] for r in change.proposed_requirements]})
    db.commit()
    return change


@dataclass(frozen=True)
class ChangeOutcome:
    applied: bool
    baseline: ScopeBaseline | None
    reason: str | None = None


def apply_change_decision(db: Session, change: ScopeChange, *, approve: bool, decision_ref: str) -> ChangeOutcome:
    """Applies a VERIFIED buyer decision (`decision_ref` identifies the recorded decision). Does not commit.

    Approving appends a new AMENDED baseline (previous baselines are untouched). If another approval meanwhile took one of the keys, the
    change is marked SUPERSEDED instead of rewriting anything."""
    if change.status != ChangeStatus.PROPOSED.value:
        raise ApiError(409, "change_not_pending", "This change has already been decided or superseded.")
    now = utcnow()
    if not approve:
        change.status, change.resolved_at = ChangeStatus.REJECTED.value, now
        record_audit(db, change.project_id, "CHANGE_REJECTED", AuditActor.BUYER_EMAIL_VERIFIED, "change", change.id,
                     {"decision_ref": decision_ref})
        return ChangeOutcome(False, None, "REJECTED")
    latest = latest_baseline(db, change.project_id)
    existing = {r.key: r for r in latest.requirements}
    if existing.keys() & {r["key"] for r in change.proposed_requirements}:
        change.status, change.resolved_at = ChangeStatus.SUPERSEDED.value, now
        record_audit(db, change.project_id, "CHANGE_SUPERSEDED", AuditActor.SYSTEM, "change", change.id,
                     {"reason": "REQUIREMENT_KEY_CONFLICT", "decision_ref": decision_ref})
        return ChangeOutcome(False, None, "REQUIREMENT_KEY_CONFLICT")
    rows = [{"key": r.key, "title": r.title, "description": r.description, "origin": r.origin} for r in existing.values()]
    rows += [{"key": r["key"], "title": r["title"], "description": r.get("description", ""), "origin": f"CHANGE:{change.id}"}
             for r in change.proposed_requirements]
    baseline = _insert_baseline(db, change.project_id, latest.version + 1, BaselineKind.AMENDED, rows, None, latest, change.id)
    change.status, change.resolved_at, change.resolved_baseline_id = ChangeStatus.APPROVED.value, now, baseline.id
    record_audit(db, change.project_id, "CHANGE_APPROVED", AuditActor.BUYER_EMAIL_VERIFIED, "change", change.id,
                 {"decision_ref": decision_ref, "new_baseline_id": baseline.id, "version": baseline.version})
    return ChangeOutcome(True, baseline)
