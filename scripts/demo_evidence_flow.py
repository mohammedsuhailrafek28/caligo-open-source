"""Offline end-to-end demonstration of the Caligo evidence workflow. SYNTHETIC DATA ONLY.

Runs entirely in-process: a temporary SQLite database, the MOCK PayPal provider, a fake outbox instead of e-mail and the
deterministic rule-based demo analysis. It makes no network calls, reads no credentials and touches no PayPal state.

    python scripts/demo_evidence_flow.py [--out DIR]

Story (all names fictional): a freelancer agrees a 3-page website, the buyer approves one extra feature (a blog) through a verified
mailbox, the seller also builds a loyalty-points feature that the buyer never approved, and the buyer disputes the delivery."""
import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

SELLER_EMAIL = "demo-seller@example.com"
SELLER_PASSWORD = "synthetic-placeholder-pw-1"  # synthetic, used only against the temporary database
BUYER_EMAIL = "demo-buyer@example.org"
ORIGIN = {"Origin": "http://localhost:3000"}
TOKEN = re.compile(r"token=([A-Za-z0-9_-]{20,})")
CODE = re.compile(r"code: (\d{6})")

ORIGINAL = [
    {"key": "R1", "title": "Home page", "description": "Responsive home page with hero section and navigation menu."},
    {"key": "R2", "title": "Contact form", "description": "Contact form that sends email to the site owner."},
    {"key": "R3", "title": "Pricing page", "description": "Pricing page listing three subscription plans."},
]
BLOG = [{"key": "R4", "title": "Blog", "description": "Blog with five posts and an RSS feed."}]
LOYALTY = [{"key": "R5", "title": "Loyalty points", "description": "Loyalty points program with a rewards dashboard."}]


class Demo:
    def __init__(self, app) -> None:
        self.app = app
        self.seller = TestClient(app, base_url="http://localhost:8000", headers=ORIGIN)
        self.buyer = TestClient(app, base_url="http://localhost:8000", headers=ORIGIN)
        self.csrf = ""
        self.steps: list[str] = []

    def say(self, text: str) -> None:
        self.steps.append(text)
        print(f"  - {text}")

    def post(self, path: str, body: dict | None = None, headers: dict | None = None):
        response = self.seller.post(path, json=body, headers={"X-CSRF-Token": self.csrf, **(headers or {})})
        if response.status_code >= 400:
            raise RuntimeError(f"{path} -> {response.status_code} {response.json().get('error', {}).get('code')}")
        return response.json()

    def login(self) -> None:
        self.seller.post("/api/v1/auth/register", json={"email": SELLER_EMAIL, "password": SELLER_PASSWORD,
                                                        "display_name": "Demo Seller (synthetic)"}).raise_for_status()
        login = self.seller.post("/api/v1/auth/login", json={"email": SELLER_EMAIL, "password": SELLER_PASSWORD})
        login.raise_for_status()
        self.csrf = login.json()["csrf_token"]

    def buyer_decides(self, request_kind: dict, project: str, decision: str, statement: str) -> dict:
        outbox = self.app.state.mailer.outbox
        self.post(f"/api/v1/projects/{project}/buyer-requests", {**request_kind, "buyer_email": BUYER_EMAIL})
        token = TOKEN.search(outbox[-1].body).group(1)
        self.buyer.post("/api/v1/buyer/challenge", json={"token": token}).raise_for_status()
        code = CODE.search(outbox[-1].body).group(1)
        session = self.buyer.post("/api/v1/buyer/verify", json={"token": token, "code": code})
        session.raise_for_status()
        done = self.buyer.post("/api/v1/buyer/decide", json={
            "session_token": session.json()["session_token"], "decision": decision, "statement": statement})
        done.raise_for_status()
        return done.json()


def run_demo(workdir: Path) -> dict:
    app = create_app(Settings(database_url=f"sqlite:///{workdir / 'demo.sqlite3'}", paypal_mode="mock",
                              buyer_mail_mode="fake", ai_mode="demo"))
    try:
        return _scenario(app)
    finally:
        app.state.engine.dispose()  # release the SQLite file so the temporary directory can be removed on Windows


def _scenario(app) -> dict:
    demo = Demo(app)
    print("CALIGO offline evidence demo - SYNTHETIC DATA, MOCK PayPal, no network")
    demo.login()
    project = demo.post("/api/v1/projects", {"title": "SYNTHETIC DEMO: Acme website build"})["id"]
    base = f"/api/v1/projects/{project}"
    record = demo.post(f"{base}/scope-records", {"kind": "ORIGINAL", "source_label": "Synthetic signed proposal",
                                                 "content": "Home page, contact form and pricing page."})
    demo.say(f"Original scope recorded (sha256 {record['source_sha256'][:12]}...)")
    baseline = demo.post(f"{base}/baselines", {"source_scope_record_id": record["id"], "requirements": ORIGINAL})
    demo.say(f"Baseline v{baseline['version']} frozen with {len(baseline['requirements'])} requirements; hash {baseline['content_sha256'][:12]}...")

    blog = demo.post(f"{base}/scope-changes", {"title": "Add a blog", "description": "Buyer asked for a blog.", "requirements": BLOG})
    loyalty = demo.post(f"{base}/scope-changes", {"title": "Add loyalty points", "description": "Seller idea; never discussed.",
                                                  "requirements": LOYALTY})
    demo.say("Two changes proposed; both stay PROPOSED until verified buyer evidence exists")
    outcome = demo.buyer_decides({"kind": "SCOPE_CHANGE_APPROVAL", "change_id": blog["id"]}, project, "APPROVE_CHANGE",
                                 "Yes, please add the blog.")
    demo.say(f"Buyer (verified mailbox) approved the blog -> agreement amended: {outcome['scope_applied']}")
    demo.say("Loyalty points was never sent to the buyer, so it remains unapproved")

    artifacts = [
        ("home.html", "Responsive home page with hero section and navigation menu.", ["R1"], {}),
        ("contact.php", "Contact form that sends email to the site owner.", ["R2"], {}),
        ("blog.zip", "Blog with five posts and an RSS feed.", ["R4"], {}),
        ("loyalty.zip", "Loyalty points program with a rewards dashboard.", ["R5"], {}),
        ("pricing.png", None, ["R3"], {"declared_sha256": "c" * 64}),
    ]
    registered = []
    for name, text, keys, extra in artifacts:
        body = {"name": name, "requirement_keys": keys, **extra}
        if text:
            body["content_text"] = text
        registered.append(demo.post(f"{base}/artifacts", body))
    demo.say(f"{len(registered)} artifacts registered; the server hashed 4 and only recorded a declared hash for pricing.png")

    invoice = demo.post(f"{base}/invoices", {"currency": "USD", "amount_minor": 120000, "recipient_email": BUYER_EMAIL},
                        {"Idempotency-Key": "demo-invoice-0001"})
    demo.say(f"MOCK invoice drafted ({invoice['provider']}); no money moved, PayPal not contacted")

    demo.buyer_decides({"kind": "DELIVERY_REVIEW", "artifact_ids": [a["id"] for a in registered]}, project, "DISPUTE_DELIVERY",
                       "I never agreed to the loyalty points feature and the pricing page is missing.")
    demo.say("Buyer disputed the delivery (extra feature + unverifiable pricing page)")

    analysis = demo.post(f"{base}/analysis")
    demo.say(f"Analysis ({analysis['provider_name']}, NOT PRODUCTION): {len(analysis['findings'])} findings, all cite stored records")
    report = demo.seller.get(f"{base}/evidence-report").json()
    markdown = demo.seller.get(f"{base}/evidence-report/markdown").text
    demo.say(f"Evidence report generated; report_sha256 {report['report_sha256'][:12]}...")
    return {"project_id": project, "report": report, "markdown": markdown, "analysis": analysis, "steps": demo.steps,
            "changes": {"approved": blog["id"], "unapproved": loyalty["id"]}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None, help="directory for evidence-report.json / .md (default: not written)")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        result = run_demo(Path(tmp))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "evidence-report.json").write_text(json.dumps(result["report"], indent=2, sort_keys=True), encoding="utf8")
        (args.out / "evidence-report.md").write_text(result["markdown"], encoding="utf8")
        print(f"Wrote {args.out / 'evidence-report.json'} and evidence-report.md")
    unresolved = ", ".join(sorted({u["type"] for u in result["report"]["unresolved"]}))
    print(f"Unresolved in the report: {unresolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
