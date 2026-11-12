# S4: integrated recovery, diagnosis and health: frontend guide

One backend, three parts: **S2** guarded recovery of the simulated merchant store, **S3** advisory evidence-grounded diagnosis, **S3.5** integration-health assessment. Everything is owner-scoped under `/api/v1/projects/{project_id}/state-recovery/`, offline, and **simulated**: no PayPal call, no real funds, no credentials. Merchant-side facts are simulated; provider facts carry a provenance label (`SIMULATED_MOCK_PROVIDER` in this build).

## The seven-step flow

| # | Step | Call | What to show |
|---|---|---|---|
| 1 | Retrieve the divergence | `GET divergences` | `divergences[]` (`code`, `payment_id`, `order_ids`, `suggested_plan_kind`) and `integration_health` |
| 2 | Retrieve diagnosis + citations | `GET diagnosis?kind=&payment_id=` (before a plan) or `GET plans/{id}/diagnosis` | `diagnosis` (classification, facts vs hypotheses, supporting/contradictory ids, limitations, human steps) and `evidence[]` (the cited records) |
| 3 | Inspect the plan | `POST plans {kind, payment_id}` then `GET plans/{id}` | `status` (PROPOSED / NEEDS_REVIEW / BLOCKED / ADVISORY), `steps[]` with plain descriptions, `reasons[]`, `evidence` |
| 4 | Preview | `GET plans/{id}/preview` | `would_execute`, `blockers[]`, per-step `WOULD_APPLY / ALREADY_DONE / BLOCKED`, `guarantees` (all zero) |
| 5 | Approve (human) | `POST plans/{id}/approve {version, evidence_fingerprint}` | send back the `version` and `evidence_fingerprint` the user just reviewed |
| 6 | Execute (human) | `POST plans/{id}/execute` | `{plan, executed_now}`; `executed_now=false` means it had already run and nothing changed |
| 7 | Read the audit trail | `GET plans/{id}/events` | ordered, append-only history incl. refusals |

Diagnosis is **advisory**: it can never approve, preview, execute or change anything, and a diagnosis does not authorize a recovery. Execution needs an approval bound to the exact plan version and fingerprint, fresh provider evidence at execution time, and passes the guards in `S2_GUARDED_RECOVERY.md` (dispatched shipments are never reversed; no refund/cancel/capture exists on this path).

## Diagnosis response (`DiagnosisResponse`)

* `mode`: `RULES_ONLY` (default) or `RULES_AND_LLM`. `llm_evaluation`: `NOT_RUN` when no actual model is configured (the default in every build), `VALID`, or `REJECTED` (fabricated citation, unsupported fact, malformed answer; `llm_rejection` names the error class only; the rules diagnosis is unaffected). **Real-LLM evaluation is NOT RUN** until a model transport is configured by the operator (`app.state.llm_transport`); no SDK, key or network client is bundled.
* `diagnosis.classification`: `duplicate_payment_orders` (A), `capture_order_divergence` (B), `stale_paid_after_refund` (C), `unproven_integration_health` (D), `duplicate_side_effects`, `repeated_recovery`, `inconclusive`. `facts` copy exact stored fields; every causal statement is a `hypothesis`. `human_review_required` is always `true`.
* **Citations.** `evidence[].evidence_id` equals the S2 citation ref (`payment:<id>`, `order:<id>`, `event:<id>`, `fulfillment:<id>`, `movement:<id>`; health claims use `health:*`). Every id in `supporting_evidence_ids`, `contradictory_evidence_ids`, `facts` and `hypotheses` is in `evidence[]`; `citations_resolved` is always `true` (otherwise the call fails with 409).
* **Provenance.** `evidence[].source` is `provider` or `merchant` (simulated) or `operator` (a caller-reported claim); `provenance_label` says exactly what it is. `observed_at` is the server receipt/check time; `occurred_at` is the sender-claimed time and is never proof of order.
* **Completeness.** `observation_window_complete=false` and `incomplete_evidence_warnings[]` appear when provider evidence is incomplete; diagnosis then says so and a recovery plan is BLOCKED.
* Distinct business effects stay distinct: three deliveries of one event id are three evidence records; three stock deductions share one `effect_key` but have three `entity_id`s.

## Integration health (`GET integration-health`)

Authoritative assessment (see below). `assessment.status`: `UNKNOWN`, `UNVERIFIED`, `DEGRADED`, `TRANSACTION_VERIFIED`. Configuration alone is at most `UNVERIFIED`; a provider-paid payment the merchant never applied is `DEGRADED`; `TRANSACTION_VERIFIED` needs a fresh (24 h default) provider-PAID payment applied by the safe processor. `production_certification` is always `false`. Query params `credentials_configured` / `heartbeat_ok` are caller-reported simulation inputs, not observations: the response lists the capabilities that rest on them in `caller_reported_capabilities` (`configuration_observed`, `provider_connectivity_observed`); show them as "reported", not "observed". Any open provider/merchant divergence (duplicate orders, refund vs paid order, provider-paid but unapplied) is a current failure and keeps the status `DEGRADED` even if an older transaction was proven. Show `assessment.missing_checks`, `stale_evidence_warnings` and `suggested_next_diagnostic_action`; do not render a green "healthy" badge for anything but `TRANSACTION_VERIFIED`.

## Which health implementation is authoritative, and why

**`app.integration_health` (S3.5) is the single authoritative policy.** It has what S1's version lacked: explicit project/integration/environment scope, freshness by check time (stale evidence cannot be healthy), provenance rules per check (only simulation runners count in `simulation`; operator claims and untrusted logs never promote status), citation validation, equal-time conflict handling and a fixed non-certification statement. `app.simulation.health` is now only a **compatibility adapter**: it builds a `HealthInput` from stored state and maps the result to the older `HealthAssessment` tuple (`configuration`, `heartbeat`, `end_to_end`, `overall`) so S1/S2 callers and tests did not change. It contains no decision logic of its own. Webhook evidence is not required in this simulation (`webhook_required=false`); no webhook source exists yet.

## Errors

| Status / code | Meaning |
|---|---|
| 404 `not_found` | unknown, or another seller's project/plan/payment |
| 422 `invalid_request` / validation | bad `kind`, missing `payment_id` for a payment kind, bad id format |
| 409 `no_divergence` | nothing of that kind to recover or diagnose right now |
| 409 `unresolved_evidence` | a cited record cannot be resolved inside this project and payment |
| 409 `approval_required`, `approval_mismatch`, `plan_not_approvable`, `plan_superseded`, `plan_tampered`, `provider_evidence_changed`, `business_state_changed`, `dispatch_evidence`, `MULTIPLE_DISPATCHED_ORDERS`, `processor_declined`, `concurrent_modification` | a recovery guard refused (recorded in the plan's events); preview again, or create the plan again for a new version |
| 429 / 403 | throttled / missing CSRF on a write |

## Demo fixtures

`backend/.venv/Scripts/python scripts/demo_recovery_integrated.py --out demo_recovery_output` runs scenarios A (one payment, three orders), B (provider paid, merchant failed), C (refund then stale paid) and D (false healthy) through the API and writes every response to `demo_recovery_output/api/` (gitignored); `checks.json` lists the 40 checks. In tests, `tests/test_state_simulation.py::sim` and `tests/test_s4_integration.py` build the same fixtures. Contract schemas: `contracts/DiagnosisResponse.schema.json`, `contracts/IntegrationHealthResponse.schema.json` (plus the S3/S3.5 module schemas in `contracts/diagnosis/` and `contracts/integration-health/`).

## Changes to the Codex modules in S4

Only two additive rule extensions in `app/diagnosis/rules.py`, so S2's real shapes are recognised: (1) a merchant order still `pending` after a provider capture counts like `failed` for `capture_order_divergence`; (2) provider evidence `refunded` while a merchant order is `paid`/`fulfilled` yields `stale_paid_after_refund`. The committed benchmark (`diagnosis_benchmark/`) gives identical per-case predictions and accuracy. Everything else in S3/S3.5 is unchanged.

## Remaining limitations

Simulation only (SQLite; Postgres needs the guards ported, see `S2_GUARDED_RECOVERY.md`). No real LLM has been evaluated. Rules diagnose recognised patterns only (no order for a paid payment stays `inconclusive`; the S2 divergence list still reports it). Diagnosis ids are plan/payment-scoped; there is no project-wide diagnosis list. No webhook evidence source exists. A configured LLM transport must enforce its own timeout (the adapter calls it synchronously) and its classification is only checked for citation validity, so the deterministic `diagnosis` stays primary; treat `llm_diagnosis` as an unverified second opinion. Health inputs `credentials_configured` / `heartbeat_ok` are caller-reported.
