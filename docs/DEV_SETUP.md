# Development environment: reproducible install and verification

Everything below is offline-safe except step 2, which downloads packages from PyPI. **Run step 2 yourself (or approve it explicitly)**; this
repository's tooling never installs anything on its own, and test runs must not rely on packages borrowed from another interpreter.

1. Create a fresh virtual environment (Python 3.11+; 3.12 was used):
   `cd backend && python -m venv .venv`
2. Install the project and its test dependencies (network access to PyPI required, once):
   `.venv/Scripts/python -m pip install -e ".[dev]"`   (POSIX: `.venv/bin/python`)
   Runtime dependencies are declared in `backend/pyproject.toml`; R1 added `alembic`, R2 added `cryptography`. There is no lock file: versions
   are the ranges in `pyproject.toml`; record the resolved versions with `pip freeze` when you cut a release.
3. Verify the environment is complete and self-contained (read-only, no network):
   `.venv/Scripts/python ../scripts/verify_environment.py` must print `ENVIRONMENT OK`. It fails if a required package is missing or imports from
   outside the environment (`PYTHONPATH`, user site, a global interpreter).
4. Verify behaviour (offline, no credentials): `.venv/Scripts/python -m pytest -q` (about 6 minutes) and, from the repo root,
   `backend/.venv/Scripts/python scripts/check_secrets.py --git`.
5. Encryption key for R2 (only needed to enrol or verify authenticators): `python scripts/enroll_buyer_authenticator.py generate-key`, then put
   `AUTHENTICATOR_KEYS=k1:<key>` in your private environment. Never commit it, never log it. Losing it makes enrolled authenticators unusable
   (verification fails closed); the recovery is revoke and re-enrol.

## Status of the author's working environment (honest record)
At R2 acceptance the project virtualenv lacked `alembic`, `mako`, `markupsafe` and `cryptography`; the suite had been run with copies borrowed from a global
interpreter. The declared install (`pip install -e ".[dev]"` with `backend/.venv`'s Python) was then run with approval and added exactly Mako 1.4.3, MarkupSafe 3.0.4,
alembic 1.20.1 and cryptography 50.0.2. `scripts/verify_environment.py` printed `ENVIRONMENT OK` with `PYTHONPATH` empty and the full suite passed (926) using only that
environment. The environment is therefore reproducible by the steps above; R3 work and tests were run without any `PYTHONPATH` workaround.
