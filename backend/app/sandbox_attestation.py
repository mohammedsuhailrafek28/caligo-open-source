"""Which payments carry AUTHENTICATED PayPal Sandbox evidence.

A `PaymentRecord` with provider PAYPAL_SANDBOX is produced by the Sandbox adapter, but the same adapter is also driven by the fake PayPal in tests
and in the offline rehearsal. Only the controlled Sandbox runner, after a real, allowlisted, authenticated GET of that exact invoice returned HTTP 200
over the real transport, may attest a payment here. Everything else stays `UNATTESTED_SANDBOX_PROVIDER` (and a MOCK provider is never Sandbox).

The registry is process-local and in memory: nothing here is persisted by this module (the runner keeps its own non-secret run state and re-attests
on start-up). `attest` must be called from `app.sandbox_runner` only; `tests/test_sandbox_runner.py` enforces that by scanning the source tree.
"""
import threading

LABEL_AUTHENTICATED = "AUTHENTICATED_PAYPAL_SANDBOX"
LABEL_UNATTESTED = "UNATTESTED_SANDBOX_PROVIDER"
LABEL_MOCK = "SIMULATED_MOCK_PROVIDER"

_lock = threading.Lock()
_attested: set[str] = set()


def attest(payment_id: str) -> None:
    """Record that this payment's provider evidence came from an authenticated Sandbox read. Runner-only."""
    with _lock:
        _attested.add(payment_id)


def is_attested(payment_id: str) -> bool:
    with _lock:
        return payment_id in _attested


def clear() -> None:
    """Forget every attestation (used by tests and at runner start-up)."""
    with _lock:
        _attested.clear()


def provider_label(provider: str, payment_id: str) -> str:
    """The provenance label for a payment record's provider evidence."""
    if provider == "MOCK_PAYPAL":
        return LABEL_MOCK
    if provider == "PAYPAL_SANDBOX":
        return LABEL_AUTHENTICATED if is_attested(payment_id) else LABEL_UNATTESTED
    return "UNLABELLED_PROVIDER"
