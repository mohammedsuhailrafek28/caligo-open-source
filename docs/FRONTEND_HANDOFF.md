# Caligo backend: frontend team handoff (R6)

> **Status and limitations: see `docs/PAYPAL_SANDBOX_STATUS.md` and `docs/BACKEND_REFERENCE.md`** (Sandbox evidence vs simulation, uncertain-outcome handling, endpoint inventory). This R6 document is still accurate for cookie/CSRF mechanics, the buyer outbox and report rendering, but its limitations section predates the Sandbox validation.

Backend only. No frontend is included. Everything here runs **offline** with a MOCK PayPal provider; there is no live PayPal mode in this codebase.

## 1. Repository and branch
* Repository (private): `the private development repository`
* Integration into `main` is the owner's decision.
* Machine-readable API: **`contracts/openapi.json`** (OpenAPI 3.1, generated from the code, drift-checked by `backend/tests/test_contracts.py`), JSON Schemas in `contracts/*.schema.json`, human-readable route table in `contracts/API_CONTRACT.md`. A running server also serves Swagger at `/docs` and the spec at `/openapi.json` (disabled when `APP_ENV=production`).

## 2. Local setup and startup
Python 3.11+ (3.12 used). From the repository root:
```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"        # POSIX: .venv/bin/python ; needs network once, for PyPI
.venv/Scripts/python ../scripts/verify_environment.py   # must print ENVIRONMENT OK
cp ../.env.example .env                                  # Windows: copy ..\.env.example .env ; then edit as below
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```
The API is then at `http://localhost:8000`, health at `GET /health`, interactive docs at `http://localhost:8000/docs`. A fresh SQLite database (`local_dev.sqlite3`, git-ignored) is created and migrated automatically; an existing database is never reset (a stale schema answers `503 schema_out_of_date` until `alembic upgrade head`).

### Environment variables (placeholders only; never commit real values)
| Variable | Value for local frontend work | Notes |
|---|---|---|
| `APP_ENV` | `development` | `production` requires `COOKIE_SECURE=true` and disables `/docs` |
| `DATABASE_URL` | `sqlite:///./local_dev.sqlite3` | |
| `PAYPAL_MODE` | `mock` | in-process fake; `disabled` turns payments off. Leave `sandbox` to the owner |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | *(empty)* | not needed for `mock`; never put real values in the repo |
| `BUYER_MAIL_MODE` | `fake` | buyer e-mail goes to an in-process outbox only; nothing is ever sent (optionally also to a local file, see section 6a) |
| `AI_MODE` | `demo` | deterministic rule-based analysis, labelled NOT PRODUCTION |
| `CORS_ORIGINS` | `http://localhost:3000` | explicit list; also the CSRF Origin allow-list; `*` is rejected |
| `AUTHENTICATOR_KEYS` | `k1:<base64 32-byte key>` | only for the R2 buyer-side authenticator; generate with `python scripts/enroll_buyer_authenticator.py generate-key` |
| `COOKIE_SECURE` / `COOKIE_SAMESITE` | `false` / `lax` | `true` over HTTPS |
| `PAYMENT_INSTRUCTION_REQUIRED` | `false` | `true`: no invoice without a recorded payment instruction |

## 3. Base URL, authentication, CSRF
* Base URL: `http://localhost:8000/api/v1` (no versioning beyond `v1`). JSON in, JSON out. `/api/v1/capabilities` says what is enabled.
* **Seller auth is a session cookie.** `POST /auth/register {email, password, display_name}` → `201`; `POST /auth/login {email, password}` → `200 {csrf_token, …}` and sets `pem_session` (HttpOnly) and `pem_csrf` cookies. Send requests with `credentials: "include"` from `http://localhost:3000` (cookies are SameSite=Lax; serve the frontend and API on the same site, e.g. both `localhost`).
* The session lasts 8 hours by default (`SESSION_TTL_MINUTES`); after that expect `401 not_authenticated` and log in again.
* **CSRF (double submit):** every non-GET seller request must carry header `X-CSRF-Token: <csrf_token from login>` *and* the `pem_csrf` cookie. A browser `Origin` header, when present, must be in `CORS_ORIGINS` or the request is `403 origin_not_allowed`. A request with **no** `Origin` header skips the origin check (non-browser clients), so CSRF protection then rests on the token alone. Missing/wrong token: `403 csrf_failed`. Not logged in: `401 not_authenticated`.
* Buyer endpoints (`/buyer/...`) use **no seller cookie**: they authenticate by a one-time link token + e-mailed code (scope/delivery) or by challenge + TOTP (instruction changes). Use a separate client/cookie jar for the buyer.
* Other seller's resources: `404 not_found` (never 403).
* Money is **integer minor units** (`amount_minor`, e.g. 120000 = 1,200.00 USD). Mutating payment calls need an `Idempotency-Key` header (8–64 chars).

## 4. Endpoints by workflow
All paths below are under `/api/v1`. "S" = seller session + CSRF on writes; "B" = buyer (no seller session). Full schemas: `contracts/openapi.json`.

**Auth & project**
`POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` · `GET|POST /projects`, `GET /projects/{id}` · `GET /capabilities`, `GET /health` (outside `/api/v1`).

**1. Scope (S)**
`POST|GET /projects/{id}/scope-records` (the original proposal text) · `POST|GET /projects/{id}/baselines` (freeze the ORIGINAL agreement as immutable v1) · `POST|GET /projects/{id}/scope-changes` (changes stay PROPOSED until a verified buyer approves).

**2. Delivery evidence (S)**
`POST|GET /projects/{id}/artifacts` · `POST /projects/{id}/artifacts/{artifact_id}/links` · `GET /projects/{id}/timeline` · `GET /projects/{id}/audit-events` · `POST|GET /projects/{id}/analysis` (AI/heuristic findings, always human-review) · `GET /projects/{id}/evidence-report` and `/evidence-report/markdown` (Stage 3 report).

**3. Buyer decisions on scope/delivery (S requests, B decides)**
`POST|GET /projects/{id}/buyer-requests`, `POST …/buyer-requests/{request_id}/cancel`, `GET /projects/{id}/buyer-decisions` · buyer: `POST /buyer/challenge {token}` → `POST /buyer/verify {token, code}` → `POST /buyer/decide {session_token, decision, statement?}`.

**4. Payment instruction integrity (S) and buyer-side authorization (B)**
`POST|GET /projects/{id}/payment-instruction` · `GET …/payment-instruction/versions` · `POST|GET …/payment-instruction/changes` · `POST …/changes/{change_id}/withdraw` · `GET /projects/{id}/payment-hold` · `GET /projects/{id}/buyer-authenticators` (masked, read-only) · buyer: `POST /buyer/instruction-changes/challenge`, `POST /buyer/instruction-changes/decide`.

**5. Payments (S, MOCK provider)**
`POST /projects/{id}/invoices` (+`Idempotency-Key`) · `POST …/payments/{payment_id}/send` · `POST /projects/{id}/payments/sync` · `GET /projects/{id}/payments` · `GET …/payments/{payment_id}/events` · `POST …/payments/{payment_id}/resolve-review`.

**6. Observations, divergence, recovery (S, read-throttled)**
`POST|GET /projects/{id}/payment-observations` (seller claims) · `POST|GET /projects/{id}/merchant-ledger/imports` (CSV claims) · `GET /projects/{id}/observation-comparison` · `GET /projects/{id}/recovery-cases` and `…/recovery-cases/{case_id}` (advisory, evidence-cited). · S2 (simulated merchant store only): `GET|POST /projects/{id}/state-recovery/…` divergences, plans, preview, approve, execute, events: see `docs/S2_GUARDED_RECOVERY.md`; S4 adds read-only advisory `…/state-recovery/diagnosis`, `…/plans/{id}/diagnosis` and `…/integration-health`. The seven-step frontend flow, error table, provenance labels and RULES_ONLY / real-LLM NOT RUN semantics are in `docs/S4_INTEGRATION_GUIDE.md` (execute is at-most-once; 409 codes name the cause; approvals bind to a plan version + fingerprint).

**7. Unified report (S, read-throttled)**
`GET /projects/{id}/unified-report?payments_limit=25&payments_offset=0` (JSON) · `GET …/unified-report/markdown` (same hash).

## 5. Example requests and responses
(Real specimens from the offline demo are in `docs/demo_examples/`; ids and times differ per run.)
```http
POST /api/v1/auth/login            {"email":"demo-seller@example.com","password":"…"}        → 200 {"csrf_token":"…", …}
POST /api/v1/projects              X-CSRF-Token: …   {"title":"Acme website"}                 → 201 {"id":"…","status":"DRAFT",…}
POST /api/v1/projects/{id}/payment-instruction   Idempotency-Key: instr-0001
     {"recipient_email":"buyer@example.org","currency":"USD","amount_minor":120000}
     → 201 {"version":1,"fingerprint":"…64 hex…","verification_status":"PENDING_VERIFICATION","provenance":"SELLER_ENTERED",…}
POST /api/v1/projects/{id}/payment-instruction/changes   Idempotency-Key: chg-0001
     {"recipient_email":"pay-here-now@example.net","currency":"USD","amount_minor":120000,"reason":"URGENT: bank details changed"}
     → 201 {"id":"…","holds_payment_operations":true,"from_fingerprint":"…","proposed_fingerprint":"…"}
GET  /api/v1/projects/{id}/payment-hold
     → 200 {"active":true,"reasons":["UNVERIFIED_INSTRUCTION_CHANGE"],"release_requires":"BUYER_SIDE_VERIFICATION","scope":"CALIGO_OPERATIONS_ONLY","pending_change_ids":["…"]}
```
Buyer-side decision (no seller session; the buyer's authenticator code is a 6-digit TOTP):
```http
POST /api/v1/buyer/instruction-changes/challenge  {"project_id":"…","change_id":"…","buyer_email":"buyer@example.org"}
     → 200 {"challenge_id":"…","change":{"from_fingerprint":"…","proposed_fingerprint":"…","current":{…},"proposed":{…},"seller_reason":"…"},"allowed_decisions":[…]}
POST /api/v1/buyer/instruction-changes/decide
     {"challenge_id":"…","project_id":"…","change_id":"…","buyer_email":"…","from_fingerprint":"…","proposed_fingerprint":"…",
      "decision":"REJECT_INSTRUCTION_CHANGE","totp_code":"123456"}
     → 200 {"outcome":"REJECTED","hold_active":false,"note":"Authorized by an operator-established second channel; this is not verified real-world identity …"}
```
Errors for this call: **`400 verification_failed` is the single answer for every failed decision** (wrong code, stale or changed request, fingerprint or binding mismatch, expired challenge); it is deliberately uniform, so offer "start again" (new challenge), never a specific diagnosis. Also `404 not_found` (unknown project/change/buyer), `429 too_many_attempts`, `503 verification_busy` (retry), `503 authenticator_unavailable` / `authenticator_keys_missing` (server not configured for R2).

## 6. Buyer authorization workflow and its trust limits (show this in the UI)
1. The seller records an instruction (version 1, **seller-entered, unverified**).
2. A change request (for example a new payee) is proposed by the seller. It immediately **holds** payment operations performed through Caligo. The hold never freezes PayPal funds. The seller **cannot** approve, reject or release it; withdrawing a change does not release the hold.
3. The **operator** (not the seller, not the API) enrolls a TOTP authenticator for the buyer on the server host: `python scripts/enroll_buyer_authenticator.py enroll --project <id> --buyer <email>` (prints the secret once). There is no HTTP route for enrolment.
4. The buyer gets a challenge (shows what changes: payee, currency, amount, both fingerprints) and decides with a TOTP code. Only this releases the hold: approve creates version 2 (the only version ever shown as `VERIFIED_BY_ENROLLED_SECOND_CHANNEL`); reject leaves version 1 in force.

**Trust limitations — do not paraphrase these away in the UI:** an operator-established authenticator shows that *whoever held that authenticator answered*. It is **not** proof of any person's real-world identity, and it is **not independent of the seller** if the seller is also the operator. Every authorization record carries `real_world_identity_verified: false` and `independent_of_seller: "NOT_ESTABLISHED"`. A buyer mailbox decision (scope/delivery) proves only that whoever controlled the mailbox made it. Labels to render: `PENDING_VERIFICATION`, `VERIFIED_BY_ENROLLED_SECOND_CHANNEL`, `REJECTED_BY_ENROLLED_SECOND_CHANNEL`, `UNVERIFIED_INCONSISTENT_RECORD` (a stored record disagrees with its change/authenticator/version: show as untrusted).

## 6a. Exercising the buyer link / one-time-code flow locally (development only)
By default nobody can read the buyer's e-mail (the link and code live only in server memory). For local frontend work there is one opt-in, **development-only** mechanism: a local outbox file.

* Setting: `DEV_OUTBOX_FILE=dev_outbox/buyer.jsonl` (empty = off, the default). It is only accepted with `BUYER_MAIL_MODE=fake`, and settings validation **refuses to start** if it is set in production or without the fake mailer. It adds **no HTTP route**, and no seller-facing response ever contains a buyer token or code.
* The file receives one JSON line per fake e-mail (`to`, `subject`, `body`, `sent_at`), created owner-only where the OS supports it. It holds live one-time secrets of a synthetic test buyer: `dev_outbox/` and `*.outbox.jsonl` are git-ignored; delete it when finished (`python scripts/dev_outbox.py dev_outbox/buyer.jsonl --clear`). Never use real buyer addresses.
* No SMTP, no credentials, no generated secret is committed. Use a throwaway address such as `buyer@example.org`.

Exact steps (all on your machine; the API on `:8000`, the frontend on `:3000`):
1. In `backend/.env` (never committed) set `APP_ENV=development`, `BUYER_MAIL_MODE=fake`, `DEV_OUTBOX_FILE=../dev_outbox/buyer.jsonl`, `BUYER_PORTAL_BASE_URL=http://localhost:3000`, `COOKIE_SECURE=false`. Start the API as in section 2.
2. As the **seller** (your normal logged-in session): freeze a scope baseline, propose a change, then `POST /api/v1/projects/{id}/buyer-requests` with `{"kind":"SCOPE_CHANGE_APPROVAL","buyer_email":"buyer@example.org","change_id":"<id>"}`. The response does not contain the link; that is intended.
3. Open a **separate browser profile or private window** for the buyer (separate cookie jar; the buyer is a different person and must never share the seller's session).
4. From the repo root run `backend/.venv/Scripts/python scripts/dev_outbox.py dev_outbox/buyer.jsonl`: it prints `link: http://localhost:3000/buyer/review?token=...`. Give that token to `POST /api/v1/buyer/challenge` `{"token": ...}` (202).
5. Run the same script again: it now also prints `code: NNNNNN`. Send `POST /api/v1/buyer/verify` `{"token","code"}` and use the returned `session_token` with `POST /api/v1/buyer/decide`.
6. R2 instruction-change flow: generate a key (`python scripts/enroll_buyer_authenticator.py generate-key`), export `AUTHENTICATOR_KEYS=k1:<that key>` in the environment of the API and of the next command, enrol with `python scripts/enroll_buyer_authenticator.py enroll --project <id> --buyer buyer@example.org` (prints the TOTP secret once), add it to an authenticator app, then use the R2 challenge/decision endpoints from section 6. The TOTP secret is never written to the outbox file.
7. When done: `scripts/dev_outbox.py ... --clear`, and remove the key from your shell.

## 7. Consuming the unified report
`GET /projects/{id}/unified-report` returns one document; render sections in this order and keep the **evidence-class badge** on every item (never merge classes visually):
`evidence_classes` (legend) → `summary` → `scope_and_delivery` (`available:false` when no agreement is frozen) → `payment_instructions` (`hash_chain_intact`, `versions`, `changes[].state`, `authorizations[]`, `hold`) → `payments.payments[]` (`authoritative_status` is the **only** payment state; show `provider_observations` beside `seller_claims`; `claim_vs_provider` may be `NOT_COMPARABLE_*`, show it as "cannot compare", never as a mismatch or a match) → `payments.evidence_gaps` → `payments.recovery_cases.items[]` (advisory text + `recommended_next_action` + `prohibited_automated_actions`; render as guidance, never as buttons that act) → `citation_check` (if `all_resolved` is false, show a prominent warning) → `truncation` (page more: use `payments_offset`) → `limitations`.
Evidence classes: `PROVIDER_OBSERVATION`, `SERVER_RECORDED`, `SELLER_CLAIM`, `OPERATOR_ESTABLISHED_VERIFICATION`, `AI_INTERPRETATION`, `DERIVED_ADVISORY` (definitions are inside the response). `report_sha256` is the canonical SHA-256 of the body without that key; the Markdown endpoint prints the same hash. Details and bounds: `docs/R5_UNIFIED_REPORT.md`, cases: `docs/R4_RECOVERY.md`, observations: `docs/R3_OBSERVATIONS.md`.

## 8. Common errors
Envelope: `{"error":{"code":"…","message":"…","request_id":"…"}}` (validation errors add `details` and never echo input). Quote the `request_id` when reporting a problem.
| HTTP | codes you will see |
|---|---|
| 400 | `idempotency_key_invalid`, `verification_failed` |
| 401 | `not_authenticated`, `invalid_credentials`, `buyer_session_invalid` |
| 403 | `csrf_failed`, `origin_not_allowed` |
| 404 | `not_found` (unknown or another seller's resource) |
| 409 | `baseline_required`, `baseline_exists`, `change_pending`, `change_not_pending`, `instruction_exists`, `instruction_required`, `project_already_invoiced`, `replacement_required`, `idempotency_key_conflict`, `operation_in_progress`, `review_required`, `provider_mismatch`, … |
| 422 | `validation_error`, `approver_mismatch`, `hash_mismatch`, `unknown_requirement`, `unsupported_currency`, `recipient_confirmation_mismatch`, `ledger_invalid`, `no_change`, … |
| 429 | `too_many_attempts` (send `Retry-After`) |
| 502/503 | `provider_*`, `analysis_*`, `ai_disabled`, `buyer_mail_disabled`, `authenticator_keys_missing`, `schema_out_of_date`, `verification_busy` |
A held payment operation is `409 payment_hold_active` with `details: [{"reason": …}]` (a request that disagrees with the recorded instruction is a `409` with `details: [{"field": …}]`); read `GET /payment-hold` and tell the user it is released only by buyer-side verification. Idempotency keys are 8-64 characters `[A-Za-z0-9_-]`.

## 9. Run the offline demo
```bash
backend/.venv/Scripts/python scripts/demo_backend_full.py --out demo_output      # POSIX: backend/.venv/bin/python
```
Isolated temporary database, MOCK PayPal, fake outbox, demo-only generated key. It reads neither `.env` nor environment variables, makes no network call, and uses no existing database; it also hashes `backend/.smoke/state.json` before and after as a post-check. `--out` is refused unless the directory is absent, empty or was created by a previous demo run (marker file); it never deletes the repo, `backend`, `.smoke` or your home directory. It walks: scope freeze → artifacts + buyer acknowledgment → instruction → paid (simulated) invoice → conflicting merchant ledger → suspicious instruction change (hold) → operator-enrolled authenticator rejects it → a legitimate change approved → recovery cases → unified JSON + Markdown reports, and verifies ~19 expected outcomes (`demo_output/checks.json`); exit code is non-zero on any failure. Output: `demo_output/api/NN_*.json` (example responses), `unified-report.final.json|md`, `unified-report.during-hold.json`. The older `scripts/demo_evidence_flow.py` shows the scope/delivery-dispute story. Committed specimens: `docs/demo_examples/`.

## 10. Known backend limitations
* **L1 — buyer e-mail is not delivered anywhere.** There is no real mailer (none is approved). Locally, the fake outbox can additionally be written to a git-ignored file for development (section 6a); in production that setting is refused and the fake mailer is disabled.
* Payments are MOCK only here; there is no live PayPal. Sandbox mode exists only for the owner's own test account.
* The AI analysis is a heuristic labelled NOT PRODUCTION; its output is never fact.
* No webhook ingestion, no provider timestamps, no detection of a transaction that disappears from a later provider reading; evidence gaps are permanent (so a payment that ever had one stays "incomplete").
* No case-management state (no acknowledge/resolve of recovery cases); no PDF export; no pagination of scope/evidence lists beyond what each endpoint documents; the unified report's payment sections cover one page of payments.
* Read throttling and login throttling are in-process (not shared across workers); SQLite only (Postgres is unsupported); single-process deployment.
* Not legal advice and not identity verification; see the report `limitations`.
