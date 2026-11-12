# Caligo backend reference (API table, modes and structure)

**Status:** the offline evidence MVP is implemented and verified (738 offline tests at the evidence-MVP baseline). Everything runs against an
in-process MOCK PayPal or deterministic fakes; the PayPal Sandbox adapter exists and is tested against a fake PayPal, with only limited,
separately approved real Sandbox contact so far. There is **no live PayPal mode**. No PayPal credentials are in this repository.

## What it does
For directly contracted digital-service sellers: record the agreed scope, freeze it as an immutable versioned baseline, track change
proposals that stay pending until a verified buyer decision, register delivery artifacts with server-computed SHA-256 and server
timestamps, link evidence to requirements, run a source-grounded (heuristic, NOT PRODUCTION) scope analysis, and generate a deterministic
evidence report that keeps **confirmed observations**, **seller claims** and **AI judgments** apart. A MOCK invoice links payment state to
the project. Nothing here proves legal identity: a buyer decision shows only that someone controlling the verified mailbox made it.

## Implemented API (authoritative list: `contracts/API_CONTRACT.md`)
| Area | Routes |
|---|---|
| Foundation | `GET /health`, `GET /api/v1/capabilities` |
| Auth | `POST /api/v1/auth/register`, `login`, `logout`; `GET /api/v1/auth/me` |
| Projects and scope records | `POST/GET /api/v1/projects`, `GET /projects/{id}` (includes derived `delivery_status`), `POST/GET .../scope-records` |
| Payments (MOCK or SANDBOX) | `POST .../invoices` (DRAFT, `Idempotency-Key`), `.../payments/{id}/send`, `GET .../payments`, `POST .../payments/sync`, `.../payments/{id}/resolve-review`, `GET .../payments/{id}/events` |
| Scope baselines and changes | `POST/GET .../baselines`, `POST/GET .../scope-changes`, `GET .../audit-events` |
| Delivery evidence | `POST/GET .../artifacts`, `POST .../artifacts/{id}/links`, `GET .../timeline` |
| Buyer decisions | seller: `POST/GET .../buyer-requests`, `.../cancel`, `GET .../buyer-decisions`; public: `POST /api/v1/buyer/challenge`, `verify`, `decide` |
| Analysis and report | `POST/GET .../analysis`, `GET .../evidence-report`, `GET .../evidence-report/markdown` |
| Payment instruction (R1) | `GET/POST .../payment-instruction`, `.../versions`, `GET/POST .../changes`, `POST .../changes/{id}/withdraw`, `GET .../payment-hold` |
| Independent authorization (R2) | `GET .../buyer-authenticators` (read-only), public `POST /api/v1/buyer/instruction-changes/challenge` and `/decide`; operator CLI `scripts/enroll_buyer_authenticator.py` (see `docs/R2_AUTHORIZATION.md`) |
| Unified report (R5) | `GET .../unified-report`, `GET .../unified-report/markdown` (one deterministic, read-only report over scope/delivery, instruction integrity and payments; see `docs/R5_UNIFIED_REPORT.md`) |
| Recovery cases (R4) | `GET .../recovery-cases`, `GET .../recovery-cases/{case_id}` (advisory, evidence-cited, read-only; see `docs/R4_RECOVERY.md`) |
| Payment observations (R3) | `POST/GET .../merchant-ledger/imports`, `POST/GET .../payment-observations`, `GET .../observation-comparison` (claims vs provider evidence; see `docs/R3_OBSERVATIONS.md`) |

**Planned, not built:** deliverable upload/release routes (superseded by the evidence flow), webhooks, PDF export, a real mailer, a real AI
provider, object storage, any frontend. See `docs/ARCHITECTURE.md` for the unused scaffolding and the next-work dependency points.

## Modes (environment variables, see `.env.example`)
`PAYPAL_MODE`: `disabled` (default, payment routes return 503), `mock`, `sandbox` (test environment only; needs credentials and an allowlist).
`BUYER_MAIL_MODE`: `disabled` (default) or `fake` (in-memory outbox). `AI_MODE`: `disabled` (default) or `demo` (deterministic heuristic).
`APP_ENV=production` rejects every mode except all-disabled. `APP_ENV` defaults to `development`, so production must set it explicitly.

## Structure
```text
backend/app/        FastAPI app: routers/ (thin), services/ (behavior), models.py, schemas.py, payment adapters
backend/tests/      offline tests (network blocked except loopback; fake PayPal in fake_paypal.py)
contracts/          generated JSON schemas + API_CONTRACT.md (guarded by test_contracts.py)
docs/               ARCHITECTURE.md (tables, scaffolding, lifecycle, dependency points), EVIDENCE_MVP.md, SECURITY.md, stage notes
scripts/            check_secrets.py, export_contracts.py, demo_evidence_flow.py, Sandbox tooling (never run with --execute without approval)
frontend/           instructions only; paused
```

## Local start (Python 3.11+)
```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
# then verify: python ../scripts/verify_environment.py  (see docs/DEV_SETUP.md)
cp ../.env.example .env                               # Windows: copy ..\.env.example .env
uvicorn app.main:app --reload
```
`pytest -q` in `backend/` runs the offline suite (738 at the evidence-MVP baseline, 852 after R1 hardening; no network or credentials needed). `python scripts/check_secrets.py --git`
from the repo root runs the secret heuristic (not a replacement for gitleaks/trufflehog). After editing `schemas.py`: `python scripts/export_contracts.py`.
The full offline backend demo (R6: hold, buyer-side authorization, recovery cases, unified report; self-verifying, exits non-zero on failure): `backend/.venv/Scripts/python scripts/demo_backend_full.py --out demo_output`; frontend team guide: `docs/FRONTEND_HANDOFF.md`; OpenAPI: `contracts/openapi.json`. The older scope/delivery demo: `python scripts/demo_evidence_flow.py --out demo_output` (synthetic data, no network). Schema changes use Alembic (`backend/migrations`): a fresh database is created and stamped automatically; an existing one is never reset, and the API answers 503 `schema_out_of_date` until you run `alembic upgrade head` from `backend/` (a pre-migration R0 database is adopted with `alembic stamp 0001`, which refuses unless the schema is exactly R0; see `docs/ARCHITECTURE.md`). Re-run `pip install -e '.[dev]'` to get Alembic.

## Payment state
A payment becomes `PAID` (or refunded) only through reconciliation (`POST .../payments/sync`) after provider-side evidence is consistent;
`GET` never contacts the provider and nothing over HTTP can mark a payment paid. Inconsistent evidence goes to a sticky `NEEDS_REVIEW` that exits
only through the audited `resolve-review`. `Project.status` is financial only; delivery state is the separate derived `delivery_status`
(buyer-decision based). Refunds never reverse `PAID`. See `docs/STAGE_2C.md` and `docs/ARCHITECTURE.md`.

## Security invariants
- Never commit a `.env`, token, secret, buyer communication or real personal data. No credentials are read by assistants.
- Seller sessions are server-side with hashed tokens and CSRF protection; every project route is owner-scoped (other sellers get 404).
- Buyer link tokens, one-time codes and sessions are stored as hashes, delivered only to the buyer mailbox, single use, expiring and lock-out protected; the seller has no route that records a buyer decision.
- Evidence is append-only (ORM guards plus SQLite triggers; Postgres triggers are not built). Analysis output is untrusted: schema-validated, every citation must resolve to a record of the same project.
- Hackathon build limits: single node, in-memory throttles, unpaginated lists, no seller email verification, artifact text only. Do not deploy as-is.
