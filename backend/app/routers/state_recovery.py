"""Guarded recovery of the SIMULATED merchant store (S2). Owner-scoped. Nothing here calls a payment provider: a plan is created from stored
evidence, previewed (read-only), approved by the owner for one exact version + evidence fingerprint, then executed at most once."""
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from sqlalchemy.orm import Session

from app import schemas
from app.config import Settings
from app.deps import (check_origin, get_current_seller, get_db, get_owned_project, get_settings_dep, limit_observation_reads,
                      limit_payment_ops, require_csrf)
from app.errors import ApiError
from app.models import Project, Seller
from app.sim_enums import RecoveryKind
from app.simulation import recovery
from app.simulation.divergence import detect_divergences
from app.simulation.diagnosis_adapter import DiagnosisInputError, DiagnosisReport, diagnose
from app.simulation.log_evidence import build_log_batch, environment_for, get_service
from app.simulation.health import CALLER_REPORTED_CAPABILITIES, assess_integration_health, assess_project_health, summarize
from app.simulation.recovery_plan import KIND_FOR_DIVERGENCE, RecoveryError, build_draft

router = APIRouter(prefix="/api/v1/projects/{project_id}/state-recovery", tags=["state-recovery"],
                   dependencies=[Depends(check_origin)])
KindParam = Literal["DUPLICATE_ORDERS", "PAID_NOT_APPLIED", "STALE_PAID_AFTER_REFUND", "FALSE_HEALTHY"]
PLAN_ID = Path(max_length=36, pattern=r"^[0-9a-f-]{36}$")
_STATUS = {"not_found": 404, "invalid_request": 422, "no_divergence": 409, "malformed_evidence": 422, "unresolved_evidence": 409}


def _api(exc: recovery.RecoveryRefused | RecoveryError | DiagnosisInputError) -> ApiError:
    status = getattr(exc, "status", None) or _STATUS.get(exc.code, 409)
    detail = getattr(exc, "detail", None)
    return ApiError(status, exc.code, exc.message, details=[detail] if detail else None)


@router.get("/divergences", response_model=schemas.DivergenceList, dependencies=[Depends(limit_observation_reads)])
def divergences(credentials_configured: bool = True, heartbeat_ok: bool = True, project: Project = Depends(get_owned_project),
                db: Session = Depends(get_db)):
    """Divergences between provider payment state and the simulated merchant store, plus the end-to-end health assessment."""
    health = assess_integration_health(db, project.id, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
    found = [{"code": d.code.value, "payment_id": d.payment_id, "order_ids": list(d.order_ids), "detail": d.detail,
              "suggested_plan_kind": getattr(KIND_FOR_DIVERGENCE.get(d.code), "value", None)} for d in detect_divergences(db, project.id)]
    return {"project_id": project.id, "divergences": found,
            "integration_health": {"configuration": health.configuration, "heartbeat": health.heartbeat, "end_to_end": health.end_to_end,
                                   "overall": health.overall, "reasons": list(health.reasons)}}


@router.post("/plans", response_model=schemas.RecoveryPlanView, status_code=201, dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def create_plan(body: schemas.RecoveryPlanCreate, response: Response, project: Project = Depends(get_owned_project),
                seller: Seller = Depends(get_current_seller), db: Session = Depends(get_db)):
    """Idempotent: identical evidence returns the existing plan (200); changed evidence creates a new version (201)."""
    try:
        plan, created = recovery.create_plan(db, project.id, seller.id, RecoveryKind(body.kind), body.payment_id,
                                             credentials_configured=body.credentials_configured, heartbeat_ok=body.heartbeat_ok)
    except RecoveryError as exc:
        raise _api(exc) from exc
    if not created:
        response.status_code = 200
    return recovery.plan_view(db, plan)


@router.get("/plans", response_model=schemas.RecoveryPlanList, dependencies=[Depends(limit_observation_reads)])
def list_plans(limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=100_000),
               project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    rows, total = recovery.list_plans(db, project.id, limit=limit, offset=offset)
    return {"items": [recovery.plan_view(db, p) for p in rows], "total": total, "limit": limit, "offset": offset}


def _load(db: Session, project: Project, plan_id: str):
    try:
        return recovery.get_plan(db, project.id, plan_id)
    except RecoveryError as exc:
        raise _api(exc) from exc


@router.get("/plans/{plan_id}", response_model=schemas.RecoveryPlanView, dependencies=[Depends(limit_observation_reads)])
def get_plan(plan_id: str = PLAN_ID, project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return recovery.plan_view(db, _load(db, project, plan_id))


@router.get("/plans/{plan_id}/preview", response_model=schemas.RecoveryPreview, dependencies=[Depends(limit_observation_reads)])
def preview(plan_id: str = PLAN_ID, project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    """Dry run against FRESH evidence. Writes nothing and never calls a provider."""
    _load(db, project, plan_id)
    return recovery.preview_plan(db, project.id, plan_id)


@router.post("/plans/{plan_id}/approve", response_model=schemas.RecoveryPlanView, dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def approve(body: schemas.RecoveryPlanApprove, plan_id: str = PLAN_ID, project: Project = Depends(get_owned_project),
            seller: Seller = Depends(get_current_seller), db: Session = Depends(get_db)):
    plan = _load(db, project, plan_id)
    try:
        plan, _ = recovery.approve_plan(db, project.id, plan.id, seller.id, version=body.version, evidence_fingerprint=body.evidence_fingerprint)
    except recovery.RecoveryRefused as exc:
        raise _api(exc) from exc
    return recovery.plan_view(db, plan)


@router.post("/plans/{plan_id}/execute", response_model=schemas.RecoveryExecutionResult, dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def execute(plan_id: str = PLAN_ID, project: Project = Depends(get_owned_project), seller: Seller = Depends(get_current_seller),
            db: Session = Depends(get_db)):
    """At most once. A repeat returns the recorded result with executed_now=false and changes nothing. A refusal is recorded and
    returns 409; after fixing the cause, preview again (or create a fresh plan) and retry."""
    plan = _load(db, project, plan_id)
    try:
        result = recovery.execute_plan(db, project.id, plan.id, seller.id)
    except recovery.RecoveryRefused as exc:
        raise _api(exc) from exc
    return {"plan": recovery.plan_view(db, result["plan"]), "executed_now": result["executed_now"]}


@router.get("/plans/{plan_id}/events", response_model=schemas.RecoveryEventList, dependencies=[Depends(limit_observation_reads)])
def events(plan_id: str = PLAN_ID, project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    """The append-only history: creation, approval, each applied step, execution, and every refusal."""
    rows = recovery.list_events(db, project.id, _load(db, project, plan_id).id)
    return {"plan_id": plan_id, "events": [{"id": e.id, "seq": e.seq, "event_type": e.event_type, "actor_seller_id": e.actor_seller_id,
                                            "approved_version": e.approved_version, "approved_fingerprint": e.approved_fingerprint,
                                            "detail": e.detail, "created_at": e.created_at} for e in rows]}


# --- read-only advisory diagnosis and integration health (S4) -------------------------------------------------------------------------

def _diagnosis_response(report: DiagnosisReport, *, kind: RecoveryKind, payment_id: str | None, plan_id: str | None, plan_version: int | None,
                        fingerprint: str) -> dict:
    labels = report.context.provenance_labels
    return {"case_id": report.context.data.case_id, "kind": kind.value, "payment_id": payment_id, "plan_id": plan_id, "plan_version": plan_version,
            "evidence_fingerprint": fingerprint, "mode": report.mode, "llm_evaluation": report.llm_evaluation, "llm_rejection": report.llm_rejection,
            "diagnosis": report.diagnosis.model_dump(mode="json"),
            "llm_diagnosis": None if report.llm_diagnosis is None else report.llm_diagnosis.model_dump(mode="json"),
            "evidence": [{"evidence_id": e.evidence_id, "source": "merchant" if e.source == "simulation" else e.source, "kind": e.kind,
                          "entity_id": e.entity_id, "status": e.status, "observed_at": e.observed_at, "occurred_at": e.occurred_at,
                          "event_id": e.event_id, "effect_key": e.effect_key, "provenance_label": labels.get(e.evidence_id, "UNLABELLED")}
                         for e in report.context.data.evidence],
            "observation_window_complete": report.context.data.observation_window_complete,
            "incomplete_evidence_warnings": list(report.context.data.incomplete_evidence_warnings)}


def _run_diagnosis(request: Request, db: Session, project_id: str, payment_id: str | None, envelope: dict, case_id: str, observed_at=None) -> DiagnosisReport:
    try:
        return diagnose(db, project_id, payment_id, envelope, case_id=case_id, llm_transport=getattr(request.app.state, "llm_transport", None), observed_at=observed_at)
    except DiagnosisInputError as exc:
        raise _api(exc) from exc


@router.get("/diagnosis", response_model=schemas.DiagnosisResponse, dependencies=[Depends(limit_observation_reads)])
def diagnose_divergence(request: Request, kind: KindParam, payment_id: str | None = Query(default=None, max_length=36),
                        credentials_configured: bool = True, heartbeat_ok: bool = True, project: Project = Depends(get_owned_project),
                        db: Session = Depends(get_db)):
    """Advisory diagnosis of a current divergence BEFORE any plan exists: cited, read-only, writes nothing."""
    try:
        draft = build_draft(db, project.id, RecoveryKind(kind), payment_id, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
    except RecoveryError as exc:
        raise _api(exc) from exc
    report = _run_diagnosis(request, db, project.id, draft.payment_id, draft.evidence, f"{kind}:{draft.payment_id or project.id}")
    return _diagnosis_response(report, kind=draft.kind, payment_id=draft.payment_id, plan_id=None, plan_version=None, fingerprint=draft.evidence_fingerprint)


@router.get("/plans/{plan_id}/diagnosis", response_model=schemas.DiagnosisResponse, dependencies=[Depends(limit_observation_reads)])
def diagnose_plan(request: Request, plan_id: str = PLAN_ID, project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    """Advisory diagnosis of a stored plan's evidence. It does not approve or change the plan; the plan's own guards still decide execution."""
    plan = _load(db, project, plan_id)
    report = _run_diagnosis(request, db, project.id, plan.payment_id, plan.evidence, plan.id, observed_at=plan.created_at)
    return _diagnosis_response(report, kind=RecoveryKind(plan.kind), payment_id=plan.payment_id, plan_id=plan.id, plan_version=plan.version,
                               fingerprint=plan.evidence_fingerprint)


@router.get("/integration-health", response_model=schemas.IntegrationHealthResponse, dependencies=[Depends(limit_observation_reads)])
def integration_health(credentials_configured: bool = True, heartbeat_ok: bool = True, project: Project = Depends(get_owned_project),
                       db: Session = Depends(get_db)):
    """The authoritative, cited health assessment of the project's SIMULATED integration. Configuration alone is never TRANSACTION_VERIFIED."""
    result = assess_project_health(db, project.id, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
    summary = summarize(result, credentials_configured=credentials_configured, heartbeat_ok=heartbeat_ok)
    return {"project_id": project.id, "assessment": result.model_dump(mode="json"), "caller_reported_capabilities": list(CALLER_REPORTED_CAPABILITIES),
            "summary": {"configuration": summary.configuration, "heartbeat": summary.heartbeat, "end_to_end": summary.end_to_end,
                        "overall": summary.overall, "reasons": list(summary.reasons)}}


@router.post("/log-intelligence", response_model=schemas.LogIntelligenceResponse, dependencies=[Depends(require_csrf), Depends(limit_observation_reads)])
def log_intelligence(request: Request, body: schemas.LogIntelligenceRequest, project: Project = Depends(get_owned_project),
                     settings: Settings = Depends(get_settings_dep), db: Session = Depends(get_db)):
    """Read-only advisory reading of submitted log lines next to the server-built evidence (a POST only because it carries a body: it writes nothing).

    The caller supplies lines only; scope, citations and provenance (`untrusted_log`) are assigned here. The deterministic reading is the default;
    `requested_mode=stanza` uses the optional local model when the server has one and enough memory, and otherwise falls back visibly."""
    try:
        draft = build_draft(db, project.id, RecoveryKind(body.kind), body.payment_id, credentials_configured=body.credentials_configured,
                            heartbeat_ok=body.heartbeat_ok)
    except RecoveryError as exc:
        raise _api(exc) from exc
    report = _run_diagnosis(request, db, project.id, draft.payment_id, draft.evidence, f"{body.kind}:{draft.payment_id or project.id}")
    environment = environment_for(report.context.provenance_labels, draft.payment_id)
    try:
        batch = build_log_batch(project.id, draft.payment_id, [line.text for line in body.logs], environment=environment)
        result = get_service(request.app, settings).for_s4(report.context, batch, project_id=project.id, payment_id=draft.payment_id,
                                                          requested_mode=body.requested_mode)
    except ValueError as exc:  # the message never includes the rejected text
        raise ApiError(422, "invalid_log_evidence", "The submitted log lines were rejected by input validation.") from exc
    shared = _diagnosis_response(report, kind=draft.kind, payment_id=draft.payment_id, plan_id=None, plan_version=None, fingerprint=draft.evidence_fingerprint)
    return {"requested_mode": result.requested_mode, "effective_mode": result.effective_mode, "fallback_reason": result.fallback_reason,
            "case_id": shared["case_id"], "kind": draft.kind.value, "payment_id": draft.payment_id, "evidence_fingerprint": draft.evidence_fingerprint,
            "log_lines_analyzed": len(body.logs),
            "log_sources": [{"evidence_ref": r.evidence_ref, "log_id": r.log_id, "provenance": r.provenance, "environment": r.environment} for r in batch.records],
            "evidence": shared["evidence"], "report": result.report.model_dump(mode="json"),
            "observation_window_complete": shared["observation_window_complete"], "incomplete_evidence_warnings": shared["incomplete_evidence_warnings"]}
