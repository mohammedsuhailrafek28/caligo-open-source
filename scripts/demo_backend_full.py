"""R6 offline end-to-end backend demonstration. SYNTHETIC DATA ONLY: in-process app, temporary SQLite database, MOCK PayPal provider, fake outbox.

    backend/.venv/Scripts/python scripts/demo_backend_full.py [--out demo_output]        (Windows)
    backend/.venv/bin/python scripts/demo_backend_full.py [--out demo_output]            (POSIX)

It makes no network call, reads no .env file and no credential, and never touches the development database or `backend/.smoke`.
It exits non-zero unless every expected outcome holds (see checks.json in the output directory).

Story (fictional): a freelancer freezes a 3-requirement scope and delivers artifacts; the buyer acknowledges receipt. A payment instruction is
recorded, the invoice is paid (simulated) and the seller's merchant ledger disagrees with the provider. Then an urgent "our bank account
changed" instruction change arrives: it holds payment operations, and the operator-enrolled buyer-side authenticator REJECTS it. A later
legitimate change is APPROVED. Recovery cases and the unified evidence report are generated at the end (and once during the hold)."""
import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import demo_evidence_flow as base  # noqa: E402  (reuses its Demo client helper and synthetic constants)
from sqlalchemy.orm import Session  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import authenticators as auth  # noqa: E402
from app.services.payments import invoice_number_for  # noqa: E402
from app.services.scope import canonical_sha256  # noqa: E402

BUYER = base.BUYER_EMAIL
SMOKE_STATE = ROOT / "backend" / ".smoke" / "state.json"
LEDGER_HEADER = "kind,invoice_number,currency,amount_minor,transaction_id,asserted_at,note,merchant_row_id"
PROVIDER_TXN = "TXN-DEMO-PROVIDER-1"
SUSPICIOUS = {"recipient_email": "pay-here-now@example.net", "currency": "USD", "amount_minor": 120000,
              "reason": "URGENT: our bank details changed, please pay to this new address today."}
LEGITIMATE = {"recipient_email": BUYER, "currency": "USD", "amount_minor": 125000, "reason": "Agreed scope increase (blog), see approved change."}
REQUIREMENTS = base.ORIGINAL


class DemoSettings(Settings):
    """Settings that read ONLY the values passed in: not .env and not process environment variables (APP_ENV, COOKIE_SECURE, PAYPAL_*, ...)."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, *args, **kwargs):
        return (init_settings,)


SENTINEL = ".caligo_demo_output"


def refuse_unsafe_output(out: Path) -> str | None:
    """Return a reason to refuse, or None. Only an absent/empty directory, or one a previous demo run marked with SENTINEL, may be (re)used."""
    target = out.resolve()
    protected = [ROOT, ROOT / "backend", ROOT / "backend" / ".smoke", ROOT / "scripts", ROOT / "docs", ROOT / "contracts", Path.home()]
    for path in protected:
        resolved = path.resolve()
        if target == resolved or target in resolved.parents:
            return f"{target} is (or contains) a protected directory ({resolved})"
    if target.parent == target:
        return "refusing a filesystem root"
    if target.exists() and (not target.is_dir() or (any(target.iterdir()) and not (target / SENTINEL).exists())):
        return f"{target} exists and is not an empty demo output directory (no {SENTINEL} marker)"
    return None


class Recorder:
    """Writes every notable API response to <out>/api/NN_name.json and tracks the pass/fail checks."""

    def __init__(self, out: Path) -> None:
        self.api = out / "api"
        self.api.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self.checks: list[dict] = []

    def save(self, name: str, payload) -> None:
        self.count += 1
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True)
        (self.api / f"{self.count:02d}_{name}.{'md' if isinstance(payload, str) else 'json'}").write_text(text, encoding="utf8")

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "passed": bool(condition), "detail": detail})
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}{(' - ' + detail) if detail and not condition else ''}")


def sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class Scenario:
    def __init__(self, app, rec: Recorder) -> None:
        self.app, self.rec = app, rec
        self.demo = base.Demo(app)
        self.keyring = auth.parse_keyring(app.state.settings.authenticator_keys.get_secret_value(), app.state.settings.authenticator_active_key_id)
        self.used_steps: set[int] = set()
        self.secret = b""
        self.authenticator_id = ""

    def get(self, path: str):
        response = self.demo.seller.get(path)
        response.raise_for_status()
        return response

    def project_path(self, suffix: str = "") -> str:
        return f"/api/v1/projects/{self.project}{suffix}"

    # --- A/B: scope, artifacts, buyer acknowledgment ---------------------------------------------------------------------
    def scope_and_delivery(self) -> None:
        d = self.demo
        d.login()
        self.project = d.post("/api/v1/projects", {"title": "SYNTHETIC DEMO: Acme website build"})["id"]
        record = d.post(self.project_path("/scope-records"), {"kind": "ORIGINAL", "source_label": "Synthetic signed proposal",
                                                              "content": "Home page, contact form and pricing page."})
        baseline = d.post(self.project_path("/baselines"), {"source_scope_record_id": record["id"], "requirements": REQUIREMENTS})
        self.rec.save("baseline", baseline)
        artifacts = [d.post(self.project_path("/artifacts"), {"name": n, "requirement_keys": [k], "content_text": t})
                     for n, k, t in (("home.html", "R1", REQUIREMENTS[0]["description"]), ("contact.php", "R2", REQUIREMENTS[1]["description"]),
                                     ("pricing.html", "R3", REQUIREMENTS[2]["description"]))]
        self.rec.save("artifacts", artifacts)
        decision = d.buyer_decides({"kind": "DELIVERY_REVIEW", "artifact_ids": [a["id"] for a in artifacts]}, self.project,
                                   "ACKNOWLEDGE_DELIVERY", "Received the three files. Not yet reviewed in depth.")
        self.rec.save("buyer_delivery_acknowledgment", decision)
        self.rec.check("buyer acknowledgment is mailbox-verified, not identity", decision["decision"]["verification_method"] == "EMAIL_OTP")

    # --- C: instruction, invoice, simulated provider payment, conflicting ledger ----------------------------------------
    def payment_and_ledger(self) -> None:
        d = self.demo
        instruction = d.post(self.project_path("/payment-instruction"), {"recipient_email": BUYER, "currency": "USD", "amount_minor": 120000},
                             {"Idempotency-Key": "demo-instruction-0001"})
        self.rec.save("payment_instruction_v1", instruction)
        self.rec.check("first instruction is seller-entered and unverified", instruction["verification_status"] == "PENDING_VERIFICATION")
        invoice = d.post(self.project_path("/invoices"), {"currency": "USD", "amount_minor": 120000, "recipient_email": BUYER},
                         {"Idempotency-Key": "demo-invoice-0001"})
        d.post(self.project_path(f"/payments/{invoice['id']}/send"), {"confirm_recipient_email": BUYER})
        self.app.state.payment_provider.simulate_payment(invoice["paypal_invoice_id"], transaction_id=PROVIDER_TXN)  # fake provider, in memory
        synced = d.post(self.project_path("/payments/sync"))
        self.rec.save("payments_after_sync", synced)
        number = invoice_number_for(invoice["id"])
        rows = [f"PAYMENT,{number},USD,100000,{PROVIDER_TXN},,merchant says 1000.00,M-1",
                f"PAYMENT,{number},USD,5000,TXN-LEDGER-ONLY-1,,merchant says a second payment,M-2"]
        imported = d.post(self.project_path("/merchant-ledger/imports"), {"csv": "\n".join([LEDGER_HEADER, *rows]) + "\n"})
        self.rec.save("merchant_ledger_import", imported)
        self.payment_id = invoice["id"]
        comparison = self.get(self.project_path("/observation-comparison")).json()
        self.rec.save("observation_comparison", comparison)

    # --- D/E: suspicious change -> hold -> buyer-side authenticator --------------------------------------------------------
    def _code(self) -> str:
        now = auth.now()
        for offset in (0, 30, -30):  # the replay guard forbids reusing a time step, so pick one not yet used
            step = auth.step_at(now + offset)
            if step not in self.used_steps:
                self.used_steps.add(step)
                return auth.totp(self.secret, now + offset)
        raise RuntimeError("no unused TOTP step available; rerun the demo")

    def decide(self, change_id: str, decision: str) -> dict:
        buyer = self.demo.buyer
        challenge = buyer.post("/api/v1/buyer/instruction-changes/challenge", json={"project_id": self.project, "change_id": change_id, "buyer_email": BUYER})
        challenge.raise_for_status()
        ch = challenge.json()
        body = {"challenge_id": ch["challenge_id"], "project_id": self.project, "change_id": change_id, "buyer_email": BUYER,
                "from_fingerprint": ch["change"]["from_fingerprint"], "proposed_fingerprint": ch["change"]["proposed_fingerprint"],
                "decision": decision, "totp_code": self._code()}
        done = buyer.post("/api/v1/buyer/instruction-changes/decide", json=body)
        done.raise_for_status()
        return done.json()

    def hold_and_authorize(self) -> None:
        d = self.demo
        with Session(self.app.state.engine, expire_on_commit=False) as db:  # OPERATOR action on the server host; there is no HTTP route for it
            row, self.secret = auth.enroll(db, self.keyring, self.project, BUYER, "demo-operator")
            self.authenticator_id = row.id
        suspicious = d.post(self.project_path("/payment-instruction/changes"), SUSPICIOUS, {"Idempotency-Key": "demo-change-suspicious-1"})
        self.rec.save("suspicious_change_proposed", suspicious)
        hold = self.get(self.project_path("/payment-hold")).json()
        self.rec.save("payment_hold_active", hold)
        self.rec.check("suspicious change triggers a hold", hold["active"] is True and hold["reasons"] == ["UNVERIFIED_INSTRUCTION_CHANGE"])
        self.during_hold = self.get(self.project_path("/unified-report")).json()
        rejected = self.decide(suspicious["id"], "REJECT_INSTRUCTION_CHANGE")
        self.rec.save("buyer_side_rejection", rejected)
        versions = self.get(self.project_path("/payment-instruction/versions")).json()
        self.rec.check("rejection leaves the instruction unchanged", rejected["outcome"] == "REJECTED" and len(versions) == 1)
        legit = d.post(self.project_path("/payment-instruction/changes"), LEGITIMATE, {"Idempotency-Key": "demo-change-legitimate-1"})
        approved = self.decide(legit["id"], "APPROVE_INSTRUCTION_CHANGE")
        self.rec.save("buyer_side_approval", approved)
        self.rec.check("approval applies the proposed version and releases the hold", approved["outcome"] == "APPLIED" and approved["hold_active"] is False)
        self.rec.save("payment_instruction_versions", self.get(self.project_path("/payment-instruction/versions")).json())

    # --- G/H: recovery cases and unified reports ----------------------------------------------------------------------------
    def reports(self) -> None:
        cases = self.get(self.project_path("/recovery-cases")).json()
        self.rec.save("recovery_cases", cases)
        categories = {c["category"] for c in cases["cases"]}
        self.rec.check("recovery cases cover the ledger/provider conflict", {"AMOUNT_OR_CURRENCY_MISMATCH", "CLAIM_NOT_CORROBORATED"} <= categories,
                       f"got {sorted(categories)}")
        self.rec.check("every case is advisory, cited and lists prohibited actions", all(
            c["advisory_only"] and c["citations"] and c["citations_verified"] and c["prohibited_automated_actions"] and c["recommended_next_action"]
            for c in cases["cases"]))
        detail = self.get(self.project_path(f"/recovery-cases/{cases['cases'][0]['case_id']}")).json()
        self.rec.save("recovery_case_detail", detail)
        report = self.get(self.project_path("/unified-report")).json()
        markdown = self.get(self.project_path("/unified-report/markdown")).text
        self.report, self.markdown = report, markdown
        self.rec.save("unified_report_markdown", markdown)
        self.verify_reports(report, markdown)

    def verify_reports(self, report: dict, markdown: str) -> None:
        r = self.rec
        body = {k: v for k, v in report.items() if k != "report_sha256"}
        r.check("report hash is reproducible", canonical_sha256(body) == report["report_sha256"] and f"`{report['report_sha256']}`" in markdown)
        r.check("report is deterministic", self.get(self.project_path("/unified-report")).json() == report)
        r.check("every citation resolves to this project and payment", report["citation_check"]["all_resolved"] is True)
        during = self.during_hold
        r.check("report during the hold shows it pending with no authorization",
                during["payment_instructions"]["hold"]["active"] is True and during["payment_instructions"]["authorizations"] == [])
        instr = report["payment_instructions"]
        r.check("final report: hash chain intact and hold released", instr["hash_chain_intact"] and not instr["hold"]["active"])
        outcomes = sorted(a["outcome"] for a in instr["authorizations"])
        r.check("both authorization records present, consistent and never identity proof", outcomes == ["APPLIED", "REJECTED"] and all(
            a["record_consistent"] and a["real_world_identity_verified"] is False and a["independent_of_seller"] == "NOT_ESTABLISHED"
            for a in instr["authorizations"]))
        r.check("only the approved version is shown as verified",
                [v["verification_status"] for v in instr["versions"]] == ["PENDING_VERIFICATION", "VERIFIED_BY_ENROLLED_SECOND_CHANNEL"])
        pay = report["payments"]["payments"][0]
        r.check("provider status stays authoritative beside the conflicting claims", pay["authoritative_status"] == "PAID"
                and pay["claim_vs_provider"] == "CLAIMED_TOTAL_DIFFERS_FROM_PROVIDER" and pay["seller_claims"]["evidence_class"] == "SELLER_CLAIM")
        scope = report["scope_and_delivery"]
        r.check("scope/delivery shows the buyer acknowledgment without acceptance", scope["available"] and scope["buyer_evidence"]["delivery_status"] == "ACKNOWLEDGED_ONLY"
                and any(u["type"] == "DELIVERY_NOT_ACCEPTED" for u in scope["unresolved"]))
        r.check("markdown carries the trust limitation", "NOT prove the real-world identity" in markdown and "real-world identity verified: **False**" in markdown)
        r.check("no secrets or full authenticator data in the report", all(s not in json.dumps(report) for s in ("secret_ciphertext", "totp", "otpauth")))


def run(out: Path) -> int:
    smoke_before = sha256_file(SMOKE_STATE)
    rec = Recorder(out)
    with tempfile.TemporaryDirectory() as tmp:
        settings = DemoSettings(database_url=f"sqlite:///{Path(tmp) / 'demo.sqlite3'}", paypal_mode="mock", buyer_mail_mode="fake",
                            ai_mode="demo", authenticator_keys=f"k1:{auth.generate_key()}", authenticator_active_key_id="k1")
        app = create_app(settings)
        try:
            print("CALIGO R6 offline backend demo - SYNTHETIC DATA, MOCK PayPal, temporary database, no network")
            scenario = Scenario(app, rec)
            for step in (scenario.scope_and_delivery, scenario.payment_and_ledger, scenario.hold_and_authorize, scenario.reports):
                step()
        finally:
            app.state.engine.dispose()
    rec.check("no development or Sandbox state was touched", sha256_file(SMOKE_STATE) == smoke_before)
    (out / "unified-report.during-hold.json").write_text(json.dumps(scenario.during_hold, indent=2, sort_keys=True), encoding="utf8")
    (out / "unified-report.final.json").write_text(json.dumps(scenario.report, indent=2, sort_keys=True), encoding="utf8")
    (out / "unified-report.final.md").write_text(scenario.markdown, encoding="utf8")
    failed = [c for c in rec.checks if not c["passed"]]
    (out / "checks.json").write_text(json.dumps({"passed": len(rec.checks) - len(failed), "failed": len(failed), "checks": rec.checks}, indent=2), encoding="utf8")
    print(f"{len(rec.checks) - len(failed)}/{len(rec.checks)} checks passed; output in {out}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "demo_output", help="output directory (recreated on every run; default: demo_output)")
    args = parser.parse_args(argv)
    reason = refuse_unsafe_output(args.out)
    if reason:
        print(f"REFUSED: {reason}", file=sys.stderr)
        return 2
    if args.out.exists():
        shutil.rmtree(args.out)  # only reached for an empty directory or one this demo marked with SENTINEL
    args.out.mkdir(parents=True)
    (args.out / SENTINEL).write_text("created by scripts/demo_backend_full.py; safe to delete\n", encoding="utf8")
    try:
        return run(args.out)
    except Exception as exc:  # any failure is a failed demo, never a silent pass
        print(f"DEMO FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
