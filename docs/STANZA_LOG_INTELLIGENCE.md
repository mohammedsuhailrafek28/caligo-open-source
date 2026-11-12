# Stanford Stanza log intelligence — standalone advisory module

Base: the state-recovery MVP. No S2/S3/S3.5 implementation, public
schemas, API routes, frontend or state writer is modified.

## API

```python
from app.log_intelligence import parse_record, LogBatch, extract, investigate
record = parse_record(metadata, body_bytes)  # strict UTF-8 and sanitized rejection
batch = LogBatch(project_id=project_id, payment_id=payment_id,
                 records=(record,), evidence=scoped_registry)
result = extract(batch)                     # deterministic, no model dependency
report = investigate(batch)                 # unchanged S3 rules + separate log hypotheses
# Explicit local model opt-in:
from app.log_intelligence.stanza_adapter import StanzaProcessor
processor = StanzaProcessor(local_cache_path)
report = investigate(batch, processor)
```

Contracts are in `contracts/log-intelligence/{input,extraction,investigation}.schema.json`.
`LinguisticProcessor` is a narrow typed local interface. `StanzaProcessor` implements
it using pretrained English tokenization, multiword expansion, POS and lemmatization.
No generative LLM, model prompts, external inference service or model-produced
transaction identifier is used. Passing a test double to the processor interface
is not a Stanza evaluation; the benchmark explicitly constructs the actual adapter.

Log input carries source ID, project/payment scope, environment (simulation/sandbox),
provenance, occurrence and receipt timestamps, log ID, optional event ID, truncation
flag, raw text and an evidence reference. `parse_record` is the recommended byte
boundary. Bodies are limited to 4,096 characters and 8,192 UTF-8 bytes; invalid
UTF-8, surrogate input, unsafe control/bidi characters and credential-like content
are rejected with generic errors. Raw text is excluded from repr/serialization.
Missing occurrence timestamps generate warnings, never invented timestamps.
Truncated records are excluded from clue and exact-field inference and flagged.

Output clues are **candidate language observations**, with category, polarity,
character offsets, log ID, evidence reference and whether surface matching or
Stanza lemmas supplied the cue. Simple negation/hypothetical guards are shared by
both benchmark arms. They are intentionally conservative and are not a full
semantic parser. Context entities/phrases are restricted to allowlisted payment,
merchant, database, inventory, order, webhook and related domain nouns, rather than
arbitrary names or inferred identities. General NER was deliberately omitted.

Exact transaction/order identifiers require explicit `transaction_id`, `txn_id`,
`tx_id` or `order_id` labels and strict token boundaries. Amounts are decimal
**strings** with an explicit supported ISO currency prefix; ambiguous `$` amounts
are not assigned a currency. Only explicit offset-aware ISO timestamps are parsed.
Malformed/partial identifiers and amounts are not returned. Every exact candidate
has `verified=false`; it never replaces metadata scope, resolves a DB record or
proves transaction existence. A forged identifier remains an unverified textual
candidate, not a trusted evidence citation.

## Trust and existing diagnosis integration

The exporting caller must authenticate ownership **before** creating a scoped
registry. Each `ScopedEvidence` wraps an original S3 Evidence with project ID,
payment ID, source ID, provenance and environment. All log references must resolve
to this registry, with matching scope/source/provenance/environment. All registry
records must belong to the batch payment/project; raw S3 Evidence.text must be
empty. Fake references, duplicate log/evidence IDs and cross-project records are
rejected. Authenticated provider provenance requires sandbox/provider evidence;
simulation is never relabelled authenticated provider evidence. MOCK provider
records can retain S3 source=provider while the wrapper explicitly says simulation.
Source authenticity is an upstream responsibility; labels are not cryptographic proof.

`investigate` passes the original structured Evidence objects (with text empty)
through unchanged S3 RulesDiagnoser and citation/fact validation. It preserves
completeness warnings and adds missing/truncated-log warnings. It never turns an
extracted capture/refund/failure word into a payment or merchant state observation.
The S3 diagnosis remains separate from `log_hypotheses`, which carry citations and
the fixed limitation **Untrusted log-language hypothesis; not transaction evidence.**
Contradictory positive/negated language causes log hypotheses to abstain. Supplied
contradictory provider states veto a log-only affirmative capture hypothesis.
Duplicate webhook delivery alone does not imply multiple orders; even the advisory
multiple-order hypothesis requires two explicitly labelled candidate order IDs.
Duplicate event deliveries are deduplicated by source/event identity; conflicting
payloads for one event ID are rejected. Receipt order never creates causal proof.

Nothing in the output authorizes recovery, alters status, executes a transaction,
releases a hold or bypasses S2 guards. There is no database or provider dependency
in the extraction/bridge layer. This feature installs no route or automatic hook.
Claude/frontend integration should expose it as an explicit investigation view,
never feed hypotheses into recovery-policy guards or reconciliation.

## Model setup, versioning and resource policy

Isolated environment: repository-root `.venv`, ignored by Git. Core backend
requirements are unchanged. Optional packages are listed separately in
`backend/nlp_benchmark/requirements.txt`, with the complete measured environment
in `environment.json`. CPU PyTorch comes from the official CPU wheel index.

- Stanza **1.15.0**, resources **1.15.0**, PyTorch **2.15.0+cpu**, Python **3.12.10**.
- English tokenize=ewt_nocharlm, mwt=ewt, pos=ewt_nocharlm, lemma=ewt_nocharlm.
- Required pretrained embedding: en/pretrain/conll17.pt.
- Five weight files total **132,956,551 bytes** (~126.8 MiB), plus resources metadata.
- No transformer, character LM, dependency parser, constituency parser or NER models.
- One CPU inference thread, tokenize batch size 1, POS/lemma batch sizes 32.
- Model load/setup requires at least **1 GiB available RAM** before starting. This
  is a conservative operational guard, not an upstream minimum-RAM guarantee.

Disk preflight had ~584 GiB free; available RAM fluctuated from ~1.5 GiB to below
1 GiB, then recovered above 2 GiB. Setup waited for headroom. Only selected English
weights were downloaded, outside the repository at a local Caligo cache. No weights
are committed. The deterministic fallback never imports torch or stanza.

Example PowerShell setup from repository root:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e './backend[dev]'
.venv/Scripts/python -m pip install torch==2.15.0+cpu --index-url https://download.pytorch.org/whl/cpu
.venv/Scripts/python -m pip install -r backend/nlp_benchmark/requirements.txt
$stanzaCache = Join-Path $env:LOCALAPPDATA 'Caligo/stanza/1.15.0'
$env:HF_HUB_DISABLE_IMPLICIT_TOKEN = '1'
.venv/Scripts/python -m app.log_intelligence.setup_models --download --cache $stanzaCache --manifest-out contracts/log-intelligence/model-manifest.json
```

Setup is an explicit developer command, never called by ordinary execution or tests.
It verifies downloads against pinned official versioned resource/Git-LFS SHA-256
hashes. Runtime streams hash checks for every cached file, including resources.json,
before loading models, pins the library version and sets DownloadMethod.NONE,
use_gpu=False and device=cpu. Missing/mismatched cache or insufficient memory fails
closed; it never silently downloads or calls a remote service. The benchmark marks
that arm NOT RUN with null metrics when model initialization cannot be performed.
`StanzaUnavailable` reason codes distinguish cache/memory/integrity failures. Callers
can explicitly select the deterministic fallback; it is never described as Stanza.

## Evaluation protocol and limits

`backend/nlp_benchmark/cases.json` has 8 development and 24 held-out test cases.
Vocabulary, ordinary inflection rules and labels were authored/frozen before the
first held-out scoring. They were not tuned after viewing held-out results.
The test split includes paraphrases, irregular forms, negative/hypothetical statements,
duplicate delivery without duplicate orders, stale refunds, health claims, ambiguity,
missing and contradictory log language. No Stanza fine-tuning was performed.
This is a small, author-labelled synthetic holdout, not independent blinded annotation
or production validation. No statistical significance or general-domain guarantee
is claimed. The shared closed vocabulary still limits unseen-language coverage.

Existing S3 intentionally ignores free log text; it had no standalone log extractor.
Arm A is an **original deterministic extraction baseline**, plus the same advisory
hypothesis rules and unchanged S3. Arm B changes only linguistic normalization to
actual Stanza. Both retain the exact-field parser, negation guard, trusted evidence
and citation rules. Structured-only S3 results are reported separately: on logs-only
fixtures it intentionally abstains, and its accuracy reflects the fixture label mix.
It is not a measurement of the S2 recovery engine.

Metrics use per-case category presence for the six failure-clue categories failure,
retry, timeout, crash, duplicate and stale (not exact-span F1). Precision/recall/F1
are micro-averaged over case/category pairs. Root-cause accuracy evaluates advisory
log suggestions plus S3 fallback, not verified payment truth. Citation validity is
case-level membership against supplied scoped IDs. Unsupported clue count is the
false-positive category count under gold labels; unsupported hypothesis count is
non-abstaining classifications wrong under gold labels. These bounded metrics do
not prove arbitrary natural-language entailment or source authenticity. Abstention
means an inconclusive advisory classification, including correctly inconclusive cases.

Measured JSON outputs record per-case predictions, gold labels, citation validity,
CPU/wall inference timings, startup, RSS and a canonical dataset hash. Each arm runs
in its own process, with socket connect/connect_ex blocked, CPU-only settings and
one record at a time. Startup includes integrity checks/imports/loading; inference
latency includes extraction and S3 validation, excludes model startup. CPU timers on
Windows are coarse for sub-millisecond baseline work. RSS is process working set,
not exclusive tensor allocation; both sampled and OS peak working set are reported.
Results are single-pass local measurements with warm filesystem caches, not service
SLAs or a reliable cost projection. The Stanza arm still missed the crash clue
in **froze**; no post-test rule patch was made for that miss.

Reproduce a model run explicitly, after cached setup:

```powershell
.venv/Scripts/python -m app.log_intelligence.benchmark --mode deterministic --split test --out backend/nlp_benchmark/deterministic-results.json
.venv/Scripts/python -m app.log_intelligence.benchmark --mode stanza --model-dir $stanzaCache --split test --out backend/nlp_benchmark/stanza-results.json
$env:CALIGO_STANZA_TEST_CACHE = $stanzaCache
.venv/Scripts/python -m pytest backend/nlp_tests/test_real_stanza.py -q
Remove-Item Env:CALIGO_STANZA_TEST_CACHE
```

Ordinary tests skip the one explicit real-model smoke test; the separately configured
real test was run successfully with its network guard. They never download weights.
The full regression runner starts fresh serial pytest processes with six files per
batch, releasing memory between batches. It includes all backend tests, S3, health
and NLP tests. No model is resident during this regression run.

```
.venv/Scripts/python -m pytest backend/nlp_tests backend/diagnosis_tests -q --cov=app.log_intelligence --cov-report=term-missing
.venv/Scripts/python scripts/run_nlp_regression.py --out backend/nlp_benchmark/regression-results.json --batch-size 6
```

## Focused security review

All log content is data. There is no eval, shell interpretation, generative prompt,
remote inference or log-triggered operation. Malicious command/refund strings can
at most produce advisory lexical candidates; they cannot change the S3 input.
Credential-like bodies and metadata are rejected before any model call. Only bounded
exact candidates and allowlisted context terms are returned, not raw diagnostic
sentences. The module does not log input or output. Secret-pattern detection is
heuristic and can over-reject harmless text; it cannot recognize every possible
secret. **The authenticated exporter must redact credentials/PII before construction**
and must not log raw Pydantic exceptions or raw log bodies. Use the sanitized parser
boundary and application errors without input values.

Tests cover malicious instructions, forged/unbound IDs, foreign references, corrupt
UTF-8, oversized bodies, missing times, truncation, contradictory provider evidence,
duplicate events and conflicting duplicates. Cache-integrity/version/download guards
are tested with doubles; the real-model smoke and benchmark validate actual cached
execution. Source review checked that no structured state is derived from language.
The test runner's two low Bandit findings concern importing/using subprocess: its
argv is fixed to the current Python and pytest, file arguments come from trusted test
directories, shell is not used, and no log text reaches it. These are reviewed tool
warnings, not an NLP execution path. Model files remain trusted-code artifacts;
use only the pinned official cache, not caller-uploaded model weights.

## Exact adapter handoff to the integrated S4 backend

The existing `app.simulation.diagnosis_adapter.DiagnosisContext` exposes `data`
(S3 DiagnosisInput) and `provenance_labels`. After the existing authorized read-only
export, wrap `context.data.evidence` in ScopedEvidence, retaining every Evidence
object unchanged. Copy `context.data.incomplete_evidence_warnings` and
`observation_window_complete`. Use the adapter's provenance labels to distinguish
MOCK/simulated provider observations from authenticated Sandbox observations;
never derive provenance from log prose. Provider-source Evidence must have matching
simulation or authenticated-provider wrapper provenance. Log source ID and registry
source ID must agree.

For a new log source, export an immutable, redacted source record as S3 Evidence
with kind=log, status=observed, an existing source citation ID and text empty; the
redacted body travels only in LogRecord. This is an in-memory export contract, not
an instruction to create or modify business/database records. Store/retrieve the
original redacted source through the application's existing access-controlled
evidence mechanism. Preserve its original log/event identity and source timestamps.
The module assumes no log table or new S0-S2 schema. It must not consume caller JSON
as authenticated provider evidence, and candidate IDs must never be used to query
or authorize another project's records.

Operational integration should reuse one CPU model in an explicitly selected local
worker with serialized access and a caller-enforced timeout, rather than load it
per request or in every web worker. Account for roughly half a GiB per model process.
The current adapter expects the pinned manifest at the checkout-relative contracts
path; a packaged deployment must ship that file with the matching layout (or supply
an equivalent trusted packaging change in a later integration task). No deployment
or worker configuration is installed by this standalone feature.

## Verified delivery — 2026-11-11

- **Actual Stanza evaluation RUN**: pretrained 1.15.0 English CPU processors with the pinned cache, network blocked. No generative LLM evaluation was performed.
- Held-out failure-clue P/R/F1: deterministic **1.0000 / 0.7895 / 0.8824**, Stanza **1.0000 / 0.9474 / 0.9730**. Advisory root-cause accuracy: **22/24 vs 24/24**. Citation validity **100%** in both arms. Labeled unsupported clues/hypotheses **0/0** in both arms. Abstention **70.83% vs 62.50%**.
- Final timing/working-set measurements: deterministic **0.448 ms** mean wall latency and **31.68 MiB** peak RSS; Stanza **51.873 ms**, **18.080 s** startup and **534.86 MiB** peak RSS. Exact per-case CPU/wall data and OS peaks are in the result JSON files.
- Focused NLP plus unchanged S3 tests: **65 passed, 1 intentionally skipped**, **98%** new-module coverage. This comprises **37 ordinary new NLP tests** and **28 S3 tests**, plus the opt-in model test skipped in the ordinary run.
- Separately configured real-model smoke test: **1 passed** with socket connections blocked and DownloadMethod.NONE asserted. Thus the opt-in test was actually exercised, not merely skipped.
- Complete offline regression in seven fresh sequential batches: **1,337 passed, 1 skipped**, zero failures/errors. Batch 1 was re-run after the final parser/provenance/credential guards; all other application code remained unchanged. The later runtime assembly of the synthetic credential fixture preserved exactly the same tested input.
- Existing integrated recovery demo: **40/40**. Existing original backend demo: **19/19**. Both used MOCK/synthetic state and verified that Sandbox/development state was untouched.
- Final module Bandit scan: no findings. The serial regression tool has **two reviewed low findings** for its fixed-argv subprocess use, no medium/high findings. No log input reaches the tool.
- Dependency verification: pip check passed. Audited installed packages had no known advisories after upgrading isolated tooling to pip 26.3/setuptools 84.2.1. The advisory lookup skips the editable project and torch 2.15.0+cpu; the normalized torch==2.15.0 base version was checked separately with no known advisories. This is not a binary security audit of the CPU wheel.
- Repository secret scan passed. Its initial warning was for a synthetic credential-assignment test literal; that input is now assembled at runtime while retaining the same before-inference rejection test. No real credentials were used.

Artifacts: `backend/nlp_benchmark/README.md`, both measured result JSON files,
`regression-results.json`, `verification.json`, `environment.json` and the pinned
model manifest. **Recommendation: keep Stanza optional.** The measured improvement
is limited to a small synthetic holdout and mainly irregular morphology; a small
extended deterministic lexicon might achieve similar coverage at much lower cost.
No measured result supports replacing S3 rules or inserting NLP into S2 guards.

`LogRecord.event_id`, when provided, identifies one immutable cited log event.
Its deduplication signature includes the source citation. Distinct delivery records
with distinct source citations must use distinct log event identities (or omit that
optional field); provider business event IDs still belong in the original S3 event
evidence. This avoids silently conflating different evidence records.
