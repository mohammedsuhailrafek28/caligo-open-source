"""The Stage 2D smoke-test tooling, exercised only against the fake PayPal (no network, no real credentials)."""
import json
import re
from pathlib import Path

import pytest

from app import sandbox_smoke as smoke
from app.payment_provider import ProviderInvoice, ProviderInvoiceState, ProviderTransaction
from tests.fake_paypal import CLIENT_ID, CLIENT_SECRET, MERCHANT, FakePayPal

BUYER = "buyer@example.org"
ENV = {
    "PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID, "PAYPAL_SANDBOX_CLIENT_SECRET": CLIENT_SECRET,
    "PAYPAL_SANDBOX_MERCHANT_EMAIL": MERCHANT, "PAYPAL_SMOKE_BUYER_EMAIL": BUYER,
    "PAYPAL_SMOKE_CONFIRM": smoke.CONFIRM_VALUE,
}
CREATE = ("POST", "/v2/invoicing/invoices")


@pytest.fixture()
def marker(tmp_path):
    return tmp_path / "smoke" / "state.json"


def go(fake, marker, env=None, execute=True):
    lines: list[str] = []
    code = smoke.run(env or ENV, execute=execute, marker=marker, out=lines.append, transport=fake.transport())
    return code, "\n".join(lines)


# --- default is a dry run -------------------------------------------------------------------------------

def test_default_run_is_a_dry_run_with_no_network_and_no_marker(fake, marker):
    code, output = go(fake, marker, execute=False)
    assert code == smoke.EXIT_OK and "DRY RUN" in output and fake.calls == [] and not marker.exists()


def test_execute_needs_the_confirmation_variable(fake, marker):
    code, output = go(fake, marker, env={k: v for k, v in ENV.items() if k != "PAYPAL_SMOKE_CONFIRM"})
    assert code == smoke.EXIT_REFUSED and "PAYPAL_SMOKE_CONFIRM" in output and fake.calls == []


def test_missing_variables_are_named_but_never_their_values(fake, marker):
    code, output = go(fake, marker, env={"PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID})
    assert code == smoke.EXIT_REFUSED and "PAYPAL_SANDBOX_CLIENT_SECRET" in output and CLIENT_ID not in output
    assert fake.calls == []


@pytest.mark.parametrize("override", [{"PAYPAL_SMOKE_BUYER_EMAIL": MERCHANT}, {"PAYPAL_SMOKE_BUYER_EMAIL": "not-an-email"},
                                      {"PAYPAL_SANDBOX_MERCHANT_EMAIL": "bad"}])
def test_invalid_or_identical_accounts_are_refused(fake, marker, override):
    code, _ = go(fake, marker, env={**ENV, **override})
    assert code == smoke.EXIT_REFUSED and fake.calls == []


# --- the happy path: exactly one draft, read back, verified, never sent ---------------------------------------

def test_happy_path_creates_one_draft_reads_it_back_and_never_sends_or_pays(fake, marker):
    code, output = go(fake, marker)
    assert code == smoke.EXIT_OK and "RESULT: PASS" in output
    invoice = fake.only()
    assert invoice["status"] == "DRAFT" and invoice["payments"]["transactions"] == [] and invoice["refunds"]["transactions"] == []
    assert invoice["detail"]["invoice_number"].startswith("SMOKE-")
    assert fake.count("POST", "/send") == 0 and fake.count("POST", "/v2/invoicing/invoices") == 1
    assert fake.count("POST", "/v1/oauth2/token") == 1
    assert fake.count("GET", invoice["id"]) == 2  # the adapter's own verification + the explicit read-back
    assert not [c for c in fake.calls if c[0] not in ("GET", "POST")]
    state = json.loads(marker.read_text())
    assert state["state"] == "VERIFIED" and state["invoice_id"] == invoice["id"]
    assert CLIENT_SECRET not in marker.read_text() and CLIENT_ID not in marker.read_text()


def test_a_second_run_is_refused_so_at_most_one_invoice_is_ever_created(fake, marker):
    assert go(fake, marker)[0] == smoke.EXIT_OK
    calls = len(fake.calls)
    code, output = go(fake, marker)
    assert code == smoke.EXIT_REFUSED and "marker" in output.lower() or "previous smoke run" in output
    assert len(fake.calls) == calls and len(fake.invoices) == 1


# --- failures ------------------------------------------------------------------------------------------------------

def test_oauth_failure_creates_nothing_and_keeps_the_single_attempt(fake, marker):
    code, output = go(fake, marker, env={**ENV, "PAYPAL_SANDBOX_CLIENT_SECRET": "wrong-example-secret-value"})
    assert code == smoke.EXIT_PROVIDER_ERROR and "OAuth FAILED" in output
    assert fake.count("POST", "/v2/invoicing/invoices") == 0 and not marker.exists()
    assert "wrong-example-secret-value" not in output


@pytest.mark.parametrize("action", ["create_then_timeout", "timeout", 500])
def test_ambiguous_creation_is_never_retried_and_blocks_further_runs(fake, marker, action):
    fake.fault(*CREATE, action)
    code, output = go(fake, marker)
    assert code == smoke.EXIT_AMBIGUOUS and "NOT retrying" in output
    assert fake.count("POST", "/v2/invoicing/invoices") == 1  # exactly one attempt
    assert json.loads(marker.read_text())["state"] == "AMBIGUOUS"
    assert go(fake, marker)[0] == smoke.EXIT_REFUSED and fake.count("POST", "/v2/invoicing/invoices") == 1
    assert fake.count("POST", "/send") == 0


def test_created_but_unreadable_keeps_the_invoice_id_and_blocks_reruns(fake, marker):
    fake.fault("GET", "", 500)
    code, _ = go(fake, marker)
    assert code == smoke.EXIT_AMBIGUOUS
    state = json.loads(marker.read_text())
    assert state["invoice_id"] == fake.only()["id"] and fake.count("POST", "/v2/invoicing/invoices") == 1


@pytest.mark.parametrize("status", [403, 422, 429])
def test_definitive_failures_do_not_consume_the_attempt_and_are_not_retried(fake, marker, status):
    fake.fault(*CREATE, status)
    code, output = go(fake, marker)
    assert code == smoke.EXIT_PROVIDER_ERROR and "before any invoice existed" in output
    assert fake.count("POST", "/v2/invoicing/invoices") == 1 and not marker.exists() and fake.invoices == {}


def test_a_business_account_that_is_not_the_configured_merchant_fails_verification(marker):
    fake = FakePayPal()
    fake.invoicer_mode = "other"  # PayPal accepted the request but the stored invoice names a different merchant
    code, output = go(fake, marker)
    assert code == smoke.EXIT_VERIFY_FAILED and "[FAIL] merchant is the configured sandbox business account" in output
    assert json.loads(marker.read_text())["state"] == "VERIFY_FAILED" and fake.count("POST", "/send") == 0


# --- output never leaks -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["ok", "ambiguous", "oauth"])
def test_output_never_contains_credentials_tokens_or_full_email_addresses(fake, marker, scenario):
    env = ENV
    if scenario == "ambiguous":
        fake.fault(*CREATE, "create_then_timeout")
    if scenario == "oauth":
        env = {**ENV, "PAYPAL_CLIENT_SECRET": "wrong-example-secret-value"}
    _, output = go(fake, marker, env=env)
    for forbidden in (CLIENT_SECRET, CLIENT_ID, fake.valid_token or "never-issued", "Bearer fake-token", BUYER, MERCHANT):
        assert forbidden not in output
    assert "b***@example.org" in output


def test_redactor_scrubs_secrets_bearer_tokens_paypal_tokens_and_emails():
    redact = smoke.Redactor(["super-secret-value"])
    text = "x super-secret-value Bearer abcdefghijklmnop A21AAabcdefghijklmnopqrstuvwxyz1234 who@example.org"
    cleaned = redact(text)
    assert "super-secret-value" not in cleaned and "abcdefghijklmnop" not in cleaned
    assert "A21AA" not in cleaned and "who@example.org" not in cleaned and "w***@example.org" in cleaned


# --- verification logic ----------------------------------------------------------------------------------------------------

def _pair(**changes):
    request = smoke.build_request(smoke.load_config(ENV), "abc123def456")
    base = dict(invoice_id="INV2-0001-AAAAAAAA", invoice_number=request.invoice_number, state=ProviderInvoiceState.DRAFT,
                currency="USD", total_minor=100, payments=(), refunds=(), recipient_email=BUYER, merchant_email=MERCHANT)
    created = ProviderInvoice(**base)
    return created, ProviderInvoice(**{**base, **changes}), request


@pytest.mark.parametrize("change,failing", [
    ({"invoice_id": "INV2-0002-BBBBBBBB"}, "invoice id"), ({"invoice_number": "OTHER"}, "invoice number"),
    ({"merchant_email": "x@example.com"}, "merchant"), ({"merchant_email": None}, "merchant"),
    ({"total_minor": 101}, "total amount"), ({"currency": "EUR"}, "currency"),
    ({"state": ProviderInvoiceState.SENT}, "DRAFT"), ({"state": ProviderInvoiceState.PAID}, "DRAFT"),
    ({"recipient_email": "x@example.org"}, "recipient"),
    ({"payments": (ProviderTransaction("P1", "PAYPAL", 100, "USD"),)}, "no payments"),
    ({"refunds": (ProviderTransaction("R1", "PAYPAL", 100, "USD"),)}, "no refunds"),
])
def test_every_identity_field_is_verified(change, failing):
    created, readback, request = _pair(**change)
    failed = [label for label, ok in smoke.verify_invoice(created, readback, request, MERCHANT) if not ok]
    assert failed and any(failing in label for label in failed)


def test_a_matching_invoice_passes_every_check():
    created, readback, request = _pair()
    assert all(ok for _, ok in smoke.verify_invoice(created, readback, request, MERCHANT))


# --- structural guarantees -------------------------------------------------------------------------------------------------------

SOURCE = Path(smoke.__file__).read_text(encoding="utf8")
WRAPPER = (Path(smoke.__file__).resolve().parents[2] / "scripts" / "sandbox_smoke_test.py").read_text(encoding="utf8")


def test_smoke_code_has_no_path_to_send_pay_or_refund():
    for forbidden in ("send_invoice", "simulate", "record_payment", "/payments", "/refunds", "/cancel", "/remind", "MARKED_AS", "mark_as"):
        assert forbidden not in SOURCE, forbidden


def test_smoke_code_never_reads_environment_files_or_builds_application_settings():
    combined = SOURCE + WRAPPER
    for forbidden in ("get_settings", "Settings(", "dotenv", "env_file", "read_text(", "read_bytes(", ".open(\"r"):
        assert forbidden not in combined, forbidden
    assert not re.search(r"[.]env([^a-z]|$)", combined)  # no environment-file path (os.environ is fine)


def test_smoke_provider_is_built_without_any_automatic_retries():
    assert "max_attempts=1" in SOURCE


# --- review-driven hardening (Stage 2D security review) ----------------------------------------------------------

def test_two_overlapping_runs_cannot_both_create_an_invoice(fake, marker):
    results = []

    def during_oauth(method, path):
        if path == "/v1/oauth2/token" and not results:
            results.append(go(FakePayPal(), marker)[0])  # a second operator starts while the first is authenticating

    fake.on_request = during_oauth
    code, _ = go(fake, marker)
    assert code == smoke.EXIT_OK and results == [smoke.EXIT_REFUSED] and len(fake.invoices) == 1


def test_an_unwritable_marker_location_is_refused_before_any_network_call(fake, tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    code, output = go(fake, blocker / "state.json")
    assert code == smoke.EXIT_REFUSED and fake.calls == [] and "marker" in output


def test_an_unexpected_error_is_ambiguous_and_never_a_raw_traceback(fake, marker, monkeypatch):
    monkeypatch.setattr(smoke, "_write_marker", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    code, output = go(fake, marker)
    assert code == smoke.EXIT_AMBIGUOUS and "UNEXPECTED ERROR (OSError)" in output and "Traceback" not in output
    assert fake.count("POST", "/v2/invoicing/invoices") == 0


def test_production_style_variable_names_are_not_used_by_the_smoke_test(fake, marker):
    legacy = {"PAYPAL_CLIENT_ID": CLIENT_ID, "PAYPAL_CLIENT_SECRET": CLIENT_SECRET,
              "PAYPAL_SANDBOX_MERCHANT_EMAIL": MERCHANT, "PAYPAL_SMOKE_BUYER_EMAIL": BUYER,
              "PAYPAL_SMOKE_CONFIRM": smoke.CONFIRM_VALUE}
    code, output = go(fake, marker, env=legacy)
    assert code == smoke.EXIT_REFUSED and "PAYPAL_SANDBOX_CLIENT_ID" in output and fake.calls == []


# --- Stage 2D incident regression -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("style", ["legacy_host", "link_description", "links", "representation"])
def test_smoke_passes_against_every_documented_create_response_shape(marker, style):
    fake = FakePayPal()
    fake.create_style = style
    code, output = go(fake, marker)
    assert code == smoke.EXIT_OK and "RESULT: PASS" in output
    assert json.loads(marker.read_text())["invoice_id"] == fake.only()["id"]
    assert fake.count("POST", "/send") == 0 and fake.count("POST", "/v2/invoicing/invoices") == 1


def test_unreadable_create_response_is_reported_with_its_reason_and_still_blocks_reruns(fake, marker):
    import httpx
    fake.create_response = lambda i: httpx.Response(201, json={})
    code, output = go(fake, marker)
    assert code == smoke.EXIT_AMBIGUOUS and "NO_ID" in output and "NOT retrying" in output
    assert json.loads(marker.read_text())["state"] == "AMBIGUOUS" and json.loads(marker.read_text())["invoice_id"] == ""
    assert go(fake, marker)[0] == smoke.EXIT_REFUSED and fake.count("POST", "/v2/invoicing/invoices") == 1


def test_smoke_create_asserts_the_merchant_and_a_rejection_consumes_nothing(marker):
    fake = FakePayPal(merchant="the-token-account@example.com")  # configured merchant is not the token's account
    code, output = go(fake, marker)
    assert code == smoke.EXIT_PROVIDER_ERROR and "before any invoice existed" in output
    assert fake.invoices == {} and not marker.exists() and fake.count("POST", "/send") == 0


def test_smoke_sends_the_configured_merchant_as_invoicer(fake, marker):
    assert go(fake, marker)[0] == smoke.EXIT_OK
    assert fake.create_bodies[0]["invoicer"] == {"email_address": MERCHANT}
