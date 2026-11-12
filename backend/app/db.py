from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect

from app.config import get_settings
from app.models import Base

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


class SchemaOutOfDateError(RuntimeError):
    """The database already has tables but is not at the current migration head. Never fixed automatically."""


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args)
    if is_sqlite:
        # SQLite ignores foreign keys unless asked; ownership integrity depends on them.
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def _alembic_config():
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def init_db(engine: Engine) -> None:
    """Fresh (empty) database: create the current schema and stamp it as migrated. Existing database: it must already be at the
    migration head, otherwise refuse to run. Existing data is never reset, dropped or altered implicitly; use `alembic upgrade head`."""
    from alembic.runtime.migration import MigrationContext

    if not inspect(engine).get_table_names():
        with engine.begin() as conn:  # one transaction: a concurrent starter never sees tables without a revision
            Base.metadata.create_all(conn)
            MigrationContext.configure(conn).stamp(_script_directory(), "head")
        return
    current = current_revision(engine)
    if current != head_revision():
        raise SchemaOutOfDateError(
            f"database is at revision {current!r}, application needs {head_revision()!r}: run `alembic upgrade head` "
            "(a database created before migrations existed must first be stamped with `alembic stamp 0001`)")


def _script_directory():
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config())
