import json
from pathlib import Path
from .models import HealthInput, HealthAssessment
from .assessor import assess

NOW = '2026-11-11T12:00:00Z'

def record(evidence_id, check, **updates):
    values = dict(evidence_id=evidence_id, project_id='project1', integration_id='integration1',
        environment='simulation', event_id='event-'+evidence_id, check=check, outcome='success',
        source='simulation_runner', checked_at='2026-11-11T11:00:00Z', received_at='2026-11-11T11:01:00Z')
    values.update(updates)
    return values

def verified_records():
    return [record('config', 'configuration'), record('connect', 'connectivity'),
        record('capture', 'provider_capture', transaction_id='tx1'),
        record('order', 'order_completion', transaction_id='tx1'),
        record('transaction', 'transaction', transaction_id='tx1', evidence_refs=['capture', 'order']),
        record('webhook', 'webhook')]

def example_inputs():
    records = verified_records()
    examples = {
        'configuration_only': records[:1],
        'connectivity_failed_transaction': records[:2] + [record('failed', 'transaction', outcome='failure')],
        'fresh_transaction': records,
        'stale_transaction': [dict(e, checked_at='2026-11-09T11:00:00Z', received_at='2026-11-09T11:01:00Z') for e in records],
        'missing_webhook': records[:-1],
        'contradictory_claim': records[:2] + [record('failed', 'transaction', outcome='failure'), record('claim', 'health_claim', source='operator_claim')],
    }
    return {name: HealthInput(project_id='project1', integration_id='integration1', environment='simulation', as_of=NOW, evidence=items)
            for name, items in examples.items()}

def export(root):
    folder = root / 'contracts' / 'integration-health'
    folder.mkdir(parents=True, exist_ok=True)
    for name, model in [('input', HealthInput), ('output', HealthAssessment)]:
        (folder / (name+'.schema.json')).write_text(json.dumps(model.model_json_schema(), indent=2)+'\n', encoding='utf-8')
    examples = {name: dict(input=data.model_dump(mode='json'), output=assess(data).model_dump(mode='json'))
                for name, data in example_inputs().items()}
    (folder / 'examples.json').write_text(json.dumps(examples, indent=2)+'\n', encoding='utf-8')

if __name__ == '__main__':
    export(Path(__file__).resolve().parents[3])
