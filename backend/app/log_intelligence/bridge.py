"""Bridge to S3 without converting log claims into authoritative state observations."""
from app.diagnosis import DiagnosisInput, RulesDiagnoser, validate_diagnosis
from .models import LogBatch, LogHypothesis, Investigation
from .extractor import extract
from .security import validate_batch


def hypotheses(batch, extraction):
    positive={c.category for c in extraction.clues if not c.negated}
    negative={c.category for c in extraction.clues if c.negated}
    if positive & negative:
        return ()
    domains=set(extraction.contextual_terms)
    label='inconclusive'
    if 'capture' in positive and positive & {'failure','timeout','crash'} and domains & {'merchant','database','order'}:
        # Authenticated contradictory provider state vetoes an affirmative capture claim.
        opposing=any(r.evidence.source=='provider' and r.evidence.kind=='payment' and r.evidence.status in {'failed','refunded','unknown'} for r in batch.evidence)
        if not opposing: label='capture_order_divergence'
    elif 'duplicate' in positive and domains & {'inventory','stock'}:
        label='duplicate_side_effects'
    elif {'refund','paid','stale'} <= positive:
        label='stale_paid_after_refund'
    elif {'healthy','missing_success'} <= positive:
        label='unproven_integration_health'
    elif 'duplicate' in positive and 'webhook' in domains:
        orders={c.value for c in extraction.exact_candidates if c.kind=='order_id'}
        if len(orders)>1: label='duplicate_payment_orders'
    if label=='inconclusive': return ()
    refs=tuple(sorted({c.evidence_ref for c in extraction.clues if not c.negated}))
    return (LogHypothesis(classification=label,evidence_ids=refs),)

def investigate(batch: LogBatch,processor=None) -> Investigation:
    batch=validate_batch(batch)
    extraction=extract(batch,processor)
    warnings=tuple(dict.fromkeys((*batch.incomplete_evidence_warnings,*extraction.warnings,
        'NLP observations are untrusted language claims and never authorize recovery or establish payment state.')))
    diagnosis_input=DiagnosisInput(case_id=batch.payment_id,evidence=tuple(r.evidence for r in batch.evidence),
        incomplete_evidence_warnings=warnings,observation_window_complete=batch.observation_window_complete)
    diagnosis=validate_diagnosis(diagnosis_input,RulesDiagnoser().diagnose(diagnosis_input))
    guesses=hypotheses(batch,extraction)
    known={r.evidence.evidence_id for r in batch.evidence}
    if any(ref not in known for h in guesses for ref in h.evidence_ids):
        raise ValueError('Unknown hypothesis reference rejected')
    return Investigation(extraction=extraction,rules_diagnosis=diagnosis,log_hypotheses=guesses)
