# State divergence and recovery: architecture (S0) and simulated merchant state (S1)

Status: **S0 audit + S1 simulation implemented**. S2 (guarded recovery), S3 and S4 are **not** started.
Baseline: the R0 to R6 backend.
This is a hackathon MVP over a **simulation**. It is not a production payment-recovery service, makes no provider call and performs no real financial action.

## 1. What already exists (S0 audit) and how S1 reuses it

| Existing piece | What it gives S1 | How S1 uses it |
|---|---|---|
| `PaymentRecord` (+ `PaymentEvent`) | The ONLY provider-side truth: `provider_status`, `amount_minor`, `amount_paid_minor`, `currency`, `paypal_transaction_id`, optimistic `version`, one active invoice per project, append-only history | **Read-only.** A simulated order points at a `PaymentRecord` of the same project; amount/currency are copied from it. The reconciliation state machine (`services/reconcile.py`, `payments.py`) is **not duplicated or modified** |
| `PaymentObservation` (R3, source/confidence enforced by CHECKs), `ProviderEvidenceGap` | Provider evidence vs merchant claims, and the marker that evidence is *incomplete* | A `ProviderEvidenceGap` for a payment makes the safe processor **hold** instead of acting. Merchant/webhook-style input stays a claim |
| `MerchantLedgerImport` (R3) | Existing path for a seller-uploaded merchant ledger (claims) | Not reused: the simulation is a different thing (merchant *business* state: orders/stock/shipments), kept in its own tables |
| Recovery cases (R4, `services/recovery.py`) | **Derived, not stored**: cases are computed from payments/observations/hold with deterministic ids (`_case_id`), bounded, with `prohibited_automated_actions` | S2 should add a new category fed by `detect_divergences`; S1 does not touch R4 |
| Unified report (R5) | Deterministic JSON/Markdown with a canonical hash and citation validation | S3 candidate: add a "merchant state" section citing `sim_*` ids. Untouched in S1 |
| HOLD / instruction integrity (R1-R2) | Blocks operations through Caligo only | Unchanged. Nothing here can release or create a hold |
| Conventions | `String(36)` UUID ids, `UTCDateTime`, `monotonic_utcnow`, `enum_check`, `make_append_only` (ORM guard + SQLite triggers), integer minor units + 3-letter upper-case currency CHECK, alembic migrations with frozen trigger SQL, refuse-to-downgrade when evidence exists | All followed (migration `0006`) |
| `tests/fake_paypal.py` / MOCK provider | Offline provider (`provider = MOCK_PAYPAL`) | Provider fixtures are therefore explicitly simulated; `paid`/`unpaid` fixtures supply PaymentRecords |

Identifiers: project id (ownership root), payment id (`PaymentRecord.id`), provider transaction id (`paypal_transaction_id`, unique), invoice number. Ownership is always `project_id`; sellers only ever reach their own projects (404 otherwise).

## 2. Proposed (and implemented) model

All tables are project-scoped (`project_id` FK) and defined in `app/sim_models.py`; enums in `app/sim_enums.py` (not part of the public API contract, so `contracts/` is unchanged).

| Table | Mutable? | Purpose / key constraints |
|---|---|---|
| `sim_inventory_items` | append-only | `(project, sku)` unique, `initial_stock >= 0`. Balance is **derived**: `initial - RESERVE - DEDUCT + RELEASE` |
| `sim_inventory_movements` | append-only | kind `RESERVE/DEDUCT/RELEASE`, `quantity >= 1`, **`(project, operation_key)` unique** = business operation identity |
| `sim_orders` | state + version only | one `payment_id` (same project), `business_key` unique per project (default: one order per payment), integer `amount_minor`, `currency`, `state`, optimistic `version`. Identity columns immutable by trigger |
| `sim_fulfillments` | state + version only | `(project, operation_key)` unique; `(state='DISPATCHED') = (dispatched_at NOT NULL)`; **DISPATCHED is final** (trigger) |
| `sim_incoming_events` | append-only | one row per *delivery*: sender `event_key` (a claim), `provider_txn_ref`, type, `asserted_at` (sender-claimed) **and** `received_at` (server), `payload_sha256`, `replay_status` (`FIRST / REPLAY_IDENTICAL / REPLAY_DIFFERENT_PAYLOAD`), `replay_of_id`; `source` must start with `SIMULATED_` |
| `sim_audit_records` | append-only | every effect and every processing attempt (`EVENT_PROCESSED` + outcome), duplicates, refusals (`DISPATCH_PROTECTED`), and which processor acted (`SAFE` vs `FAULTY_FIXTURE`) |

Database-level integrity (SQLite triggers + CHECKs, frozen in the migration, verified equal to a fresh database by a test): append-only history, order/fulfillment identity immutability, dispatch finality, **same-project check on every cross-table reference**, source marker, enum CHECKs, order/fulfillment/movement uniqueness.

A processing outcome is *not* stored on the event (receipts are immutable); it lives in `sim_audit_records`. A crash writes nothing: its signature is an event with no `EVENT_PROCESSED` record (`store.unprocessed_events`).

### Code layout (`backend/app/simulation/`)
* `store.py` primitives: orders, movements, fulfillment, event receipt, dispatch evidence, derived balance. Flush only; callers own the transaction.
* `processor.py` the **safe** processor: `process_event` (commits) and `preview_event` (savepoint, never persists). Failure-injection hook for crash tests.
* `divergence.py` read-only `detect_divergences(db, project_id)`; `health.py` configuration vs proven end-to-end.
* `faulty.py` + `scenarios.py` deliberately wrong behavior and scenarios A-D. **Nothing in routers/main/services imports them** (test-enforced) and there is **no HTTP route** for the simulation.

## 3. Trust boundaries

1. **Provider evidence** (`PaymentRecord`/observations, written only by reconciliation) is the only financial truth. The simulation reads it and never writes it.
2. **Incoming events are claims.** They are stored as received, labelled simulated, never trusted for money movement without matching provider evidence (status, amount, currency, transaction, no evidence gap).
3. **Merchant state is not authoritative.** A merchant-says-paid order with no provider payment is reported as a divergence, not accepted.
4. **Arrival order and sender timestamps are not truth.** Staleness is decided by the transition table plus provider state; a paid event asserted newer *or* older than a refund cannot revive a refunded order.
5. **Irreversible facts stay facts.** A dispatched parcel is never marked undone; a refund after dispatch records `DISPATCH_PROTECTED` and does not restore stock.
6. **Faulty behavior is quarantined** to explicit fixtures and always marked `FAULTY_FIXTURE`.
7. **No cross-project references**: service checks plus database triggers.

## 4. Safe processing rules (what "safe" means here)
One order, one deduction and one fulfillment **per payment** (keys derive from the payment/order, never the delivery). Order states move only along a forward table; `REFUNDED`/`CANCELLED` are terminal; `FAILED -> PAID` is allowed only with provider evidence of payment. Outcomes: `APPLIED`, `DUPLICATE_NO_EFFECT`, `HELD_AWAITING_PROVIDER_EVIDENCE`, and `REJECTED_{STALE, AMOUNT_MISMATCH, CURRENCY_MISMATCH, UNKNOWN_PAYMENT, INCOMPLETE_EVIDENCE, CONFLICTING_REPLAY, CONTRADICTED_BY_PROVIDER, NO_MERCHANT_ORDER}`. Partial refunds are out of scope (amount must equal the paid amount).

## 5. Scenario outcomes (asserted by tests)
| Scenario | Result |
|---|---|
| **A** one payment, three orders (faulty non-idempotent handler, 3 deliveries) | 3 `REPLAY` receipts kept; 3 orders, 3 deductions (stock `-3` instead of `-1`), 3 fulfillments, **2 already dispatched** and un-cancellable; divergences `MULTIPLE_ORDERS_FOR_ONE_PAYMENT`, `DUPLICATE_INVENTORY_DEDUCTION`, `DUPLICATE_FULFILLMENT`. The safe processor on the same deliveries: 1 order, 2 `DUPLICATE_NO_EFFECT` |
| **B** payment success, merchant failed (crash before persist) | provider `PAID`; order stays `PENDING`; nothing partially persisted; event listed as unprocessed; divergence `PROVIDER_PAID_MERCHANT_NOT_PAID` |
| **C** refund then stale paid event | safe verdict (previewed first): `REJECTED_STALE`; the faulty handler regresses the order `REFUNDED -> PAID` and deducts stock again while the provider says `REFUNDED`; divergence `PROVIDER_REFUNDED_MERCHANT_STILL_PAID` |
| **D** false healthy | naive report `HEALTHY` from configuration + heartbeat; honest assessment `UNVERIFIED` (nothing proven) or `DEGRADED` (provider paid, merchant not), `HEALTHY` only after a safely processed end-to-end payment |

## 6. Risks and limits
* Simulation only: real merchant systems have unknown semantics; the processor's rules are assumptions.
* `PaymentRecord.paypal_transaction_id` is the join key; a payment without one cannot be matched (events are rejected/held, which is the safe direction).
* Sender timestamps are stored but deliberately not used for decisions; if a future processor uses them it must say why and never alone.
* Concurrency: unique operation keys and optimistic versions protect the DB; the processor handles a lost race as a duplicate. No cross-process load testing was done.
* The migration refuses to downgrade while simulated records exist (by design).
* Divergence detection is a read-only finding list; it does not rank, route or close anything.

## 7. S2 (guarded recovery): implemented, see `S2_GUARDED_RECOVERY.md`. Original requirements:
1. A recovery **plan** per divergence (propose, never auto-apply): e.g. for B, replay the unprocessed event through `process_event` (idempotent) after re-checking provider evidence; for A, identify which duplicate orders have dispatched evidence and which can be cancelled/restocked, keeping the "canonical" order explicit.
2. **Guards**: dry-run first (`preview_event` pattern), per-step precondition re-check against fresh provider evidence, optimistic-version/compare-and-set on every state change, refusal when dispatch evidence exists, human confirmation with an audited actor, idempotent execution keyed by plan id.
3. Append-only **recovery records** (plan, approval, each step, result) linked to `sim_audit_records` and R4 cases; no deletes, no history rewrite.
4. Explicit non-goals to keep: no refund/cancel call to a provider, no silent state rewrite, no claim that a dispatch or refund was reversed without evidence.
5. A read-only API/report surface (S3) fed from `detect_divergences` and the audit trail, with citations, following the R4/R5 patterns.


## 8. S4 integration

S3 diagnosis (`app/diagnosis`) and S3.5 health (`app/integration_health`) are merged. The authoritative health policy is `app.integration_health`; `app/simulation/health.py` is a compatibility adapter. `app/simulation/diagnosis_adapter.py` turns the S2 evidence envelope into a scoped, cited `DiagnosisInput`. See `S4_INTEGRATION_GUIDE.md`.
