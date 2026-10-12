"""Subscription entitlements only. Contract text is never stored."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import settings

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_LOCK = threading.Lock()

PREMIUM_PARTNER_TIER = "Premium Partner ($100/mo)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS partners (
    customer_id TEXT PRIMARY KEY,
    subscription_id TEXT,
    checkout_session_id TEXT NOT NULL,
    payment_intent_id TEXT,
    event_id TEXT NOT NULL,
    email TEXT,
    tier TEXT NOT NULL,
    payment_status TEXT NOT NULL,
    amount_total INTEGER,
    currency TEXT,
    provisioned_at TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class PartnerOrder:
    event_id: str
    checkout_session_id: str
    customer_id: str
    subscription_id: str | None
    payment_intent_id: str | None
    email: str | None
    tier: str
    payment_status: str
    amount_total: int | None
    currency: str | None
    provisioned_at: str


def partners_db_path() -> Path:
    configured = settings.partners_db_path.strip()
    if configured:
        return Path(configured)
    return _BACKEND_DIR / "data" / "partners.sqlite"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    return conn


def upsert_partner(order: PartnerOrder) -> None:
    """Write the same row for a repeated delivery of the same checkout."""
    path = partners_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = _connect(path)
        try:
            conn.execute(
                """
                INSERT INTO partners (
                    customer_id, subscription_id, checkout_session_id,
                    payment_intent_id, event_id, email, tier, payment_status,
                    amount_total, currency, provisioned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(customer_id) DO UPDATE SET
                    subscription_id = excluded.subscription_id,
                    checkout_session_id = excluded.checkout_session_id,
                    payment_intent_id = excluded.payment_intent_id,
                    event_id = excluded.event_id,
                    email = COALESCE(excluded.email, partners.email),
                    tier = excluded.tier,
                    payment_status = excluded.payment_status,
                    amount_total = excluded.amount_total,
                    currency = excluded.currency,
                    provisioned_at = excluded.provisioned_at
                """,
                (
                    order.customer_id,
                    order.subscription_id,
                    order.checkout_session_id,
                    order.payment_intent_id,
                    order.event_id,
                    order.email,
                    order.tier,
                    order.payment_status,
                    order.amount_total,
                    order.currency,
                    order.provisioned_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def partner_tier(customer_id: str) -> str | None:
    path = partners_db_path()
    if not path.exists():
        return None
    with _LOCK:
        conn = _connect(path)
        try:
            row = conn.execute(
                "SELECT tier FROM partners WHERE customer_id = ?",
                (customer_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return str(row[0])
