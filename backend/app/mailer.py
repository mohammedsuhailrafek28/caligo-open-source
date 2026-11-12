"""Outbound e-mail port. Only an in-process outbox exists: nothing is ever sent over a network."""
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class MailMessage:
    to: str
    subject: str
    body: str
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class FakeOutboxMailer:
    """Stands in for the buyer's inbox in tests and the demo. It is never reachable through any HTTP route."""

    def __init__(self, dev_file: str | Path | None = None) -> None:
        """`dev_file` (local development only, set via DEV_OUTBOX_FILE) additionally appends each message as one JSON line, so a developer can
        read the buyer's inbox from a file. It never leaves the machine and is never reachable through an HTTP route."""
        self._lock = threading.Lock()
        self.outbox: list[MailMessage] = []
        self._dev_file = Path(dev_file) if dev_file else None

    def send(self, to: str, subject: str, body: str) -> None:
        message = MailMessage(to, subject, body)
        with self._lock:
            self.outbox.append(message)
            if self._dev_file is not None:
                self._append(message)

    def _append(self, message: MailMessage) -> None:
        line = json.dumps({"to": message.to, "subject": message.subject, "body": message.body, "sent_at": message.sent_at.isoformat()})
        self._dev_file.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(self._dev_file, flags, 0o600)  # owner-only where the platform supports it
        with os.fdopen(fd, "a", encoding="utf8") as handle:
            handle.write(line + "\n")

    def messages_to(self, address: str) -> list[MailMessage]:
        return [m for m in self.outbox if m.to == address.lower()]
