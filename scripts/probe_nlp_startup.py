"""Fresh-process backend/endpoint import probe. No optional NLP loads are allowed."""
import argparse
import gc
import importlib.abc
import json
import socket
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
HEAVY={'stanza','torch'}

class BlockHeavy(importlib.abc.MetaPathFinder):
    def __init__(self): self.attempts=[]
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in HEAVY:
            self.attempts.append(fullname)
            raise AssertionError('Optional model import during ordinary backend execution')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    import psutil  # profiler tool only; not a backend startup requirement
    process=psutil.Process();guard=BlockHeavy();sys.meta_path.insert(0,guard)
    if HEAVY.intersection(sys.modules): raise AssertionError('Probe must start in a clean process')
    initial=process.memory_info().rss
    from app.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient
    imported=process.memory_info().rss
    actual_connect=socket.socket.connect;actual_connect_ex=socket.socket.connect_ex
    def allowed(address):
        host=address[0] if isinstance(address,tuple) else address
        if host not in ('127.0.0.1','::1','localhost'):
            raise AssertionError('External network forbidden during startup probe')
    def connect(sock,address): allowed(address);return actual_connect(sock,address)
    def connect_ex(sock,address): allowed(address);return actual_connect_ex(sock,address)
    socket.socket.connect=connect;socket.socket.connect_ex=connect_ex
    statuses={}
    with tempfile.TemporaryDirectory(prefix='caligo-startup-probe-') as temp:
        settings=Settings(_env_file=None,app_env='test',database_url='sqlite:///'+str(Path(temp)/'probe.sqlite3'),paypal_mode='mock',ai_mode='disabled')
        app=create_app(settings)
        with TestClient(app) as client:
            for path in ['/health','/api/v1/capabilities','/openapi.json']:
                statuses[path]=client.get(path).status_code
            body={'email':'startup-probe@example.com','password':'correct horse battery staple','display_name':'Synthetic startup probe'}
            assert client.post('/api/v1/auth/register',json=body).status_code==201
            login=client.post('/api/v1/auth/login',json={k:body[k] for k in ('email','password')})
            assert login.status_code==200
            project=client.post('/api/v1/projects',json={'title':'Synthetic startup probe'},headers={'X-CSRF-Token':login.json()['csrf_token']})
            assert project.status_code==201
            base='/api/v1/projects/'+project.json()['id']+'/state-recovery'
            statuses['s4_integration_health']=client.get(base+'/integration-health').status_code
            response=client.get(base+'/diagnosis',params={'kind':'FALSE_HEALTHY'})
            statuses['s4_diagnosis']=response.status_code
            assert response.status_code==200 and response.json()['mode']=='RULES_ONLY'
            gc.collect();before_facade=process.memory_info().rss
            from app.log_intelligence import LogIntelligenceService,LogBatch
            service=LogIntelligenceService()
            after_import=process.memory_info().rss
            result=service.investigate(LogBatch(project_id='probe',payment_id='payment',records=()))
            assert result.effective_mode=='deterministic'
            fallback=service.investigate(LogBatch(project_id='probe',payment_id='payment',records=()),requested_mode='stanza')
            assert fallback.effective_mode=='deterministic' and fallback.fallback_reason=='model_not_configured'
            assert client.get(base+'/diagnosis',params={'kind':'FALSE_HEALTHY'}).json()['mode']=='RULES_ONLY'
            gc.collect();after_facade=process.memory_info().rss
        assert not any(name.split('.')[0] in HEAVY for name in sys.modules)
    assert not guard.attempts and all(code==200 for code in statuses.values())
    payload=dict(initial_rss_bytes=initial,backend_import_rss_bytes=imported,warm_endpoint_rss_bytes=before_facade,
        facade_import_rss_bytes=after_import,facade_after_default_request_rss_bytes=after_facade,
        facade_import_delta_bytes=after_import-before_facade,facade_total_delta_bytes=after_facade-before_facade,
        model_import_attempts=guard.attempts,stanza_loaded=False,torch_loaded=False,endpoint_statuses=statuses,
        s4_mode='RULES_ONLY',nlp_default_mode='deterministic',unconfigured_stanza_mode='deterministic',unconfigured_stanza_reason='model_not_configured')
    args.out.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    print('PASS: backend endpoints and default/fallback facade executed without Stanza or PyTorch')

if __name__=='__main__': main()
