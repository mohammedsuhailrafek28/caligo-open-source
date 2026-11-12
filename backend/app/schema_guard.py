"""Fail-closed compatibility check for adopting a database that predates Alembic.

A database created by R0 (`create_all`, no `alembic_version`) may be stamped at revision 0001 only if its schema is EXACTLY the R0 schema:
same tables, columns, constraints, indexes and append-only triggers, as emitted by the R0 code (`migrations/r0_schema.json`, generated from
commit 6f60916, not from migration 0001). Anything missing, different or unexpected is a refusal, and nothing is stamped, created or changed.
Adoption is SQLite-only because the reference is SQLite DDL; any other dialect is refused.
"""
import json
import re
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

R0_REVISION = "0001"
REFERENCE_PATH = Path(__file__).resolve().parents[1] / "migrations" / "r0_schema.json"
_IGNORED = ("sqlite_%", "alembic_version")


class SchemaIncompatibleError(RuntimeError):
    """The database is not exactly the R0 schema (or cannot be adopted at all)."""


def _normalize(sql: str) -> str:
    """Collapse whitespace OUTSIDE single-quoted literals only: a literal such as DEFAULT 'a  b' must keep its exact content."""
    parts = re.split(r"('(?:[^']|'')*')", sql)
    return "".join(p if i % 2 else re.sub(r"\s+", " ", p) for i, p in enumerate(parts)).strip()


def reference_objects() -> dict[tuple[str, str], str]:
    data = json.loads(REFERENCE_PATH.read_text(encoding="utf8"))
    return {(o["type"], o["name"]): o["sql"] for o in data["objects"]}


def actual_objects(conn: Connection) -> dict[tuple[str, str], str]:
    rows = conn.exec_driver_sql("SELECT type, name, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' "  # GLOB: LIKE treats _ as a wildcard and ignores case
                                
                                "AND name != 'alembic_version' AND sql IS NOT NULL").fetchall()
    return {(t, n): _normalize(s) for t, n, s in rows}


def problems(conn: Connection) -> list[str]:
    """Every difference between this database and the R0 schema, plus dangling foreign keys. Empty list = exactly R0."""
    if conn.dialect.name != "sqlite":
        return [f"pre-Alembic adoption is only supported for SQLite, not {conn.dialect.name}"]
    expected, actual = reference_objects(), actual_objects(conn)
    found = [f"missing {t} {n}" for (t, n) in sorted(expected.keys() - actual.keys())]
    found += [f"unexpected {t} {n}" for (t, n) in sorted(actual.keys() - expected.keys())]
    found += [f"different {t} {n}" for (t, n) in sorted(expected.keys() & actual.keys()) if expected[(t, n)] != actual[(t, n)]]
    violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        found.append(f"{len(violations)} foreign key violation(s) in existing data")
    return found


def has_user_objects(conn: Connection) -> bool:
    """True if the database holds ANY schema object of ours (table, view, index, trigger). Non-SQLite: inspect tables only."""
    if conn.dialect.name != "sqlite":
        return any(name != "alembic_version" for name in inspect(conn).get_table_names())
    return bool(conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' "
                                     "AND name != 'alembic_version' LIMIT 1").fetchone())


def has_recorded_revision(conn: Connection) -> bool:
    """True only if alembic_version exists AND holds a revision. An empty or purged version table is NOT a managed database."""
    if not inspect(conn).has_table("alembic_version"):
        return False
    return conn.exec_driver_sql("SELECT count(*) FROM alembic_version").scalar() > 0


def require_adoptable(conn: Connection, target_revision: str | None) -> None:
    """Called by migrations/env.py when the database has tables but no alembic_version. Raises unless it is exactly R0 and the
    requested revision is 0001 (stamping any later revision would skip the R1 migration)."""
    if target_revision != R0_REVISION:
        raise SchemaIncompatibleError(
            f"this database predates Alembic: it can only be adopted with `alembic stamp {R0_REVISION}` (requested {target_revision!r})")
    found = problems(conn)
    if found:
        shown = "; ".join(found[:8]) + (f"; ... {len(found) - 8} more" if len(found) > 8 else "")
        raise SchemaIncompatibleError(f"refusing to stamp: the database is not exactly the R0 schema ({shown}). Nothing was changed.")
