"""Narrow adapter: S2 recovery evidence (`caligo.recovery_evidence.v1`) -> S3 `DiagnosisInput` -> advisory `Diagnosis`.

Read-only and advisory. Nothing here writes, calls a provider, executes a recovery step or influences a recovery-policy guard: a diagnosis is
text for a human; the S2 approval/execution guards decide what may happen. Properties preserved from the S2 envelope:

* Stable evidence IDs: `evidence_id` == the S2 citation ref (`payment:<id>`, `order:<id>`, `event:<id>`, `fulfillment:<id>`, `movement:<id>`).
* Scope: every record is loaded with a project filter and a payment filter; every ref the envelope cites must resolve to such a record, else
  the whole input is rejected (foreign-project, fabricated or stale reference). Evidence is never copied from the caller's JSON.
* Provenance: provider-side facts are `source='provider'`; all merchant-side facts are `source='simulation'` (the merchant store is simulated and
  never provider truth). `provenance_labels` additionally says whether the provider record is MOCK or Sandbox.
* Timestamps: `observed_at` is the server receipt/check time; `occurred_at` is the sender-claimed time (never proof of order).
* Completeness: `observation_window_complete` and `incomplete_evidence_warnings` come from the envelope's completeness block.
* Distinct business-effect identities: one evidence record per stored row; side effects carry `effect_key` = the business effect (what it is
  for), `entity_id` = the row, so the same effect applied twice shows as two entities under one key.
* Free text from the database or callers never enters the input: `text` is always empty and sender-supplied ids (event keys) reach the model only
  as plain tokens (else a stable digest), so there is nothing for an instruction to hide in.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.diagnosis import Diagnosis, DiagnosisInput, Evidence, LLMDiagnoser, RulesDiagnoser, validate_diagnosis
from app.models import PaymentRecord
from app.sandbox_attestation import provider_label
from app.schemas import PaymentProviderStatus as P
from app.sim_enums import AuditAction, EventType, FulfillmentState, MovementKind
from app.sim_models import SimAuditRecord, SimFulfillment, SimIncomingEvent, SimInventoryMovement, SimOrder
from app.simulation.recovery_plan import EVIDENCE_SCHEMA

MAX_PER_KIND = 200
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_EVENT_STATUS = {EventType.PAYMENT_COMPLETED.value: "paid", EventType.PAYMENT_REFUNDED.value: "refunded"}


class DiagnosisInputError(Exception):
    """The evidence cannot be turned into a trustworthy diagnosis input (malformed, foreign, fabricated or unresolvable)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class DiagnosisContext:
    data: DiagnosisInput
    provenance_labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DiagnosisReport:
    context: DiagnosisContext
    diagnosis: Diagnosis
    mode: str  # RULES_ONLY | RULES_AND_LLM
    llm_evaluation: str  # NOT_RUN | VALID | REJECTED
    llm_diagnosis: Diagnosis | None = None
    llm_rejection: str | None = None


def _safe_id(value: str) -> str:
    """Sender-supplied identifiers reach the model only if they are plain tokens; anything else is replaced by a stable digest."""
    return value if _SAFE_ID.match(value) else "sha256:" + hashlib.sha256(value.encode("utf8")).hexdigest()[:40]


def _aware(value: datetime | None, fallback: datetime | None = None) -> datetime:
    value = value or fallback or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _provider_status(payment: PaymentRecord) -> str:
    return "captured" if payment.provider_status == P.PAID.value else payment.provider_status.lower()


def _check_envelope(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict) or envelope.get("schema") != EVIDENCE_SCHEMA:
        raise DiagnosisInputError("malformed_evidence", "The evidence is not a recognised recovery evidence envelope.")
    citations = envelope.get("citations")
    if not isinstance(citations, list) or any(not isinstance(c, dict) or not isinstance(c.get("ref"), str) for c in citations):
        raise DiagnosisInputError("malformed_evidence", "The evidence citations are malformed.")
    if not isinstance(envelope.get("completeness"), dict):
        raise DiagnosisInputError("malformed_evidence", "The evidence has no completeness block.")
    return envelope


def _evidence(ref: str, source: str, kind: str, entity_id: str, payment_id: str, status: str, observed_at: datetime, **extra) -> Evidence:
    return Evidence(evidence_id=ref, source=source, kind=kind, entity_id=entity_id, payment_id=payment_id, status=status,
                    observed_at=_aware(observed_at), **extra)


def _payment_evidence(db: Session, project_id: str, payment_id: str, labels: dict[str, str]) -> tuple[Evidence, PaymentRecord]:
    payment = db.get(PaymentRecord, payment_id)
    if payment is None or payment.project_id != project_id:
        raise DiagnosisInputError("unresolved_evidence", "The payment does not belong to this project.")
    ref = f"payment:{payment.id}"
    labels[ref] = provider_label(payment.provider, payment.id)  # MOCK | UNATTESTED sandbox | AUTHENTICATED sandbox (runner-attested only)
    return _evidence(ref, "provider", "payment", payment.id, payment.id, _provider_status(payment), _aware(payment.checked_at, payment.created_at)), payment


def _event_records(db: Session, project_id: str, payment_id: str, labels: dict[str, str]) -> tuple[list[Evidence], dict[str, str]]:
    keys: dict[str, str] = {}
    out = []
    rows = db.scalars(select(SimIncomingEvent).where(SimIncomingEvent.project_id == project_id, SimIncomingEvent.payment_id == payment_id)
                      .order_by(SimIncomingEvent.received_at, SimIncomingEvent.id).limit(MAX_PER_KIND))
    for e in rows:
        keys[e.id] = e.event_key
        ref = f"event:{e.id}"
        labels[ref] = "SIMULATED_INCOMING_EVENT"
        out.append(_evidence(ref, "simulation", "event", e.id, payment_id, _EVENT_STATUS.get(e.event_type, e.event_type.lower()), e.received_at,
                             occurred_at=None if e.asserted_at is None else _aware(e.asserted_at), event_id=_safe_id(e.event_key)))
    return out, keys


def _order_records(db: Session, project_id: str, payment_id: str, event_keys: dict[str, str], labels: dict[str, str]) -> tuple[list[Evidence], list[SimOrder]]:
    orders = list(db.scalars(select(SimOrder).where(SimOrder.project_id == project_id, SimOrder.payment_id == payment_id)
                             .order_by(SimOrder.created_at, SimOrder.id).limit(MAX_PER_KIND)))
    out = []
    for o in orders:
        created = db.scalar(select(SimAuditRecord).where(SimAuditRecord.project_id == project_id, SimAuditRecord.entity_id == o.id,
                                                         SimAuditRecord.action == AuditAction.ORDER_CREATED.value))
        cause = event_keys.get(created.event_id) if created is not None and created.event_id else None
        ref = f"order:{o.id}"
        labels[ref] = "SIMULATED_MERCHANT_STATE"
        out.append(_evidence(ref, "simulation", "order", o.id, payment_id, o.state.lower(), o.created_at, caused_by_event_id=cause))
    return out, orders


def _effect_records(db: Session, project_id: str, payment_id: str, orders: list[SimOrder], labels: dict[str, str]) -> list[Evidence]:
    out = []
    ids = [o.id for o in orders]
    if not ids:
        return out
    net: dict[str, int] = {}
    moves = list(db.scalars(select(SimInventoryMovement).where(SimInventoryMovement.project_id == project_id, SimInventoryMovement.order_id.in_(ids))
                            .order_by(SimInventoryMovement.created_at, SimInventoryMovement.id).limit(MAX_PER_KIND)))
    for m in moves:
        net[m.order_id] = net.get(m.order_id, 0) + (-1 if m.kind == MovementKind.RELEASE.value else 1) * m.quantity
    for m in moves:
        if m.kind == MovementKind.RELEASE.value:
            continue
        ref = f"movement:{m.id}"
        labels[ref] = "SIMULATED_MERCHANT_STATE"
        status = "applied" if net.get(m.order_id, 0) > 0 else "released"  # a unit already returned is no longer a live effect
        out.append(_evidence(ref, "simulation", "inventory", m.id, payment_id, status, m.created_at, effect_key=f"stock:{payment_id}:{m.item_id}"[:128]))
    for f in db.scalars(select(SimFulfillment).where(SimFulfillment.project_id == project_id, SimFulfillment.order_id.in_(ids))
                        .order_by(SimFulfillment.created_at, SimFulfillment.id).limit(MAX_PER_KIND)):
        ref = f"fulfillment:{f.id}"
        labels[ref] = "SIMULATED_MERCHANT_STATE"
        status = "cancelled" if f.state == FulfillmentState.CANCELLED.value else "applied"
        out.append(_evidence(ref, "simulation", "fulfillment", f.id, payment_id, status, f.created_at, effect_key=f"shipment:{payment_id}"))
    return out


def _health_records(envelope: dict[str, Any], project_id: str, labels: dict[str, str], observed: datetime) -> list[Evidence]:
    health = envelope.get("health")
    if not isinstance(health, dict):
        return []
    scope = f"project:{project_id}"
    out = []
    claim = health.get("configuration") == "CONFIGURED" and health.get("heartbeat") == "RESPONDING"
    if claim:  # caller-reported configuration + heartbeat is a CLAIM of health, never proof
        labels["health:claim"] = "CALLER_REPORTED_CLAIM"
        out.append(_evidence("health:claim", "operator", "integration", "health-claim", scope, "healthy", observed))
    if health.get("end_to_end") == "VERIFIED":
        labels["health:e2e"] = "AUTHORITATIVE_HEALTH_ASSESSMENT"
        out.append(_evidence("health:e2e", "simulation", "integration", "health-e2e", scope, "e2e_succeeded", observed))
    return out


def build_diagnosis_input(db: Session, project_id: str, payment_id: str | None, envelope: Any, *, case_id: str,
                          observed_at: datetime | None = None) -> DiagnosisContext:
    """Rebuild the evidence from stored, project-scoped records and require that everything the envelope cites resolves to them."""
    env = _check_envelope(envelope)
    labels: dict[str, str] = {}
    records: list[Evidence] = []
    if payment_id is not None:
        payment_ev, _ = _payment_evidence(db, project_id, payment_id, labels)
        events, keys = _event_records(db, project_id, payment_id, labels)
        orders, order_rows = _order_records(db, project_id, payment_id, keys, labels)
        records = [payment_ev, *events, *orders, *_effect_records(db, project_id, payment_id, order_rows, labels)]
    records += _health_records(env, project_id, labels, _aware(observed_at))  # a stored plan's claims are as old as the plan
    known = {r.evidence_id for r in records}
    cited = {c["ref"] for c in env["citations"]}
    audit_ids = {ref.split(":", 1)[1] for ref in cited if ref.startswith("audit:")}
    verified_audit = set(db.scalars(select(SimAuditRecord.id).where(SimAuditRecord.project_id == project_id, SimAuditRecord.id.in_(audit_ids)))) if audit_ids else set()
    # audit rows are checked for scope but are not diagnostic inputs (they describe merchant-side processing, not payment state)
    unresolved = sorted(ref for ref in cited if ref not in known and not (ref.startswith("audit:") and ref.split(":", 1)[1] in verified_audit))
    if unresolved:
        raise DiagnosisInputError("unresolved_evidence", f"{len(unresolved)} cited record(s) could not be resolved inside this project and payment.")
    completeness = env["completeness"]
    warnings = tuple(f"Missing evidence: {m}" for m in completeness.get("missing", ()) if isinstance(m, str))
    data = DiagnosisInput(case_id=case_id[:128], evidence=tuple(records), incomplete_evidence_warnings=warnings,
                          observation_window_complete=bool(completeness.get("complete")))
    return DiagnosisContext(data, labels)


def diagnose(db: Session, project_id: str, payment_id: str | None, envelope: Any, *, case_id: str, llm_transport: Any | None = None,
             observed_at: datetime | None = None) -> DiagnosisReport:
    """Rules diagnosis always; an LLM diagnosis too only when a transport is configured (its output must pass the S3 citation/fact validation)."""
    context = build_diagnosis_input(db, project_id, payment_id, envelope, case_id=case_id, observed_at=observed_at)
    rules = validate_diagnosis(context.data, RulesDiagnoser().diagnose(context.data))
    if llm_transport is None:
        return DiagnosisReport(context, rules, "RULES_ONLY", "NOT_RUN")
    try:
        llm = LLMDiagnoser(llm_transport).diagnose(context.data)
    except Exception as exc:  # a model failure or an invalid answer must never break or alter the deterministic diagnosis
        return DiagnosisReport(context, rules, "RULES_AND_LLM", "REJECTED", llm_rejection=type(exc).__name__)
    return DiagnosisReport(context, rules, "RULES_AND_LLM", "VALID", llm_diagnosis=llm)
