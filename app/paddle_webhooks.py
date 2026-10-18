"""Paddle Billing sandbox webhook.

Verifies the raw body against the paddle-signature header, then logs the
event name only. Customer, contract, and custom_data fields are read
nowhere and written nowhere. No access or subscription state is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("lexshield.paddle")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

_WEBHOOK_SECRET_ENV = "PADDLE_SANDBOX_WEBHOOK_SECRET"
_SIGNATURE_HEADER = "paddle-signature"
_MAX_BODY_BYTES = 64 * 1024
# Paddle rejects notifications whose timestamp is more than five seconds off.
_SIGNATURE_MAX_AGE_SECONDS = 5
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9._]{0,63}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PARTNER_PLAN_USD = 100

router = APIRouter(tags=["paddle"])


def _secret_value(name: str) -> str:
    """Return a configured secret. Placeholder values count as unset."""
    value = os.environ.get(name, "").strip()
    lowered = value.lower()
    if not value or "your_" in lowered or lowered.startswith("replace"):
        return ""
    return value


def webhook_secret_configured() -> bool:
    """The sandbox webhook secret must be present. It is never logged."""
    return bool(_secret_value(_WEBHOOK_SECRET_ENV))


def parse_paddle_signature(header: str) -> tuple[str, list[str]] | None:
    """Split `ts=<unix>;h1=<hex>` into the timestamp and every h1 signature."""
    timestamp = ""
    signatures: list[str] = []
    for part in header.split(";"):
        key, separator, value = part.strip().partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if key == "ts" and value.isdigit():
            timestamp = value
        elif key == "h1" and _SHA256_HEX.fullmatch(value):
            signatures.append(value)
    if not timestamp or not signatures:
        return None
    return timestamp, signatures


def verify_paddle_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """HMAC-SHA256 of `timestamp:raw_body`, compared in constant time."""
    if not secret or not signature_header:
        return False
    parsed = parse_paddle_signature(signature_header)
    if parsed is None:
        return False
    timestamp, signatures = parsed
    try:
        issued_at = int(timestamp)
    except ValueError:
        return False
    age = int(time.time()) - issued_at
    if age < -_SIGNATURE_MAX_AGE_SECONDS or age > _SIGNATURE_MAX_AGE_SECONDS:
        return False
    signed_payload = timestamp.encode("ascii") + b":" + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def build_paddle_signature(raw_body: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a paddle-signature header for a body signed with the webhook secret."""
    issued_at = int(time.time()) if timestamp is None else timestamp
    timestamp_text = str(issued_at)
    signed_payload = timestamp_text.encode("ascii") + b":" + raw_body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"ts={timestamp_text};h1={digest}"


def event_type_from_body(raw_body: bytes) -> str:
    """Return a safe event name. Every other field is discarded."""
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(event, dict):
        return ""
    value = event.get("event_type")
    if isinstance(value, str) and _EVENT_TYPE.fullmatch(value):
        return value
    return ""


def handle_lifecycle_event(event_type: str) -> None:
    """Record activation or revocation. The log line carries no customer data."""
    if event_type == "subscription.created":
        logger.info(
            "PADDLE SANDBOX: partner activation confirmed ($%s/mo).",
            _PARTNER_PLAN_USD,
        )
        return
    if event_type == "subscription.canceled":
        logger.warning("PADDLE SANDBOX SECURITY ALERT: partner access revoked.")
        return
    if event_type:
        logger.info("PADDLE SANDBOX: lifecycle event ignored (%s).", event_type)
        return
    logger.info("PADDLE SANDBOX: lifecycle event ignored (unparsed).")


@router.post("/api/v1/paddle/webhook")
async def paddle_webhook(request: Request) -> dict[str, bool]:
    """Ingest a raw Paddle sandbox notification and drop everything but the event name."""
    if not webhook_secret_configured():
        logger.error("PADDLE SANDBOX: webhook secret is not configured.")
        raise HTTPException(status_code=503, detail="Paddle sandbox webhook secret is not configured.")

    raw_body = await request.body()
    if len(raw_body) > _MAX_BODY_BYTES:
        logger.warning("PADDLE SANDBOX: payload rejected (oversized).")
        raise HTTPException(status_code=413, detail="Webhook payload is too large.")

    signature = request.headers.get(_SIGNATURE_HEADER, "")
    secret = _secret_value(_WEBHOOK_SECRET_ENV)
    if not verify_paddle_signature(raw_body, signature, secret):
        logger.warning("PADDLE SANDBOX: signature verification failed.")
        raise HTTPException(status_code=400, detail="Invalid Paddle signature.")

    handle_lifecycle_event(event_type_from_body(raw_body))
    return {"received": True}
