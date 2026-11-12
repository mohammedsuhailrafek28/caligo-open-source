"""Alembic migrations: existing databases are upgraded in place (data kept), never reset or silently altered."""
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.config import Settings
from app.db import MIGRATIONS_DIR, SchemaOutOfDateError, current_revision, head_revision, init_db, make_engine
from app.main import create_app
from app.models import Base
from tests.helpers import login, register

NEW_TABLES = {"payment_instructions", "instruction_change_requests", "instruction_change_resolutions"}


def config_for(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture()
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'm.sqlite3'}"


def test_head_is_the_r1_revision():
    assert head_revision() == "0008"


def test_upgrade_keeps_existing_data_and_adds_the_r1_schema(url):
    command.upgrade(config_for(url), "0001")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sellers (id, email, password_hash, display_name, created_at) "
                          "VALUES ('s1', 'keep@example.com', 'x', 'Keep', '2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO projects (id, seller_id, title, status, created_at) "
                          "VALUES ('p1', 's1', 'Old project', 'DRAFT', '2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO payment_records (id, project_id, provider, idempotency_key, request_fingerprint, recipient_email, "
                          "currency, amount_minor, amount_paid_minor, amount_refunded_minor, provider_status, operation_state, "
                          "created_at, version) VALUES ('r1','p1','MOCK_PAYPAL','k','f','b@example.org','USD',500,0,0,'UNVERIFIED','READY',"
                          "'2026-01-01 00:00:00',1)"))
    assert not NEW_TABLES & set(inspect(engine).get_table_names())
    command.upgrade(config_for(url), "head")
    engine = make_engine(url)
    assert NEW_TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT email FROM sellers")).scalar() == "keep@example.com"
        assert conn.execute(text("SELECT title FROM projects")).scalar() == "Old project"
        row = conn.execute(text("SELECT amount_minor, instruction_id, instruction_fingerprint FROM payment_records")).one()
        assert tuple(row) == (500, None, None)  # a legacy invoice has no instruction and keeps working
    assert current_revision(engine) == head_revision()


def test_the_migrated_schema_equals_the_models(url):
    command.upgrade(config_for(url), "head")
    engine = make_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_the_append_only_triggers_exist_after_migration_and_match_a_fresh_database(url, tmp_path):
    command.upgrade(config_for(url), "head")
    fresh_url = f"sqlite:///{tmp_path / 'fresh.sqlite3'}"
    fresh = make_engine(fresh_url)
    init_db(fresh)

    def triggers(engine):
        with engine.connect() as conn:
            return {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    migrated = triggers(make_engine(url))
    assert migrated == triggers(fresh) and "payment_instructions_no_update" in migrated and "audit_events_no_delete" in migrated


def test_downgrade_returns_to_the_r0_schema(url):
    command.upgrade(config_for(url), "head")
    command.downgrade(config_for(url), "0001")
    engine = make_engine(url)
    assert not NEW_TABLES & set(inspect(engine).get_table_names())
    assert "instruction_id" not in {c["name"] for c in inspect(engine).get_columns("payment_records")}


def test_a_fresh_database_is_created_and_stamped_at_head(url):
    engine = make_engine(url)
    init_db(engine)
    assert current_revision(engine) == head_revision()
    init_db(engine)  # idempotent


def test_a_pre_migration_database_is_refused_not_modified(url):
    engine = make_engine(url)
    Base.metadata.create_all(engine)  # what R0 did: tables but no alembic_version
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE instruction_change_resolutions"))
        conn.execute(text("INSERT INTO sellers (id, email, password_hash, display_name, created_at) "
                          "VALUES ('s1', 'keep@example.com', 'x', 'Keep', '2026-01-01 00:00:00')"))
    with pytest.raises(SchemaOutOfDateError, match="alembic"):
        init_db(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sellers")).scalar() == 1
        assert "alembic_version" not in inspect(conn).get_table_names()


def test_the_api_answers_503_not_a_reset_when_the_schema_is_out_of_date(url):
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    app = create_app(Settings(database_url=url, paypal_mode="mock"))
    response = TestClient(app).post("/api/v1/auth/register", json={"email": "a@example.com", "password": "correct horse battery staple", "display_name": "A"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "schema_out_of_date"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sellers")).scalar() == 0


def test_the_one_active_invoice_index_and_foreign_keys_survive_the_r1_migration(url):
    command.upgrade(config_for(url), "head")
    engine = make_engine(url)
    with engine.connect() as conn:
        sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name='uq_payment_records_one_active_per_project'")).scalar()
        assert sql and "UNIQUE" in sql.upper() and "CANCELLED" in sql
    foreign = {fk["referred_table"] for fk in inspect(engine).get_foreign_keys("payment_records")}
    assert {"payment_instructions", "projects", "scope_records", "payment_records"} <= foreign
