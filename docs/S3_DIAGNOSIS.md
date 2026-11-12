# S3 diagnosis integration contract (v1)

Standalone read-only Python module: `backend/app/diagnosis`. No routes, ORM models,
provider calls, state writers, recovery commands or frontend changes are installed.
Baseline: the R0 to R6 backend.

## API

```python
from app.diagnosis import DiagnosisInput, RulesDiagnoser, LLMDiagnoser
payload = DiagnosisInput.model_validate(s2_export)
result = RulesDiagnoser().diagnose(payload)
# Optional: LLMDiagnoser(actual_model_transport).diagnose(payload)
```

`Diagnoser` is a structural Python protocol in `models.py`. Input/output JSON schemas
are `contracts/diagnosis/input.schema.json` and `contracts/diagnosis/output.schema.json`.
Output separates exact structured observations (`facts`) from causal `hypotheses`.
Facts establish what a source reported, not that the report is true. Every result
requires human review. No numerical confidence is invented.

## Mapping Claude must supply from S2

Create a detached export in the authorized project/merchant scope; do not pass ORM
objects, credentials, customer PII, payment capabilities or database sessions.
The caller must enforce access control and scope **before** constructing this input.
`case_id` identifies one diagnostic case. Evidence IDs must be unique within it;
assign distinct IDs to repeated deliveries of the same event. Do not collapse them.

For each record supply `evidence_id`, `source`, `kind`, `entity_id`, `payment_id`,
`status`, timezone-aware `observed_at`; supply `occurred_at` when known. Use the
same payment correlation key only for records that truly belong to that payment.
Sources: provider (authenticated observation), merchant, simulation, operator,
untrusted_log. Never relabel simulation, seller claims or log prose as provider.
Kinds: event/payment/order/inventory/fulfillment/integration/recovery/log.
Unknown statuses can be retained; rules use only the following exact normalized values:

| Records | Required values / links |
|---|---|
| Event deliveries | status paid/refunded; stable provider `event_id`; occurrence and receipt times separately |
| Provider observations | kind payment, source provider, status captured |
| Merchant order snapshots | kind order, status failed where applicable; unique order entity_id; caused_by_event_id if supported by audit evidence |
| Inventory / shipment effects | kind inventory/fulfillment, status applied; entity_id identifies a distinct committed effect, effect_key identifies one intended logical effect |
| Recovery requests | kind recovery; distinct request entity_id and shared logical effect_key for repeats |
| Integration | kind integration, status healthy or e2e_succeeded; scope export to one integration and observation window |

Export one selected snapshot per order at a consistent cutoff, not every historical
version as simultaneous current state. Distinct effect IDs must represent actual
committed mutations, not repeated reads of one mutation. Recovery requests are
observations only: repeated requests do not prove repeated execution.
Populate `incomplete_evidence_warnings` for missing/delayed/unmapped records,
clock uncertainty, unavailable provider data and truncation. Set
`observation_window_complete` only when completeness is established. Missing
`occurred_at` prevents a stale-event ordering conclusion. Absent e2e evidence
means health is unproven, even in a complete window; it does not prove failure.
No assumption about S0-S2 tables, columns or migration names is made.

Rules select the first matching pattern in this priority: duplicate orders,
capture divergence, stale paid after refund, duplicate effects, repeated recovery,
then unproven health. Multiple patterns are explicitly flagged. Contradictory IDs
identify other same-payment, same-kind, same-entity records reporting a different
status from supporting records. Different observation times can explain such
conflicts; this is counterevidence to inspect, not proof of a corrupt source.

## Actual model adapter and trust boundary

Implement `LLMTransport.complete(system=..., evidence_json=..., output_schema=...)`
returning a JSON string and a nonempty actual `model_id`. Configure SDK, timeout,
rate limits and credentials outside this module. The transport must be a text-only
model invocation with no financial, filesystem, database or execution tools. Do not
run a model on customer data until caller-side redaction is applied. Exceptions
propagate; no automatic retry or rules-as-AI fallback occurs.

`LLMDiagnoser` validates JSON, case binding, all evidence citations (including facts
and hypotheses), exact structured fact values, method and human-review requirement.
Log text is explicitly untrusted in the system instruction and never drives rules.
Citation validity does not prove semantic entailment: a model can cite a real ID for
an unjustified hypothesis. Such prose is untrusted and requires human review.
The adapter cannot prove that a caller-supplied transport is a real model; test doubles
must never be presented as model evaluations.

## Reproduce benchmark and tests

From backend with project dependencies installed:

```
python -m pytest diagnosis_tests -q
python -m app.diagnosis.benchmark
```

`diagnosis_benchmark/cases.json` contains 16 synthetic labeled cases, including
positive rules and adversarial negative controls. `results.json` records the actual
offline run. These small hand-authored labels are not a held-out production dataset.
Classification accuracy is exact label match; citation validity is the fraction of
cases passing structural citation/fact validation. Unsupported structured claim
frequency counts non-inconclusive classifications with a wrong label or supporting
IDs outside labeled support. Arbitrary prose unsupported-claim frequency is null
and semantic review is NOT RUN; this metric cannot be honestly automated here.
Latency is local wall-clock elapsed time, environment-dependent and not a service SLA.

To compare a real model, call `app.diagnosis.benchmark.run(actual_transport)` and
save its returned report, model/version and configuration. No actual model was
configured for this delivery: AI evaluation is NOT RUN. Mock adapter tests only
verify boundary behavior. All rules results are deterministic heuristics.

## Reference study and originality

Studied https://github.com/skbgp/pay-saga at
9f02161be7700c7b2a0c2edc94c8b25595bc7cd3: README, common/workflow.go,
common/idempotency.go, services/order/outbox.go, services/payment/processor.go,
services/inventory/processor.go, saga/orchestrator.go and saga/compensation.go.
No LICENSE/COPYING or declared license was found in the tracked tree at that commit;
reuse permission was not established. No code or prose was copied/adapted.
All S3 implementation, labels and tests are original. Architectural study informed
investigation prompts around transactional outbox acknowledgement, durable event
identity, committed effect uniqueness, compensation failure and occurrence vs
receipt ordering. An outbox may redeliver after publish-before-commit failure;
duplicate delivery alone is deliberately not diagnosed as duplicate effects.
No competing saga or recovery engine was built.

## Verified delivery checks (2026-11-11)

- Full offline backend run: `python -m pytest tests diagnosis_tests -q -n 6 --cov=app.diagnosis --cov-report=term-missing`: **1,154 passed**, seven existing Starlette/httpx deprecation warnings, 202.07 seconds. This preceded the final schema-error redaction check.
- Final focused run after that boundary change: `python -m pytest diagnosis_tests -q --cov=app.diagnosis --cov-report=term-missing`: **28 passed**, **99%** module statement coverage (only benchmark CLI print entrypoint uncovered).
- Rules benchmark: **16/16** exact classifications, **100%** case-level citation validation, **0%** unsupported structured classifications under the supplied labels. Measured diagnosis timings and canonical fixture digest are in `results.json`.
- Actual model evaluation: **NOT RUN**. Free-prose semantic unsupported-claim review: **NOT RUN**. Neither has a fabricated score.
- `python -m bandit -r backend/app/diagnosis -q`: no findings. `python -m pip_audit --skip-editable --progress-spinner off`: no known vulnerabilities in the isolated installed environment; editable project excluded from package advisory lookup and reviewed as source. The venv's initial bundled pip was upgraded to resolve its advisory findings. No repository dependencies changed.
- `python scripts/check_secrets.py --git`: passed. This is the repository's heuristic pattern scanner, not a full secret-detection guarantee.
- Careful self-review checked scope binding, source trust, prompt injection, all structured citation paths, read-only dependency boundaries, error redaction and fixture labels. No S0-S2 models, migrations, recovery engine, financial operations, API routes or frontend files were modified.

Environment: Windows, Python 3.12.10, Pydantic 2.14.3. Test tools and project
dependencies were installed in an ignored local virtual environment. The initial
host-only full-suite attempt could not import email-validator; the isolated run
above resolved that environment issue. A slower serial full run was replaced by
the completed parallel run. Model transports must sanitize their own exception
messages and enforce timeout/redaction policy; no real transport was exercised.
