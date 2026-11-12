# API contract v0.10 — Stage 3E + R1 + R2 + R3 + R4 + R5 implemented (auth, projects, MOCK/SANDBOX invoicing, scope baselines, evidence, buyer decisions, analysis, report)

Routes marked **implemented** are served and tested; rows marked **Gate N** / **Optional Gate N** are **planned, NOT built, and absent from the server** until their gate.
Planned-only today: `/deliverables`, `/deliveries/{id}/link`, `/buyer/deliverables/{id}` (superseded by the evidence flow; see `docs/ARCHITECTURE.md`) and `/webhooks/paypal`.
`backend/tests/test_contracts.py` fails if the served routes differ from this table.

**Payments run against an in-process MOCK provider (`provider: "MOCK_PAYPAL"`, ids prefixed `MOCK-`) or the PayPal
SANDBOX test environment (`provider: "PAYPAL_SANDBOX"`).** Live PayPal is not implemented. `PAYPAL_MODE=disabled` makes
the invoice/payment routes return 503. `GET` payment routes are strictly read-only; only `POST .../payments/sync`
contacts the provider to learn payment state.

| Method | Path | Input | Output | Auth | Phase |
|---|---|---|---|---|---|
| GET | `/health` | — | status + integration modes | none | Step 1 implemented |
| GET | `/api/v1/capabilities` | — | available integrations | none | Step 1 implemented |
| POST | `/api/v1/auth/register` | email, password (12–128 chars), display_name | `{"status":"registered"}` (identical if the email exists) | none | Stage 2B implemented |
| POST | `/api/v1/auth/login` | email, password | seller, csrf_token, expires_at; sets session + CSRF cookies | none | Stage 2B implemented |
| GET | `/api/v1/auth/me` | — | seller, expires_at | session | Stage 2B implemented |
| POST | `/api/v1/auth/logout` | — | 204; revokes the session server-side | session + CSRF | Stage 2B implemented |
| POST | `/api/v1/projects` | title | `ProjectView` (owner from session) | session + CSRF | Stage 2B implemented |
| GET | `/api/v1/projects` | — | own projects | session | Stage 2B implemented |
| GET | `/api/v1/projects/{project_id}` | — | `ProjectDetail` (project + payment summary + derived read-only `delivery_status`) | owner | Stage 2B implemented |
| POST | `/api/v1/projects/{project_id}/scope-records` | kind, source_label, content | immutable `ScopeRecordView` with server-computed sha256 | owner + CSRF | Stage 2B implemented |
| GET | `/api/v1/projects/{project_id}/scope-records` | — | scope records, oldest first | owner | Stage 2B implemented |
| POST | `/api/v1/projects/{project_id}/invoices` | currency, amount_minor, recipient_email, optional replaces_payment_id; `Idempotency-Key` header | DRAFT `PaymentLink` (201 new, 200 replay); nobody is emailed | owner + CSRF | Stage 2C implemented |
| POST | `/api/v1/projects/{project_id}/payments/{payment_id}/send` | confirm_recipient_email | `PaymentLink` (invoice sent by the provider; explicit seller action) | owner + CSRF | Stage 2C implemented |
| GET | `/api/v1/projects/{project_id}/payments` | — | stored `PaymentLink[]`; read-only, no provider call | owner | Stage 2C implemented |
| POST | `/api/v1/projects/{project_id}/payments/sync` | — | `PaymentLink[]` reconciled from provider evidence fetched server to server | owner + CSRF | Stage 2C implemented |
| POST | `/api/v1/projects/{project_id}/payments/{payment_id}/resolve-review` | resolution (`ACCEPT_PROVIDER_EVIDENCE` \| `ABANDON_UNCREATED_INVOICE`), note (10–200 chars), `confirm_no_provider_invoice` (must be true for `ABANDON_UNCREATED_INVOICE`) | `PaymentLink`; the only way out of `NEEDS_REVIEW`; audited as `REVIEW_RESOLVED` | owner + CSRF | Stage 2C implemented |
| GET | `/api/v1/projects/{project_id}/payments/{payment_id}/events` | — | append-only `PaymentEventView[]` history | owner | Stage 2C implemented |
| GET | `/api/v1/projects/{project_id}/payment-instruction` | — | `PaymentInstructionState`: instruction in force (always `PENDING_VERIFICATION` in R1), pending changes, `HoldStatus` | owner | Stage R1 implemented |
| POST | `/api/v1/projects/{project_id}/payment-instruction` | recipient_email, currency, amount_minor, optional merchant_email (may only confirm the provider's merchant); `Idempotency-Key` header | `PaymentInstructionView` v1 (201 new, 200 replay; 409 `instruction_exists`, `merchant_evidence_missing`; 422 `merchant_mismatch`) | owner + CSRF | Stage R1 implemented |
| GET | `/api/v1/projects/{project_id}/payment-instruction/versions` | — | append-only `PaymentInstructionView[]` | owner | Stage R1 implemented |
| GET | `/api/v1/projects/{project_id}/payment-instruction/changes` | — | `InstructionChangeView[]` with `resolution` (null = pending) | owner | Stage R1 implemented |
| POST | `/api/v1/projects/{project_id}/payment-instruction/changes` | recipient_email, currency, amount_minor, optional merchant_email (only the provider's configured merchant is accepted), reason; `Idempotency-Key` header | `InstructionChangeView` (201/200); creating one **starts a HOLD**; 409 `change_pending` (also after a withdrawal), `instruction_required`; 422 `no_change`, `merchant_mismatch` | owner + CSRF | Stage R1 implemented |
| POST | `/api/v1/projects/{project_id}/payment-instruction/changes/{change_id}/withdraw` | optional note | `InstructionChangeView` with `resolution=WITHDRAWN`; recorded immutably and idempotent, but it **does not release the hold** (`holds_payment_operations` stays true) and the instruction in force is unchanged. There is no approve or release route; only a buyer-side verification (R2, below) releases it | owner + CSRF | Stage R1 implemented |
| GET | `/api/v1/projects/{project_id}/payment-hold` | — | `HoldStatus` (read-only) | owner | Stage R1 implemented |
| GET | `/api/v1/projects/{project_id}/buyer-authenticators` | — | `BuyerAuthenticatorView[]` (masked address, `ACTIVE`/`REVOKED`, `trust_basis=OPERATOR_ESTABLISHED`; never a secret). Read-only: there is **no seller route to enrol, replace, reset or reveal** an authenticator (operator CLI only) | owner | Stage R2 implemented |
| POST | `/api/v1/projects/{project_id}/merchant-ledger/imports` | `{csv}` (UTF-8, <=200 KB, <=500 rows; columns kind, invoice_number, currency, amount_minor, optional transaction_id, asserted_at, note, merchant_row_id) | `MerchantLedgerImportView` (201 new, 200 same file again). **All-or-nothing**: any invalid row gives 422 `ledger_invalid` with row/column details and records nothing. Every row is an `UNVERIFIED_CLAIM`; an invoice number that is unknown or belongs to another project is one indistinguishable error. Changes no status, verifies nothing, releases no hold | owner + CSRF | Stage R3 implemented |
| GET | `/api/v1/projects/{project_id}/merchant-ledger/imports` | `limit` (1-100, default 50), `offset` (0-100000) | `MerchantLedgerImportView[]`, stable order; `X-Total-Count` header; read-throttled (429) | owner | Stage R3 implemented |
| POST | `/api/v1/projects/{project_id}/payment-observations` | invoice_number, kind (PAYMENT/REFUND), currency, amount_minor, optional transaction_id, asserted_at, note, claim_id. Source and confidence are server-chosen; extra fields are rejected | `SellerObservationResult` (201 new, 200 retry of the same claim): a `SELLER_ENTRY` / `UNVERIFIED_CLAIM`; 422 `unknown_invoice_reference`; 409 `claim_id_conflict` (same claim_id, different content); 409 `observation_limit_reached` (20,000 stored claims per project; also for imports) | owner + CSRF | Stage R3 implemented |
| GET | `/api/v1/projects/{project_id}/payment-observations` | optional `source`, `payment_id`, `limit` (1-200, default 100), `offset` (0-100000) | `PaymentObservationView[]` ordered by server observation time then id, with an `X-Total-Count` header; read-throttled (429); `PROVIDER_API` rows are written only by reconciliation | owner | Stage R3 implemented |
| GET | `/api/v1/projects/{project_id}/observation-comparison` | `limit` (payments per page, 1-50, default 25), `offset` | `ObservationComparison` (one page of payments; `total_payments`): per payment the authoritative reconciliation status, provider vs claimed totals, `provider_evidence_complete` / `evidence_gap_count`, and source-cited findings (at most 20 citations each, `citations_total` gives the real count; at most the newest 2000 claims per payment are compared and provider observations are always loaded separately, so a flood of claims cannot push them out; `COMPARISON_INPUT_TRUNCATED` and `claims_truncated` say so, and absence-based findings are then withheld; at most 200 findings per payment), plus a `comparison_sha256` snapshot. Read-only, advisory, read-throttled | owner | Stage R3 implemented |
| GET | `/api/v1/projects/{project_id}/recovery-cases` | `limit` (payments per page, 1-50, default 25), `offset`, optional `category`, `severity` | `RecoveryCaseList`: advisory, evidence-cited recovery cases derived on read from stored observations, gaps, reconciliation and instruction records (nothing stored; no write route exists). Each case has `case_id`, `category`, `severity`, `confidence` + `confidence_limits`, `current_state`, `explanation`, `recommended_next_action` (for a human), `citations` (re-verified against the project), `advisory_only: true` and `prohibited_automated_actions`. Incomplete evidence yields `EVIDENCE_INCOMPLETE`, never a missing-payment conclusion. Read-throttled | owner | Stage R4 implemented |
| GET | `/api/v1/projects/{project_id}/recovery-cases/{case_id}` | — | `RecoveryCase` (404 for an unknown, outdated or another project's id; 422 for a malformed id) | owner | Stage R4 implemented |
| GET | `/api/v1/projects/{project_id}/state-recovery/divergences` | `credentials_configured`, `heartbeat_ok` | `DivergenceList`: divergences between provider payment state and the SIMULATED merchant store, plus the end-to-end health assessment. Read-throttled | owner | Stage S2 implemented |
| POST | `/api/v1/projects/{project_id}/state-recovery/plans` | `RecoveryPlanCreate` `{kind, payment_id?}` | `RecoveryPlanView`. 201 new version, 200 identical evidence (idempotent); 404 unknown payment, 409 `no_divergence`; CSRF, rate-limited | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/state-recovery/plans` | `limit` (1-100), `offset` | `RecoveryPlanList`. Read-throttled | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/state-recovery/plans/{plan_id}` | — | `RecoveryPlanView`: steps, evidence envelope, reasons, fingerprints, derived state, approval, execution (404 for another seller's or project's plan) | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/state-recovery/plans/{plan_id}/preview` | — | `RecoveryPreview`: dry run against fresh evidence; writes nothing, no provider call | owner | Stage S2 implemented |
| POST | `/api/v1/projects/{project_id}/state-recovery/plans/{plan_id}/approve` | `RecoveryPlanApprove` `{version, evidence_fingerprint}` | `RecoveryPlanView`. Idempotent for the same binding; 409 `approval_mismatch`/`plan_not_approvable`/`plan_superseded`/`plan_tampered`, each recorded; CSRF, rate-limited | owner | Stage S2 implemented |
| POST | `/api/v1/projects/{project_id}/state-recovery/plans/{plan_id}/execute` | — | `RecoveryExecutionResult` `{plan, executed_now}`. At most once; a repeat returns `executed_now=false`; 409 with a stable code when a guard refuses (recorded); CSRF, rate-limited | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/state-recovery/plans/{plan_id}/events` | — | `RecoveryEventList`: the append-only history | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/state-recovery/diagnosis` | `kind` (required), `payment_id` (all kinds but `FALSE_HEALTHY`), `credentials_configured`, `heartbeat_ok` | `DiagnosisResponse`: advisory, cited, read-only diagnosis of a current divergence before any plan exists; `mode` RULES_ONLY / RULES_AND_LLM, `llm_evaluation` NOT_RUN when no model is configured; 409 `no_divergence` / `unresolved_evidence`. Read-throttled | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/state-recovery/plans/{plan_id}/diagnosis` | — | `DiagnosisResponse` for a stored plan's evidence (never approves or changes the plan; 404 for another seller's plan). Read-throttled | owner | Stage S2 implemented |
| POST | `/api/v1/projects/{project_id}/state-recovery/log-intelligence` | `LogIntelligenceRequest` `{kind, payment_id?, requested_mode?, logs[1..100]{text<=4096}}` | `LogIntelligenceResponse`: advisory, read-only reading of submitted log lines next to the server-built evidence. A POST only because it carries a body; it writes nothing, calls no provider and changes no payment, order or plan. The server assigns scope, citations (`log:N`) and provenance (`untrusted_log`); the caller cannot supply ids, provenance, project or a model path (422). `requested_mode` defaults to `deterministic`; `stanza` uses the optional local model, else `effective_mode=deterministic` with a `fallback_reason` (200). Hypotheses are untrusted clues, exact candidates are unverified, `rules_diagnosis` is the existing one. 404 another seller's project or unknown payment, 409 `no_divergence`, 422 `invalid_log_evidence` (never echoes the text); CSRF, read-throttled | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/payee-registry` | — | `PayeeVersionView[]`: the current (latest) version of every payee in the project. Read-throttled | owner | Stage S2 implemented |
| POST | `/api/v1/projects/{project_id}/payee-registry` | `PayeeCreate` `{label, destination, note?}` | `PayeeVersionView` 201. SELLER_ASSERTED, append-only, chained; the seller's own record of where a counterparty says to pay, never provider/buyer evidence and never a verification. 409 `payee_exists`/`payee_limit`, 422 `invalid_destination`; CSRF; audited (`PAYEE_REGISTERED`, destination masked) | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/payee-registry/{payee_id}` | — | `PayeeView`: current version plus full history (newest first); `independently_verified` is always false. 404 for another seller's or project's payee | owner | Stage S2 implemented |
| POST | `/api/v1/projects/{project_id}/payee-registry/{payee_id}/versions` | `PayeeDestinationChange` `{destination, note?}` | `PayeeVersionView` 201: a NEW chained version (never an edit). Does not verify anything and never creates, clears or overrides a payment hold or touches the payment instruction. 409 `destination_unchanged`; CSRF; audited (`PAYEE_DESTINATION_CHANGED`) | owner | Stage S2 implemented |
| POST | `/api/v1/projects/{project_id}/payee-registry/screen` | `PayeeScreenRequest` `{payee_id?, destination?, amount_minor?, currency?, text?<=4096}` | `PayeeScreenResponse`: read-only advisory screen (a POST only because it carries a body; writes nothing, no provider call). Cited signals vs the registry and the instruction in force plus BEC wording hints and literal, unverified text extraction (null when not stated). Outcome is only `NO_SIGNALS_FOUND` or `REVIEW_RECOMMENDED`; it can never approve or clear. 404 unknown payee, 422 unsafe/oversize text (never echoed); CSRF, read-throttled | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/state-recovery/integration-health` | `credentials_configured`, `heartbeat_ok` | `IntegrationHealthResponse`: the authoritative cited health assessment of the SIMULATED integration; configuration alone is never TRANSACTION_VERIFIED. Read-throttled | owner | Stage S2 implemented |
| GET | `/api/v1/projects/{project_id}/unified-report` | `payments_limit` (1-50, default 25), `payments_offset` (0-100000) | deterministic JSON: scope/delivery, instruction integrity + authorization, claims vs provider observations, evidence gaps, recovery cases, citation check, truncation, `report_sha256` (see `docs/R5_UNIFIED_REPORT.md`) | owner | Stage R5 implemented |
| GET | `/api/v1/projects/{project_id}/unified-report/markdown` | same | the same report as Markdown (`text/markdown`), carrying the JSON `report_sha256` | owner | Stage R5 implemented |
| POST | `/api/v1/buyer/instruction-changes/challenge` | project_id, change_id, buyer_email | `InstructionChallengeResponse`: single-use challenge (5 min) plus the exact current/proposed values rendered by the server and `allowed_decisions` (a withdrawn change, or one proposed before the authenticator was enrolled, can only be rejected). Uniform 404 for anything not matching, for a locked-out authenticator and for an exhausted issue limit (no existence oracle; the signal is logged); 503 `verification_busy` (database contention, input-independent, hold unchanged); no seller cookie is read | public (rate limited) | Stage R2 implemented |
| POST | `/api/v1/buyer/instruction-changes/decide` | challenge_id, project_id, change_id, buyer_email, from_fingerprint, proposed_fingerprint, decision, 6-digit totp_code | `InstructionDecideResponse` (`APPLIED` creates the new instruction version whose fingerprint equals the proposed one, `REJECTED` leaves it unchanged; both release the hold atomically). Any failure: 400 `verification_failed` (uniform, hold unchanged); 503 `authenticator_unavailable` (key lost, fails closed; reachable only with a valid challenge), 503 `verification_busy` | public (rate limited) | Stage R2 implemented |
| POST | `/api/v1/projects/{project_id}/baselines` | source_scope_record_id (an ORIGINAL record), requirements[{key,title,description}] | immutable `BaselineView` v1 (409 `baseline_exists` afterwards) | owner + CSRF | Stage 3A implemented |
| GET | `/api/v1/projects/{project_id}/baselines` | — | all baselines with requirements, oldest first | owner | Stage 3A implemented |
| POST | `/api/v1/projects/{project_id}/scope-changes` | title, description, requirements[], optional supersedes_change_id | `ScopeChangeView` with status `PROPOSED`; changes no baseline | owner + CSRF | Stage 3A implemented |
| GET | `/api/v1/projects/{project_id}/scope-changes` | — | changes with their status | owner | Stage 3A implemented |
| GET | `/api/v1/projects/{project_id}/audit-events` | — | append-only `AuditEventView[]` | owner | Stage 3A implemented |
| POST | `/api/v1/projects/{project_id}/artifacts` | name, optional media_type, description, content_text (<=64 KB), declared_sha256, asserted_at, requirement_keys, supersedes_artifact_id | immutable `ArtifactView`; the server hashes content and stamps `ingested_at` (409 `duplicate_artifact`) | owner + CSRF | Stage 3B implemented |
| GET | `/api/v1/projects/{project_id}/artifacts` | — | all artifact versions, oldest first | owner | Stage 3B implemented |
| POST | `/api/v1/projects/{project_id}/artifacts/{artifact_id}/links` | requirement_keys | `ArtifactView` with the (idempotently extended) seller-asserted links | owner + CSRF | Stage 3B implemented |
| GET | `/api/v1/projects/{project_id}/timeline` | — | `TimelineItem[]` ordered by server recording time | owner | Stage 3B implemented |
| POST | `/api/v1/projects/{project_id}/deliverables` | file + version | deliverable ID, sha256 | owner | Gate 2 |
| POST | `/api/v1/deliveries/{deliverable_id}/link` | destination | short-lived buyer link created | owner | Gate 2 |
| POST | `/api/v1/projects/{project_id}/buyer-requests` | kind (`SCOPE_CHANGE_APPROVAL` \| `DELIVERY_REVIEW`), buyer_email, change_id or artifact_ids | `BuyerRequestView` (201); the link goes to the buyer's mailbox only, never to the seller | owner + CSRF | Stage 3C implemented |
| GET | `/api/v1/projects/{project_id}/buyer-requests` | — | requests with masked buyer address | owner | Stage 3C implemented |
| POST | `/api/v1/projects/{project_id}/buyer-requests/{request_id}/cancel` | — | cancelled `BuyerRequestView` | owner + CSRF | Stage 3C implemented |
| GET | `/api/v1/projects/{project_id}/buyer-decisions` | — | append-only `BuyerDecisionView[]` | owner | Stage 3C implemented |
| POST | `/api/v1/buyer/challenge` | token | 202; mails a one-time code (identical answer for any token) | public (link token) | Stage 3C implemented |
| POST | `/api/v1/buyer/verify` | token, 6-digit code | short-lived buyer session + what is being decided | public (link token + code) | Stage 3C implemented |
| POST | `/api/v1/buyer/decide` | session_token, decision, optional statement | `BuyerDecisionResponse`; the only way a change is approved | buyer session | Stage 3C implemented |
| GET | `/api/v1/buyer/deliverables/{deliverable_id}` | release token | deliverable bytes | verified buyer session | Gate 2 |
| POST | `/api/v1/projects/{project_id}/analysis` | — | `AnalysisRunView`: validated findings citing stored records (503 `ai_disabled`, 409 `baseline_required`, 502 `analysis_invalid`/`analysis_failed`) | owner | Stage 3D implemented |
| GET | `/api/v1/projects/{project_id}/analysis` | — | stored analysis runs | owner | Stage 3D implemented |
| GET | `/api/v1/projects/{project_id}/evidence-report` | — | deterministic JSON report (`report_sha256`); confirmed observations, seller claims and AI judgments kept apart; PDF not implemented | owner | Stage 3E implemented |
| GET | `/api/v1/projects/{project_id}/evidence-report/markdown` | — | the same report as Markdown (`text/markdown`) | owner | Stage 3E implemented |
| POST | `/api/v1/webhooks/paypal` | provider event | ack; async verification | signed webhook | Optional Gate 5 |

### Authentication, CSRF and ownership
- Sessions are server-side. The cookie `pem_session` (HttpOnly, SameSite `lax`/`strict`, `Secure` when `COOKIE_SECURE=true`) holds an
  opaque 256-bit token; only its SHA-256 is stored. Sessions expire after `SESSION_TTL_MINUTES` and are revoked on logout.
- Every mutating authenticated request needs header `X-CSRF-Token` equal to the `pem_csrf` cookie **and** to the token issued for that
  session at login. A present `Origin` header must be in `CORS_ORIGINS`. Failures return 403 (`csrf_failed`, `origin_not_allowed`).
- Passwords are hashed with Argon2id. Login is throttled per account and per IP, and registration per IP (429 `too_many_attempts`).
- A project that belongs to another seller returns **404**, identical to a project that does not exist. Request bodies never carry
  `seller_id`, `status`, or `provider_status`; unknown fields on invoice, scope and auth requests are rejected with 422.

### Canonical enums
`ProjectStatus`: `DRAFT | INVOICED | PAID | DELIVERED | DISPUTED` — only `DRAFT`, `INVOICED` and `PAID` are ever assigned (by payment reconciliation); `DELIVERED` and `DISPUTED` are **reserved** and unassigned
`DeliveryStatus` (derived, read-only, never stored or client-settable): `NONE | ACKNOWLEDGED_ONLY | ACCEPTED | DISPUTED` — from the buyer's latest verified delivery decision; unrelated to `ProjectStatus`
`InstructionProvenance`: `SELLER_ENTERED | INVOICE_REQUEST`
`HoldReason`: `UNVERIFIED_INSTRUCTION_CHANGE | INSTRUCTION_INTEGRITY_FAILURE | MERCHANT_MISMATCH | PROVIDER_MISMATCH | INSTRUCTION_REQUIRED`
`ChangeResolution`: `WITHDRAWN`
`ScopeKind`: `ORIGINAL | CHANGE_REQUEST | APPROVAL | DELIVERY_NOTE`
`PaymentProviderStatus`: `UNVERIFIED | PENDING | PARTIALLY_PAID | PAID | PARTIALLY_REFUNDED | REFUNDED | CANCELLED | NEEDS_REVIEW`
`PaymentProviderName`: `MOCK_PAYPAL | PAYPAL_SANDBOX | PAYPAL`
`DeliveryEventType`: `LINK_SENT | LINK_ACCESSED | FILE_RELEASED | BUYER_ACKNOWLEDGED` — **unused scaffolding**, produced by nothing (see `docs/ARCHITECTURE.md`)
`EvidenceStatus`: `SUPPORTED | AMBIGUOUS | UNSUPPORTED` — legacy shape; the served analysis uses `FindingStatus`
`BuyerRequestKind`: `SCOPE_CHANGE_APPROVAL | DELIVERY_REVIEW`
`BuyerDecision`: `APPROVE_CHANGE | REJECT_CHANGE | ACKNOWLEDGE_DELIVERY | ACCEPT_DELIVERY | DISPUTE_DELIVERY`
`FindingStatus`: `SUPPORTED | UNSUPPORTED | AMBIGUOUS` · `FindingCategory`: `REQUIREMENT_COVERAGE | SCOPE_MISMATCH | CHANGE_AUTHORIZATION | MISSING_EVIDENCE | CONFLICTING_EVIDENCE | DELIVERY_ACCEPTANCE`

### Response conventions
- JSON objects include an `id` and ISO 8601 UTC timestamps when persisted.
- Errors: `{ "error": { "code": "...", "message": "...", "request_id": "..." } }` (implemented; validation errors add `details` and never echo input).
- Money is integer minor units, e.g. USD 800.00 is `amount_minor=80000`.
- Never accept `provider_status=PAID` from the client. Only a verified PayPal adapter can set it. Request bodies carry no `provider_status` and no `seller_id`; the owner comes from the authenticated session.
- Source of truth: Pydantic models/enums in `backend/app/schemas.py`. `contracts/*.schema.json` are generated by `scripts/export_contracts.py`; `backend/tests/test_contracts.py` fails on drift between code, schemas, this document, and database CHECK constraints.
- Timestamps are timezone-aware UTC (`AwareDatetime`); the database layer normalizes to UTC on every backend.
- A `SUPPORTED` finding must cite at least one source record; `human_review_required` is always `true`.
- Invoice creation requires an `Idempotency-Key` header (8–64 chars `[A-Za-z0-9_-]`), scoped to the project. Same key and payload replays the stored invoice (200); same key with a different payload returns 409 `idempotency_key_conflict`. A project holds one active (non-cancelled) invoice (409 `project_already_invoiced`).
- Financial state changes only inside the server-side reconciliation path (`POST .../payments/sync`, plus creation/send which record provider-confirmed lifecycle). `PAID` requires provider evidence matching the stored invoice id, currency and total, with PayPal-processed payments summing to the total. An invoice merely `MARKED_AS_PAID` (external) is never treated as PayPal payment and goes to `NEEDS_REVIEW`. Inconsistent or unexpected evidence fails closed to `NEEDS_REVIEW`; verified states are never silently downgraded.
- A verified full refund moves the payment to `REFUNDED`; a partial refund to `PARTIALLY_REFUNDED`. Refunds never change the project's `PAID` status or imply that completed work is reversed. Payment rows and `payment_events` are never deleted.
- A cancelled or fully refunded invoice may be replaced only by an explicit authenticated `POST .../invoices` with `replaces_payment_id`; at most one active invoice exists per project.
- Invoices are created as DRAFT. Only `POST .../send` (with `confirm_recipient_email`) emails a recipient. In sandbox mode recipients must be on `PAYPAL_SANDBOX_RECIPIENT_ALLOWLIST` and an ORIGINAL scope record must exist; the invoice records `scope_record_id`.
- Interrupted provider operations are persisted (`operation_state`): resume a create with the same `Idempotency-Key`; resolve an uncertain send via sync.
- Scope records are append-only: no update or delete route exists, and the ORM and (on SQLite) database triggers refuse modification.
- Error codes: `not_authenticated` 401, `invalid_credentials` 401, `csrf_failed` 403, `origin_not_allowed` 403, `not_found` 404,
  `idempotency_key_conflict` 409, `project_already_invoiced` 409, `validation_error` 422, `too_many_attempts` 429,
  `idempotency_key_invalid` 400 (checked after body validation, so an invalid body yields 422 first), `provider_unavailable` 502,
  `provider_response_mismatch` 502, `provider_auth_failed` 502, `provider_outcome_uncertain` 502, `provider_rate_limited` 503, `provider_rejected` 422,
  `replacement_required` 409, `replaced_payment_not_found` 404, `invoice_not_sendable` 409, `operation_in_progress` 409, `sync_conflict` 409,
  `recipient_not_allowed` 422, `recipient_confirmation_mismatch` 422, `original_scope_required` 409, `unsupported_currency` 422, `provider_mismatch` 409, `idempotency_key_failed` 409, `not_in_review` 409, `review_required` 409, `review_not_resolved` 409, `abandon_confirmation_required` 422, `review_evidence_inconsistent` 409, `abandon_not_applicable` 409, `no_provider_invoice` 409, `provider_sync_failed` 502, `replacement_blocked_review` 409, `too_many_attempts` 429 (also per-seller on create/send/sync, `PAYMENT_OPS_PER_MINUTE`), `payments_disabled` 503 (both invoice and payments routes), `internal_error` 500, `payload_too_large` 413 (declared Content-Length over `MAX_BODY_BYTES`).
- `GET /api/v1/projects/{project_id}/payments` performs no provider call and no write.
- `POST .../payments/sync` persists everything it could reconcile and then, if any payment could not be synchronized, responds with an error (`provider_unavailable` 502, `provider_auth_failed` 502, `provider_rate_limited` 503 + `Retry-After`, `provider_sync_failed` 502, `sync_conflict` 409) whose `details` list `{payment_id, outcome}`; the stored state of those payments is unchanged and visible via `GET .../payments`. A 200 means every payment was synchronized.
- `NEEDS_REVIEW` never clears automatically. Consistent evidence only adds the note `EVIDENCE_NOW_CONSISTENT_AWAITING_RESOLUTION`; the seller must call `resolve-review`. `ACCEPT_PROVIDER_EVIDENCE` re-fetches the invoice and is refused (409) while any anomaly remains; `ABANDON_UNCREATED_INVOICE` applies only when no provider invoice id exists.
- `Origin` and CSRF checks apply to mutating methods only. List endpoints are unpaginated in this stage.
- Resource IDs alone confer no rights: ownership checked server-side on every request.
- AI findings expose `id`, `category`, `claim`, `status`, `confidence`, `basis`, `source_refs`, `explanation` and `human_review_required` (always true). Every citation must resolve to a record of the same project, and only coverage, authorization and acceptance findings may be `SUPPORTED`, each with a relationship check.
- A buyer decision proves control of a mailbox at that time, not legal identity. Seller notes, artifact access and seller-side acknowledgments are never buyer approval.
- Analysis (`AI_MODE=demo`) and buyer e-mail (`BUYER_MAIL_MODE=fake`) are deterministic offline fakes, rejected when `APP_ENV=production`; the analysis provider is a keyword heuristic labelled NOT PRODUCTION.
- `DeliveryStatus` is derived (see enums); delivery decisions never change `ProjectStatus` or payment status.

### Sample analysis response (illustrative fixture, older shape; see `AnalysisRunView` for the served one)
```json
{
  "project_id": "project-demo-1",
  "findings": [
    {"claim": "A blog page was added to agreed scope", "status": "AMBIGUOUS", "source_refs": ["source-4", "source-5"], "explanation": "Request and informal reply exist, but explicit approval context is unclear."},
    {"claim": "Buyer formally accepted delivery", "status": "UNSUPPORTED", "source_refs": [], "explanation": "No explicit acceptance event or verifiable acceptance message."}
  ],
  "human_review_required": true
}
```

### Payment instruction integrity (R1)
- An instruction (merchant/payee, buyer recipient, currency, exact minor-unit amount, agreement context) is append-only and versioned, with a SHA-256 fingerprint and a hash chain. Every instruction is `PENDING_VERIFICATION`: seller-entered, **not independently verified**. Independent buyer-side verification (R2) is not built and never simulated.
- A HOLD blocks only operations performed through this service: invoice creation, invoice sending and accepting provider evidence in `resolve-review` (409 `payment_hold_active`). It never freezes or redirects funds at PayPal, and `payments/sync` still records what the provider reports. An invoice request that deviates from the recorded instruction is refused with 409 `instruction_mismatch`.
- Error `details` carry reason codes or field names only, never addresses or amounts.
- `PAYMENT_INSTRUCTION_REQUIRED=true` makes an instruction mandatory before invoicing; otherwise the first invoice request records one implicitly (`INVOICE_REQUEST`).
- **Withdrawal is history, not release.** A withdrawn change keeps the project held until a buyer-side verification with an enrolled second channel exists (R2); R1 has none, so such a project cannot create or send invoices and cannot take a second proposal. Reconciliation (`payments/sync`) and reads still work.
- **In-flight rule.** An invoice create that was durably reserved (committed, bound to the exact instruction fingerprint) before a hold was established may complete or resume against exactly that instruction; a hold never changes its recipient, amount or currency. The linearization point is the commit of the reservation under the project write lock. A send starts only if no unreleased change exists at the moment its claim commits. A provider call already in progress when a hold commits is not interrupted.
- **Schema adoption.** A database created before migrations is adopted only by `alembic stamp 0001` and only if it is exactly the R0 schema; otherwise the command stops with nothing changed (see `docs/ARCHITECTURE.md`).
