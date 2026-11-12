"""Synthetic held-out benchmark. No model downloads; one arm per process."""
import argparse
import hashlib
import json
import platform
import socket
import statistics
import threading
from pathlib import Path
from time import perf_counter,process_time
from .models import LogBatch, LogRecord, ScopedEvidence
from .bridge import investigate
from .vocabulary import FAILURE_CLUES
from app.diagnosis.models import Evidence

ROOT=Path(__file__).resolve().parents[3]
CASES=ROOT/'backend'/'nlp_benchmark'/'cases.json'

def batch_for(case):
    records=[];registry=[]
    for i,text in enumerate(case['texts']):
        ref=f"log:{case['id']}:{i}"
        ev=Evidence(evidence_id=ref,source='untrusted_log',kind='log',entity_id=f'line{i}',payment_id='payment1',status='observed',observed_at='2026-11-11T00:01:00Z')
        registry.append(ScopedEvidence(project_id='project1',payment_id='payment1',environment='simulation',source_id='fixture',provenance='untrusted_log',evidence=ev))
        records.append(LogRecord(log_id=f'line{i}',source_id='fixture',project_id='project1',payment_id='payment1',environment='simulation',timestamp='2026-11-11T00:00:00Z',received_at='2026-11-11T00:01:00Z',provenance='untrusted_log',evidence_ref=ref,text=text))
    return LogBatch(project_id='project1',payment_id='payment1',records=records,evidence=registry)

def score(cases,processor=None):
    rows=[];tp=fp=fn=unsupported_hypotheses=0
    for case in cases:
        batch=batch_for(case);start=perf_counter();cpu=process_time()
        report=investigate(batch,processor)
        latency=(perf_counter()-start)*1000;cpu_ms=(process_time()-cpu)*1000
        predicted={c.category for c in report.extraction.clues if not c.negated and c.category in FAILURE_CLUES}
        gold=set(case['failure_clues'])
        tp+=len(predicted&gold);fp+=len(predicted-gold);fn+=len(gold-predicted)
        label=report.log_hypotheses[0].classification if report.log_hypotheses else report.rules_diagnosis.classification
        unsupported_hypotheses+=int(label!='inconclusive' and label!=case['root_cause'])
        known={r.evidence.evidence_id for r in batch.evidence}
        refs={c.evidence_ref for c in report.extraction.clues}|{c.evidence_ref for c in report.extraction.exact_candidates}|{r for h in report.log_hypotheses for r in h.evidence_ids}
        valid=refs<=known
        rows.append(dict(id=case['id'],expected=case['root_cause'],predicted=label,clues=sorted(predicted),gold_clues=sorted(gold),citation_valid=valid,latency_ms=latency,cpu_ms=cpu_ms,s3_classification=report.rules_diagnosis.classification))
    n=len(rows);precision=tp/(tp+fp) if tp+fp else 0;recall=tp/(tp+fn) if tp+fn else 0
    return dict(cases=rows,precision=precision,recall=recall,f1=2*precision*recall/(precision+recall) if precision+recall else 0,
        root_cause_accuracy=sum(r['predicted']==r['expected'] for r in rows)/n if n else None,
        s3_structured_only_accuracy=sum(r['s3_classification']==r['expected'] for r in rows)/n if n else None,
        citation_validity=sum(r['citation_valid'] for r in rows)/n if n else None,
        unsupported_clue_count=fp,unsupported_hypothesis_count=unsupported_hypotheses,
        abstention_rate=sum(r['predicted']=='inconclusive' for r in rows)/n if n else None,
        mean_latency_ms=statistics.mean(r['latency_ms'] for r in rows) if rows else None,
        mean_cpu_ms=statistics.mean(r['cpu_ms'] for r in rows) if rows else None)

class MemorySampler:
    def __init__(self):
        import psutil
        self.psutil=psutil;self.process=psutil.Process();self.stop=threading.Event()
        self.peak=self.process.memory_info().rss;self.min_free=psutil.virtual_memory().available
        self.thread=threading.Thread(target=self.sample,daemon=True)
    def sample(self):
        while not self.stop.is_set():
            self.peak=max(self.peak,self.process.memory_info().rss)
            self.min_free=min(self.min_free,self.psutil.virtual_memory().available)
            self.stop.wait(.01)
    def __enter__(self): self.thread.start();return self
    def __exit__(self,*args): self.stop.set();self.thread.join()

def run(mode='deterministic',model_dir=None,split='test'):
    def denied(*args,**kwargs): raise RuntimeError('Network forbidden during benchmark')
    original=socket.socket.connect;original_ex=socket.socket.connect_ex
    socket.socket.connect=denied;socket.socket.connect_ex=denied
    try:
        cases=json.loads(CASES.read_text(encoding='utf-8'));selected=[c for c in cases if c['split']==split]
        with MemorySampler() as memory:
            baseline_rss=memory.process.memory_info().rss;start=perf_counter();processor=None
            if mode=='stanza':
                try:
                    from .stanza_adapter import StanzaProcessor
                    processor=StanzaProcessor(model_dir)
                except (RuntimeError,ImportError,OSError) as exc:
                    from .stanza_adapter import StanzaUnavailable
                    reason=str(exc) if isinstance(exc,StanzaUnavailable) else type(exc).__name__
                    return dict(status='NOT RUN',mode=mode,reason=reason,metrics=None,available_memory_bytes=memory.psutil.virtual_memory().available,required_free_memory_bytes=1024**3,model_weights_downloaded=False)
            load_ms=(perf_counter()-start)*1000;loaded_rss=memory.process.memory_info().rss
            metrics=score(selected,processor)
        return dict(status='RUN',mode=mode,split=split,python=platform.python_version(),canonical_cases_sha256=hashlib.sha256(json.dumps(cases,sort_keys=True,separators=(",",":")).encode()).hexdigest(),
            model_load_ms=load_ms,baseline_rss_bytes=baseline_rss,loaded_rss_bytes=loaded_rss,
            os_peak_working_set_bytes=getattr(memory.process.memory_info(),'peak_wset',None),
            peak_sampled_rss_bytes=max(memory.peak,baseline_rss,loaded_rss),min_available_memory_bytes=memory.min_free,metrics=metrics)
    finally:
        socket.socket.connect=original;socket.socket.connect_ex=original_ex

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['deterministic','stanza'],default='deterministic')
    parser.add_argument('--model-dir',type=Path)
    parser.add_argument('--split',choices=['development','test'],default='test')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();result=run(args.mode,args.model_dir,args.split)
    args.out.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(result['status'])

if __name__=='__main__': main()
