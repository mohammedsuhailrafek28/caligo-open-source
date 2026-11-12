"""Tests for scripts/check_secrets.py. Fake credentials are assembled at runtime so this
file never contains a literal that looks like a real secret."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_secrets", REPO / "scripts" / "check_secrets.py")
check_secrets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_secrets)

FAKE_VALUE = "Zq7" * 13  # 39 harmless characters (contains digits like real credentials)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf8")
    return path


def test_flags_assignment_with_real_looking_value_without_echoing_it(tmp_path):
    _write(tmp_path, "settings.yaml", "PAYPAL_CLIENT_" + "SECRET: " + FAKE_VALUE + "\n")
    violations = check_secrets.scan(tmp_path)
    assert violations and "PayPal client secret assignment" in violations[0]
    assert FAKE_VALUE not in "\n".join(violations)


def test_ignores_blank_and_placeholder_values(tmp_path):
    _write(tmp_path, ".env.example", "PAYPAL_CLIENT_" + "SECRET=\nAI_API_" + "KEY=<your-key-here>\n")
    assert check_secrets.scan(tmp_path) == []


def test_flags_env_files_by_name_without_reading_them(tmp_path):
    _write(tmp_path, ".env", "ANYTHING=1\n")
    _write(tmp_path, ".env.local", "ANYTHING=1\n")
    assert len(check_secrets.scan(tmp_path)) == 2


def test_flags_known_token_shapes_in_any_file_type(tmp_path):
    _write(tmp_path, "notes.txt", "token " + "gh" + "p_" + "A" * 36 + "\n")
    _write(tmp_path, "run.sh", "curl -H 'Authorization: Bearer " + "a" * 30 + "'\n")
    _write(tmp_path, "key.txt", "-----BEGIN " + "RSA PRIVATE KEY-----\n")
    rules = " ".join(check_secrets.scan(tmp_path))
    assert "GitHub token" in rules and "bearer token" in rules and "private key block" in rules


def test_flags_credentials_embedded_in_urls(tmp_path):
    _write(tmp_path, "cfg.py", 'URL = "postgres://user:' + FAKE_VALUE + '@db.example.net/app"\n')
    assert any("credentials in URL" in v for v in check_secrets.scan(tmp_path))


def test_clean_tree_passes_and_binary_files_are_skipped(tmp_path):
    _write(tmp_path, "README.md", "Set the PayPal client secret in your server environment.\n")
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2" + b"Bearer " + b"a" * 30)
    assert check_secrets.scan(tmp_path) == []


def test_repository_itself_is_clean():
    # Scan what git would track so a developer's own (gitignored) .env or logs cannot break the test.
    assert check_secrets.scan(REPO, use_git=(REPO / ".git").exists()) == []
