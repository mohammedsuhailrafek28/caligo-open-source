"""Fixed, deterministic wording for R4 recovery cases (no model output, no invented evidence). Every recommended action is a step for a
HUMAN; none is performed by Caligo. `tests/test_recovery_cases.py` checks that every reconciliation anomaly code in the source is mapped
and that no action text instructs an automated financial step."""
import re
from dataclasses import dataclass

from app.schemas import FindingSeverity as Sev, RecoveryCategory as C, RecoveryConfidence as Conf


@dataclass(frozen=True)
class Entry:
    category: C | None  # None = informational note, no case
    severity: Sev
    explanation: str
    action: str


# category templates for cases built from R3 findings: (severity, confidence, explanation, action, confidence limits)
CATEGORY_TEXT: dict[C, tuple[Sev, Conf, str, str, tuple[str, ...]]] = {
    C.LEDGER_MISSING_PROVIDER_PAYMENT: (
        Sev.INFO, Conf.PROVIDER_EVIDENCE,
        "The payment provider reported a PayPal transaction that no merchant-ledger claim mentions.",
        "Check your ledger for this transaction and add it if it is genuine. Confirm the payment in your PayPal dashboard rather than in Caligo.",
        ("The ledger is only your own record; a missing ledger line does not make the provider payment wrong.",)),
    C.CLAIM_NOT_CORROBORATED: (
        Sev.WARNING, Conf.CLAIM_VS_PROVIDER,
        "A merchant or seller claim cannot be matched to the provider's stored evidence for this payment (the transaction is not in it, "
        "or the claim carries no transaction reference).",
        "Check the transaction reference against your PayPal dashboard, then correct the claim or sync again later. "
        "Do not treat the claim as a received payment.",
        ("The claim is unverified.", "Provider evidence is a snapshot of what Caligo's last sync saw.")),
    C.AWAITING_PROVIDER_SYNC: (
        Sev.INFO, Conf.UNVERIFIED_CLAIM_ONLY,
        "Claims exist for this payment but the provider has not been synchronized, so there is nothing to compare them with yet. "
        "This says nothing about whether a payment was made.",
        "Run a provider sync for the project, then review the comparison again.",
        ("No provider evidence has been stored yet.",)),
    C.AMOUNT_OR_CURRENCY_MISMATCH: (
        Sev.CONFLICT, Conf.CLAIM_VS_PROVIDER,
        "A record disagrees with another on amount or currency (a claim against the provider's record or the invoice, or the provider's own totals).",
        "Compare the records with the transaction in your PayPal dashboard and correct whichever is wrong. "
        "Contact the buyer only through a channel you already trust.",
        ("Claims are unverified; the provider's record is what reconciliation judges.",)),
    C.DUPLICATE_REFERENCE: (
        Sev.WARNING, Conf.UNVERIFIED_CLAIM_ONLY,
        "The same transaction reference appears more than once, or conflicting records name the same transaction.",
        "Check whether this is one payment recorded twice or two payments, using your PayPal dashboard; then correct the ledger.",
        ("Identical-looking claims from different submissions are kept separately and may be one real payment.",)),
    C.PROVIDER_STATE_REGRESSION: (
        Sev.CONFLICT, Conf.PROVIDER_EVIDENCE,
        "The provider reported this invoice or one of its transactions differently at different times (for example moving back to an earlier state).",
        "Open the invoice in your PayPal dashboard and review its history before relying on its status. Caligo changes nothing.",
        ("Caligo only sees what each sync returned, not the provider's full audit trail.",)),
    C.EVIDENCE_INCOMPLETE: (
        Sev.WARNING, Conf.INCOMPLETE_EVIDENCE,
        "Some of what the provider reported could not be stored, or older observations were left out of the comparison. "
        "Caligo therefore cannot tell whether a payment is missing.",
        "Review the invoice and its transactions directly in your PayPal dashboard. Treat the stored comparison as partial.",
        ("A missing observation is not evidence that a payment did not happen.",)),
    C.EXTERNAL_PAYMENT: (
        Sev.WARNING, Conf.PROVIDER_EVIDENCE,
        "The provider lists a payment that was recorded outside PayPal (for example marked as paid by hand). It is not proof that PayPal received money.",
        "Confirm in your own bank or PayPal balance that money actually arrived before treating the invoice as paid.",
        ("An externally recorded payment can be entered by the seller.",)),
    C.CLAIM_TIMING_ANOMALY: (
        Sev.INFO, Conf.UNVERIFIED_CLAIM_ONLY,
        "Claim timestamps look odd (in the future, recorded long after the time they assert, or out of order).",
        "Check the dates in your ledger export.", ("Claim timestamps are the seller's own.",)),
    C.RECONCILIATION_ANOMALY: (
        Sev.WARNING, Conf.RECONCILIATION_RECORD,
        "Reconciliation recorded a note for this payment.",
        "Open the invoice in your PayPal dashboard and compare it with the stored payment.",
        ("The note reflects what the provider last returned, or failed to return, to Caligo's sync; it is not an audit of the provider.",)),
    C.INSTRUCTION_INTEGRITY: (
        Sev.CONFLICT, Conf.INSTRUCTION_RECORD,
        "The stored payment instruction does not match this payment or its hash chain.",
        "Stop relying on this payment and contact the operator; Caligo will not release this automatically.",
        ("This rests on Caligo's own instruction records.",)),
    C.INSTRUCTION_HOLD: (
        Sev.WARNING, Conf.INSTRUCTION_RECORD, "A payment-instruction hold is active.",
        "Review the reasons below.", ("A hold blocks operations through Caligo only; it never freezes PayPal funds.",)),
    C.PENDING_BUYER_AUTHORIZATION: (
        Sev.WARNING, Conf.INSTRUCTION_RECORD, "A change is waiting for buyer-side verification.",
        "Ask the buyer to approve it through their enrolled authenticator.",
        ("A buyer decision is evidence that whoever controlled the verified channel made it, not legal identity.",)),
}

_REVIEW = ("Open the invoice in your PayPal dashboard and compare it with the stored payment. Resolving a review in Caligo is a decision only you can "
           "make, after you understand the difference; nothing here resolves it for you.")
ANOMALIES: dict[str, Entry] = {
    "INVOICE_ID_MISMATCH": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider returned a different invoice than the one stored.", _REVIEW),
    "INVOICE_NUMBER_MISMATCH": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider's invoice number differs from the stored one.", _REVIEW),
    "AMOUNT_OR_CURRENCY_MISMATCH": Entry(C.AMOUNT_OR_CURRENCY_MISMATCH, Sev.CONFLICT, "The provider's invoice total or currency differs from what was requested.", _REVIEW),
    "TRANSACTION_CURRENCY_MISMATCH": Entry(C.AMOUNT_OR_CURRENCY_MISMATCH, Sev.CONFLICT, "A provider transaction is in a different currency than the invoice.", _REVIEW),
    "RECIPIENT_MISMATCH": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider's recipient differs from the bound payment instruction.", _REVIEW),
    "MERCHANT_MISMATCH": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider's payee differs from the expected merchant.", _REVIEW),
    "MISSING_TRANSACTION_ID": Entry(C.RECONCILIATION_ANOMALY, Sev.WARNING, "A PayPal payment arrived without a transaction id.", _REVIEW),
    "AMOUNT_DECREASED": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider now reports less money than was recorded before (recorded money never shrinks).", _REVIEW),
    "OVERPAYMENT": Entry(C.AMOUNT_OR_CURRENCY_MISMATCH, Sev.CONFLICT, "The provider reports more money paid than the invoice total.", _REVIEW),
    "REFUND_EXCEEDS_PAYMENT": Entry(C.AMOUNT_OR_CURRENCY_MISMATCH, Sev.CONFLICT, "The provider reports more refunded than paid.", _REVIEW),
    "EXTERNALLY_MARKED_PAID": Entry(C.EXTERNAL_PAYMENT, Sev.WARNING, "The invoice was marked paid outside PayPal.", _REVIEW),
    "EXTERNALLY_MARKED_REFUNDED": Entry(C.EXTERNAL_PAYMENT, Sev.WARNING, "The invoice was marked refunded outside PayPal.", _REVIEW),
    "PAID_STATE_WITHOUT_MATCHING_PAYPAL_PAYMENTS": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider says PAID but its PayPal payments do not add up to the invoice.", _REVIEW),
    "PAID_STATE_WITH_REFUNDS": Entry(C.RECONCILIATION_ANOMALY, Sev.WARNING, "The provider says PAID although refunds are listed.", _REVIEW),
    "PARTIAL_PAYMENT_INCONSISTENT": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider says partially paid but the amounts do not fit.", _REVIEW),
    "PARTIAL_STATE_WITH_REFUNDS": Entry(C.RECONCILIATION_ANOMALY, Sev.WARNING, "The provider says partially paid although refunds are listed.", _REVIEW),
    "REFUNDED_STATE_NOT_A_FULL_PAYPAL_REFUND": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider says refunded but the PayPal refunds are not a full refund.", _REVIEW),
    "PARTIAL_REFUND_INCONSISTENT": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider says partially refunded but the amounts do not fit.", _REVIEW),
    "CANCELLED_WITH_PAYMENTS": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider says cancelled but lists payments.", _REVIEW),
    "PAYMENT_ACTIVITY_IN_UNPAID_STATE": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "Payments are listed although the provider's state is unpaid.", _REVIEW),
    "PAID_AFTER_CANCEL_MANUAL_REVIEW": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "A payment appeared after the invoice had been cancelled.", _REVIEW),
    "UNEXPECTED_AFTER_CANCEL": Entry(C.RECONCILIATION_ANOMALY, Sev.WARNING, "The provider reported unexpected activity after cancellation.", _REVIEW),
    "UNEXPECTED_AFTER_REFUND": Entry(C.RECONCILIATION_ANOMALY, Sev.WARNING, "The provider reported unexpected activity after a refund.", _REVIEW),
    "TRANSACTION_ID_CHANGED_REVIEW": Entry(C.DUPLICATE_REFERENCE, Sev.CONFLICT, "The provider reported a different transaction id than the one recorded (a recorded id is immutable).", _REVIEW),
    "DUPLICATE_TRANSACTION_ID": Entry(C.DUPLICATE_REFERENCE, Sev.CONFLICT, "The provider reported a transaction id that is already recorded on a different payment.", _REVIEW),
    "NO_TARGET_STATE_REVIEW": Entry(C.RECONCILIATION_ANOMALY, Sev.WARNING, "Reconciliation could not map the provider's state to a payment status.", _REVIEW),
    "EVIDENCE_NOW_CONSISTENT_AWAITING_RESOLUTION": Entry(
        C.RECONCILIATION_ANOMALY, Sev.INFO, "The provider's evidence is consistent again, but a person must resolve the earlier review.",
        "Review the history in your PayPal dashboard; resolving the review in Caligo remains your own decision."),
    "INSTRUCTION_TAMPERED": Entry(
        C.INSTRUCTION_INTEGRITY, Sev.CONFLICT, "The stored payment instruction no longer matches this payment or its hash chain.",
        "Stop relying on this payment and contact the operator; Caligo will not release this automatically."),
    "SEND_OUTCOME_UNCONFIRMED_RETRY_SYNC_LATER": Entry(
        C.RECONCILIATION_ANOMALY, Sev.INFO, "The provider did not confirm that the invoice was sent.", "Run a sync later and check the invoice in your PayPal dashboard."),
    "CREATED_UNVERIFIED_SYNC_REQUIRED": Entry(
        C.RECONCILIATION_ANOMALY, Sev.INFO, "The invoice was created but its details have not been verified yet.", "Run a provider sync."),
    "CREATED_NOT_IN_DRAFT": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "A newly created invoice was not in draft at the provider; it may already have been sent.", _REVIEW),
    "REJECTED_AFTER_UNCERTAIN_CREATE_REVIEW": Entry(C.RECONCILIATION_ANOMALY, Sev.CONFLICT, "The provider's answer conflicts with an earlier uncertain create.", _REVIEW),
    "CREATE_OUTCOME_UNKNOWN": Entry(
        C.RECONCILIATION_ANOMALY, Sev.WARNING, "It is not known whether the provider created the invoice.",
        "Check your PayPal dashboard for the invoice and run a sync. Do not repeat the action until you have confirmed what the provider holds."),
    "SEND_OUTCOME_UNKNOWN": Entry(
        C.RECONCILIATION_ANOMALY, Sev.WARNING, "It is not known whether the provider sent the invoice to the buyer.",
        "Check in your PayPal dashboard whether the invoice was sent, then run a provider sync."),
    # benign notes: no case
    "PAYMENT_PENDING_AT_PROVIDER": Entry(None, Sev.INFO, "", ""),
    "SEND_CONFIRMED_BY_PROVIDER": Entry(None, Sev.INFO, "", ""),
    "ABANDONED_BY_SELLER": Entry(None, Sev.INFO, "", ""),
}
_STATUS_UNCHANGED = re.compile(r"^PROVIDER_[A-Z0-9_]+_STATUS_UNCHANGED$")
_REJECTED = re.compile(r"^PROVIDER_REJECTED:[A-Z0-9_]{1,60}$")
_PROVIDER_FAILED = re.compile(r"^PROVIDER_[A-Z0-9]{3,60}$")  # PROVIDER_<ExceptionName>: the call failed before it completed
UNRECOGNIZED = Entry(C.RECONCILIATION_ANOMALY, Sev.WARNING, "Reconciliation recorded a note that this version of Caligo does not recognize.", _REVIEW)
SYNC_FAILED = Entry(C.RECONCILIATION_ANOMALY, Sev.INFO, "The last provider sync could not complete, so the stored state may be out of date.",
                    "Run a provider sync again later; check the invoice in your PayPal dashboard meanwhile.")
FAILED_CALL = Entry(C.RECONCILIATION_ANOMALY, Sev.INFO, "A provider call for this payment failed before it completed; nothing was changed.",
                    "Check the invoice in your PayPal dashboard and run a sync. Do not repeat the action until you have confirmed what the provider holds.")
REJECTED = Entry(C.RECONCILIATION_ANOMALY, Sev.WARNING, "The provider rejected a request for this payment.", _REVIEW)


def entry_for(code: str) -> tuple[str, Entry]:
    """(code that is safe to show, entry). Free text from the database is never echoed: unknown values show as UNRECOGNIZED_CODE."""
    if code in ANOMALIES:
        return code, ANOMALIES[code]
    if _STATUS_UNCHANGED.fullmatch(code):
        return code, SYNC_FAILED
    if _REJECTED.fullmatch(code):
        return code, REJECTED
    if _PROVIDER_FAILED.fullmatch(code):
        return code, FAILED_CALL
    return "UNRECOGNIZED_CODE", UNRECOGNIZED


# R3 finding code -> recovery category (CLAIM_CORROBORATED is good news and produces no case)
FINDING_CATEGORY: dict[str, C | None] = {
    "CLAIM_CORROBORATED": None,
    "PROVIDER_PAYMENT_NOT_IN_LEDGER": C.LEDGER_MISSING_PROVIDER_PAYMENT,
    "CLAIM_NOT_IN_PROVIDER_EVIDENCE": C.CLAIM_NOT_CORROBORATED,
    "CLAIM_WITHOUT_TRANSACTION_REFERENCE": C.CLAIM_NOT_CORROBORATED,
    "CLAIM_AWAITING_PROVIDER_EVIDENCE": C.AWAITING_PROVIDER_SYNC,
    "CLAIM_AMOUNT_MISMATCH": C.AMOUNT_OR_CURRENCY_MISMATCH,
    "CLAIM_CURRENCY_MISMATCH": C.AMOUNT_OR_CURRENCY_MISMATCH,
    "CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER": C.AMOUNT_OR_CURRENCY_MISMATCH,
    "CLAIMED_PAYMENTS_EXCEED_INVOICE": C.AMOUNT_OR_CURRENCY_MISMATCH,
    "CLAIMED_REFUNDS_EXCEED_PAYMENTS": C.AMOUNT_OR_CURRENCY_MISMATCH,
    "DUPLICATE_CLAIM": C.DUPLICATE_REFERENCE,
    "CONFLICTING_CLAIMS": C.DUPLICATE_REFERENCE,
    "PROVIDER_STATE_REGRESSION": C.PROVIDER_STATE_REGRESSION,
    "PROVIDER_TRANSACTION_CHANGED": C.PROVIDER_STATE_REGRESSION,
    "PROVIDER_EVIDENCE_INCOMPLETE": C.EVIDENCE_INCOMPLETE,
    "CLAIM_PROVIDER_EVIDENCE_INCOMPLETE": C.EVIDENCE_INCOMPLETE,
    "COMPARISON_INPUT_TRUNCATED": C.EVIDENCE_INCOMPLETE,
    "PROVIDER_REPORTS_EXTERNAL_PAYMENT": C.EXTERNAL_PAYMENT,
    "CLAIM_MATCHES_EXTERNAL_RECORD": C.EXTERNAL_PAYMENT,
    "CLAIM_TIMESTAMP_IN_FUTURE": C.CLAIM_TIMING_ANOMALY,
    "DELAYED_OBSERVATION": C.CLAIM_TIMING_ANOMALY,
    "OUT_OF_ORDER_OBSERVATION": C.CLAIM_TIMING_ANOMALY,
}
# Categories that assert something is ABSENT from the provider's picture: never produced while the evidence is incomplete.
ABSENCE_DEPENDENT = frozenset({C.CLAIM_NOT_CORROBORATED, C.LEDGER_MISSING_PROVIDER_PAYMENT})
# Reconciliation notes that rest on the provider's list of payments being complete; never shown while evidence is incomplete.
ABSENCE_DEPENDENT_NOTES = frozenset({"PAID_STATE_WITHOUT_MATCHING_PAYPAL_PAYMENTS", "CANCELLED_WITH_PAYMENTS",
                                     "PAYMENT_ACTIVITY_IN_UNPAID_STATE", "AMOUNT_DECREASED"})
GENERIC_HOLD = Entry(C.INSTRUCTION_HOLD, Sev.WARNING, "A payment-instruction hold is active for a reason this version does not describe.",
                     "Review the instruction history; only buyer-side verification releases a hold.")

HOLD_TEXT: dict[str, Entry] = {
    "UNVERIFIED_INSTRUCTION_CHANGE": Entry(
        C.PENDING_BUYER_AUTHORIZATION, Sev.WARNING,
        "A change to the payment instruction is waiting for buyer-side verification. Payment operations through Caligo are on hold; funds at PayPal are not frozen.",
        "Ask the buyer to approve the change through their enrolled authenticator. The seller cannot approve it, and Caligo never releases the hold automatically."),
    "INSTRUCTION_INTEGRITY_FAILURE": Entry(
        C.INSTRUCTION_INTEGRITY, Sev.CONFLICT,
        "The stored instruction history no longer hashes consistently. Payment operations through Caligo are on hold.",
        "Stop relying on stored payment instructions and contact the operator; Caligo will not release this automatically."),
    "MERCHANT_MISMATCH": Entry(
        C.INSTRUCTION_HOLD, Sev.CONFLICT,
        "The configured PayPal merchant differs from the merchant in the recorded instruction. Payment operations through Caligo are on hold.",
        "Check which merchant account is configured and whether the instruction should change; use the change process with buyer-side verification."),
    "PROVIDER_MISMATCH": Entry(
        C.INSTRUCTION_HOLD, Sev.CONFLICT,
        "The payment provider mode differs from the one in the recorded instruction. Payment operations through Caligo are on hold.",
        "Check which provider mode is configured; use the change process with buyer-side verification if the instruction should change."),
    "INSTRUCTION_REQUIRED": Entry(
        C.INSTRUCTION_HOLD, Sev.WARNING,
        "A payment instruction is required for this project but none has been recorded.",
        "Record the payment instruction; it still needs buyer-side verification before it is treated as independently verified."),
}
