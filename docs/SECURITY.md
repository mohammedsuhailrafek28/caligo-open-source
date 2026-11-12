# Threat model and security gates

## Sensitive objects
PayPal OAuth secret, buyer emails, scope documents, handover links, file storage keys, event metadata, evidence reports.

## Key threats
- Cross-seller access to project data (IDOR).
- Fraudulent event insertion or replay / access tokens leaked in logs.
- Buyer link forwarding and unreliable identity attribution.
- Prompt injection in client communications leading to invented assertions.
- Private client documents sent to AI provider without authorization.
- PayPal webhook spoofing/replay and duplicate notifications.

## Required before exposing business routes
- Seller session auth and per-object ownership authorization.
- Short-lived, one-time delivery/OTP challenges; store token hashes, not plaintext.
- Explicit buyer notice, minimized access logs, retention/deletion policy.
- File allowlists, upload size bounds, private object storage, malware screening as appropriate.
- Model output must match persisted evidence refs and be checked against source text.
- PayPal server-to-server invoice/payment verification; verified webhook signatures.
- Secret management, request-id tracing, rate limiting, safe log redaction.
- Never claim cryptographic proof of receipt or PayPal account-holder identity from an access log.

For the hackathon demo, use synthetic users and project files only.

## Secret scanning (current state)
`scripts/check_secrets.py` is a local heuristic. Run it as `python scripts/check_secrets.py --git`
before every commit; it scans exactly the files git would track and prints `path:line: rule` only,
never matched text. It covers: PayPal/AI key assignments, known token shapes (GitHub, AWS, Slack,
Google, `sk-` keys, JWTs, bearer tokens, PayPal-style tokens), private key blocks, credentials in
URLs, generic quoted secret assignments, and any `.env`/`.env.*` file other than `.env.example`.

Limitations: no git-history scan; no entropy analysis of arbitrary strings; short values (<8 chars)
and digit-free unquoted values on the named PayPal/AI variables are ignored; files over 1 MB and
binary files are skipped; novel token formats are not recognized. Gitleaks was not installed
(not present on the machine; installing system packages was out of scope). **Run gitleaks or
trufflehog, including history, before any push or public release.**

## Implemented in Stage 2B (all covered by tests in `backend/tests/`)
- **Passwords:** Argon2id (`argon2-cffi` defaults), 12–128 characters, never stored or logged. Unknown accounts still pay for one
  Argon2 verification so timing does not reveal registered emails. Registration answers identically for existing emails.
- **Sessions:** opaque 256-bit tokens; the database stores only SHA-256 digests (session and CSRF). Absolute expiry
  (`SESSION_TTL_MINUTES`), server-side revocation on logout, fresh token on every login. Cookies are `HttpOnly` (session),
  `SameSite=lax|strict`, and `Secure` when `COOKIE_SECURE=true` (enforced in production settings).
- **CSRF:** every authenticated mutation needs `X-CSRF-Token` equal to the cookie and to the digest bound to that session; a present
  `Origin` must be in `CORS_ORIGINS`; CORS uses explicit origins only (`*` is rejected).
- **Authorization:** every project route loads the project through `get_owned_project`; other sellers' resources return the same 404
  as nonexistent ones. Request bodies cannot carry `seller_id`, `status` or `provider_status` (rejected with 422).
- **Throttling:** failed logins are counted per account and per IP (429 with `Retry-After`).
- **Payments (MOCK only):** `Idempotency-Key` required; same key + same payload replays, different payload returns 409; the database
  allows one non-cancelled invoice per project. `PAID` is assigned in exactly one function and only when the provider response matches the
  stored invoice id, currency and amount and carries a transaction id; provider errors fail closed. No HTTP route can mark a payment paid.
- **Evidence integrity:** scope records are append-only (no update/delete route, ORM guard, SQLite triggers); the SHA-256 is computed by the server.
- **Errors and logs:** contract error envelope with a server-generated request id; validation errors never echo input; audit logs carry
  ids only and pass through a redaction filter; tests assert no password, session token or CSRF token appears in logs.
- **Production posture:** `/docs` and `/openapi.json` are disabled when `APP_ENV=production`; production refuses `DEV_AUTH_ENABLED`,
  insecure cookies, and any non-`disabled` `PAYPAL_MODE`.

## Implemented in Stage 2C (covered by `test_paypal_adapter.py`, `test_sandbox_flow.py`, `test_payments.py`)
- Sandbox-only adapter with a constant base URL; no live mode. Credentials come from environment variables, are held as `SecretStr`,
  sent only to the OAuth endpoint, never logged; tokens are cached in memory and renewed before expiry. `app.provider` logs carry op/status only.
- Explicit timeouts; bounded retries with backoff, only for safe operations or requests that provably never reached PayPal.
- No DB transaction is held across a provider call (tested); interrupted operations persist and recover.
- Financial state derives only from verified provider evidence in one reconciliation module; fail-closed on inconsistency;
  `payment_events` is append-only (ORM and SQLite triggers); GET is read-only.
- Unit tests block non-loopback sockets, so no real PayPal request can occur.

## Known residual risks (accepted for the hackathon build; from the Stage 2B ECC security, FastAPI and Python reviews)
No Critical or High findings remained after fixes. Open items, most important first:
- **Account pre-registration (Medium):** there is no email verification, and registering an existing email answers identically
  (to avoid enumeration). An attacker could register a victim's address first; the victim's later signup then silently no-ops.
  Needs an email-verification flow, which arrives with the Gate 2 email infrastructure. Do not expose publicly until then.
- **Targeted lockout (Medium):** failed logins are throttled per account, so an attacker can lock a known email for the window.
  Per-process in-memory throttle: it resets on restart, multiplies by worker count, and sees only the proxy address behind a reverse
  proxy (X-Forwarded-For is deliberately not trusted). Memory is bounded (10,000 keys). Check-then-record is not atomic, so
  concurrent guesses can exceed the limit by up to the worker-thread count.
- **Request size:** only the declared `Content-Length` is capped (default 1 MB, then 413); chunked uploads must be capped at the proxy.
  No per-seller limits on projects, scope records or sessions; list endpoints are unpaginated.
- **Registration cost:** registration is throttled per IP (default 10 per window), but each accepted call runs a full Argon2id hash.
- **Payments:** a `REFUNDED` record leaves the project `PAID` and still blocks a new invoice (product decision pending). The provider call
  runs inside the database transaction, which is harmless for the mock but must be restructured (reserve, commit, call, update) for a
  real adapter. Concurrent verification is last-writer-wins on provider-derived facts; terminal states (`CANCELLED`, `REFUNDED`) are
  re-read before writing and never overwritten, and a payment arriving after a cancel is flagged `PAID_AFTER_CANCEL_MANUAL_REVIEW`, never applied.
- **Defaults:** `APP_ENV` defaults to `development` (insecure cookies, `/docs` on, mock payments allowed); production settings are
  validated, but nothing forces `APP_ENV=production` on a real deployment. No `__Host-` cookie prefix; currencies are not checked against an ISO list.
- Expired sessions of *other* sellers are never purged (a seller's dead sessions are purged on their next login).
- No password reset, MFA, or `needs_rehash` upgrade path. Registration timing differs by a few milliseconds for new vs existing emails.
- `GET .../payments` is a state-changing GET with no rate limit (it calls the provider once per record).
- The mock provider keeps state in process memory; SQLite is single-writer; no migrations; the real PayPal adapter, webhooks and
  signature verification do not exist, so nothing here proves a real payment.

## Review findings still deferred to later gates
- No token/OTP tables for buyer delivery -> Gate 2 (store hashes only).
- `EvidenceClaim.source_refs` is an unvalidated JSON list -> Gate 3 service-layer validation against same-project records.
- Delivery events are not append-only or idempotent -> Gate 2 (idempotency key; no update/delete routes).
- `ScopeRecord.content` retention/deletion policy and `Deliverable.storage_key` uniqueness -> Gate 2.
- Security headers beyond `nosniff`/`no-referrer`/`no-store`, and `create_all` instead of migrations.
Fixed by the baseline commit and Stage 2B: secrets are `SecretStr`; `DEV_AUTH_ENABLED` rejected in production; SQLite foreign keys
enforced; enum/CHECK constraints; UTC-aware datetimes; client input cannot carry `seller_id` or `provider_status`; session tables;
tenant scoping through one ownership dependency; `/docs` hidden in production; request ids.
