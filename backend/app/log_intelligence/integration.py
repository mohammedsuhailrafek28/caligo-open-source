"""Explicit opt-in facade. No optional NLP dependencies are imported at module load."""
from pathlib import Path
from threading import Lock
from typing import Literal, Mapping, Protocol
from pydantic import model_validator
from app.diagnosis import DiagnosisInput, RulesDiagnoser, validate_diagnosis
from .models import Model, LogBatch, Investigation
from .bridge import investigate
from .security import validate_batch

Mode = Literal['deterministic', 'stanza']
FallbackReason = Literal['model_not_configured', 'dependency_missing', 'insufficient_memory',
    'cache_missing', 'manifest_missing', 'cache_integrity_failed', 'version_mismatch',
    'model_unavailable', 'inference_failed']
MIN_AVAILABLE_BYTES = 1024**3

class IntegrationResult(Model):
    schema_version: Literal['1'] = '1'
    requested_mode: Mode
    effective_mode: Mode
    fallback_reason: FallbackReason | None = None
    report: Investigation
    advisory_only: Literal[True] = True

    @model_validator(mode='after')
    def consistent_modes(self):
        if self.effective_mode != self.report.extraction.mode:
            raise ValueError('Effective mode does not match extraction')
        if self.requested_mode == self.effective_mode:
            if self.fallback_reason is not None:
                raise ValueError('Fallback reason requires a fallback')
        elif not (self.requested_mode == 'stanza' and self.effective_mode == 'deterministic' and self.fallback_reason):
            raise ValueError('Invalid fallback mode labels')
        return self

class S4DiagnosisContext(Protocol):
    data: DiagnosisInput
    provenance_labels: Mapping[str, str]


def _available_memory():
    import psutil  # optional, only on explicit model requests
    return psutil.virtual_memory().available


def _fallback_reason(error, phase):
    if isinstance(error, MemoryError):
        return 'insufficient_memory'
    if isinstance(error, ImportError):
        return 'dependency_missing'
    code = getattr(error, 'code', None)
    if code in {'insufficient_memory', 'cache_missing', 'manifest_missing', 'cache_integrity_failed', 'version_mismatch'}:
        return code
    return 'inference_failed' if phase == 'inference' else 'model_unavailable'

class LogIntelligenceService:
    """One optional local worker instance; no cache load until requested_mode='stanza'."""
    def __init__(self, model_dir: Path | None = None):
        self._model_dir = Path(model_dir) if model_dir is not None else None
        self._processor = None
        self._lock = Lock()

    def _load_processor(self):
        from .stanza_adapter import StanzaProcessor
        return StanzaProcessor(self._model_dir)

    def investigate(self, batch: LogBatch, *, requested_mode: Mode = 'deterministic') -> IntegrationResult:
        if requested_mode not in ('deterministic', 'stanza'):
            raise ValueError('Unknown extraction mode')
        # Entire input is validated and a safe baseline computed before model loading.
        baseline = investigate(batch)
        if requested_mode == 'deterministic':
            return IntegrationResult(requested_mode=requested_mode, effective_mode='deterministic', report=baseline)
        if self._model_dir is None:
            return self._fallback(baseline, 'model_not_configured')
        with self._lock:
            phase = 'load'
            try:
                if _available_memory() < MIN_AVAILABLE_BYTES:
                    self._processor = None
                    return self._fallback(baseline, 'insufficient_memory')
                if self._processor is None:
                    self._processor = self._load_processor()
                phase = 'inference'
                report = investigate(batch, self._processor)
            except Exception as error:
                # No raw exception/input text crosses the integration boundary.
                self._processor = None
                return self._fallback(baseline, _fallback_reason(error, phase))
        return IntegrationResult(requested_mode='stanza', effective_mode='stanza', report=report)

    @staticmethod
    def _fallback(baseline, reason):
        return IntegrationResult(requested_mode='stanza', effective_mode='deterministic',
                                 fallback_reason=reason, report=baseline)

    def for_s4(self, context: S4DiagnosisContext, batch: LogBatch, *, project_id: str,
               payment_id: str | None, requested_mode: Mode = 'deterministic') -> IntegrationResult:
        """Call only after existing S4 authorization and scoped context construction."""
        batch, data = _check_s4_context(context, batch, project_id, payment_id)
        result = self.investigate(batch, requested_mode=requested_mode)
        # Keep S4's case ID, evidence, facts, hypotheses and rules outcome unchanged.
        original = validate_diagnosis(data, RulesDiagnoser().diagnose(data))
        guesses = result.report.log_hypotheses
        opposed = any(e.source == 'provider' and e.kind == 'payment' and e.status in {'failed', 'refunded', 'unknown'} for e in data.evidence)
        if opposed:
            guesses = tuple(h for h in guesses if h.classification != 'capture_order_divergence')
        report = result.report.model_copy(update={'rules_diagnosis': original, 'log_hypotheses': guesses})
        return IntegrationResult.model_validate(dict(result.model_dump(), report=report))


def _check_s4_context(context, batch, project_id, payment_id):
    batch = validate_batch(batch)
    scope = payment_id if payment_id is not None else f'project:{project_id}'
    if (batch.project_id, batch.payment_id) != (project_id, scope):
        raise ValueError('Authorized S4 scope does not match log scope')
    data = DiagnosisInput.model_validate(context.data.model_dump())
    if any(e.payment_id not in {scope, f'project:{project_id}'} for e in data.evidence):
        raise ValueError('Foreign S4 evidence scope rejected')
    known = {e.evidence_id: e for e in data.evidence}
    for wrapped in batch.evidence:
        evidence = wrapped.evidence
        if evidence.evidence_id in known:
            if evidence != known[evidence.evidence_id]:
                raise ValueError('Modified S4 evidence rejected')
            if evidence.source == 'provider':
                expected = {'SIMULATED_MOCK_PROVIDER': ('simulation', 'simulation'),
                            # Only a runner-attested real Sandbox read is "authenticated"; UNATTESTED_SANDBOX_PROVIDER has no mapping and is rejected.
                            'AUTHENTICATED_PAYPAL_SANDBOX': ('provider_authenticated', 'sandbox')}.get(context.provenance_labels.get(evidence.evidence_id))
                if expected != (wrapped.provenance, wrapped.environment):
                    raise ValueError('S4 provider provenance mismatch')
        elif (evidence.source, evidence.kind, evidence.status, wrapped.provenance) != ('untrusted_log', 'log', 'observed', 'untrusted_log'):
            raise ValueError('Additional authoritative evidence rejected')
    warnings = tuple(dict.fromkeys((*data.incomplete_evidence_warnings, *batch.incomplete_evidence_warnings)))
    batch = batch.model_copy(update={'incomplete_evidence_warnings': warnings,
        'observation_window_complete': data.observation_window_complete and batch.observation_window_complete})
    return validate_batch(batch), data
