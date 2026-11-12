"""Offline benchmark runner. Optional actual model is injected by Python caller."""
import json
import hashlib
import platform
from pathlib import Path
from time import perf_counter
from .models import DiagnosisInput
from .rules import RulesDiagnoser
from .llm import LLMDiagnoser, validate_diagnosis

FIXTURES = Path(__file__).resolve().parents[2] / 'diagnosis_benchmark' / 'cases.json'

def evaluate(cases, diagnoser):
    rows = []
    for case in cases:
        data = DiagnosisInput.model_validate(case['input'])
        start = perf_counter()
        try:
            result = validate_diagnosis(data, diagnoser.diagnose(data))
            # Labels assess structured causal classification and supporting citations.
            # Free prose still requires independent human semantic review.
            unsupported = result.classification != 'inconclusive' and (result.classification != case['expected'] or not set(result.supporting_evidence_ids) <= set(case['allowed_support']))
            rows.append(dict(case_id=data.case_id, expected=case['expected'], predicted=result.classification, supporting_evidence_ids=result.supporting_evidence_ids, correct=result.classification == case['expected'], citation_valid=True, unsupported_structured_claim=unsupported, latency_ms=(perf_counter()-start)*1000))
        except (ValueError, TypeError) as exc:
            rows.append(dict(case_id=data.case_id, correct=False, citation_valid=False, unsupported_structured_claim=True, latency_ms=(perf_counter()-start)*1000, error=type(exc).__name__))
    n = len(rows)
    return dict(status='RUN', cases=rows, classification_accuracy=sum(r['correct'] for r in rows)/n if n else None,
        citation_validity=sum(r['citation_valid'] for r in rows)/n if n else None,
        unsupported_structured_claim_frequency=sum(r['unsupported_structured_claim'] for r in rows)/n if n else None,
        semantic_unsupported_claim_frequency=None, semantic_review='NOT RUN',
        mean_latency_ms=sum(r['latency_ms'] for r in rows)/n if n else None)

def run(transport=None):
    cases = json.loads(FIXTURES.read_text(encoding='utf-8'))
    return dict(benchmark_version='1', canonical_fixture_sha256=hashlib.sha256(json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), python_version=platform.python_version(), rules=evaluate(cases, RulesDiagnoser()),
        ai=dict(status='NOT RUN', reason='No actual model configured') if transport is None else dict(model_id=transport.model_id, **evaluate(cases, LLMDiagnoser(transport))))

if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
