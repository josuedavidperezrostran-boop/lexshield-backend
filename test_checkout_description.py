"""Stripe Checkout copy matches the public privacy notice (no Zero Data Retention claim)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from app import billing
from app.config import settings
from app.main import app


class CheckoutDescriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._key = settings.stripe_secret_key
        settings.stripe_secret_key = "sk_test_unit_only"
        self.client = TestClient(app)

    def tearDown(self) -> None:
        settings.stripe_secret_key = self._key

    def test_description_discloses_external_ai_and_drops_zdr(self) -> None:
        create = mock.Mock(return_value=SimpleNamespace(url="https://checkout.stripe.com/c/pay/cs_test_x"))
        # Stripe is mocked: no session, coupon or charge is created.
        with mock.patch.object(billing, "ensure_founder_promotion"), mock.patch.object(
            billing.stripe.checkout.Session, "create", create
        ):
            response = self.client.post("/billing/checkout", json={})

        self.assertEqual(response.status_code, 200)
        description = create.call_args.kwargs["line_items"][0]["price_data"]["product_data"]["description"]
        self.assertNotIn("Zero Data Retention", description)
        self.assertIn("OpenAI", description)


if __name__ == "__main__":
    unittest.main()
