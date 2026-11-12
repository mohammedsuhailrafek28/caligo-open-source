"""Adopting a database that predates Alembic: fail closed unless it is EXACTLY the R0 schema (reference generated from commit 6f60916).

The R0 database in these tests is built from the stored R0 DDL, i.e. what the R0 code itself emitted, not from migration 0001."""
import sqlite3
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app import schema_guard
from app.db import MIGRATIONS_DIR, make_engine
from app.schema_guard import SchemaIncompatibleError

NOW = "2026-01-01 00:00:00"


def cfg(url: str, connection=None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url)
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def objects_in_creation_order():
    ref = schema_guard.reference_objects()
    order = {"table": 0, "index": 1, "trigger": 2}
    return [(t, n, sql) for (t, n), sql in sorted(ref.items(), key=lambda kv: (order[kv[0][0]], kv[0][1]))]


def build_r0(path, populate=True):
    conn = sqlite3.connect(path)
    for _t, _n, sql in objects_in_creation_order():
        conn.execute(sql)
    if populate:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"INSERT INTO sellers (id, email, password_hash, display_name, created_at) VALUES ('s1','keep@example.com','x','Keep','{NOW}')")
        conn.execute(f"INSERT INTO projects (id, seller_id, title, status, created_at) VALUES ('p1','s1','Old project','INVOICED','{NOW}')")
        conn.execute("INSERT INTO payment_records (id, project_id, provider, idempotency_key, request_fingerprint, recipient_email, currency, "
                     "amount_minor, amount_paid_minor, amount_refunded_minor, provider_status, operation_state, created_at, version) "
                     f"VALUES ('r1','p1','MOCK_PAYPAL','k','f','b@example.org','USD',500,0,0,'PENDING','READY','{NOW}',3)")
        conn.execute("INSERT INTO payment_events (id, payment_id, project_id, event_type, actor, created_at) "
                     f"VALUES ('e1','r1','p1','INVOICE_CREATED','SELLER','{NOW}')")
        conn.execute("INSERT INTO audit_events (id, project_id, event_type, actor_role, subject_type, subject_id, detail, created_at) "
                     f"VALUES ('a1','p1','BASELINE_FROZEN','SELLER','baseline','b1','{{}}','{NOW}')")
    conn.commit()
    conn.close()


@pytest.fixture()
def r0(tmp_path):
    path = tmp_path / "r0.sqlite3"
    build_r0(path)
    return f"sqlite:///{path}", path


def table_names(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def scalar(path, sql):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql).fetchone()[0]


# --- the reference itself ----------------------------------------------------------------------------------------------

def test_the_reference_is_the_r0_schema_with_every_object_kind():
    ref = schema_guard.reference_objects()
    kinds = [t for (t, _n) in ref]
    assert kinds.count("table") == 18 and kinds.count("trigger") == 18 and kinds.count("index") == 23
    assert ("table", "payment_instructions") not in ref  # R1 objects are not part of R0


def structure(url):
    """Dialect-level structure (not DDL text, which alembic formats differently): columns, keys, indexes, checks, triggers."""
    engine = create_engine(url)
    insp = inspect(engine)
    out = {}
    for table in sorted(insp.get_table_names()):
        if table == "alembic_version":
            continue
        out[table] = {
            "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(table)],
            "pk": insp.get_pk_constraint(table)["constrained_columns"],
            "fks": sorted((tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"])) for f in insp.get_foreign_keys(table)),
            "uniques": sorted((u["name"] or "", tuple(u["column_names"])) for u in insp.get_unique_constraints(table)),
            "indexes": sorted((i["name"] or "", tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)),
            "checks": sorted((c["name"] or "", " ".join(c["sqltext"].split())) for c in insp.get_check_constraints(table)),
        }
    with engine.connect() as conn:
        out["__triggers__"] = sorted(r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'"))
    engine.dispose()
    return out


def test_migration_0001_is_structurally_the_r0_schema(tmp_path):
    """The migration's DDL text differs from create_all's, so equality is checked structurally against a database built from the R0 DDL."""
    migrated_url = f"sqlite:///{tmp_path / 'm.sqlite3'}"
    command.upgrade(cfg(migrated_url), "0001")
    r0_path = tmp_path / "r0.sqlite3"
    build_r0(r0_path, populate=False)
    assert structure(migrated_url) == structure(f"sqlite:///{r0_path}")


# --- adopting a populated R0 database ------------------------------------------------------------------------------------

def test_an_exact_r0_database_is_adopted_and_upgraded_with_all_records_preserved(r0):
    url, path = r0
    command.stamp(cfg(url), "0001")
    assert scalar(path, "SELECT version_num FROM alembic_version") == "0001"
    command.upgrade(cfg(url), "head")
    assert scalar(path, "SELECT version_num FROM alembic_version") == "0008"
    assert {"payment_instructions", "instruction_change_requests", "instruction_change_resolutions"} <= table_names(path)
    assert scalar(path, "SELECT email FROM sellers") == "keep@example.com"
    assert scalar(path, "SELECT title FROM projects") == "Old project"
    assert scalar(path, "SELECT amount_minor || ':' || version || ':' || provider_status FROM payment_records") == "500:3:PENDING"
    assert scalar(path, "SELECT count(*) FROM payment_events WHERE payment_id='r1'") == 1  # the child rows survived the table rebuild
    assert scalar(path, "SELECT count(*) FROM audit_events") == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT instruction_id, instruction_fingerprint FROM payment_records").fetchone() == (None, None)
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "payment_instructions_no_update" in triggers and "payment_events_no_delete" in triggers


def test_the_adopted_and_upgraded_schema_matches_the_models(r0):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from app.models import Base
    url, _path = r0
    command.stamp(cfg(url), "0001")
    command.upgrade(cfg(url), "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_a_populated_database_survives_the_table_rebuild_when_a_connection_with_foreign_keys_on_is_never_used(r0):
    """The env creates its own connection with foreign keys OFF; the rebuilt payment_records keeps its self-reference and children."""
    url, path = r0
    command.stamp(cfg(url), "0001")
    with sqlite3.connect(path) as conn:  # a self-referencing replacement plus a child, to exercise the rebuild's foreign keys
        conn.execute("INSERT INTO payment_records (id, project_id, provider, idempotency_key, request_fingerprint, recipient_email, currency, "
                     "amount_minor, amount_paid_minor, amount_refunded_minor, provider_status, operation_state, created_at, version, replaces_payment_id) "
                     f"VALUES ('r2','p1','MOCK_PAYPAL','k2','f2','b@example.org','USD',600,0,0,'CANCELLED','READY','{NOW}',1,'r1')")
    command.upgrade(cfg(url), "head")
    assert scalar(path, "SELECT replaces_payment_id FROM payment_records WHERE id='r2'") == "r1"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- refusals: nothing stamped, nothing changed -----------------------------------------------------------------------------

def _drop_table(conn):
    conn.execute("DROP TABLE deliverables")


def _extra_table(conn):
    conn.execute("CREATE TABLE rogue (id INTEGER PRIMARY KEY)")


def _extra_column(conn):
    conn.execute("ALTER TABLE sellers ADD COLUMN is_admin BOOLEAN")


def _missing_trigger(conn):
    conn.execute("DROP TRIGGER audit_events_no_update")


def _changed_trigger(conn):
    conn.execute("DROP TRIGGER audit_events_no_update")
    conn.execute("CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT 1; END")


def _missing_index(conn):
    conn.execute("DROP INDEX ix_payment_records_project_id")


def _extra_index(conn):
    conn.execute("CREATE INDEX ix_rogue ON sellers (display_name)")


def _weaker_partial_index(conn):
    conn.execute("DROP INDEX uq_payment_records_one_active_per_project")
    conn.execute("CREATE UNIQUE INDEX uq_payment_records_one_active_per_project ON payment_records (project_id)")


def _already_has_r1_objects(conn):
    conn.execute("CREATE TABLE payment_instructions (id VARCHAR(36) NOT NULL, PRIMARY KEY (id))")


def _dangling_foreign_key(conn):
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(f"INSERT INTO payment_events (id, payment_id, project_id, event_type, actor, created_at) "
                 f"VALUES ('orphan','no-such-payment','p1','INVOICE_CREATED','SELLER','{NOW}')")


@pytest.mark.parametrize("damage", [_drop_table, _extra_table, _extra_column, _missing_trigger, _changed_trigger, _missing_index,
                                    _extra_index, _weaker_partial_index, _already_has_r1_objects, _dangling_foreign_key])
def test_an_incompatible_pre_alembic_database_is_never_stamped(r0, damage):
    url, path = r0
    with sqlite3.connect(path) as conn:
        damage(conn)
    before = scalar(path, "SELECT count(*) FROM sqlite_master")
    with pytest.raises(SchemaIncompatibleError, match="refusing to stamp"):
        command.stamp(cfg(url), "0001")
    assert "alembic_version" not in table_names(path)
    assert scalar(path, "SELECT count(*) FROM sqlite_master") == before
    assert scalar(path, "SELECT email FROM sellers") == "keep@example.com"


def test_a_pre_alembic_database_cannot_be_stamped_past_0001_or_upgraded_blindly(r0):
    url, path = r0
    for action in (lambda: command.stamp(cfg(url), "head"), lambda: command.stamp(cfg(url), "0002"),
                   lambda: command.upgrade(cfg(url), "head")):
        with pytest.raises(SchemaIncompatibleError, match="stamp 0001"):
            action()
    assert "alembic_version" not in table_names(path) and "payment_instructions" not in table_names(path)


def test_the_problem_report_names_what_is_wrong(r0):
    _url, path = r0
    with sqlite3.connect(path) as conn:
        _extra_table(conn)
        _missing_index(conn)
        _extra_column(conn)
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        found = schema_guard.problems(conn)
    assert "unexpected table rogue" in found and "missing index ix_payment_records_project_id" in found and "different table sellers" in found


def test_only_sqlite_can_be_adopted():
    fake = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    assert schema_guard.problems(fake)[0].startswith("pre-Alembic adoption is only supported for SQLite")
    with pytest.raises(SchemaIncompatibleError):
        schema_guard.require_adoptable(fake, "0001")


# --- foreign-key behaviour during rebuilds ---------------------------------------------------------------------------------

def test_a_connection_with_foreign_keys_on_is_refused_before_anything_changes(r0):
    url, path = r0
    command.stamp(cfg(url), "0001")
    engine = make_engine(url)  # the application engine turns foreign keys ON
    with engine.connect() as conn:
        with pytest.raises(SchemaIncompatibleError, match="foreign keys OFF"):
            command.upgrade(cfg(url, connection=conn), "head")
    assert scalar(path, "SELECT version_num FROM alembic_version") == "0001"
    assert "payment_instructions" not in table_names(path)
    engine.dispose()


def test_a_migration_that_would_leave_dangling_references_is_refused_up_front(r0):
    url, path = r0
    command.stamp(cfg(url), "0001")
    with sqlite3.connect(path) as conn:
        _dangling_foreign_key(conn)
    with pytest.raises(SchemaIncompatibleError, match="foreign key violations"):
        command.upgrade(cfg(url), "head")
    assert scalar(path, "SELECT version_num FROM alembic_version") == "0001" and "payment_instructions" not in table_names(path)


def test_an_empty_database_still_migrates_from_scratch(tmp_path):
    url = f"sqlite:///{tmp_path / 'empty.sqlite3'}"
    command.upgrade(cfg(url), "head")
    assert "payment_instructions" in inspect(create_engine(url)).get_table_names()


def test_the_application_still_refuses_to_serve_a_pre_alembic_database_without_touching_it(r0):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    url, path = r0
    app = create_app(Settings(database_url=url, paypal_mode="mock"))
    response = TestClient(app).post("/api/v1/auth/register", json={"email": "n@example.com", "password": "correct horse battery staple",
                                                                  "display_name": "N"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "schema_out_of_date"
    assert "alembic_version" not in table_names(path) and "payment_instructions" not in table_names(path)


# --- review findings: filter, empty version table, literal whitespace -------------------------------------------------------

def _hidden_trigger(conn):
    conn.execute("CREATE TRIGGER sqliteXevil AFTER INSERT ON sellers BEGIN SELECT 1; END")  # LIKE 'sqlite_%' would have hidden this


def _hidden_view(conn):
    conn.execute("CREATE VIEW sqliteXview AS SELECT * FROM sellers")


@pytest.mark.parametrize("damage", [_hidden_trigger, _hidden_view])
def test_objects_named_like_sqlite_internals_cannot_hide_from_the_comparison(r0, damage):
    url, path = r0
    with sqlite3.connect(path) as conn:
        damage(conn)
    with pytest.raises(SchemaIncompatibleError, match="unexpected"):
        command.stamp(cfg(url), "0001")
    assert "alembic_version" not in table_names(path)


def test_an_empty_alembic_version_table_does_not_make_a_database_managed(r0):
    url, path = r0
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")
        conn.execute("DROP TABLE deliverables")  # not R0 any more
    for action in (lambda: command.stamp(cfg(url), "0001"), lambda: command.stamp(cfg(url), "0002"),
                   lambda: command.stamp(cfg(url), "head"), lambda: command.upgrade(cfg(url), "head")):
        with pytest.raises(SchemaIncompatibleError):
            action()
    assert scalar(path, "SELECT count(*) FROM alembic_version") == 0 and "payment_instructions" not in table_names(path)


def test_an_empty_alembic_version_table_on_an_exact_r0_database_still_only_allows_stamp_0001(r0):
    url, path = r0
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")
    with pytest.raises(SchemaIncompatibleError, match="stamp 0001"):
        command.stamp(cfg(url), "head")
    command.stamp(cfg(url), "0001")
    command.upgrade(cfg(url), "head")
    assert scalar(path, "SELECT version_num FROM alembic_version") == "0008"


def test_whitespace_inside_a_quoted_literal_is_significant_but_layout_outside_it_is_not():
    n = schema_guard._normalize
    assert n("CREATE TABLE t (a TEXT DEFAULT 'a  b')") != n("CREATE TABLE t (a TEXT DEFAULT 'a b')")
    assert n("CREATE TABLE t (\n\ta TEXT,\n\tb INTEGER\n)") == n("CREATE TABLE t ( a TEXT, b INTEGER )")
    assert n("x 'it''s  ok' y") == "x 'it''s  ok' y"


def test_an_r0_database_with_the_original_multiline_layout_is_still_adopted(tmp_path):
    """Real create_all output is multi-line and tab-indented; only the layout differs from the stored reference."""
    import re
    path = tmp_path / "layout.sqlite3"
    conn = sqlite3.connect(path)
    for kind, _name, sql in objects_in_creation_order():
        if kind != "table":  # create_all emits indexes and triggers on one line; only table DDL is multi-line
            conn.execute(sql)
            continue
        parts = re.split(r"('(?:[^']|'')*')", sql)  # re-layout only OUTSIDE quoted literals
        conn.execute("".join(part if n % 2 else part.replace(", ", ",\n\t").replace("( ", "(\n\t").replace(" )", "\n)")
                             for n, part in enumerate(parts)))
    conn.commit()
    conn.close()
    url = f"sqlite:///{path}"
    command.stamp(cfg(url), "0001")
    command.upgrade(cfg(url), "head")
    assert scalar(path, "SELECT version_num FROM alembic_version") == "0008"
