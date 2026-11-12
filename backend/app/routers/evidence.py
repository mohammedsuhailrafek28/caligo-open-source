"""Seller-facing evidence routes (Stage 3): baselines, scope changes and the audit trail."""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app import schemas
from app.deps import check_origin, get_current_seller, get_db, get_owned_project, require_csrf
from app.errors import ApiError
from app.models import AuditEvent, Project, ScopeBaseline, ScopeChange, Seller
from app.services import analysis as analysis_service
from app.services import buyer as buyer_service
from app.services import evidence as evidence_service
from app.services import report as report_service
from app.services.report_markdown import render_markdown
from app.services import scope as scope_service

router = APIRouter(prefix="/api/v1/projects", tags=["evidence"], dependencies=[Depends(check_origin)])


@router.post("/{project_id}/baselines", status_code=201, response_model=schemas.BaselineView,
             dependencies=[Depends(require_csrf)])
def freeze_baseline(body: schemas.BaselineCreate, project: Project = Depends(get_owned_project),
                    db: Session = Depends(get_db)) -> ScopeBaseline:
    """Freezes the ORIGINAL agreement as immutable version 1. Later versions exist only through verified buyer approval."""
    return scope_service.freeze_original(db, project, body)


@router.get("/{project_id}/baselines", response_model=list[schemas.BaselineView])
def list_baselines(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> list[ScopeBaseline]:
    return scope_service.list_baselines(db, project)


@router.post("/{project_id}/scope-changes", status_code=201, response_model=schemas.ScopeChangeView,
             dependencies=[Depends(require_csrf)])
def propose_change(body: schemas.ScopeChangeCreate, project: Project = Depends(get_owned_project),
                   db: Session = Depends(get_db)) -> ScopeChange:
    """Creates a PROPOSED change. It changes no baseline; there is deliberately no seller route that approves it."""
    return scope_service.propose_change(db, project, body)


@router.get("/{project_id}/scope-changes", response_model=list[schemas.ScopeChangeView])
def list_changes(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> list[ScopeChange]:
    return scope_service.list_changes(db, project)


@router.get("/{project_id}/audit-events", response_model=list[schemas.AuditEventView])
def list_audit_events(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> list[AuditEvent]:
    return scope_service.list_audit_events(db, project)


# --- delivery evidence artifacts and timeline (Stage 3B) -----------------------------------------

@router.post("/{project_id}/artifacts", status_code=201, response_model=schemas.ArtifactView,
             dependencies=[Depends(require_csrf)])
def register_artifact(body: schemas.ArtifactCreate, project: Project = Depends(get_owned_project),
                      db: Session = Depends(get_db)) -> schemas.ArtifactView:
    """Registers a delivery artifact. The server hashes any submitted content and stamps its own ingestion time."""
    return evidence_service.register_artifact(db, project, body)


@router.get("/{project_id}/artifacts", response_model=list[schemas.ArtifactView])
def list_artifacts(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> list[schemas.ArtifactView]:
    return evidence_service.list_artifacts(db, project)


@router.post("/{project_id}/artifacts/{artifact_id}/links", response_model=schemas.ArtifactView,
             dependencies=[Depends(require_csrf)])
def link_artifact(artifact_id: str, body: schemas.ArtifactLinkCreate, project: Project = Depends(get_owned_project),
                  db: Session = Depends(get_db)) -> schemas.ArtifactView:
    """Adds seller-asserted links to requirements (idempotent). A link is a claim, not proof that a requirement is met."""
    return evidence_service.link_artifact(db, project, artifact_id, body.requirement_keys)


@router.get("/{project_id}/timeline", response_model=list[schemas.TimelineItem])
def timeline(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> list[schemas.TimelineItem]:
    return evidence_service.build_timeline(db, project)


# --- buyer requests and decisions (Stage 3C): the seller asks; only the verified buyer can decide ----------------

def limit_buyer_mail(request: Request, seller: Seller = Depends(get_current_seller)) -> None:
    throttle = request.app.state.buyer_mail_throttle
    if throttle.is_blocked(seller.id):
        raise ApiError(429, "too_many_attempts", "Too many buyer requests. Try again shortly.", headers={"Retry-After": "60"})
    throttle.record_failure(seller.id)


@router.post("/{project_id}/buyer-requests", status_code=201, response_model=schemas.BuyerRequestView,
             dependencies=[Depends(require_csrf), Depends(limit_buyer_mail)])
def create_buyer_request(body: schemas.BuyerRequestCreate, request: Request, project: Project = Depends(get_owned_project),
                         db: Session = Depends(get_db)) -> schemas.BuyerRequestView:
    """Mails the buyer a link. The response never contains the link, token or code."""
    return buyer_service.create_request(db, request.app.state.mailer, request.app.state.settings.buyer_portal_base_url,
                                        project, body)


@router.get("/{project_id}/buyer-requests", response_model=list[schemas.BuyerRequestView])
def list_buyer_requests(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return buyer_service.list_requests(db, project)


@router.post("/{project_id}/buyer-requests/{request_id}/cancel", response_model=schemas.BuyerRequestView,
             dependencies=[Depends(require_csrf)])
def cancel_buyer_request(request_id: str, project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return buyer_service.cancel_request(db, project, request_id)


@router.get("/{project_id}/buyer-decisions", response_model=list[schemas.BuyerDecisionView])
def list_buyer_decisions(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return buyer_service.list_decisions(db, project)


# --- scope analysis (Stage 3D): opinions that cite stored records, never facts ---------------------------------------

def limit_analysis(request: Request, seller: Seller = Depends(get_current_seller)) -> None:
    throttle = request.app.state.analysis_throttle
    if throttle.is_blocked(seller.id):
        raise ApiError(429, "too_many_attempts", "Too many analysis runs. Try again shortly.", headers={"Retry-After": "60"})
    throttle.record_failure(seller.id)


@router.post("/{project_id}/analysis", status_code=201, response_model=schemas.AnalysisRunView,
             dependencies=[Depends(require_csrf), Depends(limit_analysis)])
def run_analysis(request: Request, project: Project = Depends(get_owned_project),
                 db: Session = Depends(get_db)) -> schemas.AnalysisRunView:
    return analysis_service.run_analysis(db, request.app.state.analysis_provider, project)


@router.get("/{project_id}/analysis", response_model=list[schemas.AnalysisRunView])
def list_analysis(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return analysis_service.list_runs(db, project)


# --- evidence report (Stage 3E): read-only, deterministic ------------------------------------------------------------

@router.get("/{project_id}/evidence-report")
def evidence_report(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> dict:
    return report_service.build_report(db, project)


@router.get("/{project_id}/evidence-report/markdown")
def evidence_report_markdown(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> Response:
    text = render_markdown(report_service.build_report(db, project))
    return Response(text, media_type="text/markdown; charset=utf-8")
