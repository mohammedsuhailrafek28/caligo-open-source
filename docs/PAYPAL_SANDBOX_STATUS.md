# PayPal Sandbox integration: what exists and what has been proven

Short version: Caligo contains a tested PayPal Sandbox adapter and a strict rule for when a record may be called authenticated Sandbox evidence. Real Sandbox contact has been limited, and **no record in this project currently meets that rule**. The demo is simulated and says so.

## What is implemented (all tested offline against a fake PayPal)

- `backend/app/paypal_sandbox.py`: Sandbox-only adapter (create a draft invoice, read it back, explicit send, sync). Host allowlist is `api-m.sandbox.paypal.com`; there is no live mode, and no refund, cancel, capture or payout method exists.
- `backend/app/sandbox_readonly.py` and `scripts/sandbox_verify_invoice.py`: read-only verifier for one existing invoice (an OAuth token request and one GET of that exact invoice, nothing else).
- `backend/app/sandbox_runner.py`, `sandbox_runner_guard.py`, `sandbox_attestation.py`: a controlled runner. Only it may attest that a payment's evidence came from a fresh, authenticated, allowlisted Sandbox read, which is the only way a record can carry the label `AUTHENTICATED_PAYPAL_SANDBOX`. Mock records carry `SIMULATED_MOCK_PROVIDER` and can never be promoted; Sandbox-adapter records without that attestation carry `UNATTESTED_SANDBOX_PROVIDER`. The attestation is held in memory and is re-established by a new authenticated read; it is not stored.

## What real Sandbox contact has happened (limited, each separately approved by the owner)

- One real run created a single synthetic 1.00 USD **draft** invoice (seen in the Sandbox dashboard). The adapter reported an ambiguous outcome because of a response-parsing defect, which was then fixed offline (`docs/STAGE_2D_SMOKE_TEST.md`, incident record).
- Two read-only verifications of that draft followed. The draft was never sent, never paid, refunded or cancelled. The later run passed 10 of 11 checks and **failed the merchant-identity check**, because PayPal returned no `invoicer.email_address` for a draft that was created without one. By the project's own rules that draft's merchant is therefore **not verified**, and the payment would stay in review.
- The fix (send the configured merchant as explicit invoicer on create) has been verified only against the fake PayPal. Its real behaviour is unobserved.

## What has not been proven

- No Sandbox payment has been verified as paid. Response shapes for PAID, payments and refunds are unobserved against real PayPal.
- No record has been attested as `AUTHENTICATED_PAYPAL_SANDBOX`. The runner has never made a real request.
- The app's screens display the source of each payment record and a mode banner, but cannot show an authenticated Sandbox label until a record earns it.

## What the demo shows

Seeded, simulated data only (`docs/DEMO_GUIDE.md`). It does not contain, imply or reproduce Sandbox evidence.
