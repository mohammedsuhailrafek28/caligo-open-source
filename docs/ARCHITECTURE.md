# Caligo backend architecture (R0 baseline)

Baseline: evidence MVP, 738 offline tests before R0. This file is guarded by `backend/tests/test_docs_consistency.py`:
every table must be inventoried here, every `file::symbol` in the dependency section must exist, and the unused scaffolding must stay unused
until this document is updated.

## Layers
`routers/` (thin, auth + CSRF + ownership) -> `services/` (all behavior) -> `models.py` (SQLAlchemy, append-only guards) with
`schemas.py` (Pydantic, the contract source) and `payment_provider.py` / `paypal_sandbox.py` (the only provider access).
There is exactly one payment adapter port (`PaymentProvider`) and exactly one authority on payment status (`reconcile.TRANSITIONS`).

## Table inventory (21 tables)
| Table | Purpose | Append-only | Written by | Status |
|---|---|---|---|---|
| `sellers` | seller accounts (Argon2id hash) | no | auth router | in use |
| `seller_sessions` | server-side sessions (hashed tokens) | no | auth router | in use |
| `projects` | one agreement; `status` is financial only (see lifecycle below) | no | projects router, reconcile | in use |
| `scope_records` | immutable seller-entered scope text + SHA-256 | yes | projects router | in use |
| `payment_records` | one provider invoice per row; optimistic locking | no (history in `payment_events`) | payments service, reconcile | in use |
| `payment_events` | immutable payment history | yes | reconcile, payments service | in use |
| `scope_baselines` | immutable versioned agreement snapshots | yes | scope service | in use |
| `requirements` | requirements belonging to a baseline | yes | scope service | in use |
| `scope_changes` | proposed changes (status mutable through the scope service only) | no | scope service | in use |
| `audit_events` | immutable trail of meaningful transitions | yes | scope service `record_audit` | in use |
| `evidence_artifacts` | delivery artifacts with server hash and versions | yes | evidence service | in use |
| `artifact_links` | seller-asserted artifact -> requirement links | yes | evidence service | in use |
| `buyer_requests` | pending asks to one mailbox (hashes only) | no | buyer service | in use |
| `buyer_decisions` | verified-mailbox decisions | yes | buyer service | in use |
| `analysis_runs` | validated analysis output + input hash | yes | analysis service | in use |
| `payment_instructions` | R1: versioned, fingerprinted payment instruction (payee, recipient, currency, amount, agreement context, hash chain) | yes | instructions service | in use |
| `instruction_change_requests` | R1: proposed instruction changes (a pending one HOLDS payment operations) | yes | instructions service | in use |
| `instruction_change_resolutions` | R1: immutable record that a proposal was `WITHDRAWN` (history only; it does NOT release the hold; applying or verifying a change is R2) | yes | instructions service | in use |
| `buyer_authenticators` | R2: operator-established TOTP authenticator per project and buyer; secret stored AES-256-GCM encrypted | yes | authenticators service (operator CLI) | in use |
| `authenticator_revocations` | R2: operator revocation of an authenticator (replacement = revoke + enrol) | yes | authenticators service (operator CLI) | in use |
| `instruction_verification_challenges` | R2: single-use challenge bound to project, change, fingerprints, approver, authenticator, expiry; only `failed_attempts`/`consumed_at` change | no (two counters) | instruction_verification service | in use |
| `totp_uses` | R2: consumed TOTP time steps (UNIQUE per authenticator = replay guard) | yes | instruction_verification service | in use |
| `instruction_change_verifications` | R2: the ONLY record that releases a hold (APPLIED or REJECTED), one per change | yes | instruction_verification service | in use |
| `merchant_ledger_imports` | R3: one uploaded merchant-ledger file (file hash = idempotency); rows are unverified claims | yes | observations service | in use |
| `payment_observations` | R3: what each source (PROVIDER_API written only by reconciliation; MERCHANT_IMPORT/SELLER_ENTRY are claims) said about a payment | yes | observations service, reconcile | in use |
| `provider_evidence_gaps` | R3: provider values that could not be stored as observations (evidence is incomplete); bounded hash only | yes | observations service | in use |
| `sim_inventory_items` | S1 SIMULATED merchant stock item and initial stock (balance is derived); see STATE_RECOVERY_ARCHITECTURE.md | yes | simulation store | in use (simulation only) |
| `sim_inventory_movements` | S1 SIMULATED stock movements keyed by a unique business operation | yes | simulation store | in use (simulation only) |
| `sim_orders` | S1 SIMULATED merchant orders tied to one same-project payment; state + version only, identity immutable | no (state only) | simulation store | in use (simulation only) |
| `sim_fulfillments` | S1 SIMULATED shipments; DISPATCHED is final and carries its dispatch time | no (state only) | simulation store | in use (simulation only) |
| `sim_incoming_events` | S1 SIMULATED incoming events, one row per delivery, replay status and payload digest | yes | simulation store | in use (simulation only) |
| `sim_audit_records` | S1 SIMULATED merchant-side effects and processing attempts (SAFE vs FAULTY_FIXTURE) | yes | simulation processors | in use (simulation only) |
| `sim_recovery_plans` | S2 SIMULATED guarded recovery plan versions (steps, evidence, fingerprints); never rewritten; see S2_GUARDED_RECOVERY.md | no | simulation recovery | in use (simulation only) |
| `sim_recovery_events` | S2 SIMULATED append-only recovery history: creation, approval (version + fingerprint), steps, execution, refusals | yes | simulation recovery | in use (simulation only) |
| `payee_destination_versions` | Payee destination registry: append-only, chained, SELLER_ASSERTED destination versions per project; advisory input to screening only, never an authority over holds or instructions | yes | payee registry | in use |
| `deliverables` | scaffolding from the original plan | no | nothing | **unused** |
| `delivery_events` | scaffolding from the original plan | no | nothing | **unused** |
| `evidence_claims` | scaffolding from the original plan | no | nothing | **unused** |

## Unused scaffolding
Three tables and their companions were created for the original plan (Gate 2/4) and are superseded by the evidence MVP. They have no
route, no service and no test beyond schema creation. They are **kept** (no model is removed in R0) but must not be built on silently.

| Item | What it was for | Superseded by | Decision |
|---|---|---|---|
| `deliverables` (`Deliverable`) | uploaded files with `storage_key` | `evidence_artifacts` (text up to 64 KB, server hash) | Keep. Revive only when an object store exists; then add versioning to match `evidence_artifacts`. |
| `delivery_events` (`DeliveryEvent`) | `LINK_SENT` / `LINK_ACCESSED` / `FILE_RELEASED` / `BUYER_ACKNOWLEDGED` per deliverable | `audit_events` + `buyer_decisions` | Keep. It is mutable-looking and not append-only; do not use it as evidence. |
| `evidence_claims` (`EvidenceClaim`) | stored claims with `reviewer_approved` | `analysis_runs` (validated, citation-checked, append-only) | Keep. Never write claims without the analysis validator. |
| `DeliveryEventType` enum, `DeliveryEventView` schema, route rows for `/deliverables`, `/deliveries/{id}/link`, `/buyer/deliverables/{id}` | the Gate 2 handover flow | not built | Documented as planned only in the contract. |
| `EvidenceStatus`, `EvidenceFinding`, `ScopeAnalysis` schemas | the original analysis shape | `AnalysisFinding` / `AnalysisRunView` | Kept for contract continuity; `ScopeAnalysis` is not served by any route. |

Consequence: the four handover event names in older docs (`LINK_SENT` ...) describe a design, not behavior. The implemented handover
evidence is the buyer request/decision flow plus `audit_events`.

## Project lifecycle decisions (R0)
`Project.status` (`DRAFT -> INVOICED -> PAID`) is a **financial** field. It is written only by `reconcile.apply`, which advances it from
`DRAFT`/`INVOICED` to `PAID` on verified provider evidence; refunds never move it.

| Question | Decision | Reason |
|---|---|---|
| Set `DELIVERED` when the buyer accepts delivery? | **No (pending).** | The reconcile guard only moves `DRAFT`/`INVOICED` to `PAID`. Setting `DELIVERED` before payment would stop a later verified payment from ever recording `PAID`. Setting it after payment would hide that the payment was verified. |
| Set `DISPUTED` when the buyer disputes? | **No (pending).** | It would overwrite `PAID` (losing verified-payment information) and there is no defined exit once a later decision supersedes the dispute. |
| What represents delivery state instead? | **Implemented:** read-only `ProjectDetail.delivery_status` (`NONE`, `ACKNOWLEDGED_ONLY`, `ACCEPTED`, `DISPUTED`), derived from the buyer's **latest** verified `DELIVERY_REVIEW` decision (`buyer.delivery_status_of`). The evidence report uses the same function. | Backward compatible (additive field), supported only by authoritative evidence (verified buyer decisions), never stored, never client-settable, never changes `status`. Seller notes, artifacts and scope-change decisions are ignored. |
| Are `ProjectStatus.DELIVERED` / `DISPUTED` removed? | **No.** They stay reserved, unassigned enum values. | Removal would change the database CHECK constraint and the contract. `test_no_code_assigns_the_reserved_project_statuses` fails if anything assigns them. |
| When could they be assigned? | Only with a documented transition table that (a) keeps a separate financial status, (b) defines the exit from `DISPUTED`, and (c) has tests for accept-before-pay and dispute-after-pay. | See unresolved question 6. |

## R1 payment instruction integrity (implemented)
* **Migrations:** Alembic (`backend/migrations`, `0001` = R0 baseline, `0002` = R1, `0003` = R2). Fresh databases are created and stamped at head in one transaction. An existing database that is not at head is refused (503 `schema_out_of_date`), never reset or auto-stamped. **Adopting a pre-Alembic (R0) database:** run `alembic stamp 0001` from `backend/`. `migrations/env.py` first compares the database with the stored R0 reference (`migrations/r0_schema.json`, DDL emitted by the R0 code at commit `6f60916`, via `app/schema_guard.py`): every table, column, constraint, index and append-only trigger must match exactly; missing, different or unexpected objects, dangling foreign keys, a non-SQLite dialect, or any target other than `0001` are refused before anything is changed. Migrations run with SQLite foreign keys OFF (the `payment_records` rebuild needs it), after a `foreign_key_check` pre-flight and verified again afterwards; a caller-supplied connection with foreign keys ON is refused. Back up the file before migrating real data; SQLite DDL is not fully transactional.
* **Instruction:** `app/services/instructions.py`. Payee = the provider's configured merchant (never client text); MOCK has none (`merchant_email` null), SANDBOX without a merchant is refused (`merchant_evidence_missing`). Fingerprint = SHA-256 of canonical content; each version is chained to its predecessor. Every instruction is `PENDING_VERIFICATION`; nothing in R1 produces another status.
* **Hold reasons:** `UNVERIFIED_INSTRUCTION_CHANGE` (a proposal without a hold-releasing resolution; a withdrawal does not count), `INSTRUCTION_INTEGRITY_FAILURE` (recomputed fingerprint/chain differs), `MERCHANT_MISMATCH`, `PROVIDER_MISMATCH`, `INSTRUCTION_REQUIRED` (only with `PAYMENT_INSTRUCTION_REQUIRED=true`). The hold is derived from rows on every check; there is no stored flag to flip.
* **Enforcement:** `create_invoice` (project lock, hold + deviation check, binds the payment to the instruction), `send_invoice`, the `resolve-review` accept path, and resumed creates. The provider-call claim (`_claim`) also carries `no_pending_change_clause` inside its compare-and-set UPDATE, so a hold placed after the check still stops the call. `reconcile.evaluate` judges evidence against the instruction's payee and adds `INSTRUCTION_TAMPERED`. Sync is deliberately NOT blocked by a hold.
* **Seller powers:** record, propose, withdraw. **Withdraw is recorded history only: it does not release the hold.** Only a resolution in `instructions.HOLD_RELEASING_RESOLUTIONS` lifts a hold, and that set is empty in R1 (a test pins it), so a project with a proposed change stays held, including after a withdrawal, and cannot receive a second proposal (`change_pending`). The instruction in force is unchanged and stays unchanged. There is deliberately no seller-only override, release or break-glass path (tested). **Limitation:** until R2 supplies independent verification such a project cannot invoice or send again; it can still be reconciled (sync) and read.
* **Not protected (honest limits):** a compromised seller account can still record a wrong initial instruction (it is `PENDING_VERIFICATION`, not verified); someone with database write access who also drops the SQLite triggers is detected only by the fingerprint/chain check; holds do not affect PayPal itself.
* **Known limits (R1 security reviews, no critical/high findings open):** (1) Because R1 cannot apply a change, the instruction in force is effectively permanent and a held project stays held until R2. (2) The fingerprint chain is unkeyed SHA-256 in the same database: it detects accidental or naive tampering, not a database writer who recomputes it; Postgres append-only triggers are not built and Postgres was never exercised. (3) Integrity, merchant and provider conditions need hashing, so they are evaluated under the project write lock in the same transaction as the claim UPDATE (not inside its WHERE clause); only the bound record values and, for sends, the hold are inside the UPDATE. Configuration cannot change within a running process. (4) A damaged instruction row makes the next sync flag that project's payments `INSTRUCTION_TAMPERED` (NEEDS_REVIEW, sticky). (5) Each refused attempt writes an audit row, capped only by the payment rate limit.
* **In-flight operation policy and linearization point:** (a) An invoice *create* that was durably RESERVED before a hold was established may complete, and resume after a crash or retryable provider failure, against the exact instruction it was bound to. The linearization point is the commit of the reservation, which happens under the project write lock together with the hold check (`gate_create`); a proposal committed before it blocks the create, one committed after it does not. (b) The claim that starts the provider call re-checks under the lock that the integrity, merchant and provider conditions still hold and its UPDATE only matches if the stored recipient, currency, amount and instruction binding equal the values it was checked against, so a hold can never change what a reserved operation sends. (c) A *send* is only reserved when its claim commits; the claim's UPDATE also requires no unreleased change, so a hold committed first always wins. (d) After the hold every new create, every send and review acceptance is blocked. (e) Limitation: a provider call already running when the hold commits is not interrupted, and a RESERVED create that waits a long time can still complete later against its bound instruction while the hold stands.
* **Implicit instruction:** with `PAYMENT_INSTRUCTION_REQUIRED=false` (default, backward compatible) the first invoice request records an instruction with provenance `INVOICE_REQUEST`; later invoices must match it.

## R2 independent authorization (implemented)
Full trust model, enrolment rules and limitations: `docs/R2_AUTHORIZATION.md` (read it before relying on R2). Summary:
* **Release rule:** a hold is lifted only by a row in `instruction_change_verifications`. `HOLD_RELEASING_RESOLUTIONS` stays empty, so a seller withdrawal still never releases a hold; a withdrawn change can only be *rejected* by the buyer-side authenticator.
* **Enrolment is operator-established and demo-only** (`trust_basis=OPERATOR_ESTABLISHED`): an e-mail OTP proves nothing about identity and is not used. Only `scripts/enroll_buyer_authenticator.py` (needs the database and the encryption keyring) creates or revokes authenticators; there is no seller route. An authenticator enrolled after a proposal cannot approve it.
* **Verification** (`app/services/instruction_verification.py`): challenge bound to project, change, previous and proposed fingerprint, approving actor (the instruction's `recipient_email`) and authenticator; decision echoes all of them plus an RFC 6238 code. Attempts are charged and committed before the code is checked (5 per challenge, 10 challenges/hour/authenticator); an expired, consumed, replayed (step <= last used) or guessed code gives one uniform 400.
* **Atomic release:** under `lock_project`, one transaction writes the TOTP use, challenge consumption, (for APPROVE) the new instruction version whose fingerprint equals the proposed one, the verification row and the audit event. Any failure rolls back and the hold stays. Reconciliation, the payment state machine and PayPal restrictions are untouched.
* **Secrets:** AES-256-GCM, AAD = authenticator id | project | buyer address, keyring from `AUTHENTICATOR_KEYS` (SecretStr), rotation by key id. Key loss => 503 `authenticator_unavailable`, hold stays; recovery = operator revokes and re-enrols (the new authenticator can reject, not approve, older changes).
* **Version 1 is never independently verified** (it stays `PENDING_VERIFICATION` even after later changes are authorized); a seller who is also the operator is labelled `OPERATOR_ESTABLISHED`, never independent. Environment procedure: `docs/DEV_SETUP.md`.
* **Remaining limits:** see `docs/R2_AUTHORIZATION.md` section 7 (operator-established trust, operator==seller means no independence, operator/DB+key compromise, version 1 stays `PENDING_VERIFICATION`, per-process in-memory throttle, SQLite only).

## Dependency points for R1-R6
These are the exact places the planned work attaches. Nothing below is implemented yet. Every reference is verified by a test.

**R1 payment instruction + HOLD (a gate on Caligo's own operations; it never touches PayPal funds)**
* Gate before a provider call is reserved: `app/services/payments.py::create_invoice`, `app/services/payments.py::send_invoice`.
* Gate on accepting provider evidence: `app/services/payments.py::resolve_review` and `app/services/reconcile.py::apply`.
* The fields an instruction would fingerprint are already carried by `app/payment_provider.py::ProviderInvoiceRequest` and compared in `app/services/reconcile.py::evaluate` (recipient, merchant, amount, currency). A mismatch with the verified instruction becomes a new **anomaly code in `evaluate`**, not a new payment status.
* Versioned, append-only record pattern to copy: `app/services/scope.py::freeze_original`, `app/services/scope.py::apply_change_decision`, `app/models.py::make_append_only`.
* Audit and timeline: `app/services/scope.py::record_audit`, `app/schemas.py::AuditActor`, `app/services/evidence.py::_audit_summary`.

**R2 pre-enrolled TOTP (a second channel; not legal identity, not universally independent)**
* Challenge/session/decision machinery to generalize: `app/services/buyer.py::send_challenge`, `app/services/buyer.py::verify_code`, `app/services/buyer.py::decide`, `app/models.py::BuyerRequest`, `app/models.py::BuyerDecisionRecord`.
* Allowed-decision table and kinds: `app/services/buyer.py::ALLOWED`, `app/schemas.py::BuyerRequestKind`.
* `buyer_decisions` has a CHECK constraint limiting `verification_method` to `EMAIL_OTP`; adding a method needs a schema change (see question 1).
* Throttling and hashing to reuse: `app/security.py::LoginThrottle`, `app/security.py::hash_token`.

**R3 payment observations + merchant-ledger import**
* Add read methods to the single port: `app/payment_provider.py::PaymentProvider`; normalized evidence type `app/payment_provider.py::ProviderInvoice`.
* The one place provider evidence is fetched and written: `app/services/payments.py::sync_project_payments` and `app/services/payments.py::_write_sync`.
* Pure evaluation input: `app/services/reconcile.py::evaluate` and `app/services/reconcile.py::Evaluation`.
* Offline test double to extend: `tests/fake_paypal.py::FakePayPal`.

**R4 recovery advice (no financial actions)**
* Anomaly codes are the strings produced by `app/services/reconcile.py::evaluate`; every code must map to an explanation (R4 adds a test that extracts them and fails on an unmapped code).
* The only exit from `NEEDS_REVIEW` stays `app/services/payments.py::resolve_review`. The port has no refund or cancel method and must not gain one.
* History to cite: `app/models.py::PaymentEvent`.

**R5 report and analysis**
* Sections: `app/services/report.py::build_report`, `app/services/report.py::_unresolved`, `app/services/report.py::_payments`.
* Citable record types and relationship checks: `app/services/analysis.py::allowed_references`, `app/services/analysis.py::_SUPPORT_CHECKS`, `app/services/analysis.py::validate_findings`; categories in `app/schemas.py::FindingCategory`.

**R6 demo**
* `scripts/demo_evidence_flow.py` (outside the checked reference format) and the mock provider's `simulate_*` helpers in `app/payment_provider.py::MockPayPalProvider`. App wiring for any new service state lives in `app/main.py::create_app`; flags in `app/config.py::Settings`.

## Unresolved architecture questions (need your decision before the named work)
1. **Migrations (resolved at R1).** Alembic is in place; R2 must add a revision that widens the `buyer_decisions.verification_method` CHECK. Postgres has not been exercised.
2. **What is a payment instruction in PayPal terms (before R1)?** The adapter only handles Invoicing: merchant (invoicer) email, recipient email, currency, amount. There is no bank-beneficiary concept in what Caligo touches. Confirm the instruction is exactly those fields.
3. **Who is protected (before R1)?** Is the threat (a) a tampered payee/amount between agreement and invoice, or (b) a mid-project "please pay a different account" change reaching the buyer? Who verifies the change - the buyer, the seller, or both?
4. **TOTP secret handling (before R2).** TOTP needs a recoverable secret. That requires an encryption key from the environment; there is no secret manager. Where is enrollment performed and by whom, and is storing an encrypted secret acceptable for the demo?
5. **Hold release authority (before R1).** Who may release a hold, with which evidence, and is a break-glass path allowed? (The audit assumed: never the seller alone.)
6. **Reserved statuses (any time).** Do you want `DELIVERED`/`DISPUTED` ever stored, or is the derived `delivery_status` the permanent design?
7. **Time and ordering (before R3).** `monotonic_utcnow` is process-wide; multiple processes or a Postgres deployment need a database-level ordering guarantee before observations are merged from several sources.
8. **Raw provider payload retention (before R3).** Persist only normalized, hashed observations (assumed), or also raw payloads (privacy and size)?
9. **Hold TOCTOU (R1, resolved).** A create reserved before a hold may complete against its bound instruction; a send cannot start once a hold exists; see the in-flight policy in the R1 section.
10. **Append-only on Postgres.** Triggers are SQLite-only; revoke `UPDATE`/`DELETE` or add Postgres triggers before any non-SQLite deployment.
