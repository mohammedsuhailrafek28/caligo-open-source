"""Payee destination registry and advisory screening. Seller-authenticated, owner-scoped (404 for another seller), CSRF on writes.

Authority: registry rows are SELLER_ASSERTED history. These routes cannot create, release or override a payment hold, change the payment
instruction, call a provider, or record a buyer decision. `screen` is read-only (a POST only because it carries a body)."""
from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from app import schemas
from app.deps import check_origin, get_current_seller, get_db, get_owned_project, limit_observation_reads, require_csrf
from app.models import Project, Seller
from app.services import payee_registry as service

router = APIRouter(prefix="/api/v1/projects/{project_id}/payee-registry", tags=["payee-registry"], dependencies=[Depends(check_origin)])
PAYEE_ID = Path(max_length=36, pattern=r"^[0-9a-f-]{36}$")


@router.get("", response_model=list[schemas.PayeeVersionView], dependencies=[Depends(limit_observation_reads)])
def list_payees(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    """The current (latest) version of every payee."""
    return service.list_payees(db, project)


@router.post("", response_model=schemas.PayeeVersionView, status_code=201, dependencies=[Depends(require_csrf), Depends(limit_observation_reads)])
def register_payee(body: schemas.PayeeCreate, project: Project = Depends(get_owned_project), seller: Seller = Depends(get_current_seller),
                   db: Session = Depends(get_db)):
    return service.register_payee(db, project, seller, body.label, body.destination, body.note)


@router.post("/screen", response_model=schemas.PayeeScreenResponse, dependencies=[Depends(require_csrf), Depends(limit_observation_reads)])
def screen(body: schemas.PayeeScreenRequest, project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    """Advisory, read-only check of a claimed destination, amount or invoice text against the registry and the payment instruction in force."""
    return service.screen(db, project, payee_id=body.payee_id, destination=body.destination, amount_minor=body.amount_minor,
                          currency=body.currency, text=body.text)


@router.get("/{payee_id}", response_model=schemas.PayeeView, dependencies=[Depends(limit_observation_reads)])
def get_payee(payee_id: str = PAYEE_ID, project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    return service.get_payee(db, project, payee_id)


@router.post("/{payee_id}/versions", response_model=schemas.PayeeVersionView, status_code=201,
             dependencies=[Depends(require_csrf), Depends(limit_observation_reads)])
def change_destination(body: schemas.PayeeDestinationChange, payee_id: str = PAYEE_ID, project: Project = Depends(get_owned_project),
                       seller: Seller = Depends(get_current_seller), db: Session = Depends(get_db)):
    """Record a changed destination as a NEW version. It is the seller's assertion: it does not verify anything and never clears a hold."""
    return service.change_destination(db, project, seller, payee_id, body.destination, body.note)
