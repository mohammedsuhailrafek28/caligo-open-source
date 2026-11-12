# Stage 2C — PayPal Sandbox adapter (offline-verified)

Status: implemented and tested against a fake PayPal served through `httpx.MockTransport`. **No real PayPal credentials
were read, requested, generated or used, and no real PayPal request has been made.** Real sandbox authentication and the
first real API call need separate authorization.

## Flow
1. `POST /projects/{id}/invoices` — reserve a row (committed), call the provider (no DB transaction open), record the result.
   Creates a **DRAFT**; nobody is emailed. Sandbox mode needs an ORIGINAL scope record (linked as `scope_record_id`) and an
   allow-listed test recipient.
2. `POST /projects/{id}/payments/{payment_id}/send` with `confirm_recipient_email` — the only call that emails a person.
3. `POST /projects/{id}/payments/sync` — fetch each invoice from PayPal and reconcile. `GET` routes are read-only.

## Reconciliation rules (`app/services/reconcile.py`)
* `PAID` needs PayPal-processed (not EXTERNAL) payments summing to the stored total, matching invoice id, number, currency,
  total, recipient and (when configured) merchant. `MARKED_AS_PAID` never counts as payment.
* Inconsistent or unexpected evidence -> `NEEDS_REVIEW` (only from unverified/pending states); verified states are never
  silently downgraded, the anomaly is recorded in the append-only `payment_events`.
* Full refund -> `REFUNDED`; partial -> `PARTIALLY_REFUNDED`. Project `PAID` means "a verified payment was received"; refunds
  never change it.
* Replacement invoice: only through explicit `replaces_payment_id` after the previous invoice is CANCELLED/REFUNDED; one active
  invoice per project is enforced by a partial unique index. History is never deleted.

## Failure handling
Create/send are never blindly retried after an ambiguous outcome. `operation_state` (RESERVED, CREATING, CREATE_UNCERTAIN,
SEND_PENDING, SEND_UNCERTAIN, READY) is committed before each provider call; retrying create with the same Idempotency-Key
resumes with the same `PayPal-Request-Id` and the deterministic invoice number; sync resolves uncertain sends.

## Open items for the credential step (not verified offline)
* Confirm the PayPal-Request-Id idempotency window for `POST /v2/invoicing/invoices` and the exact create/send response shapes.
* Confirm the sandbox invoice `status` values and `payments`/`refunds` transaction shapes against real responses.
* No cancel endpoint is wired: cancellation is observed through sync.
* Sync fetches sequentially without a rate budget; add one before large volumes.

## Residual risks accepted at the end of Stage 2C (from the security, FastAPI and Python reviews)
* (Resolved in Stage 2D) NEEDS_REVIEW is sticky and exits only through the audited `resolve-review` route.
* An uncertain send is declared "not sent" only after 5 minutes of the provider reporting DRAFT (prevents a second email).
* Rate limit is per process, in memory (single node). Sync is sequential per project.
* (Resolved in Stage 2D) `sync` now returns an error status with per-payment `details` when any payment could not be synchronized.
* (Resolved in Stage 2D: shutdown closes the provider client.) List endpoints are unpaginated; 413/500 responses lack CORS headers.
* OpenAPI does not document error responses; `contracts/API_CONTRACT.md` does.
