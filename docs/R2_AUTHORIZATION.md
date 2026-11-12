# R2 independent authorization of payment-instruction changes

Status: design fixed before implementation; guarded by `backend/tests/test_instruction_verification.py` and `test_docs_consistency.py`.

## 1. What R2 protects, and from whom
A payment instruction (payee, buyer recipient, currency, exact amount) can only be *changed* by a seller proposal that the buyer-side party approves with a
second channel the seller does not control. The party protected is the **buyer named as `recipient_email` of the instruction in force**
("the approving actor"). The threat is a seller account (or session) that proposes "pay a different account / amount".

## 2. Enrollment trust model (read this before relying on anything)
* **An e-mail OTP alone never establishes who the buyer is.** It proves control of a mailbox at one moment, and a seller who controls the
  mailbox can pass it. R2 therefore does not use the buyer mailbox flow as identity for instruction changes at all.
* **Authenticator enrollment is OPERATOR-ESTABLISHED, DEMO-ONLY.** In this offline MVP no mechanism can prove a real-world identity. An authenticator
  (a TOTP secret) is created by the *operator* on the server host with `scripts/enroll_buyer_authenticator.py`, which needs the server's database and
  encryption key. The secret is shown once, to the operator, who must hand it to the buyer over a channel Caligo cannot see. Every authenticator is
  labelled `trust_basis = OPERATOR_ESTABLISHED`. **That label is not verified real-world identity** and nothing in the API or reports may call it that.
* **The seller has no enrollment power.** There is no seller route that creates, replaces, resets, rotates or reveals an authenticator. The seller
  can read that one exists (masked address, status, trust basis) and nothing else. If the seller and the operator are the same person (typical in a
  one-person demo), the second channel is *not* independent; the status text says so.
* **Pre-enrolled.** An approval is accepted only from an authenticator enrolled **before** the change was proposed, so enrolling after proposing
  (substitution) cannot approve it.
* **Replacement is operator-only, explicit and audited:** the operator revokes the old authenticator (append-only revocation) and enrols a new one.
  A replacement can **reject** an older pending change (this releases the hold without changing the instruction, and is the key-loss recovery
  path) but can never **approve** a change proposed before it was enrolled.

## 3. What a verification is bound to
A buyer first requests a **challenge** for (project, change, buyer address). The server stores, and the decision must echo:
project id, change id, previous (`from`) instruction fingerprint, proposed instruction fingerprint, the approving actor's address (the `recipient_email`
of the instruction in force), the authenticator id, and an expiry (5 minutes). The challenge is single-use. The decision carries the challenge id
and a current TOTP code (RFC 6238: HMAC-SHA1, 6 digits, 30 s, +/-1 step). Everything must match the stored change **at decision time** or the decision fails
and the hold stays active.

## 4. Replay, guessing, expiry
* A TOTP step is consumed once per authenticator (`totp_uses`, unique); later or equal steps are rejected, so a code cannot be reused even inside its window.
* Failed attempts are charged to the challenge **before** the code is checked (5 per challenge). Guessing is also bounded **per authenticator** across challenges (10 wrong attempts per 15 minutes locks it out), challenges are limited to 10 per hour, and an open unexpired challenge is reused so repeated requests cannot burn the budget.
* Expired, consumed, unknown or mismatching challenges fail with one uniform, non-informative error.
* Public buyer routes are rate limited per client address.

## 5. Atomic release
Approving writes, in **one transaction under the project write lock**: the verification record, the new instruction version (its fingerprint equals the
proposed fingerprint the buyer saw), the consumed challenge, the consumed TOTP step and the audit event. Rejecting writes the verification record
(no new version). Any failure rolls everything back, so a hold is never released by a half-finished verification.
**Only a row in `instruction_change_verifications` releases a hold.** A seller withdrawal (R1) remains history only: a withdrawn change can be *rejected* by
the buyer-side authenticator (to release the hold) but can never be approved or applied.

## 6. Secrets
TOTP secrets are encrypted at rest with AES-256-GCM. The associated data binds the ciphertext to its authenticator id, project and buyer address, so a
ciphertext copied to another row will not decrypt. Keys come from the environment only (`AUTHENTICATOR_KEYS`, `AUTHENTICATOR_ACTIVE_KEY_ID`),
are held as `SecretStr`, are never logged, returned or committed, and support rotation (older key ids still decrypt). **Key loss** makes verification
fail closed (503, hold stays) until the operator restores the key or revokes and re-enrols; no secret can be recovered without the key.

## 7. Remaining trust limitations (stated plainly)
1. Enrollment is operator-established; real-world identity of the buyer is not proven by Caligo.
2. If seller and operator are the same person, the second channel is not independent.
3. The operator can read the secret at enrollment and holds the key; a hostile operator or database-plus-key theft defeats the control.
4. **Version 1 of every instruction is NOT independently verified, and stays `PENDING_VERIFICATION` forever, even after later changes are authorized.** The chain root was typed by the seller; R2 only proves that a buyer-side authenticator approved each *subsequent* change. Version N>1 is `VERIFIED_BY_ENROLLED_SECOND_CHANNEL`, which means "that change was approved with an operator-established second channel", never "the whole lineage is verified".
5. A buyer address that the seller controls (seller-chosen `recipient_email`) can be enrolled by an operator who trusts the seller; the operator must enrol the real buyer.
6. TOTP is phishable if the buyer is tricked into reading a code to someone else; the challenge shows the exact before/after values, but a human must read them.
7. Throttles that are in-memory are per process; the durable challenge limits and attempt budgets are database-backed.
8. SQLite triggers are the only database-level append-only protection; Postgres is not supported.
9. A withdrawn or pending change keeps the hold until a buyer-side authenticator rejects or applies it. If no authenticator is enrolled or its key is lost, the project stays held. Runbook: revoke (if any), enrol the buyer, the buyer rejects. There is no other release.
10. The operator CLI is protected only by host access; the OS user name is recorded in the audit event, but the audit actor is `SYSTEM`.
11. The challenge table is mutable by design (`failed_attempts`, `consumed_at`); a database writer could un-consume a challenge, but the unique TOTP-step and verification constraints still prevent a second release.
12. **Seller who is also the operator:** nothing in Caligo can detect this. Such a deployment still produces `VERIFIED_BY_ENROLLED_SECOND_CHANNEL` with `trust_basis=OPERATOR_ESTABLISHED`, so every API note, the buyer-facing challenge and the CLI say plainly that this is not independence from the seller and not verified identity. Nothing labels it `INDEPENDENT`.

## 8. Enumeration and operational signals
A caller who knows (project id, change id, buyer address) learns whether an authenticator exists only from receiving a challenge, which is the function of the endpoint.
Locked-out authenticators, an exhausted challenge budget, unknown projects/changes/addresses and revoked authenticators all give the same 404; the operational
signal is a server log line carrying only the authenticator id. `503 authenticator_unavailable` is reachable only with a valid, unexpired challenge id.
`503 verification_busy` (database contention) does not depend on any input and leaves the hold unchanged.

## 9. Known availability and guessing limits (accepted, documented)
* **Denial of service by the seller.** The seller knows (project, change, buyer address), which is all the public routes require. By burning wrong attempts
  (10 per 15 minutes per authenticator) or the challenge budget the seller can delay the buyer's approve/reject. This is fail-safe (the hold stays) but it is
  a nuisance; the lockout is deliberately not shortened and there is no public unlock. An operator-visible alert/unlock tool is future work.
* **Online guessing.** A 6-digit code with a +/-1 step window is about 3 in 10^6 per guess; the per-authenticator lockout caps sustained guessing at roughly 40
  guesses an hour, which is small but not zero over weeks. A cumulative cap per change that requires operator unlock is future work. The per-IP throttle is
  in-memory and per process.
* A successful decision credits back its own pre-charged attempt; a database error after the pre-charge still spends one attempt.
* The lockout check and the attempt charge are not one atomic statement; SQLite write serialisation makes this safe, a different database would need a re-check.

