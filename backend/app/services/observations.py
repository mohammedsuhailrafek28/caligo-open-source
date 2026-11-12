"""Payment observations (R3): an append-only, provenance-aware record of what each source said about a payment.

Trust rules (see docs/R3_OBSERVATIONS.md):
* PROVIDER_API observations are written ONLY by `record_provider_evidence`, called from the reconciliation engine
  (`reconcile.apply`) with what the payment provider returned. No route can create one; the database ties source to confidence.
* MERCHANT_IMPORT and SELLER_ENTRY observations are CLAIMS (`UNVERIFIED_CLAIM`). The server, never the request, chooses the source
  and the confidence. A claim never changes a payment status, never verifies a payment and never touches a payment-instruction hold:
  this module never imports the reconciliation state machine and uses the instruction service only for its project write lock.
* WEBHOOK is reserved. `record_webhook_observation` exists as the interface a future, authenticated ingestion will use; in R3 it
  refuses, so nothing can be labelled as a webhook observation.
* Observations are never updated or deleted; duplicates are detected by a deterministic key and reported, not hidden."""
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas
from app.errors import ApiError
from app.models import MerchantLedgerImport, PaymentObservation, PaymentRecord, Project, ProviderEvidenceGap, new_id
from app.payment_provider import ProviderInvoice
from app.sandbox_attestation import provider_label
from app.schemas import MAX_AMOUNT_MINOR, AuditActor, ObservationConfidence as Conf, ObservationKind as Kind, ObservationSource as Src
from app.services import instructions
from app.services.ledger_csv import LedgerFileInvalid, LedgerRow, check_asserted_range, note_problem, parse_ledger
from app.services.scope import canonical_sha256, record_audit

log = logging.getLogger("app.audit")

_CURRENCY = re.compile(r"^[A-Z]{3}$")

CONFIDENCE_OF = {
    Src.PROVIDER_API: Conf.PROVIDER_EVIDENCE, Src.WEBHOOK: Conf.UNCONFIRMED_WEBHOOK,
    Src.MERCHANT_IMPORT: Conf.UNVERIFIED_CLAIM, Src.SELLER_ENTRY: Conf.UNVERIFIED_CLAIM,
}
PROVENANCE = {
    Src.PROVIDER_API: ("Reported by the payment provider to Caligo's reconciliation at the observed time. Provider evidence is what "
                       "reconciliation judges; an EXTERNAL instrument is a record made outside PayPal, not proof of a PayPal payment."),
    Src.WEBHOOK: "Reserved. Unauthenticated until the provider confirms it; no webhook ingestion exists in R3.",
    Src.MERCHANT_IMPORT: ("Claimed by the seller in an uploaded merchant-ledger file. Unverified: the file proves nothing about "
                          "PayPal and its timestamps are the seller's own."),
    Src.SELLER_ENTRY: "Typed by the seller as a claim. Unverified; its timestamp is the seller's own.",
}
CLAIM_SOURCES = (Src.MERCHANT_IMPORT, Src.SELLER_ENTRY)
MAX_CLAIMS_PER_PROJECT = 20_000  # stored claims (not provider evidence) per project: bounds storage and later comparison work
MAX_LIST_LIMIT = 200
MAX_LIST_OFFSET = 100_000


class ObservationStorageError(Exception):
    """Provider evidence could not be written to the observation tables (an unexpected integrity failure). The caller's whole
    unit of work must be rolled back: this is NOT a duplicate transaction id and must not be reported as one."""


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def _make(project_id: str, payment: PaymentRecord | str, *, source: Src, kind: Kind, amount: int, currency: str, txn: str | None,
          reference: str | None, state: str | None, instrument: str | None, asserted_at: datetime | None, import_id: str | None,
          note: str | None, dedupe_parts: list) -> PaymentObservation:
    payment_id = payment if isinstance(payment, str) else payment.id
    payload = {"kind": kind.value, "payment_id": payment_id, "transaction_id": txn, "currency": currency, "amount_minor": amount,
               "state": state, "instrument": instrument, "asserted_at": _iso(asserted_at)}
    payload_sha = canonical_sha256(payload)
    return PaymentObservation(
        project_id=project_id, payment_id=payment_id, import_id=import_id, source=source.value, kind=kind.value,
        confidence=CONFIDENCE_OF[source].value, actor="SELLER" if source in CLAIM_SOURCES else "SYSTEM", amount_minor=amount,
        currency=currency, source_event_id=txn, source_reference=reference, provider_state=state, instrument=instrument,
        asserted_at=asserted_at, payload=payload, payload_sha256=payload_sha, provenance=PROVENANCE[source], note=note,
        dedupe_key=canonical_sha256([source.value, payment_id, *dedupe_parts, payload_sha]))


def _existing_keys(db: Session, project_id: str, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    return set(db.scalars(select(PaymentObservation.dedupe_key).where(PaymentObservation.project_id == project_id,
                                                                      PaymentObservation.dedupe_key.in_(keys))))


# --- provider evidence (written by reconciliation only) --------------------------------------------------------------

def _gap_reason(amount: int, currency: str, txn: str | None) -> str | None:
    if not _CURRENCY.fullmatch(currency or ""):
        return "INVALID_CURRENCY"
    if amount < 0:
        return "NEGATIVE_AMOUNT"
    if amount > MAX_AMOUNT_MINOR:
        return "OVERSIZED_AMOUNT"
    if txn and len(txn) > 100:
        return "OVERLONG_TRANSACTION_ID"
    return None


def _gap(rec: PaymentRecord, kind: Kind, reason: str, amount: int, currency: str, txn: str | None) -> ProviderEvidenceGap:
    raw = hashlib.sha256(repr((kind.value, amount, (currency or "")[:16], (txn or "")[:400])).encode("utf8", "replace")).hexdigest()
    return ProviderEvidenceGap(project_id=rec.project_id, payment_id=rec.id, kind=kind.value, reason=reason, raw_sha256=raw,
                               dedupe_key=canonical_sha256([rec.id, kind.value, reason, raw]))


def record_provider_evidence(db: Session, rec: PaymentRecord, ev: ProviderInvoice) -> int:
    """Append what the provider just reported, once. Called by `reconcile.apply` inside its transaction, before it judges the
    evidence, so contradictory readings are kept too. Returns the number of rows added.

    A value that cannot be stored safely is NOT dropped silently: a `ProviderEvidenceGap` is appended (and audited) so every later
    comparison marks this payment's provider evidence as INCOMPLETE. Reconciliation still judges the evidence it was given; nothing
    here changes a status. An unexpected integrity failure raises `ObservationStorageError` (fail closed)."""
    db.flush()  # take the write lock first: the duplicate check below then sees every committed observation
    last_state = db.scalar(select(PaymentObservation).where(
        PaymentObservation.payment_id == rec.id, PaymentObservation.source == Src.PROVIDER_API.value,
        PaymentObservation.kind == Kind.INVOICE_STATE.value).order_by(PaymentObservation.observed_at.desc(), PaymentObservation.id.desc()).limit(1))
    candidates: list[PaymentObservation] = []
    gaps: list[ProviderEvidenceGap] = []
    seen: dict[str, int] = {}
    seen_gaps: dict[str, int] = {}

    def add(kind: Kind, amount: int, currency: str, txn: str | None, state: str | None, instrument: str | None, parts: list) -> None:
        reason = _gap_reason(amount, currency, txn)
        if reason is not None:
            log.error("provider_evidence_gap payment_id=%s kind=%s reason=%s", rec.id, kind.value, reason)
            gap = _gap(rec, kind, reason, amount, currency, txn)
            ordinal = seen_gaps.get(gap.dedupe_key, 0)  # identical unstorable records stay distinct, as for observations
            seen_gaps[gap.dedupe_key] = ordinal + 1
            if ordinal:
                gap.dedupe_key = canonical_sha256([gap.dedupe_key, ordinal])
            gaps.append(gap)
            return
        candidate = _make(rec.project_id, rec, source=Src.PROVIDER_API, kind=kind, amount=amount, currency=currency,
                          txn=txn, reference=rec.invoice_number, state=state, instrument=instrument,
                          asserted_at=None, import_id=None, note=None, dedupe_parts=parts)
        ordinal = seen.get(candidate.payload_sha256, 0)  # identical lines (e.g. two EXTERNAL $50 records) stay distinct
        seen[candidate.payload_sha256] = ordinal + 1
        if ordinal:
            candidate.dedupe_key = canonical_sha256([candidate.dedupe_key, ordinal])
        candidates.append(candidate)

    state_obs_hash = canonical_sha256({"state": ev.state.value, "total": ev.total_minor, "currency": ev.currency})
    last_hash = canonical_sha256({"state": last_state.provider_state, "total": last_state.amount_minor, "currency": last_state.currency}) if last_state else None
    if state_obs_hash != last_hash:  # a state is recorded when it differs from the previous reading, so A -> B -> A is kept as three
        add(Kind.INVOICE_STATE, ev.total_minor, ev.currency, ev.invoice_id, ev.state.value, None, ["state", last_state.id if last_state else None])
    for t in ev.payments:
        add(Kind.PAYMENT, t.amount_minor, t.currency, t.transaction_id, None, t.kind, ["payment"])
    for t in ev.refunds:
        add(Kind.REFUND, t.amount_minor, t.currency, t.transaction_id, None, t.kind, ["refund"])
    known = _existing_keys(db, rec.project_id, [c.dedupe_key for c in candidates])
    fresh = [c for c in candidates if c.dedupe_key not in known]
    unique_gaps = list({g.dedupe_key: g for g in gaps}.values())
    known_gaps = set(db.scalars(select(ProviderEvidenceGap.dedupe_key).where(
        ProviderEvidenceGap.project_id == rec.project_id, ProviderEvidenceGap.dedupe_key.in_([g.dedupe_key for g in unique_gaps])))) if unique_gaps else set()
    fresh_gaps = [g for g in unique_gaps if g.dedupe_key not in known_gaps]
    try:
        db.add_all(fresh)
        db.add_all(fresh_gaps)
        if fresh_gaps:
            record_audit(db, rec.project_id, "PROVIDER_EVIDENCE_INCOMPLETE", AuditActor.SYSTEM, "payment", rec.id,
                         {"gaps": len(fresh_gaps), "reasons": sorted({g.reason for g in fresh_gaps}),
                          "payment_status_changed": False, "hold_changed": False})
        db.flush()
    except IntegrityError as exc:
        raise ObservationStorageError("provider observations could not be stored") from exc
    return len(fresh)


# --- claims -------------------------------------------------------------------------------------------------------------

def _payments_by_invoice(db: Session, project_id: str, numbers: set[str]) -> dict[str, PaymentRecord]:
    rows = db.scalars(select(PaymentRecord).where(PaymentRecord.project_id == project_id, PaymentRecord.invoice_number.in_(numbers)))
    return {r.invoice_number: r for r in rows}


def _claim_count(db: Session, project_id: str) -> int:
    return db.scalar(select(func.count()).select_from(PaymentObservation).where(
        PaymentObservation.project_id == project_id, PaymentObservation.source.in_([x.value for x in CLAIM_SOURCES]))) or 0


def _refuse_over_capacity(db: Session, project_id: str, adding: int) -> None:
    if _claim_count(db, project_id) + adding > MAX_CLAIMS_PER_PROJECT:
        db.rollback()
        raise ApiError(409, "observation_limit_reached", "This project has reached the limit of stored payment claims.")


def _unknown_reference() -> dict:
    # One message for "no such invoice" and "an invoice of another project": the answer must not reveal which.
    return {"message": "invoice_number does not match a payment in this project"}


def _claim_observation(row: LedgerRow, payment: PaymentRecord, source: Src, import_id: str | None) -> PaymentObservation:
    return _make(payment.project_id, payment, source=source, kind=Kind(row.kind), amount=row.amount_minor, currency=row.currency,
                 txn=row.transaction_id, reference=row.merchant_row_id or row.invoice_number, state=None, instrument=None,
                 asserted_at=row.asserted_at, import_id=import_id, note=row.note,
                 dedupe_parts=[row.merchant_row_id] if row.merchant_row_id else [])


def _supported_currency_errors(rows: list[LedgerRow]) -> list[dict]:
    from app.paypal_sandbox import SUPPORTED_CURRENCIES

    return [{"row": r.row, "column": "currency", "message": "is not a supported currency"} for r in rows if r.currency not in SUPPORTED_CURRENCIES]


def import_ledger(db: Session, project: Project, csv_text: str) -> tuple[MerchantLedgerImport, bool]:
    """Import a merchant-ledger CSV as UNVERIFIED claims. Returns (import, created); the same file again returns the first import."""
    file_sha = hashlib.sha256(csv_text.encode("utf8")).hexdigest()
    instructions.lock_project(db, project.id)  # serialises imports (and instruction work) per project
    existing = db.scalar(select(MerchantLedgerImport).where(MerchantLedgerImport.project_id == project.id,
                                                            MerchantLedgerImport.file_sha256 == file_sha))
    if existing is not None:
        db.rollback()
        return existing, False
    try:
        rows = parse_ledger(csv_text)
    except LedgerFileInvalid as exc:
        db.rollback()
        raise ApiError(422, "ledger_invalid", "The merchant ledger file was not imported: nothing was recorded.", details=exc.errors) from None
    errors = _supported_currency_errors(rows)
    payments = _payments_by_invoice(db, project.id, {r.invoice_number for r in rows})
    errors += [{"row": r.row, "column": "invoice_number", **_unknown_reference()} for r in rows if r.invoice_number not in payments]
    if errors:
        db.rollback()
        raise ApiError(422, "ledger_invalid", "The merchant ledger file was not imported: nothing was recorded.", details=errors[:50])
    batch_id = new_id()  # known up front: the import row is append-only, so its counts must be final before it is written
    observations = [_claim_observation(r, payments[r.invoice_number], Src.MERCHANT_IMPORT, batch_id) for r in rows]
    known = _existing_keys(db, project.id, [o.dedupe_key for o in observations])
    fresh: list[PaymentObservation] = []
    for obs in observations:  # duplicates (against earlier imports or inside this file) are counted, never silently merged
        if obs.dedupe_key in known:
            continue
        known.add(obs.dedupe_key)
        fresh.append(obs)
    _refuse_over_capacity(db, project.id, len(fresh))
    batch = MerchantLedgerImport(id=batch_id, project_id=project.id, file_sha256=file_sha, row_count=len(rows),
                                 recorded_count=len(fresh), duplicate_count=len(observations) - len(fresh))
    db.add(batch)
    db.flush()
    db.add_all(fresh)
    db.flush()
    record_audit(db, project.id, "MERCHANT_LEDGER_IMPORTED", AuditActor.SELLER, "ledger_import", batch.id,
                 {"file_sha256": file_sha, "rows": len(rows), "recorded": len(fresh), "duplicates": batch.duplicate_count,
                  "confidence": Conf.UNVERIFIED_CLAIM.value, "payment_status_changed": False, "hold_changed": False})
    db.commit()
    return batch, True


def record_seller_entry(db: Session, project: Project, body: schemas.SellerObservationCreate) -> tuple[PaymentObservation, bool]:
    if body.kind is Kind.INVOICE_STATE:
        raise ApiError(422, "validation_error", "Only PAYMENT and REFUND claims can be entered.")
    from app.paypal_sandbox import SUPPORTED_CURRENCIES

    if body.currency not in SUPPORTED_CURRENCIES:
        raise ApiError(422, "validation_error", "Unsupported currency.")
    note = body.note.strip() if body.note else None
    if note and note_problem(note):
        raise ApiError(422, "validation_error", "note must be at most 200 printable characters and must not start with = + - @")
    asserted = None
    if body.asserted_at is not None:
        try:
            asserted = check_asserted_range(body.asserted_at)
        except ValueError:
            raise ApiError(422, "validation_error", "asserted_at is out of the accepted range.") from None
    instructions.lock_project(db, project.id)
    payments = _payments_by_invoice(db, project.id, {body.invoice_number})
    payment = payments.get(body.invoice_number)
    if payment is None:
        db.rollback()
        raise ApiError(422, "unknown_invoice_reference", **_unknown_reference())
    reference = f"claim:{body.claim_id}" if body.claim_id else None
    row = LedgerRow(0, body.kind.value, body.invoice_number, body.currency, body.amount_minor, body.transaction_id,
                    asserted, note, reference)
    obs = _claim_observation(row, payment, Src.SELLER_ENTRY, None)
    if reference is not None:
        reused = db.scalar(select(PaymentObservation.id).where(
            PaymentObservation.project_id == project.id, PaymentObservation.source == Src.SELLER_ENTRY.value,
            PaymentObservation.source_reference == reference, PaymentObservation.dedupe_key != obs.dedupe_key).limit(1))
        if reused is not None:
            db.rollback()
            raise ApiError(409, "claim_id_conflict", "This claim_id was already used for a claim with different content.")
    duplicate = db.scalar(select(PaymentObservation).where(PaymentObservation.project_id == project.id,
                                                           PaymentObservation.dedupe_key == obs.dedupe_key))
    if duplicate is not None:
        db.rollback()
        return duplicate, False
    _refuse_over_capacity(db, project.id, 1)
    db.add(obs)
    db.flush()
    record_audit(db, project.id, "SELLER_PAYMENT_CLAIM_RECORDED", AuditActor.SELLER, "payment_observation", obs.id,
                 {"payload_sha256": obs.payload_sha256, "confidence": Conf.UNVERIFIED_CLAIM.value,
                  "payment_status_changed": False, "hold_changed": False})
    db.commit()
    return obs, True


# --- future sources -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WebhookObservation:
    """Shape a future, authenticated webhook ingestion will hand to `record_webhook_observation`. Not used in R3."""

    provider_event_id: str
    payment_id: str
    kind: Kind
    amount_minor: int
    currency: str
    asserted_at: datetime | None


def record_webhook_observation(db: Session, observation: WebhookObservation) -> None:
    """Interface only. R3 has no webhook endpoint, no signature verification and no network access, so nothing may be recorded
    with the WEBHOOK source; failing loudly keeps the label from being forged through an internal call."""
    raise NotImplementedError("webhook ingestion is not implemented in R3 (needs authenticated delivery, R5+)")


# --- reads ---------------------------------------------------------------------------------------------------------------

def _page(limit: int, offset: int, maximum: int = MAX_LIST_LIMIT) -> tuple[int, int]:
    return max(1, min(limit, maximum)), max(0, min(offset, MAX_LIST_OFFSET))


def with_provider_provenance(db: Session, rows: list[PaymentObservation]) -> list[schemas.PaymentObservationView]:
    """Views of the rows with `provider_provenance` set for provider evidence (mock / unattested sandbox / authenticated sandbox); None for claims."""
    providers = dict(db.execute(select(PaymentRecord.id, PaymentRecord.provider).where(PaymentRecord.id.in_({r.payment_id for r in rows}))).all()) if rows else {}
    return [schemas.PaymentObservationView.model_validate(r).model_copy(update={
        "provider_provenance": provider_label(providers[r.payment_id], r.payment_id) if r.source == Src.PROVIDER_API.value and r.payment_id in providers else None})
        for r in rows]


def list_observations(db: Session, project_id: str, *, source: Src | None = None, payment_id: str | None = None,
                      limit: int = 100, offset: int = 0) -> tuple[list[PaymentObservation], int]:
    """One bounded page in a stable order (observed_at, id), and the total number of matching rows."""
    limit, offset = _page(limit, offset)
    where = [PaymentObservation.project_id == project_id]
    if source is not None:
        where.append(PaymentObservation.source == source.value)
    if payment_id is not None:
        where.append(PaymentObservation.payment_id == payment_id)
    total = db.scalar(select(func.count()).select_from(PaymentObservation).where(*where)) or 0
    rows = db.scalars(select(PaymentObservation).where(*where).order_by(PaymentObservation.observed_at, PaymentObservation.id)
                      .limit(limit).offset(offset))
    return list(rows), total


def list_imports(db: Session, project_id: str, *, limit: int = 50, offset: int = 0) -> tuple[list[MerchantLedgerImport], int]:
    limit, offset = _page(limit, offset, 100)
    total = db.scalar(select(func.count()).select_from(MerchantLedgerImport).where(MerchantLedgerImport.project_id == project_id)) or 0
    rows = db.scalars(select(MerchantLedgerImport).where(MerchantLedgerImport.project_id == project_id)
                      .order_by(MerchantLedgerImport.created_at, MerchantLedgerImport.id).limit(limit).offset(offset))
    return list(rows), total
