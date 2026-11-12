# Stage 2D — PayPal Sandbox smoke test (procedure; NOT yet run)

> **Identifier note:** `SMOKE-0123456789AB` and `INV2-0000-AAAA-BBBB-CCCC` in this document are **synthetic placeholders** that stand in for the real Sandbox draft's invoice number and PayPal invoice ID. They are not genuine Sandbox identifiers. The real values are deliberately not published. The recorded events (one draft created, read back, 10 of 11 checks passed, merchant identity failed) are unchanged.

**Status:** the tooling is built and verified only against the fake PayPal. No real credential has been read or requested,
and PayPal has not been contacted. Running the steps below needs your explicit go-ahead.

## What it does (and cannot do)
`scripts/sandbox_smoke_test.py` (logic in `backend/app/sandbox_smoke.py`):
1. Prints the prerequisite checklist and validates the environment variables (names only; values are never printed).
2. **Dry run by default** — no network. A real run needs `--execute` **and** `PAYPAL_SMOKE_CONFIRM=I_UNDERSTAND_THIS_CALLS_PAYPAL_SANDBOX`.
3. OAuth 2.0 client-credentials against `api-m.sandbox.paypal.com` (constant URL; there is no live mode).
4. Creates **at most one** synthetic **DRAFT** invoice (1.00 USD, number `SMOKE-<random>`, recipient = your sandbox buyer).
5. Reads the same invoice back and checks: invoice id, invoice number, merchant (must equal your sandbox business email),
   total, currency, status = `DRAFT`, recipient, and that there are no payments or refunds.

It contains **no code path** that sends the invoice, records a payment or refund, cancels, reminds or changes status
(a test greps the source for this). The provider is built with `max_attempts=1`, so nothing is ever retried automatically.

## Safety rules enforced in code
* The smoke test reads dedicated variables `PAYPAL_SANDBOX_CLIENT_ID` / `PAYPAL_SANDBOX_CLIENT_SECRET`, not the generic `PAYPAL_CLIENT_*` names, so a production credential exported for another purpose is never used.
* The marker is claimed atomically (exclusive create) **before** OAuth, so two overlapping runs cannot both create an invoice. `--marker` can point elsewhere for testing; do not use it to bypass the guard.
* The marker file `backend/.smoke/state.json` (git-ignored; holds identifiers only) is written **before** the create call.
  If a run ends ambiguously (timeout, 5xx, unexpected status, unreadable read-back) the marker stays and **further runs are
  refused** until you check the sandbox dashboard for `SMOKE-…` invoices and delete the marker yourself.
* A definitive failure before anything was created (OAuth failure, 403/422/429, connection refused) removes the marker so you may fix the cause and rerun.
* All output is passed through a redactor: client id/secret, bearer tokens and PayPal-style tokens are replaced with
  `[REDACTED]` and email addresses are masked (`b***@domain`).
* The script reads only process environment variables. It never reads environment files and never builds the app settings.

## Prerequisites (check manually in the PayPal Developer Dashboard first)
1. Dashboard > **Apps & Credentials** with the **Sandbox** toggle on (not Live).
2. A Sandbox **REST app** exists; you have its client ID and secret.
3. A Sandbox **Business** account (merchant) exists. Its login email is `PAYPAL_SANDBOX_MERCHANT_EMAIL`.
4. A Sandbox **Personal** account (synthetic buyer) exists. Its email is `PAYPAL_SMOKE_BUYER_EMAIL`. Do not use a real person's address.
5. The business account can open Invoicing (sign in at sandbox.paypal.com as that business user and open *Invoicing*).
6. The app belongs to that business account, so invoices are issued by it.

The script can only confirm prerequisites 2–6 indirectly: OAuth success proves the credentials; a successful create proves
Invoicing is available; the merchant check proves the invoice was issued by the expected business account.

## Exact instructions (Windows PowerShell, run in YOUR OWN terminal)
Type the secret yourself; do not paste credentials into the chat, a file, or a command line.
```powershell
cd <repo root>

# 1. credentials: current shell only, never written to disk
$env:PAYPAL_SANDBOX_CLIENT_ID = Read-Host "Sandbox client id"
$sec = Read-Host "Sandbox client secret" -AsSecureString
$env:PAYPAL_SANDBOX_CLIENT_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
$env:PAYPAL_SANDBOX_MERCHANT_EMAIL = "<sandbox business account email>"
$env:PAYPAL_SMOKE_BUYER_EMAIL     = "<sandbox personal account email>"

# 2. dry run (no network): validates the environment and prints the plan
backend\.venv\Scripts\python.exe scripts\sandbox_smoke_test.py

# 3. the real, single sandbox run (after you have approved it)
$env:PAYPAL_SMOKE_CONFIRM = "I_UNDERSTAND_THIS_CALLS_PAYPAL_SANDBOX"
backend\.venv\Scripts\python.exe scripts\sandbox_smoke_test.py --execute

# 4. clean up the shell
Remove-Item Env:PAYPAL_SANDBOX_CLIENT_ID, Env:PAYPAL_SANDBOX_CLIENT_SECRET, Env:PAYPAL_SANDBOX_MERCHANT_EMAIL, Env:PAYPAL_SMOKE_BUYER_EMAIL, Env:PAYPAL_SMOKE_CONFIRM
```
Optionally delete the leftover draft in the sandbox dashboard (it was never sent). If Claude Code runs the script for you,
use `! <command>` only after the variables are set in that same shell, and never type the secret into the chat.

## Exit codes
| Code | Meaning | Next step |
|---|---|---|
| 0 | Dry run finished, or all checks PASSED | — |
| 1 | Created and read back, but a check FAILED (see `[FAIL]` lines) | Inspect the draft; fix the cause; remove the marker to run again |
| 2 | Refused before any network call (missing/invalid env, no `--execute`/confirmation, marker present) | Fix the input |
| 3 | AMBIGUOUS creation or read-back: the invoice may exist | Inspect the dashboard for `SMOKE-…`; delete the marker only afterwards |
| 4 | OAuth failed, or PayPal refused/limited the create before anything existed | Check app type, sandbox credentials, Invoicing permission; rerun is allowed |

## Expected PASS output (shape)
`OAuth OK.` → `Created draft invoice INV2-…` → nine `[PASS]` lines → `Invoice INV2-…: status DRAFT, 1.00 USD. It was NOT sent and NOT paid` → `RESULT: PASS`.

## Things this run will finally confirm against real PayPal (currently only assumed)
* The shape of the create response (`href` link) and the invoice JSON (`status`, `amount`, `invoicer.email_address`, `primary_recipients`).
* That `PayPal-Request-Id` and the unique `invoice_number` behave as assumed for resuming an ambiguous create (this script does not exercise resume).
* Whether the sandbox rejects unusual values (e.g. the `reference` or `memo` fields).
Record any difference as a defect before wiring the application to the sandbox.

## Incident record: first real run (2026-11-08)
The first real sandbox run created draft invoice `SMOKE-0123456789AB` (1.00 USD, confirmed in the dashboard) but the adapter reported
`ProviderAmbiguousOutcome` with an empty `invoice_id`, and the marker was left as `AMBIGUOUS` (correct, safe behaviour).

**Cause (diagnosed offline):** an empty id means the failure was in create-response parsing, not in the follow-up read. The parser
required the response link host to equal `api-m.sandbox.paypal.com` exactly and ignored the documented `links` array. PayPal's
HATEOAS links need not use the host that was called (e.g. `api.sandbox.paypal.com`), so a valid 201 was judged "unreadable".
The exact real response has not been observed (no raw body was ever logged), so this is the most likely cause, not a proven one.

**Fix:** the invoice id is now taken from `id`, or from a `https://*.paypal.com/v2/invoicing/invoices/<id>` link (or the `rel=self`
entry of `links`); sources must agree; the id is regex-validated; every follow-up request still goes to the fixed sandbox host.
The create request now sends `Prefer: return=representation` (documented to return the invoice body). Unreadable responses log only a
reason code and top-level key names, and the smoke script prints the reason, so a recurrence is diagnosable without leaking data.

**State left untouched:** `backend/.smoke/state.json` still blocks further runs; the existing draft was not recreated or modified.
Re-verifying against real PayPal needs explicit approval (e.g. a read-only fetch of the existing invoice, or a new run after you
have inspected/deleted the draft and removed the marker yourself).

## Read-only verification of an existing invoice (added after the incident)
`scripts/sandbox_verify_invoice.py` (logic in `backend/app/sandbox_readonly.py`) re-checks one invoice that already exists, for example
`INV2-0000-AAAA-BBBB-CCCC` / `SMOKE-0123456789AB` (1.00 USD, DRAFT). It never creates, sends, pays, refunds, cancels or changes anything.

**What it may send (enforced by an allowlist transport, so anything else is blocked before it leaves the machine):**
1. `POST /v1/oauth2/token` (client-credentials OAuth), and
2. `GET /v2/invoicing/invoices/<that exact id>`.
Other methods, paths, hosts (including the live API), schemes, ports, query strings and other invoice ids are refused. The budget is two token
requests and two reads, which only allows the adapter's single token refresh after a 401. The provider uses `max_attempts=1`.

**It checks:** invoice id, invoice number, total, currency, status `DRAFT`, merchant (must equal `PAYPAL_SANDBOX_MERCHANT_EMAIL`), recipient
(must equal `PAYPAL_SMOKE_BUYER_EMAIL`), no payments, no refunds, no reported paid/refunded amount, and no recipient other than the buyer. Missing evidence fails. Values taken from PayPal's response are printed only if they have a strictly safe shape. If PayPal's body does not match the expected
shape the run stops with `UNREADABLE` and prints only the top-level key names and the status word, never values.

**It does not touch** `backend/.smoke/state.json` (it has no marker code at all), writes no files, reads no environment files, and prints no
credentials, tokens or full email addresses (emails are masked as `b***@domain`).

Dry run by default. A real run needs `--execute` **and** `PAYPAL_READONLY_CONFIRM=I_UNDERSTAND_THIS_READS_PAYPAL_SANDBOX`
(a different variable from the smoke test's, so one cannot authorize the other). Run it in YOUR OWN terminal; type the secret yourself:
```powershell
cd <repo root>
$env:PAYPAL_SANDBOX_CLIENT_ID = Read-Host "Sandbox client id"
$sec = Read-Host "Sandbox client secret" -AsSecureString
$env:PAYPAL_SANDBOX_CLIENT_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
$env:PAYPAL_SANDBOX_MERCHANT_EMAIL = "<sandbox business account email>"
$env:PAYPAL_SMOKE_BUYER_EMAIL     = "<sandbox personal account email>"

# dry run (no network)
backend\.venv\Scripts\python.exe scripts\sandbox_verify_invoice.py --invoice-id INV2-0000-AAAA-BBBB-CCCC --invoice-number SMOKE-0123456789AB --amount 1.00 --currency USD

# the single real read-only run (only after it has been approved)
$env:PAYPAL_READONLY_CONFIRM = "I_UNDERSTAND_THIS_READS_PAYPAL_SANDBOX"
backend\.venv\Scripts\python.exe scripts\sandbox_verify_invoice.py --invoice-id INV2-0000-AAAA-BBBB-CCCC --invoice-number SMOKE-0123456789AB --amount 1.00 --currency USD --execute

Remove-Item Env:PAYPAL_SANDBOX_CLIENT_ID, Env:PAYPAL_SANDBOX_CLIENT_SECRET, Env:PAYPAL_SANDBOX_MERCHANT_EMAIL, Env:PAYPAL_SMOKE_BUYER_EMAIL, Env:PAYPAL_READONLY_CONFIRM
```
| Exit | Meaning |
|---|---|
| 0 | Dry run finished, or every check passed |
| 1 | Invoice read, but a check FAILED (see the `[FAIL]` lines) |
| 2 | Refused before any network call, or a request outside the allowlist was blocked |
| 3 | The response was unreadable or unexpected (keys and status word are printed) |
| 4 | OAuth failed or PayPal refused the read (404, 403, 429, 5xx); one attempt, no retry |

Passing this check does not clear the smoke marker. Removing `backend/.smoke/state.json` remains a manual decision of the owner.

### Merchant identity diagnostics
The merchant comparison is strict and unchanged: the invoice's `invoicer.email_address`, trimmed and lower-cased, must equal
`PAYPAL_SANDBOX_MERCHANT_EMAIL` exactly. Aliases (`+tag`), sub-domains and look-alikes fail; no other field is used as a fallback.
When the check fails the verifier adds one line saying why, without printing any full address or any field value:
* `the invoicer object is absent` / `invoicer.email_address is absent` / `present but empty` / `is not a string`, or
* `present but differs: found m***@domain, expected x***@domain; domains match|differ; lengths match|differ`,
followed by the **names** of the fields inside `invoicer` (letters and underscores only), e.g. `invoicer fields: business_name, email_address`.
If it is "absent", PayPal did not return a merchant email for the draft. If it is "differs", set `PAYPAL_SANDBOX_MERCHANT_EMAIL` to the
address shown for that business account in the Sandbox dashboard (do not copy it from program output).

### Merchant identity: verified, missing, failed (and the explicit invoicer)
**Finding (second real read-only run, 2026-11-08):** the draft created without an `invoicer` came back with no `invoicer.email_address`
(`invoicer fields: (none)`). PayPal documents that field as optional, and OAuth success or the invoice being visible to the token does
not name the merchant account. So for that draft the merchant is **not verified**.

| Outcome | Meaning | Verifier / app result |
|---|---|---|
| **Verified** | `invoicer.email_address` is present and equals the configured merchant (trimmed, case-insensitive) | check passes; only this can PASS |
| **Missing evidence** | `invoicer` or its email is absent, empty or not a string | check FAILS (exit 1); the app keeps the payment in NEEDS_REVIEW |
| **Failed identity** | an email is present but differs (alias, sub-domain, look-alike, other account) | check FAILS (exit 1); the app keeps the payment in NEEDS_REVIEW |

There is **no override**: no flag, fallback field or "assume it is ours" mode. The existing draft (placeholder number `SMOKE-0123456789AB`) stays unverified
and is not modified.

**Change for future invoices:** when a merchant email is configured, `create_invoice` now sends
`invoicer: {"email_address": <configured merchant>}` so the read-back can verify it. PayPal's Invoicing troubleshooting guide states that the
invoicer email must match the Business account associated with the access token, so a wrong value should be rejected by PayPal at creation
(handled as a clean `provider_rejected`, nothing created). Echo-back or rejection has **not yet been observed against real PayPal**; the fake
models it, and the real error name is unconfirmed. A new real Sandbox draft is needed to observe it and requires separate approval.
