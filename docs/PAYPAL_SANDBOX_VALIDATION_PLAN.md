# PayPal Sandbox validation plan (one controlled demonstration)

> **Identifier note:** `SMOKE-0123456789AB` and `INV2-0000-AAAA-BBBB-CCCC` in this document are **synthetic placeholders** that stand in for the real Sandbox draft's invoice number and PayPal invoice ID. They are not genuine Sandbox identifiers. The real values are deliberately not published. The recorded events (one draft created, read back, 10 of 11 checks passed, merchant identity failed) are unchanged.

Status: PLAN ONLY. Nothing in this document has been run. No PayPal request was made while writing it. Every step that touches the network needs fresh, explicit owner approval in the conversation that runs it.

Baseline: the state-recovery MVP. Related: `STAGE_2D_SMOKE_TEST.md`, `S2_GUARDED_RECOVERY.md`, `S4_INTEGRATION_GUIDE.md`.

## 1. What is verified against real PayPal, and what is not

| Area | State |
|---|---|
| OAuth token, create one DRAFT invoice (placeholder number `SMOKE-0123456789AB`, 1.00 USD) | Observed once (2026-11-08) |
| Read-back of a DRAFT: status, total, invoicer, recipient; empty payments/refunds | Observed (read-only verifier) |
| `send_invoice` behaviour and response | Assumed, never observed |
| Real `payments.transactions[]` / `refunds.transactions[]` shape, `type` values, meaning of `payment_id` | Assumed from docs; covered only by `tests/fake_paypal.py` |
| Statuses SENT, UNPAID, PAID, PAYMENT_PENDING, REFUNDED etc. | Parsed by the adapter, never observed |
| Webhooks | No ingestion route exists. Polling `GET invoice` only |
| Merchant store (orders, stock, fulfillment) | Entirely simulated (`sim_*` tables). No real merchant system exists |
| Provider evidence label in this build | `SIMULATED_MOCK_PROVIDER` / MOCK_PAYPAL |

Consequence: a "genuine" demo can prove only that Caligo's provider adapter and reconciler correctly read a real Sandbox invoice that the owner's sandbox buyer actually paid. The merchant side stays simulated and must be labelled so.

## 2. Prerequisites (owner actions, not Claude's)

1. A PayPal Sandbox business account (merchant) and personal account (buyer), synthetic identities only.
2. Sandbox REST app credentials, typed by the owner in their own terminal as environment variables. Never pasted into chat, files, `.env*` read by Claude, or logs.
3. Merchant email and a recipient allowlist containing only the sandbox buyer address.
4. An isolated temporary SQLite DB (not the dev DB used by the running uvicorn server).
5. The settings must come from an init-only settings class (as in `scripts/demo_backend_full.py`) so `Settings(env_file=".env")` is never consulted.
6. Naming hazard: the app uses `PAYPAL_CLIENT_ID/SECRET`; the smoke and read-only tools use `PAYPAL_SANDBOX_CLIENT_ID/SECRET`. The demo runner must pick one and document it.
7. The existing draft (placeholder number `SMOKE-0123456789AB`) and `backend/.smoke/state.json` are not reused and not touched. The demo needs a NEW invoice, which needs its own approval.

## 3. Proposed demo (nine steps; dry-run is the default)

A new runner (not yet written) `scripts/sandbox_recovery_demo.py`, dry-run by default, `--execute` plus a confirmation env var for network steps, with a transport allowlist (token POST, create, send, GET of the one named invoice) and call budgets, like `sandbox_readonly.py`.

| # | Step | Network? | Provenance |
|---|---|---|---|
| 1 | Offline preflight: config present (names only), isolated DB, allowlist set, marker md5 unchanged | No | n/a |
| 2 | Create a seller, project, payment instruction for the sandbox buyer in the temp DB | No | Caligo |
| 3 | Create ONE 1.00 USD DRAFT invoice via the app (approval A) | Yes: token + create | Provider |
| 4 | Read back, reconcile: expect DRAFT, no anomalies | Yes: 1 GET | Provider |
| 5 | Send the invoice to the sandbox buyer (approval B) | Yes: send | Provider |
| 6 | Owner pays it in the Sandbox buyer UI (manual, not automated) | Owner | Provider |
| 7 | Sync: one GET; reconcile expects PAID, PAYPAL-type transaction with a payment id, amount and currency equal | Yes: 1 GET | Provider |
| 8 | Simulated merchant fails to create the order (injected in `sim_*`), diagnose and plan B, preview | No | Merchant (simulated) |
| 9 | Human approve and execute recovery in the simulated store; read the audit trail; evidence export | No | Merchant (simulated) |

Steps 8 and 9 reuse the existing S2/S4 engine unchanged. The only new behaviour is that the payment record comes from a real provider fetch. The `provenance_label` for the payment must say it is a Sandbox provider fetch, not `SIMULATED_MOCK_PROVIDER`; that label change is a code gap (section 5).

## 4. Permitted and prohibited operations

Permitted with approval: OAuth token, create one invoice, send that invoice, GET that invoice (budgeted).
Prohibited always: refund, cancel, pay via API, MARK_AS_PAID, record payment, delete, update, any other endpoint or host, live (non-sandbox) mode, retry of an ambiguous state-changing call, touching the smoke draft or marker, printing credentials or tokens, committing DB/log/state files.

## 5. Gaps to close before an execute run

1. Runner script with allowlist transport, budgets, dry-run default (new; needs tests with the fake PayPal).
2. Provider-evidence provenance: label real Sandbox fetches distinctly from mock (small schema/label change plus contract export).
3. Response shapes for PAID/payments are unobserved; the first real PAID read must be captured field-names-only (as the read-only verifier does) and the adapter checked against it. Expect possible adapter fixes.
4. `send_invoice` real behaviour unobserved; an ambiguous outcome must be recorded, not retried.
5. No webhook source; the demo states that polling is the only evidence path.
6. Config-name unification (section 2.6).

## 6. Expected outcomes and failure handling

Success: step 7 shows PAID with an `type=PAYPAL` transaction id; reconciler reports zero anomalies; steps 8-9 produce an approved, executed plan and an audit trail; integration health reaches TRANSACTION_VERIFIED only if the safe processor applies an order, otherwise stays DEGRADED/UNVERIFIED as designed.
Failure: any anomaly, ambiguous response, status mismatch or blocked request stops the run; the state is recorded; no automatic retry; owner decides.

## 7. Rollback and cleanup

Delete the temp DB. The Sandbox invoice can only be left or manually cancelled by the owner in the Sandbox dashboard (Caligo has no cancel method and must not gain one). Sandbox funds are not real. Unset credential environment variables. Record the invoice id, approval references and outcome in a run log under `docs/` (no secrets).

## 8. Evidence to collect

Per-step request counts, redacted response key names and status words, reconcile output, recovery audit events, marker md5 before/after, secret-scan result. Never raw tokens, credentials or full buyer email beyond the synthetic address.

## 9. Known limitations

One invoice, one currency, one buyer; no partial payments, refunds or disputes; no webhook; merchant store is simulated; this demonstrates adapter and evidence flow behaviour, not production certification.
