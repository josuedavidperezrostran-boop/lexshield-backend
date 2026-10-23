"""Checkout uses a price configured in Stripe beforehand, kept apart per mode.

The product name and description (including the external-AI disclosure that replaced the old
Zero Data Retention claim) now live on that Stripe product, not in this code.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

import stripe
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.store import MemoryStore, use_store

FIELDS = (
    "stripe_secret_key",
    "stripe_mode",
    "stripe_price_id_test",
    "stripe_price_id_live",
    "stripe_portal_configuration_id_test",
    "stripe_portal_configuration_id_live",
    "environment",
    "access_token_secret",
)


class FixedPriceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: getattr(settings, name) for name in FIELDS}
        self._render = os.environ.pop("RENDER", None)
        settings.stripe_secret_key = "sk_test_unit_only"
        settings.stripe_mode = ""
        settings.stripe_price_id_test = "price_test_lexshield"
        settings.stripe_price_id_live = "price_live_lexshield"
        settings.stripe_portal_configuration_id_test = ""
        settings.stripe_portal_configuration_id_live = ""
        settings.environment = ""
        settings.access_token_secret = "unit-test-secret"
        use_store(MemoryStore())
        self.client = TestClient(app)
        self.create = mock.Mock(return_value=SimpleNamespace(id="cs_test_x", url="https://checkout.stripe.com/c/pay/cs_test_x"))
        # Anything that would create Stripe objects other than the Checkout session itself.
        self.forbidden = [
            mock.patch.object(stripe.Product, "create"),
            mock.patch.object(stripe.Price, "create"),
            mock.patch.object(stripe.Coupon, "create"),
            mock.patch.object(stripe.PromotionCode, "create"),
            mock.patch.object(stripe.billing_portal.Configuration, "create"),
        ]
        self.forbidden_mocks = [p.start() for p in self.forbidden]
        self._create_patch = mock.patch.object(stripe.checkout.Session, "create", self.create)
        self._create_patch.start()
        # A known customer with no subscription and no open session.
        for target, name in ((stripe.Subscription, "list"), (stripe.checkout.Session, "list")):
            patcher = mock.patch.object(target, name, return_value=SimpleNamespace(data=[]))
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._create_patch.stop()
        for patcher in self.forbidden:
            patcher.stop()
        for name, value in self._saved.items():
            setattr(settings, name, value)
        if self._render is not None:
            os.environ["RENDER"] = self._render
        use_store(None)

    def checkout(self) -> object:
        from app.auth import issue_access_token

        token = issue_access_token("cus_test_owner")
        return self.client.post("/billing/checkout", json={}, headers={"Authorization": f"Bearer {token}"})

    def test_checkout_uses_the_configured_price_only(self) -> None:
        self.assertEqual(self.checkout().status_code, 200)
        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs["line_items"], [{"price": "price_test_lexshield", "quantity": 1}])
        self.assertNotIn("price_data", str(kwargs))
        for created in self.forbidden_mocks:
            created.assert_not_called()

    def test_missing_price_refuses_checkout(self) -> None:
        settings.stripe_price_id_test = ""
        self.assertEqual(self.checkout().status_code, 503)
        self.create.assert_not_called()

    def test_live_key_is_refused_outside_production(self) -> None:
        settings.stripe_secret_key = "sk_live_unit_only"
        self.assertIsNone(settings.active_stripe_mode())
        self.assertEqual(self.checkout().status_code, 503)
        self.create.assert_not_called()

    def test_declared_mode_must_match_the_key(self) -> None:
        settings.stripe_mode = "live"
        self.assertIsNone(settings.active_stripe_mode())
        self.assertEqual(self.checkout().status_code, 503)

    def test_production_must_declare_its_mode(self) -> None:
        settings.environment = "production"
        settings.stripe_secret_key = "sk_live_unit_only"
        self.assertIsNone(settings.active_stripe_mode())
        settings.stripe_mode = "live"
        self.assertEqual(settings.active_stripe_mode(), "live")
        self.assertEqual(settings.stripe_price_id(), "price_live_lexshield")
        self.assertEqual(self.checkout().status_code, 200)
        self.assertEqual(self.create.call_args.kwargs["line_items"][0]["price"], "price_live_lexshield")

    def test_sandbox_price_is_never_used_in_live_mode(self) -> None:
        self.assertEqual(settings.stripe_price_id(), "price_test_lexshield")
        settings.environment = "production"
        settings.stripe_mode = "live"
        settings.stripe_secret_key = "sk_live_unit_only"
        settings.stripe_price_id_live = ""
        self.assertIsNone(settings.stripe_price_id())
        self.assertEqual(self.checkout().status_code, 503)

    def test_portal_uses_configured_id_or_account_default_without_creating_one(self) -> None:
        from app.auth import issue_access_token

        token = issue_access_token("cus_test_owner")
        portal = mock.Mock(return_value=SimpleNamespace(url="https://billing.stripe.com/p/session/test_x"))
        with mock.patch.object(stripe.billing_portal.Session, "create", portal):
            self.client.post("/billing/portal", headers={"Authorization": f"Bearer {token}"})
            self.assertNotIn("configuration", portal.call_args.kwargs)
            settings.stripe_portal_configuration_id_test = "bpc_test_lexshield"
            self.client.post("/billing/portal", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(portal.call_args.kwargs["configuration"], "bpc_test_lexshield")
        for created in self.forbidden_mocks:
            created.assert_not_called()

    def test_configured_price_authorizes_subscriptions(self) -> None:
        from app import subscriptions

        sub = {"id": "sub_1", "status": "active", "customer": "cus_x", "metadata": {},
               "items": {"data": [{"price": {"id": "price_test_lexshield", "product": "prod_x"}}]}}
        with mock.patch.object(stripe.Product, "retrieve", return_value={"name": "Other", "metadata": {}}):
            self.assertTrue(subscriptions.subscription_is_authorized(sub))
            settings.stripe_price_id_test = "price_test_other"
            subscriptions.clear_cache()
            self.assertFalse(subscriptions.subscription_is_authorized(sub))


if __name__ == "__main__":
    unittest.main()
