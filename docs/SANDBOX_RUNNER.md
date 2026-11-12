# Controlled PayPal Sandbox validation runner

Status: implemented and tested OFFLINE only. No PayPal request has ever been made by this runner. A real run needs separate, explicit owner approval for each network stage. Plan: `PAYPAL_SANDBOX_VALIDATION_PLAN.md`.

Code: `backend/app/sandbox_runner.py` (stages, CLI), `sandbox_runner_guard.py` (allowlist transport, run state, workdir rules), `simulation/runner_recovery.py` (offline steps 8-9), `sandbox_runner_rehearsal.py` (offline nine-step rehearsal), `sandbox_attestation.py` (provenance). CLI: `scripts/sandbox_validation_runner.py`. Tests: `backend/tests/test_sandbox_runner.py`.

## Usage

```
backend/.venv/Scripts/python scripts/sandbox_validation_runner.py                           # offline rehearsal (default): fake PayPal, temp DB, no network
backend/.venv/Scripts/python scripts/sandbox_validation_runner.py --stage create            # DRY RUN: prints the exact planned requests; no network, nothing created
backend/.venv/Scripts/python scripts/sandbox_validation_runner.py --stage create --execute --workdir <dir>      # REAL (needs approval): see below
```

Stages: `rehearse` (default), `create`, `send`, `read`, `recover`. A stage without `--execute` is a dry run. The dry run of a real stage still needs the four configuration variables to be present (names are checked, values are never printed).

| Stage | Network | Requests it can ever make | Extra gate |
|---|---|---|---|
| `rehearse` | none (fake PayPal) | n/a | none |
| `create` | Sandbox | OAuth token (<=2); POST create once (one 1.00 USD item for the configured buyer); GET of the id the create returned (<=3) | `PAYPAL_RUNNER_CONFIRM_CREATE=I_APPROVE_CREATING_ONE_SANDBOX_DRAFT_INVOICE` |
| `send` | Sandbox | OAuth token (<=2); POST send once for the recorded invoice with the fixed body; GET (<=2) | `PAYPAL_RUNNER_CONFIRM_SEND=I_APPROVE_SENDING_THE_SANDBOX_INVOICE_EMAIL`; create OK and DRAFT verified |
| `read` | Sandbox | OAuth token (<=2); GET of the recorded invoice (<=2) | `PAYPAL_RUNNER_CONFIRM_READ=I_APPROVE_READING_THE_SANDBOX_INVOICE`; create OK |
| `recover` | none | n/a | a PAID read recorded in the run state |

Each real stage also needs `--execute` and a `--workdir` outside the repository, `backend/.smoke` and the home directory. The work directory holds `run.sqlite` (the isolated database) and `run_state.json` (non-secret facts: run id, masked buyer, invoice id, stage markers). The buyer (person) pays the sent invoice in the Sandbox buyer UI between `send` and `read`; nothing in the runner pays.

Exit codes: 0 ok, 1 check failed, 2 refused, 3 unreadable provider response, 4 provider error, 5 request blocked, 6 ambiguous mutation.

`read` exits 1 (check failed) when its reconciliation check fails. That check passes when the local record's invoice state equals the provider's state word and its financial status is exactly the one reconciliation derives from it: a DRAFT is correctly `UNVERIFIED` (unsent, nothing owed or paid), SENT/UNPAID are `PENDING`, PAID is `PAID`. An authenticated read proves the invoice's identity and state only; it never proves a payment.

**Uncertain 4xx on a mutation.** After a create/send, only a 4xx that PayPal answered with its own structured error (400/404/422 with an error `name`, or 401/403) is a definitive REFUSED. Any other 4xx (406, 415, 408, 409, a gateway body with no PayPal error name) is AMBIGUOUS: `ambiguous` is recorded, exit code 6, and no later mutation runs. The first real SEND returned 406 MEDIA_TYPE_NOT_ACCEPTABLE and was recorded as REFUSED under the old rule; a read-only check afterwards showed the invoice still DRAFT.

## Configuration mapping (single choice for the runner)

The runner reads only these process environment variables (never a file; `.env` and `APP_ENV` are never consulted) and builds the app Settings from them alone:

| Variable | Maps to |
|---|---|
| `PAYPAL_SANDBOX_CLIENT_ID` | `Settings.paypal_client_id` |
| `PAYPAL_SANDBOX_CLIENT_SECRET` | `Settings.paypal_client_secret` (SecretStr) |
| `PAYPAL_SANDBOX_MERCHANT_EMAIL` | `Settings.paypal_sandbox_merchant_email` |
| `PAYPAL_SMOKE_BUYER_EMAIL` | `Settings.paypal_sandbox_recipient_allowlist` (the buyer only) |

These are the names the smoke test and read-only verifier already use; the app's own `PAYPAL_CLIENT_ID/SECRET` are deliberately not read, so a credential exported for something else is never used by accident.

## Safety controls (each covered by `test_sandbox_runner.py`)

* Default dry run; real stages need `--execute`, a per-stage confirmation variable, a safe workdir and run-state preconditions. The create approval does not authorise a send.
* `RunnerTransport` allowlist: https, host `api-m.sandbox.paypal.com` only (all production hosts, other schemes, ports, userinfo, queries and fragments are blocked before sending); only the endpoints in the table; the create body must be exactly one 1.00 USD item for the configured buyer; the send body is fixed; one mutation per stage; token/read budgets; no refunds, captures, transfers, cancels, mark-as-paid, reminders, deletes, updates, other invoices.
* The existing smoke draft (`INV2-0000-AAAA-BBBB-CCCC`, numbers starting `SMOKE-`) is on a deny list; `backend/.smoke/state.json` is never opened (a test scans the source and a fixture hashes the file before and after every test).
* No automatic retry: the provider is built with `max_attempts=1`; "attempted" is persisted BEFORE a mutation leaves the process; any mutation without a clean 2xx is recorded AMBIGUOUS and blocks every later mutation until a human inspects the Sandbox and starts a new run directory.
* Unknown real-provider shapes fail closed: the status word, top-level key names and transaction `type` words (sanitized, never values) are printed and the run state does not advance.
* Output passes through a redactor (secrets, bearer tokens, emails masked); the run state holds no credentials, tokens or full emails.

## Provenance

Provider evidence labels in diagnosis output (`provenance_label`) are now three-valued:

| Label | Meaning |
|---|---|
| `SIMULATED_MOCK_PROVIDER` | the MOCK provider; never genuine, can never be promoted |
| `UNATTESTED_SANDBOX_PROVIDER` | the Sandbox adapter produced the record, but nothing attests it came from a real authenticated read (fake PayPal in tests and in the rehearsal; was `SANDBOX_PROVIDER`) |
| `AUTHENTICATED_PAYPAL_SANDBOX` | promoted only by the runner (`app/sandbox_attestation.py`; `attest` may be called from `sandbox_runner.py` only, enforced by a test) when EVERY condition below holds |

### Promotion condition (all must hold; otherwise the evidence stays `UNATTESTED_SANDBOX_PROVIDER` and the reasons are printed as fixed words)

1. **Transport authenticity:** the transport object is exactly `httpx.HTTPTransport` (`type(inner) is httpx.HTTPTransport`). A mock, wrapper or subclass is never live (`transport_not_live`).
2. **Origin and request:** the request that produced the response was https, host `api-m.sandbox.paypal.com`, no port/userinfo/query, path exactly `/v2/invoicing/invoices/{the run's invoice id}`; the response is HTTP 200 (`origin_or_path_mismatch`, `status_not_200`).
3. **Schema/identity of the body (`authenticity_failures`):** a JSON object whose `id` equals the run's invoice id and whose `detail.invoice_number` equals the number the app sent (`invoice_number_for(payment id)`); `invoicer.email_address` present and equal to the configured merchant (`merchant_missing`, `merchant_mismatch`); exactly one primary recipient equal to the configured buyer; `amount` exactly `1.00 USD`; a recognised invoice status.
4. **Payment data complete and consistent with the status:** every payment/refund entry is a `PAYPAL`-type transaction with a non-empty id and a USD amount (EXTERNAL, missing ids, other currencies, non-list/non-object containers fail); `PAID` needs payments summing to exactly 1.00 USD, `PARTIALLY_PAID` a partial sum, draft/sent/unpaid/cancelled states no payments (`payment_status_unverifiable`, `payments_transaction_data`, `refunds_transaction_data`).
5. **Reconciliation:** the sync returned success, the payment record id and `paypal_invoice_id` are the run's, and the reconciled status is not `NEEDS_REVIEW`.

The same checks (minus liveness and the not-yet-known invoice number) gate `draft_verified` after `create`, so `send` is refused when the provider's own read-back shows another merchant, recipient, amount or no merchant at all. **Known consequence:** if the real Sandbox omits `invoicer` on reads (it did on the first draft read-back), no evidence can be promoted and `send` will be refused; the response key names are then printed so the expectation can be revisited with fresh approval.

### Where the label appears (additive fields)

| Surface | Field | Values |
|---|---|---|
| Diagnosis evidence | `provenance_label` | the three labels above |
| `GET /projects/{id}/payment-observations` | `provider_provenance` (new, optional) | the three labels for `PROVIDER_API` observations; `null` for seller claims |
| Unified report JSON + markdown | `payments.payments[].provider_provenance`, markdown `[LABEL]` | the three labels |
| Stage-3 evidence report JSON + markdown | `payments.records[].provider_provenance` | the three labels |

The change is backward compatible (new optional fields; `contracts/PaymentObservationView.schema.json`, `SellerObservationResult.schema.json` and `openapi.json` were regenerated). Persistence limitation: the attestation lives in process memory; the runner re-attests from `run_state.json` at the start of `read`/`recover`. An API server process that did not run the runner shows `UNATTESTED_SANDBOX_PROVIDER` for the same payment (fails safe). `run_state.json` is local, operator-trusted data and is not tamper-proof.

## Remaining genuine verification steps (not done; each needs approval)

1. `create` (one DRAFT invoice) then confirm the read-back. 2. `send` to the sandbox buyer. 3. Owner pays in the Sandbox buyer UI. 4. `read` and capture the real PAID response shape (key names and `type` words); fix the adapter if it differs. 5. `recover` on the real PAID evidence. 6. Record the outcome (no secrets) under `docs/`.

**After an ambiguous mutation** the `read` stage (and only `read`) remains allowed, because a read-only look at the exact recorded invoice is how an ambiguous outcome is resolved. It still needs `--execute`, its own confirmation variable and a recorded invoice id. It records what it saw (`last_status`) but never clears `ambiguous`, never resets `send_attempted`/`create_attempted`, and `create`/`send` stay refused for that run.

**Provider error metadata.** For every non-2xx create/send response the run state records `last_mutation_http`, `last_mutation_error` (PayPal's error `name`, upper-case token only) and `last_mutation_debug_id` (the `PayPal-Debug-Id` header or body `debug_id`, only when it is a short plain token). They are printed on the `create:`/`send:` line for a support ticket. Messages and bodies are never stored. Older run states without these fields load unchanged.

**A blocked automatic retry still records the outcome.** If the adapter retries after a 429 and the guard blocks the second mutation, the first response's outcome (REFUSED for a 429) is saved instead of being lost.

**READ amounts.** The `read` check also requires consistent amounts: unpaid states (DRAFT, SENT, SCHEDULED, UNPAID, PAYMENT_PENDING) must have `amount_paid_minor == 0`; PAID must have `amount_paid_minor == amount_minor`; PARTIALLY_PAID must be strictly between.

**Hardening from the security review.** Error bodies are parsed only when they are at most 64 KiB and are valid, shallow JSON objects; anything else (huge, deeply nested, binary) is simply "not a PayPal error object", so a 4xx with such a body is ambiguous, never a clean rejection, and never raises. A create whose later request is blocked after the POST answered is recorded AMBIGUOUS, not OK. The error metadata is also kept when the failure path is an unexpected exception after a sent mutation. `run_state.json` is rejected as untrusted (`StateCorrupt`) if `last_mutation_error` or `last_mutation_debug_id` do not match the sanitizer shapes. Note: a run state written by this version contains the new `last_mutation_*` fields and will not load in an older runner (downgrade only); older states load here unchanged.
