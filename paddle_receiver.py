"""Paddle Billing sandbox webhook.

Verifies the raw body against the paddle-signature header, then records
subscription lifecycle only. Customer, contract, and custom_data fields
are read nowhere and written nowhere.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time

from fastapi import FastAPI, HTTPException, Request

logger = logging.getLogger("lexshield.paddle")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

_WEBHOOK_SECRET_ENV = "PADDLE_SANDBOX_WEBHOOK_SECRET"
_API_KEY_ENV = "PADDLE_SANDBOX_API_KEY"
_SIGNATURE_HEADER = "paddle-signature"
_MAX_BODY_BYTES = 64 * 1024
# Paddle rejects notifications whose timestamp is more than five seconds off.
_SIGNATURE_MAX_AGE_SECONDS = 5
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9._]{0,63}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PARTNER_PLAN_USD = 100

app = FastAPI(title="LexShield Paddle Sandbox", version="1.0.0")


def _secret_value(name: str) -> str:
    """Return a configured secret. Placeholder values count as unset."""
    value = os.environ.get(name, "").strip()
    lowered = value.lower()
    if not value or "your_" in lowered or lowered.startswith("replace"):
        return ""
    return value


def credentials_configured() -> bool:
    """Both sandbox credentials must be present. Neither is logged."""
    return bool(_secret_value(_WEBHOOK_SECRET_ENV) and _secret_value(_API_KEY_ENV))


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


@app.post("/api/v1/paddle/webhook")
async def paddle_webhook(request: Request) -> dict[str, bool]:
    """Ingest a raw Paddle sandbox notification and drop everything but the event name."""
    if not credentials_configured():
        logger.error("PADDLE SANDBOX: credentials are not configured.")
        raise HTTPException(status_code=503, detail="Paddle sandbox credentials are not configured.")

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


_CONTRACT_BAIT = "CONFIDENTIAL CLAUSE the party shall indemnify without cap"
_EMAIL_BAIT = "counsel@example.com"
_MOCK_EVENTS = (
    "subscription.created",
    "subscription.canceled",
    "transaction.completed",
)


def _mock_body(event_type: str) -> bytes:
    payload = {
        "event_id": "evt_sandbox_mock",
        "event_type": event_type,
        "occurred_at": "2026-10-13T21:50:00.000000Z",
        "notification_id": "ntf_sandbox_mock",
        "data": {
            "id": "sub_sandbox_mock",
            "status": "active" if event_type == "subscription.created" else "canceled",
            "customer_id": "ctm_sandbox_mock",
            "custom_data": {
                "contract_text": _CONTRACT_BAIT,
                "email": _EMAIL_BAIT,
            },
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def main() -> None:
    """Post signed sandbox fixtures at the webhook and print status lines only."""
    os.environ.setdefault(_WEBHOOK_SECRET_ENV, "sandbox_webhook_secret_local_only")
    os.environ.setdefault(_API_KEY_ENV, "sandbox_api_key_local_only")
    secret = _secret_value(_WEBHOOK_SECRET_ENV)
    if not secret:
        raise SystemExit("PADDLE_SANDBOX_WEBHOOK_SECRET is not configured.")

    from fastapi.testclient import TestClient

    capture = _Capture()
    logger.addHandler(capture)
    client = TestClient(app)

    print("Paddle sandbox webhook mock", flush=True)
    for event_type in _MOCK_EVENTS:
        raw_body = _mock_body(event_type)
        response = client.post(
            "/api/v1/paddle/webhook",
            content=raw_body,
            headers={_SIGNATURE_HEADER: build_paddle_signature(raw_body, secret)},
        )
        print(
            f"event={event_type} http={response.status_code} body={response.json()}",
            flush=True,
        )

    signed_body = _mock_body("subscription.created")
    tampered = bytearray(signed_body)
    tampered[-8] ^= 0x01
    response = client.post(
        "/api/v1/paddle/webhook",
        content=bytes(tampered),
        headers={_SIGNATURE_HEADER: build_paddle_signature(signed_body, secret)},
    )
    print(f"event=tampered http={response.status_code} body={response.json()}", flush=True)

    logged = "\n".join(capture.messages)
    print(
        "zdr "
        f"contract_logged={_CONTRACT_BAIT in logged} "
        f"email_logged={_EMAIL_BAIT in logged} "
        f"customer_id_logged={'ctm_sandbox_mock' in logged}",
        flush=True,
    )


if __name__ == "__main__":
    main()
