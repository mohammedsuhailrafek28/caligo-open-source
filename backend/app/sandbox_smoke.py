"""Stage 2D sandbox smoke test: ONE synthetic DRAFT invoice, read back and verified. Nothing else.

Safety properties (each is enforced by code and covered by tests/test_sandbox_smoke.py):
* Default is a DRY RUN: no credentials are read beyond checking they are present, and no network call is made. A real run
  needs BOTH the `--execute` flag and the confirmation environment variable.
* Credentials come only from process environment variables. This module never reads environment files and never builds
  the application Settings object.
* It can only: authenticate (OAuth), create one DRAFT invoice, and read it back. It has no code path that emails the
  recipient, records a payment or refund, or changes an invoice status.
* At most one creation per marker file. The marker is written BEFORE the create call. After an ambiguous outcome there is
  no retry: the run stops and the marker blocks further runs until a human has inspected the sandbox and removed it.
* Every line of output passes through a redactor that removes the credentials, any bearer token and masks email addresses.
"""
import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from app.payment_provider import (
    ProviderAmbiguousOutcome, ProviderAuthError, ProviderError, ProviderInvoice, ProviderInvoiceRequest,
    ProviderInvoiceState, ProviderRateLimited, ProviderRejected, ProviderUnavailable,
)
from app.paypal_sandbox import PayPalSandboxProvider, minor_to_value

EXIT_OK, EXIT_VERIFY_FAILED, EXIT_REFUSED, EXIT_AMBIGUOUS, EXIT_PROVIDER_ERROR = 0, 1, 2, 3, 4

# Dedicated names so a production credential exported for something else can never be used by accident.
ENV_CLIENT_ID = "PAYPAL_SANDBOX_CLIENT_ID"
ENV_CLIENT_SECRET = "PAYPAL_SANDBOX_CLIENT_SECRET"
ENV_MERCHANT = "PAYPAL_SANDBOX_MERCHANT_EMAIL"
ENV_BUYER = "PAYPAL_SMOKE_BUYER_EMAIL"
ENV_CONFIRM = "PAYPAL_SMOKE_CONFIRM"
CONFIRM_VALUE = "I_UNDERSTAND_THIS_CALLS_PAYPAL_SANDBOX"
REQUIRED_ENV = (ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_MERCHANT, ENV_BUYER)

SMOKE_AMOUNT_MINOR = 100  # synthetic: 1.00 USD
SMOKE_CURRENCY = "USD"
DEFAULT_MARKER = Path(__file__).resolve().parents[1] / ".smoke" / "state.json"
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PAYPAL_TOKEN = re.compile(r"A21AA[A-Za-z0-9_-]{20,}")
_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

PREREQUISITES = (
    "PayPal Developer Dashboard > Apps & Credentials > SANDBOX toggle on (not Live).",
    "A Sandbox REST app exists and its client ID / secret are the ones you export below.",
    "A Sandbox BUSINESS account (the merchant) exists; its email is PAYPAL_SANDBOX_MERCHANT_EMAIL.",
    "A Sandbox PERSONAL account (the synthetic buyer) exists; its email is PAYPAL_SMOKE_BUYER_EMAIL.",
    "The Sandbox business account can use Invoicing (open sandbox.paypal.com as the business user and confirm Invoicing loads).",
    "The app was created under that business account so invoices are issued by it.",
)


class SmokeRefusal(Exception):
    """The run was refused before any network call."""


@dataclass(frozen=True)
class SmokeConfig:
    client_id: str
    client_secret: str
    merchant_email: str
    buyer_email: str


class Redactor:
    """Removes known secrets, bearer tokens and email addresses from anything that is printed."""

    def __init__(self, secrets: list[str]) -> None:
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        text = _BEARER.sub("Bearer [REDACTED]", text)
        text = _PAYPAL_TOKEN.sub("[REDACTED]", text)
        return _EMAIL_IN_TEXT.sub(lambda m: mask_email(m.group(0)), text)


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def load_config(env: Mapping[str, str]) -> SmokeConfig:
    missing = [name for name in REQUIRED_ENV if not (env.get(name) or "").strip()]
    if missing:
        raise SmokeRefusal("missing environment variables: " + ", ".join(missing))
    config = SmokeConfig(env[ENV_CLIENT_ID].strip(), env[ENV_CLIENT_SECRET].strip(),
                         env[ENV_MERCHANT].strip().lower(), env[ENV_BUYER].strip().lower())
    for label, email in (("merchant", config.merchant_email), ("buyer", config.buyer_email)):
        if not _EMAIL.match(email):
            raise SmokeRefusal(f"the {label} email is not a valid address")
    if config.merchant_email == config.buyer_email:
        raise SmokeRefusal("the buyer must be a different sandbox account from the merchant")
    return config


def build_request(config: SmokeConfig, run_id: str) -> ProviderInvoiceRequest:
    return ProviderInvoiceRequest(
        idempotency_key=f"smoke-{run_id}", invoice_number=f"SMOKE-{run_id.upper()}", currency=SMOKE_CURRENCY,
        amount_minor=SMOKE_AMOUNT_MINOR, recipient_email=config.buyer_email, item_name="Synthetic smoke-test item",
        memo="Stage 2D synthetic sandbox smoke test - safe to delete", reference="SYNTH-PROJECT")


def verify_invoice(created: ProviderInvoice, readback: ProviderInvoice, request: ProviderInvoiceRequest,
                   merchant_email: str) -> list[tuple[str, bool]]:
    """Named checks of the invoice read back from PayPal against what we asked for. All must pass."""
    return [
        ("invoice id matches the id returned at creation", readback.invoice_id == created.invoice_id),
        ("invoice number matches the request", readback.invoice_number == request.invoice_number),
        ("merchant is the configured sandbox business account",
         (readback.merchant_email or "").strip().lower() == merchant_email),
        ("total amount matches", readback.total_minor == request.amount_minor),
        ("currency matches", readback.currency == request.currency),
        ("status is DRAFT", readback.state is ProviderInvoiceState.DRAFT),
        ("recipient is the synthetic buyer", (readback.recipient_email or "").strip().lower() == request.recipient_email),
        ("no payments recorded", not readback.payments),
        ("no refunds recorded", not readback.refunds),
    ]


def _write_marker(path: Path, state: str, **fields: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"state": state, "updated_at": datetime.now(timezone.utc).isoformat(), **fields}
    path.write_text(json.dumps(payload, indent=2), encoding="utf8")  # identifiers only; never credentials


def _claim_marker(path: Path, invoice_number: str) -> bool:
    """Atomically create the marker (O_EXCL). False if it already exists, so two concurrent runs cannot both proceed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf8") as handle:
            json.dump({"state": "CLAIMED", "invoice_number": invoice_number,
                       "updated_at": datetime.now(timezone.utc).isoformat()}, handle, indent=2)
    except FileExistsError:
        return False
    return True


def run(env: Mapping[str, str], *, execute: bool = False, marker: Path = DEFAULT_MARKER,
        out: Callable[[str], None] = print, transport=None) -> int:
    redact = Redactor([env.get(ENV_CLIENT_ID, ""), env.get(ENV_CLIENT_SECRET, "")])

    def say(line: str = "") -> None:
        out(redact(line))

    say("PayPal SANDBOX smoke test (Stage 2D): one synthetic DRAFT invoice. It is never sent and never paid.")
    say("Prerequisites to confirm manually in the PayPal Developer Dashboard:")
    for item in PREREQUISITES:
        say(f"  [ ] {item}")
    try:
        config = load_config(env)
    except SmokeRefusal as exc:
        say(f"REFUSED: {exc}")
        return EXIT_REFUSED
    say(f"Environment OK: client id set ({len(config.client_id)} chars), secret set, merchant "
        f"{mask_email(config.merchant_email)}, buyer {mask_email(config.buyer_email)}.")
    request = build_request(config, uuid.uuid4().hex[:12])
    say(f"Plan: OAuth -> create ONE DRAFT invoice {request.invoice_number} "
        f"({minor_to_value(request.amount_minor, request.currency)} {request.currency}) -> read it back -> verify. "
        "No send, no payment, no retries after an ambiguous creation.")
    if not execute:
        say("DRY RUN: no network call was made. To run for real add --execute and set "
            f"{ENV_CONFIRM}={CONFIRM_VALUE}.")
        return EXIT_OK
    if env.get(ENV_CONFIRM) != CONFIRM_VALUE:
        say(f"REFUSED: --execute also requires {ENV_CONFIRM}={CONFIRM_VALUE}. No network call was made.")
        return EXIT_REFUSED
    if marker.exists():
        say(f"REFUSED: a previous smoke run left {marker.name}. Inspect the sandbox dashboard for invoice numbers "
            "starting with SMOKE-, then delete the marker file yourself before running again. No network call was made.")
        return EXIT_REFUSED
    try:
        if not _claim_marker(marker, request.invoice_number):
            say("REFUSED: another smoke run holds the marker. No network call was made.")
            return EXIT_REFUSED
    except OSError as exc:
        say(f"REFUSED: cannot create the marker file ({type(exc).__name__}). No network call was made.")
        return EXIT_REFUSED
    try:
        return _execute(config, request, marker, say, transport)
    except Exception as exc:  # never show a raw traceback; the marker (if any) keeps blocking reruns
        say(f"UNEXPECTED ERROR ({type(exc).__name__}). Treat the outcome as ambiguous: check the sandbox dashboard "
            f"for {request.invoice_number}. The marker blocks reruns until you remove it.")
        return EXIT_AMBIGUOUS


def _execute(config: SmokeConfig, request: ProviderInvoiceRequest, marker: Path, say: Callable[[str], None],
             transport) -> int:
    provider = PayPalSandboxProvider(config.client_id, config.client_secret, merchant_email=config.merchant_email,
                                     transport=transport, max_attempts=1)  # max_attempts=1: no automatic retries at all
    try:
        say("Step 1/3 OAuth ...")
        try:
            provider.authenticate()
        except ProviderError as exc:
            marker.unlink(missing_ok=True)  # nothing was attempted: release the single attempt
            say(f"OAuth FAILED ({type(exc).__name__}). Check the client id/secret belong to a SANDBOX app. Nothing was created.")
            return EXIT_PROVIDER_ERROR
        say("OAuth OK.")

        _write_marker(marker, "ATTEMPTING", invoice_number=request.invoice_number)  # before the only create call
        say("Step 2/3 create ONE draft invoice ...")
        try:
            created = provider.create_invoice(request)
        except ProviderAmbiguousOutcome as exc:
            _write_marker(marker, "AMBIGUOUS", invoice_number=request.invoice_number, invoice_id=exc.invoice_id or "")
            say(f"AMBIGUOUS creation outcome ({exc}): the invoice may or may not exist"
                + (f" (provider id {exc.invoice_id})" if exc.invoice_id else "") + ". NOT retrying. "
                f"Look for {request.invoice_number} in the sandbox dashboard; the marker blocks further runs.")
            return EXIT_AMBIGUOUS
        except (ProviderAuthError, ProviderRejected, ProviderRateLimited, ProviderUnavailable) as exc:
            marker.unlink(missing_ok=True)  # the request provably created nothing, so the single attempt is not consumed
            code = getattr(exc, "code", "")
            say(f"Creation FAILED before any invoice existed ({type(exc).__name__} {code}).".replace("  ", " "))
            if isinstance(exc, ProviderAuthError):
                say("Hint: the app may lack the Invoicing feature, or the business account cannot issue invoices.")
            return EXIT_PROVIDER_ERROR
        except Exception as exc:  # unknown failure mode: treat as ambiguous, never retry
            _write_marker(marker, "AMBIGUOUS", invoice_number=request.invoice_number)
            say(f"AMBIGUOUS creation outcome ({type(exc).__name__}). NOT retrying; marker written.")
            return EXIT_AMBIGUOUS
        say(f"Created draft invoice {created.invoice_id}.")

        say("Step 3/3 read the same invoice back from PayPal and verify ...")
        try:
            readback = provider.get_invoice(created.invoice_id)
        except Exception as exc:
            _write_marker(marker, "CREATED_UNVERIFIED", invoice_number=request.invoice_number, invoice_id=created.invoice_id)
            say(f"Read-back FAILED ({type(exc).__name__}). The draft exists ({created.invoice_id}); inspect it manually.")
            return EXIT_AMBIGUOUS
        results = verify_invoice(created, readback, request, config.merchant_email)
        for label, ok in results:
            say(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        passed = all(ok for _, ok in results)
        _write_marker(marker, "VERIFIED" if passed else "VERIFY_FAILED", invoice_number=request.invoice_number,
                      invoice_id=created.invoice_id)
        say(f"Invoice {readback.invoice_id}: status {readback.state.value}, "
            f"{minor_to_value(readback.total_minor, readback.currency)} {readback.currency}. It was NOT sent and NOT paid; "
            "delete the draft in the sandbox dashboard if you wish.")
        say("RESULT: PASS" if passed else "RESULT: FAIL (see failed checks above)")
        return EXIT_OK if passed else EXIT_VERIFY_FAILED
    finally:
        provider.close()


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description="PayPal SANDBOX smoke test: one synthetic DRAFT invoice (never sent or paid).")
    parser.add_argument("--execute", action="store_true", help="really contact the PayPal sandbox (default: dry run)")
    parser.add_argument("--marker", type=Path, default=DEFAULT_MARKER, help="state file that enforces at most one creation")
    args = parser.parse_args(argv)
    return run(os.environ if env is None else env, execute=args.execute, marker=args.marker)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
