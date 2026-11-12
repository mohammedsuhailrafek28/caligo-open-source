"""Controlled PayPal Sandbox validation runner: one 1.00 USD invoice, driven through the real Caligo API against an ISOLATED database.

Default is a DRY RUN with no network. `--stage rehearse` (the default) runs all nine plan steps offline against the fake PayPal. A real
stage (`create`, `send`, `read`) needs `--execute`, its OWN confirmation variable, a work directory outside the repository, and the run-state
preconditions below; the `recover` stage is offline. Credentials come only from process environment variables (never a file, never printed).

    create   the only mutation: ONE 1.00 USD DRAFT invoice for the configured buyer, then the provider's own read-back (needs PAYPAL_RUNNER_CONFIRM_CREATE)
    send     ONE send of that invoice to the buyer's mailbox (needs PAYPAL_RUNNER_CONFIRM_SEND, create OK and DRAFT verified)
    read     OAuth + GET of that invoice through the sync path; records the status word; may attest the evidence (needs PAYPAL_RUNNER_CONFIRM_READ)
    recover  offline: simulated merchant divergence -> diagnosis -> plan -> preview -> human approval -> guarded execution on the PAID payment

Configuration mapping (the app's own PAYPAL_CLIENT_ID/SECRET are NOT read here): PAYPAL_SANDBOX_CLIENT_ID -> Settings.paypal_client_id,
PAYPAL_SANDBOX_CLIENT_SECRET -> paypal_client_secret, PAYPAL_SANDBOX_MERCHANT_EMAIL -> paypal_sandbox_merchant_email, PAYPAL_SMOKE_BUYER_EMAIL ->
paypal_sandbox_recipient_allowlist (the buyer only). Settings are built from these values alone: `.env` and APP_ENV are never consulted.
It never reads or writes `backend/.smoke/state.json`, never accesses the existing Sandbox draft, and never retries a mutation.
"""
import argparse
import os
import secrets
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import httpx

from app import sandbox_attestation
from app.config import Settings
from app.sandbox_runner_guard import (
    RUN_AMOUNT_MINOR, RUN_CURRENCY, BlockedRequest, RunnerTransport, RunState, Stage, StageBusy, StateCorrupt, refuse_unsafe_workdir, stage_lock,
)
from app.schemas import ProviderInvoiceState
from app.services.payments import invoice_number_for
from app.services.reconcile import _TARGET as _RECONCILED_STATUS  # the one provider-state -> financial-status table
from app.sandbox_smoke import Redactor, SmokeConfig, SmokeRefusal, load_config, mask_email

EXIT_OK, EXIT_CHECK_FAILED, EXIT_REFUSED, EXIT_UNREADABLE, EXIT_PROVIDER_ERROR, EXIT_BLOCKED, EXIT_AMBIGUOUS = 0, 1, 2, 3, 4, 5, 6
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIRMATIONS = {
    Stage.CREATE: ("PAYPAL_RUNNER_CONFIRM_CREATE", "I_APPROVE_CREATING_ONE_SANDBOX_DRAFT_INVOICE"),
    Stage.SEND: ("PAYPAL_RUNNER_CONFIRM_SEND", "I_APPROVE_SENDING_THE_SANDBOX_INVOICE_EMAIL"),
    Stage.READ: ("PAYPAL_RUNNER_CONFIRM_READ", "I_APPROVE_READING_THE_SANDBOX_INVOICE"),
}
PLANNED_REQUESTS = {
    Stage.CREATE: ("POST /v1/oauth2/token", "POST /v2/invoicing/invoices (one 1.00 USD item, DRAFT)", "GET /v2/invoicing/invoices/{id created above}"),
    Stage.SEND: ("POST /v1/oauth2/token", "POST /v2/invoicing/invoices/{recorded id}/send (emails the Sandbox buyer)"),
    Stage.READ: ("POST /v1/oauth2/token", "GET /v2/invoicing/invoices/{recorded id}"),
}
SELLER = ("sandbox-runner-seller@example.com", "synthetic-runner-pw-1")  # guards only the isolated run database
ORIGIN = {"Origin": "http://localhost:3000"}
DB_NAME = "run.sqlite"
_CREATE_READ_EXEMPT = frozenset({"no_expected_invoice_number", "transport_not_live"})  # unknown until the payment id exists / judged separately


class RunnerSettings(Settings):
    """Reads ONLY the values passed in: not `.env`, not process environment variables."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, *args, **kwargs):
        return (init_settings,)


class _NoNetwork(httpx.BaseTransport):
    """For offline stages: any attempt to reach PayPal fails loudly."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise BlockedRequest("this stage makes no PayPal request")


@dataclass
class Out:
    """Collects printed lines (already redacted) and named checks."""

    redact: Callable[[str], str]
    lines: list[str]
    checks: list[tuple[str, bool]]

    def say(self, text: str) -> None:
        line = self.redact(text)
        self.lines.append(line)
        print(line)

    def check(self, name: str, passed: bool) -> bool:
        self.checks.append((name, bool(passed)))
        self.say(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        return bool(passed)


def make_app(workdir: Path, config: SmokeConfig, transport: httpx.BaseTransport):
    from app.main import create_app
    from app.paypal_sandbox import PayPalSandboxProvider
    credentials = dict(zip(("paypal_client_id", "paypal_client_secret"), (config.client_id, config.client_secret)))
    settings = RunnerSettings(
        database_url=f"sqlite:///{workdir / DB_NAME}", paypal_mode="sandbox", **credentials,
        paypal_sandbox_merchant_email=config.merchant_email, paypal_sandbox_recipient_allowlist=config.buyer_email, buyer_mail_mode="fake", ai_mode="demo")
    provider = PayPalSandboxProvider(config.client_id, config.client_secret, merchant_email=config.merchant_email, transport=transport, max_attempts=1)
    return create_app(settings, provider)


@contextmanager
def session(workdir: Path, config: SmokeConfig, transport: httpx.BaseTransport):
    """An in-process app on the isolated database plus a logged-in seller client; the engine is always released (Windows keeps file locks)."""
    app = make_app(workdir, config, transport)
    try:
        yield app, Api(app)
    finally:
        app.state.engine.dispose()


class Api:
    """The seller's in-process API client against the isolated database."""

    def __init__(self, app) -> None:
        from fastapi.testclient import TestClient
        self.client = TestClient(app, base_url="http://localhost:8000", headers=ORIGIN)
        self.client.post("/api/v1/auth/register", json={"email": SELLER[0], "password": SELLER[1], "display_name": "Sandbox runner (synthetic)"})
        login = self.client.post("/api/v1/auth/login", json={"email": SELLER[0], "password": SELLER[1]})
        login.raise_for_status()
        self.csrf = login.json()["csrf_token"]

    def post(self, path: str, body: dict | None = None, headers: dict | None = None):
        return self.client.post(path, json=body, headers={"X-CSRF-Token": self.csrf, **(headers or {})})

    def get(self, path: str, **kw):
        return self.client.get(path, **kw)


def new_state(config: SmokeConfig) -> RunState:
    return RunState(run_id=secrets.token_hex(6), buyer_masked=mask_email(config.buyer_email), invoice_number="")


def precondition(stage: Stage, state: RunState | None) -> str | None:
    """Why this stage must not run now, or None."""
    if state is not None and state.ambiguous and stage is not Stage.READ:  # a read-only look is how an ambiguous outcome gets resolved
        return "an earlier mutation was ambiguous: only a separately approved read may follow; nothing is retried automatically"
    if stage is Stage.CREATE:
        return "a create was already attempted for this run" if state is not None and state.create_attempted else None
    if state is None or state.create_outcome != "OK" or not state.invoice_id or not state.payment_id:
        return "no successfully created invoice is recorded for this run"
    if stage is Stage.SEND:
        if not state.draft_verified:
            return "the created invoice was not verified as DRAFT"
        return "the send was already attempted for this run" if state.send_attempted else None
    return None


def _payment(api: Api, project_id: str, payment_id: str) -> dict | None:
    data = api.get(f"/api/v1/projects/{project_id}/payments").json()
    rows = data if isinstance(data, list) else data.get("payments", [])
    return next((r for r in rows if r["id"] == payment_id), None)


_UNPAID_WORDS = frozenset({"DRAFT", "SENT", "SCHEDULED", "UNPAID", "PAYMENT_PENDING"})


def _read_reconciled(payment: dict, status_word: str | None) -> bool:
    """The local record agrees with the provider's invoice state word and holds exactly the financial status reconciliation derives from it.

    A DRAFT is correctly UNVERIFIED (nothing owed or paid yet); every other state must have left UNVERIFIED; NEEDS_REVIEW is never reconciled."""
    try:
        expected = _RECONCILED_STATUS[ProviderInvoiceState(status_word)].value
    except (KeyError, ValueError):
        return False
    if payment.get("invoice_state") != status_word or payment.get("provider_status") != expected:
        return False
    paid, total = payment.get("amount_paid_minor"), payment.get("amount_minor")
    if not isinstance(paid, int) or not isinstance(total, int):
        return False
    if status_word in _UNPAID_WORDS:
        return paid == 0  # an unpaid state can never carry money
    if status_word == "PAID":
        return paid == total
    return 0 < paid < total if status_word == "PARTIALLY_PAID" else True


def _record_mutation_meta(state: RunState, transport: RunnerTransport) -> None:
    state.last_mutation_http = transport.mutation_status
    state.last_mutation_error, state.last_mutation_debug_id = transport.mutation_error_name, transport.mutation_debug_id


def _meta_text(transport: RunnerTransport) -> str:
    pairs = (("error", transport.mutation_error_name), ("debug_id", transport.mutation_debug_id))
    return "".join(f" {label}={value}" for label, value in pairs if value)


def _blocked_after_mutation(workdir: Path, state: RunState, transport: RunnerTransport, kind: str, exc: BlockedRequest, out: Out) -> int:
    """A guard refusal AFTER the one mutation left (e.g. the adapter retrying a 429): the mutation's own outcome is still recorded, never lost."""
    outcome = _classify_mutation(transport, state, kind)
    if kind == "create" and outcome == "OK":  # the create answered, but nothing was read back or verified: an invoice may exist unseen
        outcome, state.ambiguous = "AMBIGUOUS", True
    setattr(state, f"{kind}_outcome", outcome)
    _record_mutation_meta(state, transport)
    state.stages_done.append(kind)
    state.save(workdir)
    out.say(f"{kind}: outcome={outcome} http={transport.mutation_status}{_meta_text(transport)}; the automatic retry was blocked ({exc})")
    return EXIT_AMBIGUOUS if outcome == "AMBIGUOUS" else EXIT_PROVIDER_ERROR


def _classify_mutation(transport: RunnerTransport, state: RunState, kind: str) -> str:
    if transport.ambiguous_mutation or (transport.mutation_sent and transport.mutation_status is None):
        state.ambiguous = True
        return "AMBIGUOUS"
    status = transport.mutation_status
    if status is not None and 200 <= status < 300:
        return "OK"
    return "REFUSED"


def _unexpected(workdir: Path, state: RunState, transport: RunnerTransport, kind: str, exc: Exception, out: Out) -> int:
    """Record an unexpected failure. After a sent mutation it is AMBIGUOUS and blocks all later mutations; only the exception type is printed."""
    if transport.mutation_sent:
        state.ambiguous = True
        setattr(state, f"{kind}_outcome", "AMBIGUOUS")
        _record_mutation_meta(state, transport)
    state.save(workdir)
    out.say(f"{kind}: unexpected {type(exc).__name__}; mutation_sent={transport.mutation_sent}; outcome={'AMBIGUOUS' if transport.mutation_sent else 'not sent'}")
    return EXIT_AMBIGUOUS if transport.mutation_sent else EXIT_PROVIDER_ERROR


def run_create(workdir: Path, config: SmokeConfig, inner: httpx.BaseTransport, live: bool, out: Out) -> int:
    state = RunState.load(workdir) or new_state(config)
    state.save(workdir)

    def attempted(_kind: str) -> None:
        state.create_attempted = True  # persisted BEFORE the request leaves the process
        state.save(workdir)

    transport = RunnerTransport(inner, Stage.CREATE, buyer_email=config.buyer_email, live=live, before_mutation=attempted, merchant_email=config.merchant_email)
    try:
        with session(workdir, config, transport) as (_app, api):
            project = api.post("/api/v1/projects", {"title": "SYNTHETIC sandbox runner invoice"}).json()["id"]
            state.project_id = project
            api.post(f"/api/v1/projects/{project}/scope-records", {"kind": "ORIGINAL", "source_label": "Synthetic agreement", "content": "One synthetic 1.00 USD sandbox item."})
            response = api.post(f"/api/v1/projects/{project}/invoices", {"currency": RUN_CURRENCY, "amount_minor": RUN_AMOUNT_MINOR, "recipient_email": config.buyer_email},
                                {"Idempotency-Key": f"runner-{state.run_id}-create-0001"})
    except BlockedRequest as exc:
        if transport.mutation_sent:
            return _blocked_after_mutation(workdir, state, transport, "create", exc, out)
        state.save(workdir)
        out.say(f"BLOCKED before sending: {exc}")
        return EXIT_BLOCKED
    except Exception as exc:  # noqa: BLE001 - anything unexpected after a sent mutation is an unknown outcome, never a clean one
        return _unexpected(workdir, state, transport, "create", exc, out)
    state.create_outcome = _classify_mutation(transport, state, "create")
    _record_mutation_meta(state, transport)
    if state.create_outcome == "OK" and not (transport.created_invoice_id and response.status_code in (200, 201)):
        state.create_outcome, state.ambiguous = "AMBIGUOUS", True  # a "success" whose invoice id is unknown may be an orphan at PayPal
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if state.create_outcome == "OK" and response.status_code in (200, 201):
        state.invoice_id, state.payment_id = transport.created_invoice_id, body.get("id")
        state.invoice_number = invoice_number_for(state.payment_id) if state.payment_id else ""  # the number the app sent PayPal
        raw_ok = transport.reads_seen >= 1 and transport.response_status_word == "DRAFT"             and not set(transport.read_failures) - _CREATE_READ_EXEMPT  # the provider's own read-back must show OUR merchant, buyer, id and 1.00 USD
        state.draft_verified = bool(raw_ok and body.get("invoice_state") == "DRAFT" and body.get("amount_minor") == RUN_AMOUNT_MINOR and not body.get("amount_paid_minor"))
    state.stages_done.append(Stage.CREATE.value)
    state.save(workdir)
    error_code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
    out.say(f"create: outcome={state.create_outcome} http={transport.mutation_status}{_meta_text(transport)} requests={len(transport.statuses)} api_status={response.status_code} api_error={error_code}")
    out.check("one DRAFT invoice created and read back as DRAFT for 1.00 USD", state.create_outcome == "OK" and state.draft_verified)
    return EXIT_AMBIGUOUS if state.create_outcome == "AMBIGUOUS" else EXIT_OK if state.draft_verified else EXIT_PROVIDER_ERROR


def run_send(workdir: Path, config: SmokeConfig, inner: httpx.BaseTransport, live: bool, out: Out, state: RunState) -> int:
    def attempted(_kind: str) -> None:
        state.send_attempted = True
        state.save(workdir)

    transport = RunnerTransport(inner, Stage.SEND, buyer_email=config.buyer_email, invoice_id=state.invoice_id, live=live, before_mutation=attempted)
    try:
        with session(workdir, config, transport) as (_app, api):
            api.post(f"/api/v1/projects/{state.project_id}/payments/{state.payment_id}/send", {"confirm_recipient_email": config.buyer_email})
    except BlockedRequest as exc:
        if transport.mutation_sent:
            return _blocked_after_mutation(workdir, state, transport, "send", exc, out)
        out.say(f"BLOCKED before sending: {exc}")
        return EXIT_BLOCKED
    except Exception as exc:  # noqa: BLE001
        return _unexpected(workdir, state, transport, "send", exc, out)
    state.send_outcome = _classify_mutation(transport, state, "send")
    _record_mutation_meta(state, transport)
    state.stages_done.append(Stage.SEND.value)
    state.save(workdir)
    out.say(f"send: outcome={state.send_outcome} http={transport.mutation_status}{_meta_text(transport)}")
    out.check("the invoice was sent once", state.send_outcome == "OK")
    return EXIT_AMBIGUOUS if state.send_outcome == "AMBIGUOUS" else EXIT_OK if state.send_outcome == "OK" else EXIT_PROVIDER_ERROR


def run_read(workdir: Path, config: SmokeConfig, inner: httpx.BaseTransport, live: bool, out: Out, state: RunState) -> int:
    transport = RunnerTransport(inner, Stage.READ, buyer_email=config.buyer_email, invoice_id=state.invoice_id, live=live,
                                merchant_email=config.merchant_email, invoice_number=state.invoice_number or None)
    try:
        with session(workdir, config, transport) as (_app, api):
            response = api.post(f"/api/v1/projects/{state.project_id}/payments/sync")
            payment = _payment(api, state.project_id, state.payment_id)
    except BlockedRequest as exc:
        out.say(f"BLOCKED before sending: {exc}")
        return EXIT_BLOCKED
    if response.status_code >= 400 or payment is None or transport.response_status_word is None:
        out.say(f"read: UNREADABLE http={transport.statuses[-1:]} keys={list(transport.response_keys)} status_word={transport.response_status_word} "
                f"transactions={list(transport.transaction_types)}")
        out.check("the provider response was recognised", False)
        return EXIT_UNREADABLE
    state.last_status = transport.response_status_word
    promote = transport.authenticated_read and response.status_code < 400 and payment["id"] == state.payment_id \
        and payment["paypal_invoice_id"] == state.invoice_id and payment["provider_status"] not in ("NEEDS_REVIEW",)
    if promote and state.payment_id not in state.authenticated_payment_ids:
        state.authenticated_payment_ids.append(state.payment_id)
    if not promote:
        reasons = transport.read_failures or (("reconciliation_not_clean",) if transport.authenticated_read else ("no_read",))
        out.say(f"read: evidence NOT promoted to AUTHENTICATED_PAYPAL_SANDBOX; reasons={list(reasons)}")
    state.stages_done.append(Stage.READ.value)
    state.save(workdir)
    sandbox_attestation.clear()
    for pid in state.authenticated_payment_ids:
        sandbox_attestation.attest(pid)  # runner-only: an authenticated, allowlisted 200 read of this exact invoice
    out.say(f"read: invoice status word={state.last_status} provider_status={payment['provider_status']} transactions={list(transport.transaction_types)} "
            f"authenticated={promote}")
    reconciled = out.check("the evidence was recognised and reconciled", _read_reconciled(payment, state.last_status))
    return EXIT_OK if reconciled else EXIT_CHECK_FAILED


def run_recover(workdir: Path, config: SmokeConfig, out: Out, state: RunState) -> int:
    from app.simulation.runner_recovery import recovery_checks
    sandbox_attestation.clear()
    for pid in state.authenticated_payment_ids:
        sandbox_attestation.attest(pid)
    blocked = RunnerTransport(_NoNetwork(), Stage.READ, buyer_email=config.buyer_email, invoice_id=state.invoice_id)
    with session(workdir, config, blocked) as (app, api):
        payment = _payment(api, state.project_id, state.payment_id)
        if payment is None or payment["provider_status"] != "PAID":
            out.check("the recorded payment is PAID by provider evidence (read stage first)", False)
            return EXIT_REFUSED
        passed = recovery_checks(app, api, state.project_id, state.payment_id, out)
    state.stages_done.append("recover")
    state.save(workdir)
    return EXIT_OK if passed else EXIT_CHECK_FAILED


def dry_run(stage: Stage, state: RunState | None, config: SmokeConfig, out: Out, workdir: Path | None) -> int:
    reason = precondition(stage, state)
    out.say(f"DRY RUN (no network, nothing created): stage '{stage.value}' would make exactly these PayPal Sandbox requests:")
    for line in PLANNED_REQUESTS[stage]:
        out.say(f"  - {line}")
    name, value = CONFIRMATIONS[stage]
    out.say(f"A real run needs --execute, a work directory outside the repository, and {name}={value}.")
    out.say(f"Buyer (masked): {mask_email(config.buyer_email)}; merchant configured: yes; work directory: {workdir or 'not given'}")
    out.check("preconditions for this stage hold", reason is None)
    if reason:
        out.say(f"  would be REFUSED: {reason}")
    return EXIT_OK if reason is None else EXIT_REFUSED


def _load_state(workdir: Path | None) -> tuple[RunState | None, str | None]:
    """(state, problem). A state file that cannot be trusted, or a database without its state, refuses the stage."""
    if workdir is None or not workdir.exists():
        return None, None
    try:
        state = RunState.load(workdir)
    except StateCorrupt as exc:
        return None, str(exc)
    if state is None and (workdir / DB_NAME).exists():
        return None, "a run database exists without its run state: refusing to guess what was already done"
    return state, None


def _refuse(out: Out, reason: str) -> int:
    out.say(f"REFUSED: {reason}")
    return EXIT_REFUSED


def real_stage(stage: Stage, args, env: Mapping[str, str], config: SmokeConfig, out: Out, factory: Callable[[], httpx.BaseTransport]) -> int:
    workdir = Path(args.workdir) if args.workdir else None
    unsafe = refuse_unsafe_workdir(workdir, REPO_ROOT) if workdir else None
    state, state_problem = (None, None) if unsafe else _load_state(workdir)
    if not args.execute:
        return _refuse(out, unsafe or state_problem) if (unsafe or state_problem) else dry_run(stage, state, config, out, workdir)
    name, value = CONFIRMATIONS[stage]
    problem = (None if workdir else "--workdir is required for a real stage") or unsafe or state_problem \
        or (None if env.get(name) == value else f"missing or wrong confirmation variable {name}") or precondition(stage, state)
    if problem:
        return _refuse(out, problem)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        with stage_lock(workdir):
            state, state_problem = _load_state(workdir)  # re-read under the lock: another runner may have finished since the first look
            problem = state_problem or precondition(stage, state)
            if problem:
                return _refuse(out, problem)
            inner = factory()
            live = type(inner) is httpx.HTTPTransport  # only the genuine network transport counts; a subclass, mock or wrapper never does
            if stage is Stage.CREATE:
                return run_create(workdir, config, inner, live, out)
            return (run_send if stage is Stage.SEND else run_read)(workdir, config, inner, live, out, state)
    except StageBusy as exc:
        return _refuse(out, str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Controlled PayPal Sandbox validation runner (dry-run by default).")
    parser.add_argument("--stage", choices=["rehearse", "create", "send", "read", "recover"], default="rehearse")
    parser.add_argument("--execute", action="store_true", help="really perform the stage (needs its confirmation variable)")
    parser.add_argument("--workdir", help="run directory for the isolated database and run state (outside the repository)")
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         transport_factory: Callable[[], httpx.BaseTransport] = httpx.HTTPTransport) -> int:
    args, env = build_parser().parse_args(argv), os.environ if env is None else env
    stage_name = args.stage
    if stage_name == "rehearse":
        from app.sandbox_runner_rehearsal import rehearse
        return rehearse(Out(Redactor([]), [], []))
    try:
        config = load_config(env)
    except SmokeRefusal as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    out = Out(Redactor([config.client_id, config.client_secret]), [], [])
    if stage_name == "recover":
        workdir = Path(args.workdir) if args.workdir else None
        state, problem = (None, None) if workdir is None or refuse_unsafe_workdir(workdir, REPO_ROOT) else _load_state(workdir)
        if problem or state is None or state.last_status != "PAID":
            return _refuse(out, problem or "recover needs a safe --workdir whose run state recorded a PAID read")
        return run_recover(workdir, config, out, state)
    try:
        return real_stage(Stage(stage_name), args, env, config, out, transport_factory)
    except BlockedRequest as exc:
        out.say(f"BLOCKED before sending: {exc}")
        return EXIT_BLOCKED
    except Exception as exc:  # noqa: BLE001 - never a traceback (it could carry request details); the type name is enough
        out.say(f"stopped on unexpected {type(exc).__name__}")
        return EXIT_PROVIDER_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
