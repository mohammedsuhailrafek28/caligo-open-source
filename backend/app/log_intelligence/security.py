"""Input guards run before any model call. Errors never include rejected text."""
import re
from .models import LogRecord, LogBatch

# Conservative rejection, not a claim of universal secret detection.
SENSITIVE = re.compile(r'(?i)(?<![A-Za-z0-9])(?:authorization|bearer|password|passwd|api[_ -]?key|client[_ -]?(?:secret|id)|access[_ -]?token|secret)(?![A-Za-z0-9])|-----BEGIN|\b(?:ghp_|sk_live_|AKIA)[A-Za-z0-9]+|[A-Za-z0-9_+/=-]{40,}')

def safe_text(text):
    if len(text.encode('utf-8',errors='strict')) > 8192:
        raise ValueError('Log body exceeds byte limit')
    if SENSITIVE.search(text) or any(ord(c)<32 and c not in '\n\t\r' or 0x202A<=ord(c)<=0x202E or 0x2066<=ord(c)<=0x2069 for c in text):
        raise ValueError('Sensitive or unsafe log body rejected')
    return text

def parse_record(metadata: dict, body: bytes) -> LogRecord:
    try:
        if not isinstance(body,bytes) or len(body)>8192:
            raise ValueError('Invalid body')
        text=safe_text(body.decode('utf-8',errors='strict'))
        return LogRecord.model_validate(dict(metadata,text=text))
    except (ValueError,TypeError,UnicodeError):
        raise ValueError('Log record rejected by input validation') from None


def validate_batch(batch):
    try:
        if not isinstance(batch,LogBatch):
            raise ValueError('Expected a typed log batch')
        raw=batch.model_dump()
        raw['records']=[dict(r.model_dump(),text=r.text) for r in batch.records]
        validated=LogBatch.model_validate(raw)
        def scan(value):
            if isinstance(value,str): safe_text(value)
            elif isinstance(value,dict):
                for item in value.values(): scan(item)
            elif isinstance(value,(tuple,list)):
                for item in value: scan(item)
        scan(raw)
        return validated
    except (ValueError,TypeError,UnicodeError):
        raise ValueError('Log batch rejected by input validation') from None
