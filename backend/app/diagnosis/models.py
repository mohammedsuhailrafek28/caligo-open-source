from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator

Classification = Literal['duplicate_payment_orders', 'capture_order_divergence', 'stale_paid_after_refund', 'unproven_integration_health', 'duplicate_side_effects', 'repeated_recovery', 'inconclusive']
Source = Literal['provider', 'merchant', 'simulation', 'operator', 'untrusted_log']
Kind = Literal['event', 'payment', 'order', 'inventory', 'fulfillment', 'integration', 'recovery', 'log']

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

class Evidence(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    source: Source
    kind: Kind
    entity_id: str = Field(min_length=1, max_length=128)
    payment_id: str = Field(min_length=1, max_length=128)
    status: str = Field(max_length=80)
    observed_at: AwareDatetime
    occurred_at: AwareDatetime | None = None
    event_id: str | None = Field(default=None, max_length=128)
    caused_by_event_id: str | None = Field(default=None, max_length=128)
    effect_key: str | None = Field(default=None, max_length=128)
    text: str = Field(default='', max_length=4000)

class DiagnosisInput(StrictModel):
    schema_version: Literal['1'] = '1'
    case_id: str = Field(min_length=1, max_length=128)
    evidence: tuple[Evidence, ...] = Field(max_length=2000)
    incomplete_evidence_warnings: tuple[str, ...] = Field(default=(), max_length=100)
    observation_window_complete: bool = False

    @model_validator(mode='after')
    def unique_ids(self):
        ids = [e.evidence_id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError('Evidence IDs must be unique; retain duplicate event IDs as separate evidence records')
        return self

class Fact(StrictModel):
    evidence_id: str
    field: Literal['source', 'kind', 'entity_id', 'payment_id', 'status', 'event_id', 'effect_key']
    value: str

class Hypothesis(StrictModel):
    statement: str = Field(min_length=1, max_length=1000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=2000)

class Diagnosis(StrictModel):
    schema_version: Literal['1'] = '1'
    case_id: str
    method: Literal['rules', 'llm']
    classification: Classification
    suspected_root_cause: str = Field(min_length=1, max_length=1000)
    supporting_evidence_ids: tuple[str, ...]
    contradictory_evidence_ids: tuple[str, ...]
    facts: tuple[Fact, ...]
    hypotheses: tuple[Hypothesis, ...]
    confidence_limitations: tuple[str, ...] = Field(min_length=1)
    human_investigation_steps: tuple[str, ...] = Field(min_length=1)
    human_review_required: Literal[True] = True

class Diagnoser(Protocol):
    def diagnose(self, evidence: DiagnosisInput) -> Diagnosis: ...
