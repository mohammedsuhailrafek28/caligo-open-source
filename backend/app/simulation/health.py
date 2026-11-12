"""Integration health for the SIMULATED merchant store: a compatibility adapter over the AUTHORITATIVE policy.

Authoritative policy: `app.integration_health` (S3.5): a deterministic, citation-checked assessor with explicit project/environment scope,
freshness by check time, provenance rules and a rule that configuration or a green claim never proves a transaction. This module only
(1) turns stored state into that module's `HealthInput` and (2) maps the result onto the older S1/S2 `HealthAssessment` shape so existing
callers keep working. It contains NO health decision logic of its own.

Everything built here is explicitly SIMULATION evidence (`environment='simulation'`, `source='simulation_runner'`): the configuration and
heartbeat inputs are caller-reported claims made at `as_of`; the transaction proof is a payment the provider evidence shows PAID whose merchant
order was applied by the SAFE processor, and it ages out after the assessor's freshness window. No evidence, or stale evidence, is never HEALTHY.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integration_health import HealthAssessment as AuthoritativeAssessment, HealthInput, assess
from app.models import PaymentRecord
from app.schemas import PaymentProviderStatus as P
from app.sim_enums import AuditAction, DivergenceCode, OrderState, ProcessingOutcome
from app.sim_models import SimAuditRecord, SimOrder
from app.simulation.divergence import detect_divergences

INTEGRATION_ID = "simulated-merchant"
CALLER_REPORTED_CAPABILITIES = ("configuration_observed", "provider_connectivity_observed")  # supplied by the caller, not observed by Caligo
MAX_PROOFS = 50  # newest proven payments considered; the assessor needs only the latest and the list is bounded


@dataclass(frozen=True)
class HealthAssessment:
    """Legacy S1/S2 summary derived from the authoritative assessment (see `assess_project_health` for the full, cited result)."""

    configuration: str  # CONFIGURED | NOT_CONFIGURED
    heartbeat: str  # RESPONDING | NOT_RESPONDING
    end_to_end: str  # VERIFIED | NOT_VERIFIED | FAILING
    overall: str  # HEALTHY | UNVERIFIED | DEGRADED
    reasons: tuple[str, ...]


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    value = value or fallback
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _record(evidence_id: str, check: str, outcome: str, when: datetime, **extra) -> dict:
    return {"evidence_id": evidence_id, "project_id": extra.pop("project_id"), "integration_id": INTEGRATION_ID, "environment": "simulation",
            "event_id": evidence_id, "check": check, "outcome": outcome, "source": "simulation_runner", "checked_at": when, "received_at": when, **extra}


def _proofs(db: Session, project_id: str) -> list[tuple[PaymentRecord, SimOrder, SimAuditRecord]]:
    """Payments the provider shows PAID, with a PAID/FULFILLED merchant order created and applied by the SAFE processor (newest first)."""
    paid = select(PaymentRecord.id).where(PaymentRecord.project_id == project_id, PaymentRecord.provider_status == P.PAID.value)
    orders = db.scalars(select(SimOrder).where(SimOrder.project_id == project_id, SimOrder.payment_id.in_(paid),
                                               SimOrder.state.in_((OrderState.PAID.value, OrderState.FULFILLED.value))))
    found = []
    for order in orders:
        applied = db.scalar(select(SimAuditRecord).where(
            SimAuditRecord.project_id == project_id, SimAuditRecord.action == AuditAction.EVENT_PROCESSED.value,
            SimAuditRecord.outcome == ProcessingOutcome.APPLIED.value, SimAuditRecord.processor == "SAFE",
            SimAuditRecord.detail["order_id"].as_string() == order.id).order_by(SimAuditRecord.created_at).limit(1))
        payment = db.get(PaymentRecord, order.payment_id)
        if applied is not None and payment is not None:
            found.append((payment, order, applied))
    found.sort(key=lambda t: _aware(t[2].created_at, t[0].created_at), reverse=True)
    return found[:MAX_PROOFS]


def build_health_input(db: Session, project_id: str, *, credentials_configured: bool, heartbeat_ok: bool, as_of: datetime | None = None,
                       max_age_seconds: int = 86400) -> HealthInput:
    as_of = as_of or datetime.now(timezone.utc)
    records = [_record(f"config:{project_id}", "configuration", "success" if credentials_configured else "failure", as_of, project_id=project_id),
               _record(f"connect:{project_id}", "connectivity", "success" if heartbeat_ok else "failure", as_of, project_id=project_id)]
    for payment, order, applied in _proofs(db, project_id):
        captured, completed = _aware(payment.checked_at, payment.created_at), _aware(applied.created_at, payment.created_at)
        done = max(captured, completed)
        records += [_record(f"capture:{payment.id}", "provider_capture", "success", captured, project_id=project_id, transaction_id=payment.id),
                    _record(f"order:{order.id}", "order_completion", "success", completed, project_id=project_id, transaction_id=payment.id),
                    _record(f"txn:{payment.id}", "transaction", "success", done, project_id=project_id, transaction_id=payment.id,
                            evidence_refs=[f"capture:{payment.id}", f"order:{order.id}"])]
    for d in detect_divergences(db, project_id):  # every open divergence is a CURRENT failure (observed now), so it outranks older success proof
        prefix = "unapplied" if d.code is DivergenceCode.PROVIDER_PAID_MERCHANT_NOT_PAID else f"divergence:{d.code.value}"
        records.append(_record(f"{prefix}:{d.payment_id}", "order_completion", "failure", as_of, project_id=project_id, transaction_id=d.payment_id))
    clamp = lambda r: {**r, "checked_at": min(r["checked_at"], as_of), "received_at": min(max(r["received_at"], r["checked_at"]), as_of)}  # noqa: E731
    return HealthInput(project_id=project_id, integration_id=INTEGRATION_ID, environment="simulation", as_of=as_of, max_age_seconds=max_age_seconds,
                       webhook_required=False, evidence=tuple(clamp(r) for r in records))


def assess_project_health(db: Session, project_id: str, *, credentials_configured: bool, heartbeat_ok: bool, as_of: datetime | None = None,
                          max_age_seconds: int = 86400) -> AuthoritativeAssessment:
    """The authoritative, cited assessment for a project's simulated integration."""
    return assess(build_health_input(db, project_id, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok, as_of=as_of,
                                     max_age_seconds=max_age_seconds))


def summarize(result: AuthoritativeAssessment, *, credentials_configured: bool, heartbeat_ok: bool) -> HealthAssessment:
    verified = any(c.name == "end_to_end_transaction_observed" for c in result.supported_capabilities)
    failing = any(e.startswith("unapplied:") for e in result.contradictory_evidence_ids)
    diverging = any(e.startswith("divergence:") for e in result.contradictory_evidence_ids)
    end_to_end = "FAILING" if failing else "VERIFIED" if verified else "NOT_VERIFIED"
    overall = "HEALTHY" if result.status == "TRANSACTION_VERIFIED" else "DEGRADED" if result.status == "DEGRADED" or failing or diverging else "UNVERIFIED"
    reasons = []
    if failing:
        reasons.append("payment(s) the provider shows as paid have no paid merchant order")
    elif diverging:
        reasons.append("an open provider/merchant divergence contradicts earlier proof")
    elif not verified:
        reasons.append("no fresh end-to-end payment has been observed reaching the merchant; configuration and heartbeat prove nothing about that")
    if not credentials_configured:
        reasons.append("credentials are not configured")
    if not heartbeat_ok:
        reasons.append("heartbeat is not responding")
    reasons.extend(result.stale_evidence_warnings)
    return HealthAssessment("CONFIGURED" if credentials_configured else "NOT_CONFIGURED", "RESPONDING" if heartbeat_ok else "NOT_RESPONDING",
                            end_to_end, overall, tuple(reasons))


def assess_integration_health(db: Session, project_id: str, *, credentials_configured: bool, heartbeat_ok: bool,
                              as_of: datetime | None = None) -> HealthAssessment:
    """Compatibility entry point used by S1/S2 code: a thin mapping over the authoritative assessment."""
    result = assess_project_health(db, project_id, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok, as_of=as_of)
    return summarize(result, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
