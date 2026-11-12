"""Unified evidence report routes (R5). GET only, seller-authenticated, owner-scoped and read-throttled. Nothing a client sends can write."""
from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.deps import check_origin, get_current_seller, get_db, get_owned_project, limit_observation_reads
from app.models import Project
from app.services import unified_report
from app.services.unified_report_markdown import render_unified_markdown

REPORT_EXTRA_CHARGE = 2  # the report is several times heavier than a plain observation read: 3 throttle tokens in total


def charge_report_cost(request: Request, seller=Depends(get_current_seller)) -> None:
    throttle = request.app.state.observation_read_throttle
    for _ in range(REPORT_EXTRA_CHARGE):
        throttle.record_failure(seller.id)


router = APIRouter(prefix="/api/v1/projects", tags=["unified-report"], dependencies=[Depends(check_origin), Depends(limit_observation_reads), Depends(charge_report_cost)])


def _build(request: Request, project: Project, db: Session, limit: int, offset: int) -> dict:
    return unified_report.build_unified_report(
        db, project, request.app.state.payment_provider, required=request.app.state.settings.payment_instruction_required,
        payments_limit=limit, payments_offset=offset)


@router.get("/{project_id}/unified-report")
def unified_json(request: Request, payments_limit: int = Query(default=25, ge=1, le=50), payments_offset: int = Query(default=0, ge=0, le=100_000),
                 project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> dict:
    """One deterministic report over scope/delivery, instruction integrity and authorization, and payment observations and recovery cases.
    The payment sections cover one page of payments (`payments_limit`, `payments_offset`); `truncation` says what was cut."""
    return _build(request, project, db, payments_limit, payments_offset)


@router.get("/{project_id}/unified-report/markdown")
def unified_markdown(request: Request, payments_limit: int = Query(default=25, ge=1, le=50),
                     payments_offset: int = Query(default=0, ge=0, le=100_000), project: Project = Depends(get_owned_project),
                     db: Session = Depends(get_db)) -> Response:
    return Response(render_unified_markdown(_build(request, project, db, payments_limit, payments_offset)),
                    media_type="text/markdown; charset=utf-8")
