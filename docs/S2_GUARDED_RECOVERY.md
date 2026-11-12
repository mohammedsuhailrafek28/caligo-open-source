# S2: guarded, human-approved recovery of the SIMULATED merchant store

Scope: the isolated simulated merchant store from S1 (`sim_*` tables). **No provider is contacted and no financial action exists on this path**: no capture, refund, cancellation, transfer, invoice change or Sandbox access. Merchant state is never treated as provider truth; provider evidence (`PaymentRecord`, already verified by the existing flow) is only read.

## Flow

`detect divergence` -> `create plan` (evidence-backed, deterministic) -> `preview` (dry run, fresh evidence) -> `approve` (owner, bound to exactly one version + fingerprint) -> `execute` (at most once) -> `events` (append-only audit).

| Step | Endpoint (under `/api/v1/projects/{project_id}/state-recovery/`) | Notes |
|---|---|---|
| Divergences | `GET divergences?credentials_configured=&heartbeat_ok=` | `DivergenceList`, with the S1 end-to-end health assessment. |
| Create | `POST plans` `{kind, payment_id?}` | 201 new version; 200 identical evidence (idempotent). `kind` = `DUPLICATE_ORDERS` (A), `PAID_NOT_APPLIED` (B), `STALE_PAID_AFTER_REFUND` (C), `FALSE_HEALTHY` (D, no payment). |
| List / read | `GET plans`, `GET plans/{id}` | `RecoveryPlanView`: steps, evidence envelope, reasons, fingerprints, derived `state`, approval, execution. |
| Preview | `GET plans/{id}/preview` | Writes nothing. Re-derives from fresh evidence; lists blockers and per-step `WOULD_APPLY / ALREADY_DONE / BLOCKED`. |
| Approve | `POST plans/{id}/approve` `{version, evidence_fingerprint}` | Repeating the same approval is a no-op; any mismatch is refused (409 `approval_mismatch`) and recorded. |
| Execute | `POST plans/{id}/execute` | 200 `{plan, executed_now}`. A repeat returns `executed_now=false` and changes nothing. A refusal is recorded and returns 409 with a stable code. |
| History | `GET plans/{id}/events` | PLAN_CREATED, APPROVED, STEP_APPLIED / STEP_ALREADY_DONE, EXECUTED, APPROVAL_REFUSED, EXECUTION_REFUSED. |

All routes are owner-scoped (another seller's project or plan is 404), writes need the CSRF token and are rate-limited. Failure and retry: a 409 on execute names the cause (`provider_evidence_changed`, `business_state_changed`, `approval_required`, `plan_superseded`, `plan_tampered`, `dispatch_evidence`, `MULTIPLE_DISPATCHED_ORDERS`, `concurrent_modification`, `processor_declined`). After fixing the cause, preview again; if the evidence changed, create the plan again (it becomes a new version and must be approved again).

## Records (append-only, DB triggers + ORM guards)

* `sim_recovery_plans`: one immutable row per plan **version**. `plan_key` = hash(project, kind, payment); `evidence_fingerprint` covers kind, payment, evidence and steps; `provider_fingerprint` covers provider facts only (re-verified at execution). Unique per (key, version) and (key, fingerprint).
* `sim_recovery_events`: the history. A plan's state is **derived** from these rows (never rewritten). Partial unique indexes allow at most one APPROVED and one EXECUTED per plan; APPROVED must carry version + fingerprint.

## Guarantees

* **Approval binding.** The approval stores approver, server timestamp, plan version and fingerprint. A newer version supersedes the older one; an old approval never authorizes a changed plan. A stored plan whose content no longer matches its fingerprint is refused (`plan_tampered`).
* **Fresh evidence at execution.** The plan is rebuilt from current provider and merchant state; any difference in provider facts, merchant facts, safety preconditions or canonical-order result refuses execution.
* **Atomic, at most once.** A and C apply all steps and their audit rows in one transaction (SQLite write lock taken first; optimistic `version` on orders; unique operation keys on stock movements). B replays the event through the existing idempotent `process_event`. EXECUTED is unique per plan; concurrent executors end with one winner and a recorded result for the rest.
* **Dispatch finality.** A shipped parcel is never reversed or cancelled, its stock is never returned, an order with a shipped parcel is never cancelled. Enforced by the service and by the DB trigger.
* **Fail closed.** Missing/incomplete evidence, wrong project/payment, amount or currency mismatch, missing approval, stale plan, uncertain canonical order (NEEDS_REVIEW: two or more shipped duplicates, or no provable canonical order), concurrent modification: refused, nothing applied.
* **No fund claims.** C aligns the *merchant order* with provider refund evidence; it never issues or reverses a refund, never says funds were restored, and flags `GOODS_ALREADY_SHIPPED` for manual follow-up.
* The recovery modules never import the faulty fixtures or any provider adapter (asserted by a test).

## Scenario results

| | Plan | Executed effect |
|---|---|---|
| A one payment, 3 orders | canonical = the one dispatched order (or the order created by the FIRST event); duplicates' pending fulfillments cancelled, net stock released once, duplicate orders CANCELLED | 1 live order, stock = initial - 1, dispatch untouched; with 2+ shipped parcels: NEEDS_REVIEW, not executable |
| B provider PAID, merchant failed | replay stored paid event (or one event synthesized from provider evidence, only when an order proves what was bought) | one APPLIED, one order, one deduction; repeat = no effect; no event and no order = BLOCKED |
| C refunded, merchant PAID | order -> REFUNDED, pending fulfillment cancelled, undispatched stock returned once | shipped goods stay shipped and stock is not returned |
| D false healthy | ADVISORY only: run an end-to-end verification | nothing mutated, nothing executable |

## Evidence output contract for S3/S4

Each plan carries `evidence` = `{schema: "caligo.recovery_evidence.v1", citations[] (stable `ref` such as `order:<id>`, `event:<id>`, `audit:<id>`, `payment:<id>`), provenance per item (SIMULATED_* vs provider evidence), completeness {complete, missing[]}}`, plus `reasons[]` (BLOCKER / REVIEW / FOLLOW_UP / INFO with stable codes), `evidence_fingerprint`, `provider_fingerprint` and timestamps. IDs and fingerprints are stable for identical evidence, so an S4 adapter can map them into S3 diagnosis input without re-deriving anything. S4 chooses the authoritative health module; S2 only reuses `assess_integration_health` for D.

## Limits

Simulation only. SQLite write locking is the concurrency guard here; a Postgres port needs row locks (`SELECT ... FOR UPDATE`) in the same place. Compare-and-set relies on the orders' `version` column and unique operation keys. The crash hook `fail_at` is for tests/demos and is not reachable from the API.
