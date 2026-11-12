"""The controlled Sandbox validation runner, exercised ONLY against the fake PayPal: no network, no real credentials, no real invoice."""
import hashlib
import re
from pathlib import Path

import httpx
import pytest

from app import sandbox_attestation as attestation
from app import sandbox_runner as runner
from app.sandbox_runner_guard import (
    DENIED_INVOICE_IDS, BlockedRequest, RunnerTransport, RunState, Stage, refuse_unsafe_workdir,
)
from tests.fake_paypal import CLIENT_ID, CLIENT_SECRET, MERCHANT, FakePayPal

BUYER = "buyer@example.org"
APP_DIR = Path(runner.__file__).resolve().parent
REPO = APP_DIR.parents[1]
SMOKE_STATE = REPO / "backend" / ".smoke" / "state.json"
BASE_ENV = {"PAYPAL_SANDBOX_CLIENT_ID": CLIENT_ID, "PAYPAL_SANDBOX_CLIENT_SECRET": CLIENT_SECRET,
            "PAYPAL_SANDBOX_MERCHANT_EMAIL": MERCHANT, "PAYPAL_SMOKE_BUYER_EMAIL": BUYER}
CONFIRM = {s: {name: value} for s, (name, value) in runner.CONFIRMATIONS.items()}
ALLOWED_CALLS = {("POST", "/v1/oauth2/token"), ("POST", "/v2/invoicing/invoices")}


@pytest.fixture(autouse=True)
def _clean_attestations():
    attestation.clear()
    yield
    attestation.clear()


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@pytest.fixture(autouse=True)
def _marker_preserved():
    before = _sha(SMOKE_STATE)
    yield
    assert _sha(SMOKE_STATE) == before  # the existing smoke marker is never touched


def go(fake, stage, workdir, *, execute=True, confirm=True, env=None, argv_extra=()):
    argv = ["--stage", stage, "--workdir", str(workdir), *(["--execute"] if execute else []), *argv_extra]
    merged = {**BASE_ENV, **(CONFIRM[Stage(stage)] if confirm and stage in ("create", "send", "read") else {}), **(env or {})}
    return runner.main(argv, merged, lambda: fake.transport())


def full_run(fake, workdir):
    assert go(fake, "create", workdir) == runner.EXIT_OK
    assert go(fake, "send", workdir) == runner.EXIT_OK
    fake.pay(fake.only())
    assert go(fake, "read", workdir) == runner.EXIT_OK


def _never():
    raise AssertionError("a network transport was requested")


# --- default dry-run ---------------------------------------------------------------------------

def test_default_is_the_offline_rehearsal_and_passes(capsys):
    assert runner.main([], BASE_ENV, _never) == runner.EXIT_OK
    out = capsys.readouterr().out
    assert "REHEARSAL" in out and "FAIL" not in out and "UNATTESTED_SANDBOX_PROVIDER" in out


def test_a_real_stage_without_execute_is_a_dry_run_and_never_builds_a_transport(tmp_path, capsys):
    assert runner.main(["--stage", "create", "--workdir", str(tmp_path)], BASE_ENV, _never) == runner.EXIT_OK
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "POST /v2/invoicing/invoices" in out
    assert not list(tmp_path.iterdir())  # nothing created


def test_dry_run_of_send_without_a_created_invoice_would_be_refused(tmp_path):
    assert runner.main(["--stage", "send", "--workdir", str(tmp_path)], BASE_ENV, _never) == runner.EXIT_REFUSED


def test_there_is_no_option_to_choose_a_base_url():
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(["--stage", "create", "--base-url", "https://api-m.paypal.com"])


# --- explicit confirmation, one per mutating stage -----------------------------------------------

@pytest.mark.parametrize("stage", ["create", "send", "read"])
def test_execute_without_its_confirmation_is_refused_before_any_transport(tmp_path, stage):
    code = runner.main(["--stage", stage, "--execute", "--workdir", str(tmp_path)], BASE_ENV, _never)
    assert code == runner.EXIT_REFUSED


def test_another_stages_confirmation_is_not_accepted(tmp_path):
    env = {**BASE_ENV, **CONFIRM[Stage.CREATE]}
    assert runner.main(["--stage", "send", "--execute", "--workdir", str(tmp_path)], env, _never) == runner.EXIT_REFUSED


def test_missing_credentials_are_named_but_never_their_values(tmp_path, capsys):
    env = {k: v for k, v in BASE_ENV.items() if k != "PAYPAL_SANDBOX_CLIENT_SECRET"}
    assert runner.main(["--stage", "create", "--workdir", str(tmp_path)], env, _never) == runner.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "PAYPAL_SANDBOX_CLIENT_SECRET" in out and CLIENT_ID not in out


def test_workdir_is_required_and_must_be_outside_the_repository_and_smoke_directory(tmp_path):
    env = {**BASE_ENV, **CONFIRM[Stage.CREATE]}
    assert runner.main(["--stage", "create", "--execute"], env, _never) == runner.EXIT_REFUSED
    for bad in (REPO / "backend" / ".smoke", REPO, REPO / "docs" / "run"):
        assert refuse_unsafe_workdir(bad, REPO) is not None
        assert runner.main(["--stage", "create", "--execute", "--workdir", str(bad)], env, _never) == runner.EXIT_REFUSED
    assert refuse_unsafe_workdir(tmp_path, REPO) is None


# --- the allowlist transport --------------------------------------------------------------------

class Recorder(httpx.BaseTransport):
    def __init__(self):
        self.seen = []

    def handle_request(self, request):
        self.seen.append((request.method, request.url.path))
        return httpx.Response(200, json={})


def _req(method, url, body=None):
    return httpx.Request(method, url, json=body) if body is not None else httpx.Request(method, url)


SB = "https://api-m.sandbox.paypal.com"
GOOD_BODY = {"detail": {"invoice_number": "PEM-ABC", "currency_code": "USD"}, "primary_recipients": [{"billing_info": {"email_address": BUYER}}],
             "items": [{"name": "x", "quantity": "1", "unit_amount": {"currency_code": "USD", "value": "1.00"}}]}


def make(stage, invoice_id=None, inner=None):
    return RunnerTransport(inner or Recorder(), stage, buyer_email=BUYER, invoice_id=invoice_id)


@pytest.mark.parametrize("url", [
    "https://api-m.paypal.com/v1/oauth2/token", "https://api.paypal.com/v1/oauth2/token", "https://api.sandbox.paypal.com/v1/oauth2/token",
    "http://api-m.sandbox.paypal.com/v1/oauth2/token", "https://api-m.sandbox.paypal.com:8443/v1/oauth2/token",
    "https://api-m.sandbox.paypal.com.evil.example/v1/oauth2/token", "https://user@api-m.sandbox.paypal.com/v1/oauth2/token",
    "https://api-m.sandbox.paypal.com/v1/oauth2/token?x=1", "https://example.com/v1/oauth2/token"])
def test_production_and_foreign_hosts_schemes_ports_and_queries_are_blocked(url):
    t = make(Stage.CREATE)
    with pytest.raises(BlockedRequest):
        t.handle_request(_req("POST", url))
    assert t._inner.seen == []


@pytest.mark.parametrize("method,path", [
    ("POST", "/v2/invoicing/invoices/INV2-0001-AAAAAAAA/cancel"), ("POST", "/v2/invoicing/invoices/INV2-0001-AAAAAAAA/payments"),
    ("POST", "/v2/invoicing/invoices/INV2-0001-AAAAAAAA/refunds"), ("POST", "/v2/invoicing/invoices/INV2-0001-AAAAAAAA/remind"),
    ("DELETE", "/v2/invoicing/invoices/INV2-0001-AAAAAAAA"), ("PUT", "/v2/invoicing/invoices/INV2-0001-AAAAAAAA"),
    ("PATCH", "/v2/invoicing/invoices/INV2-0001-AAAAAAAA"), ("GET", "/v2/invoicing/invoices"), ("POST", "/v2/payments/captures/X1/refund"),
    ("POST", "/v1/payments/payouts"), ("POST", "/v2/checkout/orders/X1/capture"), ("GET", "/v2/invoicing/invoices/INV2-0002-BBBBBBBB"),
    ("POST", "/v2/invoicing/invoices/INV2-0002-BBBBBBBB/send")])
@pytest.mark.parametrize("stage", list(Stage))
def test_every_other_endpoint_is_blocked_in_every_stage(stage, method, path):
    t = make(stage, None if stage is Stage.CREATE else "INV2-0001-AAAAAAAA")
    with pytest.raises(BlockedRequest):
        t.handle_request(_req(method, SB + path, {"x": 1} if method != "GET" else None))
    assert t._inner.seen == []


def test_the_existing_smoke_invoice_can_never_be_named_or_reached():
    smoke = next(iter(DENIED_INVOICE_IDS))
    for stage in (Stage.SEND, Stage.READ):
        with pytest.raises(ValueError):
            make(stage, smoke)
    t = make(Stage.CREATE)
    with pytest.raises(BlockedRequest):
        t.handle_request(_req("GET", f"{SB}/v2/invoicing/invoices/{smoke}"))
    bad = {**GOOD_BODY, "detail": {"invoice_number": "SMOKE-0123456789AB", "currency_code": "USD"}}
    with pytest.raises(BlockedRequest):
        t.handle_request(_req("POST", SB + "/v2/invoicing/invoices", bad))
    assert t._inner.seen == []


@pytest.mark.parametrize("mutate", [
    lambda b: b["items"][0]["unit_amount"].update(value="2.00"), lambda b: b["items"][0]["unit_amount"].update(currency_code="EUR"),
    lambda b: b["items"].append(b["items"][0]), lambda b: b["items"][0].update(quantity="2"),
    lambda b: b["primary_recipients"][0]["billing_info"].update(email_address="someone-else@example.org"),
    lambda b: b["primary_recipients"].append(b["primary_recipients"][0])])
def test_a_create_body_other_than_one_1_00_usd_item_for_the_buyer_is_blocked(mutate):
    import copy
    body = copy.deepcopy(GOOD_BODY)
    mutate(body)
    t = make(Stage.CREATE)
    with pytest.raises(BlockedRequest):
        t.handle_request(_req("POST", SB + "/v2/invoicing/invoices", body))
    assert t._inner.seen == []


def test_request_budgets_and_one_mutation_per_stage():
    t = make(Stage.CREATE, inner=Recorder())
    for _ in range(2):
        t.handle_request(_req("POST", SB + "/v1/oauth2/token"))
    with pytest.raises(BlockedRequest):
        t.handle_request(_req("POST", SB + "/v1/oauth2/token"))
    r = make(Stage.READ, "INV2-0001-AAAAAAAA")
    for _ in range(2):
        r.handle_request(_req("GET", SB + "/v2/invoicing/invoices/INV2-0001-AAAAAAAA"))
    with pytest.raises(BlockedRequest):
        r.handle_request(_req("GET", SB + "/v2/invoicing/invoices/INV2-0001-AAAAAAAA"))
    c = make(Stage.CREATE)
    c.handle_request(_req("POST", SB + "/v2/invoicing/invoices", GOOD_BODY))
    with pytest.raises(BlockedRequest):
        c.handle_request(_req("POST", SB + "/v2/invoicing/invoices", GOOD_BODY))


def test_send_needs_the_send_stage_and_the_fixed_body():
    s = make(Stage.SEND, "INV2-0001-AAAAAAAA")
    with pytest.raises(BlockedRequest):
        s.handle_request(_req("POST", SB + "/v2/invoicing/invoices/INV2-0001-AAAAAAAA/send", {"send_to_recipient": True, "send_to_invoicer": True}))
    r = make(Stage.READ, "INV2-0001-AAAAAAAA")
    with pytest.raises(BlockedRequest):
        r.handle_request(_req("POST", SB + "/v2/invoicing/invoices/INV2-0001-AAAAAAAA/send", {"send_to_recipient": True, "send_to_invoicer": False}))


# --- a full controlled run against the fake, with separate approvals -------------------------------

def test_full_run_makes_only_allowed_calls_and_attests_nothing_when_not_live_default(tmp_path, capsys):
    fake = FakePayPal()
    full_run(fake, tmp_path)
    assert {(m, p) for m, p in fake.calls if m == "POST"} <= ALLOWED_CALLS | {("POST", f"/v2/invoicing/invoices/{fake.only()['id']}/send")}
    assert all(m in ("POST", "GET") for m, _ in fake.calls) and len(fake.invoices) == 1
    assert fake.count("POST", "/v2/invoicing/invoices") == 1 and fake.count("POST", "/send") == 1
    assert runner.main(["--stage", "recover", "--workdir", str(tmp_path)], BASE_ENV, _never) == runner.EXIT_OK


def test_send_cannot_run_before_create_and_needs_its_own_confirmation(tmp_path):
    fake = FakePayPal()
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    assert go(fake, "send", tmp_path, confirm=False, env=CONFIRM[Stage.CREATE]) == runner.EXIT_REFUSED  # the create approval does not authorise sending
    assert fake.count("POST", "/send") == 0
    assert go(fake, "send", tmp_path) == runner.EXIT_OK
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED  # one send only
    assert fake.count("POST", "/send") == 1


def test_a_second_create_is_refused(tmp_path):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    assert go(fake, "create", tmp_path) == runner.EXIT_REFUSED
    assert len(fake.invoices) == 1


def test_an_ambiguous_create_is_recorded_and_never_retried(tmp_path):
    fake = FakePayPal()
    fake.fault("POST", "/v2/invoicing/invoices", "create_then_timeout")
    assert go(fake, "create", tmp_path) == runner.EXIT_AMBIGUOUS
    state = RunState.load(tmp_path)
    assert state.ambiguous and state.create_attempted and state.create_outcome == "AMBIGUOUS"
    assert fake.count("POST", "/v2/invoicing/invoices") == 1
    assert go(fake, "create", tmp_path) == runner.EXIT_REFUSED
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED
    assert fake.count("POST", "/v2/invoicing/invoices") == 1 and fake.count("POST", "/send") == 0


def test_an_ambiguous_send_is_recorded_and_never_retried(tmp_path):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    fake.fault("POST", "/send", 500)
    assert go(fake, "send", tmp_path) == runner.EXIT_AMBIGUOUS
    assert RunState.load(tmp_path).ambiguous
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED
    assert fake.count("POST", "/send") == 1


def test_an_unrecognised_response_shape_is_reported_by_key_names_only(tmp_path, capsys):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    assert go(fake, "send", tmp_path) == runner.EXIT_OK
    invoice = fake.only()
    invoice["status"] = "SOMETHING_NEW"
    invoice["secret_note"] = "TOP-SECRET-VALUE"
    capsys.readouterr()
    assert go(fake, "read", tmp_path) == runner.EXIT_UNREADABLE
    out = capsys.readouterr().out
    assert "UNREADABLE" in out and "TOP-SECRET-VALUE" not in out and BUYER not in out
    assert RunState.load(tmp_path).last_status is None


def test_an_unknown_transaction_type_fails_closed(tmp_path, capsys):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    assert go(fake, "send", tmp_path) == runner.EXIT_OK
    fake.pay(fake.only(), kind="PAYPAL")
    fake.only()["payments"]["transactions"][0]["type"] = "MYSTERY"
    capsys.readouterr()
    assert go(fake, "read", tmp_path) == runner.EXIT_UNREADABLE
    assert "payments:MYSTERY" in capsys.readouterr().out  # a sanitized type word; no other value is echoed
    assert RunState.load(tmp_path).last_status is None


def test_output_never_contains_credentials_tokens_or_the_full_buyer_address(tmp_path, capsys):
    fake = FakePayPal()
    full_run(fake, tmp_path)
    runner.main(["--stage", "recover", "--workdir", str(tmp_path)], BASE_ENV, _never)
    out = capsys.readouterr().out
    assert CLIENT_SECRET not in out and CLIENT_ID not in out and "fake-token-" not in out and BUYER not in out
    assert re.search(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", out) is None
    state_text = (tmp_path / "run_state.json").read_text(encoding="utf8")
    assert CLIENT_SECRET not in state_text and BUYER not in state_text and "token" not in state_text.lower().replace("authenticated", "")


# --- provenance ----------------------------------------------------------------------------------

class FakeLive(httpx.HTTPTransport):
    """Stands in for the genuine network transport in tests: same type check as production, but it only ever talks to the fake PayPal."""

    def __init__(self, fake):  # deliberately no super().__init__: no connection pool, no socket
        self._fake = fake.transport()

    def handle_request(self, request):
        return self._fake.handle_request(request)

    def close(self):
        pass


@pytest.fixture()
def live(monkeypatch):
    """Make `httpx.HTTPTransport` the FakeLive class so the runner's `type(inner) is httpx.HTTPTransport` test passes for it, and only for it."""
    monkeypatch.setattr(httpx, "HTTPTransport", FakeLive)


def go_live(fake, stage, workdir):
    argv = ["--stage", stage, "--workdir", str(workdir), "--execute"]
    return runner.main(argv, {**BASE_ENV, **CONFIRM[Stage(stage)]}, lambda: FakeLive(fake))


def live_run_until_read(fake, workdir, mutate=None):
    assert go_live(fake, "create", workdir) == runner.EXIT_OK
    assert go_live(fake, "send", workdir) == runner.EXIT_OK
    fake.pay(fake.only())
    if mutate:
        mutate(fake.only())
    return go_live(fake, "read", workdir)


def test_only_an_authenticated_live_read_attests_the_evidence_and_the_label_follows(tmp_path, live):
    fake = FakePayPal()
    assert live_run_until_read(fake, tmp_path) == runner.EXIT_OK
    state = RunState.load(tmp_path)
    assert state.authenticated_payment_ids == [state.payment_id]
    assert runner.main(["--stage", "recover", "--workdir", str(tmp_path)], BASE_ENV, _never) == runner.EXIT_OK
    assert attestation.provider_label("PAYPAL_SANDBOX", state.payment_id) == attestation.LABEL_AUTHENTICATED


def test_a_mock_or_wrapped_transport_is_never_live(tmp_path):
    fake = FakePayPal()
    full_run(fake, tmp_path)  # MockTransport: type is not httpx.HTTPTransport
    assert RunState.load(tmp_path).authenticated_payment_ids == []

    class Wrapper(httpx.BaseTransport):
        def handle_request(self, request):
            return fake.handle_request(request)

    other = tmp_path.parent / (tmp_path.name + "-b")
    fake2 = FakePayPal()
    assert runner.main(["--stage", "create", "--workdir", str(other), "--execute"], {**BASE_ENV, **CONFIRM[Stage.CREATE]}, lambda: Wrapper()) in (runner.EXIT_OK, runner.EXIT_PROVIDER_ERROR, runner.EXIT_AMBIGUOUS)
    assert fake2.calls == []  # (the wrapper pointed at the first fake; nothing is attested either way)
    state = RunState.load(other)
    assert state is None or state.authenticated_payment_ids == []


@pytest.mark.parametrize("label,mutate", [
    ("merchant is another account", lambda inv: inv.update(invoicer={"email_address": "someone-else@example.com"})),
    ("merchant is missing", lambda inv: inv.pop("invoicer")),
    ("merchant is empty", lambda inv: inv.update(invoicer={})),
    ("invoice number differs", lambda inv: inv["detail"].update(invoice_number="PEM-OTHER")),
    ("recipient differs", lambda inv: inv["primary_recipients"][0]["billing_info"].update(email_address="other-buyer@example.org")),
    ("amount differs", lambda inv: inv["amount"].update(value="2.00")),
    ("currency differs", lambda inv: inv["amount"].update(currency_code="EUR")),
    ("transaction id missing", lambda inv: inv["payments"]["transactions"][0].update(payment_id=None)),
    ("transaction id empty", lambda inv: inv["payments"]["transactions"][0].update(payment_id="")),
    ("payment recorded as EXTERNAL", lambda inv: inv["payments"]["transactions"][0].update(type="EXTERNAL")),
    ("PAID with no transactions", lambda inv: inv["payments"].update(transactions=[])),
    ("PAID but amount below total", lambda inv: inv["payments"]["transactions"][0]["amount"].update(value="0.50")),
    ("transaction currency differs", lambda inv: inv["payments"]["transactions"][0]["amount"].update(currency_code="EUR")),
    ("payments is not an object", lambda inv: inv.update(payments=[])),
    ("transactions is not a list", lambda inv: inv["payments"].update(transactions="x")),
    ("refund without an id", lambda inv: inv["refunds"].update(transactions=[{"type": "PAYPAL", "refund_id": None, "amount": {"currency_code": "USD", "value": "1.00"}}])),
])
def test_a_read_that_does_not_match_the_run_is_never_promoted(tmp_path, live, label, mutate):
    fake = FakePayPal()
    live_run_until_read(fake, tmp_path, mutate)  # the exit code may be anything; what matters is that nothing is attested
    state = RunState.load(tmp_path)
    assert state.authenticated_payment_ids == [], label
    assert attestation.provider_label("PAYPAL_SANDBOX", state.payment_id) == attestation.LABEL_UNATTESTED


def test_a_mismatched_invoice_id_in_the_body_is_never_promoted(tmp_path, live):
    fake = FakePayPal()
    live_run_until_read(fake, tmp_path, lambda inv: inv.update(id="INV2-9999-ZZZZZZZZ"))
    assert RunState.load(tmp_path).authenticated_payment_ids == []


def test_an_unrecognised_status_is_unreadable_and_not_promoted(tmp_path, live, capsys):
    fake = FakePayPal()
    assert live_run_until_read(fake, tmp_path, lambda inv: inv.update(status="WEIRD")) == runner.EXIT_UNREADABLE
    assert RunState.load(tmp_path).authenticated_payment_ids == []


@pytest.mark.parametrize("body", [b"[]", b"null", b"not json", b'"text"', b"{}"])
def test_a_malformed_successful_response_is_unreadable_and_not_promoted(tmp_path, live, body):
    fake = FakePayPal()
    assert go_live(fake, "create", tmp_path) == runner.EXIT_OK
    assert go_live(fake, "send", tmp_path) == runner.EXIT_OK
    real_handle = fake.handle

    def malformed(request):
        response = real_handle(request)
        return httpx.Response(200, content=body) if request.method == "GET" else response

    fake.handle = malformed
    assert go_live(fake, "read", tmp_path) == runner.EXIT_UNREADABLE
    assert RunState.load(tmp_path).authenticated_payment_ids == []


def test_a_draft_read_back_with_the_wrong_merchant_does_not_verify_the_draft(tmp_path):
    fake = FakePayPal()
    fake.validate_invoicer = False
    fake.invoicer_mode = "other"
    go(fake, "create", tmp_path)
    state = RunState.load(tmp_path)
    assert state.draft_verified is False
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED and fake.count("POST", "/send") == 0


def test_a_draft_read_back_without_a_merchant_does_not_verify_the_draft(tmp_path):
    fake = FakePayPal()
    fake.invoicer_mode = "omit"
    go(fake, "create", tmp_path)
    assert RunState.load(tmp_path).draft_verified is False
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED


def test_the_origin_and_path_of_the_read_are_part_of_the_promotion_condition():
    t = RunnerTransport(Recorder(), Stage.READ, buyer_email=BUYER, invoice_id="INV2-0001-AAAAAAAA", live=True, merchant_email=MERCHANT, invoice_number="PEM-X")
    body = {"id": "INV2-0001-AAAAAAAA", "status": "DRAFT", "detail": {"invoice_number": "PEM-X", "currency_code": "USD"}, "invoicer": {"email_address": MERCHANT},
            "primary_recipients": [{"billing_info": {"email_address": BUYER}}], "amount": {"currency_code": "USD", "value": "1.00"}}
    good = httpx.Response(200, json=body)
    assert t._promotable(good, _req("GET", f"{SB}/v2/invoicing/invoices/INV2-0001-AAAAAAAA")) is True
    for url in ("https://api-m.paypal.com/v2/invoicing/invoices/INV2-0001-AAAAAAAA", "http://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-0001-AAAAAAAA",
                f"{SB}/v2/invoicing/invoices/INV2-0002-BBBBBBBB"):
        assert t._promotable(httpx.Response(200, json=body), _req("GET", url)) is False
        assert "origin_or_path_mismatch" in t.read_failures
    assert t._promotable(httpx.Response(201, json=body), _req("GET", f"{SB}/v2/invoicing/invoices/INV2-0001-AAAAAAAA")) is False
    assert "status_not_200" in t.read_failures
    t_not_live = RunnerTransport(Recorder(), Stage.READ, buyer_email=BUYER, invoice_id="INV2-0001-AAAAAAAA", live=False, merchant_email=MERCHANT, invoice_number="PEM-X")
    assert t_not_live._promotable(httpx.Response(200, json=body), _req("GET", f"{SB}/v2/invoicing/invoices/INV2-0001-AAAAAAAA")) is False
    assert "transport_not_live" in t_not_live.read_failures


def test_no_expected_merchant_or_invoice_number_means_no_promotion():
    t = RunnerTransport(Recorder(), Stage.READ, buyer_email=BUYER, invoice_id="INV2-0001-AAAAAAAA", live=True)
    assert t._promotable(httpx.Response(200, json={"id": "INV2-0001-AAAAAAAA", "status": "DRAFT"}), _req("GET", f"{SB}/v2/invoicing/invoices/INV2-0001-AAAAAAAA")) is False
    assert {"no_expected_merchant", "no_expected_invoice_number"} <= set(t.read_failures)


# --- provenance through the pipeline -----------------------------------------------------------

def _pipeline(tmp_path):
    state = RunState.load(tmp_path)
    blocked = RunnerTransport(runner._NoNetwork(), Stage.READ, buyer_email=BUYER, invoice_id=state.invoice_id)
    with runner.session(tmp_path, runner.load_config(BASE_ENV), blocked) as (_app, api):
        root = f"/api/v1/projects/{state.project_id}"
        observations = api.get(f"{root}/payment-observations").json()
        unified = api.get(f"{root}/unified-report").json()
        markdown = api.get(f"{root}/unified-report/markdown").text
    return state, observations, unified, markdown


def _unified_payment_rows(unified):
    return unified["payments"]["payments"]


def test_authenticated_provenance_reaches_observations_and_both_reports(tmp_path, live):
    fake = FakePayPal()
    assert live_run_until_read(fake, tmp_path) == runner.EXIT_OK
    assert runner.main(["--stage", "recover", "--workdir", str(tmp_path)], BASE_ENV, _never) == runner.EXIT_OK
    state, observations, unified, markdown = _pipeline(tmp_path)
    provider_obs = [o for o in observations if o["source"] == "PROVIDER_API"]
    assert provider_obs and all(o["provider_provenance"] == "AUTHENTICATED_PAYPAL_SANDBOX" for o in provider_obs)
    assert all(o["provider_provenance"] is None for o in observations if o["source"] != "PROVIDER_API")
    assert [r["provider_provenance"] for r in _unified_payment_rows(unified)] == ["AUTHENTICATED_PAYPAL_SANDBOX"]
    assert "[AUTHENTICATED_PAYPAL_SANDBOX]" in markdown.replace("\\", "")  # the markdown escaper backslash-escapes underscores


def test_unattested_sandbox_evidence_is_labelled_unattested_everywhere(tmp_path):
    fake = FakePayPal()
    full_run(fake, tmp_path)  # MockTransport: never attested
    state, observations, unified, markdown = _pipeline(tmp_path)
    provider_obs = [o for o in observations if o["source"] == "PROVIDER_API"]
    assert provider_obs and all(o["provider_provenance"] == "UNATTESTED_SANDBOX_PROVIDER" for o in provider_obs)
    assert [r["provider_provenance"] for r in _unified_payment_rows(unified)] == ["UNATTESTED_SANDBOX_PROVIDER"]
    plain = markdown.replace("\\", "")
    assert "[UNATTESTED_SANDBOX_PROVIDER]" in plain and "AUTHENTICATED_PAYPAL_SANDBOX" not in plain


def test_mock_provider_evidence_stays_mock_in_observations_and_reports(seller):
    from tests.test_payments import INVOICE, SEND, invoice, send, sync
    pid = seller.post("/api/v1/projects", json={"title": "Mock"}).json()["id"]
    seller.post(f"/api/v1/projects/{pid}/scope-records", json={"kind": "ORIGINAL", "source_label": "a", "content": "b"})
    created = invoice(seller, pid).json()
    assert send(seller, pid, created["id"]).status_code == 200
    assert sync(seller, pid).status_code == 200
    observations = seller.get(f"/api/v1/projects/{pid}/payment-observations").json()
    assert {o["provider_provenance"] for o in observations if o["source"] == "PROVIDER_API"} == {"SIMULATED_MOCK_PROVIDER"}
    rows = seller.get(f"/api/v1/projects/{pid}/unified-report").json()["payments"]["payments"]
    assert [r["provider_provenance"] for r in rows] == ["SIMULATED_MOCK_PROVIDER"]


# --- state persistence ordering ------------------------------------------------------------------

def test_attempted_is_persisted_before_the_create_request_leaves_the_process(tmp_path):
    fake = FakePayPal()
    seen = {}

    def hook(method, path):
        if method == "POST" and path == "/v2/invoicing/invoices":
            seen["state"] = RunState.load(tmp_path)

    fake.on_request = hook
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    assert seen["state"].create_attempted is True and seen["state"].create_outcome is None


def test_attempted_is_persisted_before_the_send_request_leaves_the_process(tmp_path):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    seen = {}

    def hook(method, path):
        if method == "POST" and path.endswith("/send"):
            seen["state"] = RunState.load(tmp_path)

    fake.on_request = hook
    assert go(fake, "send", tmp_path) == runner.EXIT_OK
    assert seen["state"].send_attempted is True and seen["state"].send_outcome is None


def test_rehearsal_and_unattested_sandbox_and_mock_labels_stay_distinct():
    assert attestation.provider_label("MOCK_PAYPAL", "p1") == "SIMULATED_MOCK_PROVIDER"
    assert attestation.provider_label("PAYPAL_SANDBOX", "p1") == "UNATTESTED_SANDBOX_PROVIDER"
    attestation.attest("p1")
    assert attestation.provider_label("PAYPAL_SANDBOX", "p1") == "AUTHENTICATED_PAYPAL_SANDBOX"
    assert attestation.provider_label("MOCK_PAYPAL", "p1") == "SIMULATED_MOCK_PROVIDER"  # a mock record can never be promoted


def test_a_non_live_transport_never_marks_a_read_as_authenticated():
    fake = FakePayPal()
    t = RunnerTransport(fake.transport(), Stage.CREATE, buyer_email=BUYER, live=False)
    assert t.authenticated_read is False


def test_only_the_runner_may_attest():
    offenders = []
    for path in list(APP_DIR.rglob("*.py")) + list((REPO / "scripts").glob("*.py")):
        if path.name in ("sandbox_attestation.py", "sandbox_runner.py"):
            continue
        if re.search(r"\battest\(", path.read_text(encoding="utf8")):
            offenders.append(path.name)
    assert offenders == []


def test_the_runner_source_never_touches_the_smoke_state_file_or_reads_env_files():
    import ast
    for name in ("sandbox_runner.py", "sandbox_runner_guard.py", "sandbox_runner_rehearsal.py", "simulation/runner_recovery.py"):
        tree = ast.parse((APP_DIR / name).read_text(encoding="utf8"))
        for node in ast.walk(tree):  # drop docstrings: only executable string constants are inspected
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr)                     and isinstance(getattr(node.body[0], "value", None), ast.Constant):
                node.body = node.body[1:] or [ast.Pass()]
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        banned = ("state.json", ".env") if name == "sandbox_runner_guard.py" else ("state.json", ".smoke", ".env")  # the guard names .smoke only to REFUSE it as a workdir
        assert not [t for t in strings if t in banned or t.startswith(".env")], name


# --- hardening from the independent security review -------------------------------------------------

def test_a_corrupt_or_unexpected_run_state_refuses_the_stage(tmp_path):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    path = tmp_path / "run_state.json"
    path.write_text('{"run_id": "x", "surprise": 1}', encoding="utf8")
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED
    path.write_text("not json", encoding="utf8")
    assert go(fake, "create", tmp_path) == runner.EXIT_REFUSED
    assert fake.count("POST", "/send") == 0 and len(fake.invoices) == 1


def test_a_database_without_its_run_state_is_never_treated_as_a_fresh_run(tmp_path):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    (tmp_path / "run_state.json").unlink()
    assert go(fake, "create", tmp_path) == runner.EXIT_REFUSED
    assert len(fake.invoices) == 1


def test_a_held_stage_lock_refuses_a_concurrent_stage(tmp_path):
    fake = FakePayPal()
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / ".stage.lock").write_text("", encoding="utf8")
    assert go(fake, "create", tmp_path) == runner.EXIT_REFUSED
    assert fake.calls == []
    (tmp_path / ".stage.lock").unlink()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    assert not (tmp_path / ".stage.lock").exists()  # released after the stage


def test_a_created_id_that_could_widen_the_get_allowlist_is_not_adopted():
    for evil in ("X1234567/send", "INV2-0001/../../x", "INV2-0001-AAAAAAAA?x=1"):
        inner = httpx.MockTransport(lambda r, evil=evil: httpx.Response(201, json={"id": evil}))
        t = make(Stage.CREATE, inner=inner)
        t.handle_request(_req("POST", SB + "/v2/invoicing/invoices", GOOD_BODY))
        assert t.ambiguous_mutation and t.created_invoice_id is None
        with pytest.raises(BlockedRequest):
            t.handle_request(_req("GET", f"{SB}/v2/invoicing/invoices/{evil}"))


def test_a_create_without_a_learnable_invoice_id_is_ambiguous_not_ok(tmp_path):
    fake = FakePayPal()
    fake.create_response = lambda invoice_id: httpx.Response(202, json={})
    assert go(fake, "create", tmp_path) == runner.EXIT_AMBIGUOUS
    state = RunState.load(tmp_path)
    assert state.ambiguous and state.create_outcome == "AMBIGUOUS" and state.invoice_id is None
    assert go(fake, "create", tmp_path) == runner.EXIT_REFUSED


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(additional_recipients=[{"email_address": "x@example.org"}]), lambda b: b.update(amount={"breakdown": {}}),
    lambda b: b["detail"].update(payment_term={"term_type": "NET_10"}), lambda b: b["items"][0].update(discount={"percent": "100"}),
    lambda b: b["primary_recipients"][0].update(shipping_info={"name": "x"}), lambda b: b.update(configuration={"allow_tip": True})])
def test_a_create_body_with_extra_fields_is_blocked(mutate):
    import copy
    body = copy.deepcopy(GOOD_BODY)
    mutate(body)
    t = make(Stage.CREATE)
    with pytest.raises(BlockedRequest):
        t.handle_request(_req("POST", SB + "/v2/invoicing/invoices", body))


def test_an_unexpected_failure_after_a_sent_mutation_is_recorded_as_ambiguous(tmp_path, monkeypatch, capsys):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    original_post = runner.Api.post

    def post_then_fail(self, path, *a, **k):
        response = original_post(self, path, *a, **k)
        if path.endswith("/send"):
            raise KeyError("boom after the send went out")
        return response

    monkeypatch.setattr(runner.Api, "post", post_then_fail)
    assert go(fake, "send", tmp_path) == runner.EXIT_AMBIGUOUS
    out = capsys.readouterr().out
    assert "Traceback" not in out and "boom" not in out and "KeyError" in out
    state = RunState.load(tmp_path)
    assert state.ambiguous and state.send_outcome == "AMBIGUOUS" and fake.count("POST", "/send") == 1
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED


def test_an_unexpected_failure_before_any_mutation_is_not_marked_ambiguous(tmp_path, monkeypatch):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    monkeypatch.setattr(runner.Api, "post", lambda self, *a, **k: (_ for _ in ()).throw(KeyError("x")))
    assert go(fake, "send", tmp_path) == runner.EXIT_PROVIDER_ERROR
    assert not RunState.load(tmp_path).ambiguous and fake.count("POST", "/send") == 0


# --- READ reconciliation check and exit status; uncertain 4xx on a mutation -----------------------------------------------------

@pytest.mark.parametrize("word,invoice_state,status,expected", [
    ("DRAFT", "DRAFT", "UNVERIFIED", True),       # an unsent draft is correctly UNVERIFIED: nothing is owed or paid
    ("SENT", "SENT", "PENDING", True),
    ("UNPAID", "UNPAID", "PENDING", True),
    ("PAID", "PAID", "PAID", True),
    ("SENT", "SENT", "UNVERIFIED", False),        # a sent invoice that did not leave UNVERIFIED was not reconciled
    ("PAID", "PAID", "PENDING", False),           # never weaken: PAID needs PAID
    ("DRAFT", "DRAFT", "PENDING", False),         # a draft must not look sent
    ("DRAFT", "DRAFT", "PAID", False),
    ("PAID", "PAID", "NEEDS_REVIEW", False),
    ("DRAFT", "DRAFT", "NEEDS_REVIEW", False),
    ("SENT", "DRAFT", "PENDING", False),          # the local record disagrees with the provider's word
    ("DRAFT", None, "UNVERIFIED", False),
])
def test_read_reconciled_requires_the_status_that_matches_the_provider_word(word, invoice_state, status, expected):
    payment = {"provider_status": status, "invoice_state": invoice_state, "amount_minor": 100, "amount_paid_minor": 100 if status == "PAID" else 0}
    assert runner._read_reconciled(payment, word) is expected


@pytest.mark.parametrize("word,status,paid,expected", [
    ("DRAFT", "UNVERIFIED", 100, False),   # an unsent draft can never have money on it
    ("SENT", "PENDING", 50, False),
    ("UNPAID", "PENDING", 1, False),
    ("PAID", "PAID", 0, False),            # PAID with nothing paid is never accepted
    ("PAID", "PAID", 99, False),
    ("PAID", "PAID", 100, True),
    ("PAID", "PAID", None, False),
])
def test_read_reconciled_checks_the_paid_amount_never_just_the_status_word(word, status, paid, expected):
    payment = {"provider_status": status, "invoice_state": word, "amount_minor": 100, "amount_paid_minor": paid}
    assert runner._read_reconciled(payment, word) is expected


def test_reading_an_unsent_draft_passes_every_check_and_pays_nothing(tmp_path, live, capsys):
    fake = FakePayPal()
    assert go_live(fake, "create", tmp_path) == runner.EXIT_OK
    capsys.readouterr()
    assert go_live(fake, "read", tmp_path) == runner.EXIT_OK
    out = capsys.readouterr().out
    state = RunState.load(tmp_path)
    assert "[PASS] the evidence was recognised and reconciled" in out and "FAIL" not in out
    assert state.last_status == "DRAFT" and state.send_attempted is False
    assert state.authenticated_payment_ids == [state.payment_id]
    assert fake.only()["status"] == "DRAFT" and fake.count("POST", "/send") == 0


def test_reading_a_sent_unpaid_invoice_passes_and_is_not_paid(tmp_path, live, capsys):
    fake = FakePayPal()
    assert go_live(fake, "create", tmp_path) == runner.EXIT_OK
    assert go_live(fake, "send", tmp_path) == runner.EXIT_OK
    assert go_live(fake, "read", tmp_path) == runner.EXIT_OK
    assert RunState.load(tmp_path).last_status == "SENT"
    assert "FAIL" not in capsys.readouterr().out


def test_reading_a_paid_invoice_still_passes(tmp_path, live, capsys):
    assert live_run_until_read(FakePayPal(), tmp_path) == runner.EXIT_OK
    assert RunState.load(tmp_path).last_status == "PAID" and "FAIL" not in capsys.readouterr().out


def test_a_failed_validation_check_makes_the_read_exit_nonzero_without_losing_the_evidence(tmp_path, live, monkeypatch, capsys):
    fake = FakePayPal()
    assert go_live(fake, "create", tmp_path) == runner.EXIT_OK
    monkeypatch.setattr(runner, "_read_reconciled", lambda payment, word: False)
    assert go_live(fake, "read", tmp_path) == runner.EXIT_CHECK_FAILED
    state = RunState.load(tmp_path)
    assert "read" in state.stages_done and state.last_status == "DRAFT"  # the read itself is still recorded
    assert "[FAIL] the evidence was recognised and reconciled" in capsys.readouterr().out


def test_an_incomplete_read_response_is_unreadable_not_ok(tmp_path, live):
    fake = FakePayPal()
    assert go_live(fake, "create", tmp_path) == runner.EXIT_OK
    real_handle = fake.handle
    fake.handle = lambda request: httpx.Response(200, json={"status": "DRAFT"}) if request.method == "GET" else real_handle(request)
    assert go_live(fake, "read", tmp_path) == runner.EXIT_UNREADABLE
    assert RunState.load(tmp_path).authenticated_payment_ids == []


def _create_then_send_with_fault(tmp_path, fault):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    fake.fault("POST", "/send", fault)
    return fake, go(fake, "send", tmp_path)


@pytest.mark.parametrize("status", [406, 415, 408, 409])
def test_an_uncertain_4xx_on_send_is_ambiguous_and_blocks_every_retry(tmp_path, status):
    fake, code = _create_then_send_with_fault(tmp_path, status)
    state = RunState.load(tmp_path)
    assert code == runner.EXIT_AMBIGUOUS
    assert state.send_attempted is True and state.send_outcome == "AMBIGUOUS" and state.ambiguous is True
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED and fake.count("POST", "/send") == 1


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_structured_paypal_rejection_on_send_remains_a_definitive_refusal(tmp_path, status):
    fake, code = _create_then_send_with_fault(tmp_path, status)
    state = RunState.load(tmp_path)
    assert code == runner.EXIT_PROVIDER_ERROR
    assert state.send_outcome == "REFUSED" and state.ambiguous is False and state.send_attempted is True


def test_a_4xx_without_a_paypal_error_body_is_ambiguous(tmp_path):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    real_handle = fake.handle
    fake.handle = lambda r: httpx.Response(422, text="<html>gateway</html>") if r.url.path.endswith("/send") else real_handle(r)
    assert go(fake, "send", tmp_path) == runner.EXIT_AMBIGUOUS
    assert RunState.load(tmp_path).ambiguous is True


# --- ambiguity never blocks the read-only check; sanitized error metadata; a blocked retry never leaves an unrecorded mutation ----

def test_a_read_is_allowed_after_an_ambiguous_send_and_preserves_the_ambiguity(tmp_path, capsys):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    fake.fault("POST", "/send", "timeout")
    assert go(fake, "send", tmp_path) == runner.EXIT_AMBIGUOUS
    before = RunState.load(tmp_path)
    assert go(fake, "read", tmp_path) == runner.EXIT_OK
    after = RunState.load(tmp_path)
    assert after.ambiguous is True and after.send_attempted is True and after.send_outcome == "AMBIGUOUS"  # observed, never cleared
    assert after.last_status == "DRAFT" and before.stages_done + ["read"] == after.stages_done
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED and go(fake, "create", tmp_path) == runner.EXIT_REFUSED
    assert fake.count("POST", "/send") == 1 and fake.count("POST", "/v2/invoicing/invoices") == 1


def test_a_read_after_an_ambiguous_create_with_no_known_invoice_is_still_refused(tmp_path):
    fake = FakePayPal()
    fake.fault("POST", "/v2/invoicing/invoices", "timeout")
    assert go(fake, "create", tmp_path) == runner.EXIT_AMBIGUOUS
    calls = len(fake.calls)
    assert go(fake, "read", tmp_path) == runner.EXIT_REFUSED and len(fake.calls) == calls


def test_an_uncertain_4xx_on_create_is_ambiguous(tmp_path):
    fake = FakePayPal()
    fake.fault("POST", "/v2/invoicing/invoices", 406)
    assert go(fake, "create", tmp_path) == runner.EXIT_AMBIGUOUS
    state = RunState.load(tmp_path)
    assert state.create_outcome == "AMBIGUOUS" and state.ambiguous is True and state.invoice_id is None


def test_sanitized_provider_error_metadata_is_recorded_and_printed(tmp_path, capsys):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    fake.fault("POST", "/send", (406, {"PayPal-Debug-Id": "a1b2c3d4e5f6"}))
    assert go(fake, "send", tmp_path) == runner.EXIT_AMBIGUOUS
    state = RunState.load(tmp_path)
    assert (state.last_mutation_http, state.last_mutation_error, state.last_mutation_debug_id) == (406, "SIMULATED", "a1b2c3d4e5f6")
    printed = capsys.readouterr().out
    assert "http=406" in printed and "error=SIMULATED" in printed and "debug_id=a1b2c3d4e5f6" in printed


@pytest.mark.parametrize("hostile", ["x y", "a" * 200, "bad;id", "<script>", ""])
def test_an_unsafe_debug_id_is_never_stored(tmp_path, hostile):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    fake.fault("POST", "/send", (422, {"PayPal-Debug-Id": hostile}))
    assert go(fake, "send", tmp_path) == runner.EXIT_PROVIDER_ERROR
    state = RunState.load(tmp_path)
    assert state.last_mutation_debug_id is None and state.last_mutation_http == 422


def test_a_rate_limited_send_is_recorded_not_left_unrecorded_when_the_retry_is_blocked(tmp_path):
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_OK
    fake.fault("POST", "/send", 429, 429, 429)
    code = go(fake, "send", tmp_path)
    state = RunState.load(tmp_path)
    assert code == runner.EXIT_PROVIDER_ERROR
    assert state.send_attempted is True and state.send_outcome == "REFUSED" and state.ambiguous is False and state.last_mutation_http == 429
    assert "send" in state.stages_done and fake.count("POST", "/send") == 1  # the guard stopped the automatic retry
    assert go(fake, "send", tmp_path) == runner.EXIT_REFUSED


# --- findings of the independent security review ------------------------------------------------------------------------------

def test_a_create_whose_readback_is_blocked_is_ambiguous_not_ok(tmp_path, monkeypatch):
    from app import sandbox_runner_guard as guard
    monkeypatch.setattr(guard, "CREATE_GET_BUDGET", 0)  # the create POST succeeds, then the guard refuses the read-back
    fake = FakePayPal()
    assert go(fake, "create", tmp_path) == runner.EXIT_AMBIGUOUS
    state = RunState.load(tmp_path)
    assert state.create_attempted is True and state.create_outcome == "AMBIGUOUS" and state.ambiguous is True and state.draft_verified is False
    assert go(fake, "create", tmp_path) == runner.EXIT_REFUSED and fake.count("POST", "/v2/invoicing/invoices") == 1


def test_error_metadata_survives_an_unexpected_failure_after_a_sent_mutation(tmp_path):
    from app.sandbox_runner_guard import RunnerTransport
    transport = RunnerTransport(httpx.MockTransport(lambda r: httpx.Response(500)), Stage.SEND, buyer_email=BUYER, invoice_id="INV2-AAAA-BBBBBBBB")
    transport.mutation_sent, transport.mutation_status = True, 406
    transport.mutation_error_name, transport.mutation_debug_id = "MEDIA_TYPE_NOT_ACCEPTABLE", "dbg-1234abcd"
    state = RunState(run_id="r1", buyer_masked="b***@example.org", invoice_number="PEM-X")
    out = runner.Out(lambda t: t, [], [])
    assert runner._unexpected(tmp_path, state, transport, "send", RuntimeError("boom"), out) == runner.EXIT_AMBIGUOUS
    saved = RunState.load(tmp_path)
    assert (saved.last_mutation_http, saved.last_mutation_error, saved.last_mutation_debug_id) == (406, "MEDIA_TYPE_NOT_ACCEPTABLE", "dbg-1234abcd")
    assert saved.send_outcome == "AMBIGUOUS" and saved.ambiguous is True


@pytest.mark.parametrize("field,value", [("last_mutation_debug_id", "bad id;<x>"), ("last_mutation_debug_id", "a" * 100), ("last_mutation_error", "lower_case"), ("last_mutation_error", "X" * 80)])
def test_a_state_file_with_an_unsafe_error_field_is_untrusted(tmp_path, field, value):
    import json
    from app.sandbox_runner_guard import StateCorrupt
    state = RunState(run_id="r1", buyer_masked="b***@example.org", invoice_number="PEM-X")
    state.save(tmp_path)
    path = tmp_path / "run_state.json"
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf8")), field: value}), encoding="utf8")
    with pytest.raises(StateCorrupt):
        RunState.load(tmp_path)


def test_a_blocked_retry_after_a_201_create_is_never_recorded_as_a_clean_ok(tmp_path):
    from app.sandbox_runner_guard import BlockedRequest, RunnerTransport
    transport = RunnerTransport(httpx.MockTransport(lambda r: httpx.Response(500)), Stage.CREATE, buyer_email=BUYER)
    transport.mutation_sent, transport.mutation_status = True, 201  # the create left and answered; a later request was then refused
    state = RunState(run_id="r1", buyer_masked="b***@example.org", invoice_number="PEM-X", create_attempted=True)
    code = runner._blocked_after_mutation(tmp_path, state, transport, "create", BlockedRequest("budget"), runner.Out(lambda t: t, [], []))
    saved = RunState.load(tmp_path)
    assert code == runner.EXIT_AMBIGUOUS and saved.create_outcome == "AMBIGUOUS" and saved.ambiguous is True and saved.draft_verified is False
