"""Run in a backend-only environment to verify optional packages are not required."""
import argparse
import importlib.metadata
import importlib.util
import json
import socket
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    packages={d.metadata['Name']:d.version for d in importlib.metadata.distributions()}
    absent={name:importlib.util.find_spec(name) is None for name in ('stanza','torch','psutil')}
    assert all(absent.values()), 'Use a separate backend-only environment'
    from app.main import create_app
    from app.config import Settings
    from fastapi.testclient import TestClient
    from app.log_intelligence import LogIntelligenceService,LogBatch
    original=socket.socket.connect
    def connect(sock,address):
        host=address[0] if isinstance(address,tuple) else address
        if host not in ('127.0.0.1','::1','localhost'):
            raise AssertionError('External connections forbidden in core-only probe')
        return original(sock,address)
    socket.socket.connect=connect
    try:
        with tempfile.TemporaryDirectory(prefix='caligo-core-only-') as temp:
            app=create_app(Settings(_env_file=None,app_env='test',database_url='sqlite:///'+str(Path(temp)/'core.sqlite3'),paypal_mode='mock'))
            # Windows asyncio uses a loopback socketpair; external connections stay blocked.
            with TestClient(app) as client: assert client.get('/health').status_code==200
            service=LogIntelligenceService(Path(temp)/'missing-model-cache')
            batch=LogBatch(project_id='core',payment_id='payment',records=())
            default=service.investigate(batch)
            fallback=service.investigate(batch,requested_mode='stanza')
            assert default.effective_mode=='deterministic'
            assert fallback.effective_mode=='deterministic' and fallback.fallback_reason=='dependency_missing'
            assert 'torch' not in sys.modules and 'stanza' not in sys.modules
    finally: socket.socket.connect=original
    args.out.write_text(json.dumps(dict(status='PASS',optional_packages_absent=absent,default_mode=default.effective_mode,
        requested_stanza_effective_mode=fallback.effective_mode,fallback_reason=fallback.fallback_reason,packages=packages),indent=2)+'\n',encoding='utf-8')
    print('PASS: core-only backend and deterministic fallback require no Stanza/PyTorch/psutil')

if __name__=='__main__': main()
