# Architectural decisions — frozen Step 1

1. Temporary repository name: `paypal-evidence-mvp`. No final brand selected.
2. Primary user: direct-contract Indian software freelancer serving overseas PayPal buyers.
3. P0 workflow: PayPal invoice/payment -> project -> authenticated delivery -> AI scope analysis -> human-reviewed source-traceable report.
4. Scope data must distinguish `ORIGINAL`, `CHANGE_REQUEST`, `APPROVAL`, `DELIVERY_NOTE`; an informal request is never automatically a contract variation.
5. Use `amount_minor` as integer and ISO currency code, never floating-point currency values.
6. Payment provider status is unverified unless confirmed server-to-server; do not trust browser claims.
7. Buyer email OTP would establish email control only; does not prove payer identity or legal acceptance.
8. Event types have separate semantics. `LINK_ACCESSED` != `FILE_RELEASED` != `BUYER_ACKNOWLEDGED`.
9. The LLM analyzes unstructured documents but cannot invent source IDs, assert seller protection eligibility, or approve claims itself.
10. Public APIs are NOT implemented until seller auth, tenant authorization, rate limits, and buyer-token verification are added.
11. No credential access in Step 1; integration modes stay disabled.
12. P1 webhook automation only after P0; automated PayPal dispute submission excluded.
