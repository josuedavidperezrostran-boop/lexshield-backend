"""Signature, entitlement, and log checks for the Stripe webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.partners import PREMIUM_PARTNER_TIER, partner_tier
from app.webhooks import logger

SECRET = "whsec_test_secret_value_123"
CONTRACT = "CONFIDENTIAL CLAUSE the party shall indemnify without cap"
EMAIL = "counsel@example.com"


def _sign(payload: bytes, secret: str = SECRET) -> str:
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _event(event_type: str = "checkout.session.completed") -> dict:
    return {
        "id": "evt_test_123",
        "object": "event",
        "type": event_type,
        "livemode": False,
        "created": 1760000000,
        "data": {
            "object": {
                "id": "cs_test_session123",
                "object": "checkout.session",
                "mode": "subscription",
                "payment_status": "paid",
                "status": "complete",
                "customer": "cus_test_customer123",
                "subscription": "sub_test_sub123",
                "payment_intent": "pi_test_intent123",
                "amount_total": 10000,
                "currency": "usd",
                "metadata": {
                    "product": "lexshield-professional",
                    "contract_text": CONTRACT,
                },
                "customer_details": {"email": EMAIL, "name": CONTRACT},
            }
        },
    }


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class StripeWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._secret = settings.stripe_webhook_secret
        self._db = settings.partners_db_path
        self._tmpdir = tempfile.TemporaryDirectory()
        settings.stripe_webhook_secret = SECRET
        settings.partners_db_path = str(Path(self._tmpdir.name) / "partners.sqlite")
        self.capture = _Capture()
        logger.addHandler(self.capture)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        logger.removeHandler(self.capture)
        settings.stripe_webhook_secret = self._secret
        settings.partners_db_path = self._db
        self._tmpdir.cleanup()

    def _post(self, payload: bytes, signature: str | None) -> object:
        headers = {}
        if signature is not None:
            headers["stripe-signature"] = signature
        return self.client.post("/api/v1/webhooks/stripe", content=payload, headers=headers)

    def test_paid_checkout_provisions_tier_without_logging_contract_text(self) -> None:
        payload = json.dumps(_event()).encode()
        response = self._post(payload, _sign(payload))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"received": True})
        self.assertEqual(partner_tier("cus_test_customer123"), PREMIUM_PARTNER_TIER)

        logged = "\n".join(self.capture.messages)
        self.assertIn("cs_test_session123", logged)
        self.assertIn("cus_test_customer123", logged)
        self.assertIn("sub_test_sub123", logged)
        self.assertIn("pi_test_intent123", logged)
        self.assertIn("evt_test_123", logged)
        self.assertIn("lexshield-professional", logged)
        self.assertIn(PREMIUM_PARTNER_TIER, logged)
        self.assertNotIn(CONTRACT, logged)
        self.assertNotIn(EMAIL, logged)
        self.assertNotIn("contract_text", logged)

    def test_bad_signature_does_not_provision(self) -> None:
        payload = json.dumps(_event()).encode()
        response = self._post(payload, _sign(payload, "whsec_wrong_secret_value"))
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(partner_tier("cus_test_customer123"))
        logged = "\n".join(self.capture.messages)
        self.assertNotIn(CONTRACT, logged)

    def test_other_events_are_acknowledged_without_provisioning(self) -> None:
        payload = json.dumps(_event("invoice.paid")).encode()
        response = self._post(payload, _sign(payload))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(partner_tier("cus_test_customer123"))

    def test_missing_secret_is_rejected(self) -> None:
        settings.stripe_webhook_secret = ""
        payload = json.dumps(_event()).encode()
        response = self._post(payload, _sign(payload))
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
