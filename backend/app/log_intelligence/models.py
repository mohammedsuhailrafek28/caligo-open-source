"""Provider-neutral log investigation contracts; raw text is never serialized."""
from typing import Annotated, Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator
from app.diagnosis.models import Evidence, Diagnosis, Classification

ID = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')]
Provenance = Literal['provider_authenticated', 'merchant', 'simulation', 'operator', 'untrusted_log']

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, revalidate_instances='always')

class ScopedEvidence(Model):
    project_id: ID
    payment_id: ID
    environment: Literal['simulation', 'sandbox']
    source_id: ID
    provenance: Provenance
    evidence: Evidence

class LogRecord(Model):
    log_id: ID
    source_id: ID
    project_id: ID
    payment_id: ID
    environment: Literal['simulation', 'sandbox']
    timestamp: AwareDatetime | None = None
    received_at: AwareDatetime
    provenance: Provenance
    evidence_ref: ID
    event_id: ID | None = None
    truncated: bool = False
    text: str = Field(max_length=4096, repr=False, exclude=True)

class LogBatch(Model):
    project_id: ID
    payment_id: ID
    records: tuple[LogRecord, ...] = Field(max_length=100)
    evidence: tuple[ScopedEvidence, ...] = Field(default=(), max_length=2000)
    incomplete_evidence_warnings: tuple[Annotated[str, Field(max_length=1000)], ...] = Field(default=(), max_length=100)
    observation_window_complete: bool = False

    @model_validator(mode='after')
    def scope_and_refs(self):
        index = {r.evidence.evidence_id:r for r in self.evidence}
        if len(index) != len(self.evidence) or len({r.log_id for r in self.records}) != len(self.records):
            raise ValueError('Duplicate evidence or log IDs rejected')
        for r in self.evidence:
            if (r.project_id,r.payment_id) != (self.project_id,self.payment_id) or r.evidence.payment_id != self.payment_id:
                raise ValueError('Cross-scope evidence rejected')
            if r.evidence.text:
                raise ValueError('Registry evidence must exclude raw text')
            if r.provenance == 'provider_authenticated' and (r.environment != 'sandbox' or r.evidence.source != 'provider'):
                raise ValueError('Invalid authenticated-provider provenance')
            if r.evidence.source == 'provider':
                expected = 'simulation' if r.environment == 'simulation' else 'provider_authenticated'
                if r.provenance != expected:
                    raise ValueError('Provider evidence lacks matching provenance')
            if r.provenance == 'simulation' and r.environment != 'simulation':
                raise ValueError('Simulation provenance cannot represent sandbox evidence')
        for r in self.records:
            ref = index.get(r.evidence_ref)
            if not ref or (r.project_id,r.payment_id) != (self.project_id,self.payment_id):
                raise ValueError('Unknown or foreign log citation rejected')
            if (r.source_id,r.provenance,r.environment) != (ref.source_id,ref.provenance,ref.environment):
                raise ValueError('Log provenance does not match scoped registry')
        return self

class Clue(Model):
    log_id: ID
    evidence_ref: ID
    category: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    negated: bool
    basis: Literal['surface_rule', 'stanza_lemma']

class ExactCandidate(Model):
    log_id: ID
    evidence_ref: ID
    kind: Literal['transaction_id', 'order_id', 'amount', 'currency', 'timestamp']
    value: str
    start: int
    end: int
    verified: Literal[False] = False

class Extraction(Model):
    project_id: ID
    payment_id: ID
    mode: Literal['deterministic', 'stanza']
    clues: tuple[Clue, ...]
    exact_candidates: tuple[ExactCandidate, ...]
    contextual_terms: tuple[str, ...]
    warnings: tuple[str, ...]
    provenance: tuple[tuple[ID, ID, Provenance, Literal['simulation', 'sandbox']], ...]
    advisory_only: Literal[True] = True

class LogHypothesis(Model):
    classification: Classification
    evidence_ids: tuple[ID, ...]
    limitation: Literal['Untrusted log-language hypothesis; not transaction evidence.'] = 'Untrusted log-language hypothesis; not transaction evidence.'

class Investigation(Model):
    extraction: Extraction
    rules_diagnosis: Diagnosis
    log_hypotheses: tuple[LogHypothesis, ...]
    human_review_required: Literal[True] = True

class LinguisticProcessor(Protocol):
    def analyze(self, text: str) -> tuple[tuple[str, int, int], ...]: ...
