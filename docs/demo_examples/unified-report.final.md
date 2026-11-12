# Unified evidence report: SYNTHETIC DEMO: Acme website build

Project: `783eed50-91a9-462b-9032-1f88f23174d9` · report version R5.1
Report SHA-256: `cb7f81a6ee5cfbd452bb4afe94d33144a71e9329170a7ac56c81373e8108e06c`

## How to read this report

Every statement carries an evidence class. They are never interchangeable.

- **PROVIDER\_OBSERVATION**: What the payment provider returned to Caligo's last sync, stored immutably. It is not independently audited and is only as complete as the sync.
- **SERVER\_RECORDED**: A fact Caligo's server recorded itself (a hash, a server time, a hash-chained instruction version, a mailbox-verified buyer action).
- **SELLER\_CLAIM**: Asserted by the seller or in a seller-supplied file. Nothing independent confirms it; it is never proof of a payment or approval.
- **OPERATOR\_ESTABLISHED\_VERIFICATION**: A check by an authenticator an OPERATOR created on the server host. It shows that whoever held that authenticator answered; it does NOT prove the real-world identity of any buyer and is not independent of the seller if the seller is also the operator.
- **AI\_INTERPRETATION**: An opinion of an analysis provider: labelled, schema-validated, never a fact, and always needs human review.
- **DERIVED\_ADVISORY**: Deterministic advice derived from the records above (recovery cases). It recommends human checks and never acts.

## Summary

- Payments on this page: 1 of 1
- Payments with incomplete provider evidence: none
- Recovery cases on this page: 2 {'CONFLICT': 1, 'WARNING': 1, 'INFO': 0}
- Delivery status: ACKNOWLEDGED\_ONLY; unresolved scope/delivery items: 1
- Instruction versions: 2; hash chain intact: True; payment hold active: False
- Authorization records: 2 (0 inconsistent)
- All citations resolved: True
- Where evidence is incomplete, conflicting or unverified the report says so and does not resolve it.

## Scope and delivery

Stage 3 evidence report hash: `c0774f4738cd0ef43bcd10d33ba2c15b8cc470ece7ead8bdd93e894cc2515457`

### Agreement versions

- Version 1 (ORIGINAL), hash `c6d1749cf2e888e78cd51042c05a64e6dc38bccbab79955696ff1d5703843169`, 3 requirements

### Changes

_None._

### Buyer evidence (mailbox-verified; not legal identity)

A buyer decision shows that whoever controlled the verified mailbox made it at that time. It is not legal identity verification, and anyone who controls that mailbox could have made it.
Delivery status: **ACKNOWLEDGED\_ONLY**

- 2026-11-10T17:35:15.481173+00:00 ACKNOWLEDGE\_DELIVERY by d\*\*\*&#64;example.org

### Seller claims (unconfirmed)

- The seller states that 'home.html' v1 relates to requirement R1.
- The seller states that 'contact.php' v1 relates to requirement R2.
- The seller states that 'pricing.html' v1 relates to requirement R3.

### AI interpretations (never fact)

_No analysis has been run._

### Unresolved

- **DELIVERY\_NOT\_ACCEPTED**: The buyer acknowledged receipt only; acceptance was not recorded.

## Payment instruction integrity

Hash chain intact: **True** (2 versions)
Hold active: **False** . A hold blocks operations through Caligo only; it never freezes funds held by PayPal. Only buyer-side verification releases it.

- v1 `70d0e1780b28990a163610317a2e83cbb5b5eedb59ec29da16ddfa46b3386569` (SELLER\_ENTERED; PENDING\_VERIFICATION; class SELLER\_CLAIM) 120000 USD minor units to demo-buyer&#64;example.org; fingerprint recomputes: True
- v2 `a0ad14e08632100dc4b20a82ce68f5698b938a956e9ec109fc200862a0fb445f` (SELLER\_ENTERED; VERIFIED\_BY\_ENROLLED\_SECOND\_CHANNEL; class OPERATOR\_ESTABLISHED\_VERIFICATION) 125000 USD minor units to demo-buyer&#64;example.org; fingerprint recomputes: True

### Change requests

- `0015b111-f5a3-474c-ae98-7a5bf9df4e85` REJECTED: 120000 USD to pay-here-now&#64;example.net; seller's stated reason (claim): URGENT: our bank details changed, please pay to this new address today.
- `0b4b559d-e8fc-4472-809e-5b2db46119fa` APPLIED: 125000 USD to demo-buyer&#64;example.org; seller's stated reason (claim): Agreed scope increase (blog), see approved change.

## Buyer-side authorization records

> A check by an authenticator an OPERATOR created on the server host. It shows that whoever held that authenticator answered; it does NOT prove the real-world identity of any buyer and is not independent of the seller if the seller is also the operator.

- 2026-11-10T17:35:15.940595+00:00 REJECTED by d\*\*\*&#64;example.org via OPERATOR\_ESTABLISHED - label REJECTED\_BY\_ENROLLED\_SECOND\_CHANNEL; real-world identity verified: **False**; independent of seller: NOT\_ESTABLISHED; record consistent: True 
- 2026-11-10T17:35:16.030542+00:00 APPLIED by d\*\*\*&#64;example.org via OPERATOR\_ESTABLISHED - label VERIFIED\_BY\_ENROLLED\_SECOND\_CHANNEL; real-world identity verified: **False**; independent of seller: NOT\_ESTABLISHED; record consistent: True 

### Enrolled authenticators

- `9030070e-9c2f-4b28-af78-003dcd15ce4a` d\*\*\*&#64;example.org ACTIVE (OPERATOR\_ESTABLISHED)

## Payments: seller claims vs provider observations

Payment status is decided only by provider reconciliation. Merchant-ledger claims are shown beside provider evidence for comparison; they never confirm a payment, change a status or release a hold.

Page: offset 0, limit 25, 1 of 1 payments

- `f9637ef1-0163-4471-b4f3-c3750436911d` PEM-B76ECFABAF1A332008D9 (SIMULATED): reconciliation status **PAID**; provider observed paid 120000 (complete: True, gaps: 0); seller claimed 105000 (unverified); **CLAIMED\_TOTAL\_DIFFERS\_FROM\_PROVIDER**

## Provider evidence gaps

A gap means part of what the provider reported could not be stored. Absence of evidence is then not absence of payment.
Total 0, shown 0

_None._

## Recovery cases (advisory only)

Cases hash `9a8c28ac8746b9cabe4284409b3a1bf90b1c2a3ed81304c2b0ac772973778e2b`. These recommend human checks; nothing here acts.

### CONFLICT AMOUNT\_OR\_CURRENCY\_MISMATCH (`rc_f8bd3b5625f3a4c74db7aa7d`)

Confidence: CLAIM\_VS\_PROVIDER; evidence complete: True

A record disagrees with another on amount or currency (a claim against the provider's record or the invoice, or the provider's own totals). Details: The claim says 100000 minor units; the provider reported 120000 for the same transaction.; Claimed payments total 105000; PayPal payments the provider reported total 120000.

**Recommended human action:** Compare the records with the transaction in your PayPal dashboard and correct whichever is wrong. Contact the buyer only through a channel you already trust.

Citations: OBSERVATION:`35777fd1-cf89-4ae4-be4a-696fdfa66f13`, OBSERVATION:`69fe2706-1033-45df-acd3-a6fce745b2d7`, OBSERVATION:`d0db3ecc-1500-4729-8dd5-ab4df49de914`

Never automated: Issue, approve or request a refund; Capture, transfer or move funds; Cancel, void, send or modify a PayPal invoice; Mark a payment as paid or verified because of a merchant or seller claim; Release, override or bypass a payment-instruction hold; Edit or delete stored observations, gaps or history

### WARNING CLAIM\_NOT\_CORROBORATED (`rc_ee1fb28ae6d4bb175dbf1cbe`)

Confidence: CLAIM\_VS\_PROVIDER; evidence complete: True

A merchant or seller claim cannot be matched to the provider's stored evidence for this payment (the transaction is not in it, or the claim carries no transaction reference). Details: The provider's evidence for this invoice does not contain this transaction.

**Recommended human action:** Check the transaction reference against your PayPal dashboard, then correct the claim or sync again later. Do not treat the claim as a received payment.

Citations: OBSERVATION:`1a37fb89-3f9c-447d-8c90-63d9fa83bb33`, OBSERVATION:`d0db3ecc-1500-4729-8dd5-ab4df49de914`

Never automated: Issue, approve or request a refund; Capture, transfer or move funds; Cancel, void, send or modify a PayPal invoice; Mark a payment as paid or verified because of a merchant or seller claim; Release, override or bypass a payment-instruction hold; Edit or delete stored observations, gaps or history

## Citation check

Distinct citations checked: 13; all resolved: **True**


## Truncation

- payments\_page: {'total\_payments': 1, 'offset': 0, 'limit': 25, 'returned': 1, 'more': False}
- evidence\_gaps\_truncated: False
- instruction\_versions\_truncated: False
- instruction\_changes\_truncated: False
- authenticators\_truncated: False
- authorizations\_truncated: False

## Limitations

- This report summarises stored records for a human reviewer. It is not legal advice and it decides no dispute.
- Payment state is decided only by provider reconciliation. This report shows conflicts and gaps beside that state and never resolves them.
- A verification by an operator-established authenticator is not proof of any person's real-world identity.
- Provider observations carry no provider timestamps, a transaction that disappears from a later provider reading is not detected, and there is no webhook ingestion.
- Evidence gaps are permanent: a payment that ever had one stays marked incomplete, and absence of evidence is then never read as absence of payment.
- Scope and delivery sections reuse the Stage 3 evidence report and are bounded only by the limits of that report.

