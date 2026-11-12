"""Strict, offline parser for merchant-ledger CSV files (R3). Pure: no database, no network.

The file is untrusted input from the seller. Validation is all-or-nothing: if any row is invalid nothing is imported, and the
errors name the row and column so the file can be fixed. Rows are CLAIMS; nothing here (or after it) verifies them."""
import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.schemas import MAX_AMOUNT_MINOR

MAX_BYTES = 200_000
MAX_ROWS = 500
MAX_ERRORS = 50
MAX_FUTURE = timedelta(days=2)
EARLIEST = datetime(2000, 1, 1, tzinfo=timezone.utc)

REQUIRED = ("kind", "invoice_number", "currency", "amount_minor")
OPTIONAL = ("transaction_id", "asserted_at", "note", "merchant_row_id")
KINDS = ("PAYMENT", "REFUND")

_INVOICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TXN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,99}$")
_ROW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_AMOUNT = re.compile(r"^[1-9][0-9]{0,11}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")  # defence in depth: never let a stored cell be a spreadsheet formula


class LedgerFileInvalid(Exception):
    """The file as a whole is unusable. `errors` is a list of {row, column, message}; row 0 means the header or file."""

    def __init__(self, errors: list[dict]) -> None:
        super().__init__("invalid merchant ledger file")
        self.errors = errors[:MAX_ERRORS]


@dataclass(frozen=True)
class LedgerRow:
    row: int  # 1-based data row number in the file
    kind: str
    invoice_number: str
    currency: str
    amount_minor: int
    transaction_id: str | None
    asserted_at: datetime | None
    note: str | None
    merchant_row_id: str | None


def _printable(value: str) -> bool:
    return all(c.isprintable() for c in value)


def check_asserted_range(value: datetime, now: datetime | None = None) -> datetime:
    """UTC-normalise a seller-asserted time; ValueError if it is naive or outside the accepted range (also covers overflow)."""
    now = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timezone is required")
    try:
        utc = value.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("timestamp is out of range") from None
    if utc < EARLIEST or utc > now + MAX_FUTURE:
        raise ValueError("timestamp is out of range")
    return utc


def note_problem(note: str) -> bool:
    return len(note) > 200 or not _printable(note) or note.startswith(_FORMULA_LEAD)


def _parse_time(value: str, now: datetime) -> datetime:
    text = value.strip()
    parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    return check_asserted_range(parsed, now)


def _cell(raw: dict, column: str) -> str:
    value = raw.get(column)
    return "" if value is None else value.strip()


def _validate_row(number: int, raw: dict, now: datetime, errors: list[dict]) -> LedgerRow | None:
    problems: list[tuple[str, str]] = []
    kind, invoice, currency, amount = (_cell(raw, c) for c in REQUIRED)
    txn, asserted, note, row_id = (_cell(raw, c) for c in OPTIONAL)
    if kind not in KINDS:
        problems.append(("kind", "must be PAYMENT or REFUND"))
    if not _INVOICE.fullmatch(invoice):
        problems.append(("invoice_number", "is not a valid invoice number"))
    if not _CURRENCY.fullmatch(currency):
        problems.append(("currency", "must be a 3-letter upper-case code"))
    if not _AMOUNT.fullmatch(amount) or int(amount) > MAX_AMOUNT_MINOR:
        problems.append(("amount_minor", "must be a positive integer number of minor units"))
    if txn and not _TXN.fullmatch(txn):
        problems.append(("transaction_id", "is not a valid transaction reference"))
    if row_id and not _ROW_ID.fullmatch(row_id):
        problems.append(("merchant_row_id", "is not a valid row id"))
    if note and note_problem(note):
        problems.append(("note", "must be at most 200 printable characters and must not start with = + - @"))
    when = None
    if asserted:
        try:
            when = _parse_time(asserted, now)
        except ValueError as exc:
            problems.append(("asserted_at", f"must be an ISO-8601 time with a timezone ({exc})"))
    if problems:
        errors.extend({"row": number, "column": column, "message": message} for column, message in problems)
        return None
    return LedgerRow(number, kind, invoice, currency, int(amount), txn or None, when, note or None, row_id or None)


def parse_ledger(text: str, now: datetime | None = None) -> list[LedgerRow]:
    """Returns the validated rows or raises LedgerFileInvalid. Never returns a partial result."""
    now = now or datetime.now(timezone.utc)
    if len(text.encode("utf8")) > MAX_BYTES:
        raise LedgerFileInvalid([{"row": 0, "column": "", "message": f"file is larger than {MAX_BYTES} bytes"}])
    if "\x00" in text:
        raise LedgerFileInvalid([{"row": 0, "column": "", "message": "file contains NUL bytes"}])
    text = text.removeprefix("﻿")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
    except StopIteration:
        raise LedgerFileInvalid([{"row": 0, "column": "", "message": "file is empty"}]) from None
    except csv.Error as exc:
        raise LedgerFileInvalid([{"row": 0, "column": "", "message": f"malformed CSV: {exc}"}]) from None
    names = [h.strip() for h in header]
    errors: list[dict] = []
    if len(set(names)) != len(names):
        errors.append({"row": 0, "column": "", "message": "duplicate column names"})
    unknown = [n for n in names if n not in REQUIRED + OPTIONAL]
    missing = [c for c in REQUIRED if c not in names]
    if unknown:
        errors.append({"row": 0, "column": ",".join(unknown)[:80], "message": "unknown columns are not accepted"})
    if missing:
        errors.append({"row": 0, "column": ",".join(missing), "message": "required columns are missing"})
    if errors:
        raise LedgerFileInvalid(errors)

    rows: list[LedgerRow] = []
    seen_ids: set[str] = set()
    try:
        for number, values in enumerate(reader, start=1):
            if not values or all(not v.strip() for v in values):
                continue  # blank line
            if number > MAX_ROWS:
                raise LedgerFileInvalid([{"row": number, "column": "", "message": f"more than {MAX_ROWS} rows"}])
            if len(values) != len(names):
                errors.append({"row": number, "column": "", "message": f"expected {len(names)} fields, found {len(values)}"})
                continue
            row = _validate_row(number, dict(zip(names, values)), now, errors)
            if row is None:
                continue
            if row.merchant_row_id:
                if row.merchant_row_id in seen_ids:
                    errors.append({"row": number, "column": "merchant_row_id", "message": "is repeated in this file"})
                    continue
                seen_ids.add(row.merchant_row_id)
            rows.append(row)
    except csv.Error as exc:
        raise LedgerFileInvalid(errors + [{"row": 0, "column": "", "message": f"malformed CSV: {exc}"}]) from None
    if errors:
        raise LedgerFileInvalid(errors)
    if not rows:
        raise LedgerFileInvalid([{"row": 0, "column": "", "message": "file has no data rows"}])
    return rows
