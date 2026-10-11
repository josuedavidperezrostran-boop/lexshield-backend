from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

from .config import settings

TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


def issue_access_token(customer_id: Optional[str] = None) -> str:
    issued_at = str(int(time.time()))
    payload = f"{issued_at}.{customer_id}" if customer_id else issued_at
    return f"{payload}.{_sign(payload)}"


def token_is_valid(token: Optional[str]) -> bool:
    payload = _signed_payload(token)
    if payload is None:
        return False
    issued_at = payload.split(".", 1)[0]
    try:
        timestamp = int(issued_at)
    except ValueError:
        return False
    return 0 <= time.time() - timestamp < TOKEN_TTL_SECONDS


def customer_id_from_token(token: Optional[str]) -> Optional[str]:
    payload = _signed_payload(token)
    if not payload or "." not in payload:
        return None
    _, customer_id = payload.split(".", 1)
    if not customer_id.startswith("cus_"):
        return None
    return customer_id


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
