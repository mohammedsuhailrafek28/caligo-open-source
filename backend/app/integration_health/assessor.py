"""Pure evidence assessment; no network, clocks, credentials, persistence or recovery."""
from app.diagnosis.models import DiagnosisInput, Evidence, Diagnosis, Fact
from app.diagnosis.llm import validate_diagnosis
from .models import HealthInput, HealthAssessment, Capability

CAPABILITIES = {
    'configuration': 'configuration_observed',
    'connectivity': 'provider_connectivity_observed',
    'transaction': 'end_to_end_transaction_observed',
    'webhook': 'webhook_processing_observed',
}

def authoritative(e):
    if e.source == 'simulation_runner':
        return e.environment == 'simulation' and e.check != 'health_claim'
    allowed = {
        'configuration': {'check_runner'}, 'connectivity': {'check_runner', 'provider'},
        'transaction': {'check_runner'}, 'webhook': {'check_runner', 'merchant'},
        'provider_capture': {'provider'}, 'order_completion': {'merchant'},
    }
    return e.source in allowed.get(e.check, set())

def fresh(data, e):
    return (data.as_of - e.checked_at).total_seconds() <= data.max_age_seconds

def latest(records):
    """Ties favor failure/unknown over success; receipt order cannot overwrite history."""
    severity = {'success': 0, 'unknown': 1, 'failure': 2}
    return max(records, key=lambda e: (e.checked_at, severity[e.outcome], e.evidence_id)) if records else None

def proof_for(data, transaction, records, *, current=True):
    if not transaction.transaction_id:
        return ()
    refs = {e.event_id for e in data.evidence if e.evidence_id in transaction.evidence_refs}
    if not current:
        records = [e for e in records if e.checked_at <= transaction.checked_at]
    proof = []
    for kind in ('provider_capture', 'order_completion'):
        # A newer failed observation for that transaction invalidates old success proof.
        observation = latest([e for e in records if e.check == kind and e.transaction_id == transaction.transaction_id])
        if not observation or observation.event_id not in refs or observation.outcome != 'success':
            return ()
        if (current and not fresh(data, observation)) or observation.checked_at > transaction.checked_at:
            return ()
        proof.append(observation)
    return tuple(proof)

def _assess(data):
    records = sorted((e for e in data.evidence if authoritative(e)), key=lambda e: e.evidence_id)
    # Collapse redelivery by immutable event ID, retaining one stable citation.
    unique = {}
    for e in records:
        unique.setdefault(e.event_id, e)
    records = list(unique.values())
    selected = {kind: latest([e for e in records if e.check == kind]) for kind in CAPABILITIES}
    capabilities, support, missing = [], set(), []
    for kind, name in CAPABILITIES.items():
        record = selected[kind]
        proof = proof_for(data, record, records) if record and kind == 'transaction' else ()
        ok = record and record.outcome == 'success' and fresh(data, record) and (kind != 'transaction' or proof)
        if ok:
            ids = tuple(sorted({record.evidence_id, *(e.evidence_id for e in proof)}))
            capabilities.append(Capability(name=name, evidence_ids=ids))
            support.update(ids)
        elif kind != 'webhook' or data.webhook_required:
            missing.append(kind)
    status, contradictions = classify(data, records, selected, capabilities, missing)
    stale = tuple(f'Stale evidence: {e.evidence_id}' for e in records if not fresh(data, e))
    successes = [e for e in records if e.outcome == 'success'
                 and (e.check != 'transaction' or proof_for(data, e, records, current=False))]
    last = {kind: max(e.checked_at for e in successes if e.check == kind)
            for kind in CAPABILITIES if any(e.check == kind for e in successes)}
    return HealthAssessment(project_id=data.project_id, integration_id=data.integration_id,
        environment=data.environment, as_of=data.as_of, status=status,
        supported_capabilities=tuple(capabilities), missing_checks=tuple(missing),
        stale_evidence_warnings=stale, incomplete_evidence_warnings=data.incomplete_evidence_warnings,
        supporting_evidence_ids=tuple(sorted(support)), contradictory_evidence_ids=contradictions,
        last_successful_checks=last,
        facts=tuple(Fact(evidence_id=e.evidence_id, field='status', value=e.outcome) for e in sorted(data.evidence, key=lambda e:e.evidence_id)),
        assumptions=('Source provenance and project access are authenticated by the exporting caller.',
                     'Supplied clock times and the configured freshness window are appropriate for this integration.',
                     'Observed success is limited to the named environment and supplied checks; it is not production certification.'),
        suggested_next_diagnostic_action=next_action(status, missing))

def classify(data, records, selected, capabilities, missing):
    current = [e for e in selected.values() if e]
    proof_keys = {(e.check, e.transaction_id) for e in records if e.check in ('provider_capture', 'order_completion')}
    current += [latest([e for e in records if (e.check, e.transaction_id) == key]) for key in sorted(proof_keys, key=str)]
    failures = [e for e in current if e.outcome == 'failure' and fresh(data, e)]
    # Conflicting outcomes at the latest check time cannot establish a healthy check.
    conflicts = [e for e in records if e.check in selected and selected[e.check]
                 and e.checked_at == selected[e.check].checked_at and e.outcome != selected[e.check].outcome]
    claims = [e for e in data.evidence if e.check == 'health_claim' and e.outcome == 'success']
    if not records:
        status = 'UNKNOWN'
    elif failures or conflicts or data.incomplete_evidence_warnings:
        status = 'DEGRADED'
    elif not missing:
        status = 'TRANSACTION_VERIFIED'
    elif any(not fresh(data, e) for e in current) or any(c.name == 'end_to_end_transaction_observed' for c in capabilities):
        status = 'DEGRADED'
    else:
        status = 'UNVERIFIED'
    contradictions = {e.evidence_id for e in failures + conflicts}
    if status != 'TRANSACTION_VERIFIED':
        contradictions.update(e.evidence_id for e in claims)
    return status, tuple(sorted(contradictions))

def next_action(status, missing):
    if status == 'TRANSACTION_VERIFIED':
        return 'Review the cited check records and schedule the next offline evidence assessment before expiry.'
    if missing:
        return 'Inspect or obtain authorized evidence for: ' + ', '.join(missing) + '. No check is executed by this module.'
    return 'Inspect contradictory records and completeness warnings with the integration owner.'

def validate_assessment(data: HealthInput, result: HealthAssessment) -> HealthAssessment:
    data = HealthInput.model_validate(data.model_dump())
    result = HealthAssessment.model_validate(result.model_dump())
    if (result.project_id, result.integration_id, result.environment, result.as_of) != (data.project_id, data.integration_id, data.environment, data.as_of):
        raise ValueError('Cross-scope assessment rejected')
    # Reuse S3 citation and exact-fact validation through a detached, read-only projection.
    projected = DiagnosisInput(case_id=data.integration_id, evidence=tuple(
        Evidence(evidence_id=e.evidence_id, source='operator', kind='integration', entity_id=e.event_id,
                 payment_id=data.integration_id, status=e.outcome, observed_at=e.received_at)
        for e in data.evidence))
    references = result.supporting_evidence_ids + tuple(r for c in result.supported_capabilities for r in c.evidence_ids)
    proxy = Diagnosis(case_id=data.integration_id, method='rules', classification='inconclusive',
        suspected_root_cause='Health citation validation only.', supporting_evidence_ids=references,
        contradictory_evidence_ids=result.contradictory_evidence_ids, facts=result.facts, hypotheses=(),
        confidence_limitations=('Projection validates citations, not source authenticity.',),
        human_investigation_steps=('Review source evidence.',))
    validate_diagnosis(projected, proxy)
    if result != _assess(data):
        raise ValueError('Assessment is not supported by deterministic evidence rules')
    return result

def assess(data: HealthInput) -> HealthAssessment:
    # Revalidate even model_construct/model_copy objects at the public boundary.
    data = HealthInput.model_validate(data.model_dump())
    return validate_assessment(data, _assess(data))

class DeterministicHealthAssessor:
    def assess(self, evidence: HealthInput) -> HealthAssessment:
        return assess(evidence)
