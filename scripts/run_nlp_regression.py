"""Run all offline backend tests in fresh, sequential memory-safe batches."""
import argparse
import json
import subprocess
import sys
import tempfile
from defusedxml import ElementTree as ET
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--batch-size',type=int,default=6)
    args=parser.parse_args()
    if not 1<=args.batch_size<=10: raise ValueError('Batch size must be between 1 and 10')
    backend=ROOT/'backend'
    files=sorted(p.relative_to(backend).as_posix() for folder in ['tests','diagnosis_tests','health_tests','nlp_tests'] for p in (backend/folder).rglob('test_*.py'))
    rows=[]
    with tempfile.TemporaryDirectory(prefix='caligo-regression-') as temp:
        for start in range(0,len(files),args.batch_size):
            group=files[start:start+args.batch_size];xml=Path(temp)/f'batch-{start}.xml'
            result=subprocess.run([sys.executable,'-m','pytest','-q',*group,'--junitxml='+str(xml)],cwd=backend,check=False)  # fixed argv; no shell or untrusted input
            counts=dict(tests=0,failures=0,errors=0,skipped=0)
            if xml.exists():
                for suite in ET.parse(xml).getroot().iter('testsuite'):
                    for key in counts: counts[key]+=int(suite.attrib.get(key,0))
            rows.append(dict(files=group,exit_code=result.returncode,**counts))
            args.out.write_text(json.dumps(dict(status='RUNNING',batches=rows),indent=2)+'\n',encoding='utf-8')
            if result.returncode: break
    totals={key:sum(r[key] for r in rows) for key in ['tests','failures','errors','skipped']}
    complete=sum(len(r['files']) for r in rows)==len(files)
    passed=complete and all(r['exit_code']==0 for r in rows)
    args.out.write_text(json.dumps(dict(status='PASS' if passed else 'FAIL',all_files_executed=complete,file_count=len(files),totals=totals,batches=rows),indent=2)+'\n',encoding='utf-8')
    return 0 if passed else 1

if __name__=='__main__': raise SystemExit(main())
