"""S4 integrated recovery demo: scenarios A-D through the real API (divergence -> cited diagnosis -> plan -> preview -> approval -> guarded
execution -> audit trail). SYNTHETIC DATA ONLY: in-process app, temporary SQLite database, MOCK PayPal provider; no network, no credentials,
no development database, no `backend/.smoke`. Merchant state is SIMULATED; diagnosis runs in RULES_ONLY mode (no real LLM is configured, so
real-LLM evaluation is NOT RUN).

    backend/.venv/Scripts/python scripts/demo_recovery_integrated.py [--out demo_recovery_output]

Exits non-zero unless every expected outcome holds (see checks.json in the output directory)."""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import demo_evidence_flow as base  # noqa: E402
from demo_backend_full import SENTINEL, SMOKE_STATE, DemoSettings, Recorder, refuse_unsafe_output, sha256_file  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.main import create_app  # noqa: E402
from app.models import PaymentRecord  # noqa: E402
from app.sim_models import SimFulfillment, SimOrder  # noqa: E402
from app.simulation import scenarios, store  # noqa: E402
from app.simulation.processor import process_event  # noqa: E402
from app.sim_enums import EventSource, EventType  # noqa: E402

BUYER = base.BUYER_EMAIL
STOCK = 10


class Demo:
    def __init__(self, app, rec: Recorder) -> None:
        self.app, self.rec = app, rec
        self.d = base.Demo(app)
        self.d.login()
        self.n = 0

    def api(self, method: str, path: str, **kw):
        headers = {"X-CSRF-Token": self.d.csrf} if method == "post" else {}
        return getattr(self.d.seller, method)(path, headers=headers, **kw)

    def new_paid_project(self, label: str, *, refund: bool = False):
        self.n += 1
        pid = self.d.post("/api/v1/projects", {"title": f"SYNTHETIC S4 {label}"})["id"]
        invoice = self.d.post(f"/api/v1/projects/{pid}/invoices", {"currency": "USD", "amount_minor": 80000, "recipient_email": BUYER},
                              {"Idempotency-Key": f"s4-invoice-{self.n:04d}-padding"})
        self.d.post(f"/api/v1/projects/{pid}/payments/{invoice['id']}/send", {"confirm_recipient_email": BUYER})
        provider = self.app.state.payment_provider
        provider.simulate_payment(invoice["paypal_invoice_id"], transaction_id=f"TXN-S4-{self.n}")
        self.d.post(f"/api/v1/projects/{pid}/payments/sync")
        if refund:
            provider.simulate_refund(invoice["paypal_invoice_id"])
            self.d.post(f"/api/v1/projects/{pid}/payments/sync")
        return pid, invoice["id"]

    def lifecycle(self, name: str, pid: str, kind: str, payment_id: str | None, expect: str, steps: int) -> dict:
        root, r = f"/api/v1/projects/{pid}/state-recovery", self.rec
        divergences = self.api("get", f"{root}/divergences").json()
        r.save(f"{name}_1_divergences", divergences)
        r.check(f"{name}: a divergence is detected", bool(divergences["divergences"]) or kind == "FALSE_HEALTHY")
        params = {"kind": kind, **({"payment_id": payment_id} if payment_id else {})}
        diag = self.api("get", f"{root}/diagnosis", params=params).json()
        r.save(f"{name}_2_diagnosis", diag)
        ids = {e["evidence_id"] for e in diag["evidence"]}
        d = diag["diagnosis"]
        cited = set(d["supporting_evidence_ids"]) | set(d["contradictory_evidence_ids"]) | {f["evidence_id"] for f in d["facts"]}
        r.check(f"{name}: diagnosis is {expect} and advisory", d["classification"] == expect and diag["advisory_only"] and d["human_review_required"])
        r.check(f"{name}: every cited id resolves to labelled evidence", cited <= ids and all(e["provenance_label"] != "UNLABELLED" for e in diag["evidence"]))
        r.check(f"{name}: RULES_ONLY, real-LLM evaluation NOT RUN", diag["mode"] == "RULES_ONLY" and diag["llm_evaluation"] == "NOT_RUN")
        plan = self.api("post", f"{root}/plans", json={"kind": kind, "payment_id": payment_id}).json()
        r.save(f"{name}_3_plan", plan)
        preview = self.api("get", f"{root}/plans/{plan['id']}/preview").json()
        r.save(f"{name}_4_preview", preview)
        r.check(f"{name}: preview writes nothing and calls no provider", preview["guarantees"] == {"provider_calls": 0, "financial_mutations": 0, "dispatched_shipments_reversed": 0})
        if kind == "FALSE_HEALTHY":
            r.check(f"{name}: advisory plan has nothing to approve or execute", plan["status"] == "ADVISORY" and
                    self.api("post", f"{root}/plans/{plan['id']}/execute").status_code == 409)
            return plan
        r.check(f"{name}: execution before approval is refused", self.api("post", f"{root}/plans/{plan['id']}/execute").status_code == 409)
        bad = self.api("post", f"{root}/plans/{plan['id']}/approve", json={"version": plan["version"], "evidence_fingerprint": "0" * 64})
        r.check(f"{name}: an approval for the wrong evidence is refused", bad.status_code == 409)
        ok = self.api("post", f"{root}/plans/{plan['id']}/approve", json={"version": plan["version"], "evidence_fingerprint": plan["evidence_fingerprint"]})
        r.save(f"{name}_5_approved", ok.json())
        done = self.api("post", f"{root}/plans/{plan['id']}/execute")
        r.save(f"{name}_6_executed", done.json())
        again = self.api("post", f"{root}/plans/{plan['id']}/execute").json()
        r.check(f"{name}: executed once; a repeat changes nothing", done.json()["executed_now"] is True and again["executed_now"] is False)
        events = self.api("get", f"{root}/plans/{plan['id']}/events").json()["events"]
        r.save(f"{name}_7_audit_trail", events)
        kinds = [e["event_type"] for e in events]
        r.check(f"{name}: audit trail holds creation, one approval and one execution", kinds.count("APPROVED") == 1 and kinds.count("EXECUTED") == 1 and "PLAN_CREATED" in kinds)
        return plan


def run(out: Path) -> int:
    smoke_before = sha256_file(SMOKE_STATE)
    rec = Recorder(out)
    with tempfile.TemporaryDirectory() as tmp:
        settings = DemoSettings(database_url=f"sqlite:///{Path(tmp) / 'demo.sqlite3'}", paypal_mode="mock", buyer_mail_mode="fake", ai_mode="demo")
        app = create_app(settings)
        try:
            print("CALIGO S4 integrated recovery demo - SYNTHETIC DATA, MOCK PayPal, simulated merchant store, no network")
            demo = Demo(app, rec)
            engine = app.state.engine

            pid_a, pay_a = demo.new_paid_project("A one payment, three orders")
            with Session(engine, expire_on_commit=False) as db:
                item = store.add_inventory_item(db, pid_a, "SKU-S4", STOCK)
                db.commit()
                outcome = scenarios.scenario_a_one_payment_three_orders(db, pid_a, db.get(PaymentRecord, pay_a), item, dispatched=1)
                shipped = outcome.facts["dispatched_order_ids"]
            demo.lifecycle("A", pid_a, "DUPLICATE_ORDERS", pay_a, "duplicate_payment_orders", 3)
            with Session(engine) as db:
                live = [o for o in db.scalars(select(SimOrder).where(SimOrder.project_id == pid_a)) if o.state != "CANCELLED"]
                kept = db.scalars(select(SimFulfillment).where(SimFulfillment.project_id == pid_a, SimFulfillment.state == "DISPATCHED")).all()
                rec.check("A: one live order remains and the dispatched parcel is untouched", [o.id for o in live] == shipped and len(kept) == 1)
                rec.check("A: stock reflects exactly one order", store.stock_balance(db, item.id) == STOCK - 1)

            pid_b, pay_b = demo.new_paid_project("B provider paid, merchant failed")
            with Session(engine, expire_on_commit=False) as db:
                item_b = store.add_inventory_item(db, pid_b, "SKU-S4", STOCK)
                db.commit()
                scenarios.scenario_b_payment_success_merchant_failed(db, pid_b, db.get(PaymentRecord, pay_b), item_b)
            demo.lifecycle("B", pid_b, "PAID_NOT_APPLIED", pay_b, "capture_order_divergence", 1)
            with Session(engine) as db:
                rec.check("B: order applied once, stock deducted once", store.stock_balance(db, item_b.id) == STOCK - 1)

            pid_c, pay_c = demo.new_paid_project("C refund then stale paid", refund=False)
            with Session(engine, expire_on_commit=False) as db:
                item_c = store.add_inventory_item(db, pid_c, "SKU-S4", STOCK)
                db.commit()
                record = db.get(PaymentRecord, pay_c)
                event = store.receive_event(db, pid_c, source=EventSource.SIMULATED_PROVIDER_WEBHOOK, event_key="evt-c-paid", event_type=EventType.PAYMENT_COMPLETED,
                                            provider_txn_ref=record.paypal_transaction_id, amount_minor=record.amount_minor, currency=record.currency,
                                            asserted_at=None, payload={"sku": "SKU-S4", "quantity": 1})
                db.commit()
                process_event(db, pid_c, event.id)
            invoice_row = demo.api("get", f"/api/v1/projects/{pid_c}/payments").json()
            paypal_invoice = invoice_row[0]["paypal_invoice_id"] if isinstance(invoice_row, list) else invoice_row["payments"][0]["paypal_invoice_id"]
            app.state.payment_provider.simulate_refund(paypal_invoice)
            demo.d.post(f"/api/v1/projects/{pid_c}/payments/sync")
            demo.lifecycle("C", pid_c, "STALE_PAID_AFTER_REFUND", pay_c, "stale_paid_after_refund", 2)
            with Session(engine) as db:
                rec.check("C: the provider refund is untouched and the order now agrees with it",
                          db.get(PaymentRecord, pay_c).provider_status == "REFUNDED" and db.scalar(select(SimOrder.state).where(SimOrder.project_id == pid_c)) == "REFUNDED")

            pid_d = demo.d.post("/api/v1/projects", {"title": "SYNTHETIC S4 D false healthy"})["id"]
            demo.lifecycle("D", pid_d, "FALSE_HEALTHY", None, "unproven_integration_health", 0)
            health = demo.api("get", f"/api/v1/projects/{pid_d}/state-recovery/integration-health").json()
            rec.save("D_8_integration_health", health)
            rec.check("D: configuration and heartbeat do not verify a transaction", health["assessment"]["status"] != "TRANSACTION_VERIFIED"
                      and health["assessment"]["production_certification"] is False)

            other = base.Demo(app)
            other.seller.post("/api/v1/auth/register", json={"email": "other-seller@example.com", "password": "correct-horse-battery-9", "display_name": "Other"})
            login = other.seller.post("/api/v1/auth/login", json={"email": "other-seller@example.com", "password": "correct-horse-battery-9"})
            rec.check("cross-project: another seller gets 404 for every recovery read",
                      login.status_code == 200 and all(other.seller.get(f"/api/v1/projects/{pid_a}/state-recovery/{p}", params=q).status_code == 404
                                                      for p, q in (("divergences", {}), ("plans", {}), ("integration-health", {}), ("diagnosis", {"kind": "FALSE_HEALTHY"}))))
        finally:
            app.state.engine.dispose()
    rec.check("no development or Sandbox state was touched", sha256_file(SMOKE_STATE) == smoke_before)
    failed = [c for c in rec.checks if not c["passed"]]
    (out / "checks.json").write_text(json.dumps({"passed": len(rec.checks) - len(failed), "failed": len(failed), "checks": rec.checks}, indent=2), encoding="utf8")
    print(f"{len(rec.checks) - len(failed)}/{len(rec.checks)} checks passed; output in {out}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "demo_recovery_output", help="output directory (recreated on every run)")
    args = parser.parse_args(argv)
    reason = refuse_unsafe_output(args.out)
    if reason:
        print(f"REFUSED: {reason}", file=sys.stderr)
        return 2
    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)
    (args.out / SENTINEL).write_text("created by scripts/demo_recovery_integrated.py; safe to delete\n", encoding="utf8")
    try:
        return run(args.out)
    except Exception as exc:  # any failure is a failed demo, never a silent pass
        print(f"DEMO FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
