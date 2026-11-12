# R3 payment observations and merchant ledger

Status: implemented; guarded by `backend/tests/test_payment_observations.py`.

## 1. Purpose
An append-only, provenance-aware record of what each source said about a payment, so Caligo can show where a merchant's own records and the
payment provider's evidence **disagree** without inventing a payment fact. It feeds the future R4 divergence explanation; it explains nothing itself.

## 2. Sources and what they are worth
| Source | Written by | Confidence | Meaning |
|---|---|---|---|
| `PROVIDER_API` | reconciliation only (`reconcile.apply` -> `observations.record_provider_evidence`) | `PROVIDER_EVIDENCE` | what the provider returned to Caligo at the observed time |
| `MERCHANT_IMPORT` | seller CSV upload | `UNVERIFIED_CLAIM` | a claim; proves nothing about PayPal |
| `SELLER_ENTRY` | seller, one claim | `UNVERIFIED_CLAIM` | a claim |
| `WEBHOOK` | nobody in R3 | `UNCONFIRMED_WEBHOOK` | reserved; the interface (`record_webhook_observation`) refuses |

The **server** chooses source, confidence and actor; requests with those fields are rejected (`extra=forbid`) and CHECK constraints tie them together in the
database (a claim cannot be stored as `PROVIDER_EVIDENCE`; `INVOICE_STATE` exists only for `PROVIDER_API`). Source-asserted time (`asserted_at`) and server time
(`observed_at`) are separate columns; the first is never trusted. Each row stores its normalized payload, `payload_sha256`, a deterministic dedupe key and a plain-language provenance limit.

## 3. Authority
Payment status is decided only by `reconcile.evaluate/apply`. `evaluate` never reads observations; `apply` makes one append-only recording call. A merchant import
or claim never changes a payment status, project status, payment history or any payment-instruction hold, and never counts as R2 verification (tested, including a held project).
The comparison endpoint is read-only and advisory.

## 4. Merchant ledger import
JSON body `{"csv": "..."}`; UTF-8, at most 200 KB and 500 rows; required columns `kind, invoice_number, currency, amount_minor`, optional `transaction_id, asserted_at, note, merchant_row_id`;
integer minor units only; supported currencies only; timestamps need a timezone; notes may not start with `= + - @`. **All-or-nothing:** one bad row refuses the file and records nothing.
`invoice_number` must belong to a payment of *this* project; an unknown number and another project's number give the same error. The file hash is the idempotency key (same file again = 200 replay).
Rows repeated in an overlapping file are counted as duplicates, never silently merged. Imports are serialized per project.

## 5. Findings (comparison)
Deterministic, each citing observation ids, source, confidence and payload hash: claim corroborated / amount or currency mismatch / not in provider evidence / awaiting evidence / no transaction reference,
future-dated, delayed (>24 h) and out-of-order claims, conflicting and duplicate claims, provider payment missing from the ledger, provider-reported EXTERNAL payment, provider state regression,
claimed payments over the invoice, refunds over payments, claimed total differing from provider. `comparison_sha256` snapshots the result.

## 6. Limits (honest)
* A merchant ledger is only as good as the seller who uploads it; nothing here proves it. Corroboration by a provider observation is still reconciliation's judgement.
* Provider observations are what Caligo's sync saw, not a provider audit trail; a state that changed and reverted between syncs is not seen.
* Provider rows carry no provider timestamps (the port does not expose them), so delay/out-of-order checks apply to claims.
* No webhook ingestion, no live provider calls and no CSV export exist in R3. Postgres is not supported (append-only triggers are SQLite).
* Claims share the payment-operations rate limit; reads have their own, larger per-seller budget (see section 8).

## Review hardening notes

- Provider totals and matches use only the newest provider reading of each transaction; an earlier reading with a different amount is history, flagged as `PROVIDER_TRANSACTION_CHANGED`.
- A claim matching a provider record whose instrument is `EXTERNAL` (recorded outside PayPal, which a seller can create) is never "corroborated"; it gets `CLAIM_MATCHES_EXTERNAL_RECORD`.
- `asserted_at` is range-checked (2000-01-01 to now + 2 days) for CSV rows and single entries alike; notes follow one validator.

## 7. Acceptance hardening

**Integrity failures.** A duplicate transaction id means exactly one thing: the UNIQUE constraint on `payment_records.paypal_transaction_id` (`payments._is_duplicate_transaction`). Anything else
(an observation-table constraint, an FK, a CHECK) fails closed: the whole unit of work is rolled back, nothing is half-written, no `DUPLICATE_TRANSACTION_ID` note or anomaly event is created, and the
sync answers 500 `evidence_storage_failed` listing the affected payments. Observation write failures raise `ObservationStorageError` for the same path.

**Incomplete provider evidence.** A provider value that cannot be stored (amount above `MAX_AMOUNT_MINOR`, negative, malformed currency, transaction id over 100 characters) is no longer only logged:
an append-only `ProviderEvidenceGap` (table `provider_evidence_gaps`, migration 0005; reason, kind, bounded SHA-256 of the raw value, never the value) is written in the same transaction and an audit event
`PROVIDER_EVIDENCE_INCOMPLETE` is recorded (`payment_status_changed: false`). The comparison then reports `provider_evidence_complete: false`, a `PROVIDER_EVIDENCE_INCOMPLETE` finding, and replaces
"not in provider evidence" with `CLAIM_PROVIDER_EVIDENCE_INCOMPLETE`; it suppresses the claimed-vs-provider total comparison. Absence of an observation is never read as "no payment happened".
Gaps never change a status: reconciliation judges the evidence it received exactly as before (so unstorable values that happen to net out are still judged on the raw evidence; the gap is the visible warning, R4 should treat a gap as a review trigger). A payment that ever had a gap stays flagged (history is append-only); conservative by design.

**Resource limits.** Listings are paged (`limit`, `offset`, `X-Total-Count`; observations <=200, imports <=100, offset <=100000) in a stable (observed_at, id) order. The comparison is paged by payment
(<=50, default 25), compares at most the newest 2000 *claims* per payment (older ones are reported as `COMPARISON_INPUT_TRUNCATED` with `claims_truncated: true`, never silently ignored; findings that rest on the
absence of a claim are then withheld) and always loads provider observations separately (up to 5000; beyond that the provider evidence is flagged incomplete), so a seller cannot flood provider evidence out of the
comparison. At most 200 findings per payment; each finding cites at most 20 observations (the oldest 10 and newest 10; `citations_total` is the real count). A project may hold at most 20,000 stored claims (409 `observation_limit_reached`; an import that would exceed it records nothing). Reads have a separate per-seller
throttle (3x the payment-operations budget, 429 with `Retry-After`).

**Duplicate claims.** Provenance is never merged away. A retry (same file hash, or an identical seller entry) is reported as a replay. A seller entry may carry `claim_id`: the same id with the same content
is a retry, the same id with different content is 409 `claim_id_conflict`, and different ids are independent claims even with identical content (both counted when there is no transaction reference).
Identical claims from different submissions or sources are all kept and reported as one `DUPLICATE_CLAIM` finding naming the sources. A provider transaction is counted once, from its newest reading,
even when listed twice.

**Duplicate transaction ids.** `DUPLICATE_TRANSACTION_ID` is applied only when the database error is the transaction-id UNIQUE violation AND the evidence's PayPal transaction id is really recorded on a
different payment. In that case the provider's observations (and any gaps) are still stored in the same commit as the anomaly note and event, so the contradictory evidence is not lost. A benign race on the
observation dedupe key is retried once; a persistent observation failure answers 500 `evidence_storage_failed`.

## 8. Remaining limitations (exact)
* Provider observations carry no provider timestamps; there is no webhook ingestion or signature verification (the interface refuses); Postgres is unsupported (SQLite triggers).
* Findings are computed on read; `comparison_sha256` is a snapshot hash of one page, not a stored record.
* A line without a transaction reference, a `merchant_row_id` or a `claim_id` that is repeated inside one import file is counted once (reported as a duplicate); give each ledger line a row id to keep them apart.
  Rows that repeat an earlier file's rows are counted as import duplicates and not stored again; the earlier row keeps its provenance.
* A provider transaction that disappears from a later reading is not flagged; the newest reading of each transaction ever seen is what counts.
* A page loads up to 50 payments with two bounded queries each; the read throttle is a request count, not a cost weight. `claim_id` replay/conflict correctness relies on the per-project write lock (SQLite), there is no unique DB constraint on it.
* Downgrading migration 0005 deletes the gap evidence.
* Read throttling is in-process (single node), like the other throttles. `OFFSET` paging is bounded but not a snapshot: concurrent inserts can shift pages.

## 9. Final fixes
* Findings are ordered CONFLICT, WARNING, INFO and, within a severity, provider-derived before claim-derived. More than 5 findings of one claim-derived kind collapse into one that keeps the count and
  the citations (`citations_total`, oldest-id and newest-id shown), so no number of claims can hide a gap, a state regression or an external-payment warning (test: 1,500-claim flood).
* `CLAIMED_REFUNDS_EXCEED_PAYMENTS` is withheld when `claims_truncated` is true.
* A storage failure while recording the provider's answer to a *new invoice* returns 500 `evidence_storage_failed`, marks the create `CREATE_UNCERTAIN` and calls the provider nothing more; a client retry with
  the same Idempotency-Key resumes (the provider deduplicates on its request id).
* Migrations 0004 and 0005 refuse to downgrade while evidence rows exist.
