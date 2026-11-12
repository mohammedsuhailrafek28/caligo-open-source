"""Alembic environment. The URL comes from the application settings (DATABASE_URL), never from a file in Git.

Safety rules enforced here (fail closed):
* A database that has tables but no alembic_version (it predates Alembic) is adopted only by `alembic stamp 0001`, and only if its
  schema is exactly the R0 schema (app.schema_guard). Otherwise the command stops before changing anything.
* SQLite migrations run with foreign keys OFF (table rebuilds need it), after a foreign_key_check pre-flight, and are verified again
  afterwards. A caller-supplied connection with foreign keys ON is refused rather than risking a silent cascade during a rebuild."""
from alembic import context
from sqlalchemy import create_engine

from app.config import get_settings
from app.models import Base
from app.schema_guard import SchemaIncompatibleError, has_recorded_revision, has_user_objects, require_adoptable

target_metadata = Base.metadata


def _url() -> str:
    return context.config.attributes.get("url") or context.config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _guard_and_run(connection) -> None:
    sqlite = connection.dialect.name == "sqlite"
    if sqlite and connection.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise SchemaIncompatibleError("run migrations with SQLite foreign keys OFF (table rebuilds would otherwise cascade or fail)")
    if not has_recorded_revision(connection) and has_user_objects(connection):  # also an EMPTY alembic_version table
        require_adoptable(connection, context.get_revision_argument())
    elif sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        raise SchemaIncompatibleError("existing data has foreign key violations; fix them before migrating. Nothing was changed.")
    connection.commit()  # end the read-only autobegun transaction so the migration commits its own
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
    if sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        raise SchemaIncompatibleError("foreign key violations after migrating; restore from backup")
    connection.commit()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _guard_and_run(connection)
        return
    engine = create_engine(_url())  # no foreign-key pragma here on purpose: SQLite default is OFF
    with engine.connect() as conn:
        _guard_and_run(conn)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
