"""Optional real-model smoke test. Requires an explicitly configured, verified cache."""
import os
import socket
from pathlib import Path
import pytest
from app.log_intelligence import extract
from app.log_intelligence.benchmark import batch_for

@pytest.mark.skipif(not os.environ.get('CALIGO_STANZA_TEST_CACHE'),reason='Explicit local Stanza cache not configured')
def test_real_stanza_offline(monkeypatch):
    def denied(*args,**kwargs): raise AssertionError('No downloads or network during model tests')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(socket.socket,'connect_ex',denied)
    from app.log_intelligence.stanza_adapter import StanzaProcessor
    processor=StanzaProcessor(Path(os.environ['CALIGO_STANZA_TEST_CACHE']))
    batch=batch_for(dict(id='model-smoke',texts=['The database workers were failing.']))
    result=extract(batch,processor)
    assert any(c.category=='failure' and c.basis=='stanza_lemma' for c in result.clues)
    from stanza.pipeline.core import DownloadMethod
    assert processor.pipeline.download_method is DownloadMethod.NONE
    assert processor.pipeline.device=='cpu'

@pytest.mark.skipif(not os.environ.get('CALIGO_STANZA_TEST_CACHE'),reason='Explicit local Stanza cache not configured')
def test_real_service_reports_actual_stanza_mode(monkeypatch):
    def denied(*args,**kwargs): raise AssertionError('No network during optional service test')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(socket.socket,'connect_ex',denied)
    from app.log_intelligence import LogIntelligenceService
    service=LogIntelligenceService(Path(os.environ['CALIGO_STANZA_TEST_CACHE']))
    batch=batch_for(dict(id='service-smoke',texts=['The database failed.']))
    assert service._processor is None
    baseline=service.investigate(batch)
    assert baseline.effective_mode=='deterministic' and service._processor is None
    result=service.investigate(batch,requested_mode='stanza')
    assert result.requested_mode==result.effective_mode=='stanza'
    assert result.fallback_reason is None
    assert result.report.extraction.mode=='stanza'
    assert any(c.basis=='stanza_lemma' for c in result.report.extraction.clues)
    assert result.report.rules_diagnosis==baseline.report.rules_diagnosis
