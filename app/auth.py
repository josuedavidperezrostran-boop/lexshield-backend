from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from .config import settings

# A token names a Stripe customer and a session. Access itself is always read from Stripe.
#
# Session: starts when the customer proves who they are (a fresh paid Checkout session or an
# email recovery code) and lasts at most SESSION_MAX_SECONDS from that proof. Renewing never
# moves that start, so renewals cannot be chained past it. A session can be revoked (logout) and
# all of a customer's sessions can be revoked at once (app/sessions.py, shared store).
#
# Token stages by age since issue, all inside the session limit:
# - Current (< TOKEN_TTL_SECONDS): accepted on API calls.
# - Renewal credential (< REFRESH_WINDOW_SECONDS): accepted only by /billing/refresh and, to
#   identify the customer, by /billing/checkout.
# - Older, or past the session limit: refused everywhere; the customer proves mailbox control
#   again through account recovery.
#
# Why these numbers: 7 days bounds how long a copied token is useful between renewals; 60 days
# covers two billing cycles away from the app; 90 days caps a stolen token's whole life, after
# which only someone who controls the customer's mailbox can sign in again.
TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60
REFRESH_WINDOW_SECONDS = 60 * 24 * 60 * 60
SESSION_MAX_SECONDS = 90 * 24 * 60 * 60

_V2 = "v2"


@dataclass(frozen=True)
class TokenInfo:
    issued_at: int
    # Time (seconds, millisecond precision) of the proof that started the session. Tokens from
    # before sessions existed use their own issue time, so their session limit counts from then.
    auth_at: float
    session_id: Optional[str]
    customer_id: Optional[str]

    def age(self, now: float) -> float:
        return now - self.issued_at

    def session_age(self, now: float) -> float:
        return now - self.auth_at


def tokens_available() -> bool:
    """In production a missing or default ACCESS_TOKEN_SECRET would let anyone forge tokens."""
    return settings.token_secret_is_safe() or not settings.is_production()


def new_session_id() -> str:
    return secrets.token_urlsafe(12)


def issue_access_token(
    customer_id: Optional[str] = None,
    *,
    auth_at: Optional[float] = None,
    session_id: Optional[str] = None,
) -> str:
    """Without auth_at/session_id this starts a new session; renewals pass the existing ones."""
    if not tokens_available():
        raise RuntimeError("ACCESS_TOKEN_SECRET must be set to a private value in production.")
    now = time.time()
    auth_ms = int((auth_at if auth_at is not None else now) * 1000)
    payload = ".".join(
        [_V2, str(int(now)), str(auth_ms), session_id or new_session_id(), customer_id or "-"]
    )
    return f"{payload}.{_sign(payload)}"


def parse_token(token: Optional[str]) -> Optional[TokenInfo]:
    """Signature-checked contents, or None. Says nothing about age or revocation."""
    if not tokens_available():
        return None
    payload = _signed_payload(token)
    if payload is None:
        return None
    parts = payload.split(".")
    try:
        if parts[0] == _V2 and len(parts) == 5:
            customer = parts[4] if parts[4].startswith("cus_") else None
            return TokenInfo(int(parts[1]), int(parts[2]) / 1000, parts[3], customer)
        # Pre-session format: "<issued_at>" or "<issued_at>.<customer_id>".
        if len(parts) in (1, 2):
            issued = int(parts[0])
            customer = parts[1] if len(parts) == 2 and parts[1].startswith("cus_") else None
            return TokenInfo(issued, issued, None, customer)
    except ValueError:
        return None
    return None


def _usable(info: Optional[TokenInfo], max_age: float) -> bool:
    if info is None:
        return False
    now = time.time()
    age = info.age(now)
    # issued_at is truncated to the second, so a session started in that same second is fine.
    started_before_issue = info.auth_at < info.issued_at + 1
    return 0 <= age < max_age and 0 <= info.session_age(now) < SESSION_MAX_SECONDS and started_before_issue


def token_is_valid(token: Optional[str]) -> bool:
    """Current stage and inside the session limit. Revocation is checked in app/sessions.py."""
    return _usable(parse_token(token), TOKEN_TTL_SECONDS)


def token_is_refreshable(token: Optional[str]) -> bool:
    """Renewal stage or newer, names a customer, inside the session limit."""
    info = parse_token(token)
    return _usable(info, REFRESH_WINDOW_SECONDS) and info is not None and info.customer_id is not None


def customer_id_from_token(token: Optional[str]) -> Optional[str]:
    info = parse_token(token)
    return info.customer_id if info else None


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    scheme, _, token = (authorization or "").partition(" ")
    return token if scheme.lower() == "bearer" and token else None


def _signed_payload(token: Optional[str]) -> Optional[str]:
    if not token or "." not in token:
        return None
    payload, digest = token.rsplit(".", 1)
    if not hmac.compare_digest(digest, _sign(payload)):
        return None
    return payload


def _sign(payload: str) -> str:
    return hmac.new(
        settings.access_token_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
