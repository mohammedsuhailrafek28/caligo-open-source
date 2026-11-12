"""Password hashing, opaque tokens, and login throttling. No secrets are ever logged here."""
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# argon2-cffi's PasswordHasher defaults to Argon2id with RFC 9106 low-memory parameters.
_hasher = PasswordHasher()
# Verified for unknown accounts so response time does not reveal whether an email exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant-effort check; a missing account still pays for one Argon2 verification."""
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def normalize_email(email: str) -> str:
    return email.strip().lower()


def new_token() -> str:
    """256 bits from the OS CSPRNG, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are high-entropy, so a fast digest is appropriate; the database stores only this."""
    return hashlib.sha256(token.encode("utf8")).hexdigest()


def tokens_match(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf8"), b.encode("utf8"))


class LoginThrottle:
    """In-memory sliding-window failure counter (per process; fine for a single-node demo)."""

    def __init__(self, max_failures: int, window_seconds: int, clock=time.monotonic) -> None:
        self._max = max_failures
        self._window = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str) -> deque[float]:
        events = self._failures[key]
        cutoff = self._clock() - self._window
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            self._failures.pop(key, None)
            return deque()
        return events

    def is_blocked(self, key: str, limit_multiplier: int = 1) -> bool:
        with self._lock:
            return len(self._prune(key)) >= self._max * limit_multiplier

    MAX_KEYS = 10_000  # hard bound on tracked keys (memory-exhaustion guard)

    def record_failure(self, key: str) -> None:
        with self._lock:
            if len(self._failures) >= self.MAX_KEYS and key not in self._failures:
                for stale in list(self._failures):  # sweep everything that has aged out
                    self._prune(stale)
                if len(self._failures) >= self.MAX_KEYS:  # still full: drop the oldest tracked key
                    self._failures.pop(next(iter(self._failures)))
            self._prune(key)
            self._failures[key].append(self._clock())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
