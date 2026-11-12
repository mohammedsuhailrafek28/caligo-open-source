"""Recovery-case routes (R4). Read-only, seller-authenticated and owner-scoped. There is deliberately no POST/PUT/PATCH/DELETE here:
cases are derived on every read from stored records, are advisory, and nothing a client sends can create, change or resolve one."""
from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app import schemas
from app.deps import check_origin, get_db, get_owned_project, limit_observation_reads
from app.errors import ApiError
from app.models import Project
from app.services import recovery

router = APIRouter(prefix="/api/v1/projects", tags=["recovery-cases"],
                   dependencies=[Depends(check_origin), Depends(limit_observation_reads)])


def _required(request: Request) -> bool:
    return request.app.state.settings.payment_instruction_required


@router.get("/{project_id}/recovery-cases", response_model=schemas.RecoveryCaseList)
def list_cases(request: Request, limit: int = Query(default=25, ge=1, le=50), offset: int = Query(default=0, ge=0, le=100_000),
               category: schemas.RecoveryCategory | None = None, severity: schemas.FindingSeverity | None = None,
               project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    """Advisory recovery cases for one page of the project's payments (plus project-level instruction cases on the first page)."""
    return recovery.build_cases(db, project, request.app.state.payment_provider, required=_required(request), limit=limit, offset=offset,
                                category=category, severity=severity)


@router.get("/{project_id}/recovery-cases/{case_id}", response_model=schemas.RecoveryCase)
def get_case(request: Request, case_id: str = Path(pattern=r"^rc_[0-9a-f]{24}$"), project: Project = Depends(get_owned_project),
             db: Session = Depends(get_db)):
    case = recovery.find_case(db, project, request.app.state.payment_provider, required=_required(request), case_id=case_id)
    if case is None:  # unknown, resolved since, or another project's id: one answer for all
        raise ApiError(404, "not_found", "No such recovery case.")
    return case
