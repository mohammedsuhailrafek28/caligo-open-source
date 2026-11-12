"""Read-only verification of ONE existing PayPal Sandbox invoice. It looks; it never changes anything.

Safety properties (each is enforced by code and covered by tests/test_sandbox_readonly.py):
* Default is a DRY RUN with no network call. A real run needs BOTH `--execute` and the confirmation variable.
* The only requests that can leave the process are: the OAuth token request, and a GET of the one named invoice.
  An allowlist transport blocks everything else (other methods, paths, hosts, schemes, queries, invoices) before it is sent.
* One attempt: the provider is built with max_attempts=1. The single extra request that can happen is the adapter's one
  token refresh after a 401, which the allowlist budget permits and nothing more.
* Credentials come only from process environment variables. Environment files are never read and the app Settings are never built.
* Every line of output passes through a redactor; email addresses are masked and are never printed in full.
* It writes no files and does not touch the smoke-test state in any way.
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Callable, Mapping

import httpx

from app.payment_provider import (
    ProviderAuthError, ProviderError, ProviderInvoice, ProviderInvoiceState, ProviderRateLimited, ProviderRejected,
    ProviderUnavailable,
)
from app.paypal_sandbox import (
    INVOICES_PATH, SANDBOX_BASE_URL, TOKEN_PATH, PayPalSandboxProvider, minor_to_value, value_to_minor,
)
from app.sandbox_smoke import (
    ENV_BUYER, ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_MERCHANT, Redactor, SmokeRefusal, load_config, mask_email,
)

EXIT_OK, EXIT_VERIFY_FAILED, EXIT_REFUSED, EXIT_UNREADABLE, EXIT_PROVIDER_ERROR = 0, 1, 2, 3, 4

ENV_CONFIRM = "PAYPAL_READONLY_CONFIRM"  # deliberately different from the smoke test's variable
CONFIRM_VALUE = "I_UNDERSTAND_THIS_READS_PAYPAL_SANDBOX"

_SANDBOX_HOST = httpx.URL(SANDBOX_BASE_URL).host
_ID_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{5,63}$")
_NUMBER = re.compile(r"^[A-Za-z0-9._-]{1,25}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SAFE_KEY = re.compile(r"^[A-Za-z_]{1,32}$")
_STATUS_WORD = re.compile(r"^[A-Z_]{1,32}$")
_ZERO = re.compile(r"^0+([.]0+)?$")
_DOMAIN = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")
MERCHANT_LABEL = "merchant is the configured sandbox business account"
_NOT_FOUND_CODES = ("RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
_MAX_KEYS = 20
_TOKEN_BUDGET = 2  # first token plus at most one refresh
_GET_BUDGET = 2    # first read plus at most one repeat after a refresh


class BlockedRequest(Exception):
    """An outbound request outside the allowlist was refused before it was sent."""


class AllowlistTransport(httpx.BaseTransport):
    """Lets exactly two kinds of request through: POST the OAuth token path, and GET the one named invoice. Nothing else.

    It also remembers, for diagnostics only, the top-level key names and the status word of the invoice response, so an
    unreadable response can be explained without ever printing a value."""

    def __init__(self, inner: httpx.BaseTransport, invoice_id: str) -> None:
        try:
            self._invoice_id = PayPalSandboxProvider._check_invoice_id(invoice_id)
        except ProviderError as exc:
            raise ValueError("invalid invoice id") from exc
        self._inner = inner
        self._token_path = TOKEN_PATH.encode()
        self._invoice_path = f"{INVOICES_PATH}/{self._invoice_id}".encode()
        self._tokens_left = _TOKEN_BUDGET
        self._reads_left = _GET_BUDGET
        self.response_keys: tuple[str, ...] = ()
        self.response_status_word: str | None = None
        self.shape_parsed = False          # the body was a JSON object
        self.amounts_reported = False      # payments.paid_amount / refunds.refunded_amount present and not provably zero
        self.extra_recipients = False      # more than one primary recipient, or any additional recipient
        self.invoicer_present = False      # the response has an `invoicer` object
        self.invoicer_keys: tuple[str, ...] = ()  # names (never values) of the fields inside it

    def _allowed(self, request: httpx.Request) -> str | None:
        url = request.url
        if url.scheme != "https" or url.host != _SANDBOX_HOST or url.port is not None or url.userinfo or url.fragment:
            return None
        if request.method == "POST" and url.raw_path == self._token_path:
            return "token"
        if request.method == "GET" and url.raw_path == self._invoice_path:
            return "read"
        return None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        kind = self._allowed(request)
        if kind is None:
            raise BlockedRequest("request outside the read-only allowlist")
        if kind == "token":
            if self._tokens_left <= 0:
                raise BlockedRequest("OAuth request budget exhausted")
            self._tokens_left -= 1
            return self._inner.handle_request(request)
        if self._reads_left <= 0:
            raise BlockedRequest("invoice read budget exhausted")
        self._reads_left -= 1
        response = self._inner.handle_request(request)
        self._remember_shape(response)
        return response

    def _remember_shape(self, response: httpx.Response) -> None:
        try:
            response.read()
            data = json.loads(response.content)
        except (ValueError, httpx.HTTPError):
            return
        if not isinstance(data, dict):
            return
        self.shape_parsed = True
        invoicer = data.get("invoicer")
        if isinstance(invoicer, dict):
            self.invoicer_present = True
            self.invoicer_keys = tuple(sorted(k for k in invoicer if isinstance(k, str) and _SAFE_KEY.match(k))[:_MAX_KEYS])
        self.amounts_reported = _reported(data.get("payments"), "paid_amount") or _reported(data.get("refunds"), "refunded_amount")
        primary, additional = data.get("primary_recipients"), data.get("additional_recipients")
        self.extra_recipients = (isinstance(primary, list) and len(primary) > 1) or not (primary is None or isinstance(primary, list)) \
            or bool(additional) or not (additional is None or isinstance(additional, list))
        self.response_keys = tuple(sorted(k for k in data if isinstance(k, str) and _SAFE_KEY.match(k))[:_MAX_KEYS])
        status = data.get("status")
        self.response_status_word = status if isinstance(status, str) and _STATUS_WORD.match(status) else None

    def close(self) -> None:
        self._inner.close()


def _reported(container: object, key: str) -> bool:
    """True unless the amount is absent or provably zero. Anything malformed counts as reported (fail closed)."""
    if container is None:
        return False
    if not isinstance(container, dict):
        return True
    amount = container.get(key)
    if amount is None:
        return False
    value = amount.get("value") if isinstance(amount, dict) else None
    return not (isinstance(value, str) and _ZERO.match(value))


def _printable(value: object, pattern: re.Pattern) -> str:
    """A value from PayPal's response is printed only if it has a strictly safe shape."""
    return value if isinstance(value, str) and pattern.match(value) else "(not printable)"


@dataclass(frozen=True)
class Expectation:
    invoice_id: str
    invoice_number: str
    amount_minor: int
    currency: str
    merchant_email: str
    buyer_email: str


def build_expectation(invoice_id: str, invoice_number: str, amount: str, currency: str,
                      merchant_email: str, buyer_email: str) -> Expectation:
    """Validate what the operator expects to find. Raises SmokeRefusal; nothing has been sent at this point."""
    try:
        PayPalSandboxProvider._check_invoice_id(invoice_id)
    except ProviderError:
        raise SmokeRefusal("the invoice id is not a valid PayPal invoice id") from None
    if not _NUMBER.match(invoice_number or ""):
        raise SmokeRefusal("the invoice number is not valid (1-25 letters, digits, '.', '_' or '-')")
    if not _CURRENCY.match(currency or ""):
        raise SmokeRefusal("the currency must be a 3-letter upper-case code")
    try:
        amount_minor = value_to_minor(amount, currency)
    except ProviderError:
        raise SmokeRefusal("the amount or currency is not valid (use a plain decimal such as 1.00)") from None
    return Expectation(invoice_id, invoice_number, amount_minor, currency, merchant_email, buyer_email)


def _text(value: object) -> str:
    """The normalized text of a provider-supplied identity. Anything that is not a string is treated as missing."""
    return value.strip().lower() if isinstance(value, str) else ""


def _masked(address: str) -> str:
    """First character plus domain, e.g. m***@example.com. Anything that does not look like an address is not shown."""
    local, sep, domain = address.partition("@")
    if not sep or not local or not _DOMAIN.match(domain):
        return "(not an email address)"
    first = local[0] if local[0].isascii() and local[0].isalnum() else "?"
    return f"{first}***@{domain.lower()}"


def merchant_diagnostic(readback: ProviderInvoice, expected: Expectation, gate: "AllowlistTransport") -> str:
    """Why the merchant check failed, without any full address or any value other than the verdicts below."""
    fields = ", ".join(gate.invoicer_keys) or "(none)"
    raw = readback.merchant_email
    if not gate.invoicer_present:
        return "the `invoicer` object is absent from the response (no merchant identity was provided)"
    if raw is None:
        return f"`invoicer.email_address` is absent (invoicer fields: {fields})"
    if not isinstance(raw, str):
        return f"`invoicer.email_address` is present but is not a string (invoicer fields: {fields})"
    found = raw.strip().lower()
    if not found:
        return f"`invoicer.email_address` is present but empty (invoicer fields: {fields})"
    same_domain = found.partition("@")[2] == expected.merchant_email.partition("@")[2]
    same_length = len(found) == len(expected.merchant_email)
    return (f"`invoicer.email_address` is present but differs: found {_masked(found)}, expected {_masked(expected.merchant_email)}; "
            f"domains {'match' if same_domain else 'differ'}; lengths {'match' if same_length else 'differ'} "
            f"(invoicer fields: {fields})")


def verify(readback: ProviderInvoice, expected: Expectation) -> list[tuple[str, bool, str]]:
    """Named checks of the invoice PayPal returned against what was expected. All must pass; missing evidence fails."""
    merchant = _text(readback.merchant_email)
    recipient = _text(readback.recipient_email)
    return [
        ("invoice id matches", readback.invoice_id == expected.invoice_id, f"found {_printable(readback.invoice_id, _ID_SHAPE)}"),
        ("invoice number matches", readback.invoice_number == expected.invoice_number, f"found {_printable(readback.invoice_number, _NUMBER)}"),
        (MERCHANT_LABEL, merchant == expected.merchant_email, ""),
        ("total amount matches", readback.total_minor == expected.amount_minor,
         f"found {minor_to_value(readback.total_minor, readback.currency)}"),
        ("currency matches", readback.currency == expected.currency, f"found {readback.currency}"),
        ("status is DRAFT", readback.state is ProviderInvoiceState.DRAFT, f"found {readback.state.value}"),
        ("recipient is the configured buyer", recipient == expected.buyer_email, ""),
        ("no payments recorded", not readback.payments, f"found {len(readback.payments)}"),
        ("no refunds recorded", not readback.refunds, f"found {len(readback.refunds)}"),
    ]


def run(env: Mapping[str, str], *, invoice_id: str, invoice_number: str, amount: str, currency: str = "USD",
        execute: bool = False, out: Callable[[str], None] = print, transport: httpx.BaseTransport | None = None) -> int:
    redact = Redactor([(env.get(ENV_CLIENT_ID) or "").strip(), (env.get(ENV_CLIENT_SECRET) or "").strip(),
                       env.get(ENV_CLIENT_ID, ""), env.get(ENV_CLIENT_SECRET, "")])

    def say(line: str = "") -> None:
        out(redact(line))

    say("PayPal SANDBOX read-only invoice verification. It cannot create, send, pay, refund, cancel or change anything.")
    try:
        config = load_config(env)
        expected = build_expectation(invoice_id, invoice_number, amount, currency,
                                     config.merchant_email, config.buyer_email)
    except SmokeRefusal as exc:
        say(f"REFUSED: {exc}. No network call was made.")
        return EXIT_REFUSED
    say(f"Expecting invoice {expected.invoice_id} / {expected.invoice_number}: "
        f"{minor_to_value(expected.amount_minor, expected.currency)} {expected.currency}, status DRAFT, "
        f"merchant {mask_email(expected.merchant_email)}, buyer {mask_email(expected.buyer_email)}.")
    say(f"Plan: POST {TOKEN_PATH} (OAuth) -> GET {INVOICES_PATH}/{expected.invoice_id} -> compare. Nothing else is permitted.")
    if not execute:
        say(f"DRY RUN: no network call was made. To run for real add --execute and set {ENV_CONFIRM}={CONFIRM_VALUE}.")
        return EXIT_OK
    if env.get(ENV_CONFIRM) != CONFIRM_VALUE:
        say(f"REFUSED: --execute also requires {ENV_CONFIRM}={CONFIRM_VALUE}. No network call was made.")
        return EXIT_REFUSED
    gate = AllowlistTransport(transport if transport is not None else httpx.HTTPTransport(), expected.invoice_id)
    provider = PayPalSandboxProvider(config.client_id, config.client_secret, merchant_email=config.merchant_email,
                                     transport=gate, max_attempts=1)
    try:
        return _verify_remote(provider, gate, expected, say)
    except BlockedRequest:
        say("REFUSED: a request outside the read-only allowlist was blocked before it was sent. Stopping.")
        return EXIT_REFUSED
    except Exception as exc:  # never show a traceback; the type name is enough and nothing was changed remotely
        say(f"UNEXPECTED ERROR ({type(exc).__name__}). Nothing was changed. Stopping.")
        return EXIT_UNREADABLE
    finally:
        provider.close()


def _verify_remote(provider: PayPalSandboxProvider, gate: AllowlistTransport, expected: Expectation,
                   say: Callable[[str], None]) -> int:
    say("Step 1/2 OAuth ...")
    try:
        provider.authenticate()
    except ProviderError as exc:
        say(f"OAuth FAILED ({type(exc).__name__}). Check the client id/secret belong to a SANDBOX app.")
        return EXIT_PROVIDER_ERROR
    say("OAuth OK.")
    say("Step 2/2 GET the invoice ...")
    try:
        readback = provider.get_invoice(expected.invoice_id)
    except (ProviderAuthError, ProviderRateLimited, ProviderUnavailable, ProviderRejected) as exc:
        code = getattr(exc, "code", "")
        say(f"Invoice read FAILED ({type(exc).__name__}{' ' + code if code else ''}). One attempt was made; not retrying.")
        if code in _NOT_FOUND_CODES:
            say("The invoice was not found: check the id, and that these credentials belong to the same sandbox business account.")
        return EXIT_PROVIDER_ERROR
    except ProviderError:
        say("UNREADABLE invoice response (it did not match the expected shape, so nothing is trusted).")
        say("  top-level keys: " + (", ".join(gate.response_keys) or "(none readable)"))
        say("  status word: " + (gate.response_status_word or "(none)"))
        return EXIT_UNREADABLE
    results = verify(readback, expected) + _shape_checks(gate)
    for label, ok, detail in results:
        say(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" ({detail})" if detail and not ok else ""))
        if label == MERCHANT_LABEL and not ok:
            say("      merchant diagnostic: " + merchant_diagnostic(readback, expected, gate))
    passed = all(ok for _, ok, _ in results)
    say(f"Invoice {readback.invoice_id}: status {readback.state.value}, "
        f"{minor_to_value(readback.total_minor, readback.currency)} {readback.currency}. Nothing was changed.")
    say("RESULT: PASS" if passed else "RESULT: FAIL (see failed checks above)")
    return EXIT_OK if passed else EXIT_VERIFY_FAILED


def _shape_checks(gate: AllowlistTransport) -> list[tuple[str, bool, str]]:
    """Evidence the normalized invoice does not carry. If the raw body was not seen, both checks fail."""
    seen = gate.shape_parsed
    return [
        ("no paid or refunded amount reported", seen and not gate.amounts_reported, ""),
        ("the configured buyer is the only recipient", seen and not gate.extra_recipients, ""),
    ]


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description="PayPal SANDBOX read-only verification of one existing invoice.")
    parser.add_argument("--invoice-id", required=True, help="the PayPal invoice id to read")
    parser.add_argument("--invoice-number", required=True, help="the invoice number you expect, e.g. SMOKE-...")
    parser.add_argument("--amount", required=True, help="the total you expect, e.g. 1.00")
    parser.add_argument("--currency", default="USD", help="the currency you expect (default USD)")
    parser.add_argument("--execute", action="store_true", help="really contact the PayPal sandbox (default: dry run)")
    args = parser.parse_args(argv)
    return run(os.environ if env is None else env, invoice_id=args.invoice_id, invoice_number=args.invoice_number,
               amount=args.amount, currency=args.currency, execute=args.execute)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
