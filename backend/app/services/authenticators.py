"""Operator-established TOTP authenticators for buyer-side verification of payment-instruction changes (R2).

Trust model: see docs/R2_AUTHORIZATION.md. Authenticators are created and revoked ONLY by the operator (scripts/enroll_buyer_authenticator.py);
no HTTP route creates, replaces, resets or reveals one. Secrets are encrypted at rest with AES-256-GCM, bound by associated data to the row they
belong to, and keys come from the environment only. Nothing in this module logs a secret, a code or a key."""
import base64
import binascii
import hashlib
import hmac
import os
import secrets
import struct
import time
from dataclasses import dataclass
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import AuthenticatorRevocation, BuyerAuthenticator, Project, new_id
from app.schemas import AuditActor, AuthenticatorTrustBasis
from app.security import normalize_email
from app.services import instructions
from app.services.scope import record_audit

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # accept the previous, current and next step (clock skew)
SECRET_BYTES = 20  # 160-bit, the RFC 4226 recommendation
KEY_BYTES = 32
_AAD_PREFIX = "caligo-authenticator-v1"


class AuthenticatorUnavailable(Exception):
    """The secret cannot be decrypted (key lost, rotated away, or ciphertext moved). Verification fails closed."""


class KeyringError(ValueError):
    """The configured keyring is unusable. The message never contains key material."""


@dataclass(frozen=True)
class Keyring:
    keys: dict[str, bytes]
    active_id: str

    def __repr__(self) -> str:  # never print key material
        return f"Keyring(ids={sorted(self.keys)}, active={self.active_id!r})"


def parse_keyring(raw: str, active_id: str) -> Keyring | None:
    """`raw` is "id:base64key,id2:base64key2". Returns None when no keys are configured (feature disabled)."""
    raw = raw.strip()
    if not raw:
        return None
    keys: dict[str, bytes] = {}
    for item in raw.split(","):
        key_id, sep, encoded = item.strip().partition(":")
        if not sep or not key_id or len(key_id) > 32 or not all(c.isalnum() or c in "-_" for c in key_id):
            raise KeyringError("authenticator key entries must look like id:base64key with a short alphanumeric id")
        try:
            key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (binascii.Error, ValueError):
            raise KeyringError("authenticator key is not valid base64") from None
        if len(key) != KEY_BYTES:
            raise KeyringError(f"authenticator keys must be exactly {KEY_BYTES} bytes")
        if key_id in keys:
            raise KeyringError("duplicate authenticator key id")
        keys[key_id] = key
    active = active_id.strip() or next(iter(keys))
    if active not in keys:
        raise KeyringError("the active authenticator key id is not in the keyring")
    return Keyring(keys, active)


def generate_key() -> str:
    """A fresh key in the configuration format (id not included). For the operator to put in the environment, never in Git."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode()


def _aad(authenticator_id: str, project_id: str, buyer_email: str) -> bytes:
    return f"{_AAD_PREFIX}|{authenticator_id}|{project_id}|{buyer_email}".encode("utf8")


def encrypt_secret(keyring: Keyring, secret: bytes, authenticator_id: str, project_id: str, buyer_email: str) -> tuple[str, str]:
    nonce = os.urandom(12)
    sealed = AESGCM(keyring.keys[keyring.active_id]).encrypt(nonce, secret, _aad(authenticator_id, project_id, buyer_email))
    return keyring.active_id, base64.b64encode(nonce + sealed).decode()


def decrypt_secret(keyring: Keyring | None, row: BuyerAuthenticator) -> bytes:
    if keyring is None or row.key_id not in keyring.keys:
        raise AuthenticatorUnavailable("key not available")
    try:
        blob = base64.b64decode(row.secret_ciphertext, validate=True)
        return AESGCM(keyring.keys[row.key_id]).decrypt(blob[:12], blob[12:], _aad(row.id, row.project_id, row.buyer_email))
    except (InvalidTag, binascii.Error, ValueError):
        raise AuthenticatorUnavailable("secret cannot be decrypted") from None


# --- TOTP (RFC 6238 over RFC 4226, HMAC-SHA1) -------------------------------------------------------------------------

def now() -> float:
    return time.time()  # indirection so tests can pin the clock


def hotp(secret: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def step_at(at: float) -> int:
    return int(at // TOTP_STEP_SECONDS)


def totp(secret: bytes, at: float, digits: int = TOTP_DIGITS) -> str:
    return hotp(secret, step_at(at), digits)


def matching_step(secret: bytes, code: str, at: float) -> int | None:
    """The time step whose code equals `code` (within the skew window), or None. Every candidate is compared in constant time."""
    found: int | None = None
    for step in range(step_at(at) - TOTP_WINDOW, step_at(at) + TOTP_WINDOW + 1):
        if hmac.compare_digest(hotp(secret, step), code) and found is None:
            found = step
    return found


def provisioning_uri(secret: bytes, account: str, issuer: str = "Caligo") -> str:
    encoded = base64.b32encode(secret).decode().rstrip("=")
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={encoded}&issuer={quote(issuer)}&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"


# --- registry (operator actions and read helpers) --------------------------------------------------------------------

def active_authenticator(db: Session, project_id: str, buyer_email: str) -> BuyerAuthenticator | None:
    revoked = select(AuthenticatorRevocation.authenticator_id)
    return db.scalar(select(BuyerAuthenticator).where(
        BuyerAuthenticator.project_id == project_id, BuyerAuthenticator.buyer_email == buyer_email,
        BuyerAuthenticator.id.notin_(revoked)).order_by(BuyerAuthenticator.created_at.desc()).limit(1))


def revocation_of(db: Session, authenticator_id: str) -> AuthenticatorRevocation | None:
    return db.scalar(select(AuthenticatorRevocation).where(AuthenticatorRevocation.authenticator_id == authenticator_id))


def list_for_project(db: Session, project_id: str) -> list[BuyerAuthenticator]:
    return list(db.scalars(select(BuyerAuthenticator).where(BuyerAuthenticator.project_id == project_id)
                           .order_by(BuyerAuthenticator.created_at, BuyerAuthenticator.id)))


def enroll(db: Session, keyring: Keyring | None, project_id: str, buyer_email: str, operator: str = "unknown") -> tuple[BuyerAuthenticator, bytes]:
    """OPERATOR action. Returns the row and the plaintext secret (shown once to the operator, never stored or returned elsewhere)."""
    if keyring is None:
        raise ApiError(503, "authenticator_keys_missing", "No authenticator encryption key is configured.")
    email = normalize_email(buyer_email)
    project = db.get(Project, project_id)
    if project is None:
        raise ApiError(404, "not_found", "Project not found.")
    instructions.lock_project(db, project_id)
    current = instructions.effective_instruction(db, project_id)
    if current is None:
        db.rollback()
        raise ApiError(409, "instruction_required", "Record the payment instruction before enrolling an authenticator.")
    if email != current.recipient_email:
        db.rollback()
        raise ApiError(422, "approver_mismatch", "The authenticator must belong to the buyer named in the payment instruction.")
    if active_authenticator(db, project_id, email) is not None:
        db.rollback()
        raise ApiError(409, "authenticator_exists", "An active authenticator exists; revoke it explicitly before enrolling another.")
    secret = secrets.token_bytes(SECRET_BYTES)
    auth_id = new_id()
    key_id, ciphertext = encrypt_secret(keyring, secret, auth_id, project_id, email)
    row = BuyerAuthenticator(id=auth_id, project_id=project_id, buyer_email=email, secret_ciphertext=ciphertext, key_id=key_id,
                             trust_basis=AuthenticatorTrustBasis.OPERATOR_ESTABLISHED.value)
    db.add(row)
    db.flush()
    record_audit(db, project_id, "AUTHENTICATOR_ENROLLED", AuditActor.SYSTEM, "authenticator", row.id,
                 {"trust_basis": row.trust_basis, "channel": "OPERATOR_CLI", "operator": operator[:64], "real_world_identity_verified": False})
    db.commit()
    return row, secret


def revoke(db: Session, authenticator_id: str, reason: str, operator: str = "unknown") -> AuthenticatorRevocation:
    """OPERATOR action. Append-only; an existing revocation is returned unchanged."""
    row = db.get(BuyerAuthenticator, authenticator_id)
    if row is None:
        raise ApiError(404, "not_found", "Authenticator not found.")
    instructions.lock_project(db, row.project_id)
    existing = revocation_of(db, authenticator_id)
    if existing is not None:
        db.rollback()
        return existing
    revocation = AuthenticatorRevocation(authenticator_id=authenticator_id, project_id=row.project_id, reason="".join(c for c in reason if c.isprintable()).strip()[:200] or "unspecified")
    db.add(revocation)
    record_audit(db, row.project_id, "AUTHENTICATOR_REVOKED", AuditActor.SYSTEM, "authenticator", authenticator_id,
                 {"channel": "OPERATOR_CLI", "operator": operator[:64]})
    db.commit()
    return revocation


def view_of(db: Session, row: BuyerAuthenticator):
    from app import schemas
    from app.services.buyer import mask_email

    revoked = revocation_of(db, row.id)
    return schemas.BuyerAuthenticatorView(
        id=row.id, project_id=row.project_id, buyer_email_masked=mask_email(row.buyer_email),
        trust_basis=AuthenticatorTrustBasis(row.trust_basis), status="REVOKED" if revoked else "ACTIVE",
        created_at=row.created_at, revoked_at=revoked.created_at if revoked else None)

