from collections import defaultdict
from .models import DiagnosisInput, Diagnosis, Fact, Hypothesis

CAUSES = {
    'duplicate_payment_orders': 'Possible event deduplication failure created multiple orders.',
    'capture_order_divergence': 'Possible merchant projection failure after provider capture.',
    'stale_paid_after_refund': 'Possible stale paid event delivery after a refund.',
    'unproven_integration_health': 'Integration health claim lacks a successful end-to-end transaction in this evidence window.',
    'duplicate_side_effects': 'Possible duplicate inventory or fulfillment effects.',
    'repeated_recovery': 'Repeated recovery requests require idempotency investigation.',
    'inconclusive': 'Available evidence does not establish a supported failure pattern.',
}

def candidates(data):
    """Only structured non-log records influence rules; text is never interpreted."""
    groups = defaultdict(list)
    for e in data.evidence:
        if e.source != 'untrusted_log' and e.kind != 'log':
            groups[e.payment_id].append(e)
    for records in groups.values():
        events = [e for e in records if e.kind == 'event' and e.event_id]
        for event_id in sorted({e.event_id for e in events}):
            deliveries = [e for e in events if e.event_id == event_id and e.status == 'paid']
            orders = [e for e in records if e.kind == 'order' and e.caused_by_event_id == event_id]
            if len(deliveries) > 1 and len({e.entity_id for e in orders}) > 1:
                yield 'duplicate_payment_orders', deliveries + orders
        captures = [e for e in records if e.kind == 'payment' and e.source == 'provider' and e.status == 'captured']
        # S4: a merchant order still 'pending' after a provider capture is the same unapplied-projection shape as 'failed'.
        failed = [e for e in records if e.kind == 'order' and e.status in ('failed', 'pending')]
        if captures and failed:
            yield 'capture_order_divergence', captures + failed
        refunds = [e for e in records if e.kind == 'event' and e.status == 'refunded' and e.occurred_at]
        for refund in refunds:
            stale = [e for e in events if e.status == 'paid' and e.occurred_at and e.occurred_at < refund.occurred_at and e.observed_at > refund.observed_at]
            if stale:
                yield 'stale_paid_after_refund', [refund] + stale
        # S4: provider evidence says refunded while a merchant order still says paid/fulfilled (no refund event delivery required).
        refunded = [e for e in records if e.kind == 'payment' and e.source == 'provider' and e.status == 'refunded']
        paid_orders = [e for e in records if e.kind == 'order' and e.status in ('paid', 'fulfilled')]
        if refunded and paid_orders:
            yield 'stale_paid_after_refund', refunded + paid_orders
        for kind in ('inventory', 'fulfillment'):
            effects = [e for e in records if e.kind == kind and e.effect_key and e.status == 'applied']
            for key in sorted({e.effect_key for e in effects}):
                repeated = [e for e in effects if e.effect_key == key]
                if len({e.entity_id for e in repeated}) > 1:
                    yield 'duplicate_side_effects', repeated
        recovery = [e for e in records if e.kind == 'recovery' and e.effect_key]
        for key in sorted({e.effect_key for e in recovery}):
            repeated = [e for e in recovery if e.effect_key == key]
            if len({e.entity_id for e in repeated}) > 1:
                yield 'repeated_recovery', repeated
    health = [e for e in data.evidence if e.kind == 'integration' and e.status == 'healthy' and e.source != 'untrusted_log']
    success = any(e.kind == 'integration' and e.status == 'e2e_succeeded' and e.source != 'untrusted_log' for e in data.evidence)
    if health and not success:
        yield 'unproven_integration_health', health

class RulesDiagnoser:
    def diagnose(self, evidence: DiagnosisInput) -> Diagnosis:
        priority = tuple(CAUSES)
        matches = sorted(candidates(evidence), key=lambda match: (priority.index(match[0]), match[1][0].payment_id))
        classification, supporting = matches[0] if matches else ('inconclusive', [])
        ids = tuple(dict.fromkeys(e.evidence_id for e in supporting))
        payments = {e.payment_id for e in supporting}
        # Different reported states for the same scoped entity are counterevidence.
        contradictions = tuple(
            e.evidence_id for e in evidence.evidence
            if e.payment_id in payments and e.evidence_id not in ids
            and any(e.kind == s.kind and e.entity_id == s.entity_id
                    and e.status != s.status for s in supporting)
        )
        limitations = ['Patterns suggest causes; they do not prove causality or authorize recovery.', 'Source classifications are supplied by the caller; snapshots may be stale.']
        limitations.extend(evidence.incomplete_evidence_warnings)
        if not evidence.observation_window_complete:
            limitations.append('Observation window is incomplete; absent events are not proof of failure.')
        if len(matches) > 1:
            limitations.append('Multiple patterns matched; classification uses documented rule priority.')
        return Diagnosis(case_id=evidence.case_id, method='rules', classification=classification,
            suspected_root_cause=CAUSES[classification], supporting_evidence_ids=ids,
            contradictory_evidence_ids=contradictions,
            facts=tuple(Fact(evidence_id=e.evidence_id, field='status', value=e.status) for e in supporting),
            hypotheses=(Hypothesis(statement=CAUSES[classification], evidence_ids=ids),) if ids else (),
            confidence_limitations=tuple(limitations),
            human_investigation_steps=('Inspect source records and correlation IDs in a read-only view.', 'Compare event occurrence and receipt times, outbox acknowledgments, deduplication keys and effect audit records.', 'Confirm evidence completeness with the operator before considering any recovery.'))
