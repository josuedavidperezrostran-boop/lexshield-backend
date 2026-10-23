"""Outgoing mail behind one interface. No real provider is wired yet.

MAIL_BACKEND:
- "" (default): disabled. Sending raises MailUnavailable; recovery stays unavailable.
- "directory:/path": writes each message to a file. Local development only; refused in
  production because the files contain one-time sign-in links.
A provider adapter (for example Resend over HTTPS) is added once the provider is chosen.
Tests inject MemoryMailer through use_mailer().
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from .config import settings


class MailUnavailable(Exception):
    """No usable mail backend. Callers must not pretend the message was sent."""


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str


class Mailer(Protocol):
    def send(self, message: Message) -> None: ...


class MemoryMailer:
    def __init__(self) -> None:
        self.sent: list[Message] = []
        self._lock = threading.Lock()

    def send(self, message: Message) -> None:
        with self._lock:
            self.sent.append(message)


class DirectoryMailer:
    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)

    def send(self, message: Message) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}.txt"
        (self._dir / name).write_text(
            f"To: {message.to}\nSubject: {message.subject}\n\n{message.text}\n", encoding="utf-8"
        )


_override: Optional[Mailer] = None


def use_mailer(mailer: Optional[Mailer]) -> None:
    """Tests inject an adapter; None returns to MAIL_BACKEND."""
    global _override
    _override = mailer


def get_mailer() -> Mailer:
    if _override is not None:
        return _override
    backend = settings.mail_backend.strip()
    if not backend:
        raise MailUnavailable("MAIL_BACKEND is not configured.")
    if settings.is_production() and (backend == "memory" or backend.startswith("directory:")):
        raise MailUnavailable("Development mail backends are refused in production.")
    if backend.startswith("directory:"):
        return DirectoryMailer(backend[len("directory:"):])
    raise MailUnavailable("Unsupported MAIL_BACKEND.")
