"""Delivery evidence artifacts, requirement links and the chronological timeline (Stage 3B).

A SHA-256 shows that these exact bytes were submitted; it does not show who made them, when they were made, or that the buyer
received them. `content_verification` therefore says exactly what the server did: hashed the content itself, took the seller's
word for a hash, or has nothing."""
import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas
from app.errors import ApiError
from app.models import (
    ArtifactLink, AuditEvent, EvidenceArtifact, PaymentEvent, Project, Requirement, ScopeChange, ScopeRecord,
)
from app.schemas import AuditActor, ContentVerification
from app.services.scope import record_audit


def known_requirement_keys(db: Session, project_id: str) -> set[str]:
    """Keys of every baseline requirement plus every key proposed in any change (so unapproved work can be linked and flagged)."""
    keys = set(db.scalars(select(Requirement.key).where(Requirement.project_id == project_id)))
    for proposed in db.scalars(select(ScopeChange.proposed_requirements).where(ScopeChange.project_id == project_id)):
        keys.update(item["key"] for item in proposed)
    return keys


def _owned_artifact(db: Session, project: Project, artifact_id: str) -> EvidenceArtifact:
    artifact = db.scalar(select(EvidenceArtifact).where(EvidenceArtifact.id == artifact_id,
                                                        EvidenceArtifact.project_id == project.id))
    if artifact is None:
        raise ApiError(404, "not_found", "Artifact not found.")
    return artifact


def artifact_keys(db: Session, artifact_id: str) -> list[str]:
    return sorted(db.scalars(select(ArtifactLink.requirement_key).where(ArtifactLink.artifact_id == artifact_id)))


def artifact_view(db: Session, artifact: EvidenceArtifact) -> schemas.ArtifactView:
    superseded = db.scalar(select(EvidenceArtifact.id).where(EvidenceArtifact.supersedes_artifact_id == artifact.id)) is not None
    return schemas.ArtifactView(
        id=artifact.id, project_id=artifact.project_id, name=artifact.name, version=artifact.version,
        media_type=artifact.media_type, description=artifact.description, sha256=artifact.sha256,
        content_verification=ContentVerification(artifact.content_verification), size_bytes=artifact.size_bytes,
        asserted_at=artifact.asserted_at, ingested_at=artifact.ingested_at,
        supersedes_artifact_id=artifact.supersedes_artifact_id, is_latest=not superseded,
        requirement_keys=artifact_keys(db, artifact.id))


def list_artifacts(db: Session, project: Project) -> list[schemas.ArtifactView]:
    rows = db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.project_id == project.id)
                      .order_by(EvidenceArtifact.ingested_at, EvidenceArtifact.id))
    return [artifact_view(db, row) for row in rows]


def _hash_and_label(body: schemas.ArtifactCreate) -> tuple[str | None, ContentVerification, int | None]:
    if body.content_text is not None:
        raw = body.content_text.encode("utf8")
        digest = hashlib.sha256(raw).hexdigest()
        if body.declared_sha256 is not None and body.declared_sha256 != digest:
            raise ApiError(422, "hash_mismatch", "The declared hash does not match the submitted content.")
        return digest, ContentVerification.VERIFIED_SERVER_HASH, len(raw)
    if body.declared_sha256 is not None:
        return body.declared_sha256, ContentVerification.DECLARED_ONLY, None
    return None, ContentVerification.UNAVAILABLE, None


def _check_keys(db: Session, project_id: str, keys: list[str]) -> None:
    if set(keys) - known_requirement_keys(db, project_id):
        raise ApiError(422, "unknown_requirement", "A requirement key does not exist in this project.")


def register_artifact(db: Session, project: Project, body: schemas.ArtifactCreate) -> schemas.ArtifactView:
    previous = None
    if body.supersedes_artifact_id is not None:
        previous = _owned_artifact(db, project, body.supersedes_artifact_id)
        if previous.name != body.name:
            raise ApiError(422, "artifact_name_mismatch", "A new version must keep the artifact name.")
        if db.scalar(select(EvidenceArtifact.id).where(EvidenceArtifact.supersedes_artifact_id == previous.id)) is not None:
            raise ApiError(409, "artifact_not_latest", "Only the latest version can be replaced.")
    digest, label, size = _hash_and_label(body)
    if digest is not None:
        duplicate = db.scalar(select(EvidenceArtifact.id).where(EvidenceArtifact.project_id == project.id,
                                                                EvidenceArtifact.sha256 == digest))
        if duplicate is not None:
            raise ApiError(409, "duplicate_artifact", "This content is already registered.",
                           details=[{"existing_artifact_id": duplicate}])
    _check_keys(db, project.id, body.requirement_keys)
    artifact = EvidenceArtifact(
        project_id=project.id, name=body.name.strip(), version=previous.version + 1 if previous else 1,
        media_type=body.media_type, description=body.description.strip(), sha256=digest, content_verification=label.value,
        size_bytes=size, content_text=body.content_text, asserted_at=body.asserted_at,
        supersedes_artifact_id=previous.id if previous else None)
    try:
        db.add(artifact)
        db.flush()
        for key in body.requirement_keys:
            db.add(ArtifactLink(artifact_id=artifact.id, project_id=project.id, requirement_key=key))
        record_audit(db, project.id, "ARTIFACT_REGISTERED", AuditActor.SELLER, "artifact", artifact.id, {
            "name": artifact.name, "version": artifact.version, "sha256": digest, "content_verification": label.value,
            "asserted_at": body.asserted_at.isoformat() if body.asserted_at else None,
            "supersedes_artifact_id": artifact.supersedes_artifact_id, "requirement_keys": body.requirement_keys})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "artifact_conflict", "The artifact could not be registered; it may already exist.") from None
    return artifact_view(db, artifact)


def link_artifact(db: Session, project: Project, artifact_id: str, keys: list[str]) -> schemas.ArtifactView:
    artifact = _owned_artifact(db, project, artifact_id)
    _check_keys(db, project.id, keys)
    existing = set(artifact_keys(db, artifact.id))
    added = [key for key in keys if key not in existing]
    for key in added:
        db.add(ArtifactLink(artifact_id=artifact.id, project_id=project.id, requirement_key=key))
    if added:
        record_audit(db, project.id, "ARTIFACT_LINKED", AuditActor.SELLER, "artifact", artifact.id, {"requirement_keys": added})
    db.commit()
    return artifact_view(db, artifact)


def _audit_summary(event: AuditEvent) -> str:
    d = event.detail or {}
    templates = {
        "BASELINE_FROZEN": lambda: f"Original agreement frozen as baseline v{d.get('version')}",
        "CHANGE_PROPOSED": lambda: f"Scope change proposed ({', '.join(d.get('requirement_keys', []))}); not part of the agreement",
        "CHANGE_SUPERSEDED": lambda: "Scope change superseded",
        "CHANGE_APPROVED": lambda: f"Scope change approved by verified buyer; baseline v{d.get('version')} created",
        "CHANGE_REJECTED": lambda: "Scope change rejected by verified buyer",
        "ARTIFACT_REGISTERED": lambda: f"Artifact '{d.get('name')}' v{d.get('version')} registered ({d.get('content_verification')})",
        "ARTIFACT_LINKED": lambda: f"Artifact linked by the seller to {', '.join(d.get('requirement_keys', []))}",
        "BUYER_REQUEST_CREATED": lambda: f"Seller asked {d.get('buyer_email_masked')} to review ({d.get('kind')})",
        "BUYER_REQUEST_CANCELLED": lambda: "Seller cancelled a pending buyer request",
        "BUYER_REQUEST_OPENED": lambda: "Holder of the buyer mailbox opened the request after e-mail verification (access, not approval)",
        "BUYER_DECISION_RECORDED": lambda: f"Verified mailbox holder recorded decision {d.get('decision')}",
        "DELIVERY_ACKNOWLEDGED": lambda: "Verified mailbox holder acknowledged receipt of the listed artifacts (not acceptance)",
        "DELIVERY_ACCEPTED": lambda: "Verified mailbox holder stated acceptance of the listed artifacts",
        "DELIVERY_DISPUTED": lambda: "Verified mailbox holder disputed the delivery",
    }
    make = templates.get(event.event_type)
    return make() if make else event.event_type.replace("_", " ").capitalize()


def build_timeline(db: Session, project: Project) -> list[schemas.TimelineItem]:
    """Every recorded item, ordered by the SERVER's recording time. Seller-asserted times are shown alongside, never used to order."""
    items: list[tuple] = []
    for event in db.scalars(select(AuditEvent).where(AuditEvent.project_id == project.id)):
        asserted = (event.detail or {}).get("asserted_at")
        items.append((event.created_at, 1, event.id, schemas.TimelineItem(
            at=event.created_at, event_type=event.event_type, actor_role=AuditActor(event.actor_role),
            subject_type=event.subject_type, subject_id=event.subject_id, summary=_audit_summary(event),
            asserted_at=asserted)))
    for record in db.scalars(select(ScopeRecord).where(ScopeRecord.project_id == project.id)):
        items.append((record.created_at, 0, record.id, schemas.TimelineItem(
            at=record.created_at, event_type="SCOPE_RECORD_ADDED", actor_role=AuditActor.SELLER, subject_type="scope_record",
            subject_id=record.id, summary=f"{record.kind} record '{record.source_label}' added by the seller "
                                            f"(sha256 {record.source_sha256[:12]})")))
    for pe in db.scalars(select(PaymentEvent).where(PaymentEvent.project_id == project.id)):
        items.append((pe.created_at, 2, pe.id, schemas.TimelineItem(
            at=pe.created_at, event_type=f"PAYMENT_{pe.event_type}", actor_role=AuditActor(pe.actor), subject_type="payment",
            subject_id=pe.payment_id, summary=f"Payment record event {pe.event_type}"
                                                f"{' -> ' + pe.to_status if pe.to_status else ''}")))
    items.sort(key=lambda row: row[:3])
    return [row[3] for row in items]
