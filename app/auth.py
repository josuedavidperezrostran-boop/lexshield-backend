from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

from .config import settings

# One signed token, three stages by age:
# - Current (< TOKEN_TTL_SECONDS): identifies the Stripe customer on API calls. It never grants
#   access by itself; every call also checks the subscription in Stripe (app/subscriptions.py).
#   Seven days bounds how long a copied token is useful while a monthly customer rarely sees it
#   expire.
# - Renewal credential (TOKEN_TTL_SECONDS .. REFRESH_WINDOW_SECONDS): accepted only by
#   /billing/refresh, which re-reads Stripe and issues a new token only while the customer still
#   has a LexShield subscription to use or fix, and by /billing/checkout to reuse the customer.
#   Sixty days covers two billing cycles away from the app without asking for recovery.
# - Older: refused everywhere. The customer needs account recovery (proof of mailbox control).
TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60
REFRESH_WINDOW_SECONDS = 60 * 24 * 60 * 60


def tokens_available() -> bool:
    """In production a missing or default ACCESS_TOKEN_SECRET would let anyone forge tokens."""
    return settings.token_secret_is_safe() or not settings.is_production()


def issue_access_token(customer_id: Optional[str] = None) -> str:
    if not tokens_available():
        raise RuntimeError("ACCESS_TOKEN_SECRET must be set to a private value in production.")
    issued_at = str(int(time.time()))
    payload = f"{issued_at}.{customer_id}" if customer_id else issued_at
    return f"{payload}.{_sign(payload)}"


def _token_age(token: Optional[str]) -> Optional[float]:
    if not tokens_available():
        return None
    payload = _signed_payload(token)
    if payload is None:
        return None
    issued_at = payload.split(".", 1)[0]
    try:
        timestamp = int(issued_at)
    except ValueError:
        return None
    age = time.time() - timestamp
    return age if age >= 0 else None


def token_is_valid(token: Optional[str]) -> bool:
    age = _token_age(token)
    return age is not None and age < TOKEN_TTL_SECONDS


def token_is_refreshable(token: Optional[str]) -> bool:
    """Authentic, names a customer, and still inside the refresh window."""
    age = _token_age(token)
    return age is not None and age < REFRESH_WINDOW_SECONDS and customer_id_from_token(token) is not None


def customer_id_from_token(token: Optional[str]) -> Optional[str]:
    if not tokens_available():
        return None
    payload = _signed_payload(token)
    if not payload or "." not in payload:
        return None
    _, customer_id = payload.split(".", 1)
    if not customer_id.startswith("cus_"):
        return None
    return customer_id


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


def _sign(issued_at: str) -> str:
    return hmac.new(
        settings.access_token_secret.encode("utf-8"),
        issued_at.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
