"""Signature and log checks for the Paddle sandbox webhook."""

from __future__ import annotations

import json
import logging
import os
import time
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.paddle_webhooks import build_paddle_signature, logger

SECRET = "sandbox_webhook_secret_test_only"
SECRET_ENV = "PADDLE_SANDBOX_WEBHOOK_SECRET"
API_KEY_ENV = "PADDLE_SANDBOX_API_KEY"
CONTRACT = "CONFIDENTIAL CLAUSE the party shall indemnify without cap"
EMAIL = "counsel@example.com"
PATH = "/api/v1/paddle/webhook"


def _body(event_type: str) -> bytes:
    payload = {
        "event_id": "evt_sandbox_test",
        "event_type": event_type,
        "occurred_at": "2026-10-16T00:00:00.000000Z",
        "notification_id": "ntf_sandbox_test",
        "data": {
            "id": "sub_sandbox_test",
            "status": "active",
            "customer_id": "ctm_sandbox_test",
            "custom_data": {
                "contract_text": CONTRACT,
                "email": EMAIL,
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


class PaddleWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_secret = os.environ.get(SECRET_ENV)
        self._previous_api_key = os.environ.get(API_KEY_ENV)
        os.environ[SECRET_ENV] = SECRET
        os.environ.pop(API_KEY_ENV, None)
        self.capture = _Capture()
        logger.addHandler(self.capture)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        logger.removeHandler(self.capture)
        if self._previous_secret is None:
            os.environ.pop(SECRET_ENV, None)
        else:
            os.environ[SECRET_ENV] = self._previous_secret
        if self._previous_api_key is None:
            os.environ.pop(API_KEY_ENV, None)
        else:
            os.environ[API_KEY_ENV] = self._previous_api_key

    def _post(self, payload: bytes, signature: str | None) -> object:
        headers = {}
        if signature is not None:
            headers["paddle-signature"] = signature
        return self.client.post(PATH, content=payload, headers=headers)

    def test_signed_lifecycle_events_are_acknowledged(self) -> None:
        self.assertNotIn(API_KEY_ENV, os.environ)
        for event_type in (
            "subscription.created",
            "subscription.canceled",
            "transaction.completed",
        ):
            with self.subTest(event_type=event_type):
                payload = _body(event_type)
                response = self._post(payload, build_paddle_signature(payload, SECRET))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"received": True})

    def test_second_h1_signature_is_accepted(self) -> None:
        payload = _body("transaction.completed")
        signature = build_paddle_signature(payload, SECRET)
        timestamp, _, digest = signature.partition(";h1=")
        header = f"{timestamp};h1={'0' * 64};h1={digest}"
        response = self._post(payload, header)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"received": True})

    def test_tampered_body_is_rejected(self) -> None:
        payload = bytearray(_body("subscription.created"))
        signature = build_paddle_signature(bytes(payload), SECRET)
        payload[-1] ^= 0x01
        response = self._post(bytes(payload), signature)
        self.assertEqual(response.status_code, 400)

    def test_missing_signature_is_rejected(self) -> None:
        response = self._post(_body("subscription.created"), None)
        self.assertEqual(response.status_code, 400)

    def test_stale_timestamp_is_rejected(self) -> None:
        payload = _body("subscription.created")
        issued_at = int(time.time()) - 6
        response = self._post(payload, build_paddle_signature(payload, SECRET, timestamp=issued_at))
        self.assertEqual(response.status_code, 400)

    def test_missing_secret_is_rejected(self) -> None:
        payload = _body("subscription.created")
        signature = build_paddle_signature(payload, SECRET)
        cases: dict[str, str | None] = {
            "unset": None,
            "empty": "",
            "placeholder": "your_paddle_webhook_secret",
            "replace_prefix": "replace-me",
        }
        for name, value in cases.items():
            with self.subTest(secret=name):
                if value is None:
                    os.environ.pop(SECRET_ENV, None)
                else:
                    os.environ[SECRET_ENV] = value
                response = self._post(payload, signature)
                self.assertEqual(response.status_code, 503)

    def test_oversized_body_is_rejected(self) -> None:
        payload = b"x" * ((64 * 1024) + 1)
        response = self._post(payload, None)
        self.assertEqual(response.status_code, 413)

    def test_signed_event_does_not_log_custom_data(self) -> None:
        payload = _body("subscription.created")
        signature = build_paddle_signature(payload, SECRET)
        response = self._post(payload, signature)
        self.assertEqual(response.status_code, 200)
        logged = "\n".join(self.capture.messages)
        self.assertIn("partner activation confirmed", logged)
        self.assertNotIn(CONTRACT, logged)
        self.assertNotIn(EMAIL, logged)
        self.assertNotIn(SECRET, logged)
        self.assertNotIn(signature, logged)

    def test_stripe_route_remains_registered(self) -> None:
        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/api/v1/webhooks/stripe", paths)
        self.assertIn("/api/v1/paddle/webhook", paths)


if __name__ == "__main__":
    unittest.main()
