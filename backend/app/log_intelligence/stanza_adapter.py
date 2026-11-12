"""Explicit opt-in CPU Stanza. Ordinary execution never downloads resources."""
from pathlib import Path
import hashlib
import json

class StanzaUnavailable(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


PROCESSORS={'tokenize':'ewt_nocharlm','mwt':'ewt','pos':'ewt_nocharlm','lemma':'ewt_nocharlm'}

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

class StanzaProcessor:
    def __init__(self,model_dir: Path):
        import psutil
        if psutil.virtual_memory().available < 1024**3:
            raise StanzaUnavailable('insufficient_memory')
        if not (Path(model_dir)/'resources.json').is_file():
            raise StanzaUnavailable('cache_missing')
        manifest_path=Path(__file__).resolve().parents[3]/'contracts'/'log-intelligence'/'model-manifest.json'
        if not manifest_path.is_file():
            raise StanzaUnavailable('manifest_missing')
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        for name,expected in manifest['files'].items():
            path=Path(model_dir)/name
            if not path.is_file() or digest(path)!=expected['sha256']:
                raise StanzaUnavailable('cache_integrity_failed')
        import torch
        import stanza
        from stanza.pipeline.core import DownloadMethod
        if stanza.__version__!='1.15.0':
            raise StanzaUnavailable('version_mismatch')
        torch.set_num_threads(1)
        self.pipeline=stanza.Pipeline('en',dir=str(model_dir),processors=PROCESSORS,package=None,
            use_gpu=False,device='cpu',download_method=DownloadMethod.NONE,verbose=False,
            tokenize_batch_size=1,pos_batch_size=32,lemma_batch_size=32)

    def analyze(self,text):
        try:
            doc=self.pipeline(text)
            return tuple((w.lemma.lower(),w.start_char,w.end_char) for s in doc.sentences for w in s.words
                         if w.lemma and w.start_char is not None and w.end_char is not None)
        except MemoryError:
            raise StanzaUnavailable('insufficient_memory') from None
        except Exception:
            # Never echo input, model-produced text or library error details.
            raise RuntimeError('Local linguistic inference failed') from None
