"""LOCAL DEVELOPMENT ONLY: print the newest buyer link and one-time code from the dev outbox file (DEV_OUTBOX_FILE).

    python scripts/dev_outbox.py dev_outbox/buyer.jsonl            # newest link + newest code
    python scripts/dev_outbox.py dev_outbox/buyer.jsonl --all      # every message, oldest first
    python scripts/dev_outbox.py dev_outbox/buyer.jsonl --clear    # delete the file (it holds live one-time secrets)

Reads a local file only; no network, no database, no credentials. The values are one-time secrets of a SYNTHETIC test buyer.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LINK_RE = re.compile(r"https?://\S+/buyer/review\?token=\S+")
CODE_RE = re.compile(r"code: (\d{6})")


def load(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"skipping malformed line {number}", file=sys.stderr)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path)
    parser.add_argument("--all", action="store_true", help="print every message")
    parser.add_argument("--clear", action="store_true", help="delete the file")
    args = parser.parse_args(argv)
    if args.clear:
        args.file.unlink(missing_ok=True)
        print(f"removed {args.file}")
        return 0
    if not args.file.is_file():
        print(f"no outbox file at {args.file}: is the API running with BUYER_MAIL_MODE=fake and DEV_OUTBOX_FILE set?", file=sys.stderr)
        return 1
    rows = load(args.file)
    if args.all:
        for row in rows:
            print(f"[{row.get('sent_at')}] to={row.get('to')} subject={row.get('subject')}\n{row.get('body')}\n")
        return 0
    link = next((m.group(0) for row in reversed(rows) if (m := LINK_RE.search(row.get("body", "")))), None)
    code = next((m.group(1) for row in reversed(rows) if (m := CODE_RE.search(row.get("body", "")))), None)
    print(f"link: {link or '(none yet)'}")
    print(f"code: {code or '(none yet)'}")
    return 0 if link or code else 1


if __name__ == "__main__":
    raise SystemExit(main())
