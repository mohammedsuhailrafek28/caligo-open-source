"""Payee destination registry and advisory screening (selectively adapted from the teammate's supplier registry and BEC rules).

Authority boundaries (deliberate):
* Registry entries are SELLER_ASSERTED, project-scoped and append-only; recording or changing one never touches a payment instruction, a hold,
  a payment, an order or a recovery plan, and never counts as verification. Independent verification of a destination stays in R2 (the buyer's
  enrolled second channel); nothing here can approve, release or override a hold.
* `screen` is read-only: it compares a claimed destination/amount/text with the registry and with the instruction in force, and returns cited
  advisory signals. It cannot return "approved" or "cleared".
"""
import hashlib
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import PaymentInstruction, Project, Seller, new_id, utcnow
from app.payee_models import PROVENANCE_SELLER_ASSERTED, PayeeDestinationVersion
from app.schemas import AuditActor
from app.services import bec_signals
from app.services.scope import record_audit

RECENT_CHANGE_DAYS = 7
MAX_PAYEES_PER_PROJECT = 200
MAX_VERSIONS_PER_PAYEE = 100
_DESTINATION = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,5}$")


def normalise_destination(value: str) -> str:
    cleaned = value.strip().lower()
    if len(cleaned) > 254 or not _DESTINATION.fullmatch(cleaned):
        raise ApiError(422, "invalid_destination", "The destination must be a valid e-mail address.")
    return cleaned


def mask(destination: str) -> str:
    local, _, domain = destination.partition("@")
    return f"{local[:1]}***@{domain}"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def _chain(previous: PayeeDestinationVersion | None, payee_id: str, version: int, destination_sha: str) -> str:
    return _sha(f"{previous.chain_sha256 if previous else 'GENESIS'}|{payee_id}|{version}|{destination_sha}")


def versions_of(db: Session, project_id: str, payee_id: str) -> list[PayeeDestinationVersion]:
    return list(db.scalars(select(PayeeDestinationVersion).where(PayeeDestinationVersion.project_id == project_id,
                                                                 PayeeDestinationVersion.payee_id == payee_id)
                           .order_by(PayeeDestinationVersion.version)))


def view(row: PayeeDestinationVersion) -> dict:
    return {"id": row.id, "payee_id": row.payee_id, "version": row.version, "label": row.label, "destination": row.destination,
            "destination_sha256": row.destination_sha256, "chain_sha256": row.chain_sha256, "previous_version_id": row.previous_version_id,
            "provenance": row.provenance, "note": row.note, "created_at": row.created_at}


def _current_rows(db: Session, project_id: str) -> list[PayeeDestinationVersion]:
    latest = (select(PayeeDestinationVersion.payee_id, func.max(PayeeDestinationVersion.version).label("v"))
              .where(PayeeDestinationVersion.project_id == project_id).group_by(PayeeDestinationVersion.payee_id).subquery())
    return list(db.scalars(select(PayeeDestinationVersion).join(
        latest, (PayeeDestinationVersion.payee_id == latest.c.payee_id) & (PayeeDestinationVersion.version == latest.c.v))
        .where(PayeeDestinationVersion.project_id == project_id).order_by(PayeeDestinationVersion.created_at, PayeeDestinationVersion.id)))


def list_payees(db: Session, project: Project) -> list[dict]:
    return [view(r) for r in _current_rows(db, project.id)]


def get_payee(db: Session, project: Project, payee_id: str) -> dict:
    rows = versions_of(db, project.id, payee_id)
    if not rows:
        raise ApiError(404, "not_found", "Payee not found.")
    return {"payee_id": payee_id, "current": view(rows[-1]), "history": [view(r) for r in reversed(rows)]}


def _audit(db: Session, project: Project, event: str, row: PayeeDestinationVersion, extra: dict) -> None:
    record_audit(db, project.id, event, AuditActor.SELLER, "payee", row.payee_id,
                 {"version": row.version, "label": row.label, "destination": mask(row.destination), "destination_sha256": row.destination_sha256,
                  "provenance": PROVENANCE_SELLER_ASSERTED, **extra})


def register_payee(db: Session, project: Project, seller: Seller, label: str, destination: str, note: str | None) -> dict:
    dest, name = normalise_destination(destination), label.strip()
    existing = _current_rows(db, project.id)
    if len(existing) >= MAX_PAYEES_PER_PROJECT:
        raise ApiError(409, "payee_limit", "The payee registry for this project is full.")
    if any(r.label.lower() == name.lower() for r in existing):
        raise ApiError(409, "payee_exists", "A payee with this label already exists in this project.")
    new = new_id()  # known up front: history rows are append-only, so nothing may be patched after the insert
    sha = _sha(dest)
    row = PayeeDestinationVersion(id=new, payee_id=new, project_id=project.id, version=1, label=name, destination=dest, destination_sha256=sha,
                                  chain_sha256=_chain(None, new, 1, sha), note=note, created_by_seller_id=seller.id)
    db.add(row)
    db.flush()
    _audit(db, project, "PAYEE_REGISTERED", row, {})
    db.commit()
    return view(row)


def change_destination(db: Session, project: Project, seller: Seller, payee_id: str, destination: str, note: str | None) -> dict:
    rows = versions_of(db, project.id, payee_id)
    if not rows:
        raise ApiError(404, "not_found", "Payee not found.")
    dest, current = normalise_destination(destination), rows[-1]
    if dest == current.destination:
        raise ApiError(409, "destination_unchanged", "The destination already matches the current registry entry.")
    if current.version >= MAX_VERSIONS_PER_PAYEE:
        raise ApiError(409, "version_limit", "This payee has reached the maximum number of recorded destination versions.")
    version = current.version + 1
    sha = _sha(dest)
    row = PayeeDestinationVersion(project_id=project.id, payee_id=payee_id, version=version, label=current.label, destination=dest,
                                  destination_sha256=sha, previous_version_id=current.id, chain_sha256=_chain(current, payee_id, version, sha),
                                  note=note, created_by_seller_id=seller.id)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:  # a concurrent change took this version number: nothing was written, the caller may retry
        db.rollback()
        raise ApiError(409, "version_conflict", "The payee changed concurrently; reload it and try again.") from exc
    _audit(db, project, "PAYEE_DESTINATION_CHANGED", row, {"previous_version": current.version, "previous_destination": mask(current.destination),
                                                          "independently_verified": False})
    db.commit()
    return view(row)


def _signal(code: str, severity: str, message: str, cites: list[str] | None = None, spans: list[dict] | None = None) -> dict:
    return {"code": code, "severity": severity, "message": message, "cites": cites or [], "spans": spans or []}


def _instruction_signals(db: Session, project_id: str, destination: str | None, amount_minor: int | None, currency: str | None) -> list[dict]:
    row = db.scalar(select(PaymentInstruction).where(PaymentInstruction.project_id == project_id).order_by(PaymentInstruction.version.desc()).limit(1))
    if row is None:
        return []
    cite, out = [f"payment_instruction:{row.id}"], []
    if destination and destination != (row.recipient_email or "").strip().lower():
        out.append(_signal("DESTINATION_DIFFERS_FROM_INSTRUCTION", "HIGH", "The destination differs from the payment instruction in force.", cite))
    if amount_minor is not None and amount_minor != row.amount_minor:
        out.append(_signal("AMOUNT_DIFFERS_FROM_INSTRUCTION", "MEDIUM", "The amount differs from the payment instruction in force.", cite))
    if currency and currency != row.currency:
        out.append(_signal("CURRENCY_DIFFERS_FROM_INSTRUCTION", "MEDIUM", "The currency differs from the payment instruction in force.", cite))
    return out


def _registry_signals(db: Session, project_id: str, payee_id: str | None, destination: str | None) -> list[dict]:
    if not destination and not payee_id:
        return []
    out = []
    if payee_id:
        rows = versions_of(db, project_id, payee_id)
        if not rows:
            raise ApiError(404, "not_found", "Payee not found.")
        current = rows[-1]
        cite = [f"payee:{payee_id}@v{current.version}"]
        if destination and destination != current.destination:
            out.append(_signal("DESTINATION_DIFFERS_FROM_REGISTRY", "HIGH", "The destination differs from the payee's current registry entry.", cite))
        if current.version > 1 and utcnow() - current.created_at < timedelta(days=RECENT_CHANGE_DAYS):
            out.append(_signal("DESTINATION_RECENTLY_CHANGED", "MEDIUM", f"The registry destination changed within the last {RECENT_CHANGE_DAYS} days.", cite))
        if destination and any(r.destination == destination for r in rows[:-1]):
            out.append(_signal("DESTINATION_SUPERSEDED", "MEDIUM", "The destination is a superseded entry for this payee.", [f"payee:{payee_id}@v{r.version}" for r in rows[:-1] if r.destination == destination]))
    elif destination:
        known = db.scalar(select(func.count()).select_from(PayeeDestinationVersion).where(
            PayeeDestinationVersion.project_id == project_id, PayeeDestinationVersion.destination == destination))
        if not known:
            out.append(_signal("DESTINATION_NOT_REGISTERED", "MEDIUM", "The destination is not in this project's payee registry."))
    return out


def _text_signals(extracted: bec_signals.Extracted, text: str | None, destination: str | None, known: set[str]) -> list[dict]:
    if not text:
        return []
    out = [_signal(p.code, "LOW", "Wording commonly seen in altered-destination fraud (a hint, not a finding).", spans=[{"start": s.start, "end": s.end} for s in p.spans])
           for p in bec_signals.phrase_signals(text)]
    other = [(e, s) for e, s in extracted.emails if e not in known and e != destination]
    if other and known:
        out.append(_signal("TEXT_MENTIONS_OTHER_DESTINATION", "MEDIUM", "The text names an e-mail address that is neither the claimed, the registered nor the instructed destination.",
                           spans=[{"start": s.start, "end": s.end} for _, s in other]))
    return out


def _extracted_view(x: bec_signals.Extracted) -> dict:
    span = lambda s: None if s is None else {"start": s.start, "end": s.end}  # noqa: E731
    return {"verified": False, "emails": [{"value": v, "span": span(s)} for v, s in x.emails], "amount_minor": x.amount_minor,
            "amount_span": span(x.amount_span), "currency": x.currency, "invoice_reference": x.invoice_reference,
            "invoice_reference_span": span(x.invoice_reference_span)}


def screen(db: Session, project: Project, *, payee_id: str | None, destination: str | None, amount_minor: int | None, currency: str | None,
           text: str | None) -> dict:
    """Read-only. Writes nothing and calls no provider."""
    try:
        text = bec_signals.check_text(text) if text else None
    except bec_signals.TextRejected as exc:
        raise ApiError(422, "invalid_text", str(exc)) from exc
    dest = normalise_destination(destination) if destination else None
    extracted = bec_signals.extract_fields(text) if text else bec_signals.extract_fields("")
    signals = _registry_signals(db, project.id, payee_id, dest) + _instruction_signals(db, project.id, dest, amount_minor, currency)
    known = {dest} if dest else set()
    row = db.scalar(select(PaymentInstruction).where(PaymentInstruction.project_id == project.id).order_by(PaymentInstruction.version.desc()).limit(1))
    if row is not None and row.recipient_email:
        known.add(row.recipient_email.strip().lower())
    if payee_id:
        known.add(versions_of(db, project.id, payee_id)[-1].destination)
    signals += _text_signals(extracted, text, dest, known)
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    signals.sort(key=lambda s: (order[s["severity"]], s["code"]))
    return {"project_id": project.id, "outcome": "REVIEW_RECOMMENDED" if signals else "NO_SIGNALS_FOUND", "signals": signals,
            "extracted": _extracted_view(extracted) if text else None, "advisory_only": True, "human_review_required": True}
