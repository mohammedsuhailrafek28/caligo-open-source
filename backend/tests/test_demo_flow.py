"""Slice 6: the offline demo must run end to end and tell the intended, honest story."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_evidence_flow.py"


@pytest.fixture(scope="module")
def demo_module():
    spec = importlib.util.spec_from_file_location("demo_evidence_flow", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_evidence_flow"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def result(demo_module, tmp_path_factory):
    return demo_module.run_demo(tmp_path_factory.mktemp("demo"))


def test_the_demo_runs_offline_and_produces_a_verifiable_report(result):
    report = result["report"]
    body = {k: v for k, v in report.items() if k != "report_sha256"}
    from app.services.scope import canonical_sha256
    assert report["report_sha256"] == canonical_sha256(body) and report["project"]["title"].startswith("SYNTHETIC DEMO")


def test_the_approved_change_is_authorized_and_the_unapproved_one_is_not(result):
    changes = {c["id"]: c for c in result["report"]["changes"]}
    assert changes[result["changes"]["approved"]]["authorization"] == "VERIFIED_BUYER_APPROVAL"
    assert changes[result["changes"]["unapproved"]]["authorization"] == "NONE"
    assert [b["version"] for b in result["report"]["agreement"]["baselines"]] == [1, 2]


def test_the_extra_feature_and_the_unverifiable_artifact_are_flagged(result):
    kinds = {u["type"] for u in result["report"]["unresolved"]}
    assert {"UNAPPROVED_WORK_DELIVERED", "CHANGE_AWAITING_BUYER", "ARTIFACT_NOT_HASHED_BY_SERVER", "DELIVERY_DISPUTED"} <= kinds
    findings = result["analysis"]["findings"]
    assert any(f["category"] == "SCOPE_MISMATCH" and "R5" in f["claim"] for f in findings)
    assert [f for f in findings if f["category"] == "DELIVERY_ACCEPTANCE"][0]["status"] == "UNSUPPORTED"


def test_payment_is_simulated_and_the_report_never_claims_acceptance_or_payment(result):
    payments = result["report"]["payments"]["records"]
    assert payments[0]["is_simulated"] is True and payments[0]["amount_paid_minor"] == 0
    assert result["report"]["buyer_evidence"]["delivery_status"] == "DISPUTED"
    assert "SIMULATED" in result["markdown"] and "NOT PRODUCTION" in result["markdown"].upper()


def test_the_cli_writes_the_report_files_and_leaves_no_other_state(demo_module, tmp_path, capsys):
    assert demo_module.main(["--out", str(tmp_path / "out")]) == 0
    assert json.loads((tmp_path / "out" / "evidence-report.json").read_text(encoding="utf8"))["report_sha256"]
    assert (tmp_path / "out" / "evidence-report.md").read_text(encoding="utf8").startswith("# Evidence report")
    assert "SYNTHETIC DATA" in capsys.readouterr().out
