"""Pydantic API schemas. These are the single source of truth for canonical enums;
SQLAlchemy CHECK constraints in models.py are generated from them, and
contracts/*.schema.json are generated from them (see scripts/export_contracts.py)."""
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator,
    model_validator,
)

CURRENCY_PATTERN = r"^[A-Z]{3}$"
# Matches the database Integer column on every backend (PostgreSQL INTEGER is 32-bit).
MAX_AMOUNT_MINOR = 2_000_000_000
MAX_SCOPE_CONTENT_BYTES = 100_000
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


class ProjectStatus(str, Enum):
    DRAFT = "DRAFT"
    INVOICED = "INVOICED"
    PAID = "PAID"
    DELIVERED = "DELIVERED"
    DISPUTED = "DISPUTED"


class ScopeKind(str, Enum):
    ORIGINAL = "ORIGINAL"
    CHANGE_REQUEST = "CHANGE_REQUEST"
    APPROVAL = "APPROVAL"
    DELIVERY_NOTE = "DELIVERY_NOTE"


class PaymentProviderName(str, Enum):
    """Which backend produced a payment record. MOCK_PAYPAL is never a real transaction;
    PAYPAL_SANDBOX is PayPal's test environment, never real money. PAYPAL (live) is not implemented."""

    MOCK_PAYPAL = "MOCK_PAYPAL"
    PAYPAL_SANDBOX = "PAYPAL_SANDBOX"
    PAYPAL = "PAYPAL"


class PaymentProviderStatus(str, Enum):
    """Financial state derived ONLY from verified provider evidence by the reconciliation path.
    Never accepted from a client."""

    UNVERIFIED = "UNVERIFIED"  # no payment evidence yet (invoice draft, or not yet synchronized)
    PENDING = "PENDING"  # invoice sent, awaiting payment
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"  # full refund of what was paid
    CANCELLED = "CANCELLED"
    NEEDS_REVIEW = "NEEDS_REVIEW"  # contradictory or unexpected evidence: a human must look


class ProviderInvoiceState(str, Enum):
    """PayPal Invoicing v2 invoice lifecycle status (what the provider reports, not proof of payment)."""

    DRAFT = "DRAFT"
    SENT = "SENT"
    SCHEDULED = "SCHEDULED"
    UNPAID = "UNPAID"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    MARKED_AS_PAID = "MARKED_AS_PAID"  # recorded manually/externally: NOT proof of a PayPal payment
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    MARKED_AS_REFUNDED = "MARKED_AS_REFUNDED"  # recorded manually/externally
    CANCELLED = "CANCELLED"


class OperationState(str, Enum):
    """Local state of an in-flight or interrupted provider operation (crash/ambiguity recovery)."""

    RESERVED = "RESERVED"  # row committed; provider create not yet attempted (or safely retryable)
    CREATING = "CREATING"
    CREATE_UNCERTAIN = "CREATE_UNCERTAIN"  # outcome unknown: resume with the same Idempotency-Key
    CREATE_FAILED = "CREATE_FAILED"  # provider definitively rejected; no invoice exists
    READY = "READY"
    SEND_PENDING = "SEND_PENDING"
    SEND_UNCERTAIN = "SEND_UNCERTAIN"  # outcome unknown: synchronize to learn the truth


class PaymentEventType(str, Enum):
    INVOICE_CREATED = "INVOICE_CREATED"
    INVOICE_SENT = "INVOICE_SENT"
    STATUS_CHANGED = "STATUS_CHANGED"
    AMOUNTS_UPDATED = "AMOUNTS_UPDATED"
    ANOMALY = "ANOMALY"
    OPERATION_UNCERTAIN = "OPERATION_UNCERTAIN"
    OPERATION_FAILED = "OPERATION_FAILED"
    REVIEW_RESOLVED = "REVIEW_RESOLVED"  # explicit, audited decision by the seller


class DeliveryEventType(str, Enum):
    LINK_SENT = "LINK_SENT"
    LINK_ACCESSED = "LINK_ACCESSED"
    FILE_RELEASED = "FILE_RELEASED"
    BUYER_ACKNOWLEDGED = "BUYER_ACKNOWLEDGED"


class EvidenceStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"


# --- authentication -----------------------------------------------------------------

class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: SecretStr = Field(min_length=12, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: SecretStr = Field(min_length=1, max_length=128)


class RegistrationAck(BaseModel):
    """Identical for new and already-registered emails (no account enumeration)."""

    status: Literal["registered"] = "registered"


class SellerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    display_name: str


class LoginResponse(BaseModel):
    seller: SellerView
    csrf_token: str
    expires_at: AwareDatetime


class MeResponse(BaseModel):
    seller: SellerView
    expires_at: AwareDatetime


# --- projects and scope -------------------------------------------------------------

class ProjectDraft(BaseModel):
    """Client input. The owning seller is derived from the session, never the body."""

    title: str = Field(min_length=1, max_length=160)


class ProjectView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    seller_id: str
    title: str = Field(min_length=1, max_length=160)
    status: ProjectStatus
    created_at: AwareDatetime


class ScopeRecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ScopeKind
    source_label: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def content_within_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf8")) > MAX_SCOPE_CONTENT_BYTES:
            raise ValueError(f"content exceeds {MAX_SCOPE_CONTENT_BYTES} bytes")
        return value


class ScopeRecordView(BaseModel):
    """Append-only source record. `source_sha256` is computed by the server over `content`."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    kind: ScopeKind
    source_label: str
    source_sha256: str
    content: str
    created_at: AwareDatetime


# --- payments -----------------------------------------------------------------------

class InvoiceRequest(BaseModel):
    """No provider status, project status or owner can be supplied: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(pattern=CURRENCY_PATTERN)
    amount_minor: int = Field(ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    recipient_email: EmailStr
    # Required when the project already has a provider invoice that is now cancelled or fully refunded:
    # an explicit, recorded replacement. Never allowed while another invoice is active.
    replaces_payment_id: str | None = Field(default=None, min_length=1, max_length=36)


# --- payment instruction integrity (R1) -----------------------------------------------------------

class InstructionProvenance(str, Enum):
    SELLER_ENTERED = "SELLER_ENTERED"  # typed by the authenticated seller; not independently verified
    INVOICE_REQUEST = "INVOICE_REQUEST"  # recorded implicitly from the first invoice request (instruction not required)


class InstructionVerification(str, Enum):
    PENDING_VERIFICATION = "PENDING_VERIFICATION"  # seller-entered, nobody independent has confirmed it
    VERIFIED_BY_ENROLLED_SECOND_CHANNEL = "VERIFIED_BY_ENROLLED_SECOND_CHANNEL"  # approved via an operator-established authenticator (R2); NOT real-world identity


class HoldReason(str, Enum):
    UNVERIFIED_INSTRUCTION_CHANGE = "UNVERIFIED_INSTRUCTION_CHANGE"
    INSTRUCTION_INTEGRITY_FAILURE = "INSTRUCTION_INTEGRITY_FAILURE"
    MERCHANT_MISMATCH = "MERCHANT_MISMATCH"
    PROVIDER_MISMATCH = "PROVIDER_MISMATCH"
    INSTRUCTION_REQUIRED = "INSTRUCTION_REQUIRED"


class ChangeResolution(str, Enum):
    WITHDRAWN = "WITHDRAWN"  # the proposer discarded it; the instruction in force is unchanged. Applying a change is R2.


class PaymentInstructionCreate(BaseModel):
    """The payee is the provider's configured merchant, never free text: a client cannot name a different account here."""

    model_config = ConfigDict(extra="forbid")

    recipient_email: EmailStr
    currency: str = Field(pattern=CURRENCY_PATTERN)
    amount_minor: int = Field(ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    merchant_email: EmailStr | None = None  # optional confirmation; rejected unless it equals the provider's merchant


class InstructionChangeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_email: EmailStr
    currency: str = Field(pattern=CURRENCY_PATTERN)
    amount_minor: int = Field(ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    merchant_email: EmailStr | None = None  # a requested NEW payee account; None keeps the current one
    reason: str = Field(min_length=3, max_length=500)


class InstructionChangeWithdraw(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=500)


class PaymentInstructionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    version: int
    provider: PaymentProviderName
    merchant_email: str | None
    recipient_email: str
    currency: str
    amount_minor: int
    context: dict
    fingerprint: str
    chain_sha256: str
    previous_instruction_id: str | None
    provenance: InstructionProvenance
    verification_status: InstructionVerification = InstructionVerification.PENDING_VERIFICATION
    created_at: AwareDatetime


class InstructionChangeView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    from_instruction_id: str
    from_fingerprint: str
    merchant_email: str | None
    recipient_email: str
    currency: str
    amount_minor: int
    proposed_fingerprint: str
    reason: str
    provenance: InstructionProvenance
    resolution: ChangeResolution | None = None  # WITHDRAWN is history only; R1 has no outcome that releases the hold
    holds_payment_operations: bool = True  # stays true after a withdrawal; only a buyer-side verification (R2, enrolled second channel) releases it
    verification: "InstructionChangeVerificationView | None" = None
    created_at: AwareDatetime


class HoldStatus(BaseModel):
    """A hold blocks operations performed through Caligo only. It never freezes or redirects funds held by PayPal."""

    active: bool
    reasons: list[HoldReason]
    pending_change_ids: list[str]
    release_requires: str = "BUYER_SIDE_VERIFICATION"  # an enrolled second channel; not proof of independence from the seller
    scope: str = "CALIGO_OPERATIONS_ONLY"


class PaymentInstructionState(BaseModel):
    instruction: PaymentInstructionView | None
    versions: int
    pending_changes: list[InstructionChangeView]
    hold: HoldStatus


# --- independent authorization of instruction changes (R2) ---------------------------------------------------------

class AuthenticatorTrustBasis(str, Enum):
    OPERATOR_ESTABLISHED = "OPERATOR_ESTABLISHED"  # created by the operator on the server host; NOT verified real-world identity


class VerificationOutcome(str, Enum):
    APPLIED = "APPLIED"  # the buyer-side authenticator approved; a new instruction version was created and the hold released
    REJECTED = "REJECTED"  # the buyer-side authenticator rejected; the instruction in force is unchanged and the hold released


class InstructionDecision(str, Enum):
    APPROVE_INSTRUCTION_CHANGE = "APPROVE_INSTRUCTION_CHANGE"
    REJECT_INSTRUCTION_CHANGE = "REJECT_INSTRUCTION_CHANGE"


class BuyerAuthenticatorView(BaseModel):
    """What the seller may see: that a second channel exists, never the secret and never the full address."""

    id: str
    project_id: str
    buyer_email_masked: str
    trust_basis: AuthenticatorTrustBasis
    status: str  # ACTIVE | REVOKED
    created_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    note: str = ("Operator-established second channel for a demo; this is not verified real-world identity, and it is not independent of the "
                 "seller if the seller is also the operator.")


class InstructionChangeVerificationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    change_id: str
    outcome: VerificationOutcome
    trust_basis: AuthenticatorTrustBasis
    applied_instruction_id: str | None
    from_fingerprint: str
    proposed_fingerprint: str
    created_at: AwareDatetime


class InstructionChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    change_id: str = Field(min_length=1, max_length=36)
    buyer_email: EmailStr


class InstructionValues(BaseModel):
    merchant_email: str | None
    recipient_email: str
    currency: str
    amount_minor: int


class InstructionChangeForBuyer(BaseModel):
    """Rendered by the server from stored rows (never from seller-typed text) so the approver sees exactly what would be applied."""

    project_id: str
    change_id: str
    current: InstructionValues
    proposed: InstructionValues
    from_fingerprint: str
    proposed_fingerprint: str
    seller_reason: str  # seller-written, shown as a claim
    withdrawn_by_seller: bool
    approvable: bool  # false for a withdrawn change: it can only be rejected
    trust_note: str = ("The second channel was established by the operator for this demo; it is not verified real-world identity "
                       "and is not independent of the seller if the seller is also the operator. The current values may themselves be an "
                       "unverified seller entry (version 1 is never independently verified).")


class InstructionChallengeResponse(BaseModel):
    challenge_id: str
    expires_at: AwareDatetime
    change: InstructionChangeForBuyer
    allowed_decisions: list[InstructionDecision]


class InstructionDecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(min_length=1, max_length=36)
    project_id: str = Field(min_length=1, max_length=36)
    change_id: str = Field(min_length=1, max_length=36)
    buyer_email: EmailStr
    from_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: InstructionDecision
    totp_code: str = Field(pattern=r"^[0-9]{6}$")


class InstructionDecideResponse(BaseModel):
    outcome: VerificationOutcome
    verification: InstructionChangeVerificationView
    hold_active: bool
    note: str = ("Authorized by an operator-established second channel; this is not verified real-world identity, not proof of independence "
                 "from the seller, and version 1 of the instruction remains unverified.")


InstructionChangeView.model_rebuild()  # resolves the forward reference to InstructionChangeVerificationView


class SendInvoiceRequest(BaseModel):
    """Sending emails the recipient through PayPal, so the seller must re-type the stored address."""

    model_config = ConfigDict(extra="forbid")

    confirm_recipient_email: EmailStr


class ReviewResolution(str, Enum):
    ACCEPT_PROVIDER_EVIDENCE = "ACCEPT_PROVIDER_EVIDENCE"  # re-fetch from the provider; apply only if now consistent
    ABANDON_UNCREATED_INVOICE = "ABANDON_UNCREATED_INVOICE"  # no provider invoice exists; seller confirmed in the dashboard


class ReviewResolutionRequest(BaseModel):
    """Leaving NEEDS_REVIEW is always an explicit seller decision with a recorded reason."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    resolution: ReviewResolution
    note: str = Field(min_length=10, max_length=200)
    # Required (true) for ABANDON_UNCREATED_INVOICE: the seller attests that no invoice exists at the provider.
    confirm_no_provider_invoice: bool = False


class PaymentLink(BaseModel):
    """Server-produced payment summary. Every financial field is derived from provider evidence."""

    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    project_id: str
    provider: PaymentProviderName = PaymentProviderName.MOCK_PAYPAL
    paypal_invoice_id: str | None = None
    paypal_transaction_id: str | None = None
    currency: str = Field(pattern=CURRENCY_PATTERN)
    amount_minor: int = Field(ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    provider_status: PaymentProviderStatus = PaymentProviderStatus.UNVERIFIED
    invoice_state: ProviderInvoiceState | None = None
    operation_state: OperationState = OperationState.READY
    amount_paid_minor: int = Field(default=0, ge=0)
    amount_refunded_minor: int = Field(default=0, ge=0)
    replaces_payment_id: str | None = None
    scope_record_id: str | None = None
    checked_at: AwareDatetime | None = None
    verification_note: str | None = None
    created_at: AwareDatetime | None = None


class PaymentEventView(BaseModel):
    """Append-only payment history entry."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    payment_id: str
    event_type: PaymentEventType
    from_status: PaymentProviderStatus | None = None
    to_status: PaymentProviderStatus | None = None
    actor: Literal["SELLER", "SYSTEM"]
    note: str | None = None
    amount_paid_minor: int | None = None
    amount_refunded_minor: int | None = None
    created_at: AwareDatetime


class DeliveryStatus(str, Enum):
    """Derived from the buyer's LATEST verified delivery decision. Never stored, never settable by a client."""

    NONE = "NONE"
    ACKNOWLEDGED_ONLY = "ACKNOWLEDGED_ONLY"  # receipt acknowledged; NOT acceptance
    ACCEPTED = "ACCEPTED"
    DISPUTED = "DISPUTED"


class ProjectDetail(ProjectView):
    payments: list[PaymentLink] = Field(default_factory=list)
    delivery_status: DeliveryStatus = DeliveryStatus.NONE


# --- delivery and evidence (later gates) --------------------------------------------

class DeliveryEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    deliverable_id: str
    event_type: DeliveryEventType
    occurred_at: AwareDatetime
    actor_type: str
    auth_method: str


class EvidenceFinding(BaseModel):
    claim: str
    status: EvidenceStatus
    source_refs: list[str] = Field(default_factory=list)
    explanation: str

    @model_validator(mode="after")
    def supported_requires_sources(self) -> "EvidenceFinding":
        if self.status is EvidenceStatus.SUPPORTED and not self.source_refs:
            raise ValueError("a SUPPORTED finding must cite at least one source record")
        return self


class ScopeAnalysis(BaseModel):
    project_id: str
    findings: list[EvidenceFinding]
    human_review_required: Literal[True] = True


# --- scope baselines, requirements, change proposals, audit trail (Stage 3A) ---------------------

REQUIREMENT_KEY_PATTERN = r"^[A-Z][A-Z0-9_-]{0,23}$"


class BaselineKind(str, Enum):
    ORIGINAL = "ORIGINAL"
    AMENDED = "AMENDED"


class ChangeStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class AuditActor(str, Enum):
    SELLER = "SELLER"
    BUYER_EMAIL_VERIFIED = "BUYER_EMAIL_VERIFIED"
    SYSTEM = "SYSTEM"


class RequirementDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=REQUIREMENT_KEY_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


def _unique_keys(items: list[RequirementDraft]) -> list[RequirementDraft]:
    keys = [item.key for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError("requirement keys must be unique")
    return items


class BaselineCreate(BaseModel):
    """Freezes the ORIGINAL agreement. The version, kind and owner are decided by the server."""

    model_config = ConfigDict(extra="forbid")

    source_scope_record_id: str = Field(min_length=1, max_length=36)
    requirements: list[RequirementDraft] = Field(min_length=1, max_length=100)

    _keys_unique = field_validator("requirements")(_unique_keys)


class RequirementView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    baseline_id: str
    key: str
    title: str
    description: str
    origin: str  # "ORIGINAL" or "CHANGE:<scope change id>"
    created_at: AwareDatetime


class BaselineView(BaseModel):
    """An immutable, versioned snapshot of what was agreed. `content_sha256` covers the requirements only."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    version: int
    kind: BaselineKind
    source_scope_record_id: str | None
    previous_baseline_id: str | None
    change_id: str | None
    content_sha256: str
    created_at: AwareDatetime
    requirements: list[RequirementView] = Field(default_factory=list)


class ScopeChangeCreate(BaseModel):
    """A PROPOSED change. Nothing here can approve it: approval exists only as verified buyer evidence."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    requirements: list[RequirementDraft] = Field(min_length=1, max_length=50)
    supersedes_change_id: str | None = Field(default=None, max_length=36)

    _keys_unique = field_validator("requirements")(_unique_keys)


class ScopeChangeView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    title: str
    description: str
    proposed_requirements: list[RequirementDraft]
    status: ChangeStatus
    base_baseline_id: str
    resolved_baseline_id: str | None
    supersedes_change_id: str | None
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None


class AuditEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    event_type: str
    actor_role: AuditActor
    subject_type: str
    subject_id: str
    detail: dict
    created_at: AwareDatetime


# --- delivery evidence artifacts and the timeline (Stage 3B) -------------------------------------

MAX_ARTIFACT_CONTENT_BYTES = 64_000
ASSERTED_TIME_FUTURE_TOLERANCE_SECONDS = 300


class ContentVerification(str, Enum):
    """What the server can honestly say about an artifact's bytes. A hash never proves authorship, creation time or receipt."""

    VERIFIED_SERVER_HASH = "VERIFIED_SERVER_HASH"  # the server hashed the submitted content itself
    DECLARED_ONLY = "DECLARED_ONLY"                # only the seller's claimed hash exists; the content was never seen
    UNAVAILABLE = "UNAVAILABLE"                    # metadata only: no content and no hash


class ArtifactCreate(BaseModel):
    """Registers delivery evidence. Server-decided fields (hash of content, ingestion time, version, ids) cannot be supplied."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    content_text: str | None = None
    declared_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    asserted_at: AwareDatetime | None = None  # the SELLER's claim about when it happened; never used for ordering
    requirement_keys: list[str] = Field(default_factory=list, max_length=50)
    supersedes_artifact_id: str | None = Field(default=None, max_length=36)

    @field_validator("content_text")
    @classmethod
    def content_within_limit(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf8")) > MAX_ARTIFACT_CONTENT_BYTES:
            raise ValueError(f"content_text exceeds {MAX_ARTIFACT_CONTENT_BYTES} bytes")
        return value

    @field_validator("asserted_at")
    @classmethod
    def not_in_the_future(cls, value):
        from datetime import datetime, timedelta, timezone

        if value is not None and value > datetime.now(timezone.utc) + timedelta(seconds=ASSERTED_TIME_FUTURE_TOLERANCE_SECONDS):
            raise ValueError("asserted_at cannot be in the future")
        return value

    @field_validator("requirement_keys")
    @classmethod
    def keys_well_formed(cls, value: list[str]) -> list[str]:
        import re

        if any(not re.fullmatch(REQUIREMENT_KEY_PATTERN, key) for key in value):
            raise ValueError("invalid requirement key")
        return sorted(set(value))


class ArtifactLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_keys: list[str] = Field(min_length=1, max_length=50)

    _keys = field_validator("requirement_keys")(ArtifactCreate.keys_well_formed.__func__)


class ArtifactView(BaseModel):
    id: str
    project_id: str
    name: str
    version: int
    media_type: str
    description: str
    sha256: str | None
    content_verification: ContentVerification
    size_bytes: int | None
    asserted_at: AwareDatetime | None  # seller-asserted, unverified
    ingested_at: AwareDatetime         # server clock
    supersedes_artifact_id: str | None
    is_latest: bool
    requirement_keys: list[str]


class TimelineItem(BaseModel):
    at: AwareDatetime  # always the server's recording time
    time_basis: Literal["SERVER_RECORDED"] = "SERVER_RECORDED"
    event_type: str
    actor_role: AuditActor
    subject_type: str
    subject_id: str
    summary: str
    asserted_at: AwareDatetime | None = None  # a separate, unverified user-asserted time, when one exists


# --- buyer acknowledgment and authorization (Stage 3C) ---------------------------------------------

class BuyerRequestKind(str, Enum):
    SCOPE_CHANGE_APPROVAL = "SCOPE_CHANGE_APPROVAL"
    DELIVERY_REVIEW = "DELIVERY_REVIEW"


class BuyerDecision(str, Enum):
    """Distinct buyer acts. Acknowledging receipt is not accepting the work, and neither is a payment dispute."""

    APPROVE_CHANGE = "APPROVE_CHANGE"
    REJECT_CHANGE = "REJECT_CHANGE"
    ACKNOWLEDGE_DELIVERY = "ACKNOWLEDGE_DELIVERY"
    ACCEPT_DELIVERY = "ACCEPT_DELIVERY"
    DISPUTE_DELIVERY = "DISPUTE_DELIVERY"


class BuyerRequestStatus(str, Enum):
    PENDING = "PENDING"
    DECIDED = "DECIDED"
    CANCELLED = "CANCELLED"


class BuyerRequestCreate(BaseModel):
    """Asks an e-mail address to decide. The link and the code go to that mailbox only; the seller never receives them."""

    model_config = ConfigDict(extra="forbid")

    kind: BuyerRequestKind
    buyer_email: EmailStr
    change_id: str | None = Field(default=None, max_length=36)
    artifact_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def subject_matches_kind(self) -> "BuyerRequestCreate":
        if self.kind is BuyerRequestKind.SCOPE_CHANGE_APPROVAL and (not self.change_id or self.artifact_ids):
            raise ValueError("a scope change approval needs change_id and no artifact_ids")
        if self.kind is BuyerRequestKind.DELIVERY_REVIEW and (self.change_id or not self.artifact_ids):
            raise ValueError("a delivery review needs artifact_ids and no change_id")
        return self


class BuyerRequestView(BaseModel):
    id: str
    project_id: str
    kind: BuyerRequestKind
    change_id: str | None
    artifact_ids: list[str]
    buyer_email_masked: str
    status: BuyerRequestStatus
    expires_at: AwareDatetime
    created_at: AwareDatetime
    link_opened: bool
    decided_at: AwareDatetime | None


class BuyerChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=200)


class BuyerChallengeAck(BaseModel):
    status: Literal["challenge_sent_if_valid"] = "challenge_sent_if_valid"  # identical for valid and invalid tokens


class BuyerVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=200)
    code: str = Field(pattern=r"^[0-9]{6}$")


class BuyerSessionResponse(BaseModel):
    session_token: str
    expires_at: AwareDatetime
    kind: BuyerRequestKind
    project_title: str
    subject: dict
    allowed_decisions: list[BuyerDecision]


class BuyerDecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_token: str = Field(min_length=20, max_length=200)
    decision: BuyerDecision
    statement: str | None = Field(default=None, max_length=500)


class BuyerDecisionView(BaseModel):
    """Append-only record of a decision made by the holder of a verified e-mail address (identity beyond that is NOT verified)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    request_id: str
    project_id: str
    kind: BuyerRequestKind
    decision: BuyerDecision
    buyer_email_masked: str
    statement: str | None
    verification_method: Literal["EMAIL_OTP"]
    change_id: str | None
    artifact_bindings: list[dict]
    verified_at: AwareDatetime
    decided_at: AwareDatetime


class BuyerDecisionResponse(BaseModel):
    decision: BuyerDecisionView
    scope_applied: bool | None  # None for delivery decisions
    scope_note: str | None = None


# --- AI scope intelligence boundary (Stage 3D) -------------------------------------------------------

class FindingCategory(str, Enum):
    REQUIREMENT_COVERAGE = "REQUIREMENT_COVERAGE"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    CHANGE_AUTHORIZATION = "CHANGE_AUTHORIZATION"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    DELIVERY_ACCEPTANCE = "DELIVERY_ACCEPTANCE"


class FindingStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    AMBIGUOUS = "AMBIGUOUS"


class FindingConfidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FindingBasis(str, Enum):
    RULE_BASED_DEMO = "RULE_BASED_DEMO"
    LLM = "LLM"


MAX_FINDINGS = 200
REFERENCE_PATTERN = r"^(requirement|artifact|change|buyer_decision|scope_record):[A-Za-z0-9-]{1,36}$"


class AnalysisFinding(BaseModel):
    """One judgment. It is an AI/heuristic opinion, never a fact: it must cite stored records and always needs human review."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=40)
    category: FindingCategory
    claim: str = Field(min_length=1, max_length=500)
    status: FindingStatus
    confidence: FindingConfidence
    basis: FindingBasis
    source_refs: list[str] = Field(default_factory=list, max_length=50)
    explanation: str = Field(max_length=2000)
    human_review_required: Literal[True]

    @field_validator("source_refs")
    @classmethod
    def refs_are_typed(cls, value: list[str]) -> list[str]:
        for ref in value:
            if not re.match(REFERENCE_PATTERN, ref):
                raise ValueError("source_refs must look like 'artifact:<id>'")
        return value


class AnalysisRunView(BaseModel):
    id: str
    project_id: str
    baseline_id: str
    baseline_version: int
    provider_name: str
    production_grade: bool
    label: str
    human_review_required: Literal[True] = True
    input_sha256: str
    limitations: list[str]
    findings: list[AnalysisFinding]
    created_at: AwareDatetime


# --- payment observations and merchant ledger (R3) -----------------------------------------------------------------

class ObservationSource(str, Enum):
    PROVIDER_API = "PROVIDER_API"  # written ONLY by the reconciliation engine from what the payment provider returned
    WEBHOOK = "WEBHOOK"  # reserved: no ingestion exists in R3; unauthenticated until the provider confirms
    MERCHANT_IMPORT = "MERCHANT_IMPORT"  # a seller-uploaded merchant-ledger CSV: a CLAIM
    SELLER_ENTRY = "SELLER_ENTRY"  # one claim typed by the seller: a CLAIM


class ObservationKind(str, Enum):
    PAYMENT = "PAYMENT"
    REFUND = "REFUND"
    INVOICE_STATE = "INVOICE_STATE"  # provider-reported invoice lifecycle state (PROVIDER_API only)


class ObservationConfidence(str, Enum):
    PROVIDER_EVIDENCE = "PROVIDER_EVIDENCE"  # what the provider reported to us; the basis of reconciliation
    UNCONFIRMED_WEBHOOK = "UNCONFIRMED_WEBHOOK"  # reserved
    UNVERIFIED_CLAIM = "UNVERIFIED_CLAIM"  # asserted by the merchant/seller; never proof of a PayPal payment


class FindingSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CONFLICT = "CONFLICT"


class FindingCode(str, Enum):
    CLAIM_CORROBORATED = "CLAIM_CORROBORATED"
    CLAIM_AMOUNT_MISMATCH = "CLAIM_AMOUNT_MISMATCH"
    CLAIM_CURRENCY_MISMATCH = "CLAIM_CURRENCY_MISMATCH"
    CLAIM_NOT_IN_PROVIDER_EVIDENCE = "CLAIM_NOT_IN_PROVIDER_EVIDENCE"
    CLAIM_AWAITING_PROVIDER_EVIDENCE = "CLAIM_AWAITING_PROVIDER_EVIDENCE"
    CLAIM_WITHOUT_TRANSACTION_REFERENCE = "CLAIM_WITHOUT_TRANSACTION_REFERENCE"
    CLAIM_TIMESTAMP_IN_FUTURE = "CLAIM_TIMESTAMP_IN_FUTURE"
    DELAYED_OBSERVATION = "DELAYED_OBSERVATION"
    OUT_OF_ORDER_OBSERVATION = "OUT_OF_ORDER_OBSERVATION"
    CONFLICTING_CLAIMS = "CONFLICTING_CLAIMS"
    DUPLICATE_CLAIM = "DUPLICATE_CLAIM"
    PROVIDER_PAYMENT_NOT_IN_LEDGER = "PROVIDER_PAYMENT_NOT_IN_LEDGER"
    PROVIDER_REPORTS_EXTERNAL_PAYMENT = "PROVIDER_REPORTS_EXTERNAL_PAYMENT"
    PROVIDER_STATE_REGRESSION = "PROVIDER_STATE_REGRESSION"
    CLAIMED_PAYMENTS_EXCEED_INVOICE = "CLAIMED_PAYMENTS_EXCEED_INVOICE"
    CLAIMED_REFUNDS_EXCEED_PAYMENTS = "CLAIMED_REFUNDS_EXCEED_PAYMENTS"
    CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER = "CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER"
    CLAIM_MATCHES_EXTERNAL_RECORD = "CLAIM_MATCHES_EXTERNAL_RECORD"
    PROVIDER_TRANSACTION_CHANGED = "PROVIDER_TRANSACTION_CHANGED"
    PROVIDER_EVIDENCE_INCOMPLETE = "PROVIDER_EVIDENCE_INCOMPLETE"
    CLAIM_PROVIDER_EVIDENCE_INCOMPLETE = "CLAIM_PROVIDER_EVIDENCE_INCOMPLETE"
    COMPARISON_INPUT_TRUNCATED = "COMPARISON_INPUT_TRUNCATED"


class PaymentObservationView(BaseModel):
    """One recorded observation. `confidence` says what it is worth: only PROVIDER_EVIDENCE came from the provider."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    payment_id: str
    import_id: str | None
    source: ObservationSource
    kind: ObservationKind
    confidence: ObservationConfidence
    amount_minor: int
    currency: str
    source_event_id: str | None
    source_reference: str | None
    provider_state: str | None
    instrument: str | None
    asserted_at: AwareDatetime | None  # the source's own claimed time (not trusted)
    observed_at: AwareDatetime  # when Caligo recorded it (server clock)
    payload_sha256: str
    provenance: str
    note: str | None
    # Additive. For PROVIDER_API observations: SIMULATED_MOCK_PROVIDER | UNATTESTED_SANDBOX_PROVIDER | AUTHENTICATED_PAYPAL_SANDBOX. None for claims.
    provider_provenance: str | None = None


class SellerObservationCreate(BaseModel):
    """A single claim typed by the seller. The source is always SELLER_ENTRY: it is not a request field."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    kind: ObservationKind
    transaction_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{2,99}$")
    currency: str = Field(pattern=CURRENCY_PATTERN)
    amount_minor: int = Field(ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    asserted_at: AwareDatetime | None = None
    note: str | None = Field(default=None, max_length=200)
    # Optional client token. The same claim_id with the same content is a retry of ONE claim; distinct claim_ids are independent claims
    # even when their contents are identical. Without it, an identical submission is treated as a retry.
    claim_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class SellerObservationResult(BaseModel):
    observation: PaymentObservationView
    created: bool
    note: str = "Recorded as an unverified seller claim. It does not change any payment status or release any hold."


class MerchantLedgerImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv: str = Field(min_length=1, max_length=300_000)


class MerchantLedgerImportView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    file_sha256: str
    row_count: int
    recorded_count: int
    duplicate_count: int
    created_at: AwareDatetime
    replay: bool = False
    note: str = ("Every row is an unverified merchant claim. Importing changes no payment status, verifies no payment "
                 "and releases no payment-instruction hold.")


class ObservationCitation(BaseModel):
    observation_id: str
    source: ObservationSource
    confidence: ObservationConfidence
    payload_sha256: str


class ObservationFinding(BaseModel):
    code: FindingCode
    severity: FindingSeverity
    summary: str
    payment_id: str
    citations: list[ObservationCitation]  # at most MAX_CITATIONS, oldest first; see citations_total
    citations_total: int = 0
    gap_ids: list[str] = []  # provider evidence gaps this finding rests on


class PaymentComparison(BaseModel):
    payment_id: str
    invoice_number: str | None
    authoritative_status: PaymentProviderStatus  # reconciliation's decision; comparison never changes it
    provider_paid_minor: int  # PayPal-instrument payments the provider reported (observations)
    claimed_paid_minor: int  # distinct merchant/seller payment claims (unverified)
    provider_evidence_complete: bool = True  # False: some provider values could not be stored; absence proves nothing
    evidence_gap_count: int = 0
    claims_truncated: bool = False  # True: older claims were not compared, so "missing from the ledger" findings are withheld
    findings: list[ObservationFinding]


class ObservationComparison(BaseModel):
    project_id: str
    authority: str = ("Payment status is decided only by provider reconciliation. Merchant-ledger claims are shown beside provider "
                      "evidence for comparison; they never confirm a payment, change a status or release a hold.")
    payments: list[PaymentComparison]
    total_payments: int = 0
    offset: int = 0
    limit: int = 0
    comparison_sha256: str  # hash of the findings and their citations: a verifiable snapshot for later explanation




# --- recovery cases (R4): derived on read from immutable records; advisory only ----------------------------------------------

class RecoveryCategory(str, Enum):
    LEDGER_MISSING_PROVIDER_PAYMENT = "LEDGER_MISSING_PROVIDER_PAYMENT"  # the provider reported a payment no ledger claim mentions
    CLAIM_NOT_CORROBORATED = "CLAIM_NOT_CORROBORATED"  # a claim the (complete) provider evidence does not contain
    AWAITING_PROVIDER_SYNC = "AWAITING_PROVIDER_SYNC"  # claims exist but the provider has not been synchronized: nothing to compare
    AMOUNT_OR_CURRENCY_MISMATCH = "AMOUNT_OR_CURRENCY_MISMATCH"
    DUPLICATE_REFERENCE = "DUPLICATE_REFERENCE"
    PROVIDER_STATE_REGRESSION = "PROVIDER_STATE_REGRESSION"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"  # gaps or truncation: Caligo cannot say whether a payment is missing
    EXTERNAL_PAYMENT = "EXTERNAL_PAYMENT"  # recorded outside PayPal: not proof of a PayPal payment
    CLAIM_TIMING_ANOMALY = "CLAIM_TIMING_ANOMALY"
    RECONCILIATION_ANOMALY = "RECONCILIATION_ANOMALY"
    INSTRUCTION_INTEGRITY = "INSTRUCTION_INTEGRITY"
    INSTRUCTION_HOLD = "INSTRUCTION_HOLD"
    PENDING_BUYER_AUTHORIZATION = "PENDING_BUYER_AUTHORIZATION"


class RecoveryConfidence(str, Enum):
    PROVIDER_EVIDENCE = "PROVIDER_EVIDENCE"  # rests on provider observations or the reconciliation record
    CLAIM_VS_PROVIDER = "CLAIM_VS_PROVIDER"  # an UNVERIFIED claim set against provider evidence
    UNVERIFIED_CLAIM_ONLY = "UNVERIFIED_CLAIM_ONLY"  # rests only on seller/merchant claims
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"  # the evidence is known to be partial: no conclusion about missing payments
    INSTRUCTION_RECORD = "INSTRUCTION_RECORD"  # rests on the R1/R2 instruction records
    RECONCILIATION_RECORD = "RECONCILIATION_RECORD"  # rests on Caligo's reconciliation notes (what the provider last returned, or failed to)


class CaseCitationKind(str, Enum):
    OBSERVATION = "OBSERVATION"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    PAYMENT_RECORD = "PAYMENT_RECORD"
    PAYMENT_EVENT = "PAYMENT_EVENT"
    INSTRUCTION_VERSION = "INSTRUCTION_VERSION"
    INSTRUCTION_CHANGE = "INSTRUCTION_CHANGE"


class CaseCitation(BaseModel):
    """A pointer to a stored record of the SAME project. `source`/`confidence`/`payload_sha256` are set for observations."""

    kind: CaseCitationKind
    id: str
    source: ObservationSource | None = None
    confidence: ObservationConfidence | None = None
    payload_sha256: str | None = None


PROHIBITED_AUTOMATED_ACTIONS = [
    "Issue, approve or request a refund",
    "Capture, transfer or move funds",
    "Cancel, void, send or modify a PayPal invoice",
    "Mark a payment as paid or verified because of a merchant or seller claim",
    "Release, override or bypass a payment-instruction hold",
    "Edit or delete stored observations, gaps or history",
]


class RecoveryCase(BaseModel):
    """One evidence-cited, advisory recovery case. It is computed from stored records on every read and stored nowhere:
    nothing here changes a payment, a status, a hold or any evidence, and Caligo takes none of the recommended steps itself."""

    case_id: str  # stable for (project, payment, category[, reason]); `basis_sha256` changes when the evidence behind it does
    project_id: str
    payment_id: str | None
    category: RecoveryCategory
    severity: FindingSeverity
    confidence: RecoveryConfidence
    confidence_limits: list[str]
    current_state: dict[str, str | int | bool | None]
    explanation: str
    recommended_next_action: str  # for a human; Caligo performs no action
    citations: list[CaseCitation]
    citations_total: int
    citations_verified: bool  # every citation was re-resolved to a stored record of this project
    derived_from: list[str]  # R3 finding codes / reconciliation anomaly codes / hold reasons this case rests on
    evidence_complete: bool
    advisory_only: Literal[True] = True
    prohibited_automated_actions: list[str] = PROHIBITED_AUTOMATED_ACTIONS
    basis_sha256: str


class RecoveryCaseList(BaseModel):
    project_id: str
    authority: str = ("Recovery cases are advisory. They are derived from stored provider observations, unverified merchant claims, "
                      "reconciliation records and instruction records; they never change a payment, release a hold or promote a claim "
                      "to provider evidence, and Caligo performs none of the recommended steps.")
    cases: list[RecoveryCase]
    total_payments: int
    offset: int
    limit: int
    cases_sha256: str


# --- S2 guarded recovery of the SIMULATED merchant store -------------------------------------------------------------------------

class RecoveryPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["DUPLICATE_ORDERS", "PAID_NOT_APPLIED", "STALE_PAID_AFTER_REFUND", "FALSE_HEALTHY"]
    payment_id: str | None = Field(default=None, max_length=36)  # required except for FALSE_HEALTHY
    credentials_configured: bool = True  # FALSE_HEALTHY only: operator-reported inputs to the health diagnosis
    heartbeat_ok: bool = True


class RecoveryPlanApprove(BaseModel):
    """Approval binds to exactly one plan version and its evidence fingerprint; it can never authorize a changed plan."""
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecoveryStepView(BaseModel):
    step_id: str
    kind: str
    description: str
    params: dict


class RecoveryApprovalView(BaseModel):
    approved_by_seller_id: str
    approved_at: datetime | None = None
    plan_version: int | None
    evidence_fingerprint: str | None


class RecoveryPlanView(BaseModel):
    id: str
    project_id: str
    plan_key: str
    version: int
    latest_version: int
    superseded: bool
    kind: str
    payment_id: str | None
    status: Literal["PROPOSED", "NEEDS_REVIEW", "BLOCKED", "ADVISORY"]
    state: Literal["PROPOSED", "NEEDS_REVIEW", "BLOCKED", "ADVISORY", "APPROVED", "EXECUTED"]
    steps: list[RecoveryStepView]
    evidence: dict  # envelope caligo.recovery_evidence.v1: stable citation refs, provenance, completeness
    reasons: list[dict]
    provider_fingerprint: str
    evidence_fingerprint: str
    created_by_seller_id: str
    created_at: datetime | None = None
    approval: dict | None
    execution: dict | None
    executable: bool


class RecoveryPlanList(BaseModel):
    items: list[RecoveryPlanView]
    total: int
    limit: int
    offset: int


class RecoveryPreviewStep(BaseModel):
    step_id: str
    kind: str
    description: str
    status: Literal["WOULD_APPLY", "ALREADY_DONE", "BLOCKED"]


class RecoveryPreview(BaseModel):
    plan_id: str
    version: int
    state: str
    plan_current: bool
    would_execute: bool
    blockers: list[dict[str, str]]
    steps: list[RecoveryPreviewStep]
    provider_fingerprint: str
    evidence_fingerprint: str
    fresh_evidence_fingerprint: str | None
    guarantees: dict[str, int]


class RecoveryExecutionResult(BaseModel):
    plan: RecoveryPlanView
    executed_now: bool  # false when the plan had already been executed: nothing was changed by this call


class RecoveryEventView(BaseModel):
    id: str
    seq: int
    event_type: str
    actor_seller_id: str
    approved_version: int | None
    approved_fingerprint: str | None
    detail: dict
    created_at: datetime | None = None


class RecoveryEventList(BaseModel):
    plan_id: str
    events: list[RecoveryEventView]


class DivergenceView(BaseModel):
    code: str
    payment_id: str
    order_ids: list[str]
    detail: dict
    suggested_plan_kind: str | None


class DivergenceList(BaseModel):
    project_id: str
    simulated: Literal[True] = True
    divergences: list[DivergenceView]
    integration_health: dict


# --- S4 read-only diagnosis and integration health (advisory) ------------------------------------------------------------------------

class DiagnosisEvidenceView(BaseModel):
    evidence_id: str  # == the S2 citation ref, for example order:<id>; stable
    source: Literal["provider", "merchant", "simulation", "operator", "untrusted_log"]
    kind: str
    entity_id: str
    status: str
    observed_at: datetime
    occurred_at: datetime | None = None
    event_id: str | None = None
    effect_key: str | None = None
    provenance_label: str  # SIMULATED_MOCK_PROVIDER | UNATTESTED_SANDBOX_PROVIDER | AUTHENTICATED_PAYPAL_SANDBOX (runner-attested real Sandbox read only) |SIMULATED_INCOMING_EVENT | SIMULATED_MERCHANT_STATE | CALLER_REPORTED_CLAIM ...


class DiagnosisResponse(BaseModel):
    """Advisory only. It cannot approve, execute or alter anything; recovery guards decide what may happen."""
    case_id: str
    kind: Literal["DUPLICATE_ORDERS", "PAID_NOT_APPLIED", "STALE_PAID_AFTER_REFUND", "FALSE_HEALTHY"]
    payment_id: str | None
    plan_id: str | None
    plan_version: int | None
    evidence_fingerprint: str
    mode: Literal["RULES_ONLY", "RULES_AND_LLM"]
    llm_evaluation: Literal["NOT_RUN", "VALID", "REJECTED"]  # NOT_RUN: no actual model is configured
    llm_rejection: str | None = None
    diagnosis: dict  # S3 Diagnosis (method=rules): facts vs hypotheses, supporting/contradictory ids, limitations, human steps
    llm_diagnosis: dict | None = None
    evidence: list[DiagnosisEvidenceView]
    citations_resolved: Literal[True] = True
    observation_window_complete: bool
    incomplete_evidence_warnings: list[str]
    advisory_only: Literal[True] = True
    human_review_required: Literal[True] = True
    authority: str = ("Diagnosis is advisory text for a human. It never approves, previews, executes or changes a recovery, never contacts a "
                      "provider and never moves funds. Merchant-side facts are simulated; provider facts carry their own provenance label.")


# --- payee destination registry and advisory screening (adapted from the teammate's supplier registry) -----------------------------

class PayeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=120)
    destination: str = Field(min_length=3, max_length=254)  # an e-mail destination; normalised and validated by the service
    note: str | None = Field(default=None, max_length=500)


class PayeeDestinationChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: str = Field(min_length=3, max_length=254)
    note: str | None = Field(default=None, max_length=500)


class PayeeVersionView(BaseModel):
    """One append-only registry version. SELLER_ASSERTED: the seller's own record, never provider or buyer evidence and never a verification."""
    id: str
    payee_id: str
    version: int
    label: str
    destination: str
    destination_sha256: str
    chain_sha256: str
    previous_version_id: str | None
    provenance: Literal["SELLER_ASSERTED"]
    note: str | None
    created_at: datetime


class PayeeView(BaseModel):
    payee_id: str
    current: PayeeVersionView
    history: list[PayeeVersionView]  # newest first
    independently_verified: Literal[False] = False


class PayeeScreenRequest(BaseModel):
    """Read-only screening input. Every field is a claim to be checked, none is trusted."""
    model_config = ConfigDict(extra="forbid")
    payee_id: str | None = Field(default=None, max_length=36)
    destination: str | None = Field(default=None, min_length=3, max_length=254)
    amount_minor: int | None = Field(default=None, ge=1, le=MAX_AMOUNT_MINOR, strict=True)
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)
    text: str | None = Field(default=None, min_length=1, max_length=4096)  # invoice text, e-mail or memo; the text itself is never returned (only literal extracted fields and spans)

    @model_validator(mode="after")
    def something_to_screen(self):
        if not any((self.destination, self.amount_minor, self.text, self.payee_id)):
            raise ValueError("Provide at least one of payee_id, destination, amount_minor or text.")
        return self


class ScreenSpan(BaseModel):
    start: int
    end: int


class ScreenSignal(BaseModel):
    code: Literal["DESTINATION_DIFFERS_FROM_REGISTRY", "DESTINATION_RECENTLY_CHANGED", "DESTINATION_SUPERSEDED", "DESTINATION_NOT_REGISTERED",
                  "DESTINATION_DIFFERS_FROM_INSTRUCTION", "AMOUNT_DIFFERS_FROM_INSTRUCTION", "CURRENCY_DIFFERS_FROM_INSTRUCTION",
                  "URGENCY_LANGUAGE", "BANK_CHANGE_WORDING", "SECRECY_LANGUAGE", "TEXT_MENTIONS_OTHER_DESTINATION"]
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    message: str
    cites: list[str]  # payee:<id>@v<n> / payment_instruction:<id>; every cite resolves inside the project
    spans: list[ScreenSpan]  # character spans in the submitted text, if the signal comes from the text


class ScreenExtractedEmail(BaseModel):
    value: str
    span: ScreenSpan | None


class ScreenExtracted(BaseModel):
    """Literal text matches only; null where the text does not state the field. Never verified."""
    verified: Literal[False] = False
    emails: list[ScreenExtractedEmail]
    amount_minor: int | None
    amount_span: ScreenSpan | None
    currency: str | None
    invoice_reference: str | None
    invoice_reference_span: ScreenSpan | None


class PayeeScreenResponse(BaseModel):
    """Advisory only. There is deliberately no approved/cleared outcome: a screen cannot release a hold or authorize a payment."""
    project_id: str
    outcome: Literal["NO_SIGNALS_FOUND", "REVIEW_RECOMMENDED"]
    signals: list[ScreenSignal]
    extracted: ScreenExtracted | None
    advisory_only: Literal[True] = True
    human_review_required: Literal[True] = True
    authority: str = ("Advisory text for a human. It writes nothing, cannot approve, clear, release or override a payment hold, and does not verify "
                      "a destination: independent verification is the buyer-side second-channel check (R2).")


# --- read-only advisory log intelligence (optional Stanza) ---------------------------------------------------------------------------

class LogLine(BaseModel):
    """One untrusted log line. Ids, scope, provenance and timestamps are assigned by the server, never taken from the caller."""
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4096)


class LogIntelligenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["DUPLICATE_ORDERS", "PAID_NOT_APPLIED", "STALE_PAID_AFTER_REFUND", "FALSE_HEALTHY"]
    payment_id: str | None = Field(default=None, max_length=36)  # required except for FALSE_HEALTHY
    credentials_configured: bool = True  # FALSE_HEALTHY only
    heartbeat_ok: bool = True
    requested_mode: Literal["deterministic", "stanza"] = "deterministic"
    logs: list[LogLine] = Field(min_length=1, max_length=100)


class LogSourceView(BaseModel):
    evidence_ref: str
    log_id: str
    provenance: Literal["untrusted_log"] = "untrusted_log"
    environment: Literal["simulation", "sandbox"]


class LogIntelligenceResponse(BaseModel):
    """Advisory only. Log hypotheses are untrusted language clues, never verified payment facts; nothing here changes any record."""
    schema_version: Literal["1"] = "1"
    requested_mode: Literal["deterministic", "stanza"]
    effective_mode: Literal["deterministic", "stanza"]
    fallback_reason: Literal["model_not_configured", "dependency_missing", "insufficient_memory", "cache_missing", "manifest_missing",
                             "cache_integrity_failed", "version_mismatch", "model_unavailable", "inference_failed"] | None = None
    case_id: str
    kind: Literal["DUPLICATE_ORDERS", "PAID_NOT_APPLIED", "STALE_PAID_AFTER_REFUND", "FALSE_HEALTHY"]
    payment_id: str | None
    evidence_fingerprint: str
    log_lines_analyzed: int
    log_sources: list[LogSourceView]
    evidence: list[DiagnosisEvidenceView]
    report: dict  # log_intelligence Investigation: extraction (clues, UNVERIFIED exact candidates), rules_diagnosis, log_hypotheses; raw text excluded
    observation_window_complete: bool
    incomplete_evidence_warnings: list[str]
    advisory_only: Literal[True] = True
    human_review_required: Literal[True] = True
    authority: str = ("Advisory text for a human. Log-derived hypotheses are untrusted and are not payment facts; exact candidates are unverified. "
                      "Nothing here approves, previews, executes or changes a recovery, contacts a provider or moves funds.")


class IntegrationHealthResponse(BaseModel):
    project_id: str
    environment: Literal["simulation"] = "simulation"
    simulated: Literal[True] = True
    authoritative_policy: str = "app.integration_health (S3.5) deterministic assessor"
    assessment: dict  # S3.5 HealthAssessment: status, capabilities with citations, missing checks, stale/incomplete warnings, no production certification
    summary: dict  # legacy S1/S2 summary derived from the assessment
    caller_reported_capabilities: list[str]  # capabilities that rest on the query parameters, not on anything Caligo observed
    inputs_provenance: str = ("configuration and heartbeat are caller-reported simulation inputs at the assessment time; the transaction proof is a "
                              "provider-PAID payment whose merchant order the safe processor applied, and it expires with the freshness window")
