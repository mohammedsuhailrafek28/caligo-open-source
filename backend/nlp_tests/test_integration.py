import pytest
from types import SimpleNamespace
from app.log_intelligence import LogIntelligenceService, IntegrationResult
from app.log_intelligence.benchmark import batch_for
from app.log_intelligence import integration
from app.diagnosis import DiagnosisInput, Evidence, RulesDiagnoser


def batch(): return batch_for(dict(id='integration',texts=['Payment captured; merchant database failed.']))

class FakeProcessor:
    # Test double only; never used as a measured Stanza result.
    def analyze(self,text): return (('capture',8,16),('fail',36,42))


def test_default_is_deterministic_without_loading(monkeypatch):
    service=LogIntelligenceService()
    def forbidden(): raise AssertionError('Default must not load a model')
    monkeypatch.setattr(service,'_load_processor',forbidden)
    result=service.investigate(batch())
    assert result.requested_mode==result.effective_mode=='deterministic'
    assert result.fallback_reason is None
    assert result.report.rules_diagnosis.method=='rules'


def test_unconfigured_and_missing_dependency(monkeypatch,tmp_path):
    result=LogIntelligenceService().investigate(batch(),requested_mode='stanza')
    assert result.fallback_reason=='model_not_configured'
    service=LogIntelligenceService(tmp_path)
    def missing(): raise ModuleNotFoundError('private detail')
    monkeypatch.setattr(integration,'_available_memory',missing)
    result=service.investigate(batch(),requested_mode='stanza')
    assert result.effective_mode=='deterministic'
    assert result.report.extraction.mode=='deterministic'
    assert result.fallback_reason=='dependency_missing'
    assert 'private detail' not in result.model_dump_json()


@pytest.mark.parametrize('code',['insufficient_memory','cache_missing','manifest_missing','cache_integrity_failed','version_mismatch'])
def test_load_failure_reason_codes(monkeypatch,tmp_path,code):
    from app.log_intelligence.stanza_adapter import StanzaUnavailable
    service=LogIntelligenceService(tmp_path)
    monkeypatch.setattr(integration,'_available_memory',lambda:2*1024**3)
    def fail(): raise StanzaUnavailable(code)
    monkeypatch.setattr(service,'_load_processor',fail)
    result=service.investigate(batch(),requested_mode='stanza')
    assert result.fallback_reason==code
    assert result.effective_mode==result.report.extraction.mode=='deterministic'
    assert all(c.basis=='surface_rule' for c in result.report.extraction.clues)


def test_memory_rechecked_and_lazy_cached_model(monkeypatch,tmp_path):
    service=LogIntelligenceService(tmp_path);calls=[]
    monkeypatch.setattr(integration,'_available_memory',lambda:2*1024**3)
    monkeypatch.setattr(service,'_load_processor',lambda:(calls.append('load') or FakeProcessor()))
    for _ in range(2):
        result=service.investigate(batch(),requested_mode='stanza')
        assert result.effective_mode=='stanza' and result.fallback_reason is None
    assert calls==['load']
    monkeypatch.setattr(integration,'_available_memory',lambda:1)
    result=service.investigate(batch(),requested_mode='stanza')
    assert result.fallback_reason=='insufficient_memory'
    assert service._processor is None
    assert calls==['load']


@pytest.mark.parametrize('error',[MemoryError('private'),RuntimeError('private'),OSError('private')])
def test_unavailable_models_fall_back_without_exposing_errors(monkeypatch,tmp_path,error):
    service=LogIntelligenceService(tmp_path)
    monkeypatch.setattr(integration,'_available_memory',lambda:2*1024**3)
    def fail(): raise error
    monkeypatch.setattr(service,'_load_processor',fail)
    result=service.investigate(batch(),requested_mode='stanza')
    assert result.fallback_reason==('insufficient_memory' if isinstance(error,MemoryError) else 'model_unavailable')
    assert 'private' not in result.model_dump_json()


def test_inference_failure_returns_exact_baseline(monkeypatch,tmp_path):
    service=LogIntelligenceService(tmp_path)
    monkeypatch.setattr(integration,'_available_memory',lambda:2*1024**3)
    class Broken:
        def analyze(self,text): raise RuntimeError('private input')
    monkeypatch.setattr(service,'_load_processor',lambda:Broken())
    expected=service.investigate(batch()).report
    result=service.investigate(batch(),requested_mode='stanza')
    assert result.fallback_reason=='inference_failed'
    assert result.report==expected
    assert service._processor is None


def test_bad_mode_and_unsafe_input_are_not_hidden_by_fallback(monkeypatch,tmp_path):
    service=LogIntelligenceService(tmp_path)
    def forbidden(): raise AssertionError('Bad input must not load a model')
    monkeypatch.setattr(service,'_load_processor',forbidden)
    with pytest.raises(ValueError): service.investigate(batch(),requested_mode='unknown')
    original=batch();bad=original.records[0].model_copy(update={'project_id':'foreign'})
    with pytest.raises(ValueError): service.investigate(original.model_copy(update={'records':(bad,)}),requested_mode='stanza')


def context_and_batch(status='captured'):
    from app.simulation.diagnosis_adapter import DiagnosisContext
    logs=batch()
    provider=Evidence(evidence_id='payment:core',source='provider',kind='payment',entity_id='core',payment_id=logs.payment_id,status=status,observed_at='2026-11-11T00:00:00Z')
    context=DiagnosisContext(DiagnosisInput(case_id='existing-s4-case',evidence=(provider,),incomplete_evidence_warnings=('Existing S4 gap',)),{'payment:core':'SIMULATED_MOCK_PROVIDER'})
    return context,logs


def test_s4_context_keeps_rules_case_id_and_evidence():
    context,logs=context_and_batch('failed')
    result=LogIntelligenceService().for_s4(context,logs,project_id='project1',payment_id='payment1')
    assert result.report.rules_diagnosis==RulesDiagnoser().diagnose(context.data)
    assert result.report.rules_diagnosis.case_id=='existing-s4-case'
    assert not result.report.log_hypotheses
    assert 'Existing S4 gap' in result.report.extraction.warnings
    assert result.advisory_only and result.report.human_review_required


@pytest.mark.parametrize('project,payment',[('foreign','payment1'),('project1','foreign'),('project1',None)])
def test_s4_authorized_scope_rejects_foreign_logs(project,payment):
    context,logs=context_and_batch()
    with pytest.raises(ValueError): LogIntelligenceService().for_s4(context,logs,project_id=project,payment_id=payment,requested_mode='stanza')


def test_s4_does_not_accept_fabricated_authoritative_records():
    context,logs=context_and_batch()
    wrapped=logs.evidence[0].model_copy(update={'evidence':context.data.evidence[0].model_copy(update={'evidence_id':'invented'}),'provenance':'simulation'})
    with pytest.raises(ValueError): LogIntelligenceService().for_s4(context,logs.model_copy(update={'evidence':(*logs.evidence,wrapped)}),project_id='project1',payment_id='payment1')


def test_s4_checks_mutation_scope_and_mock_provenance():
    context,logs=context_and_batch()
    core=logs.evidence[0].model_copy(update={'evidence':context.data.evidence[0],'provenance':'simulation'})
    for evidence,provenance,env in [(context.data.evidence[0].model_copy(update={'status':'failed'}),'simulation','simulation'),(context.data.evidence[0],'provider_authenticated','sandbox')]:
        wrapped=core.model_copy(update={'evidence':evidence,'provenance':provenance,'environment':env})
        with pytest.raises(ValueError): LogIntelligenceService().for_s4(context,logs.model_copy(update={'evidence':(*logs.evidence,wrapped)}),project_id='project1',payment_id='payment1')
    foreign=SimpleNamespace(data=context.data.model_copy(update={'evidence':(context.data.evidence[0].model_copy(update={'payment_id':'foreign'}),)}),provenance_labels=context.provenance_labels)
    with pytest.raises(ValueError): LogIntelligenceService().for_s4(foreign,logs,project_id='project1',payment_id='payment1')
    valid=logs.model_copy(update={'evidence':(*logs.evidence,core)})
    assert LogIntelligenceService().for_s4(context,valid,project_id='project1',payment_id='payment1').effective_mode=='deterministic'


def test_result_rejects_silent_or_inconsistent_mode_labels():
    result=LogIntelligenceService().investigate(batch()).model_dump()
    for update in [dict(effective_mode='stanza'),dict(fallback_reason='cache_missing'),dict(requested_mode='stanza')]:
        with pytest.raises(ValueError): IntegrationResult.model_validate(dict(result,**update))


def test_default_after_opt_in_does_not_use_cached_model(monkeypatch,tmp_path):
    service=LogIntelligenceService(tmp_path)
    class Forbidden:
        def analyze(self,text): raise AssertionError('Default called cached model')
    service._processor=Forbidden()
    result=service.investigate(batch())
    assert result.effective_mode=='deterministic' and result.fallback_reason is None


def test_s4_project_scoped_health_and_schema():
    import json
    from pathlib import Path
    from app.simulation.diagnosis_adapter import DiagnosisContext
    scope='project:project1';logs=batch()
    records=tuple(r.model_copy(update={'payment_id':scope}) for r in logs.records)
    registry=tuple(r.model_copy(update={'payment_id':scope,'evidence':r.evidence.model_copy(update={'payment_id':scope})}) for r in logs.evidence)
    logs=logs.model_copy(update={'payment_id':scope,'records':records,'evidence':registry})
    health=Evidence(evidence_id='health:claim',source='operator',kind='integration',entity_id='health',payment_id=scope,status='healthy',observed_at='2026-11-11T00:00:00Z')
    context=DiagnosisContext(DiagnosisInput(case_id='s4-health',evidence=(health,)),{'health:claim':'CALLER_REPORTED_CLAIM'})
    result=LogIntelligenceService().for_s4(context,logs,project_id='project1',payment_id=None)
    assert result.report.rules_diagnosis==RulesDiagnoser().diagnose(context.data)
    assert result.report.rules_diagnosis.classification=='unproven_integration_health'
    schema=Path(__file__).resolve().parents[2]/'contracts/log-intelligence/integration.schema.json'
    assert json.loads(schema.read_text())==IntegrationResult.model_json_schema()


@pytest.mark.parametrize('label,accepted',[
    ('AUTHENTICATED_PAYPAL_SANDBOX',True),   # runner-attested real Sandbox read
    ('UNATTESTED_SANDBOX_PROVIDER',False),   # adapter output nothing attested: never presented as authenticated
    ('SANDBOX_PROVIDER',False),              # retired label: fails closed
    ('SIMULATED_MOCK_PROVIDER',False),       # a mock is never Sandbox
    ('CALLER_REPORTED_CLAIM',False),
])
def test_s4_sandbox_provenance_follows_the_attested_labels(label,accepted):
    from app.simulation.diagnosis_adapter import DiagnosisContext
    context,logs=context_and_batch()
    context=DiagnosisContext(context.data,{'payment:core':label})
    core=logs.evidence[0].model_copy(update={'evidence':context.data.evidence[0],'provenance':'provider_authenticated','environment':'sandbox'})
    sandbox_logs=logs.model_copy(update={'evidence':(*logs.evidence,core)})
    service=LogIntelligenceService()
    if accepted:
        assert service.for_s4(context,sandbox_logs,project_id='project1',payment_id='payment1').effective_mode=='deterministic'
    else:
        with pytest.raises(ValueError): service.for_s4(context,sandbox_logs,project_id='project1',payment_id='payment1')


def test_the_default_backend_never_imports_the_optional_nlp_stack():
    """Starting the API must not load Stanza, torch, psutil or the log-intelligence package: the optional layer is strictly opt-in."""
    import subprocess, sys
    code = ("import sys, app.main\n"
            "loaded = [m for m in ('stanza', 'torch', 'psutil', 'app.log_intelligence') if m in sys.modules]\n"
            "assert not loaded, loaded")
    result = subprocess.run([sys.executable, '-c', code], cwd=str(__import__('pathlib').Path(__file__).resolve().parents[1]), capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr[-300:]
