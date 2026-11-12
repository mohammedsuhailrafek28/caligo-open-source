# R5 unified evidence report

## 1. What this is
One deterministic, reviewer-ready report over Caligo's three capabilities: **scope and delivery** (Stage 3), **payment-instruction integrity and buyer-side authorization** (R1/R2) and **payment observations, divergences and recovery cases** (R3/R4).
It is a thin composition layer: no tables, no migration, no clock, no randomness, no provider call, no write. It reuses `report.build_report` (Stage 3E), `instructions`, `authenticators`, `observation_compare` and `recovery`.
It decides nothing: payment status stays whatever provider reconciliation decided, and conflicts, gaps and unverified claims are listed beside it, never resolved.

## 2. Evidence classes (every statement carries one)
| Class | Meaning | Never means |
|---|---|---|
| `PROVIDER_OBSERVATION` | what the provider returned to Caligo's last sync, stored immutably | an audit of the provider, or a complete picture when gaps exist |
| `SERVER_RECORDED` | a fact the server recorded itself (hashes, server time, hash-chained instruction versions, mailbox-verified buyer actions) | legal identity |
| `SELLER_CLAIM` | asserted by the seller or a seller-supplied file (merchant ledger, seller entries, artifact links, a change's stated reason, the first instruction version) | proof of a payment or approval |
| `OPERATOR_ESTABLISHED_VERIFICATION` | an authenticator an operator created answered a challenge | **real-world identity of any buyer**, or independence from the seller |
| `AI_INTERPRETATION` | an analysis provider's labelled opinion | fact; it always needs human review |
| `DERIVED_ADVISORY` | deterministic R4 recovery advice | an action: nothing is executed |

Authorization records always carry `real_world_identity_verified: false`, `independent_of_seller: "NOT_ESTABLISHED"`, the label `VERIFIED_BY_ENROLLED_SECOND_CHANNEL` and the limitation text. No field anywhere says a buyer's identity was verified.

## 3. API (GET only, owner-scoped, read-throttled)
`GET /api/v1/projects/{id}/unified-report?payments_limit=25&payments_offset=0` → JSON. `GET .../unified-report/markdown` → `text/markdown`, same parameters.
`payments_limit` 1-50, `payments_offset` 0-100000 (422 otherwise). The payment sections cover that page of payments; scope, instruction and authorization sections cover the whole project up to the caps below. Unauthenticated: 401/403; another seller's project: 404.

### Contents (JSON)
| Key | Contents |
|---|---|
| `report_version`, `kind`, `project` | `R5.1`, `UNIFIED_EVIDENCE_REPORT`, id and title |
| `evidence_classes` | the legend above |
| `summary` | payment/case counts, payments with incomplete provider evidence, delivery status, unresolved scope items, hash-chain status, hold, authorization record counts and inconsistencies, `citations_all_resolved` |
| `scope_and_delivery` | `available`; when a baseline exists: the Stage 3 sections `agreement`, `changes`, `requirement_mapping`, `buyer_evidence`, `seller_claims`, `ai_judgments`, `unresolved` plus `evidence_report_sha256` (the hash of `GET .../evidence-report`). `available: false` with a reason when no agreement is frozen |
| `payment_instructions` | `hash_chain_intact`, `versions` (fingerprint, `fingerprint_recomputes`, chain hash, provenance, verification status, evidence class), `changes` (state `PENDING_BUYER_SIDE_VERIFICATION` / `WITHDRAWN_HOLD_NOT_RELEASED` / `APPLIED` / `REJECTED`), `authorizations` (trust labels, `record_consistent`, `record_problems`), `authenticators` (masked, never secrets), `hold` |
| `payments` | `page`, per payment: reconciliation status (`authoritative_status_source: PROVIDER_RECONCILIATION`), `provider_observations`, `seller_claims`, `claim_vs_provider` (`NO_CLAIMS`, `CLAIMED_TOTAL_MATCHES_PROVIDER`, `CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER`, `NOT_COMPARABLE_EVIDENCE_INCOMPLETE`), `observation_counts_by_source`, R3 `findings`; `evidence_gaps`; `recovery_cases` (R4 cases verbatim, with citations, recommended human action and prohibited automated actions) |
| `citation_check` | `distinct_checked`, `all_resolved`, `problems` (kind, id, reason), `problems_truncated` |
| `truncation` | payments page, gaps, instruction versions, changes and authenticators truncation flags |
| `limitations`, `report_sha256` | fixed limitations; SHA-256 of the canonical JSON of everything else |

### Example (abridged)
```json
{
  "report_version": "R5.1", "kind": "UNIFIED_EVIDENCE_REPORT",
  "summary": {"payments_with_incomplete_provider_evidence": ["…"], "payment_hold_active": false, "citations_all_resolved": true, "…": "…"},
  "payment_instructions": {"hash_chain_intact": true,
    "authorizations": [{"outcome": "APPLIED", "label": "VERIFIED_BY_ENROLLED_SECOND_CHANNEL", "evidence_class": "OPERATOR_ESTABLISHED_VERIFICATION",
                        "real_world_identity_verified": false, "independent_of_seller": "NOT_ESTABLISHED", "record_consistent": true}]},
  "payments": {"payments": [{"authoritative_status": "PAID", "authoritative_status_source": "PROVIDER_RECONCILIATION",
                             "provider_observations": {"evidence_class": "PROVIDER_OBSERVATION", "paid_minor": 80000, "evidence_complete": false},
                             "seller_claims": {"evidence_class": "SELLER_CLAIM", "claimed_paid_minor": 70000},
                             "claim_vs_provider": "NOT_COMPARABLE_EVIDENCE_INCOMPLETE"}]},
  "citation_check": {"all_resolved": true, "problems": []}, "report_sha256": "…"
}
```

## 4. Determinism and the hash
The report is a pure function of stored records. `report_sha256` is the canonical SHA-256 (sorted keys, compact separators) of the body without that key, so key order cannot move it and a copy can be re-checked. New evidence moves it.
The Markdown prints the same hash; every stored string is escaped (no raw HTML, no table breakouts). The scope section's own `evidence_report_sha256` ties it to the Stage 3 report.

## 5. Citations
Every citation is checked against the database: the record must exist **in this project**, and where the citing record is about a payment, an observation, evidence gap or payment event must belong to **that payment** (a record of another payment in the same project is `PAYMENT_MISMATCH`). Scope/delivery references (`kind:id`) are checked by kind; malformed or unknown kinds are reported as problems. Authorization records are additionally cross-checked against their change (fingerprints, authenticator in project, trust basis, applied version's fingerprint and predecessor).
Problems are reported (`citation_check`, `summary.citations_all_resolved`, a warning in the Markdown), never hidden or auto-corrected, and ids only are shown, never stored free text.

## 6. Bounds
Payments: one page (≤50). Evidence gaps ≤500 shown, instruction versions ≤200, changes ≤200, authenticators ≤100, scope references ≤5000 validated; every cut is flagged in `truncation`. Recovery cases keep R4's bounds (≤20 citations per case, with the real total).

## 7. Limits (exact)
* Inherits the R3/R4 limits: no provider timestamps, a transaction that vanishes from a later provider reading is not detected, no webhook ingestion, gaps are permanent, recommendation text is generic.
* The scope and delivery sections are the Stage 3 report and are bounded only by its own limits (it loads a project's scope records in full).
* Report generation computes R3/R4 for one page of payments, so a project's full payment picture needs paging through `payments_offset`; the hash covers the requested page only.
* The report is not legal advice, decides no dispute, and an operator-established authenticator is not proof of a person's identity.
* No PDF export; read throttling is in-process; Postgres remains unsupported.
