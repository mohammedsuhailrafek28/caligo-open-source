"""Payment observation and merchant-ledger routes (R3). Seller-authenticated and owner-scoped like every project route.

Everything a seller submits here is a CLAIM: the source and confidence are chosen by the server, never by the request. No route
creates provider evidence, changes a payment status, verifies a payment or touches a payment-instruction hold."""
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app import schemas
from app.deps import check_origin, get_db, get_owned_project, limit_observation_reads, limit_payment_ops, require_csrf
from app.models import Project
from app.services import observation_compare, observations as service

router = APIRouter(prefix="/api/v1/projects", tags=["payment-observations"], dependencies=[Depends(check_origin)])


@router.post("/{project_id}/merchant-ledger/imports", response_model=schemas.MerchantLedgerImportView,
             dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def import_ledger(body: schemas.MerchantLedgerImportRequest, response: Response, project: Project = Depends(get_owned_project),
                  db: Session = Depends(get_db)) -> schemas.MerchantLedgerImportView:
    batch, created = service.import_ledger(db, project, body.csv)
    response.status_code = 201 if created else 200
    return schemas.MerchantLedgerImportView.model_validate(batch).model_copy(update={"replay": not created})


@router.get("/{project_id}/merchant-ledger/imports", response_model=list[schemas.MerchantLedgerImportView],
            dependencies=[Depends(limit_observation_reads)])
def list_imports(response: Response, limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0, le=100_000),
                 project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    rows, total = service.list_imports(db, project.id, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return rows


@router.post("/{project_id}/payment-observations", response_model=schemas.SellerObservationResult,
             dependencies=[Depends(require_csrf), Depends(limit_payment_ops)])
def record_claim(body: schemas.SellerObservationCreate, response: Response, project: Project = Depends(get_owned_project),
                 db: Session = Depends(get_db)) -> schemas.SellerObservationResult:
    observation, created = service.record_seller_entry(db, project, body)
    response.status_code = 201 if created else 200
    return schemas.SellerObservationResult(observation=schemas.PaymentObservationView.model_validate(observation), created=created)


@router.get("/{project_id}/payment-observations", response_model=list[schemas.PaymentObservationView],
            dependencies=[Depends(limit_observation_reads)])
def list_observations(response: Response, source: schemas.ObservationSource | None = None,
                      payment_id: str | None = Query(default=None, max_length=36), limit: int = Query(default=100, ge=1, le=200),
                      offset: int = Query(default=0, ge=0, le=100_000), project: Project = Depends(get_owned_project),
                      db: Session = Depends(get_db)):
    rows, total = service.list_observations(db, project.id, source=source, payment_id=payment_id, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return service.with_provider_provenance(db, rows)


@router.get("/{project_id}/observation-comparison", response_model=schemas.ObservationComparison,
            dependencies=[Depends(limit_observation_reads)])
def get_comparison(limit: int = Query(default=25, ge=1, le=50), offset: int = Query(default=0, ge=0, le=100_000),
                   project: Project = Depends(get_owned_project), db: Session = Depends(get_db)):
    """Read-only, one page of payments: claims beside provider evidence, with source-cited findings. It never changes a status."""
    return observation_compare.compare_project(db, project.id, limit=limit, offset=offset)
