# Caligo evidence MVP (Stage 3)

An offline, deterministic workflow that produces a reviewer-ready record of **what was agreed, what changed, what was delivered,
what the buyer verifiably said, and what is still unproven**. All data in the demo is synthetic; the payment is the existing MOCK
provider; nothing contacts PayPal or any third party.

## Run the demo (no credentials, no network)
```
cd paypal-evidence-mvp
backend/.venv/Scripts/python scripts/demo_evidence_flow.py --out demo_output   # Windows; use backend/.venv/bin/python elsewhere
```
It prints the story step by step and writes `evidence-report.json` and `evidence-report.md`. Story: a 3-requirement website is agreed
and frozen; the buyer approves a blog through a verified mailbox; the seller also delivers a loyalty-points feature nobody approved;
one artifact is only a declared hash; a MOCK draft invoice is created; the buyer disputes delivery. The report lists the unapproved work,
the unverifiable artifact and the dispute as unresolved, and the analysis marks acceptance UNSUPPORTED.

## The pieces
| Stage | What | Key rule |
|---|---|---|
| 3A | Immutable, versioned scope baselines; change proposals; append-only audit trail | v1 frozen once; later versions exist only because a verified buyer approval was applied; the seller has no route that approves a change |
| 3B | Evidence artifacts with server SHA-256, versions, duplicate detection, requirement links, server-ordered timeline | seller-asserted times and links are labelled claims; `DECLARED_ONLY` / `UNAVAILABLE` are never shown as verified |
| 3C | Buyer approval / acknowledgment / acceptance / dispute via link token + e-mail OTP | token and code reach only the buyer mailbox (Mailer port); stored as hashes; single use; expiry; lockout; the decision proves control of a mailbox, **not legal identity** |
| 3D | AI analysis boundary | provider output is untrusted: schema-validated, every citation must resolve to a record of the same project, SUPPORTED acceptance/authorization needs a matching buyer decision, invalid output stores nothing; the bundled provider is a keyword heuristic labelled NOT PRODUCTION |
| 3E | Evidence report (JSON + Markdown) | pure function of stored data, `report_sha256`; observations, seller claims and AI judgments are separate sections; unresolved matters are listed |

## Endpoints (all seller-session routes unless noted)
`POST/GET /projects/{id}/baselines`, `/scope-changes`, `GET /audit-events`, `POST/GET /artifacts`, `POST /artifacts/{id}/links`,
`GET /timeline`, `POST/GET /buyer-requests`, `POST /buyer-requests/{id}/cancel`, `GET /buyer-decisions`, `POST/GET /analysis`,
`GET /evidence-report`, `GET /evidence-report/markdown`. Public buyer routes: `POST /api/v1/buyer/challenge | verify | decide`.
See `contracts/API_CONTRACT.md`.

## Configuration
* `BUYER_MAIL_MODE=disabled` (default) or `fake` (in-memory outbox, tests/demo only). No real mailer exists; `fake` is rejected in production.
* `AI_MODE=disabled` (default) or `demo` (deterministic heuristic). No real AI provider exists; `demo` is rejected in production.

## What this is not
* Not proof of legal identity, payment, delivery or acceptance in a legal sense. A seller who controls the buyer's mailbox can still forge a buyer decision.
* Artifact content is stored as text up to 64 KB; larger or binary files need an object store (not built).
* The analysis is a keyword heuristic, not a language model. A real provider must be added behind `AnalysisProvider` and will still pass through the same validator.
* No PDF export, no real e-mail delivery, no buyer-facing UI, no real PayPal evidence capture beyond the existing Sandbox adapter.

## Independent security review (Stages 3A-3E)
No CRITICAL findings. Fixed: parallel OTP guessing (attempts are now charged atomically before comparison), a seller addressing a request to their own mailbox (rejected; same-domain decisions are flagged in the report), citation checks now verify the *relationship* (right change, right requirement, latest delivery decision) and only coverage / authorization / acceptance findings may be SUPPORTED, analysis runs are rate-limited, change decisions store a hash of the exact content shown, Markdown strips control/bidi characters and escapes every interpolated field, a failing mailer no longer distinguishes valid links.
Accepted / deferred (documented, not fixed): append-only is trigger-enforced on SQLite only (add Postgres triggers or revoke UPDATE/DELETE before any Postgres deployment); per-IP buyer throttle needs trusted-proxy handling behind a reverse proxy; `APP_ENV` defaults to development, so production deployments must set it explicitly; the link token travels in a query string (an OTP is still required); the fake outbox is unbounded and for tests/demo only; the heuristic provider ingests artifact text (a real provider needs prompt-injection handling).
