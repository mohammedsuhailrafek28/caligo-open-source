"""Seeded local demo server. SYNTHETIC DATA ONLY: MOCK PayPal provider, fake outbox file, simulated merchant store, no network, no credentials.

Starts the real Caligo API with a fresh SQLite file, seeds one synthetic seller and two projects, then serves it so the frontend can be driven
through every flow in a browser:

  * "Paid, merchant failed": the mock provider reports a PAID invoice while the SIMULATED merchant store failed to apply the order. This is
    the divergence that Diagnosis, Log intelligence and Recovery operate on. The recovery plan is NOT created; the demo does that in the UI.
  * "Delivery review": a frozen scope baseline (HOME, BLOG), an artifact linked to HOME and a pending buyer request. The buyer's emailed link and one-time code are written to the outbox file.

    python scripts/demo_seeded_server.py --port 8011 --origin http://localhost:3001 --state-dir <empty dir outside the repo>

Seller login (synthetic): demo-seller@example.com / synthetic-placeholder-pw-1. Nothing here talks to PayPal."""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import demo_evidence_flow as base  # noqa: E402
from demo_backend_full import DemoSettings  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.main import create_app  # noqa: E402
from app.models import PaymentRecord  # noqa: E402
from app.simulation import scenarios, store  # noqa: E402

TOKEN = re.compile(r"token=([A-Za-z0-9_-]{20,})")


def seed(app, out: Path) -> dict:
    demo = base.Demo(app)
    demo.login()
    buyer = base.BUYER_EMAIL
    pid = demo.post("/api/v1/projects", {"title": "SYNTHETIC paid, merchant failed"})["id"]
    invoice = demo.post(f"/api/v1/projects/{pid}/invoices", {"currency": "USD", "amount_minor": 80000, "recipient_email": buyer},
                        {"Idempotency-Key": "seed-invoice-0001-padding"})
    demo.post(f"/api/v1/projects/{pid}/payments/{invoice['id']}/send", {"confirm_recipient_email": buyer})
    app.state.payment_provider.simulate_payment(invoice["paypal_invoice_id"], transaction_id="TXN-SEED-1")
    demo.post(f"/api/v1/projects/{pid}/payments/sync")
    with Session(app.state.engine, expire_on_commit=False) as db:
        item = store.add_inventory_item(db, pid, "SKU-DEMO", 10)
        db.commit()
        scenarios.scenario_b_payment_success_merchant_failed(db, pid, db.get(PaymentRecord, invoice["id"]), item)

    pid2 = demo.post("/api/v1/projects", {"title": "SYNTHETIC delivery review"})["id"]
    rec = demo.post(f"/api/v1/projects/{pid2}/scope-records", {"kind": "ORIGINAL", "source_label": "Statement of work v1",
                                                                "content": "Build a marketing homepage and a blog with five posts."})
    demo.post(f"/api/v1/projects/{pid2}/baselines", {"source_scope_record_id": rec["id"], "requirements": [
        {"key": "HOME", "title": "Marketing homepage", "description": "Responsive homepage with navigation."},
        {"key": "BLOG", "title": "Blog", "description": "Blog with five posts."}]})
    art = demo.post(f"/api/v1/projects/{pid2}/artifacts", {"name": "handover.txt", "description": "Handover notes", "media_type": "text/plain",
                                                            "content_text": "Synthetic handover notes.", "requirement_keys": ["HOME"]})
    demo.post(f"/api/v1/projects/{pid2}/buyer-requests", {"kind": "DELIVERY_REVIEW", "buyer_email": buyer, "artifact_ids": [art["id"]], "change_id": None})
    token = TOKEN.search(app.state.mailer.outbox[-1].body).group(1)
    summary = {"seller_email": base.SELLER_EMAIL, "buyer_email": buyer, "payment_project_id": pid, "payment_id": invoice["id"],
               "delivery_project_id": pid2, "buyer_token": token}
    (out / "seed.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--origin", default="http://localhost:3001", help="frontend origin (CORS/CSRF allow-list and buyer link base)")
    ap.add_argument("--state-dir", required=True, help="EMPTY directory outside the repo for the SQLite file, outbox and seed.json")
    ap.add_argument("--stanza-model-dir", default="", help="optional local Stanza cache; weights are never downloaded by this script")
    a = ap.parse_args()
    out = Path(a.state_dir).resolve()
    if ROOT in (out, *out.parents):
        sys.exit("refusing: --state-dir must be outside the repository")
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        sys.exit("refusing: --state-dir must be empty (use a fresh directory)")
    settings = DemoSettings(database_url=f"sqlite:///{out / 'demo.sqlite3'}", paypal_mode="mock", buyer_mail_mode="fake", ai_mode="demo",
                            dev_outbox_file=str(out / "outbox.jsonl"), cors_origins=a.origin, buyer_portal_base_url=a.origin,
                            stanza_model_dir=a.stanza_model_dir)
    base.ORIGIN["Origin"] = a.origin  # the seeding client must pass the same Origin check as the browser
    app = create_app(settings)
    print(json.dumps(seed(app, out), indent=2))
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
