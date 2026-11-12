"""Payment instruction and hold routes (R1). Seller-authenticated and owner-scoped like every project route.

There is deliberately NO approve, release or override route: a pending change is resolved by buyer-side verification with an enrolled second channel (R2),
and the only thing the seller can do to a pending proposal is withdraw it, which leaves the instruction in force unchanged."""
import re

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app import schemas
from app.deps import check_origin, get_db, get_owned_project, get_payment_provider, limit_payment_ops, require_csrf
from app.errors import ApiError
from app.models import InstructionChangeRequest, PaymentInstruction, Project
from app.payment_provider import PaymentProvider
from app.services import authenticators as authenticator_service
from app.services import instructions as service

router = APIRouter(prefix="/api/v1/projects", tags=["payment-instruction"], dependencies=[Depends(check_origin)])
_KEY = re.compile(schemas.IDEMPOTENCY_KEY_PATTERN)


def _key(value: str | None) -> str:
    if value is None or not _KEY.fullmatch(value):
        raise ApiError(400, "idempotency_key_invalid", "An Idempotency-Key header of 8-64 characters [A-Za-z0-9_-] is required.")
    return value


def _required(request: Request) -> bool:
    return request.app.state.settings.payment_instruction_required


@router.get("/{project_id}/payment-instruction", response_model=schemas.PaymentInstructionState)
def get_instruction(request: Request, project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return service.state(db, project, request.app.state.payment_provider, required=_required(request))


@router.get("/{project_id}/payment-hold", response_model=schemas.HoldStatus)
def get_hold(request: Request, project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return service.compute_hold(db, project.id, request.app.state.payment_provider, required=_required(request))


@router.get("/{project_id}/payment-instruction/versions", response_model=list[schemas.PaymentInstructionView])
def list_versions(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return [service.instruction_view(db, v) for v in service.versions_of(db, project.id)]


@router.post("/{project_id}/payment-instruction", response_model=schemas.PaymentInstructionView,
             dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def record_instruction(body: schemas.PaymentInstructionCreate, response: Response,
                       project: Project = Depends(get_owned_project), db: Session = Depends(get_db),
                       provider: PaymentProvider = Depends(get_payment_provider),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> PaymentInstruction:
    row, created = service.record_initial(db, project, provider, body, _key(idempotency_key))
    response.status_code = 201 if created else 200
    return row


@router.get("/{project_id}/payment-instruction/changes", response_model=list[schemas.InstructionChangeView])
def list_changes(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return [service.change_view(db, c) for c in service.all_changes(db, project.id)]


@router.post("/{project_id}/payment-instruction/changes", response_model=schemas.InstructionChangeView,
             dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def propose_change(body: schemas.InstructionChangeCreate, response: Response,
                   project: Project = Depends(get_owned_project), db: Session = Depends(get_db),
                   provider: PaymentProvider = Depends(get_payment_provider),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    change, created = service.propose_change(db, project, provider, body, _key(idempotency_key))
    response.status_code = 201 if created else 200
    return service.change_view(db, change)


@router.post("/{project_id}/payment-instruction/changes/{change_id}/withdraw", response_model=schemas.InstructionChangeView,
             dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def withdraw_change(change_id: str, body: schemas.InstructionChangeWithdraw,
                    project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    change: InstructionChangeRequest = service.withdraw_change(db, project, change_id, body)
    return service.change_view(db, change)


@router.get("/{project_id}/buyer-authenticators", response_model=list[schemas.BuyerAuthenticatorView])
def list_buyer_authenticators(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    """Read-only. The seller can see that a second channel exists (masked address, status, trust basis) - never a secret, and there is
    no seller route to enrol, replace, reset or reveal one (operator-only, scripts/enroll_buyer_authenticator.py)."""
    return [authenticator_service.view_of(db, row) for row in authenticator_service.list_for_project(db, project.id)]
