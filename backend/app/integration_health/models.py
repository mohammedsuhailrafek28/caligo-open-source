"""Provider-neutral, immutable inputs. Evidence provenance must be authenticated upstream."""
from typing import Annotated, Literal, Protocol
from pydantic import AwareDatetime, Field, model_validator
from app.diagnosis.models import StrictModel, Fact

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')]
WarningText = Annotated[str, Field(min_length=1, max_length=1000)]
Check = Literal['configuration', 'connectivity', 'transaction', 'webhook', 'provider_capture', 'order_completion', 'health_claim']
Environment = Literal['simulation', 'sandbox']

class HealthEvidence(StrictModel):
    evidence_id: Identifier
    project_id: Identifier
    integration_id: Identifier
    environment: Environment
    event_id: Identifier
    check: Check
    outcome: Literal['success', 'failure', 'unknown']
    source: Literal['check_runner', 'provider', 'merchant', 'simulation_runner', 'operator_claim', 'untrusted_log']
    checked_at: AwareDatetime
    received_at: AwareDatetime
    transaction_id: Identifier | None = None
    evidence_refs: tuple[Identifier, ...] = Field(default=(), max_length=20)
    # Text is never returned, logged, executed or used for classification.
    diagnostic_text: str = Field(default='', max_length=4000, exclude=True)

class HealthInput(StrictModel):
    schema_version: Literal['1'] = '1'
    project_id: Identifier
    integration_id: Identifier
    environment: Environment
    as_of: AwareDatetime
    max_age_seconds: int = Field(default=86400, gt=0, le=2592000, strict=True)
    webhook_required: bool = True
    incomplete_evidence_warnings: tuple[WarningText, ...] = Field(default=(), max_length=100)
    evidence: tuple[HealthEvidence, ...] = Field(max_length=2000)

    @model_validator(mode='after')
    def check_integrity(self):
        index = {e.evidence_id: e for e in self.evidence}
        if len(index) != len(self.evidence):
            raise ValueError('Duplicate evidence IDs rejected')
        events = {}
        for e in self.evidence:
            if (e.project_id, e.integration_id, e.environment) != (self.project_id, self.integration_id, self.environment):
                raise ValueError('Cross-scope evidence rejected')
            if e.checked_at > self.as_of or e.received_at > self.as_of or e.received_at < e.checked_at:
                raise ValueError('Invalid or future evidence time rejected')
            if any(ref not in index for ref in e.evidence_refs):
                raise ValueError('Unknown evidence reference rejected')
            signature = e.model_dump(exclude={'evidence_id', 'received_at', 'diagnostic_text'})
            previous = events.setdefault(e.event_id, signature)
            if previous != signature:
                raise ValueError('Conflicting duplicate event ID rejected')
        return self

class Capability(StrictModel):
    name: Literal['configuration_observed', 'provider_connectivity_observed', 'end_to_end_transaction_observed', 'webhook_processing_observed']
    evidence_ids: tuple[Identifier, ...] = Field(min_length=1)

class HealthAssessment(StrictModel):
    schema_version: Literal['1'] = '1'
    project_id: Identifier
    integration_id: Identifier
    environment: Environment
    as_of: AwareDatetime
    status: Literal['UNKNOWN', 'UNVERIFIED', 'DEGRADED', 'TRANSACTION_VERIFIED']
    supported_capabilities: tuple[Capability, ...]
    missing_checks: tuple[str, ...]
    stale_evidence_warnings: tuple[str, ...]
    incomplete_evidence_warnings: tuple[str, ...]
    supporting_evidence_ids: tuple[Identifier, ...]
    contradictory_evidence_ids: tuple[Identifier, ...]
    last_successful_checks: dict[str, AwareDatetime]
    facts: tuple[Fact, ...]
    assumptions: tuple[str, ...]
    suggested_next_diagnostic_action: str
    human_review_required: Literal[True] = True
    production_certification: Literal[False] = False

class HealthAssessor(Protocol):
    def assess(self, evidence: HealthInput) -> HealthAssessment: ...
