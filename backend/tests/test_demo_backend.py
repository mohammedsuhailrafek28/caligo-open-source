"""R6: the offline end-to-end backend demo runs in an isolated temporary database and fails loudly when an expectation breaks."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import demo_backend_full as demo  # noqa: E402


def test_the_demo_passes_every_check_and_writes_the_documented_outputs(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    assert demo.run(out) == 0
    checks = json.loads((out / "checks.json").read_text(encoding="utf8"))
    assert checks["failed"] == 0 and checks["passed"] >= 15
    for name in ("unified-report.final.json", "unified-report.final.md", "unified-report.during-hold.json"):
        assert (out / name).is_file()
    assert (out / "api").is_dir() and len(list((out / "api").iterdir())) >= 12
    final = json.loads((out / "unified-report.final.json").read_text(encoding="utf8"))
    assert final["kind"] == "UNIFIED_EVIDENCE_REPORT" and final["citation_check"]["all_resolved"] is True
    text = "".join(p.read_text(encoding="utf8") for p in out.rglob("*") if p.is_file())
    assert "secret_ciphertext" not in text and "otpauth" not in text


def test_the_demo_never_uses_the_development_database_or_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'must_not_be_used.sqlite3'}")
    out = tmp_path / "out"
    out.mkdir()
    assert demo.run(out) == 0
    assert not (tmp_path / "must_not_be_used.sqlite3").exists()


def test_the_demo_exits_nonzero_when_a_step_fails(tmp_path, monkeypatch):
    def boom(self):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(demo.Scenario, "reports", boom)
    assert demo.main(["--out", str(tmp_path / "out")]) == 1


def test_the_demo_exits_nonzero_when_an_expectation_is_false(tmp_path, monkeypatch):
    original = demo.Recorder.check

    def always_fail_one(self, name, condition, detail=""):
        original(self, name, False if name.startswith("report is deterministic") else condition, detail)

    monkeypatch.setattr(demo.Recorder, "check", always_fail_one)
    assert demo.main(["--out", str(tmp_path / "out")]) == 1


def test_the_demo_refuses_to_delete_protected_or_unmarked_directories(tmp_path, capsys):
    unmarked = tmp_path / "precious"
    unmarked.mkdir()
    (unmarked / "keep.txt").write_text("do not delete", encoding="utf8")
    for target in (demo.ROOT, demo.ROOT / "backend", demo.ROOT / "backend" / ".smoke", demo.ROOT.parent, Path.home(), unmarked, tmp_path / "keep.txt"):
        if target == tmp_path / "keep.txt":
            target = unmarked / "keep.txt"
        assert demo.main(["--out", str(target)]) == 2
    assert (unmarked / "keep.txt").read_text(encoding="utf8") == "do not delete" and (demo.ROOT / "backend").is_dir()
    assert "REFUSED" in capsys.readouterr().err


def test_the_demo_reuses_only_its_own_marked_output_directory(tmp_path):
    out = tmp_path / "out"
    assert demo.refuse_unsafe_output(out) is None  # absent
    out.mkdir()
    assert demo.refuse_unsafe_output(out) is None  # empty
    (out / "x.txt").write_text("x", encoding="utf8")
    assert demo.refuse_unsafe_output(out) is not None  # non-empty, unmarked
    (out / demo.SENTINEL).write_text("", encoding="utf8")
    assert demo.refuse_unsafe_output(out) is None  # marked by a previous run


def test_the_demo_ignores_environment_variables_that_change_settings(tmp_path, monkeypatch):
    for name, value in (("APP_ENV", "production"), ("COOKIE_SECURE", "true"), ("PAYPAL_MODE", "sandbox"), ("PAYPAL_CLIENT_ID", "should-not-load"),
                        ("AI_MODE", "disabled"), ("PAYMENT_INSTRUCTION_REQUIRED", "true")):
        monkeypatch.setenv(name, value)
    settings = demo.DemoSettings(database_url=f"sqlite:///{tmp_path / 'x.sqlite3'}", paypal_mode="mock")
    assert settings.app_env == "development" and settings.cookie_secure is False and settings.paypal_mode == "mock"
    assert settings.paypal_client_id == "" or not settings.paypal_client_id
    out = tmp_path / "out"
    assert demo.main(["--out", str(out)]) == 0 and (out / demo.SENTINEL).is_file()
