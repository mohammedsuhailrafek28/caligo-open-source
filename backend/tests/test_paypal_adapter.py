"""PayPal sandbox adapter against a fake PayPal served through httpx.MockTransport (no network)."""
import logging
import socket

import httpx
import pytest

from app.payment_provider import (
    ProviderAmbiguousOutcome, ProviderAuthError, ProviderError, ProviderInvoiceRequest, ProviderInvoiceState,
    ProviderRateLimited, ProviderRejected, ProviderUnavailable,
)
from app.paypal_sandbox import PayPalSandboxProvider, minor_to_value, value_to_minor
from tests.fake_paypal import CLIENT_ID, CLIENT_SECRET, MERCHANT, FakePayPal

REQ = ProviderInvoiceRequest("pem-key-0001", "PEM-NUMBER-0001", "USD", 80000, "buyer@example.org", "Site", "memo", "ref")
CREATE = ("POST", "/v2/invoicing/invoices")


# --- 1. OAuth --------------------------------------------------------------------------------

def test_oauth_success_token_is_cached_and_sent_as_bearer(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    invoice_id = fake.only()["id"]
    sandbox_provider.get_invoice(invoice_id)
    assert fake.tokens_issued == 1
    assert fake.count("POST", "/v1/oauth2/token") == 1
    assert all(h.startswith("Bearer fake-token-1-") for h in fake.auth_headers)


def test_oauth_failure_raises_auth_error_and_makes_no_api_call(fake, clock, sleeps):
    bad = PayPalSandboxProvider(CLIENT_ID, "wrong-credential-value", transport=fake.transport(), clock=clock, sleep=sleeps.append)
    with pytest.raises(ProviderAuthError):
        bad.create_invoice(REQ)
    assert fake.count("POST", "/v2/invoicing/invoices") == 0
    assert fake.invoices == {}


def test_missing_credentials_are_refused_at_construction():
    with pytest.raises(ValueError):
        PayPalSandboxProvider("", "")


# --- 2. token expiry and renewal ---------------------------------------------------------------

def test_token_is_renewed_shortly_before_expiry(sandbox_provider, fake, clock):
    sandbox_provider.create_invoice(REQ)
    invoice_id = fake.only()["id"]
    clock.t += 3600 - 61  # inside the validity window minus the 60 s safety skew
    sandbox_provider.get_invoice(invoice_id)
    assert fake.tokens_issued == 1
    clock.t += 5  # now inside the skew: must renew before the old token can expire mid-request
    sandbox_provider.get_invoice(invoice_id)
    assert fake.tokens_issued == 2
    assert fake.auth_headers[-1].startswith("Bearer fake-token-2-")


def test_revoked_token_is_refreshed_once_then_succeeds(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    invoice_id = fake.only()["id"]
    fake.valid_token = "revoked-elsewhere"  # PayPal now rejects the cached token
    assert sandbox_provider.get_invoice(invoice_id).invoice_id == invoice_id
    assert fake.tokens_issued == 2


# --- 3. invoice creation ----------------------------------------------------------------------

def test_create_returns_a_verified_draft_with_exact_money(sandbox_provider, fake):
    invoice = sandbox_provider.create_invoice(REQ)
    assert invoice.state is ProviderInvoiceState.DRAFT
    assert (invoice.currency, invoice.total_minor) == ("USD", 80000)
    assert invoice.invoice_number == "PEM-NUMBER-0001"
    assert invoice.recipient_email == "buyer@example.org" and invoice.merchant_email == "merchant@example.com"
    body = fake.only()["request_body"]
    assert body["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "800.00"}
    assert fake.count("POST", "/send") == 0  # creating never emails anyone


# --- 4. idempotency and ambiguous outcomes -----------------------------------------------------

def test_same_request_id_never_creates_a_second_invoice(sandbox_provider, fake):
    first = sandbox_provider.create_invoice(REQ)
    again = sandbox_provider.create_invoice(REQ)
    assert first.invoice_id == again.invoice_id and len(fake.invoices) == 1


def test_timeout_after_processing_is_ambiguous_not_retried_and_resumable(sandbox_provider, fake):
    fake.fault(*CREATE, "create_then_timeout")
    with pytest.raises(ProviderAmbiguousOutcome) as caught:
        sandbox_provider.create_invoice(REQ)
    assert caught.value.invoice_id is None
    assert fake.count("POST", "/v2/invoicing/invoices") == 1  # a state-changing call is never retried blindly
    resumed = sandbox_provider.create_invoice(REQ)  # same PayPal-Request-Id -> the SAME invoice
    assert len(fake.invoices) == 1 and resumed.invoice_id == fake.only()["id"]


def test_server_error_on_create_is_ambiguous_and_not_retried(sandbox_provider, fake):
    fake.fault(*CREATE, 500)
    with pytest.raises(ProviderAmbiguousOutcome):
        sandbox_provider.create_invoice(REQ)
    assert fake.count("POST", "/v2/invoicing/invoices") == 1


def test_created_but_unverifiable_keeps_the_invoice_id(sandbox_provider, fake):
    fake.fault("GET", "", 500, 500, 500)
    with pytest.raises(ProviderAmbiguousOutcome) as caught:
        sandbox_provider.create_invoice(REQ)
    assert caught.value.invoice_id == fake.only()["id"]


def test_duplicate_invoice_number_with_new_request_id_is_rejected(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    other = ProviderInvoiceRequest("pem-key-0002", REQ.invoice_number, "USD", 80000, "buyer@example.org")
    with pytest.raises(ProviderRejected) as caught:
        sandbox_provider.create_invoice(other)
    assert caught.value.code == "DUPLICATE_INVOICE_NUMBER" and len(fake.invoices) == 1


def test_unreachable_before_sending_is_retried_then_succeeds(sandbox_provider, fake, sleeps):
    fake.fault("POST", "/v1/oauth2/token", "connect")
    sandbox_provider.create_invoice(REQ)
    assert len(sleeps) == 1 and len(fake.invoices) == 1


# --- 5/11. send, rate limits, server errors, timeouts -------------------------------------------

def test_send_moves_a_draft_to_sent(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    invoice = fake.only()
    sandbox_provider.send_invoice(invoice["id"])
    assert invoice["status"] == "SENT" and fake.count("POST", "/send") == 1


def test_send_of_a_non_draft_is_rejected_by_the_provider(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    invoice = fake.only()
    sandbox_provider.send_invoice(invoice["id"])
    with pytest.raises(ProviderRejected) as caught:
        sandbox_provider.send_invoice(invoice["id"])
    assert caught.value.code == "INVALID_INVOICE_STATUS"


def test_send_timeout_is_ambiguous_and_never_retried(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    fake.fault("POST", "/send", "timeout")
    with pytest.raises(ProviderAmbiguousOutcome):
        sandbox_provider.send_invoice(fake.only()["id"])
    assert fake.count("POST", "/send") == 1


def test_rate_limit_is_retried_with_backoff_and_retry_after(sandbox_provider, fake, sleeps):
    fake.fault(*CREATE, (429, {"Retry-After": "2"}))
    sandbox_provider.create_invoice(REQ)
    assert fake.count("POST", "/v2/invoicing/invoices") == 2 and len(fake.invoices) == 1
    assert sleeps and sleeps[0] >= 2


def test_persistent_rate_limit_is_bounded(sandbox_provider, fake):
    fake.fault(*CREATE, 429, 429, 429, 429)
    with pytest.raises(ProviderRateLimited):
        sandbox_provider.create_invoice(REQ)
    assert fake.count("POST", "/v2/invoicing/invoices") == 3 and fake.invoices == {}


def test_safe_get_retries_server_errors_then_recovers(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    fake.fault("GET", "", 500, 503)
    assert sandbox_provider.get_invoice(fake.only()["id"]).state is ProviderInvoiceState.DRAFT


def test_get_gives_up_after_bounded_attempts(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    fake.fault("GET", "", 500, 500, 500, 500)
    with pytest.raises(ProviderUnavailable):
        sandbox_provider.get_invoice(fake.only()["id"])


def test_get_timeout_is_retried_as_a_safe_read(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    fake.fault("GET", "", "timeout")
    assert sandbox_provider.get_invoice(fake.only()["id"]).invoice_id == fake.only()["id"]


def test_persistent_connection_failure_is_unavailable(sandbox_provider, fake):
    fake.fault("POST", "/v1/oauth2/token", "connect", "connect", "connect")
    with pytest.raises(ProviderUnavailable):
        sandbox_provider.create_invoice(REQ)


# --- exact money ---------------------------------------------------------------------------------

@pytest.mark.parametrize("minor,currency,text", [
    (80000, "USD", "800.00"), (5, "USD", "0.05"), (0, "USD", "0.00"), (123456789, "EUR", "1234567.89"),
    (500, "JPY", "500"), (7, "HUF", "7"),
])
def test_minor_units_round_trip_exactly(minor, currency, text):
    assert minor_to_value(minor, currency) == text
    assert value_to_minor(text, currency) == minor


@pytest.mark.parametrize("value,currency", [
    ("1e3", "USD"), ("-1.00", "USD"), ("1.005", "USD"), ("1.5", "JPY"), ("", "USD"), ("1,000.00", "USD"),
    (800.0, "USD"), (None, "USD"), ("10.00", "XXX"),
])
def test_malformed_or_inexact_money_is_rejected(value, currency):
    with pytest.raises(ProviderError):
        value_to_minor(value, currency)


def test_floats_and_bools_are_not_valid_amounts():
    for bad in (1.5, True, -1):
        with pytest.raises(ProviderError):
            minor_to_value(bad, "USD")


# --- fail closed on unexpected provider data ------------------------------------------------------

def test_unrecognized_status_or_transaction_type_is_rejected(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    invoice = fake.only()
    invoice["status"] = "SOMETHING_NEW"
    with pytest.raises(ProviderError):
        sandbox_provider.get_invoice(invoice["id"])
    invoice["status"] = "PAID"
    invoice["payments"]["transactions"].append({"payment_id": "P1", "type": "CRYPTO", "amount": {"currency_code": "USD", "value": "1.00"}})
    with pytest.raises(ProviderError):
        sandbox_provider.get_invoice(invoice["id"])


def test_create_link_pointing_at_another_host_is_not_followed(clock, sleeps):
    fake = FakePayPal()
    inner = fake.handle

    def handler(request: httpx.Request) -> httpx.Response:
        response = inner(request)
        if request.url.path == "/v2/invoicing/invoices" and request.method == "POST":
            return httpx.Response(201, json={"href": "https://evil.example.net/v2/invoicing/invoices/INV2-0001-AAAAAAAA"})
        return response

    provider = PayPalSandboxProvider(CLIENT_ID, CLIENT_SECRET, transport=httpx.MockTransport(handler), clock=clock, sleep=sleeps.append)
    with pytest.raises(ProviderAmbiguousOutcome):
        provider.create_invoice(REQ)
    assert fake.count("GET", "AAAAAAAA") == 0


def test_invoice_ids_are_validated_before_being_put_in_a_path(sandbox_provider):
    for bad in ("../x", "a/b", "", "x" * 200):
        with pytest.raises(ProviderError):
            sandbox_provider.get_invoice(bad)


# --- 13/14. secrets never exposed, no real network ------------------------------------------------

def test_credentials_and_tokens_never_reach_logs_repr_or_errors(sandbox_provider, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.fault(*CREATE, 429)
    sandbox_provider.create_invoice(REQ)
    fake.valid_token = "rotated"
    sandbox_provider.get_invoice(fake.only()["id"])
    token = fake.valid_token
    with pytest.raises(ProviderAuthError) as caught:
        PayPalSandboxProvider(CLIENT_ID, "wrong-credential-value", transport=fake.transport(), sleep=lambda _s: None).get_invoice("INV2-0001-ABCDEFGH")
    haystack = caplog.text + repr(sandbox_provider) + str(caught.value)
    for secret in (CLIENT_SECRET, token, "Bearer", "wrong-credential-value"):
        assert secret not in haystack


def test_adapter_has_no_live_mode_and_targets_only_the_sandbox_host(sandbox_provider):
    assert str(sandbox_provider._client.base_url).startswith("https://api-m.sandbox.paypal.com")
    assert "live" not in repr(sandbox_provider).lower().replace("sandbox", "")


def test_unit_tests_cannot_reach_the_real_internet():
    real = PayPalSandboxProvider(CLIENT_ID, CLIENT_SECRET, sleep=lambda _s: None)  # deliberately no mock transport
    with pytest.raises((AssertionError, ProviderError)):
        real.get_invoice("INV2-0001-ABCDEFGH")


def test_unexpected_status_on_a_state_changing_call_is_ambiguous(sandbox_provider, fake):
    fake.fault(*CREATE, 202)
    with pytest.raises(ProviderAmbiguousOutcome):
        sandbox_provider.create_invoice(REQ)


def test_unicode_digits_are_not_money():
    with pytest.raises(ProviderError):
        value_to_minor("\u0668\u0660\u0660.\u0660\u0660", "USD")


def test_odd_retry_after_values_do_not_crash_backoff(sandbox_provider, fake):
    fake.fault(*CREATE, (429, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}))
    assert sandbox_provider.create_invoice(REQ).invoice_id


def test_second_401_after_refresh_is_an_auth_error(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    invoice_id = fake.only()["id"]
    fake.fault("GET", "", 401, 401)
    with pytest.raises(ProviderAuthError):
        sandbox_provider.get_invoice(invoice_id)


def test_connect_failure_on_the_create_post_is_retried_safely(sandbox_provider, fake):
    fake.fault(*CREATE, "connect")
    sandbox_provider.create_invoice(REQ)
    assert len(fake.invoices) == 1


# --- create-response parsing (Stage 2D incident: draft created at PayPal, adapter reported "unreadable") ------------------

@pytest.mark.parametrize("style", ["href", "legacy_host", "link_description", "links", "representation"])
def test_every_documented_create_response_shape_yields_the_invoice(sandbox_provider, fake, style):
    fake.create_style = style
    invoice = sandbox_provider.create_invoice(REQ)
    assert invoice.invoice_id == fake.only()["id"] and invoice.state is ProviderInvoiceState.DRAFT
    assert len(fake.invoices) == 1 and fake.count("POST", "/send") == 0


def test_create_asks_for_the_full_representation(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    assert fake.create_headers[0]["prefer"] == "return=representation"
    assert fake.create_headers[0]["paypal-request-id"] == REQ.idempotency_key


def _respond(response_factory):
    def factory(_invoice_id):
        return response_factory(_invoice_id)
    return factory


@pytest.mark.parametrize("reason,make", [
    ("NOT_JSON", lambda i: httpx.Response(201, content=b"<html>created</html>")),
    ("NOT_JSON", lambda i: httpx.Response(201, content=b"")),
    ("NOT_AN_OBJECT", lambda i: httpx.Response(201, json=["x"])),
    ("NO_ID", lambda i: httpx.Response(201, json={})),
    ("NO_ID", lambda i: httpx.Response(201, json={"links": []})),
    ("BAD_LINK", lambda i: httpx.Response(201, json={"href": 12345})),
    ("BAD_LINK", lambda i: httpx.Response(201, json={"href": "https://api.sandbox.paypal.com/v1/invoicing/invoices/" + i})),
    ("BAD_LINK", lambda i: httpx.Response(201, json={"href": f"https://api.sandbox.paypal.com/v2/invoicing/invoices/{i}/send"})),
    ("BAD_LINK", lambda i: httpx.Response(201, json={"href": f"http://api.sandbox.paypal.com/v2/invoicing/invoices/{i}"})),
    ("BAD_LINK", lambda i: httpx.Response(201, json={"href": f"https://evil.example.net/v2/invoicing/invoices/{i}"})),
    ("BAD_LINK", lambda i: httpx.Response(201, json={"href": f"https://paypal.com.evil.net/v2/invoicing/invoices/{i}"})),
    ("BAD_LINK", lambda i: httpx.Response(201, json={"href": f"https://evilpaypal.com/v2/invoicing/invoices/{i}"})),
    ("BAD_LINK", lambda i: httpx.Response(201, json={"href": f"https://user:pw@api.sandbox.paypal.com/v2/invoicing/invoices/{i}"})),
    ("BAD_ID", lambda i: httpx.Response(201, json={"id": "../../x"})),
    ("BAD_ID", lambda i: httpx.Response(201, json={"id": 42})),
    ("BAD_ID", lambda i: httpx.Response(201, json={"href": "https://api.sandbox.paypal.com/v2/invoicing/invoices/a b"})),
    ("ID_MISMATCH", lambda i: httpx.Response(201, json={"id": "INV2-9999-ZZZZZZZZ", "href": f"https://api.sandbox.paypal.com/v2/invoicing/invoices/{i}"})),
])
def test_malformed_create_responses_are_ambiguous_with_a_reason_and_trigger_no_further_calls(sandbox_provider, fake, reason, make):
    fake.create_response = _respond(make)
    with pytest.raises(ProviderAmbiguousOutcome) as caught:
        sandbox_provider.create_invoice(REQ)
    assert caught.value.invoice_id is None and reason in str(caught.value)
    assert fake.count("POST", "/v2/invoicing/invoices") == 1  # never retried
    assert not [c for c in fake.calls if c[0] == "GET"]       # never fetched an attacker-influenced URL
    assert len(fake.invoices) == 1                            # the invoice does exist: the caller must treat it as uncertain


def test_unreadable_create_response_is_diagnosable_without_leaking_data(sandbox_provider, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.create_response = lambda i: httpx.Response(201, json={"surprise": {"secret": "do-not-log-this"}, "href": f"https://x.example.net/{i}"})
    with pytest.raises(ProviderAmbiguousOutcome):
        sandbox_provider.create_invoice(REQ)
    text = caplog.text
    assert "create_response_unreadable" in text and "reason=BAD_LINK" in text and "surprise" in text
    assert "do-not-log-this" not in text and "x.example.net" not in text


def test_sandbox_and_legacy_paypal_hosts_are_accepted_but_lookalikes_are_not(sandbox_provider, fake):
    for host in ("api-m.sandbox.paypal.com", "api.sandbox.paypal.com"):
        fake.invoices.clear()
        fake.by_request_id.clear()
        fake.create_response = lambda i, host=host: httpx.Response(201, json={"href": f"https://{host}/v2/invoicing/invoices/{i}"})
        assert sandbox_provider.create_invoice(REQ).invoice_id == fake.only()["id"]


def test_follow_up_requests_always_use_the_fixed_sandbox_host_even_if_the_link_names_another(sandbox_provider, fake):
    fake.create_response = lambda i: httpx.Response(201, json={"href": f"https://api.paypal.com/v2/invoicing/invoices/{i}"})
    invoice = sandbox_provider.create_invoice(REQ)  # the fake asserts every request host is the sandbox API host
    assert invoice.invoice_id == fake.only()["id"]


def test_created_but_unverifiable_still_keeps_the_id_for_every_shape(sandbox_provider, fake):
    fake.create_style = "legacy_host"
    fake.fault("GET", "", 500, 500, 500)
    with pytest.raises(ProviderAmbiguousOutcome) as caught:
        sandbox_provider.create_invoice(REQ)
    assert caught.value.invoice_id == fake.only()["id"]


def test_create_timeout_and_server_error_remain_ambiguous_without_an_id(sandbox_provider, fake):
    for action in ("timeout", 500, "create_then_timeout"):
        fake.fault(*CREATE, action)
        with pytest.raises(ProviderAmbiguousOutcome) as caught:
            sandbox_provider.create_invoice(ProviderInvoiceRequest("pem-key-" + str(action), "PEM-" + str(action).upper(),
                                                                    "USD", 100, "buyer@example.org"))
        assert caught.value.invoice_id is None


# --- explicit invoicer on create (merchant identity asserted by us, not left to PayPal's default) ---------------------------

def test_create_sends_the_configured_merchant_as_the_invoicer(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    assert fake.create_bodies[0]["invoicer"] == {"email_address": MERCHANT}
    assert set(fake.create_bodies[0]["invoicer"]) == {"email_address"}  # nothing else about the merchant is invented


def test_the_invoicer_email_is_normalized_like_the_comparison(fake, clock, sleeps):
    provider = PayPalSandboxProvider(CLIENT_ID, CLIENT_SECRET, merchant_email="  MERCHANT@Example.COM ",
                                     transport=fake.transport(), clock=clock, sleep=sleeps.append)
    provider.create_invoice(REQ)
    assert fake.create_bodies[0]["invoicer"]["email_address"] == MERCHANT


def test_without_a_configured_merchant_no_invoicer_is_sent(fake, clock, sleeps):
    provider = PayPalSandboxProvider(CLIENT_ID, CLIENT_SECRET, transport=fake.transport(), clock=clock, sleep=sleeps.append)
    provider.create_invoice(REQ)
    assert "invoicer" not in fake.create_bodies[0]


def test_a_matching_response_round_trips_the_merchant(sandbox_provider, fake):
    invoice = sandbox_provider.create_invoice(REQ)
    assert invoice.merchant_email == MERCHANT and fake.only()["invoicer"] == {"email_address": MERCHANT}


@pytest.mark.parametrize("mode,expected", [("omit", None), ("empty", None), ("other", "someone-else@example.com")])
def test_a_missing_or_different_merchant_in_the_response_is_reported_as_is_never_filled_in(sandbox_provider, fake, mode, expected):
    fake.invoicer_mode = mode
    invoice = sandbox_provider.create_invoice(REQ)
    assert invoice.merchant_email == expected  # the adapter does not substitute the configured address


def test_paypal_rejecting_the_invoicer_creates_nothing_and_is_a_clean_rejection(fake, clock, sleeps):
    provider = PayPalSandboxProvider(CLIENT_ID, CLIENT_SECRET, merchant_email="not-the-token-account@example.com",
                                     transport=fake.transport(), clock=clock, sleep=sleeps.append)
    with pytest.raises(ProviderRejected) as caught:
        provider.create_invoice(REQ)
    assert caught.value.code == "INVALID_PARAMETER_VALUE" and not isinstance(caught.value, ProviderAmbiguousOutcome)
    assert fake.invoices == {} and fake.count("POST", "/v2/invoicing/invoices") == 1 and fake.count("POST", "/send") == 0


# --- an uncertain 4xx on a state-changing call is never "nothing happened" -----------------------------------------------------

@pytest.mark.parametrize("status", [406, 415, 408, 409])
def test_send_with_an_uncertain_4xx_is_ambiguous_and_not_retried(sandbox_provider, fake, status):
    sandbox_provider.create_invoice(REQ)
    fake.fault("POST", "/send", status)
    with pytest.raises(ProviderAmbiguousOutcome):
        sandbox_provider.send_invoice(fake.only()["id"])
    assert fake.count("POST", "/send") == 1


def test_send_with_a_4xx_that_has_no_paypal_error_name_is_ambiguous(fake, clock, sleeps):
    def handler(request):
        return httpx.Response(422, text="<html>gateway</html>") if request.url.path.endswith("/send") else fake.handle(request)

    provider = PayPalSandboxProvider(CLIENT_ID, CLIENT_SECRET, merchant_email=MERCHANT, transport=httpx.MockTransport(handler), clock=clock, sleep=sleeps.append)
    provider.create_invoice(REQ)
    with pytest.raises(ProviderAmbiguousOutcome):
        provider.send_invoice(fake.only()["id"])


@pytest.mark.parametrize("status", [400, 404, 422])
def test_send_with_a_structured_paypal_rejection_is_still_a_clean_rejection(sandbox_provider, fake, status):
    sandbox_provider.create_invoice(REQ)
    fake.fault("POST", "/send", status)
    with pytest.raises(ProviderRejected):
        sandbox_provider.send_invoice(fake.only()["id"])


def test_a_read_with_a_406_stays_a_safe_unavailable_error(sandbox_provider, fake):
    sandbox_provider.create_invoice(REQ)
    fake.fault("GET", fake.only()["id"], 406)
    with pytest.raises(ProviderRejected):
        sandbox_provider.get_invoice(fake.only()["id"])


# --- hostile error bodies never break classification (security review L1) -----------------------------------------------------

@pytest.mark.parametrize("body", [b"[" * 200_000, b"x" * 2_000_000, b'{"name": "' + b"A" * 2_000_000 + b'"}', b"\xff\xfe\x00"])
def test_a_hostile_or_huge_error_body_is_never_a_clean_rejection_and_never_raises(body):
    from app.paypal_sandbox import is_definitive_rejection, provider_debug_id, provider_error_name
    response = httpx.Response(422, content=body)
    assert is_definitive_rejection(response) is False
    assert provider_error_name(response) is None and provider_debug_id(response) is None


def test_a_well_formed_small_error_body_is_still_a_clean_rejection():
    from app.paypal_sandbox import is_definitive_rejection, provider_debug_id
    response = httpx.Response(422, json={"name": "INVALID_INVOICE_STATUS", "debug_id": "abc123def456"})
    assert is_definitive_rejection(response) is True and provider_debug_id(response) == "abc123def456"
