import hashlib
import re

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas
from app.deps import (
    check_origin, get_current_seller, get_db, get_owned_project, get_payment_provider, limit_payment_ops,
    require_csrf,
)
from app.errors import ApiError
from app.models import PaymentEvent, PaymentRecord, Project, ScopeRecord, Seller
from app.payment_provider import PaymentProvider
from app.services import buyer as buyer_service
from app.services import payments as payment_service

router = APIRouter(prefix="/api/v1/projects", tags=["projects"], dependencies=[Depends(check_origin)])
_IDEMPOTENCY_KEY = re.compile(schemas.IDEMPOTENCY_KEY_PATTERN)


@router.post("", status_code=201, response_model=schemas.ProjectView, dependencies=[Depends(require_csrf)])
def create_project(body: schemas.ProjectDraft, seller: Seller = Depends(get_current_seller),
                   db: Session = Depends(get_db)) -> Project:
    project = Project(seller_id=seller.id, title=body.title.strip())  # owner comes from the session only
    db.add(project)
    db.commit()
    return project


@router.get("", response_model=list[schemas.ProjectView])
def list_projects(seller: Seller = Depends(get_current_seller), db: Session = Depends(get_db)) -> list[Project]:
    return list(db.scalars(select(Project).where(Project.seller_id == seller.id)
                           .order_by(Project.created_at.desc(), Project.id)))


@router.get("/{project_id}", response_model=schemas.ProjectDetail)
def get_project(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> schemas.ProjectDetail:
    return schemas.ProjectDetail(
        **schemas.ProjectView.model_validate(project).model_dump(),
        payments=[schemas.PaymentLink.model_validate(r) for r in payment_service.list_payments(db, project)],
        delivery_status=buyer_service.delivery_status(db, project),
    )


# --- scope records (append-only) ----------------------------------------------------

@router.post("/{project_id}/scope-records", status_code=201, response_model=schemas.ScopeRecordView,
             dependencies=[Depends(require_csrf)])
def add_scope_record(body: schemas.ScopeRecordCreate, project: Project = Depends(get_owned_project),
                     db: Session = Depends(get_db)) -> ScopeRecord:
    record = ScopeRecord(
        project_id=project.id, kind=body.kind.value, source_label=body.source_label.strip(),
        content=body.content, source_sha256=hashlib.sha256(body.content.encode("utf8")).hexdigest(),
    )
    db.add(record)
    db.commit()
    return record


@router.get("/{project_id}/scope-records", response_model=list[schemas.ScopeRecordView])
def list_scope_records(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> list[ScopeRecord]:
    return list(db.scalars(select(ScopeRecord).where(ScopeRecord.project_id == project.id)
                           .order_by(ScopeRecord.created_at, ScopeRecord.id)))


# --- payments (mock or sandbox provider) -------------------------------------------

def _allowlist(request: Request) -> list[str]:
    return request.app.state.settings.sandbox_recipient_list


def _required(request: Request) -> bool:
    return request.app.state.settings.payment_instruction_required


def _owned_payment(db: Session, project: Project, payment_id: str) -> PaymentRecord:
    rec = db.scalar(select(PaymentRecord).where(PaymentRecord.id == payment_id, PaymentRecord.project_id == project.id))
    if rec is None:
        raise ApiError(404, "not_found", "Request could not be processed.")
    return rec


@router.post("/{project_id}/invoices", response_model=schemas.PaymentLink,
             dependencies=[Depends(require_csrf), Depends(limit_payment_ops)],
             responses={201: {"model": schemas.PaymentLink}})
def create_invoice(
    body: schemas.InvoiceRequest, response: Response, request: Request,
    project: Project = Depends(get_owned_project), db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PaymentRecord:
    """Creates a DRAFT invoice. Nobody is emailed; sending is a separate explicit action."""
    if idempotency_key is None or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise ApiError(400, "idempotency_key_invalid",
                       "An Idempotency-Key header of 8-64 characters [A-Za-z0-9_-] is required.")
    record, created = payment_service.create_invoice(
        db, provider, project, body, idempotency_key, recipient_allowlist=_allowlist(request),
        instruction_required=_required(request))
    response.status_code = 201 if created else 200
    return record


@router.post("/{project_id}/payments/{payment_id}/send", response_model=schemas.PaymentLink,
             dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def send_invoice(
    payment_id: str, body: schemas.SendInvoiceRequest, request: Request,
    project: Project = Depends(get_owned_project), db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> PaymentRecord:
    rec = _owned_payment(db, project, payment_id)
    return payment_service.send_invoice(db, provider, project, rec, body, recipient_allowlist=_allowlist(request),
                                       instruction_required=_required(request))


@router.post("/{project_id}/payments/{payment_id}/resolve-review", response_model=schemas.PaymentLink,
             dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def resolve_review(
    payment_id: str, body: schemas.ReviewResolutionRequest, request: Request,
    project: Project = Depends(get_owned_project), db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> PaymentRecord:
    """Explicit, audited exit from NEEDS_REVIEW. Re-fetches provider evidence; contradictory evidence is refused."""
    rec = _owned_payment(db, project, payment_id)
    return payment_service.resolve_review(db, provider, project, rec, body, instruction_required=_required(request))


@router.get("/{project_id}/payments", response_model=list[schemas.PaymentLink])
def get_payments(project: Project = Depends(get_owned_project), db: Session = Depends(get_db)) -> list[PaymentRecord]:
    """Strictly read-only: returns stored state only. It never contacts the provider and never writes."""
    return payment_service.list_payments(db, project)


@router.post("/{project_id}/payments/sync", response_model=list[schemas.PaymentLink],
             dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def sync_payments(project: Project = Depends(get_owned_project), db: Session = Depends(get_db),
                  provider: PaymentProvider = Depends(get_payment_provider)) -> list[PaymentRecord]:
    """Explicit server-side reconciliation: fetches each invoice from the provider and applies verified evidence."""
    return payment_service.sync_project_payments(db, provider, project)


@router.get("/{project_id}/payments/{payment_id}/events", response_model=list[schemas.PaymentEventView])
def payment_events(payment_id: str, project: Project = Depends(get_owned_project),
                   db: Session = Depends(get_db)) -> list[PaymentEvent]:
    rec = _owned_payment(db, project, payment_id)
    return list(db.scalars(select(PaymentEvent).where(PaymentEvent.payment_id == rec.id)
                           .order_by(PaymentEvent.created_at, PaymentEvent.id)))
