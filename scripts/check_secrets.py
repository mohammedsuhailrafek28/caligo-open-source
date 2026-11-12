"""Local secret-pattern guardrail. NOT a replacement for gitleaks/trufflehog.

Usage:
    python scripts/check_secrets.py            # walk the repo tree
    python scripts/check_secrets.py --git      # scan exactly what git would track
                                               # (tracked + untracked, honouring .gitignore)

Findings print as `path:line: rule` only. Matched text is never printed.

Known limitations (see docs/SECURITY.md): regex/heuristic only, no entropy analysis of
arbitrary strings, no git-history scan, no binary/archive inspection, files over 1 MB are
skipped, and a secret that does not match a known shape will not be found. Run gitleaks
or trufflehog (including history) before any push or public release.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".next", "dist"}
MAX_BYTES = 1_000_000
MIN_VALUE_LEN = 8
# Digit-free identifiers/calls such as `SecretStr(` are code, not credentials (limitation: an
# all-letters secret assigned unquoted to these names would be missed).
CODE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_.]*\(?")
PLACEHOLDER = re.compile(r"(<[^>]*>|change.?me|example|your[_-]|placeholder|replace|xxx+|\.\.\.|^\$\{)", re.I)

# (rule name, pattern, capture-group holding the value or None when the shape alone is conclusive)
RULES: list[tuple[str, re.Pattern[str], int | None]] = [
    ("PayPal client secret assignment", re.compile(r"PAYPAL_CLIENT_SECRET\s*[=:]\s*['\"]?([^\s'\"#]*)", re.I), 1),
    ("PayPal client id assignment", re.compile(r"PAYPAL_CLIENT_ID\s*[=:]\s*['\"]?([^\s'\"#]*)", re.I), 1),
    ("AI API key assignment", re.compile(r"AI_API_KEY\s*[=:]\s*['\"]?([^\s'\"#]*)", re.I), 1),
    ("PayPal access token", re.compile(r"A21AA[A-Za-z0-9_-]{40,}"), None),
    ("PayPal-style token marker", re.compile(r"access_token\$(?:sandbox|production)\$"), None),
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"), None),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), None),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"), None),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), None),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), None),
    ("provider-style secret key (sk-...)", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"), None),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), None),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}", re.I), None),
    ("credentials in URL", re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@'\"]+:([^\s/@'\"]{3,})@"), 1),
    ("generic secret assignment", re.compile(
        r"(?:secret|token|passwd|password|api[_-]?key)\w*\s*[=:]\s*['\"]([A-Za-z0-9/_+=.-]{20,})['\"]", re.I), 1),
]


def candidate_files(root: Path, use_git: bool) -> list[Path]:
    if use_git:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=True,
        ).stdout.decode("utf8", errors="ignore")
        return [root / name for name in out.split("\0") if name]
    return [p for p in root.rglob("*") if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts)]


def scan_text(text: str) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule, pattern, group in RULES:
            match = pattern.search(line)
            if not match:
                continue
            if group is not None:
                value = match.group(group)
                # Short values are type annotations/identifiers (e.g. `str`), not credentials.
                if len(value) < MIN_VALUE_LEN or PLACEHOLDER.search(value) or CODE_IDENTIFIER.fullmatch(value):
                    continue
            findings.append((lineno, rule))
    return findings


def scan(root: Path = ROOT, use_git: bool = False) -> list[str]:
    violations: list[str] = []
    for path in candidate_files(root, use_git):
        rel = path.relative_to(root).as_posix()
        name = path.name
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            violations.append(f"{rel}: environment file must not be in the repository")
            continue
        try:
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        for lineno, rule in scan_text(raw.decode("utf8", errors="ignore")):
            violations.append(f"{rel}:{lineno}: {rule}")
    return violations


def main(argv: list[str]) -> int:
    use_git = "--git" in argv
    violations = scan(ROOT, use_git)
    if violations:
        print("Potential secrets found (values intentionally not shown):")
        print("\n".join(violations))
        return 1
    mode = "git-tracked-or-trackable files" if use_git else "repository tree"
    print(f"Local secret-pattern check passed on {mode}. Heuristic only: run gitleaks/trufflehog before pushing.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
