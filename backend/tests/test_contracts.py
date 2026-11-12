"""Contract-consistency tests: JSON schemas, API_CONTRACT.md, Pydantic schemas and
SQLAlchemy models must agree. Pydantic enums/models are the source of truth."""
import importlib.util
import re
from datetime import timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.db import init_db, make_engine
from app.config import Settings
from app.main import create_app

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "contracts"
API_MD = (CONTRACTS / "API_CONTRACT.md").read_text(encoding="utf8")

_spec = importlib.util.spec_from_file_location("export_contracts", REPO / "scripts" / "export_contracts.py")
export_contracts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_contracts)

CANONICAL_ENUMS = {
    "ProjectStatus": schemas.ProjectStatus,
    "ScopeKind": schemas.ScopeKind,
    "PaymentProviderStatus": schemas.PaymentProviderStatus,
    "PaymentProviderName": schemas.PaymentProviderName,
    "DeliveryEventType": schemas.DeliveryEventType,
    "EvidenceStatus": schemas.EvidenceStatus,
}


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'c.sqlite3'}")
    init_db(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


# --- committed JSON schemas vs Pydantic models ---------------------------------------

@pytest.mark.parametrize("name", sorted(export_contracts.CONTRACT_MODELS))
def test_committed_json_schema_matches_pydantic_model(name):
    committed = (CONTRACTS / f"{name}.schema.json").read_text(encoding="utf8")
    assert committed == export_contracts.render(export_contracts.CONTRACT_MODELS[name]), (
        f"{name}.schema.json is stale; run scripts/export_contracts.py")


def test_no_orphan_schema_files():
    on_disk = {p.name.removesuffix(".schema.json") for p in CONTRACTS.glob("*.schema.json")}
    assert on_disk == set(export_contracts.CONTRACT_MODELS)


# --- API_CONTRACT.md vs Pydantic enums ------------------------------------------------

@pytest.mark.parametrize("name,enum_cls", CANONICAL_ENUMS.items())
def test_documented_enum_matches_code(name, enum_cls):
    match = re.search(rf"`{name}`:\s*`([A-Z_ |]+)`", API_MD)
    assert match, f"{name} missing from API_CONTRACT.md canonical enums"
    documented = [v.strip() for v in match.group(1).split("|")]
    assert documented == [m.value for m in enum_cls]


def test_only_documented_implemented_routes_exist():
    app = create_app(Settings())
    implemented_doc = set(re.findall(r"\| (GET|POST) \| `([^`]+)` .*\| (?:Step 1|Stage 2B|Stage 2C|Stage 3[A-Z]?|Stage R[12345]|Stage S2) implemented \|", API_MD))
    served = {(method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops}
    assert served == implemented_doc


def test_documented_but_unbuilt_routes_are_not_served():
    app = create_app(Settings())
    planned = set(re.findall(r"\| (GET|POST) \| `([^`]+)` .*\| (?:Gate [0-9]|Optional Gate 5)", API_MD))
    served = {(method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops}
    assert planned and not planned & served


# --- Pydantic enums vs SQLAlchemy CHECK constraints ----------------------------------

def _seed_project(s):
    seller = models.Seller(email="demo@example.com", password_hash="$argon2id$stub", display_name="Demo Seller")
    s.add(seller)
    s.flush()
    project = models.Project(seller_id=seller.id, title="Demo")
    s.add(project)
    s.flush()
    return project


def _payment(p, **overrides):
    values = dict(project_id=p.id, provider="MOCK_PAYPAL", idempotency_key="key-" + str(len(overrides)) + overrides.get("tag", ""),
                  request_fingerprint="f" * 64, recipient_email="b@example.org", currency="USD", amount_minor=100)
    values.update({k: v for k, v in overrides.items() if k != "tag"})
    return models.PaymentRecord(**values)


@pytest.mark.parametrize("build", [
    lambda p: models.Project(seller_id=p.seller_id, title="x", status="BOGUS"),
    lambda p: models.ScopeRecord(project_id=p.id, kind="BOGUS", content="c", source_label="l"),
    lambda p: _payment(p, provider_status="BOGUS"),
    lambda p: _payment(p, provider="STRIPE"),
    lambda p: _payment(p, currency="usd"),
    lambda p: _payment(p, amount_minor=0),
    lambda p: models.EvidenceClaim(project_id=p.id, claim="c", status="BOGUS"),
])
def test_database_rejects_values_outside_contract(session, build):
    project = _seed_project(session)
    session.add(build(project))
    with pytest.raises(IntegrityError):
        session.flush()


def test_database_rejects_unknown_event_type(session):
    project = _seed_project(session)
    deliverable = models.Deliverable(project_id=project.id, name="a.zip", sha256="0" * 64, storage_key="k")
    session.add(deliverable)
    session.flush()
    session.add(models.DeliveryEvent(deliverable_id=deliverable.id, event_type="LINK_OPENED",
                                     actor_type="SYSTEM", auth_method="SYSTEM"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_every_enum_member_is_storable(session):
    project = _seed_project(session)
    for kind in schemas.ScopeKind:
        session.add(models.ScopeRecord(project_id=project.id, kind=kind.value, content="c", source_label="l"))
    for status in schemas.PaymentProviderStatus:  # one project each: only one non-cancelled invoice is allowed
        other = models.Project(seller_id=project.seller_id, title=f"p-{status.value}")
        session.add(other)
        session.flush()
        session.add(_payment(other, provider_status=status.value, idempotency_key=f"key-{status.value}"))
    for status in schemas.EvidenceStatus:
        session.add(models.EvidenceClaim(project_id=project.id, claim="c", status=status.value))
    session.flush()  # no IntegrityError


def test_foreign_keys_are_enforced_on_sqlite(session):
    session.add(models.Project(seller_id="no-such-seller", title="orphan"))
    with pytest.raises(IntegrityError):
        session.flush()


# --- Pydantic views vs SQLAlchemy columns --------------------------------------------

@pytest.mark.parametrize("view,model", [
    (schemas.ProjectView, models.Project),
    (schemas.PaymentLink, models.PaymentRecord),
    (schemas.DeliveryEventView, models.DeliveryEvent),
    (schemas.SellerView, models.Seller),
    (schemas.ScopeRecordView, models.ScopeRecord),
])
def test_view_fields_exist_as_model_columns(view, model):
    columns = {c.key for c in model.__table__.columns}
    assert set(view.model_fields) <= columns


def test_project_view_round_trips_orm_row_with_utc_datetime(session):
    project = _seed_project(session)
    session.commit()
    session.expire_all()
    row = session.scalars(select(models.Project)).one()
    view = schemas.ProjectView.model_validate(row)
    assert view.created_at.utcoffset() == timedelta(0)
    assert row.created_at.tzinfo is timezone.utc
    assert view.status is schemas.ProjectStatus.DRAFT
    assert view.id == project.id


def test_naive_datetimes_are_rejected_by_storage(session):
    from datetime import datetime
    seller = models.Seller(email="n@example.com", password_hash="x", display_name="x", created_at=datetime(2026, 1, 1))
    session.add(seller)
    with pytest.raises(Exception):
        session.flush()


def test_committed_openapi_document_matches_the_running_app():
    """contracts/openapi.json is what the frontend generates clients from; it must not drift from the code."""
    committed = (CONTRACTS / "openapi.json").read_text(encoding="utf8")
    assert committed == export_contracts.render_openapi(), "run: backend/.venv/Scripts/python scripts/export_contracts.py"
