"""Explicit developer setup only; no runtime or test path invokes this downloader."""
import argparse
import hashlib
import re
import json
from pathlib import Path
from urllib.request import urlopen
from .stanza_adapter import PROCESSORS, digest

FILES=('resources.json','en/tokenize/ewt_nocharlm.pt','en/mwt/ewt.pt','en/pos/ewt_nocharlm.pt','en/lemma/ewt_nocharlm.pt','en/pretrain/conll17.pt')

IMMUTABLE_SOURCE=re.compile(r'^https://raw\.githubusercontent\.com/stanfordnlp/stanza-resources/[0-9a-f]{40}/resources_1\.15\.0\.json$')
MAX_RESOURCES_BYTES=4*1024**2

def pin_resources(cache,expected,fetch=urlopen):
    """Replace resources.json with the exact bytes of the pinned, immutable upstream commit. Upstream `main` moves, so what stanza.download fetched
    from it is not trusted: only bytes whose sha256 and size match the manifest are kept."""
    entry=expected['files']['resources.json']
    if not IMMUTABLE_SOURCE.match(entry['source']):
        raise RuntimeError('resources.json source is not pinned to an immutable commit')
    with fetch(entry['source'],timeout=60) as response:
        data=response.read(MAX_RESOURCES_BYTES+1)
    if len(data)>MAX_RESOURCES_BYTES or len(data)!=entry['bytes'] or hashlib.sha256(data).hexdigest()!=entry['sha256']:
        raise RuntimeError('Pinned resources.json did not match its recorded hash')
    (cache/'resources.json').write_bytes(data)

def manifest_for(root):
    import importlib.metadata
    return dict(stanza_version=importlib.metadata.version('stanza'),resources_version='1.15.0',
        processors=PROCESSORS,device='cpu',files={name:dict(sha256=digest(root/name),bytes=(root/name).stat().st_size) for name in FILES})

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--manifest-out',type=Path,required=True)
    parser.add_argument('--download',action='store_true',required=True)
    args=parser.parse_args()
    import psutil
    if psutil.virtual_memory().available < 1024**3:
        raise RuntimeError('Insufficient free memory for optional model setup')
    import stanza
    if stanza.__version__!='1.15.0': raise RuntimeError('Stanza version mismatch')
    stanza.download('en',model_dir=str(args.cache),processors=PROCESSORS,package=None,resources_version='1.15.0',verbose=False)
    expected_path=Path(__file__).resolve().parents[3]/'contracts'/'log-intelligence'/'model-manifest.json'
    expected=json.loads(expected_path.read_text(encoding='utf-8'))
    pin_resources(args.cache,expected)
    actual=manifest_for(args.cache)
    for name,entry in actual['files'].items():
        if any(entry[k]!=expected['files'][name][k] for k in ('sha256','bytes')):
            raise RuntimeError('Downloaded resources differ from pinned official hashes')
    expected['verification']='All downloaded files matched pinned official hashes'
    expected['provenance']='Official versioned resource metadata and Git LFS pointers; downloaded weights verified against these hashes'
    args.manifest_out.write_text(json.dumps(expected,indent=2)+'\n',encoding='utf-8')

if __name__=='__main__': main()
