"""Payment-provider port, evidence types, and the in-process MOCK PayPal adapter.

The mock never touches a network or credentials. Mock identifiers are prefixed `MOCK-` and the
provider name is `MOCK_PAYPAL`, so a mock record cannot be mistaken for a PayPal one. The
`simulate_*` methods exist for tests and local demos only and are not reachable over HTTP.
The real sandbox adapter lives in `app/paypal_sandbox.py` and implements the same port.
"""
import threading
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol
from uuid import uuid4

from app.schemas import PaymentProviderName, ProviderInvoiceState

# Re-exported so existing imports keep working.
__all__ = [
    "MockPayPalProvider", "PaymentProvider", "ProviderAmbiguousOutcome", "ProviderAuthError", "ProviderError",
    "ProviderInvoice", "ProviderInvoiceRequest", "ProviderInvoiceState", "ProviderRateLimited",
    "ProviderRejected", "ProviderTransaction", "ProviderUnavailable",
]


class ProviderError(Exception):
    """Base class. Callers fail closed: no error here may ever advance a financial state."""


class ProviderAuthError(ProviderError):
    """Credentials rejected or token could not be obtained. Nothing was processed."""


class ProviderRateLimited(ProviderError):
    """HTTP 429 after bounded retries. Nothing was processed."""


class ProviderUnavailable(ProviderError):
    """Could not reach the provider (or a safe read failed after bounded retries). For a
    state-changing call this is only raised when the request definitely never left."""


class ProviderRejected(ProviderError):
    """The provider definitively refused the request (4xx). `code` is a sanitized error name."""

    def __init__(self, code: str, message: str = "provider rejected the request"):
        super().__init__(message)
        self.code = code


class ProviderAmbiguousOutcome(ProviderError):
    """A state-changing request may or may not have been processed (timeout, 5xx, dropped connection).
    Never retried blindly; `invoice_id` is set when creation succeeded but verification failed."""

    def __init__(self, message: str = "outcome unknown", invoice_id: str | None = None):
        super().__init__(message)
        self.invoice_id = invoice_id


@dataclass(frozen=True)
class ProviderInvoiceRequest:
    idempotency_key: str  # becomes PayPal-Request-Id; stable across retries of the same payment record
    invoice_number: str  # deterministic per payment record; the provider rejects duplicates
    currency: str
    amount_minor: int
    recipient_email: str
    item_name: str = "Services"
    memo: str = ""
    reference: str = ""


@dataclass(frozen=True)
class ProviderTransaction:
    transaction_id: str | None
    kind: Literal["PAYPAL", "EXTERNAL"]  # EXTERNAL = recorded by hand/outside PayPal: not proof of payment
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class ProviderInvoice:
    """Normalized provider evidence. Money is integer minor units; no floats anywhere."""

    invoice_id: str
    invoice_number: str | None
    state: ProviderInvoiceState
    currency: str
    total_minor: int
    payments: tuple[ProviderTransaction, ...] = field(default_factory=tuple)
    refunds: tuple[ProviderTransaction, ...] = field(default_factory=tuple)
    recipient_email: str | None = None
    merchant_email: str | None = None

    @property
    def paypal_paid_minor(self) -> int:
        return sum(t.amount_minor for t in self.payments if t.kind == "PAYPAL")

    @property
    def external_paid_minor(self) -> int:
        return sum(t.amount_minor for t in self.payments if t.kind == "EXTERNAL")

    @property
    def paypal_refunded_minor(self) -> int:
        return sum(t.amount_minor for t in self.refunds if t.kind == "PAYPAL")

    @property
    def external_refunded_minor(self) -> int:
        return sum(t.amount_minor for t in self.refunds if t.kind == "EXTERNAL")


class PaymentProvider(Protocol):
    name: PaymentProviderName
    expected_merchant_email: str | None

    def create_invoice(self, request: ProviderInvoiceRequest) -> ProviderInvoice:
        """Create a DRAFT invoice. Must not email anyone."""

    def send_invoice(self, invoice_id: str) -> None:
        """Send the invoice to its recipient (the only call that can email a person)."""

    def get_invoice(self, invoice_id: str) -> ProviderInvoice:
        """Fetch current evidence server to server. Safe to retry."""


class MockPayPalProvider:
    name = PaymentProviderName.MOCK_PAYPAL
    expected_merchant_email: str | None = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._invoices: dict[str, ProviderInvoice] = {}
        self._requests: dict[str, ProviderInvoiceRequest] = {}
        self._by_key: dict[str, str] = {}

    # --- the port ---------------------------------------------------------------------

    def create_invoice(self, request: ProviderInvoiceRequest) -> ProviderInvoice:
        with self._lock:
            known = self._by_key.get(request.idempotency_key)
            if known is not None:
                if self._requests[known] != request:
                    raise ProviderRejected("IDEMPOTENCY_KEY_REUSED")
                return self._invoices[known]
            invoice_id = f"MOCK-INV-{uuid4().hex[:12].upper()}"
            invoice = ProviderInvoice(
                invoice_id, request.invoice_number, ProviderInvoiceState.DRAFT, request.currency,
                request.amount_minor, recipient_email=request.recipient_email)
            self._invoices[invoice_id] = invoice
            self._requests[invoice_id] = request
            self._by_key[request.idempotency_key] = invoice_id
            return invoice

    def send_invoice(self, invoice_id: str) -> None:
        with self._lock:
            invoice = self._get(invoice_id)
            if invoice.state is not ProviderInvoiceState.DRAFT:
                raise ProviderRejected("INVOICE_NOT_IN_DRAFT")
            self._invoices[invoice_id] = replace(invoice, state=ProviderInvoiceState.SENT)

    def get_invoice(self, invoice_id: str) -> ProviderInvoice:
        with self._lock:
            return self._get(invoice_id)

    def _get(self, invoice_id: str) -> ProviderInvoice:
        try:
            return self._invoices[invoice_id]
        except KeyError as exc:
            raise ProviderRejected("RESOURCE_NOT_FOUND") from exc

    # --- test / demo helpers: simulate what the provider would report ------------------

    def _txn(self, amount: int, currency: str, kind: str = "PAYPAL", transaction_id: str | None = "") -> ProviderTransaction:
        txn = f"MOCK-TXN-{uuid4().hex[:12].upper()}" if transaction_id == "" else transaction_id
        return ProviderTransaction(txn, kind, amount, currency)  # type: ignore[arg-type]

    def simulate_payment(self, invoice_id: str, *, amount_minor: int | None = None, currency: str | None = None,
                         transaction_id: str | None = "") -> ProviderInvoice:
        """Mark paid provider-side. Overriding amount/currency changes what the provider *reports* for the
        invoice total (to simulate a mismatching payment)."""
        with self._lock:
            current = self._get(invoice_id)
            total = current.total_minor if amount_minor is None else amount_minor
            cur = current.currency if currency is None else currency
            updated = replace(current, state=ProviderInvoiceState.PAID, total_minor=total, currency=cur,
                              payments=(*current.payments, self._txn(total, cur, transaction_id=transaction_id)))
            self._invoices[invoice_id] = updated
            return updated

    def simulate_partial_payment(self, invoice_id: str, amount_minor: int) -> ProviderInvoice:
        with self._lock:
            current = self._get(invoice_id)
            updated = replace(current, state=ProviderInvoiceState.PARTIALLY_PAID,
                              payments=(*current.payments, self._txn(amount_minor, current.currency)))
            self._invoices[invoice_id] = updated
            return updated

    def simulate_refund(self, invoice_id: str, amount_minor: int | None = None) -> ProviderInvoice:
        """Refund `amount_minor` (default: everything paid). Full refund -> REFUNDED, otherwise PARTIALLY_REFUNDED."""
        with self._lock:
            current = self._get(invoice_id)
            paid = current.paypal_paid_minor
            amount = paid if amount_minor is None else amount_minor
            refunds = (*current.refunds, self._txn(amount, current.currency, transaction_id="MOCK-REF-" + uuid4().hex[:10].upper()))
            refunded = sum(t.amount_minor for t in refunds)
            state = ProviderInvoiceState.REFUNDED if refunded == paid else ProviderInvoiceState.PARTIALLY_REFUNDED
            updated = replace(current, state=state, refunds=refunds)
            self._invoices[invoice_id] = updated
            return updated

    def simulate_external_payment(self, invoice_id: str) -> ProviderInvoice:
        """Seller 'marked as paid' outside PayPal: the provider status changes but no PayPal payment exists."""
        with self._lock:
            current = self._get(invoice_id)
            updated = replace(current, state=ProviderInvoiceState.MARKED_AS_PAID,
                              payments=(*current.payments, self._txn(current.total_minor, current.currency, kind="EXTERNAL", transaction_id=None)))
            self._invoices[invoice_id] = updated
            return updated

    def simulate_state(self, invoice_id: str, state: ProviderInvoiceState) -> ProviderInvoice:
        with self._lock:
            updated = replace(self._get(invoice_id), state=state)
            self._invoices[invoice_id] = updated
            return updated
