# R4 recovery cases (advisory)

## 1. What this is
R4 turns records Caligo already holds into **understandable, evidence-cited recovery cases**: where provider observations, unverified merchant claims, reconciliation anomalies and instruction holds
disagree, what that means, what a *person* could check next, and which actions Caligo will never take. It adds **no tables and no migration**: cases are derived on every read from immutable records
(`payment_observations`, `provider_evidence_gaps`, `payment_records`, `payment_events`, `payment_instructions`, `instruction_change_requests`) and stored nowhere. Nothing a client sends can create,
change or resolve a case; the two routes are `GET` only.

## 2. Authority and what a case can never do
* **Advisory only.** `advisory_only` is always `true`; every case lists `prohibited_automated_actions` (refunds, capture/transfer, cancelling/voiding/sending/modifying invoices, marking anything paid
  because of a claim, releasing/bypassing a hold, editing evidence). The service imports no provider port, no writer and no reconciliation state machine; a test checks that reads change no stored row.
* **Claims stay claims.** A seller/merchant claim is only ever cited with `confidence: UNVERIFIED_CLAIM`. A claim that matches the provider produces *no* case (corroboration is good news, not a finding)
  and never changes `authoritative_status`, a payment, or a hold. Reconciliation (`reconcile.evaluate/apply`) remains the only authority on payment state.
* **R1/R2 untouched.** Hold cases read `instructions.compute_hold`; there is no route to release, approve or withdraw anything here. Only buyer-side verification (R2) releases a hold; the case text says so.
* **Incomplete evidence never means "missing".** If a payment has evidence gaps or truncated inputs, absence-dependent categories (`CLAIM_NOT_CORROBORATED`, `LEDGER_MISSING_PROVIDER_PAYMENT`) are
  suppressed and an `EVIDENCE_INCOMPLETE` case is produced instead, whose confidence is `INCOMPLETE_EVIDENCE` and whose text says Caligo "cannot tell whether a payment is missing". Every other case on
  that payment is also marked `INCOMPLETE_EVIDENCE`. Claims before any provider evidence exist are `AWAITING_PROVIDER_SYNC`, not "missing".

## 3. Categories
| Category | Rests on | Typical source |
|---|---|---|
| `LEDGER_MISSING_PROVIDER_PAYMENT` | provider observation | R3 `PROVIDER_PAYMENT_NOT_IN_LEDGER` |
| `CLAIM_NOT_CORROBORATED` | claim vs complete provider evidence | `CLAIM_NOT_IN_PROVIDER_EVIDENCE`, claim without a transaction reference |
| `AWAITING_PROVIDER_SYNC` | claims only | `CLAIM_AWAITING_PROVIDER_EVIDENCE` |
| `AMOUNT_OR_CURRENCY_MISMATCH` | claim vs provider / provider vs invoice | `CLAIM_AMOUNT_MISMATCH`, `CLAIM_CURRENCY_MISMATCH`, claimed totals, anomalies `OVERPAYMENT`… |
| `DUPLICATE_REFERENCE` | claims / reconciliation | `DUPLICATE_CLAIM`, `CONFLICTING_CLAIMS`, anomaly `DUPLICATE_TRANSACTION_ID`, `TRANSACTION_ID_CHANGED_REVIEW` |
| `PROVIDER_STATE_REGRESSION` | provider observations | `PROVIDER_STATE_REGRESSION`, `PROVIDER_TRANSACTION_CHANGED` |
| `EVIDENCE_INCOMPLETE` | evidence gaps / truncation | `PROVIDER_EVIDENCE_INCOMPLETE`, `COMPARISON_INPUT_TRUNCATED` |
| `EXTERNAL_PAYMENT` | provider observation | `PROVIDER_REPORTS_EXTERNAL_PAYMENT`, `CLAIM_MATCHES_EXTERNAL_RECORD`, `EXTERNALLY_MARKED_*` |
| `CLAIM_TIMING_ANOMALY` | claims only | future-dated / delayed / out-of-order claims |
| `RECONCILIATION_ANOMALY` | reconciliation record | every code in `recovery_catalog.ANOMALIES` |
| `INSTRUCTION_INTEGRITY`, `INSTRUCTION_HOLD`, `PENDING_BUYER_AUTHORIZATION` | R1/R2 instruction records | `compute_hold` reasons, `INSTRUCTION_TAMPERED` |

Reconciliation note codes come from `reconcile.py` and `payments.py`; `tests/test_recovery_cases.py::test_every_reconciliation_note_code_in_the_source_has_a_recovery_mapping` extracts them from the source and
fails on an unmapped one. A stored note Caligo does not recognize is shown as `UNRECOGNIZED_CODE` and **never echoed**.

## 4. API (read-only, owner-scoped, read-throttled)
* `GET /api/v1/projects/{id}/recovery-cases?limit=25&offset=0&category=&severity=` → `RecoveryCaseList`. Pages by *payment* (limit 1-50); project-level instruction cases ride on the first page (`offset=0`).
  Ordered CONFLICT, WARNING, INFO, then category, payment, case id. `cases_sha256` snapshots the page.
* `GET /api/v1/projects/{id}/recovery-cases/{case_id}` → `RecoveryCase` (404 for an unknown id, one that no longer applies, or another seller's/project's id: one indistinguishable answer; malformed ids are 422).

`case_id` (`rc_` + 24 hex) is stable for (project, payment, category[, hold reason]); `basis_sha256` changes when the evidence behind the case changes, so a UI can show "updated" without the id moving.

### Example (abridged)
```json
{
  "case_id": "rc_3f0c1d…", "project_id": "…", "payment_id": "…",
  "category": "CLAIM_NOT_CORROBORATED", "severity": "WARNING",
  "confidence": "CLAIM_VS_PROVIDER",
  "confidence_limits": ["The claim is unverified.", "Provider evidence is a snapshot of what Caligo's last sync saw."],
  "current_state": {"authoritative_status": "PAID", "provider_paid_minor": 80000, "claimed_paid_minor": 80000,
                    "evidence_complete": true, "evidence_gap_count": 0, "claims_truncated": false, "hold_active": false},
  "explanation": "A merchant or seller claim cannot be matched to the provider's stored evidence … Details: The provider's evidence for this invoice does not contain this transaction.",
  "recommended_next_action": "Check the transaction reference against your PayPal dashboard, then correct the claim or sync again later. Do not treat the claim as a received payment.",
  "citations": [{"kind": "OBSERVATION", "id": "…", "source": "SELLER_ENTRY", "confidence": "UNVERIFIED_CLAIM", "payload_sha256": "…"},
                {"kind": "OBSERVATION", "id": "…", "source": "PROVIDER_API", "confidence": "PROVIDER_EVIDENCE", "payload_sha256": "…"}],
  "citations_total": 2, "citations_verified": true,
  "derived_from": ["CLAIM_NOT_IN_PROVIDER_EVIDENCE"], "evidence_complete": true,
  "advisory_only": true, "prohibited_automated_actions": ["Issue, approve or request a refund", "…"],
  "basis_sha256": "…"
}
```
Citation kinds: `OBSERVATION`, `EVIDENCE_GAP`, `PAYMENT_RECORD`, `PAYMENT_EVENT`, `INSTRUCTION_VERSION`, `INSTRUCTION_CHANGE`. Every shown citation is re-resolved against the same project on every read
(`citations_verified`); an unresolved one flips the flag to `false` and adds a confidence limit. At most 20 citations are shown per case (oldest-id and newest-id halves); `citations_total` is the real count.

## 5. Determinism and AI
Wording is fixed text in `app/services/recovery_catalog.py`; only counts, ids and the first three R3 finding summaries vary. The same stored records always give the same cases, ids and hashes. A later AI layer
may rephrase `explanation` / `recommended_next_action` but must keep `citations`, `derived_from`, `category`, `severity`, `confidence` and the prohibited-action list unchanged and may not add evidence.

## 6. Limits (exact)
* Cases are only as complete as the R3 comparison: provider observations carry no provider timestamps, a transaction that disappears from a later provider reading is **not** detected (no snapshot completeness
  signal yet), and there is no webhook ingestion. Gaps are permanent in the data; a payment that ever had a gap stays `INCOMPLETE_EVIDENCE`.
* Cases are computed per request (page of up to 50 payments, each bounded by the R3 limits). The single-case lookup matches the id against derived hashes for at most 5,000 payments, then computes only the
  matching payment (or the holds); an unknown id reads no evidence. Payments beyond the 5,000th are not reachable by id.
* Absence-flavoured reconciliation notes (`PAID_STATE_WITHOUT_MATCHING_PAYPAL_PAYMENTS`, `CANCELLED_WITH_PAYMENTS`, `PAYMENT_ACTIVITY_IN_UNPAID_STATE`, `AMOUNT_DECREASED`) are not shown as cases while a payment's
  evidence is incomplete. `RECONCILIATION_ANOMALY` cases have confidence `RECONCILIATION_RECORD`: they reflect what Caligo's sync last recorded, not an audit of the provider.
* With no payment provider configured, hold evaluation is partial: `hold_active` is `null` (not `false`) on payment cases and a confidence limit says so.
* `citations_verified` means every shown citation exists in this project; it does not assert that the record belongs to the same payment.
* `basis_sha256` / `cases_sha256` include the explanation text, so a wording change in a later release moves them without any evidence change.
* There is no case state (open/acknowledged/resolved): acknowledging or tracking a case needs a mutable, audited record and is an R5 decision.
* Recommended steps are generic text for a human; they do not know the seller's PayPal account and are not legal, tax or accounting advice.
* Read throttling is in-process; Postgres remains unsupported.
