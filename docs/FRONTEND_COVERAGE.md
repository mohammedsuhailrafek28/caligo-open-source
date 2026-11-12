# Frontend coverage, isolation and limitations

The frontend is built on the teammate-selective backend. It adds no backend capability.

## API to UI coverage

| Area | Endpoints (`/api/v1`) | UI |
|---|---|---|
| Auth | `auth/register`, `auth/login`, `auth/logout`, `auth/me` | `/register`, `/login`, top bar sign-out, session-expiry handling |
| Projects | `projects`, `projects/{id}` | Overview, Projects, project header data |
| Scope | `scope-records`, `baselines`, `scope-changes` | Scope |
| Delivery | `artifacts`, `buyer-requests` (+`cancel`), `buyer-decisions`, `buyer-authenticators`, `timeline` | Delivery evidence |
| Buyer | `buyer/challenge`, `buyer/verify`, `buyer/decide`, `buyer/instruction-changes/challenge`, `.../decide` | `/buyer` |
| Payments | `payment-instruction` (+`changes`, `changes/{id}/withdraw`), `invoices`, `payments`, `payments/sync`, `payments/{id}/send`, `.../resolve-review`, `.../events`, `payment-hold` (via instruction state) | Payments |
| Payee registry, screening | `payee-registry`, `{payee_id}`, `{payee_id}/versions`, `payee-registry/screen` | Payee registry, Screening |
| Reconciliation | `payment-observations`, `observation-comparison`, `merchant-ledger/imports`, `recovery-cases` | Reconciliation |
| Investigations | `state-recovery/divergences`, `diagnosis`, `integration-health`, `log-intelligence` | Diagnosis, Log intelligence, Integration health |
| Recovery | `state-recovery/plans`, `{id}/preview`, `approve`, `execute`, `events` | Recovery |
| Records | `evidence-report`, `unified-report` (+`/markdown`), `audit-events` | Evidence reports, Audit trail |
| Platform | `capabilities`, `/health` | Overview |

Not surfaced in the UI (read paths only; no workflow depends on them): `artifacts/{id}/links`, `analysis`, `recovery-cases/{id}`, `merchant-ledger/imports` listing, `state-recovery/plans/{id}` and `plans/{id}/diagnosis`. The authenticator *enrolment* endpoint does not exist for sellers, so the UI lists authenticators only.

## Safety properties enforced in the UI

- No fabricated data: every record shown is a backend response. The landing-page sample record is labelled "illustrative data, not a real transaction".
- A draft or unverified invoice reads "Not verified as paid"; only provider status `PAID` reads as paid. Uncertain create/send outcomes are shown as uncertain, never retried automatically.
- Sending an invoice and executing a recovery plan use a confirmation dialog. Recovery says it changes the simulated store only and that Caligo does not refund, cancel, capture or pay out.
- Registry entries are labelled seller-asserted; screening results are labelled advisory and "no signals found" is explicitly not verification.
- Status never depends on colour alone (icon + text on every StatusLabel).
- Seller UI cannot record a buyer decision; the buyer portal needs a mailbox one-time code (or authenticator code for instruction changes).

## LLM isolation

The frontend makes no calls to any model provider and has no chat or "Ask AI" surface. The backend has only an optional injection hook (`app.state.llm_transport`, default `None`, never set) used by `diagnose()`, and an unused `ai_api_key` setting; `AI_MODE=demo` is deterministic. Diagnosis therefore reports `llm_evaluation: NOT_RUN`, and the UI says no generative model was used. Stanza (local NLP) is advisory, provenance-labelled, and its requested versus effective mode and fallback reason are displayed.

## Verification run

Typecheck clean; vitest 9 passed; Vite build clean (JS ~89 kB gzip, CSS ~12 kB gzip); `node e2e/api-e2e.mjs` 13 of 13 passed against a mock-mode backend through the dev proxy (cookie auth, CSRF rejection, scope, delivery, instruction hold, draft invoice, payee screening, observations, diagnosis, reports, audit, logout). The E2E run caught two real defects, both fixed: requirement keys must be upper-case (`^[A-Z][A-Z0-9_-]{0,23}$`), and claims must reference an existing invoice number.

## Limitations (honest)

- **No browser E2E and no automated accessibility audit were run.** The Chrome automation extension was not connected. Keyboard, focus, contrast (palette chosen for AA ratios) and responsive layout have not been checked in a real browser; no axe run.
- The Stanza success path and the recovery-plan create/approve/execute path were not exercised end to end, because a healthy mock payment has no divergence (the API correctly answers 409 `no_divergence`). Those pages are covered by typechecking and the shared request layer only.
- The buyer decision and instruction-change flows were not exercised end to end (they need the fake-mailer outbox and an enrolled authenticator).
- Seller session expiry (8 h) returns to the sign-in page; unsaved form text is lost.
- `npm audit` reports 2 moderate advisories in react-router 6.x (open redirect via backslash, SSR hydration). All navigation targets are internal constants and there is no SSR; the fix is the breaking v7 upgrade, deferred.
- Local dev only: nothing here is deployed, pushed or hosted.
