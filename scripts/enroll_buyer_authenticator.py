"""OPERATOR-ONLY enrolment of buyer-side TOTP authenticators (R2). Run on the server host; there is no HTTP route for this by design.

    python scripts/enroll_buyer_authenticator.py generate-key
    python scripts/enroll_buyer_authenticator.py enroll --project PROJECT_ID --buyer buyer@example.com
    python scripts/enroll_buyer_authenticator.py revoke --authenticator AUTH_ID --reason "device lost"
    python scripts/enroll_buyer_authenticator.py list --project PROJECT_ID

Needs DATABASE_URL, AUTHENTICATOR_KEYS ("id:base64key", 32-byte key) and optionally AUTHENTICATOR_ACTIVE_KEY_ID in the environment.
`enroll` prints the shared secret ONCE to this terminal for the operator to hand to the buyer over a channel Caligo cannot see.
It is not stored in plaintext anywhere and cannot be shown again; lose it and the operator must revoke and enrol again. Enrolment is
OPERATOR_ESTABLISHED: it is not verified real-world identity (docs/R2_AUTHORIZATION.md)."""
import argparse
import base64
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SchemaOutOfDateError, init_db, make_engine  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.services import authenticators as auth  # noqa: E402


def _keyring() -> auth.Keyring | None:
    settings = get_settings()
    return auth.parse_keyring(settings.authenticator_keys.get_secret_value(), settings.authenticator_active_key_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-key", help="print a fresh 32-byte key for AUTHENTICATOR_KEYS (store it in the environment, never in Git)")
    enroll = sub.add_parser("enroll")
    enroll.add_argument("--project", required=True)
    enroll.add_argument("--buyer", required=True)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--authenticator", required=True)
    revoke.add_argument("--reason", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--project", required=True)
    args = parser.parse_args(argv)

    if args.command == "generate-key":
        print(f"key=\"{auth.generate_key()}\"  # use as AUTHENTICATOR_KEYS=k1:<key> ; keep it out of Git and logs")
        return 0
    try:
        engine = make_engine()
        init_db(engine)
        with Session(engine, expire_on_commit=False) as db:
            if args.command == "enroll":
                row, secret = auth.enroll(db, _keyring(), args.project, args.buyer, getpass.getuser())
                print(f"authenticator_id={row.id} trust_basis={row.trust_basis} (operator-established, NOT verified identity)")
                print("Give this secret to the buyer over a separate channel. It is shown once; clear this terminal's scrollback afterwards.")
                print("WARNING: if you are also the seller of this project, this second channel is NOT independent.")
                print("base32_secret=" + base64.b32encode(secret).decode().rstrip("="))
                print("otpauth_uri=" + auth.provisioning_uri(secret, row.buyer_email))
            elif args.command == "revoke":
                revocation = auth.revoke(db, args.authenticator, args.reason, getpass.getuser())
                print(f"revoked authenticator_id={revocation.authenticator_id}")
            else:
                for row in auth.list_for_project(db, args.project):
                    view = auth.view_of(db, row)
                    print(f"{view.id} {view.buyer_email_masked} {view.status} {view.trust_basis.value}")
    except (ApiError, auth.KeyringError, SchemaOutOfDateError) as exc:
        print(f"error: {getattr(exc, 'message', None) or exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
