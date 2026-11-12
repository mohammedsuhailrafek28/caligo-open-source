"""Exact fields remain deterministic and are always unverified text candidates."""
import re
from datetime import datetime
from .models import ExactCandidate

PATTERNS = {
 'transaction_id': re.compile(r'\b(?:transaction_id|txn_id|tx_id)\s*[=:]\s*([A-Za-z0-9][A-Za-z0-9_.:-]{0,95})(?=$|[\s,;)\]\}])',re.I),
 'order_id': re.compile(r'\border_id\s*[=:]\s*([A-Za-z0-9][A-Za-z0-9_.:-]{0,95})(?=$|[\s,;)\]\}])',re.I),
 'currency': re.compile(r'\b(USD|EUR|GBP|INR|JPY|CAD|AUD|CHF)\b'),
 'amount': re.compile(r'\b(?:USD|EUR|GBP|INR|JPY|CAD|AUD|CHF)\s+([0-9]{1,12}(?:\.[0-9]{1,6})?)(?=$|[\s,;)\]\}])'),
 'timestamp': re.compile(r'\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2}))\b'),
}

def exact_candidates(record):
    out=[]
    for kind,pattern in PATTERNS.items():
        for match in pattern.finditer(record.text):
            value=match.group(1)
            if kind=='timestamp':
                try: datetime.fromisoformat(value.replace('Z','+00:00'))
                except ValueError: continue
            out.append(ExactCandidate(log_id=record.log_id,evidence_ref=record.evidence_ref,
                kind=kind,value=value,start=match.start(1),end=match.end(1)))
    return out
