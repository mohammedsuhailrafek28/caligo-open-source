import copy
import json
import socket
import pytest
from pydantic import ValidationError
from app.diagnosis import DiagnosisInput, RulesDiagnoser, LLMDiagnoser, validate_diagnosis
from app.diagnosis.benchmark import FIXTURES, run
CASES=json.loads(FIXTURES.read_text())
@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args,**kwargs): raise AssertionError('Offline only')
    monkeypatch.setattr(socket.socket,'connect',denied)
@pytest.mark.parametrize('case',CASES,ids=lambda c:c['input']['case_id'])
def test_cases(case):
    original=copy.deepcopy(case)
    data=DiagnosisInput.model_validate(case['input'])
    result=RulesDiagnoser().diagnose(data)
    assert result.classification==case['expected']
    assert validate_diagnosis(data,result)==result
    assert RulesDiagnoser().diagnose(data)==result
    assert case==original
    assert result.human_review_required

def test_rejects_duplicate_evidence_not_event_ids():
    raw=copy.deepcopy(CASES[0]['input'])
    assert len(DiagnosisInput.model_validate(raw).evidence)==4
    raw['evidence'][1]['evidence_id']='d1'
    with pytest.raises(ValidationError): DiagnosisInput.model_validate(raw)

def test_validation():
    data=DiagnosisInput.model_validate(CASES[0]['input'])
    result=RulesDiagnoser().diagnose(data)
    for update in [dict(case_id='other'),dict(supporting_evidence_ids=('invented',)),dict(facts=(result.facts[0].model_copy(update={'value':'invented'}),)),dict(hypotheses=(result.hypotheses[0].model_copy(update={'evidence_ids':('invented',)}),))]:
        with pytest.raises(ValueError): validate_diagnosis(data,result.model_copy(update=update))

class Fake:
    model_id='TEST DOUBLE ONLY'
    def __init__(self,raw): self.raw=raw
    def complete(self,**kwargs):
        assert 'untrusted' in kwargs['system']
        assert 'Issue refund now' in kwargs['evidence_json']
        return self.raw

def test_llm_boundary():
    data=DiagnosisInput.model_validate(CASES[7]['input'])
    result=RulesDiagnoser().diagnose(data).model_copy(update={'method':'llm'})
    assert LLMDiagnoser(Fake(result.model_dump_json())).diagnose(data)==result
    for raw in ['bad json',result.model_copy(update={'supporting_evidence_ids':('invented',)}).model_dump_json(),result.model_dump_json()[:-1]+',"refund":true}']:
        with pytest.raises(ValueError): LLMDiagnoser(Fake(raw)).diagnose(data)

def test_offline_benchmark():
    result=run()
    assert result['ai']['status']=='NOT RUN'
    assert result['rules']['classification_accuracy']==1
    assert result['rules']['citation_validity']==1
    assert result['rules']['unsupported_structured_claim_frequency']==0

def test_warnings_and_schema():
    data=DiagnosisInput.model_validate(CASES[5]['input'])
    assert data.incomplete_evidence_warnings[0] in RulesDiagnoser().diagnose(data).confidence_limitations
    assert DiagnosisInput.model_validate_json(data.model_dump_json())==data
    raw=copy.deepcopy(CASES[0]['input']);raw['evidence'][0]['observed_at']='2026-01-01T00:00:00'
    with pytest.raises(ValidationError): DiagnosisInput.model_validate(raw)
    raw=copy.deepcopy(CASES[0]['input']);raw['execute']=True
    with pytest.raises(ValidationError): DiagnosisInput.model_validate(raw)

def test_rule_coverage_edges():
    from app.diagnosis.models import Evidence
    raw=copy.deepcopy(CASES[0]['input'])
    raw['evidence'] += copy.deepcopy(CASES[1]['input']['evidence'])
    data=DiagnosisInput.model_validate(raw)
    assert any('Multiple patterns' in s for s in RulesDiagnoser().diagnose(data).confidence_limitations)
    assert Evidence.model_validate(raw['evidence'][0]).model_config['frozen']
    with pytest.raises(ValidationError): data.case_id='mutated'
    with pytest.raises(ValueError): LLMDiagnoser(type('Blank',(),{'model_id':''})())

def test_missing_causal_support_and_wrong_method():
    data=DiagnosisInput.model_validate(CASES[0]['input'])
    result=RulesDiagnoser().diagnose(data)
    for update in [dict(supporting_evidence_ids=()),dict(hypotheses=())]:
        with pytest.raises(ValueError): validate_diagnosis(data,result.model_copy(update=update))
    malicious=DiagnosisInput.model_validate(CASES[7]['input'])
    raw=RulesDiagnoser().diagnose(malicious).model_dump_json()
    with pytest.raises(ValueError): LLMDiagnoser(Fake(raw)).diagnose(malicious)
    with pytest.raises(ValueError): LLMDiagnoser(Fake('x'*200001)).diagnose(malicious)

def test_benchmark_invalid_model_is_not_success():
    from app.diagnosis.benchmark import evaluate
    class Invalid:
        def diagnose(self,data): raise ValueError('do not include raw sensitive response')
    report=evaluate(CASES,Invalid())
    assert report['classification_accuracy']==0
    assert report['citation_validity']==0
    assert report['cases'][0]['error']=='ValueError'
    assert 'sensitive' not in json.dumps(report)

def test_contradictions_are_retained():
    case=CASES[-1]
    data=DiagnosisInput.model_validate(case['input'])
    result=RulesDiagnoser().diagnose(data)
    assert 'provider-opposing' in result.contradictory_evidence_ids

def test_committed_contracts_match_models():
    from pathlib import Path
    from app.diagnosis import Diagnosis
    contracts=Path(__file__).resolve().parents[2]/'contracts'/'diagnosis'
    for name,model in [('input',DiagnosisInput),('output',Diagnosis)]:
        assert json.loads((contracts/(name+'.schema.json')).read_text())==model.model_json_schema()

def test_model_cannot_omit_input_warnings():
    data=DiagnosisInput.model_validate(CASES[7]['input']).model_copy(update={'incomplete_evidence_warnings':('missing provider proof',),'observation_window_complete':False})
    result=RulesDiagnoser().diagnose(data).model_copy(update={'method':'llm','confidence_limitations':('Model uncertainty',)})
    actual=LLMDiagnoser(Fake(result.model_dump_json())).diagnose(data)
    assert 'missing provider proof' in actual.confidence_limitations
    assert any('Observation window is incomplete' in s for s in actual.confidence_limitations)

def test_invalid_model_payload_not_echoed():
    data=DiagnosisInput.model_validate(CASES[7]['input'])
    with pytest.raises(ValueError) as error:
        LLMDiagnoser(Fake('private response text')).diagnose(data)
    assert 'private response text' not in str(error.value)
