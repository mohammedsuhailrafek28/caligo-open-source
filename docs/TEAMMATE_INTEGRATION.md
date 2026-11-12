# Selective integration of the teammate's repository

Source evaluated (read-only, never modified): `https://github.com/swtzzz/Caligo`, `main` at **`ed5d3efee5046d1ae6a60f0d9dc76000da1dd3e7`** ("Add files via upload", on top of an initial commit). Base: this repository's `integration/caligo-backend-final-stanza` at `6ed9b73`. Our backend stays the source of truth; nothing was merged or copied wholesale.

## 1. What the teammate's repository is

* A small FastAPI + SQLAlchemy backend (~3,300 lines Python, 15 endpoints under `/api/v1/integrity`) and a Vite/React/TypeScript/Tailwind frontend (8 pages, 3 API modules).
* **Payer-side** payment-instruction integrity: a registry of vetted *suppliers* with approved payout destinations, a rules engine (destination changed / unknown supplier / urgency wording / amount stub), HOLDs, "four-eyes" approval of a hold by a different user name, an append-only-intended audit log, advisory AI (invoice extraction, hold explanation, checklist) and a dev stub that "executes" payments.
* Our backend is **seller-side evidence**: a project's payment instruction (versioned, fingerprinted, chained), change proposals that HOLD Caligo's payment operations until the **buyer** verifies through an enrolled second channel (TOTP, R2), payment observations, simulated merchant-state recovery (S1/S2), advisory diagnosis (S3/S3.5), optional Stanza log intelligence.

The two designs address the same threat (an altered payment destination) from opposite sides, so most of the teammate's *enforcement* is already implemented, more strongly, in ours; what is genuinely absent from ours is a **counterparty destination registry with history**, **BEC wording hints** and **literal invoice-text extraction**.

## 2. Capability comparison and decision

| Teammate capability | Ours today | Decision | What happened |
|---|---|---|---|
| Supplier registry (name + approved destination) | None | **ADAPT** | New project-scoped **payee registry**: `app/payee_models.py`, `app/services/payee_registry.py`, `app/routers/payees.py`, migration `0008`. Seller-asserted, append-only, chained versions; e-mail destinations only; masked in audit; 200 payees/project. |
| Destination history | Instruction versions with `chain_sha256` (R2) for the project's own instruction; nothing for counterparties | **ADAPT** | A destination change is a new chained version (`PayeeDestinationVersion`), never an edit. Teammate's mutable `Supplier.approved_destination` rewritten on approval is **rejected**. |
| Rules engine: destination changed, unknown supplier | R1 compares to the instruction in force | **ADAPT** | `screen` adds registry-based signals (`DESTINATION_DIFFERS_FROM_REGISTRY`, `_NOT_REGISTERED`, `_SUPERSEDED`, `_RECENTLY_CHANGED`) and instruction-based signals, each **cited** (`payee:<id>@v<n>`, `payment_instruction:<id>`). |
| Rules engine: urgency / BEC keywords | None | **ADAPT** | `app/services/bec_signals.py`: word-bounded phrase patterns (urgency, bank-change wording, secrecy), LOW severity "hints", with character spans. The teammate's plain substring match ("rush" inside "crush") is not used. |
| Amount anomaly (fixed 100,000 threshold stub) | R1 compares amount to the instruction | **REJECT (replaced)** | A fixed threshold is arbitrary; `AMOUNT_DIFFERS_FROM_INSTRUCTION` compares to the real instruction in integer minor units. |
| SHA-256 integrity seal of instruction fields | Instruction `fingerprint` + `chain_sha256` (R1/R2) | **REJECT (duplicate)** | Ours is stronger (canonical content, chained, immutable). |
| HOLD lifecycle (OPEN/APPROVED/REJECTED) in its own tables | R1 `payment-hold` computed from pending changes; no seller release | **REJECT** | A second hold authority would conflict with R1/R2. No hold is created, released or changed by anything new. |
| Hold approval by "a different user" (X-User string), verification record with free-text notes | Buyer-side independent verification (TOTP) | **REJECT** | The identity is an unauthenticated header, so segregation of duties is unenforceable; free-text notes are not verification. Registry changes are recorded as `independently_verified: false`. |
| Independent verification / compliance hold evidence | R2 (`InstructionChangeVerification`, authenticator challenge/decision) | **KEEP ours** | Already covered; nothing imported. The teammate's *verification checklist* wording is guidance text only (see frontend doc). |
| Audit trail (`audit_events`, mutable ORM table) | Append-only `audit_events` with DB triggers | **KEEP ours, REUSE** | Registry writes use the existing `record_audit` (actor `SELLER`, subject `payee`). The teammate's table is not imported. |
| AI invoice extraction (LLM or mock) | `AI_MODE=demo` heuristics; Stanza exact candidates | **ADAPT (deterministic only)** | `extract_fields`: literal regex matches (e-mails, amount, currency, invoice reference) with spans, `null` when not stated, always `verified: false`. The mock's hard-coded `"acme" → "Acme Supplies Co"` guess and float amounts are rejected. |
| AI risk explanation / verification checklist (LLM) | Cited deterministic reports | **REJECT (live LLM) / frontend text** | See security risks. Static checklist text can be shown by the UI; no AI endpoint was added. |
| `ai/client.py` OpenAI and Gemini clients | None (no outbound AI) | **REJECT** | Outbound calls with an API key in the URL query string (Gemini), key from `.env`, prompts built from untrusted invoice text. Our rule: AI is untrusted, advisory, no secrets in URLs, no new outbound calls. |
| X-User / X-Role "authentication" and RBAC | Session cookie + CSRF + owner scoping, 404 for other sellers | **REJECT** | Any caller can claim `admin`. Never imported. |
| `require_integrity_cleared` guard + `execute_stub.py` "payment execution", fabricated transaction ids, mock recovery events | No payment execution path; provider port has no refund/cancel | **REJECT** | These would be a second payment authority and fake provider evidence. The frontend's `executePayment` / `getRecoveryEvents` mocks must not be wired to anything real. |
| `/dev/reset`, `seed.py` | Demo scripts with synthetic data | **REJECT** | Not shipped. |
| CORS `allow_origins=[..., "*"]` with credentials | Explicit CORS list | **REJECT** | Invalid and unsafe combination. |
| React frontend | none | **KEEP (adapt later)** | Not modified here; see the mapping below. |

## 3. What was integrated (backend)

`POST/GET /api/v1/projects/{id}/payee-registry`, `GET …/{payee_id}`, `POST …/{payee_id}/versions`, `POST …/screen` (5 operations; total 73).

* **Reused:** session auth, `get_owned_project` (404 across sellers), CSRF, `check_origin`, the read throttle, `record_audit`, `make_append_only` (ORM guard + SQLite triggers), Alembic (`0008`), the existing error envelope, `PaymentInstruction` (read-only).
* **Authority:** registry rows are `SELLER_ASSERTED` and cannot approve, verify, clear or override anything. No route writes a hold, instruction, payment, order, recovery plan or observation. `screen` writes nothing (tested by row counts and the unchanged `payment-hold`).
* **No new dependency, no network, no model, no secret.** Stanza and the deterministic fallback are untouched; the new modules do not import `app.log_intelligence`.

## 4. Security risks found in the teammate repository

1. **Forgeable identity/roles** (X-User/X-Role) gating hold approval. Critical if ever exposed.
2. **Self-approval not preventable** (segregation-of-duties compares two attacker-chosen strings); approval evidence is free text.
3. **Mutable "audit"/hold/supplier tables** (no append-only guard); `approve_hold` overwrites the supplier's approved destination.
4. **Outbound LLM calls** with provider key in a URL query (Gemini) and in `.env`; untrusted invoice text in prompts; error text (`str(e)`) written into audit details.
5. **CORS wildcard with credentials**; `DEBUG=True` default; SQLite file `integrity.db` default path.
6. **Committed `__pycache__/*.pyc`** (Python 3.14 bytecode) in the repository.
7. **Fabricated payment/recovery data** (`execute_stub`, `mocks/recovery.ts`) presented in a "payment journey" UI.
8. Float money in the AI path and the amount stub; case-insensitive-trim-only destination comparison (no format validation).
9. Idempotency by `payment_id` only, silently voiding and rewriting an existing instruction when details differ.
10. No tests in the repository.

## 5. React-to-backend compatibility matrix

All frontend changes are **future work**; the teammate's code is unmodified. Global adaptations: (a) delete `X-User`/`X-Role` and `UserContext` demo users; call `POST /api/v1/auth/login`, send `credentials: 'include'` and the `X-CSRF-Token` header on writes (see `docs/FRONTEND_HANDOFF.md`); (b) every route is project-scoped (`/api/v1/projects/{project_id}/…`), so the app needs a project selector (`GET /api/v1/projects`); (c) ids are UUID strings, not integers; (d) errors use the backend envelope (`error`/`message` → see `docs/FRONTEND_HANDOFF.md`); (e) money is integer minor units, never floats; (f) remove `src/mocks/*`, `resetDemoData`, `executePayment`.

| React page / component | Existing API call (`src/api/*`) | Authoritative backend endpoint | Required adaptation | Missing endpoint | Security / provenance notes |
|---|---|---|---|---|---|
| `SuppliersPage` (list, create, detail) | `getSuppliers`, `getSupplier`, `createSupplier` | `GET/POST /projects/{id}/payee-registry`, `GET …/{payee_id}` | Rename supplier→payee; `approved_destination`→`current.destination`; drop `approved_by`; int id→UUID; show provenance badge **"Seller-recorded, not verified"** | none | `independently_verified` is always false; never show a "verified/approved" state for a payee |
| `SuppliersPage` destination history | `getSupplierDestinationHistory` | `GET …/payee-registry/{payee_id}` (`history`, newest first) | Map `destination/changed_at/approved_by` → `destination/created_at/note`; show chain hash optionally | none | History is append-only; "changed" means *recorded*, not *approved* |
| `SuppliersPage` "change destination" (inside create/edit flow) | (via `checkInstruction` approval side effect) | `POST …/payee-registry/{payee_id}/versions` | Explicit action; copy must say it records the seller's assertion | none | Does **not** clear any hold or alter the payment instruction |
| `NewInstructionPage` supplier picker | `getSuppliers` | `GET …/payee-registry` | as above | none | – |
| `NewInstructionPage` "Extract from invoice text" | `extractInstruction` (`/integrity/instructions/extract`) | `POST …/payee-registry/screen` with `{text}` → `extracted` | Use `extracted.emails/amount_minor/currency/invoice_reference` to pre-fill; show spans; amount in minor units; fields may be null | none (no supplier-name or due-date extraction by design) | Extraction is literal and unverified; the text is never echoed; no LLM |
| `NewInstructionPage` "Check instruction" (ALLOW/HOLD) | `checkInstruction` (`/integrity/instructions/check`) | `POST …/payee-registry/screen` (advisory) and, to actually record, `POST /payment-instruction` / `…/changes` (with `Idempotency-Key`) | Replace the decision card with `outcome` + `signals[]` (cites, severity); remove ALLOW/APPROVED wording; recording uses the existing R1 endpoints | none | A screen is not an approval; HOLD remains computed by R1 (`GET …/payment-hold`) |
| `HoldsPage` (list/filter) | `getHolds` | `GET …/payment-hold` + `GET …/payment-instruction/changes` per project | Build rows from pending/resolved changes; no status filter server-side | **Cross-project holds list** (UI aggregates over `GET /projects`) | Holds block Caligo operations only; they do not freeze PayPal funds |
| `HoldDetailPage` (detail, reasons, risk flags) | `getHoldDetail` | `GET …/payment-instruction/changes` (item), `GET …/payment-hold` | Map `risk_flags/reason` → change `reason`, `status`, blocking operations | none | – |
| `HoldDetailPage` "explanation" | `getHoldExplanation` (LLM) | none | Render static explanatory text from the change fields; optional `screen` signals | **AI explanation endpoint: intentionally absent** | No LLM; never present generated text as evidence |
| `HoldDetailPage` request verification / approve / reject | `requestVerification`, `approveHold`, `rejectHold` | Buyer-side: `POST /buyer/instruction-changes/challenge` and `/decide` (buyer, not seller). Seller may only `POST …/changes/{id}/withdraw` | **Remove approve/reject for sellers**; show "waiting for buyer verification" + withdraw | **Seller approve/reject: intentionally absent** | A seller can never clear a hold (see docs/SECURITY.md); buyer decision = control of the verified mailbox + authenticator |
| `PaymentJourneyPage` | `getAuditLogs(payment_id)`, `getIntegrityStatus` | `GET …/audit-events`, `GET …/payments/{payment_id}/events`, `GET …/payment-hold`, `GET …/payment-instruction`, `GET …/state-recovery/*` | Filter audit by `subject_id`; replace `integrity_status` badge by hold state; remove mock execute/recovery steps | none | Only provider-reconciled `PaymentEvent`s are payment facts; show provenance labels (`docs/FRONTEND_HANDOFF.md`) |
| `AuditLogPage` | `getAuditLogs` | `GET …/audit-events` | Project-scoped; client-side filter; `actor_role` instead of free-text actor | **`payment_id` server filter** (use `subject_id` client-side) | Append-only; payee events show masked destinations |
| `DashboardPage` | `getHolds`, `getAuditLogs`, `getSuppliers`, `resetDemoData` | `GET …/payment-hold`, `GET …/audit-events`, `GET …/payee-registry`, `GET …/unified-report` | Aggregate per selected project; delete reset button | none | – |
| `RecoveryPlaceholderPage`, `api/recovery.ts`, `mocks/recovery.ts` | `executePayment`, `getRecoveryEvents` (dev stub / mocks) | `GET …/state-recovery/divergences`, `…/diagnosis`, `…/plans*` (create/preview/approve/execute/events), `…/integration-health`, optionally `POST …/state-recovery/log-intelligence` | **Delete** the payout "execute" and mock events; build on the S2 plan flow (approve binds version + fingerprint) | none | Recovery acts only on the *simulated* merchant store; no refund/cancel/capture exists; mock transaction ids must never be shown |
| `UserContext`, role switcher, `TopBar` | `getCurrentDemoUser`, `X-User/X-Role` | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Replace demo users with the real session; roles do not exist (single seller per project) | none | Never trust a client-side role |
| `StatusBadge` / `Card` / layout components | – | – | Reusable as-is; extend status vocabulary to the backend enums | – | – |

## 6. Remaining limitations (honest list)

* The registry is **seller-asserted**; there is no independent verification of a payee destination (R2 verifies the project's own instruction with the buyer). Segregation of duties cannot be enforced on a single-seller account.
* Destinations are **e-mail addresses** only (PayPal payee); bank/IBAN destinations are not modelled.
* `screen` is advisory and heuristic (regex): it can miss fraud and can flag benign text; "NO_SIGNALS_FOUND" is not assurance.
* No server-side per-payment audit filter, no cross-project holds list, no seller-visible AI explanation (all deliberately or as UI work).
* No idempotency key on registry writes (duplicates are refused by label/destination checks instead).
* The frontend was not built, run or tested here; `npm` dependencies of the teammate repo were not installed or audited.
* Stanza: unchanged and independent; `screen` does not use it.
