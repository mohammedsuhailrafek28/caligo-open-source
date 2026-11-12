"""Buyer acknowledgment and authorization (Stage 3C).

Trust model, stated plainly: the link token and the one-time code are delivered only to the buyer's mailbox through the Mailer
port; no API response to the seller contains them, and the seller has no route that records a buyer decision. A decision is therefore
evidence that *whoever controls that mailbox at that moment* made it. It is not proof of legal identity, and it can be forged by anyone
who controls the buyer's mailbox (including a seller who does)."""
import logging
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app import schemas
from app.errors import ApiError
from app.mailer import Mailer
from app.models import (
    BuyerDecisionRecord, BuyerRequest, EvidenceArtifact, Project, ScopeChange, utcnow,
)
from app.schemas import AuditActor, BuyerDecision, BuyerRequestKind, BuyerRequestStatus, ChangeStatus
from app.security import hash_token, new_token, normalize_email, tokens_match
from app.services import scope as scope_service

REQUEST_TTL = timedelta(days=7)
CHALLENGE_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(minutes=30)
MAX_VERIFY_FAILURES = 5
MAX_CHALLENGE_SENDS = 5

ALLOWED = {
    BuyerRequestKind.SCOPE_CHANGE_APPROVAL: [BuyerDecision.APPROVE_CHANGE, BuyerDecision.REJECT_CHANGE],
    BuyerRequestKind.DELIVERY_REVIEW: [BuyerDecision.ACKNOWLEDGE_DELIVERY, BuyerDecision.ACCEPT_DELIVERY, BuyerDecision.DISPUTE_DELIVERY],
}
DELIVERY_EVENTS = {
    BuyerDecision.ACKNOWLEDGE_DELIVERY: "DELIVERY_ACKNOWLEDGED",
    BuyerDecision.ACCEPT_DELIVERY: "DELIVERY_ACCEPTED",
    BuyerDecision.DISPUTE_DELIVERY: "DELIVERY_DISPUTED",
}
_LOG = logging.getLogger("app.buyer")
_GENERIC_FAILURE = ("challenge_failed", "The code or link is not valid.")


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _domain(address: str) -> str:
    return address.rpartition("@")[2].strip().lower()


def _code_hash(request_id: str, code: str) -> str:
    return hash_token(f"{request_id}:{code}")


def request_view(row: BuyerRequest) -> schemas.BuyerRequestView:
    return schemas.BuyerRequestView(
        id=row.id, project_id=row.project_id, kind=BuyerRequestKind(row.kind), change_id=row.change_id,
        artifact_ids=list(row.artifact_ids or []), buyer_email_masked=mask_email(row.buyer_email),
        status=BuyerRequestStatus(row.status), expires_at=row.expires_at, created_at=row.created_at,
        link_opened=row.verified_at is not None, decided_at=row.decided_at)


def decision_view(row: BuyerDecisionRecord) -> schemas.BuyerDecisionView:
    return schemas.BuyerDecisionView(
        id=row.id, request_id=row.request_id, project_id=row.project_id, kind=BuyerRequestKind(row.kind),
        decision=BuyerDecision(row.decision), buyer_email_masked=mask_email(row.buyer_email), statement=row.statement,
        verification_method="EMAIL_OTP", change_id=row.change_id, artifact_bindings=list(row.artifact_bindings or []),
        verified_at=row.verified_at, decided_at=row.decided_at)


_DELIVERY_STATUS = {
    BuyerDecision.ACKNOWLEDGE_DELIVERY.value: schemas.DeliveryStatus.ACKNOWLEDGED_ONLY,
    BuyerDecision.ACCEPT_DELIVERY.value: schemas.DeliveryStatus.ACCEPTED,
    BuyerDecision.DISPUTE_DELIVERY.value: schemas.DeliveryStatus.DISPUTED,
}


def delivery_status_of(decisions: list[BuyerDecisionRecord]) -> schemas.DeliveryStatus:
    """The single definition of delivery state: the buyer's most recent verified DELIVERY decision, or NONE. `decisions` must be
    ordered oldest first. Scope-change decisions and every seller-side record are ignored."""
    reviews = [d for d in decisions if d.kind == BuyerRequestKind.DELIVERY_REVIEW.value]
    return _DELIVERY_STATUS[reviews[-1].decision] if reviews else schemas.DeliveryStatus.NONE


def delivery_status(db: Session, project: Project) -> schemas.DeliveryStatus:
    rows = db.scalars(select(BuyerDecisionRecord).where(BuyerDecisionRecord.project_id == project.id)
                      .order_by(BuyerDecisionRecord.decided_at, BuyerDecisionRecord.id))
    return delivery_status_of(list(rows))


def list_requests(db: Session, project: Project) -> list[schemas.BuyerRequestView]:
    rows = db.scalars(select(BuyerRequest).where(BuyerRequest.project_id == project.id)
                      .order_by(BuyerRequest.created_at, BuyerRequest.id))
    return [request_view(r) for r in rows]


def list_decisions(db: Session, project: Project) -> list[schemas.BuyerDecisionView]:
    rows = db.scalars(select(BuyerDecisionRecord).where(BuyerDecisionRecord.project_id == project.id)
                      .order_by(BuyerDecisionRecord.decided_at, BuyerDecisionRecord.id))
    return [decision_view(r) for r in rows]


def create_request(db: Session, mailer: Mailer | None, portal_base_url: str, project: Project,
                   body: schemas.BuyerRequestCreate) -> schemas.BuyerRequestView:
    if mailer is None:
        raise ApiError(503, "buyer_mail_disabled", "Buyer e-mail delivery is disabled.")
    artifact_ids: list[str] = sorted(set(body.artifact_ids))
    if body.kind is BuyerRequestKind.SCOPE_CHANGE_APPROVAL:
        change = db.scalar(select(ScopeChange).where(ScopeChange.id == body.change_id, ScopeChange.project_id == project.id))
        if change is None:
            raise ApiError(404, "not_found", "Change not found.")
        if change.status != ChangeStatus.PROPOSED.value:
            raise ApiError(409, "change_not_pending", "This change has already been decided or superseded.")
    else:
        found = set(db.scalars(select(EvidenceArtifact.id).where(EvidenceArtifact.project_id == project.id,
                                                                 EvidenceArtifact.id.in_(artifact_ids))))
        if found != set(artifact_ids):
            raise ApiError(404, "not_found", "Artifact not found.")
    email = normalize_email(str(body.buyer_email))
    if email == normalize_email(project.seller.email):
        raise ApiError(422, "buyer_is_seller", "The buyer address must not be the seller's own address.")
    for old in db.scalars(select(BuyerRequest).where(BuyerRequest.project_id == project.id, BuyerRequest.kind == body.kind.value,
                                                     BuyerRequest.status == BuyerRequestStatus.PENDING.value)):
        if old.change_id == body.change_id and sorted(old.artifact_ids or []) == artifact_ids:
            old.status, old.session_hash, old.challenge_hash = BuyerRequestStatus.CANCELLED.value, None, None
    token = new_token()
    row = BuyerRequest(project_id=project.id, kind=body.kind.value, change_id=body.change_id, artifact_ids=artifact_ids,
                       buyer_email=email, token_hash=hash_token(token), expires_at=utcnow() + REQUEST_TTL)
    db.add(row)
    db.flush()
    scope_service.record_audit(db, project.id, "BUYER_REQUEST_CREATED", AuditActor.SELLER, "buyer_request", row.id, {
        "kind": row.kind, "change_id": row.change_id, "artifact_ids": artifact_ids, "buyer_email_masked": mask_email(email)})
    db.commit()
    link = f"{portal_base_url.rstrip('/')}/buyer/review?token={token}"
    subject = "Please review a scope change" if body.kind is BuyerRequestKind.SCOPE_CHANGE_APPROVAL else "Please review a delivery"
    try:
        mailer.send(email, subject, f"A seller asked you to review something.\nOpen: {link}\n"
                                    "You will be asked for a one-time code sent to this address. If you did not expect this, ignore it.")
    except Exception:  # the token was never delivered: withdraw the request instead of leaving a dead link
        row.status, row.session_hash, row.challenge_hash = BuyerRequestStatus.CANCELLED.value, None, None
        db.commit()
        raise ApiError(502, "buyer_mail_failed", "The e-mail could not be sent; no request is pending.") from None
    return request_view(row)


def cancel_request(db: Session, project: Project, request_id: str) -> schemas.BuyerRequestView:
    row = db.scalar(select(BuyerRequest).where(BuyerRequest.id == request_id, BuyerRequest.project_id == project.id))
    if row is None:
        raise ApiError(404, "not_found", "Request not found.")
    if row.status != BuyerRequestStatus.PENDING.value:
        raise ApiError(409, "request_not_pending", "Only a pending request can be cancelled.")
    row.status, row.session_hash, row.challenge_hash = BuyerRequestStatus.CANCELLED.value, None, None
    scope_service.record_audit(db, project.id, "BUYER_REQUEST_CANCELLED", AuditActor.SELLER, "buyer_request", row.id)
    db.commit()
    return request_view(row)


def _by_token(db: Session, token: str) -> BuyerRequest | None:
    return db.scalar(select(BuyerRequest).where(BuyerRequest.token_hash == hash_token(token)))


def _open(row: BuyerRequest | None) -> bool:
    return row is not None and row.status == BuyerRequestStatus.PENDING.value and row.expires_at > utcnow()


def send_challenge(db: Session, mailer: Mailer, token: str) -> None:
    """Sends a fresh one-time code to the buyer's mailbox. Silent for unknown, expired, decided or cancelled links."""
    row = _by_token(db, token)
    if not _open(row) or row.challenge_sends >= MAX_CHALLENGE_SENDS:
        return
    code = f"{secrets.randbelow(1_000_000):06d}"
    row.challenge_hash, row.challenge_expires_at = _code_hash(row.id, code), utcnow() + CHALLENGE_TTL
    row.challenge_sends, row.verify_failures = row.challenge_sends + 1, 0
    db.commit()
    try:
        mailer.send(row.buyer_email, "Your one-time code", f"code: {code}\nIt expires in 10 minutes. Do not share it.")
    except Exception:  # never let a mail failure distinguish a valid link from an invalid one
        _LOG.error("buyer_challenge_mail_failed")


def _subject_summary(db: Session, row: BuyerRequest) -> dict:
    if row.kind == BuyerRequestKind.SCOPE_CHANGE_APPROVAL.value:
        change = db.get(ScopeChange, row.change_id)
        return {"type": "scope_change", "title": change.title, "description": change.description,
                "requirements": change.proposed_requirements}
    artifacts = db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.id.in_(row.artifact_ids or [])).order_by(EvidenceArtifact.name))
    return {"type": "delivery", "artifacts": [
        {"artifact_id": a.id, "name": a.name, "version": a.version, "sha256": a.sha256,
         "content_verification": a.content_verification, "description": a.description} for a in artifacts]}


def verify_code(db: Session, token: str, code: str) -> schemas.BuyerSessionResponse:
    row = _by_token(db, token)
    failure = ApiError(401, *_GENERIC_FAILURE)
    now = utcnow()
    if (not _open(row) or row.challenge_hash is None or row.challenge_expires_at is None or row.challenge_expires_at <= now
            or row.verify_failures >= MAX_VERIFY_FAILURES):
        raise failure
    # Charge the attempt atomically BEFORE comparing, so parallel guesses cannot all be checked against an unincremented counter.
    charged = db.execute(update(BuyerRequest).where(BuyerRequest.id == row.id, BuyerRequest.verify_failures < MAX_VERIFY_FAILURES)
                         .values(verify_failures=BuyerRequest.verify_failures + 1))
    db.commit()
    if charged.rowcount != 1 or not tokens_match(row.challenge_hash, _code_hash(row.id, code)):
        raise failure
    session = new_token()
    row.challenge_hash, row.challenge_expires_at = None, None
    row.session_hash, row.session_expires_at = hash_token(session), now + SESSION_TTL
    row.verified_at = row.verified_at or now
    scope_service.record_audit(db, row.project_id, "BUYER_REQUEST_OPENED", AuditActor.BUYER_EMAIL_VERIFIED, "buyer_request", row.id,
                               {"kind": row.kind})
    try:
        db.commit()
    except StaleDataError:  # a concurrent attempt won; this one gets the same generic answer
        db.rollback()
        raise failure from None
    project = db.get(Project, row.project_id)
    return schemas.BuyerSessionResponse(
        session_token=session, expires_at=row.session_expires_at, kind=BuyerRequestKind(row.kind), project_title=project.title,
        subject=_subject_summary(db, row), allowed_decisions=ALLOWED[BuyerRequestKind(row.kind)])


def decide(db: Session, body: schemas.BuyerDecideRequest) -> schemas.BuyerDecisionResponse:
    row = db.scalar(select(BuyerRequest).where(BuyerRequest.session_hash == hash_token(body.session_token)))
    if (not _open(row) or row.session_expires_at is None or row.session_expires_at <= utcnow()):
        raise ApiError(401, "buyer_session_invalid", "The session is not valid.")
    kind = BuyerRequestKind(row.kind)
    if body.decision not in ALLOWED[kind]:
        raise ApiError(422, "invalid_decision", "That decision does not apply to this request.")
    change = None
    if kind is BuyerRequestKind.SCOPE_CHANGE_APPROVAL:
        change = db.get(ScopeChange, row.change_id)
        if change.status != ChangeStatus.PROPOSED.value:
            raise ApiError(409, "change_not_pending", "This change has already been decided or superseded.")
    bindings = []
    if kind is BuyerRequestKind.DELIVERY_REVIEW:  # bind the decision to the exact artifacts and hashes the buyer was shown
        for artifact in db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.id.in_(row.artifact_ids or []))
                                   .order_by(EvidenceArtifact.name, EvidenceArtifact.version)):
            bindings.append({"artifact_id": artifact.id, "version": artifact.version, "sha256": artifact.sha256})
    now = utcnow()
    seller_email = db.get(Project, row.project_id).seller.email
    record = BuyerDecisionRecord(
        change_sha256=scope_service.change_content_sha256(change) if change is not None else None,
        shares_seller_domain=_domain(row.buyer_email) == _domain(seller_email), request_id=row.id, project_id=row.project_id, kind=row.kind, decision=body.decision.value, buyer_email=row.buyer_email,
        statement=body.statement, change_id=row.change_id, artifact_bindings=bindings, verified_at=row.verified_at, decided_at=now)
    try:
        db.add(record)
        row.status, row.decided_at = BuyerRequestStatus.DECIDED.value, now
        row.session_hash, row.session_expires_at, row.challenge_hash = None, None, None
        db.flush()
        scope_service.record_audit(db, row.project_id, "BUYER_DECISION_RECORDED", AuditActor.BUYER_EMAIL_VERIFIED, "buyer_decision",
                                   record.id, {"decision": body.decision.value, "kind": row.kind, "request_id": row.id})
        applied, note = None, None
        if change is not None:
            outcome = scope_service.apply_change_decision(db, change, approve=body.decision is BuyerDecision.APPROVE_CHANGE,
                                                          decision_ref=record.id)
            applied = outcome.applied
            note = None if outcome.applied or outcome.reason == "REJECTED" else outcome.reason
        else:
            scope_service.record_audit(db, row.project_id, DELIVERY_EVENTS[body.decision], AuditActor.BUYER_EMAIL_VERIFIED,
                                       "buyer_decision", record.id, {"decision_ref": record.id, "artifact_bindings": bindings})
        db.commit()
    except (StaleDataError, IntegrityError):
        db.rollback()
        raise ApiError(409, "request_not_pending", "This request was already decided.") from None
    return schemas.BuyerDecisionResponse(decision=decision_view(record), scope_applied=applied, scope_note=note)
