"""Check that the Python environment is the project's own and complete. Read-only: installs nothing, makes no network call.

    backend/.venv/Scripts/python scripts/verify_environment.py      (Windows)
    backend/.venv/bin/python scripts/verify_environment.py          (POSIX)

Exit 0 only if every required package imports from inside the active environment (sys.prefix) - not from PYTHONPATH, user site or a global
interpreter - so a green test run proves the documented install, not a borrowed one. See docs/DEV_SETUP.md."""
import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path

REQUIRED = {  # import name -> distribution name
    "fastapi": "fastapi", "uvicorn": "uvicorn", "sqlalchemy": "sqlalchemy", "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings", "argon2": "argon2-cffi", "email_validator": "email-validator", "httpx": "httpx",
    "alembic": "alembic", "mako": "mako", "markupsafe": "markupsafe", "cryptography": "cryptography", "pytest": "pytest",
}


def locate(module: str) -> Path | None:
    spec = importlib.util.find_spec(module)
    return Path(spec.origin).resolve() if spec and spec.origin else None


def is_inside(path: Path, prefix: Path) -> bool:
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


def check(prefix: Path | None = None) -> list[tuple[str, str, str]]:
    """Returns (module, status, detail) rows; status is OK, MISSING or OUTSIDE_ENV."""
    prefix = (prefix or Path(sys.prefix)).resolve()
    rows = []
    for module, dist in REQUIRED.items():
        origin = locate(module)
        if origin is None:
            rows.append((module, "MISSING", "not importable"))
            continue
        try:
            version = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        rows.append((module, "OK" if is_inside(origin, prefix) else "OUTSIDE_ENV", f"{version} {origin.parent}"))
    return rows


def main() -> int:
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    print(f"python {sys.version.split()[0]}  prefix={sys.prefix}  virtualenv={'yes' if in_venv else 'NO'}")
    borrowed = bool(os.environ.get("PYTHONPATH"))
    if borrowed:
        print("WARNING: PYTHONPATH is set; packages may be borrowed from outside this environment.")
    rows = check()
    for module, status, detail in rows:
        print(f"{status:12} {module:18} {detail}")
    ok = in_venv and not borrowed and all(status == "OK" for _, status, _ in rows)
    print("ENVIRONMENT OK" if ok else "ENVIRONMENT NOT REPRODUCIBLE: run the install steps in docs/DEV_SETUP.md")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
