"""Deterministic business-email-compromise (BEC) language signals and safe invoice-text extraction (adapted from the teammate's rules engine).

Pure functions: no I/O, no network, no model. Everything returned is ADVISORY and UNVERIFIED. Extraction never guesses: a field that is not
literally present is None, and every extracted value carries the character span it came from so a human can check it against the text.
Money is integer minor units (a decimal string is parsed with Decimal, never float).
"""
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_TEXT_CHARS = 4096
_UNSAFE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f‪-‮⁦-⁩]")

# (code, pattern). Wording that commonly accompanies altered-destination fraud. Matching is a hint, never a verdict.
_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((code, re.compile(rx, re.IGNORECASE)) for code, rx in (
    ("URGENCY_LANGUAGE", r"\b(?:urgent(?:ly)?|immediately|asap|rush|wire\s+now|today\s+only|right\s+away)\b"),
    ("BANK_CHANGE_WORDING", r"\b(?:new|updated|changed|different)\s+(?:bank(?:ing)?\s+(?:details|account|information)|payment\s+(?:details|instructions)|account\s+(?:number|details)|routing\s+number|paypal\s+(?:account|e-?mail))\b"),
    ("SECRECY_LANGUAGE", r"\b(?:do\s+not|don'?t)\s+(?:call|phone|verify|confirm)\b|\b(?:keep\s+this\s+(?:confidential|between\s+us)|confidential\s+(?:payment|transfer))\b"),
))
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,5}")
_AMOUNT = re.compile(r"(?i)\b(?:amount(?:\s+due)?|total(?:\s+due)?|balance(?:\s+due)?|please\s+pay)\b[\s:=]{0,20}(?P<cur>USD|EUR|GBP|[$€£])?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![.,]?\d)\s{0,20}(?P<cur2>USD|EUR|GBP)?\b")
_INVOICE_REF = re.compile(r"(?i)\binvoice\s*(?:#|no\.?|number|id)\s*[:#]?\s*(?P<ref>[A-Z0-9][A-Z0-9_-]{2,39})\b")
_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}
MAX_EMAILS = 10


class TextRejected(ValueError):
    """The text is oversize or contains control or bidirectional-override characters. The message never includes the text."""


@dataclass(frozen=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True)
class PhraseSignal:
    code: str
    spans: tuple[Span, ...]


@dataclass(frozen=True)
class Extracted:
    emails: tuple[tuple[str, Span], ...]
    amount_minor: int | None
    amount_span: Span | None
    currency: str | None
    invoice_reference: str | None
    invoice_reference_span: Span | None


def check_text(text: str) -> str:
    if len(text) > MAX_TEXT_CHARS:
        raise TextRejected("The text is too long.")
    if _UNSAFE.search(text):
        raise TextRejected("The text contains unsafe characters.")
    return text


def phrase_signals(text: str) -> tuple[PhraseSignal, ...]:
    found = []
    for code, pattern in _PHRASES:
        spans = tuple(Span(m.start(), m.end()) for m in pattern.finditer(text))[:5]
        if spans:
            found.append(PhraseSignal(code, spans))
    return tuple(found)


def _to_minor(number: str) -> int | None:
    try:
        value = (Decimal(number.replace(",", "")) * 100)
    except InvalidOperation:
        return None
    return int(value) if value == value.to_integral_value() and 0 < value <= 2_000_000_000 else None


def extract_fields(text: str) -> Extracted:
    emails, seen = [], set()
    for m in _EMAIL.finditer(text):
        value = m.group(0).lower()
        if value not in seen and len(emails) < MAX_EMAILS:
            seen.add(value)
            emails.append((value, Span(m.start(), m.end())))
    amount = _AMOUNT.search(text)
    minor = currency = span = None
    if amount:
        minor = _to_minor(amount.group("num"))
        raw = amount.group("cur") or amount.group("cur2")
        currency = _SYMBOL.get(raw, raw.upper() if raw else None)
        span = Span(amount.start("num"), amount.end("num")) if minor is not None else None
    ref = _INVOICE_REF.search(text)
    return Extracted(tuple(emails), minor, span, currency if minor is not None else None,
                     ref.group("ref") if ref else None, Span(ref.start("ref"), ref.end("ref")) if ref else None)
