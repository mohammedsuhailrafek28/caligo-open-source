import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.log_intelligence import stanza_adapter as adapter

MANIFEST=Path(__file__).resolve().parents[2]/'contracts'/'log-intelligence'/'model-manifest.json'

@pytest.fixture
def fake_model(tmp_path,monkeypatch):
    manifest=json.loads(MANIFEST.read_text())
    for name in manifest['files']:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'dummy')
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(virtual_memory=lambda:SimpleNamespace(available=2*1024**3)))
    monkeypatch.setattr(adapter,'digest',lambda path:manifest['files'][path.relative_to(tmp_path).as_posix()]['sha256'])
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(set_num_threads=lambda n:None))
    none=object()
    def pipeline(*args,**kwargs):
        assert kwargs['download_method'] is none
        assert kwargs['use_gpu'] is False and kwargs['device']=='cpu'
        assert kwargs['package'] is None and kwargs['processors']==adapter.PROCESSORS
        return lambda text:SimpleNamespace(sentences=[SimpleNamespace(words=[SimpleNamespace(lemma='fail',start_char=0,end_char=4)])])
    monkeypatch.setitem(sys.modules,'stanza',SimpleNamespace(__version__='1.15.0',Pipeline=pipeline))
    monkeypatch.setitem(sys.modules,'stanza.pipeline.core',SimpleNamespace(DownloadMethod=SimpleNamespace(NONE=none)))
    return tmp_path

def test_adapter_cpu_offline_and_sanitized_errors(fake_model):
    processor=adapter.StanzaProcessor(fake_model)
    assert processor.analyze('fail')==(('fail',0,4),)
    def fail(text): raise ValueError('do not reveal private text')
    processor.pipeline=fail
    with pytest.raises(RuntimeError) as error: processor.analyze('private')
    assert 'private' not in str(error.value)

def test_model_guards(fake_model,monkeypatch):
    monkeypatch.setattr(adapter,'digest',lambda path:'tampered')
    with pytest.raises(adapter.StanzaUnavailable,match='cache_integrity_failed'): adapter.StanzaProcessor(fake_model)
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(virtual_memory=lambda:SimpleNamespace(available=1)))
    with pytest.raises(adapter.StanzaUnavailable,match='insufficient_memory'): adapter.StanzaProcessor(fake_model)


def test_inference_memory_error_retains_safe_fallback_reason(fake_model):
    processor=adapter.StanzaProcessor(fake_model)
    def exhausted(text): raise MemoryError('private input')
    processor.pipeline=exhausted
    with pytest.raises(adapter.StanzaUnavailable) as error:
        processor.analyze('private')
    assert error.value.code=='insufficient_memory'
    assert str(error.value)=='insufficient_memory'

def test_missing_cache(tmp_path,monkeypatch):
    monkeypatch.setitem(sys.modules,'psutil',SimpleNamespace(virtual_memory=lambda:SimpleNamespace(available=2*1024**3)))
    with pytest.raises(adapter.StanzaUnavailable,match='cache_missing'): adapter.StanzaProcessor(tmp_path)

def test_digest_streaming(tmp_path):
    path=tmp_path/'file';path.write_bytes(b'test')
    assert adapter.digest(path)==hashlib.sha256(b'test').hexdigest()

def test_setup_requires_pinned_hashes(fake_model,monkeypatch,tmp_path):
    from app.log_intelligence import setup_models
    expected=json.loads(MANIFEST.read_text())
    monkeypatch.setattr(setup_models,'manifest_for',lambda root:dict(files={k:dict(sha256=v['sha256'],bytes=v['bytes']) for k,v in expected['files'].items()}))
    fake=sys.modules['stanza'];fake.download=lambda *args,**kwargs:None
    monkeypatch.setattr(sys,'argv',['setup','--download','--cache',str(fake_model),'--manifest-out',str(tmp_path/'manifest.json')])
    setup_models.main()
    assert json.loads((tmp_path/'manifest.json').read_text())['verification']
    monkeypatch.setattr(setup_models,'manifest_for',lambda root:dict(files={'resources.json':dict(sha256='wrong',bytes=1)}))
    with pytest.raises(RuntimeError): setup_models.main()
