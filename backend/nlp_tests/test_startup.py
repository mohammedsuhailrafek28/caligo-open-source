import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[2]

def test_backend_startup_and_existing_endpoints_do_not_import_models(tmp_path):
    if importlib.util.find_spec('psutil') is None:
        pytest.skip('psutil is required only for this separate RSS profiler')
    out=tmp_path/'startup.json'
    result=subprocess.run([sys.executable,str(ROOT/'scripts/probe_nlp_startup.py'),'--out',str(out)],cwd=ROOT,check=False,capture_output=True,text=True)
    assert result.returncode==0, result.stderr
    report=json.loads(out.read_text())
    assert not report['stanza_loaded'] and not report['torch_loaded']
    assert not report['model_import_attempts']
    assert report['s4_mode']=='RULES_ONLY'
    assert all(code==200 for code in report['endpoint_statuses'].values())
