# Setup and demo guide

Everything here runs on one machine with a **mock** PayPal provider, a fake mailer and synthetic data. Nothing contacts PayPal or any model provider. For the backend install (Python 3.11+, one-time PyPI download) follow `docs/DEV_SETUP.md`; the frontend needs Node 20+.

## What is and is not real

| Thing | Status in this demo |
|---|---|
| Payments and provider events | **Simulated** by the `MOCK_PAYPAL` provider. The UI says so in a banner and on each payment (`Source: Simulated mock provider, not PayPal`). |
| Merchant store (orders, stock) | **Simulated** (`SIMULATED_MERCHANT_STATE`). Recovery changes only this. |
| PayPal Sandbox | A Sandbox adapter exists. Real contact has been limited: one synthetic draft invoice was created and read back as DRAFT (never sent, never paid); one identity check did not pass. **No Sandbox payment has been verified as paid, and this demo does not show Sandbox evidence.** See `docs/PAYPAL_SANDBOX_STATUS.md`. |
| Buyer decisions | Real flow: one-time code from the (fake) mailbox, recorded as mailbox control, not legal identity. |
| Stanza | Real local NLP inference from a local model cache. Advisory only. It is not a generative model. |
| Generative AI / chat | None. No API keys, no model calls, no chatbot. |
| Money movement | None. No refund, cancel, capture or payout exists in the code. |

## Start (two terminals, from the repo root)

Backend with seeded synthetic data (state directory must be new, empty and outside the repo):

```
python scripts/demo_seeded_server.py --port 8011 --origin http://localhost:3001 \
  --state-dir <new empty dir> --stanza-model-dir <local Stanza 1.15.0 cache>
```

`--stanza-model-dir` is optional. Without it Stanza requests fall back to deterministic rules and the UI shows the fallback reason (designed and unit-tested in the backend; not exercised in the browser run). Model weights are never downloaded by this script and are not in the repository (`docs/STANZA_LOG_INTELLIGENCE.md`).

Frontend:

```
cd frontend && npm install
CALIGO_API_URL=http://127.0.0.1:8011 npx vite --port 3001 --strictPort
```

Open http://localhost:3001. Seeded seller (synthetic, local database only): `demo-seller@example.com` / `synthetic-placeholder-pw-1`. The server prints the buyer link token; the one-time code is written to `<state dir>/outbox.jsonl` after the buyer asks for it.

Environment variable names: backend `PAYPAL_MODE=mock`, `BUYER_MAIL_MODE=fake`, `AI_MODE=demo`, `DEV_OUTBOX_FILE`, `CORS_ORIGINS`, `DATABASE_URL`, `STANZA_MODEL_DIR` (the seeded script sets these itself); frontend `CALIGO_API_URL`. No secrets are read or needed.

**Reset between takes:** stop the backend and start it again with a new `--state-dir`. The buyer token is single use and recovery executes once, so every take needs a fresh server.

## Demo sequence (target under 3 minutes; times are estimates, rehearse once)

1. **0:00 Landing** (`/`). Say what it is: scope, delivery, payments, recovery, one evidence record. Point at the "illustrative data" label. (10 s)
2. **0:10 Sign in** as the seller. Note the banner: simulated mode, nothing here is PayPal evidence. (10 s)
3. **0:20 Buyer decision.** Open the "SYNTHETIC delivery review" project, Delivery evidence: a pending buyer request exists. In a second window open `/buyer?token=<printed token>`, send the code, paste it from `outbox.jsonl`, choose **Acknowledge delivery**. Back in the seller view: status reads "Acknowledged only" (not accepted), and the seller has no control to record or forge a decision. (45 s)
4. **1:05 Payments.** Open "SYNTHETIC paid, merchant failed" > Payments. The payment reads Paid and says its source is the simulated provider. (15 s)
5. **1:20 Diagnosis.** Scenario "Paid not applied", pick the payment, Diagnose. Show: advisory, rules only, generative model not used, cited evidence with provenance labels `SIMULATED_MOCK_PROVIDER` vs `SIMULATED_MERCHANT_STATE`. (25 s)
6. **1:45 Stanza.** Log intelligence, mode Stanza, paste two log lines, Analyse. Show "Requested Stanza, effective Stanza", advisory, untrusted-log wording. First run takes about 14 s while the model loads, so run it once before recording. (20 s)
7. **2:05 Guarded recovery.** Recovery > Propose a plan > read the dry-run ("nothing has changed") > **Approve this exact plan** > **Execute on simulated store** > read the confirmation (simulated store only, no refund/cancel/capture) > confirm. Show "Executed on the simulated store" and the plan history. (45 s)
8. **2:50 Close.** No generative AI, no money movement, mock provider; Sandbox limited to one read of a draft. (10 s)

## What was actually verified (this branch, this machine, 12 Nov 2026)

- **Real Chromium (Playwright), mock backend:** landing, login, `/buyer`, register to dashboard, project creation, hard refresh on nested routes and all 12 project pages (no blank pages, no page errors); `frontend/e2e/browser-smoke.mjs`.
- **Real Chromium against the seeded server, one fresh run:** buyer code flow and decision; seller sees "Acknowledged only" with no forging control; Payments labelled simulated; Diagnosis with provenance labels; Stanza requested and effective mode both Stanza (13.8 s first inference, local model cache); recovery propose, approve, confirm, execute; no console or page errors apart from expected signed-out 401s. `frontend/e2e/browser-demo.mjs`.
- **Backend flows, in-process on temporary SQLite with the mock provider:** `scripts/demo_backend_full.py` 19/19 and `scripts/demo_recovery_integrated.py` 40/40 (instruction hold, buyer rejection/approval, duplicate-order, paid-not-applied, stale-paid-after-refund and unproven-health recoveries, idempotent execution, cross-seller isolation).
- Frontend: typecheck clean, 12 unit tests, production build, 13/13 API E2E.
- Clean-machine install of this exact tree (fresh virtualenv from `pip install -e ./backend[dev]`, `npm ci`): `verify_environment.py` ENVIRONMENT OK; backend suite across `tests`, `diagnosis_tests`, `health_tests` and `nlp_tests` run in six parallel shards: 1,617 passed, 3 skipped, 0 failed; frontend typecheck, 12 tests and build; 13/13 API E2E; browser demo run with no Stanza cache (shows the deterministic fallback with its reason).

## Not verified (say so if asked)

- Authenticated PayPal Sandbox evidence appearing in the UI. No Sandbox payment has been verified as paid.
- Buyer payment-instruction change approval in the browser (needs an enrolled authenticator; no seller enrolment endpoint exists).
- Stanza fallback path in the browser; Firefox, Safari, mobile layouts; keyboard and screen-reader behaviour; automated accessibility audit.
