"""The read-only Sandbox invoice verifier, exercised only against the fake PayPal (no network, no real credentials)."""
import re
from pathlib import Path

import httpx
import pytest

from app import sandbox_readonly as ro
from app import sandbox_smoke as smoke
from app.payment_provider import ProviderInvoiceRequest
from tests.fake_paypal import CLIENT_ID, CLIENT_SECRET, MERCHANT, FakePayPal

BUYER = "buyer@example.org"
NUMBER = "SMOKE-0123456789AB"
ENV = {
    "PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID, "PAYPAL_SANDBOX_CLIENT_SECRET": CLIENT_SECRET,
    "PAYPAL_SANDBOX_MERCHANT_EMAIL": MERCHANT, "PAYPAL_SMOKE_BUYER_EMAIL": BUYER,
    "PAYPAL_READONLY_CONFIRM": ro.CONFIRM_VALUE,
}
TOKEN = ("POST", "/v1/oauth2/token")
APP_DIR = Path(ro.__file__).resolve().parent


@pytest.fixture()
def seeded(fake, sandbox_provider):
    """A DRAFT invoice like the one in the Sandbox dashboard. Returns its id; the fake's call log starts empty."""
    created = sandbox_provider.create_invoice(
        ProviderInvoiceRequest("seed-key-1", NUMBER, "USD", 100, BUYER, "Synthetic smoke-test item", "memo", "ref"))
    fake.calls.clear()
    fake.auth_headers.clear()
    fake.valid_token = None  # the verifier must authenticate on its own
    return created.invoice_id


def go(fake, invoice_id, *, env=None, execute=True, number=NUMBER, amount="1.00", currency="USD"):
    lines: list[str] = []
    code = ro.run(ENV if env is None else env, invoice_id=invoice_id, invoice_number=number, amount=amount,
                  currency=currency, execute=execute, out=lines.append, transport=fake.transport())
    return code, "\n".join(lines)


def invoice_get(invoice_id):
    return ("GET", f"/v2/invoicing/invoices/{invoice_id}")


# --- default is a dry run ----------------------------------------------------------------------------------------

def test_default_run_is_a_dry_run_with_no_network(fake, seeded):
    code, output = go(fake, seeded, execute=False)
    assert code == ro.EXIT_OK and "DRY RUN" in output and fake.calls == []


def test_execute_needs_the_confirmation_variable(fake, seeded):
    code, output = go(fake, seeded, env={k: v for k, v in ENV.items() if k != "PAYPAL_READONLY_CONFIRM"})
    assert code == ro.EXIT_REFUSED and "PAYPAL_READONLY_CONFIRM" in output and fake.calls == []


def test_the_smoke_confirmation_is_not_accepted(fake, seeded):
    env = {k: v for k, v in ENV.items() if k != "PAYPAL_READONLY_CONFIRM"}
    env["PAYPAL_SMOKE_CONFIRM"] = smoke.CONFIRM_VALUE
    assert go(fake, seeded, env=env)[0] == ro.EXIT_REFUSED and fake.calls == []


def test_missing_variables_are_named_but_never_their_values(fake, seeded):
    code, output = go(fake, seeded, env={"PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID})
    assert code == ro.EXIT_REFUSED and "PAYPAL_SANDBOX_CLIENT_SECRET" in output and CLIENT_ID not in output
    assert fake.calls == []


@pytest.mark.parametrize("kwargs", [
    {"invoice_id": "../../x"}, {"invoice_id": "a b"}, {"invoice_id": ""}, {"invoice_id": "INV2-1/send"},
    {"number": ""}, {"number": "x" * 80}, {"number": "bad number!"},
    {"amount": "1,00"}, {"amount": "abc"}, {"amount": "-1.00"}, {"amount": "1.005"}, {"amount": ""},
    {"currency": "usd"}, {"currency": "XXX"}, {"currency": "US"},
])
def test_invalid_expectations_are_refused_before_any_network_call(fake, seeded, kwargs):
    call = {"invoice_id": seeded, **kwargs}
    invoice_id = call.pop("invoice_id")
    code, _ = go(fake, invoice_id, **call)
    assert code == ro.EXIT_REFUSED and fake.calls == []


# --- the happy path: exactly OAuth + one GET of exactly that invoice ----------------------------------------------

def test_happy_path_makes_exactly_one_oauth_and_one_get_of_the_named_invoice(fake, seeded):
    code, output = go(fake, seeded)
    assert code == ro.EXIT_OK and "RESULT: PASS" in output
    assert fake.calls == [TOKEN, invoice_get(seeded)]
    assert "[FAIL]" not in output and output.count("[PASS]") == 11
    invoice = fake.only()
    assert invoice["status"] == "DRAFT" and invoice["payments"]["transactions"] == [] and invoice["refunds"]["transactions"] == []


def test_happy_path_output_contains_no_secret_token_or_full_email(fake, seeded):
    _, output = go(fake, seeded)
    for forbidden in (CLIENT_ID, CLIENT_SECRET, MERCHANT, BUYER, "Bearer ", "Basic "):
        assert forbidden not in output
    assert fake.auth_headers and all(h.startswith("Bearer ") for h in fake.auth_headers)
    assert fake.auth_headers[0].split(" ", 1)[1] not in output


@pytest.mark.parametrize("label,kwargs", [
    ("invoice number matches", {"number": "SMOKE-000000000000"}),
    ("total amount matches", {"amount": "2.00"}),
    ("currency matches", {"currency": "EUR"}),
])
def test_each_expectation_mismatch_fails_verification(fake, seeded, label, kwargs):
    code, output = go(fake, seeded, **kwargs)
    assert code == ro.EXIT_VERIFY_FAILED and f"[FAIL] {label}" in output and "RESULT: FAIL" in output
    assert fake.calls == [TOKEN, invoice_get(seeded)]


@pytest.mark.parametrize("label,env_change", [
    ("merchant is the configured sandbox business account", {"PAYPAL_SANDBOX_MERCHANT_EMAIL": "other-merchant@example.com"}),
    ("recipient is the configured buyer", {"PAYPAL_SMOKE_BUYER_EMAIL": "other-buyer@example.org"}),
])
def test_wrong_identity_fails_verification_without_printing_either_address(fake, seeded, label, env_change):
    code, output = go(fake, seeded, env={**ENV, **env_change})
    assert code == ro.EXIT_VERIFY_FAILED and f"[FAIL] {label}" in output
    for address in (MERCHANT, BUYER, *env_change.values()):
        assert address not in output


@pytest.mark.parametrize("mutate", [
    lambda inv: inv.pop("invoicer"),
    lambda inv: inv.__setitem__("invoicer", {}),
    lambda inv: inv.__setitem__("primary_recipients", []),
])
def test_missing_identity_evidence_fails_closed(fake, seeded, mutate):
    mutate(fake.only())
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and "[FAIL]" in output


def test_a_sent_invoice_fails_the_draft_check(fake, seeded):
    fake.only()["status"] = "SENT"
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and "[FAIL] status is DRAFT" in output and "SENT" in output


def test_a_payment_is_detected(fake, seeded):
    fake.pay(fake.only())
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED
    assert "[FAIL] status is DRAFT" in output and "[FAIL] no payments recorded" in output


def test_an_external_payment_is_detected_even_if_the_status_still_says_draft(fake, seeded):
    invoice = fake.only()
    fake.pay(invoice, kind="EXTERNAL", status="DRAFT")
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and "[FAIL] no payments recorded" in output


def test_a_refund_is_detected(fake, seeded):
    invoice = fake.only()
    fake.pay(invoice)
    fake.refund(invoice)
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and "[FAIL] no refunds recorded" in output


def test_the_returned_invoice_id_must_match_the_requested_one(fake, seeded):
    fake.only()["id"] = "INV2-9999-ZZZZZZZZ"
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and "[FAIL] invoice id matches" in output


# --- unreadable or unexpected responses: reason code and key names only -----------------------------------------------

def test_unrecognized_status_is_reported_without_leaking_other_values(fake, seeded):
    invoice = fake.only()
    invoice["status"] = "WEIRD_STATUS"
    invoice["surprise"] = {"secret": "do-not-print-this"}
    code, output = go(fake, seeded)
    assert code == ro.EXIT_UNREADABLE and "UNREADABLE" in output
    assert "WEIRD_STATUS" in output and "surprise" in output
    assert "do-not-print-this" not in output and MERCHANT not in output and BUYER not in output


@pytest.mark.parametrize("mutate", [
    lambda inv: inv.__setitem__("amount", {"currency_code": "USD", "value": "1,00"}),
    lambda inv: inv.pop("amount"),
    lambda inv: inv.pop("status"),
])
def test_malformed_invoice_bodies_fail_closed_as_unreadable(fake, seeded, mutate):
    mutate(fake.only())
    code, output = go(fake, seeded)
    assert code == ro.EXIT_UNREADABLE and "UNREADABLE" in output and "RESULT: PASS" not in output
    assert fake.calls == [TOKEN, invoice_get(seeded)]


def test_a_non_json_body_is_unreadable_and_not_echoed(fake, seeded):
    original = fake.handle

    def handle(request):
        if request.method == "GET":
            fake.calls.append((request.method, request.url.path))
            return httpx.Response(200, content=b"<html>Authorization: Bearer abcdefghijklmnop</html>")
        return original(request)

    fake.handle = handle
    code, output = go(fake, seeded)
    assert code == ro.EXIT_UNREADABLE and "abcdefghijklmnop" not in output and "<html>" not in output


# --- provider failures: one attempt, never retried ---------------------------------------------------------------------

def test_oauth_failure_makes_no_invoice_call_and_hides_the_secret(fake, seeded):
    code, output = go(fake, seeded, env={**ENV, "PAYPAL_SANDBOX_CLIENT_SECRET": "wrong-example-secret-value"})
    assert code == ro.EXIT_PROVIDER_ERROR and "OAuth FAILED" in output and "wrong-example-secret-value" not in output
    assert fake.calls == [TOKEN]


@pytest.mark.parametrize("action,expected", [(404, "SIMULATED"), (403, "ProviderAuthError"),
                                             (429, "ProviderRateLimited"), (500, "ProviderUnavailable")])
def test_get_failures_are_reported_and_never_retried(fake, seeded, action, expected):
    fake.fault("GET", seeded, action)
    code, output = go(fake, seeded)
    assert code == ro.EXIT_PROVIDER_ERROR and expected in output
    assert fake.calls == [TOKEN, invoice_get(seeded)]


def test_an_unknown_invoice_id_is_reported_as_not_found(fake, seeded):
    code, output = go(fake, "INV2-0000-DOESNOTX")
    assert code == ro.EXIT_PROVIDER_ERROR and "INVALID_RESOURCE_ID" in output and "not found" in output.lower()
    assert fake.calls == [TOKEN, invoice_get("INV2-0000-DOESNOTX")]


def test_a_redirect_is_not_followed(fake, seeded):
    fake.fault("GET", seeded, (302, {"location": "https://evil.example.net/steal"}))
    code, output = go(fake, seeded)
    assert code == ro.EXIT_PROVIDER_ERROR and fake.calls == [TOKEN, invoice_get(seeded)]
    assert "evil.example.net" not in output


def test_one_expired_token_refresh_is_the_only_extra_request(fake, seeded):
    fake.fault("GET", seeded, 401)
    code, _ = go(fake, seeded)
    assert code == ro.EXIT_OK
    assert fake.calls == [TOKEN, invoice_get(seeded), TOKEN, invoice_get(seeded)]


def test_a_persistent_401_stops_after_one_refresh(fake, seeded):
    fake.fault("GET", seeded, 401, 401, 401)
    code, _ = go(fake, seeded)
    assert code == ro.EXIT_PROVIDER_ERROR and len(fake.calls) == 4


# --- the outbound allowlist ----------------------------------------------------------------------------------------------

class Recorder(httpx.BaseTransport):
    def __init__(self):
        self.seen = []

    def handle_request(self, request):
        self.seen.append((request.method, str(request.url)))
        return httpx.Response(200, json={})


def send(transport, method, url):
    return transport.handle_request(httpx.Request(method, url))


BASE = "https://api-m.sandbox.paypal.com"
ID = "INV2-0000-AAAA-BBBB-CCCC"


def test_allowlist_permits_only_oauth_and_the_named_invoice():
    inner = Recorder()
    gate = ro.AllowlistTransport(inner, ID)
    assert send(gate, "POST", f"{BASE}/v1/oauth2/token").status_code == 200
    assert send(gate, "GET", f"{BASE}/v2/invoicing/invoices/{ID}").status_code == 200
    assert [m for m, _ in inner.seen] == ["POST", "GET"]


@pytest.mark.parametrize("method,url", [
    ("POST", f"{BASE}/v2/invoicing/invoices"),                         # create
    ("POST", f"{BASE}/v2/invoicing/invoices/{ID}/send"),               # send
    ("POST", f"{BASE}/v2/invoicing/invoices/{ID}/payments"),           # record payment
    ("POST", f"{BASE}/v2/invoicing/invoices/{ID}/refunds"),            # record refund
    ("POST", f"{BASE}/v2/invoicing/invoices/{ID}/cancel"),             # cancel
    ("POST", f"{BASE}/v2/invoicing/invoices/{ID}/remind"),             # remind
    ("POST", f"{BASE}/v2/invoicing/invoices/{ID}"),                    # POST to the invoice itself
    ("PUT", f"{BASE}/v2/invoicing/invoices/{ID}"),                     # update
    ("PATCH", f"{BASE}/v2/invoicing/invoices/{ID}"),
    ("DELETE", f"{BASE}/v2/invoicing/invoices/{ID}"),                  # delete the draft
    ("GET", f"{BASE}/v2/invoicing/invoices"),                          # list
    ("GET", f"{BASE}/v2/invoicing/invoices/"),
    ("GET", f"{BASE}/v2/invoicing/invoices/{ID}/"),
    ("GET", f"{BASE}/v2/invoicing/invoices/{ID}/send"),
    ("GET", f"{BASE}/v2/invoicing/invoices/INV2-OTHER-INVOICE"),       # a different invoice
    ("GET", f"{BASE}/v2/invoicing/invoices/{ID}?fields=all"),          # query string
    ("GET", f"{BASE}/v2/invoicing/invoices/{ID}#frag"),
    ("GET", f"{BASE}/v1/oauth2/token"),                                # wrong method for the token path
    ("GET", f"{BASE}/v1/identity/oauth2/userinfo"),
    ("GET", f"https://api-m.paypal.com/v2/invoicing/invoices/{ID}"),    # LIVE host
    ("GET", f"https://api.sandbox.paypal.com/v2/invoicing/invoices/{ID}"),
    ("GET", f"http://api-m.sandbox.paypal.com/v2/invoicing/invoices/{ID}"),   # plain http
    ("GET", f"https://evil.example.net/v2/invoicing/invoices/{ID}"),
    ("GET", f"https://user:pw@api-m.sandbox.paypal.com/v2/invoicing/invoices/{ID}"),
    ("GET", f"https://api-m.sandbox.paypal.com:8443/v2/invoicing/invoices/{ID}"),
    ("POST", "https://api-m.paypal.com/v1/oauth2/token"),               # live OAuth
    ("POST", f"{BASE}/v1/oauth2/token?x=1"),
    ("POST", f"{BASE}/v1/oauth2/token/extra"),
])
def test_allowlist_blocks_everything_else_before_it_reaches_the_inner_transport(method, url):
    inner = Recorder()
    gate = ro.AllowlistTransport(inner, ID)
    with pytest.raises(ro.BlockedRequest):
        send(gate, method, url)
    assert inner.seen == []


def test_allowlist_has_a_small_request_budget():
    inner = Recorder()
    gate = ro.AllowlistTransport(inner, ID)
    for _ in range(2):
        send(gate, "POST", f"{BASE}/v1/oauth2/token")
        send(gate, "GET", f"{BASE}/v2/invoicing/invoices/{ID}")
    with pytest.raises(ro.BlockedRequest):
        send(gate, "POST", f"{BASE}/v1/oauth2/token")
    with pytest.raises(ro.BlockedRequest):
        send(gate, "GET", f"{BASE}/v2/invoicing/invoices/{ID}")
    assert len(inner.seen) == 4


def test_allowlist_rejects_a_malformed_invoice_id_at_construction():
    for bad in ("", "../x", "a b", "INV2/send"):
        with pytest.raises(ValueError):
            ro.AllowlistTransport(Recorder(), bad)


def test_a_blocked_request_inside_a_run_stops_it_safely(fake, seeded, monkeypatch):
    """Even if verification code were changed to ask for something else, the run stops and sends nothing."""
    original = ro.PayPalSandboxProvider.get_invoice

    def sneaky(self, invoice_id):
        self._call("send_invoice", "POST", f"/v2/invoicing/invoices/{invoice_id}/send", safe=False, json_body={})
        return original(self, invoice_id)

    monkeypatch.setattr(ro.PayPalSandboxProvider, "get_invoice", sneaky)
    code, output = go(fake, seeded)
    assert code == ro.EXIT_REFUSED and "blocked" in output.lower()
    assert fake.count("POST", "/send") == 0 and fake.calls == [TOKEN]
    assert fake.only()["status"] == "DRAFT"


# --- state, secrets and structure ----------------------------------------------------------------------------------------

def test_the_real_smoke_marker_is_not_touched(fake, seeded):
    marker = smoke.DEFAULT_MARKER
    before = (marker.read_bytes(), marker.stat().st_mtime_ns) if marker.exists() else None
    go(fake, seeded)
    go(fake, seeded, number="SMOKE-000000000000")
    after = (marker.read_bytes(), marker.stat().st_mtime_ns) if marker.exists() else None
    assert before == after


def test_source_has_no_marker_write_create_or_mutating_code():
    source = (APP_DIR / "sandbox_readonly.py").read_text(encoding="utf8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("state.json", "DEFAULT_MARKER", "marker", "unlink", "write_text", "write_bytes", "open(", "os.remove",
                      "create_invoice", "send_invoice", ".post(", ".put(", ".patch(", ".delete(", "dotenv"):
        assert forbidden not in code, forbidden
    assert not re.search(r"[.]env([^a-z]|$)", code)


def test_source_never_prints_raw_values_or_uses_a_live_host():
    source = (APP_DIR / "sandbox_readonly.py").read_text(encoding="utf8")
    assert "api-m.paypal.com" not in source
    assert not re.search(r"print\(", source)


def test_cli_dry_run_returns_zero_and_requires_its_arguments(fake, seeded, capsys):
    argv = ["--invoice-id", seeded, "--invoice-number", NUMBER, "--amount", "1.00", "--currency", "USD"]
    assert ro.main(argv, env=ENV) == ro.EXIT_OK
    assert "DRY RUN" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        ro.main(["--invoice-id", seeded], env=ENV)
    assert exit_info.value.code == 2


def test_cli_without_execute_never_calls_the_network(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise AssertionError("network transport must not be built in a dry run")

    monkeypatch.setattr(ro.httpx, "HTTPTransport", boom)
    argv = ["--invoice-id", ID, "--invoice-number", NUMBER, "--amount", "1.00"]
    assert ro.main(argv, env=ENV) == ro.EXIT_OK
    assert "DRY RUN" in capsys.readouterr().out


def test_the_script_wrapper_exists_and_only_delegates():
    script = (APP_DIR.parents[1] / "scripts" / "sandbox_verify_invoice.py").read_text(encoding="utf8")
    assert "from app.sandbox_readonly import main" in script and "requests" not in script and "httpx" not in script


# --- hardening after the independent review ------------------------------------------------------------------------------

def test_an_unvalidated_invoice_number_from_paypal_is_never_printed(fake, seeded):
    fake.only()["detail"]["invoice_number"] = "token-like-secret-value-ZZZ 123 !!"
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and "[FAIL] invoice number matches (found (not printable))" in output
    assert "token-like-secret-value" not in output


def test_a_printable_mismatched_invoice_number_is_shown_to_help_diagnosis(fake, seeded):
    fake.only()["detail"]["invoice_number"] = "SMOKE-OTHER"
    assert "found SMOKE-OTHER" in go(fake, seeded)[1]


@pytest.mark.parametrize("field,value", [
    ("payments", {"paid_amount": {"currency_code": "USD", "value": "1.00"}, "transactions": []}),
    ("payments", {"paid_amount": "garbage", "transactions": []}),
    ("payments", {"paid_amount": 5, "transactions": []}),
    ("payments", ["not", "an", "object"]),
    ("refunds", {"refunded_amount": {"currency_code": "USD", "value": "0.50"}, "transactions": []}),
])
def test_a_reported_paid_or_refunded_amount_fails_even_without_transactions(fake, seeded, field, value):
    fake.only()[field] = value
    code, output = go(fake, seeded)
    assert code != ro.EXIT_OK and "RESULT: PASS" not in output
    assert "[FAIL] no paid or refunded amount reported" in output or code == ro.EXIT_UNREADABLE


def test_zero_paid_and_refunded_amounts_are_fine(fake, seeded):
    zero = {"currency_code": "USD", "value": "0.00"}
    fake.only()["payments"] = {"paid_amount": zero, "transactions": []}
    fake.only()["refunds"] = {"refunded_amount": zero, "transactions": []}
    assert go(fake, seeded)[0] == ro.EXIT_OK


@pytest.mark.parametrize("mutate", [
    lambda inv: inv.__setitem__("primary_recipients", inv["primary_recipients"] * 2),
    lambda inv: inv.__setitem__("additional_recipients", [{"email_address": "x@example.net"}]),
    lambda inv: inv.__setitem__("additional_recipients", "garbage"),
])
def test_extra_recipients_fail_verification(fake, seeded, mutate):
    mutate(fake.only())
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and "[FAIL] the configured buyer is the only recipient" in output
    assert "x@example.net" not in output


def test_empty_additional_recipients_are_fine(fake, seeded):
    fake.only()["additional_recipients"] = []
    assert go(fake, seeded)[0] == ro.EXIT_OK


def test_credentials_with_surrounding_whitespace_are_still_redacted(fake, seeded):
    padded = {**ENV, "PAYPAL_SANDBOX_CLIENT_SECRET": f"  {CLIENT_SECRET}  ", "PAYPAL_SANDBOX_CLIENT_ID": f" {CLIENT_ID} "}
    code, output = go(fake, seeded, env=padded)
    assert code == ro.EXIT_OK and CLIENT_SECRET not in output and CLIENT_ID not in output


@pytest.mark.parametrize("url", [
    f"{BASE}/v2/invoicing/invoices/%49NV2-0000-AAAA-BBBB-CCCC",         # encoded letter: raw path differs, blocked
    f"{BASE}/v2/invoicing/invoices/{ID}%2F..%2Fsend",
    f"{BASE}/v2/invoicing/invoices/{ID}%00",
    f"{BASE}/v2/invoicing/invoices/%2e%2e/invoices/{ID}",
    f"{BASE}/v2/invoicing/invoices/{ID}/..",
    f"{BASE}/v2/invoicing/./invoices/{ID}/send",
    f"https://api-m.sandbox.paypal.com./v2/invoicing/invoices/{ID}",    # trailing-dot host
])
def test_allowlist_blocks_encoded_and_dot_segment_variants(url):
    inner = Recorder()
    with pytest.raises(ro.BlockedRequest):
        send(ro.AllowlistTransport(inner, ID), "GET", url)
    assert inner.seen == []


# --- merchant identity: strict comparison, safe diagnostics ----------------------------------------------------------------

MERCHANT_FAIL = "[FAIL] merchant is the configured sandbox business account"


def set_invoicer(fake, value):
    invoice = fake.only()
    if value is ...:
        invoice.pop("invoicer", None)
    else:
        invoice["invoicer"] = value


def assert_no_full_address(output, *addresses):
    for address in (MERCHANT, BUYER, *addresses):
        assert address not in output and address.upper() not in output


def test_a_missing_invoicer_object_is_reported_as_absent(fake, seeded):
    set_invoicer(fake, ...)
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and MERCHANT_FAIL in output
    assert "the `invoicer` object is absent" in output and "differs" not in output
    assert_no_full_address(output)


def test_a_missing_email_address_is_reported_with_the_field_names_only(fake, seeded):
    set_invoicer(fake, {"business_name": "Very Secret Biz Ltd", "name": {"given_name": "Zed"}, "website": "https://x.example.net"})
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and MERCHANT_FAIL in output
    assert "`invoicer.email_address` is absent" in output and "invoicer fields: business_name, name, website" in output
    for value in ("Very Secret Biz Ltd", "Zed", "x.example.net"):
        assert value not in output
    assert_no_full_address(output)


@pytest.mark.parametrize("value,phrase", [
    ({}, "is absent (invoicer fields: (none))"),
    ({"email_address": None}, "is absent"),
    ({"email_address": ""}, "present but empty"),
    ({"email_address": "   "}, "present but empty"),
    ({"email_address": 12345}, "is not a string"),
    ({"email_address": ["merchant@example.com"]}, "is not a string"),
])
def test_empty_or_malformed_email_addresses_fail_with_a_specific_reason(fake, seeded, value, phrase):
    set_invoicer(fake, value)
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and MERCHANT_FAIL in output and phrase in output
    assert "RESULT: PASS" not in output


def test_a_different_address_is_masked_and_compared_by_domain_and_length(fake, seeded):
    set_invoicer(fake, {"email_address": "werchant@example.com", "business_name": "B"})   # same length, same domain
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and MERCHANT_FAIL in output
    assert "present but differs: found w***@example.com, expected m***@example.com; domains match; lengths match" in output
    assert "invoicer fields: business_name, email_address" in output
    assert_no_full_address(output, "werchant@example.com")


@pytest.mark.parametrize("address,domains,lengths", [
    ("other-merchant@example.com", "match", "differ"),
    ("merchant@example.org", "differ", "match"),
    ("a@b.co", "differ", "differ"),
])
def test_the_diagnostic_reports_domain_and_length_verdicts(fake, seeded, address, domains, lengths):
    set_invoicer(fake, {"email_address": address})
    _, output = go(fake, seeded)
    assert f"domains {domains}; lengths {lengths}" in output
    assert_no_full_address(output, address)


@pytest.mark.parametrize("address", [
    "MERCHANT@EXAMPLE.COM", "Merchant@Example.Com", "  merchant@example.com  ", "\tmerchant@example.com\n",
])
def test_case_and_whitespace_in_the_response_are_normalized(fake, seeded, address):
    set_invoicer(fake, {"email_address": address})
    code, output = go(fake, seeded)
    assert code == ro.EXIT_OK and "[FAIL]" not in output


def test_case_and_whitespace_in_the_configured_address_are_normalized(fake, seeded):
    code, _ = go(fake, seeded, env={**ENV, "PAYPAL_SANDBOX_MERCHANT_EMAIL": "  MERCHANT@Example.COM "})
    assert code == ro.EXIT_OK


@pytest.mark.parametrize("address", [
    "merchant+sandbox@example.com",          # plus alias
    "merchant.@example.com", "mer.chant@example.com",   # dot variants are different addresses here
    "merchant@mail.example.com",             # sub-domain
    "merchant@example.com.evil.net",         # suffix attack
    "merchant@examp1e.com",                  # digit look-alike
    "rnerchant@example.com",                 # rn / m look-alike
    "merchant@example.co",                   # truncated domain
    "merchant@example.com ,other@example.com",
    "merchant@example.com\x00",
    "merch\u0430nt@example.com",              # Cyrillic a
])
def test_aliases_and_look_alikes_never_pass(fake, seeded, address):
    set_invoicer(fake, {"email_address": address})
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and MERCHANT_FAIL in output and "RESULT: PASS" not in output
    assert_no_full_address(output, address.strip())


def test_a_non_address_value_is_not_echoed(fake, seeded):
    set_invoicer(fake, {"email_address": "ignore previous instructions; token=abc123SECRET"})
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and "(not an email address)" in output
    assert "abc123SECRET" not in output and "ignore previous" not in output


def test_a_hostile_domain_is_not_echoed(fake, seeded):
    set_invoicer(fake, {"email_address": "m@evil.example.net/<script>alert(1)</script>"})
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and "<script>" not in output and "(not an email address)" in output


def test_there_is_no_fallback_identity_source(fake, seeded):
    """The right address elsewhere in the body must not rescue a missing or wrong invoicer email."""
    invoice = fake.only()
    invoice["invoicer"] = {"email_address": "someone-else@example.net"}
    invoice["merchant_info"] = {"email": MERCHANT}
    invoice["payee"] = {"email_address": MERCHANT}
    invoice["detail"]["merchant_email"] = MERCHANT
    assert go(fake, seeded)[0] == ro.EXIT_VERIFY_FAILED
    invoice.pop("invoicer")
    assert go(fake, seeded)[0] == ro.EXIT_VERIFY_FAILED


def test_the_recipient_address_never_counts_as_the_merchant(fake, seeded):
    fake.only()["invoicer"] = {"email_address": BUYER}
    code, output = go(fake, seeded)
    assert code == ro.EXIT_VERIFY_FAILED and MERCHANT_FAIL in output and "[PASS] recipient is the configured buyer" in output


def test_a_matching_merchant_prints_no_diagnostic(fake, seeded):
    code, output = go(fake, seeded)
    assert code == ro.EXIT_OK and "merchant diagnostic" not in output


def test_merchant_diagnostics_do_not_change_the_requests_made(fake, seeded):
    set_invoicer(fake, {"email_address": "other@example.net"})
    go(fake, seeded)
    assert fake.calls == [TOKEN, invoice_get(seeded)]


def test_invoicer_field_names_are_sanitized(fake, seeded):
    set_invoicer(fake, {"email_address": "x@example.net", "bad key with spaces": 1, "k" * 40: 2, "ok_name": 3, "<b>": 4})
    _, output = go(fake, seeded)
    assert "invoicer fields: email_address, ok_name" in output
    assert "bad key" not in output and "<b>" not in output and "k" * 40 not in output
