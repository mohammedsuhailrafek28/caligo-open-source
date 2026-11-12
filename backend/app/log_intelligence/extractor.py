"""Deterministic cue baseline and optional local linguistic normalization."""
import re
from .models import LogBatch, Extraction, Clue
from .vocabulary import CUES, DOMAINS
from .exact import exact_candidates
from .security import validate_batch

WORDS=re.compile(r"\b[A-Za-z]+(?:'[a-z]+)?\b")

def surface_forms(root):
    forms={root,root+'s',root+'es',root+'ed',root+'ing'}
    if root.endswith('e'): forms.update({root[:-1]+'ed',root[:-1]+'ing'})
    if root.endswith('y'): forms.update({root[:-1]+'ies',root[:-1]+'ied'})
    return forms

SURFACE={form:category for category,roots in CUES.items() for root in roots for form in surface_forms(root)}

def negated(text,start,end):
    before=re.split(r'[.;!?\n]|\bbut\b|\band\b',text[:start],flags=re.I)[-1]
    words=WORDS.findall(before.lower())[-5:]
    sentence=re.split(r'[.;!?\n]',text[:end])[-1].lower()
    return any(w in {'not','no','never','without',"didn't","wasn't","isn't"} for w in words) or bool(re.search(r'\b(if|unless|might|would|could)\b',sentence))

def tokens_for(text,processor):
    if processor is None:
        return tuple((m.group().lower(),m.start(),m.end()) for m in WORDS.finditer(text))
    return processor.analyze(text)

def record_clues(record,processor):
    text=record.text
    tokens=tokens_for(text,processor)
    clues=[]
    for lemma,start,end in tokens:
        if not (0<=start<end<=len(text)):
            raise ValueError('Linguistic processor returned an invalid span')
        category=SURFACE.get(lemma) if processor is None else next((c for c,roots in CUES.items() if lemma in roots),None)
        if category:
            clues.append(Clue(log_id=record.log_id,evidence_ref=record.evidence_ref,category=category,
                start=start,end=end,negated=negated(text,start,end),basis='surface_rule' if processor is None else 'stanza_lemma'))
    for category,pattern in [('timeout',r'\btim(?:e|ed|ing)\s+out\b'),('stale',r'\bout.of.order\b'),
                             ('missing_success',r'\bno\s+(?:successful\s+)?(?:end.to.end|e2e|transaction)\b')]:
        for m in re.finditer(pattern,text,re.I):
            clues.append(Clue(log_id=record.log_id,evidence_ref=record.evidence_ref,category=category,
                start=m.start(),end=m.end(),negated=False if category=='missing_success' else negated(text,m.start(),m.end()),basis='surface_rule'))
    domains={lemma for lemma,_,_ in tokens if lemma in DOMAINS}
    # Only allowlisted domain phrases are returned, never arbitrary model text/entities.
    phrases={a[0]+' '+b[0] for a,b in zip(tokens,tokens[1:]) if a[0] in DOMAINS and b[0] in DOMAINS}
    return clues,domains|phrases

def extract(batch: LogBatch, processor=None) -> Extraction:
    batch=validate_batch(batch)
    clues,exact,terms,warnings=[],[],set(),list(batch.incomplete_evidence_warnings)
    seen={}
    for r in sorted(batch.records,key=lambda r:r.log_id):
        key=(r.source_id,r.event_id) if r.event_id else (r.source_id,r.log_id)
        signature=(r.text,r.timestamp,r.evidence_ref,r.provenance,r.environment,r.truncated)
        if key in seen:
            if seen[key]!=signature: raise ValueError('Conflicting duplicate log event rejected')
            warnings.append('Duplicate event delivery ignored: '+r.log_id)
            continue
        seen[key]=signature
        if r.timestamp is None: warnings.append('Missing occurrence timestamp: '+r.log_id)
        if r.truncated:
            warnings.append('Truncated log excluded from linguistic inference: '+r.log_id)
            continue
        found,context=record_clues(r,processor)
        clues.extend(found);terms.update(context);exact.extend(exact_candidates(r))
    if not batch.observation_window_complete: warnings.append('Log evidence window is incomplete.')
    return Extraction(project_id=batch.project_id,payment_id=batch.payment_id,mode='deterministic' if processor is None else 'stanza',
        clues=tuple(clues),exact_candidates=tuple(exact),contextual_terms=tuple(sorted(terms)),warnings=tuple(warnings),
        provenance=tuple((r.log_id,r.evidence_ref,r.provenance,r.environment) for r in sorted(batch.records,key=lambda r:r.log_id)))
