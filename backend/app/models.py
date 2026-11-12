"""Persistent data model."""
import threading
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    DDL, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text,
    TypeDecorator, UniqueConstraint, event, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, object_session, relationship

from app.schemas import (
    AuditActor, BaselineKind, BuyerDecision, BuyerRequestKind, BuyerRequestStatus, ChangeStatus, ContentVerification,
    AuthenticatorTrustBasis, ChangeResolution, DeliveryEventType, EvidenceStatus, InstructionProvenance, OperationState, PaymentEventType,
    ObservationConfidence, ObservationKind, ObservationSource, PaymentProviderName, PaymentProviderStatus, ProjectStatus,
    ProviderInvoiceState, ScopeKind, VerificationOutcome,
)


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_clock_lock = threading.Lock()
_last_stamp: list[datetime | None] = [None]


def monotonic_utcnow() -> datetime:
    """utcnow() that never repeats or goes backwards within the process, so same-transaction history keeps its true order."""
    with _clock_lock:
        now = utcnow()
        last = _last_stamp[0]
        if last is not None and now <= last:
            now = last + timedelta(microseconds=1)
        _last_stamp[0] = now
        return now


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo on read)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected; use timezone-aware UTC")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def enum_check(column: str, enum_cls: type[Enum], name: str, nullable: bool = False) -> CheckConstraint:
    """CHECK constraint whose allowed values come from the schema enum (single source)."""
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    prefix = f"{column} IS NULL OR " if nullable else ""
    return CheckConstraint(f"{prefix}{column} IN ({values})", name=name)


class AppendOnlyError(Exception):
    """Raised on any attempt to modify or delete a row of an append-only (evidence/history) table."""


def make_append_only(model: type) -> None:
    """ORM guard plus (on SQLite) database triggers: history can only be added to, never rewritten."""

    @event.listens_for(model, "before_update")
    def _reject_update(_mapper, _connection, target):
        session = object_session(target)
        if session is not None and session.is_modified(target, include_collections=False):
            raise AppendOnlyError(f"{model.__tablename__} rows are append-only")

    @event.listens_for(model, "before_delete")
    def _reject_delete(_mapper, _connection, _target):
        raise AppendOnlyError(f"{model.__tablename__} rows are append-only")

    table = model.__tablename__
    for op in ("UPDATE", "DELETE"):
        event.listen(
            model.__table__,
            "after_create",
            DDL(f"CREATE TRIGGER {table}_no_{op.lower()} BEFORE {op} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} are append-only'); END;").execute_if(dialect="sqlite"),
        )


class Base(DeclarativeBase):
    pass


class Seller(Base):
    __tablename__ = "sellers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # stored lower-cased (normalize_email)
    password_hash: Mapped[str] = mapped_column(String(255))  # Argon2id; never the password
    display_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SellerSession(Base):
    """Server-side session. Only SHA-256 digests of the session and CSRF tokens are stored."""

    __tablename__ = "seller_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    seller_id: Mapped[str] = mapped_column(ForeignKey("sellers.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    seller_id: Mapped[str] = mapped_column(ForeignKey("sellers.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default=ProjectStatus.DRAFT.value)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    seller: Mapped[Seller] = relationship()
    __table_args__ = (enum_check("status", ProjectStatus, "ck_projects_status"),)


class ScopeRecord(Base):
    __tablename__ = "scope_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    source_label: Mapped[str] = mapped_column(String(255))
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (enum_check("kind", ScopeKind, "ck_scope_records_kind"),)


ScopeRecordImmutableError = AppendOnlyError  # backwards-compatible name
make_append_only(ScopeRecord)


class PaymentRecord(Base):
    """One provider invoice for a project. Rows are never deleted; every change is mirrored in payment_events."""

    __tablename__ = "payment_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    provider: Mapped[str] = mapped_column(String(24))  # MOCK_PAYPAL | PAYPAL_SANDBOX (PAYPAL live: not implemented)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    request_fingerprint: Mapped[str] = mapped_column(String(64))  # sha256 of the normalized request
    recipient_email: Mapped[str] = mapped_column(String(254))
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)  # deterministic; sent to provider
    paypal_invoice_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    paypal_transaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(Integer)  # minor units; no floats
    amount_paid_minor: Mapped[int] = mapped_column(Integer, default=0)  # PayPal-verified payments only
    amount_refunded_minor: Mapped[int] = mapped_column(Integer, default=0)
    provider_status: Mapped[str] = mapped_column(String(40), default=PaymentProviderStatus.UNVERIFIED.value)
    invoice_state: Mapped[str | None] = mapped_column(String(24), nullable=True)  # provider lifecycle, as reported
    operation_state: Mapped[str] = mapped_column(String(24), default=OperationState.READY.value)
    operation_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    replaces_payment_id: Mapped[str | None] = mapped_column(ForeignKey("payment_records.id"), nullable=True)
    instruction_id: Mapped[str | None] = mapped_column(ForeignKey("payment_instructions.id"), nullable=True)  # R1
    instruction_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_record_id: Mapped[str | None] = mapped_column(ForeignKey("scope_records.id"), nullable=True)
    scope_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    verification_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # optimistic concurrency for reconciliation
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_payment_records_project_key"),
        enum_check("provider", PaymentProviderName, "ck_payment_records_provider"),
        enum_check("provider_status", PaymentProviderStatus, "ck_payment_records_provider_status"),
        enum_check("operation_state", OperationState, "ck_payment_records_operation_state"),
        enum_check("invoice_state", ProviderInvoiceState, "ck_payment_records_invoice_state", nullable=True),
        # One active invoice per project, enforced by the database so concurrent requests with different
        # idempotency keys cannot both succeed. Cancelled and fully refunded invoices are history.
        Index("uq_payment_records_one_active_per_project", "project_id", unique=True,
              sqlite_where=text("provider_status NOT IN ('CANCELLED', 'REFUNDED')"),
              postgresql_where=text("provider_status NOT IN ('CANCELLED', 'REFUNDED')")),
        CheckConstraint("amount_minor >= 1", name="ck_payment_records_amount_positive"),
        CheckConstraint("amount_paid_minor >= 0 AND amount_refunded_minor >= 0", name="ck_payment_records_amounts_nonneg"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_payment_records_currency"),
    )


class PaymentEvent(Base):
    """Append-only payment history: every financial or lifecycle change and every anomaly."""

    __tablename__ = "payment_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    payment_id: Mapped[str] = mapped_column(ForeignKey("payment_records.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor: Mapped[str] = mapped_column(String(16))  # SELLER (explicit action) | SYSTEM (reconciliation)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount_paid_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_refunded_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (
        enum_check("event_type", PaymentEventType, "ck_payment_events_event_type"),
        enum_check("from_status", PaymentProviderStatus, "ck_payment_events_from_status", nullable=True),
        enum_check("to_status", PaymentProviderStatus, "ck_payment_events_to_status", nullable=True),
        CheckConstraint("actor IN ('SELLER', 'SYSTEM')", name="ck_payment_events_actor"),
    )


make_append_only(PaymentEvent)


class Deliverable(Base):
    __tablename__ = "deliverables"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(255))  # private storage reference, not a URL
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("project_id", "name", "version"),)


class DeliveryEvent(Base):
    __tablename__ = "delivery_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deliverable_id: Mapped[str] = mapped_column(ForeignKey("deliverables.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor_type: Mapped[str] = mapped_column(String(24))  # SELLER, VERIFIED_EMAIL_HOLDER, SYSTEM
    auth_method: Mapped[str] = mapped_column(String(32))  # SESSION, EMAIL_OTP, SYSTEM, etc.
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # minimize personal data
    __table_args__ = (enum_check("event_type", DeliveryEventType, "ck_delivery_events_event_type"),)


class EvidenceClaim(Base):
    __tablename__ = "evidence_claims"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    claim: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24))
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list)  # IDs from records/events
    reviewer_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (enum_check("status", EvidenceStatus, "ck_evidence_claims_status"),)


# --- scope baselines, requirements, change proposals, audit trail (Stage 3A) ---------------------

class ScopeBaseline(Base):
    """Immutable snapshot of the agreed scope. Version 1 is the ORIGINAL agreement; later versions exist only because
    a verified buyer approval was applied. Rows are never updated or deleted."""

    __tablename__ = "scope_baselines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    source_scope_record_id: Mapped[str | None] = mapped_column(ForeignKey("scope_records.id"), nullable=True)
    previous_baseline_id: Mapped[str | None] = mapped_column(ForeignKey("scope_baselines.id"), nullable=True)
    change_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # the approved change that produced it
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    requirements: Mapped[list["Requirement"]] = relationship(order_by="Requirement.key", viewonly=True)
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_scope_baselines_project_version"),
        enum_check("kind", BaselineKind, "ck_scope_baselines_kind"),
        CheckConstraint("version >= 1", name="ck_scope_baselines_version"),
    )


make_append_only(ScopeBaseline)


class Requirement(Base):
    __tablename__ = "requirements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    baseline_id: Mapped[str] = mapped_column(ForeignKey("scope_baselines.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    key: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("baseline_id", "key", name="uq_requirements_baseline_key"),)


make_append_only(Requirement)


class ScopeChange(Base):
    """A proposed change. The only mutable evidence row of this stage: its status moves PROPOSED -> APPROVED | REJECTED |
    SUPERSEDED exclusively through services.scope, and every move is mirrored in the append-only audit_events."""

    __tablename__ = "scope_changes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    proposed_requirements: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default=ChangeStatus.PROPOSED.value)
    base_baseline_id: Mapped[str] = mapped_column(ForeignKey("scope_baselines.id"))
    resolved_baseline_id: Mapped[str | None] = mapped_column(ForeignKey("scope_baselines.id"), nullable=True)
    supersedes_change_id: Mapped[str | None] = mapped_column(ForeignKey("scope_changes.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)  # optimistic concurrency for status moves
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (enum_check("status", ChangeStatus, "ck_scope_changes_status"),)


class AuditEvent(Base):
    """Append-only trail of meaningful state transitions (who, what, which subject)."""

    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    actor_role: Mapped[str] = mapped_column(String(24))
    subject_type: Mapped[str] = mapped_column(String(24))
    subject_id: Mapped[str] = mapped_column(String(36))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (enum_check("actor_role", AuditActor, "ck_audit_events_actor_role"),)


make_append_only(AuditEvent)


# --- delivery evidence artifacts (Stage 3B) --------------------------------------------------------

class EvidenceArtifact(Base):
    """Immutable record of a delivery artifact. A replacement is a new row (version + 1) that points at its predecessor."""

    __tablename__ = "evidence_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content_verification: Mapped[str] = mapped_column(String(24))
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    asserted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    supersedes_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("evidence_artifacts.id"), nullable=True, unique=True)
    __table_args__ = (
        UniqueConstraint("project_id", "name", "version", name="uq_evidence_artifacts_project_name_version"),
        enum_check("content_verification", ContentVerification, "ck_evidence_artifacts_verification"),
        CheckConstraint("(content_verification = 'UNAVAILABLE') = (sha256 IS NULL)", name="ck_evidence_artifacts_hash_label"),
    )


make_append_only(EvidenceArtifact)


class ArtifactLink(Base):
    """Seller-asserted link of an artifact to a requirement key. A link is a claim, never proof the requirement is met."""

    __tablename__ = "artifact_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("evidence_artifacts.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    requirement_key: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (UniqueConstraint("artifact_id", "requirement_key", name="uq_artifact_links_artifact_key"),)


make_append_only(ArtifactLink)


# --- buyer acknowledgment and authorization (Stage 3C) ---------------------------------------------

class BuyerRequest(Base):
    """A pending ask to one e-mail address. Only hashes of the link token, code and session are stored."""

    __tablename__ = "buyer_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    change_id: Mapped[str | None] = mapped_column(ForeignKey("scope_changes.id"), nullable=True)
    artifact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    buyer_email: Mapped[str] = mapped_column(String(254))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=BuyerRequestStatus.PENDING.value)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    challenge_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    challenge_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    challenge_sends: Mapped[int] = mapped_column(Integer, default=0)
    verify_failures: Mapped[int] = mapped_column(Integer, default=0)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    session_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    session_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        enum_check("kind", BuyerRequestKind, "ck_buyer_requests_kind"),
        enum_check("status", BuyerRequestStatus, "ck_buyer_requests_status"),
    )


class BuyerDecisionRecord(Base):
    __tablename__ = "buyer_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(ForeignKey("buyer_requests.id"), unique=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(24))
    buyer_email: Mapped[str] = mapped_column(String(254))
    statement: Mapped[str | None] = mapped_column(String(500), nullable=True)
    verification_method: Mapped[str] = mapped_column(String(16), default="EMAIL_OTP")
    change_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    artifact_bindings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    change_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)  # what the buyer saw, for change decisions
    shares_seller_domain: Mapped[bool] = mapped_column(Boolean, default=False)  # buyer mailbox is on the seller's e-mail domain
    verified_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        enum_check("kind", BuyerRequestKind, "ck_buyer_decisions_kind"),
        enum_check("decision", BuyerDecision, "ck_buyer_decisions_decision"),
        CheckConstraint("verification_method = 'EMAIL_OTP'", name="ck_buyer_decisions_method"),
    )


make_append_only(BuyerDecisionRecord)


class AnalysisRun(Base):
    """Immutable record of one analysis: what evidence went in (hash), who produced it and the validated findings."""

    __tablename__ = "analysis_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    baseline_id: Mapped[str] = mapped_column(ForeignKey("scope_baselines.id"))
    baseline_version: Mapped[int] = mapped_column(Integer)
    provider_name: Mapped[str] = mapped_column(String(48))
    production_grade: Mapped[bool] = mapped_column(Boolean, default=False)
    input_sha256: Mapped[str] = mapped_column(String(64))
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)


make_append_only(AnalysisRun)


# --- payment instruction integrity (R1) ----------------------------------------------------------------

class PaymentInstruction(Base):
    """An immutable, versioned record of WHERE and HOW MUCH a project is to be paid. A change is a new version (R2); never an edit."""

    __tablename__ = "payment_instructions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(24))
    merchant_email: Mapped[str | None] = mapped_column(String(254), nullable=True)  # provider-configured payee; None only for MOCK
    recipient_email: Mapped[str] = mapped_column(String(254))
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(Integer)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # agreement context at recording time
    fingerprint: Mapped[str] = mapped_column(String(64))  # sha256 of the canonical content
    previous_instruction_id: Mapped[str | None] = mapped_column(ForeignKey("payment_instructions.id"), nullable=True)
    chain_sha256: Mapped[str] = mapped_column(String(64))  # binds the version to its predecessor
    provenance: Mapped[str] = mapped_column(String(24))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_payment_instructions_project_version"),
        UniqueConstraint("project_id", "idempotency_key", name="uq_payment_instructions_project_key"),
        enum_check("provider", PaymentProviderName, "ck_payment_instructions_provider"),
        enum_check("provenance", InstructionProvenance, "ck_payment_instructions_provenance"),
        CheckConstraint("amount_minor >= 1", name="ck_payment_instructions_amount_positive"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_payment_instructions_currency"),
        CheckConstraint("version >= 1", name="ck_payment_instructions_version"),
    )


make_append_only(PaymentInstruction)


class InstructionChangeRequest(Base):
    """A proposed change to the instruction in force. It stays pending (and HOLDS payment operations) until an independent
    verification resolves it; R1 provides no way to approve it."""

    __tablename__ = "instruction_change_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    from_instruction_id: Mapped[str] = mapped_column(ForeignKey("payment_instructions.id"))
    from_fingerprint: Mapped[str] = mapped_column(String(64))
    merchant_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    recipient_email: Mapped[str] = mapped_column(String(254))
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(Integer)
    proposed_fingerprint: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(String(24))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    request_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_instruction_changes_project_key"),
        enum_check("provenance", InstructionProvenance, "ck_instruction_changes_provenance"),
        CheckConstraint("amount_minor >= 1", name="ck_instruction_changes_amount_positive"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_instruction_changes_currency"),
    )


make_append_only(InstructionChangeRequest)


class InstructionChangeResolution(Base):
    """Terminal outcome of a change request. At most one per request; a request without one is pending (holds)."""

    __tablename__ = "instruction_change_resolutions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    change_id: Mapped[str] = mapped_column(ForeignKey("instruction_change_requests.id"), unique=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    resolution: Mapped[str] = mapped_column(String(24))
    actor_role: Mapped[str] = mapped_column(String(24))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        enum_check("resolution", ChangeResolution, "ck_instruction_resolutions_resolution"),
        enum_check("actor_role", AuditActor, "ck_instruction_resolutions_actor"),
    )


make_append_only(InstructionChangeResolution)


# --- independent authorization of instruction changes (R2) ---------------------------------------------------------

class BuyerAuthenticator(Base):
    """A TOTP authenticator created by the OPERATOR (never by the seller). The secret is stored encrypted (AES-256-GCM, AAD-bound)."""

    __tablename__ = "buyer_authenticators"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    buyer_email: Mapped[str] = mapped_column(String(254))
    secret_ciphertext: Mapped[str] = mapped_column(Text)  # base64(nonce || ciphertext || tag); never the secret
    key_id: Mapped[str] = mapped_column(String(32))
    trust_basis: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (enum_check("trust_basis", AuthenticatorTrustBasis, "ck_buyer_authenticators_trust_basis"),)


make_append_only(BuyerAuthenticator)


class AuthenticatorRevocation(Base):
    __tablename__ = "authenticator_revocations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    authenticator_id: Mapped[str] = mapped_column(ForeignKey("buyer_authenticators.id"), unique=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)


make_append_only(AuthenticatorRevocation)


class InstructionVerificationChallenge(Base):
    """Single-use challenge bound to the exact project, change, fingerprints, approver and authenticator. Only `failed_attempts`
    and `consumed_at` ever change, and only through the verification service."""

    __tablename__ = "instruction_verification_challenges"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    change_id: Mapped[str] = mapped_column(ForeignKey("instruction_change_requests.id"), index=True)
    authenticator_id: Mapped[str] = mapped_column(ForeignKey("buyer_authenticators.id"), index=True)
    approver_email: Mapped[str] = mapped_column(String(254))
    from_fingerprint: Mapped[str] = mapped_column(String(64))
    proposed_fingerprint: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)


class TotpUse(Base):
    """A consumed TOTP time step. UNIQUE(authenticator, step) is the replay guard."""

    __tablename__ = "totp_uses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    authenticator_id: Mapped[str] = mapped_column(ForeignKey("buyer_authenticators.id"))
    step: Mapped[int] = mapped_column(Integer)
    challenge_id: Mapped[str] = mapped_column(ForeignKey("instruction_verification_challenges.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (UniqueConstraint("authenticator_id", "step", name="uq_totp_uses_authenticator_step"),)


make_append_only(TotpUse)


class InstructionChangeVerification(Base):
    """The ONLY record that releases a payment hold. At most one per change. APPLIED references the instruction version it created."""

    __tablename__ = "instruction_change_verifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    change_id: Mapped[str] = mapped_column(ForeignKey("instruction_change_requests.id"), unique=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(16))
    authenticator_id: Mapped[str] = mapped_column(ForeignKey("buyer_authenticators.id"))
    challenge_id: Mapped[str] = mapped_column(ForeignKey("instruction_verification_challenges.id"), unique=True)
    approver_email: Mapped[str] = mapped_column(String(254))
    from_fingerprint: Mapped[str] = mapped_column(String(64))
    proposed_fingerprint: Mapped[str] = mapped_column(String(64))
    trust_basis: Mapped[str] = mapped_column(String(32))
    applied_instruction_id: Mapped[str | None] = mapped_column(ForeignKey("payment_instructions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        enum_check("outcome", VerificationOutcome, "ck_instruction_verifications_outcome"),
        enum_check("trust_basis", AuthenticatorTrustBasis, "ck_instruction_verifications_trust_basis"),
        CheckConstraint("(outcome = 'APPLIED') = (applied_instruction_id IS NOT NULL)", name="ck_instruction_verifications_applied"),
    )


make_append_only(InstructionChangeVerification)


# --- payment observations and merchant ledger (R3) -----------------------------------------------------------------

class MerchantLedgerImport(Base):
    """One uploaded merchant-ledger file. The file hash is the idempotency key: the same file is never imported twice."""

    __tablename__ = "merchant_ledger_imports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    file_sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    recorded_count: Mapped[int] = mapped_column(Integer)
    duplicate_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    __table_args__ = (
        UniqueConstraint("project_id", "file_sha256", name="uq_merchant_ledger_imports_project_file"),
        CheckConstraint("row_count >= 1 AND recorded_count >= 0 AND duplicate_count >= 0 AND recorded_count + duplicate_count = row_count",
                        name="ck_merchant_ledger_imports_counts"),
    )


make_append_only(MerchantLedgerImport)


class PaymentObservation(Base):
    """Append-only record of one thing a source said about a payment. PROVIDER_API rows are written only by reconciliation;
    every other source is a claim. The database ties source to confidence so a claim cannot be stored as provider evidence."""

    __tablename__ = "payment_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey("payment_records.id"), index=True)
    import_id: Mapped[str | None] = mapped_column(ForeignKey("merchant_ledger_imports.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(String(16))  # SELLER (claims) | SYSTEM (provider evidence)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    source_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)  # provider/merchant transaction id
    source_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)  # invoice number or merchant row id
    provider_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    instrument: Mapped[str | None] = mapped_column(String(16), nullable=True)  # provider-reported PAYPAL | EXTERNAL
    asserted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # source-claimed time, stored separately
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)  # server clock
    payload: Mapped[dict] = mapped_column(JSON)  # normalized content that was hashed
    payload_sha256: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    __table_args__ = (
        UniqueConstraint("project_id", "dedupe_key", name="uq_payment_observations_project_dedupe"),
        Index("ix_payment_observations_source_event", "project_id", "source_event_id"),
        enum_check("source", ObservationSource, "ck_payment_observations_source"),
        enum_check("kind", ObservationKind, "ck_payment_observations_kind"),
        enum_check("confidence", ObservationConfidence, "ck_payment_observations_confidence"),
        CheckConstraint("actor IN ('SELLER', 'SYSTEM')", name="ck_payment_observations_actor"),
        CheckConstraint("(source = 'PROVIDER_API') = (confidence = 'PROVIDER_EVIDENCE')", name="ck_payment_observations_provider_confidence"),
        CheckConstraint("(source = 'WEBHOOK') = (confidence = 'UNCONFIRMED_WEBHOOK')", name="ck_payment_observations_webhook_confidence"),
        CheckConstraint("(source IN ('MERCHANT_IMPORT', 'SELLER_ENTRY')) = (confidence = 'UNVERIFIED_CLAIM')", name="ck_payment_observations_claim_confidence"),
        CheckConstraint("(source IN ('MERCHANT_IMPORT', 'SELLER_ENTRY')) = (actor = 'SELLER')", name="ck_payment_observations_actor_matches_source"),
        CheckConstraint("(source = 'MERCHANT_IMPORT') = (import_id IS NOT NULL)", name="ck_payment_observations_import_link"),
        CheckConstraint("(kind = 'INVOICE_STATE') = (provider_state IS NOT NULL)", name="ck_payment_observations_state_kind"),
        CheckConstraint("kind != 'INVOICE_STATE' OR source = 'PROVIDER_API'", name="ck_payment_observations_state_from_provider"),
        CheckConstraint("amount_minor >= 0", name="ck_payment_observations_amount_nonneg"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_payment_observations_currency"),
    )


make_append_only(PaymentObservation)


class ProviderEvidenceGap(Base):
    """Append-only note that part of what the provider reported could NOT be stored as a normalized observation (R3 acceptance).

    A gap means the stored observations for this payment are INCOMPLETE: the absence of an observation is then not evidence that a
    payment did not happen. Only a bounded fingerprint of the raw value is kept, never the value itself."""

    __tablename__ = "provider_evidence_gaps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey("payment_records.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # PAYMENT | REFUND | INVOICE_STATE
    reason: Mapped[str] = mapped_column(String(32))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=monotonic_utcnow)
    dedupe_key: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("project_id", "dedupe_key", name="uq_provider_evidence_gaps_project_dedupe"),
        CheckConstraint("kind IN ('PAYMENT', 'REFUND', 'INVOICE_STATE')", name="ck_provider_evidence_gaps_kind"),
        CheckConstraint("reason IN ('OVERSIZED_AMOUNT', 'NEGATIVE_AMOUNT', 'INVALID_CURRENCY', 'OVERLONG_TRANSACTION_ID')",
                        name="ck_provider_evidence_gaps_reason"),
    )


make_append_only(ProviderEvidenceGap)


from app import sim_models  # noqa: E402,F401  (registers the simulated merchant tables on Base.metadata)
from app import payee_models  # noqa: E402,F401  (registers the payee destination registry on Base.metadata)
