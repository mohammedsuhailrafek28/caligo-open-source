# S3.5 Payment Integration Health & Failure Diagnostics

This standalone, deterministic module implements frozen Scenario D: configuration
or a green health claim cannot establish an end-to-end payment integration test.
It is evidence assessment only, with no checks executed, state writes, provider
clients, credentials, payment actions, recovery engine or production certification.
S3's API, schemas and original branch remain unchanged.

## Python API and contracts

```python
from app.integration_health import HealthInput, assess, validate_assessment
payload = HealthInput.model_validate(exported_evidence)
result = assess(payload)
validated_result = validate_assessment(payload, result)
```

`HealthAssessor` is the structural interface; `DeterministicHealthAssessor` supplies
its `assess(HealthInput) -> HealthAssessment` method. All inputs have explicit
`project_id`, `integration_id`, `environment` and `as_of`. The only environments
are simulation and sandbox. Simulation capabilities describe simulated observations,
never actual provider connectivity or an actual financial transaction.

JSON schemas: `contracts/integration-health/input.schema.json` and
`output.schema.json`. Six deterministic input/output examples live in
`contracts/integration-health/examples.json`. To regenerate all three artifacts:

```
backend/.venv/Scripts/python -m app.integration_health.examples
```

The result includes status, fresh supported capabilities with citations, missing
checks, stale and incomplete evidence warnings, supporting and contradictory IDs,
last successful check timestamps, exact source-reported facts, explicit assumptions
and a suggested manual diagnostic step. `human_review_required` is always true;
`production_certification` is always false. Facts use S3's `Fact` type: `field=status`
is a detached projection of the health record's `outcome`; success describes a
source report, not independently authenticated truth.

## Decision policy

Freshness uses **checked_at**, never delivery/receipt time or the machine clock.
`as_of` is explicitly supplied; the maximum age defaults to 86,400 seconds and is
configurable from 1 to 2,592,000 seconds. Evidence exactly at the limit remains
fresh. Future records and receipt-before-check timestamps are rejected. Exporters
must normalize clock uncertainty or report missing evidence, not silently repair it.

The current record for each check is the greatest checked_at. At equal times,
failure wins over unknown, which wins over success; conflicting outcomes degrade
health. Current capture/order checks are additionally compared per transaction.
Old failures do not overwrite newer success merely because they arrive late.

| Status | Meaning |
|---|---|
| UNKNOWN | No authoritative check evidence supplied; untrusted claims cannot promote it |
| UNVERIFIED | Some check evidence exists, but required fresh success evidence is missing |
| DEGRADED | Current fresh check failure, equal-time conflict, incomplete export, stale current required evidence, or a proven transaction with missing prerequisites/webhook evidence |
| TRANSACTION_VERIFIED | Fresh configuration and connectivity, a fresh successful transaction summary with linked fresh capture and completed-order records for the same transaction, and fresh webhook processing when required |

A success summary alone is insufficient. Its `evidence_refs` must identify both
an authoritative provider_capture success and an authoritative order_completion
success with the same nonempty transaction_id. Their checked_at must not follow
the summary. A newer failed/unknown proof observation invalidates current proof.
These are diagnostic test records, not a rule treating every ordinary declined
customer payment as an integration failure.

Capabilities are scoped to the supplied environment and check. A transaction
capability may be supported while overall status is DEGRADED due to missing
connectivity or webhook checks. `webhook_required` defaults to true; only an
explicit false makes its absence optional. Webhook failure, when supplied, remains
counterevidence even if the check is optional. No webhook success is inferred from
capture or merchant completion.

`last_successful_checks` retains historical success even when stale or followed by
failure; transaction entries require the linked proof that existed at the summary
time. It is not the time the integration was last certified healthy. Stale history
is reported but does not negate newer complete fresh evidence. Health claims have
no authority; optimistic claims contradicting an unverified/degraded state are
listed for investigation. Failure citations identify check failure observations;
contradictory does not mean their source is corrupt.

## Claude's future S0-S2 export adapter

No database schema is assumed. Supply detached provider-neutral objects from one
authorized project, integration and environment. Enforce caller access control
before exporting; matching IDs here are a consistency check, not authentication.
Do not pass ORM sessions or live provider handles.

Each record must contain:

- Unique `evidence_id`, stable immutable `event_id`, project/integration/environment.
- `check`: configuration, connectivity, transaction, webhook, provider_capture,
  order_completion, or health_claim.
- `outcome`: success, failure or unknown. Map structured error records to failure
  for the associated diagnostic check; never parse log prose as proof.
- Authenticated `source`, checked_at (actual check/occurrence time) and received_at
  (delivery/observation time), both with timezone offsets.
- A correlated transaction_id for capture/order/transaction proof and evidence_refs
  on the end-to-end transaction summary. Completion here means the merchant order
  reached the agreed diagnostic completion state, not an invented fulfillment action.

Allowed provenance by check:

| Check | Authoritative source |
|---|---|
| configuration | check_runner |
| connectivity | check_runner or provider |
| transaction summary | check_runner |
| provider_capture | provider |
| order_completion | merchant |
| webhook processing | check_runner or merchant |
| any diagnostic check in simulation | simulation_runner |
| health_claim | none |

operator_claim and untrusted_log never establish capabilities. simulation_runner
is ignored in sandbox. Source values must come from authenticated record provenance,
not client-supplied text. This module cannot cryptographically authenticate evidence
or prove that an upstream runner actually performed a check.

Preserve distinct delivery evidence IDs when an event is redelivered. Repeated
event IDs with identical structured contents collapse to one stable citation;
received_at and ignored text may differ. Any conflicting immutable event payload
(including changed source, proof references or checked_at) is rejected. Distinct
event IDs at equal check times with conflicting outcomes are accepted as conflict
evidence and assessed conservatively. Proof references to duplicate deliveries are
resolved by event identity. Evidence IDs duplicated in one input, unknown references,
cross-project/integration/environment records and future records fail validation.

Supply explicit `as_of`, freshness policy, whether webhook verification is required,
and `incomplete_evidence_warnings` for missing pages, gaps or truncated exports.
Completeness warnings prevent transaction-verified status. Never infer that absent
records establish a failed transaction. No S0-S2 table names, migrations or recovery
request contracts are required.

## Security boundaries and limitations

`assess` and `validate_assessment` revalidate detached model data at the public
boundary, including model_copy/model_construct inputs. All citations (capabilities,
facts, support and contradictions) reuse **unchanged** S3 `validate_diagnosis` via
an internal evidence projection. Scope checks run first. The health validator then
recomputes the entire deterministic assessment, rejecting forged statuses or
capabilities even when their citations name existing but unrelated records.

`diagnostic_text` is bounded, excluded from serialization, never classified,
executed, returned or logged. The module has no logging or network code. The
exporter must still redact secrets/PII before model construction and avoid logging
raw input or Pydantic exceptions with input values (`errors(include_input=False)`
is appropriate at an application boundary). There is no real credential field.
Diagnostic next steps are advice only; no check or recovery action is scheduled.
All authority remains conditional on upstream source authentication and clock
accuracy. TRANSACTION_VERIFIED certifies neither production health nor future
availability. No real provider, real financial transaction or LLM was exercised.

## Reproduce tests

From backend:

```
.venv/Scripts/python -m pytest health_tests -q --cov=app.integration_health --cov-report=term-missing
.venv/Scripts/python -m pytest tests diagnosis_tests health_tests -q -n 6
```

The pytest-xdist worker tool is local tooling only, not a new project dependency.
Tests block socket connections, use synthetic records and exercise all ten requested
case families, including malicious text, cross-scope input/output, forged existing
and nonexistent citations, event collisions, delayed history, proof correlation,
freshness boundaries, optional webhooks and historical successful-check semantics.

The proof contract checks correlation, provenance classifications, outcomes and
freshness, not amounts, currencies, merchant identity or fulfillment correctness.
The exporting diagnostic runner must validate those domain-specific details before
reporting a successful check and must use unambiguous transaction correlations.
No claim about those details is inferred from matching transaction_id alone.

## Verified delivery checks — 2026-11-11

- Focused health tests: **32 passed**; **99%** module statement coverage, with assessor and models at **100%**. Only the examples CLI entrypoint was uncovered.
- Final full offline suite: `python -m pytest tests diagnosis_tests health_tests -q -n 6 --cov=app.integration_health --cov-report=term-missing`: **1,189 passed**, seven existing Starlette/httpx deprecation warnings, 194.54 seconds.
- Bandit against `backend/app/integration_health`: no findings.
- Installed-environment `pip_audit --skip-editable --progress-spinner off`: no known vulnerabilities; editable project is excluded from package advisory lookup and reviewed as source.
- Repository `scripts/check_secrets.py --git`: passed; this is a heuristic pattern scanner, not a complete secret-detection guarantee.
- Careful source self-review covered scope isolation, citation/proof relationships, duplicate-event safety, history/freshness ordering, source trust, malicious text, no operational side effects and limitations. The final full run followed the historical-proof reporting refinement; an earlier partial run was replaced.
- All examples and tests use synthetic offline evidence. No provider, credentials, financial actions or LLM were exercised. No project dependency declarations changed.
