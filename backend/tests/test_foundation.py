import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect

from app.config import Settings
from app.db import init_db, make_engine
from app.main import create_app
from app.schemas import (
    DeliveryEventType, EvidenceFinding, EvidenceStatus, PaymentLink, ProjectDraft, ScopeAnalysis,
)


def test_health_reports_integration_modes():
    response = TestClient(create_app(Settings(paypal_mode="disabled"))).get("/health")
    assert response.status_code == 200
    assert response.json()["paypal_mode"] == "disabled"
    assert response.json()["ai_mode"] == "disabled"


def test_business_routes_require_auth_and_unbuilt_routes_do_not_exist():
    client = TestClient(create_app(Settings()))
    assert client.get("/api/v1/projects").status_code == 401
    assert client.post("/api/v1/disputes/abc/submit").status_code == 404  # dispute submission is never built
    assert client.get("/api/v1/capabilities").json()["paypal_invoicing"] is False  # real PayPal: not implemented


def test_amount_must_be_positive_integer_minor_units():
    assert PaymentLink(project_id="demo", currency="USD", amount_minor=80000).amount_minor == 80000
    for bad in (0, -5, 800.5, 80000.0, "80000"):
        with pytest.raises(ValidationError):
            PaymentLink(project_id="demo", currency="USD", amount_minor=bad)


@pytest.mark.parametrize("currency", ["usd", "US", "USDX", "12$"])
def test_currency_must_be_uppercase_iso_shape(currency):
    with pytest.raises(ValidationError):
        PaymentLink(project_id="demo", currency=currency, amount_minor=100)


def test_client_cannot_assert_unknown_payment_status():
    with pytest.raises(ValidationError):
        PaymentLink(project_id="demo", currency="USD", amount_minor=100, provider_status="PAYED")


def test_project_input_has_no_client_supplied_owner():
    assert "seller_id" not in ProjectDraft.model_fields
    assert "seller_id" not in ProjectDraft(title="Site", seller_id="someone-else").model_dump()


def test_delivery_event_types_are_distinct_and_closed():
    assert len({e.value for e in DeliveryEventType}) == 4
    with pytest.raises(ValueError):
        DeliveryEventType("LINK_OPENED")


def test_supported_finding_requires_source_refs():
    with pytest.raises(ValidationError):
        EvidenceFinding(claim="Buyer accepted delivery", status=EvidenceStatus.SUPPORTED,
                        source_refs=[], explanation="no sources")
    ok = EvidenceFinding(claim="Buyer accepted delivery", status=EvidenceStatus.UNSUPPORTED,
                         source_refs=[], explanation="No explicit acceptance")
    assert ok.source_refs == []


def test_human_review_cannot_be_disabled():
    with pytest.raises(ValidationError):
        ScopeAnalysis(project_id="p", findings=[], human_review_required=False)


def test_secrets_do_not_leak_through_repr():
    settings = Settings(paypal_client_secret="placeholder-secret-value", ai_api_key="placeholder-key-value")
    text = repr(settings) + str(settings)
    assert "placeholder-secret-value" not in text
    assert "placeholder-key-value" not in text


def test_unsafe_production_settings_are_rejected():
    with pytest.raises(ValidationError):
        Settings(app_env="production", dev_auth_enabled=True, cookie_secure=True)
    with pytest.raises(ValidationError):
        Settings(app_env="production", cookie_secure=False)
    with pytest.raises(ValidationError):
        Settings(app_env="production", cookie_secure=True, paypal_mode="mock")
    with pytest.raises(ValidationError):
        Settings(cors_origins="*")
    assert Settings(app_env="production", cookie_secure=True).cookie_secure is True
    assert Settings(app_env="development", dev_auth_enabled=True).dev_auth_enabled is True


def test_integrations_cannot_be_enabled_by_configuration():
    assert Settings(paypal_mode="mock").paypal_mode == "mock"
    for bad in ("sandbox", "live", "enabled"):
        with pytest.raises(ValidationError):
            Settings(paypal_mode=bad)
    with pytest.raises(ValidationError):
        Settings(ai_mode="enabled")


def test_database_tables_created(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    init_db(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"sellers", "projects", "scope_records", "payment_records", "deliverables",
            "delivery_events", "evidence_claims"}.issubset(tables)
    engine.dispose()
