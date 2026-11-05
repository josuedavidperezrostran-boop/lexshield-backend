"""Session revocation in the shared store.

- revoke_session: one session (logout). Kept until that session could have expired anyway.
- revoke_all: every session of a customer that started at or before now. Sessions started later
  (a new recovery or Checkout sign-in) are unaffected.
Tokens from before sessions existed carry no session id; only revoke_all reaches them.
The store is read on every authenticated call; if it is unavailable the call fails closed.
"""

from __future__ import annotations

import time

from .auth import SESSION_MAX_SECONDS, TokenInfo
from .store import get_store


def _session_key(session_id: str) -> str:
    return f"session:revoked:{session_id}"


def _customer_key(customer_id: str) -> str:
    return f"session:valid-after:{customer_id}"


def revoke_session(info: TokenInfo) -> None:
    if info.session_id:
        remaining = max(1.0, SESSION_MAX_SECONDS - info.session_age(time.time()))
        get_store().set(_session_key(info.session_id), "1", remaining)


def revoke_all(customer_id: str) -> None:
    get_store().set(_customer_key(customer_id), repr(time.time()), SESSION_MAX_SECONDS)


def session_is_active(info: TokenInfo) -> bool:
    """False when revoked. Raises StoreUnavailable when the store cannot be read."""
    store = get_store()
    if info.session_id and store.get(_session_key(info.session_id)) is not None:
        return False
    if info.customer_id:
        valid_after = store.get(_customer_key(info.customer_id))
        if valid_after is not None and info.auth_at <= float(valid_after):
            return False
    return True
