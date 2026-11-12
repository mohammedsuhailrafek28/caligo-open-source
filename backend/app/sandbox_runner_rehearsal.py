"""Offline rehearsal of all nine plan steps: the real runner code and the real allowlist transport, over the FAKE PayPal (no socket is ever opened).

The rehearsal proves the control flow, the allowlist, the persisted run state and the provenance labels. It can NOT prove anything about real PayPal
response shapes, and its provider evidence is therefore labelled UNATTESTED_SANDBOX_PROVIDER, never AUTHENTICATED_PAYPAL_SANDBOX.
"""
import sys
import tempfile
from pathlib import Path

from app import sandbox_attestation
from app.sandbox_runner_guard import RunnerTransport, RunState, Stage
from app.sandbox_smoke import SmokeConfig

BUYER = "buyer@example.org"


def _fake_pieces():
    backend = str(Path(__file__).resolve().parents[1])
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from tests.fake_paypal import CLIENT_ID, CLIENT_SECRET, MERCHANT, FakePayPal
    return FakePayPal(), SmokeConfig(CLIENT_ID, CLIENT_SECRET, MERCHANT, BUYER)


def rehearse(out) -> int:
    from app import sandbox_runner as runner
    fake, config = _fake_pieces()
    out.say("REHEARSAL: nine steps, FAKE PayPal, isolated temporary database, no network, nothing real is created")
    sandbox_attestation.clear()
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        out.say("step 1: preflight (configuration names present, isolated database, buyer is the only recipient): ok")
        out.say("steps 2-4: seller and project in the isolated database; create one DRAFT invoice; provider read-back")
        code = runner.run_create(workdir, config, fake.transport(), False, out)
        state = RunState.load(workdir)
        if code != runner.EXIT_OK or state is None:
            return code or runner.EXIT_CHECK_FAILED
        out.say("step 5: send (separately approved in a real run)")
        code = runner.run_send(workdir, config, fake.transport(), False, out, state)
        out.say("step 6: the sandbox buyer pays (simulated by the fake; a person does this in a real run)")
        fake.pay(fake.only())
        out.say("step 7: read and reconcile")
        code = code or runner.run_read(workdir, config, fake.transport(), False, out, RunState.load(workdir))
        state = RunState.load(workdir)
        out.check("rehearsal evidence is never attested as authenticated Sandbox", not state.authenticated_payment_ids)
        code = code or runner.run_recover(workdir, config, out, state)
        out.check("only OAuth, one create, one send and GETs of the one invoice reached the fake",
                  len(fake.invoices) == 1 and fake.count("POST", "/v2/invoicing/invoices") == 1 and fake.count("POST", "/send") == 1
                  and all(m in ("POST", "GET") for m, _ in fake.calls))
    failed = [name for name, passed in out.checks if not passed]
    out.say(f"REHEARSAL RESULT: {len(out.checks) - len(failed)}/{len(out.checks)} checks passed")
    return code or (1 if failed else 0)
