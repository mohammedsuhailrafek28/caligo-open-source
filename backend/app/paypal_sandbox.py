"""PayPal Invoicing v2 adapter for the SANDBOX environment only.

* The base URL is a constant (`api-m.sandbox.paypal.com`); there is no live/production mode and no way to configure one.
* Credentials come only from the caller (the application reads them from environment variables). They are held in
  memory, sent only to the OAuth endpoint, and never logged. Access tokens are cached in memory until shortly
  before they expire.
* Network behaviour: explicit timeouts; bounded retries with exponential backoff and jitter. Only SAFE calls (OAuth,
  GET) are retried after an ambiguous failure. State-changing calls (create, send) are retried only when the request
  provably never reached PayPal (connection failure, HTTP 429, HTTP 401 after a token refresh). Any other failure
  of a state-changing call raises `ProviderAmbiguousOutcome` and is never retried blindly: the caller reconciles.
* Endpoints used: POST /v2/invoicing/invoices, POST /v2/invoicing/invoices/{id}/send, GET /v2/invoicing/invoices/{id},
  plus POST /v1/oauth2/token for authentication.
* Money is converted with exact integer/Decimal arithmetic; floats are never used.
* Logs carry the operation name, HTTP status and attempt number only: never headers, bodies, tokens or secrets.
"""
import logging
import random
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from app.payment_provider import (
    ProviderAmbiguousOutcome, ProviderAuthError, ProviderError, ProviderInvoice, ProviderInvoiceRequest,
    ProviderInvoiceState, ProviderRateLimited, ProviderRejected, ProviderTransaction, ProviderUnavailable,
)
from app.schemas import PaymentProviderName

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
# PayPal puts HATEOAS links in responses whose host may differ from the host we called (e.g. api.sandbox.paypal.com).
# We never request a response link: only the invoice id is extracted, and every follow-up call goes to SANDBOX_BASE_URL (api-m.sandbox.paypal.com).
_LINK_PATH = re.compile(r"^/v2/invoicing/invoices/([^/?#]+)$")
_SAFE_KEY = re.compile(r"^[A-Za-z_]{1,32}$")
INVOICES_PATH = "/v2/invoicing/invoices"
TOKEN_PATH = "/v1/oauth2/token"

# Currencies PayPal supports for invoicing; HUF, JPY and TWD have no minor unit.
ZERO_DECIMAL_CURRENCIES = frozenset({"HUF", "JPY", "TWD"})
SUPPORTED_CURRENCIES = frozenset({
    "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP", "HKD", "HUF", "ILS", "JPY", "MXN", "MYR",
    "NOK", "NZD", "PHP", "PLN", "SEK", "SGD", "THB", "TWD", "USD",
})
_INVOICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{5,63}$")
_ERROR_NAME = re.compile(r"^[A-Z0-9_]{3,64}$")
_DEBUG_ID = re.compile(r"^[A-Za-z0-9_.-]{4,64}$")
_AUTH_REJECTIONS = frozenset({401, 403})            # credentials/permissions: refused before any processing
_STRUCTURED_REJECTIONS = frozenset({400, 404, 422})  # definitive only when PayPal's own error `name` is present


_MAX_ERROR_BODY_BYTES = 65_536  # an error body larger than this is not parsed: it is not a PayPal error object


def _error_body(response: httpx.Response) -> dict | None:
    """The response body as a JSON object, or None when it is unreadable, oversized, deeply nested or not an object. Never raises."""
    try:
        response.read()
        if len(response.content) > _MAX_ERROR_BODY_BYTES:
            return None
        body = response.json()
    except Exception:  # noqa: BLE001 - ValueError, RecursionError, decoding or stream errors: all mean "not a usable PayPal error object"
        return None
    return body if isinstance(body, dict) else None


def provider_debug_id(response: httpx.Response) -> str | None:
    """PayPal's `PayPal-Debug-Id` header (or body `debug_id`) when it is a short, plain token; never anything else. Safe to log and to show to support."""
    body = _error_body(response)
    candidates = [response.headers.get("paypal-debug-id"), body.get("debug_id") if body else None]
    return next((c for c in candidates if isinstance(c, str) and _DEBUG_ID.fullmatch(c)), None)


def provider_error_name(response: httpx.Response) -> str | None:
    """PayPal's error `name` when it is a plain upper-case token; never a message or any other body text."""
    body = _error_body(response)
    name = body.get("name") if body else None
    return name if isinstance(name, str) and _ERROR_NAME.fullmatch(name) else None


def is_definitive_rejection(response: httpx.Response) -> bool:
    """True only for a 4xx that PayPal itself answered as a clean rejection of a state-changing call.

    Any other 4xx (406, 415, 408, 409, ... or a body that is not a PayPal error object) may come from a gateway or from a response-negotiation
    failure after processing, so it must not be treated as "nothing happened"."""
    if response.status_code in _AUTH_REJECTIONS:
        return True
    return response.status_code in _STRUCTURED_REJECTIONS and provider_error_name(response) is not None
_MAX_RETRY_AFTER = 30.0

log = logging.getLogger("app.provider")


class _CreateResponseUnreadable(Exception):
    """A 2xx create response whose invoice id could not be established safely. `code` is a fixed, non-sensitive reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_paypal_host(host: str | None) -> bool:
    return bool(host) and (host == "paypal.com" or host.endswith(".paypal.com"))


def _id_from_link(href: object) -> str:
    """The invoice id from a PayPal invoice link. Strict about scheme, credentials, host suffix and path shape."""
    if not isinstance(href, str):
        raise _CreateResponseUnreadable("BAD_LINK")
    try:
        parsed = urlparse(href)
        _ = parsed.port  # raises ValueError on a malformed port
    except ValueError:
        raise _CreateResponseUnreadable("BAD_LINK") from None
    if parsed.scheme != "https" or parsed.username or parsed.password or not _is_paypal_host(parsed.hostname):
        raise _CreateResponseUnreadable("BAD_LINK")
    match = _LINK_PATH.match(parsed.path)
    if not match:
        raise _CreateResponseUnreadable("BAD_LINK")
    return _validated_id(match.group(1))


def _validated_id(value: object) -> str:
    if not isinstance(value, str) or not _INVOICE_ID.fullmatch(value):
        raise _CreateResponseUnreadable("BAD_ID")
    return value


def created_invoice_id(data: object) -> str:
    """Establish the new invoice's id from a create response.

    Accepts the documented shapes: a full representation (`id`, with `links`), or a bare link (`href`), or a `links`
    array containing rel=self. If more than one source is present they must agree; anything else fails closed."""
    if not isinstance(data, dict):
        raise _CreateResponseUnreadable("NOT_AN_OBJECT")
    found: list[str] = []
    if data.get("id") is not None:
        found.append(_validated_id(data["id"]))
    href = data.get("href")
    if href is None and isinstance(data.get("links"), list):
        href = next((link.get("href") for link in data["links"]
                     if isinstance(link, dict) and link.get("rel") == "self"), None)
    if href is not None:
        found.append(_id_from_link(href))
    if not found:
        raise _CreateResponseUnreadable("NO_ID")
    if len(set(found)) != 1:
        raise _CreateResponseUnreadable("ID_MISMATCH")
    return found[0]


def currency_exponent(currency: str) -> int:
    if currency not in SUPPORTED_CURRENCIES:
        raise ProviderError("unsupported currency")
    return 0 if currency in ZERO_DECIMAL_CURRENCIES else 2


def minor_to_value(amount_minor: int, currency: str) -> str:
    """800.00 USD <- 80000. Exact integer arithmetic."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor < 0:
        raise ProviderError("amount must be a non-negative integer")
    exponent = currency_exponent(currency)
    if exponent == 0:
        return str(amount_minor)
    whole, fraction = divmod(amount_minor, 10 ** exponent)
    return f"{whole}.{fraction:0{exponent}d}"


def value_to_minor(value: object, currency: str) -> int:
    """'800.00' -> 80000. Rejects non-decimal strings, negatives, and more decimals than the currency allows."""
    exponent = currency_exponent(currency)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,13}(\.[0-9]{1,6})?", value):
        raise ProviderError("malformed money value")
    try:
        scaled = Decimal(value).scaleb(exponent)
    except InvalidOperation as exc:  # pragma: no cover - the regex already guarantees a valid decimal
        raise ProviderError("malformed money value") from exc
    if scaled != scaled.to_integral_value():
        raise ProviderError("money value has more decimals than the currency allows")
    return int(scaled)


class PayPalSandboxProvider:
    name = PaymentProviderName.PAYPAL_SANDBOX

    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr | str,
        *,
        merchant_email: str | None = None,
        transport: httpx.BaseTransport | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
        token_skew_seconds: float = 60.0,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        secret = client_secret.get_secret_value() if isinstance(client_secret, SecretStr) else client_secret
        if not client_id or not secret:
            raise ValueError("PayPal sandbox client id and secret are required")
        self._client_id = client_id
        self._client_secret = secret
        self.expected_merchant_email = merchant_email.strip().lower() if merchant_email else None
        self._clock = clock
        self._sleep = sleep
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base
        self._skew = token_skew_seconds
        self._token_lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._client = httpx.Client(
            base_url=SANDBOX_BASE_URL, transport=transport, follow_redirects=False, trust_env=False,
            timeout=timeout or httpx.Timeout(15.0, connect=5.0),
        )

    def close(self) -> None:
        self._client.close()

    def authenticate(self) -> None:
        """Obtain (and cache) an access token. Raises ProviderAuthError on failure; no invoice call is made."""
        self._token()

    def __repr__(self) -> str:  # never expose credentials through repr/logging of the object
        return "PayPalSandboxProvider(environment='sandbox')"

    # --- OAuth 2.0 client credentials ---------------------------------------------------

    def _token(self) -> str:
        with self._token_lock:
            if self._access_token and self._clock() < self._token_expires_at:
                return self._access_token
            self._access_token = None
            response = self._call("oauth_token", "POST", TOKEN_PATH, safe=True,
                                  basic_auth=(self._client_id, self._client_secret),
                                  form={"grant_type": "client_credentials"})
            if response.status_code != 200:
                raise ProviderAuthError("token request refused")
            try:
                body = response.json()
                token, ttl = body["access_token"], int(body["expires_in"])
                if not isinstance(token, str) or not token or ttl <= 0 or body.get("token_type", "Bearer").lower() != "bearer":
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ProviderAuthError("token response invalid") from exc
            self._access_token = token
            self._token_expires_at = self._clock() + max(0.0, ttl - self._skew)
            return token

    def _invalidate_token(self, stale: str | None) -> None:
        with self._token_lock:
            if stale is None or self._access_token == stale:
                self._access_token = None
                self._token_expires_at = 0.0

    # --- HTTP with bounded, operation-aware retries --------------------------------------

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self._backoff_base * (2 ** (attempt - 1))
        if retry_after and retry_after.strip().isascii() and retry_after.strip().isdecimal():
            delay = max(delay, float(retry_after.strip()))
        delay = min(delay, _MAX_RETRY_AFTER)
        self._sleep(delay + random.uniform(0, 0.1 * delay))

    def _call(self, op: str, method: str, path: str, *, safe: bool, json_body: dict | None = None,
              headers: dict[str, str] | None = None, basic_auth: tuple[str, str] | None = None,
              form: dict[str, str] | None = None) -> httpx.Response:
        attempt, refreshed = 0, False
        while True:
            attempt += 1
            request_headers = {"Accept": "application/json", **(headers or {})}
            token = None
            kwargs: dict = {}
            if basic_auth is not None:
                kwargs["auth"] = basic_auth
            else:
                token = self._token()
                request_headers["Authorization"] = f"Bearer {token}"
            if form is not None:
                kwargs["data"] = form
            if json_body is not None:
                kwargs["json"] = json_body
            try:
                response = self._client.request(method, path, headers=request_headers, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                log.warning("provider_call op=%s outcome=not_sent attempt=%d", op, attempt)
                if attempt < self._max_attempts:
                    self._backoff(attempt, None)
                    continue
                raise ProviderUnavailable("could not reach the payment provider") from None
            except httpx.HTTPError:  # sent, but the outcome is unknown (read timeout, reset, protocol error)
                log.warning("provider_call op=%s outcome=no_response attempt=%d", op, attempt)
                if safe and attempt < self._max_attempts:
                    self._backoff(attempt, None)
                    continue
                if safe:
                    raise ProviderUnavailable("payment provider did not respond") from None
                raise ProviderAmbiguousOutcome("no response to a state-changing request") from None

            status = response.status_code
            debug = provider_debug_id(response) if status >= 400 else None
            log.info("provider_call op=%s status=%d attempt=%d%s", op, status, attempt, f" debug_id={debug}" if debug else "")
            if status == 401 and basic_auth is None:
                if refreshed:
                    raise ProviderAuthError("access token rejected after refresh")
                refreshed = True  # 401 means nothing was processed: one retry with a fresh token is always safe
                self._invalidate_token(token)
                attempt -= 1
                continue
            if status == 429:  # rejected before processing: safe to retry for every operation
                if attempt < self._max_attempts:
                    self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                raise ProviderRateLimited("rate limited by the payment provider")
            if status >= 500:
                if safe and attempt < self._max_attempts:
                    self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                if safe:
                    raise ProviderUnavailable(f"provider error {status}")
                raise ProviderAmbiguousOutcome(f"provider error {status} on a state-changing request")
            if not safe and not (200 <= status < 300 or 400 <= status < 500):
                # e.g. a redirect or 1xx/3xx on a state-changing call: we cannot know whether it was processed
                raise ProviderAmbiguousOutcome(f"unexpected status {status} on a state-changing request")
            if not safe and 400 <= status < 500 and not is_definitive_rejection(response):
                # e.g. 406/415/408/409 or a 4xx without a PayPal error body (a gateway answered): a 4xx alone does not prove nothing happened
                raise ProviderAmbiguousOutcome(f"unclassified status {status} on a state-changing request")
            return response

    @staticmethod
    def _reject(response: httpx.Response) -> ProviderError:
        """Map a 4xx to an exception carrying only a sanitized error name (never the body)."""
        if response.status_code in (401, 403):
            return ProviderAuthError("provider refused the credentials or permissions")
        code = "RESOURCE_NOT_FOUND" if response.status_code == 404 else f"HTTP_{response.status_code}"
        try:
            name = response.json().get("name")
            if isinstance(name, str) and _ERROR_NAME.fullmatch(name):
                code = name
        except (ValueError, AttributeError):
            pass
        return ProviderRejected(code)

    @staticmethod
    def _check_invoice_id(invoice_id: str) -> str:
        if not isinstance(invoice_id, str) or not _INVOICE_ID.fullmatch(invoice_id):
            raise ProviderError("invalid invoice id")
        return invoice_id

    # --- the port --------------------------------------------------------------------------

    def create_invoice(self, request: ProviderInvoiceRequest) -> ProviderInvoice:
        currency_exponent(request.currency)
        detail = {"invoice_number": request.invoice_number, "currency_code": request.currency}
        if request.reference:
            detail["reference"] = request.reference[:120]
        if request.memo:
            detail["memo"] = request.memo[:500]
        body = {
            "detail": detail,
            "primary_recipients": [{"billing_info": {"email_address": request.recipient_email}}],
            "items": [{
                "name": request.item_name[:200], "quantity": "1",
                "unit_amount": {"currency_code": request.currency,
                                "value": minor_to_value(request.amount_minor, request.currency)},
            }],
            "configuration": {"allow_tip": False},
        }
        if self.expected_merchant_email:
            # We assert the merchant instead of letting PayPal leave it out. PayPal documents that the invoicer email must be the
            # Business account tied to the access token; the read-back is still compared strictly in the verifier and reconcile.
            body["invoicer"] = {"email_address": self.expected_merchant_email}
        # POST is not safe to retry after an ambiguous failure; PayPal-Request-Id + the unique invoice number
        # let the *caller* resume deterministically with the same key.
        response = self._call("create_invoice", "POST", INVOICES_PATH, safe=False, json_body=body,
                              headers={"PayPal-Request-Id": request.idempotency_key,
                                           "Prefer": "return=representation"})  # documented: full invoice in the body
        if 200 <= response.status_code < 300 and response.status_code not in (200, 201):
            raise ProviderAmbiguousOutcome("unexpected success status on create")
        if response.status_code not in (200, 201):
            raise self._reject(response)
        data: object = None
        try:
            try:
                data = response.json()
            except ValueError:
                raise _CreateResponseUnreadable("NOT_JSON") from None
            invoice_id = created_invoice_id(data)
        except _CreateResponseUnreadable as exc:
            # The invoice almost certainly exists at PayPal but we could not learn its id: never retry, never guess.
            self._log_unreadable_create(exc.code, data)
            raise ProviderAmbiguousOutcome(f"create response unreadable: {exc.code}") from None
        try:
            return self.get_invoice(invoice_id)
        except ProviderError:
            # The invoice exists but could not be verified right now: tell the caller its id so it is never lost.
            raise ProviderAmbiguousOutcome("invoice created but not yet verified", invoice_id=invoice_id) from None

    @staticmethod
    def _log_unreadable_create(code: str, data: object) -> None:
        """Diagnosable but safe: only the reason code and top-level key NAMES (sanitized); never values, hosts or tokens."""
        keys = sorted(k for k in data if isinstance(k, str) and _SAFE_KEY.match(k))[:12] if isinstance(data, dict) else []
        log.warning("create_response_unreadable reason=%s keys=%s", code, ",".join(keys))

    def send_invoice(self, invoice_id: str) -> None:
        self._check_invoice_id(invoice_id)
        response = self._call("send_invoice", "POST", f"{INVOICES_PATH}/{invoice_id}/send", safe=False,
                              json_body={"send_to_recipient": True, "send_to_invoicer": False})
        if 200 <= response.status_code < 300 and response.status_code not in (200, 202):
            raise ProviderAmbiguousOutcome("unexpected success status on send")
        if response.status_code not in (200, 202):
            raise self._reject(response)

    def get_invoice(self, invoice_id: str) -> ProviderInvoice:
        self._check_invoice_id(invoice_id)
        response = self._call("get_invoice", "GET", f"{INVOICES_PATH}/{invoice_id}", safe=True)
        if response.status_code != 200:
            raise self._reject(response)
        try:
            return self._parse_invoice(response.json())
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderError("unreadable invoice response") from exc

    # --- response normalization (fail closed on anything unexpected) --------------------------

    @staticmethod
    def _transactions(container: object, id_key: str) -> tuple[ProviderTransaction, ...]:
        items = (container or {}).get("transactions") or []  # type: ignore[union-attr]
        out = []
        for item in items:
            kind = item.get("type")
            if kind not in ("PAYPAL", "EXTERNAL"):
                raise ProviderError("unrecognized transaction type")
            amount = item["amount"]
            currency = amount["currency_code"]
            txn_id = item.get(id_key)
            out.append(ProviderTransaction(txn_id if isinstance(txn_id, str) and txn_id else None, kind,
                                           value_to_minor(amount["value"], currency), currency))
        return tuple(out)

    def _parse_invoice(self, data: dict) -> ProviderInvoice:
        try:
            state = ProviderInvoiceState(data["status"])
        except ValueError as exc:
            raise ProviderError("unrecognized invoice status") from exc
        amount = data["amount"]
        currency = amount["currency_code"]
        recipients = data.get("primary_recipients") or []
        recipient = recipients[0]["billing_info"].get("email_address") if recipients else None
        merchant = (data.get("invoicer") or {}).get("email_address")
        return ProviderInvoice(
            invoice_id=self._check_invoice_id(data["id"]),
            invoice_number=(data.get("detail") or {}).get("invoice_number"),
            state=state,
            currency=currency,
            total_minor=value_to_minor(amount["value"], currency),
            payments=self._transactions(data.get("payments"), "payment_id"),
            refunds=self._transactions(data.get("refunds"), "refund_id"),
            recipient_email=recipient,
            merchant_email=merchant,
        )
