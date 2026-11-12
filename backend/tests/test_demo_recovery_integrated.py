"""S4: the integrated recovery demo (scenarios A-D through the API) passes every check offline in an isolated temporary database."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import demo_recovery_integrated as demo  # noqa: E402


def test_the_integrated_demo_passes_every_check_and_ignores_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'must_not_be_used.sqlite3'}")
    out = tmp_path / "out"
    out.mkdir()
    assert demo.run(out) == 0
    checks = json.loads((out / "checks.json").read_text(encoding="utf8"))
    assert checks["failed"] == 0 and checks["passed"] >= 35
    assert not (tmp_path / "must_not_be_used.sqlite3").exists()
    text = "".join(p.read_text(encoding="utf8") for p in out.rglob("*") if p.is_file())
    assert "secret_ciphertext" not in text and "otpauth" not in text


def test_the_integrated_demo_exits_nonzero_when_a_step_fails(tmp_path, monkeypatch):
    def boom(self, *args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(demo.Demo, "lifecycle", boom)
    assert demo.main(["--out", str(tmp_path / "out")]) == 1
