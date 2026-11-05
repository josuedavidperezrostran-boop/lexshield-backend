"""Stripe-backed access, token stages and duplicate-checkout guards. Stripe is mocked throughout;
no request leaves the process and nothing is charged."""

from __future__ import annotations

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import stripe
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import billing, subscriptions
from app.auth import REFRESH_WINDOW_SECONDS, TOKEN_TTL_SECONDS, _sign, customer_id_from_token
from app.config import settings
from app.main import app, require_subscription

CUSTOMER = "cus_test_owner"
OTHER = "cus_test_other"
PRODUCT_META = {"product": "lexshield-professional"}
PAST_CACHE = subscriptions.SUBSCRIPTION_CACHE_MAX_SECONDS + 1


def _token(customer_id: str | None = CUSTOMER, age: int = 0) -> str:
    issued = str(int(time.time()) - age)
    payload = f"{issued}.{customer_id}" if customer_id else issued
    return f"{payload}.{_sign(payload)}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sub(
    status: str,
    customer: str = CUSTOMER,
    *,
    authorized: bool = True,
    cancel_at_period_end: bool = False,
    sid: str = "sub_test_1",
) -> dict:
    return {
        "id": sid,
        "status": status,
        "customer": customer,
        "cancel_at_period_end": cancel_at_period_end,
        "metadata": PRODUCT_META if authorized else {},
        "items": {"data": [{"price": {"id": "price_other", "product": "prod_other"}, "current_period_end": 1_900_000_000}]},
    }


def _list(*items: object) -> SimpleNamespace:
    return SimpleNamespace(data=list(items))


class _StripeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (
            settings.stripe_secret_key,
            settings.access_token_secret,
            settings.dev_bypass_billing,
            settings.environment,
            settings.subscription_cache_seconds,
            os.environ.get("RENDER"),
        )
        settings.stripe_secret_key = "sk_test_unit_only"
        settings.access_token_secret = "unit-test-secret"
        settings.dev_bypass_billing = False
        settings.environment = ""
        settings.subscription_cache_seconds = 300
        os.environ.pop("RENDER", None)
        subscriptions.clear_cache()
        self.client = TestClient(app)
        self.subscriptions_list = self._patch(stripe.Subscription, "list", return_value=_list())
        self.product_retrieve = self._patch(stripe.Product, "retrieve", return_value={"name": "Something else", "metadata": {}})
        self.create = self._patch(stripe.checkout.Session, "create", return_value=SimpleNamespace(url="https://checkout.stripe.com/c/pay/cs_test_new"))
        self.open_sessions = self._patch(stripe.checkout.Session, "list", return_value=_list())
        self.customers = self._patch(stripe.Customer, "list", return_value=_list())
        self._patch(billing, "ensure_founder_promotion")

    def _patch(self, target: object, name: str, **kwargs: object) -> mock.Mock:
        patcher = mock.patch.object(target, name, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def tearDown(self) -> None:
        key, secret, bypass, environment, cache, render = self._saved
        settings.stripe_secret_key = key
        settings.access_token_secret = secret
        settings.dev_bypass_billing = bypass
        settings.environment = environment
        settings.subscription_cache_seconds = cache
        if render is not None:
            os.environ["RENDER"] = render
        subscriptions.clear_cache()

    def subs(self, *items: dict) -> None:
        self.subscriptions_list.return_value = _list(*items)

    def later(self, seconds: int):
        """Move the cache clock forward without sleeping."""
        return mock.patch.object(subscriptions.time, "monotonic", return_value=time.monotonic() + seconds)

    def assert_status(self, token: str, expected: int | None) -> None:
        if expected is None:
            require_subscription(f"Bearer {token}")
            return
        with self.assertRaises(HTTPException) as caught:
            require_subscription(f"Bearer {token}")
        self.assertEqual(caught.exception.status_code, expected)


class AccessRuleTests(_StripeCase):
    def _require(self, status: str, **kwargs: object) -> None:
        self.subs(_sub(status, **kwargs))
        subscriptions.clear_cache()
        require_subscription(f"Bearer {_token()}")

    def test_active_and_trialing_have_access(self) -> None:
        for status in ("active", "trialing"):
            with self.subTest(status=status):
                self._require(status)

    def test_past_due_requires_payment_update(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._require("past_due")
        self.assertEqual(caught.exception.status_code, 402)

    def test_blocked_statuses(self) -> None:
        for status in ("canceled", "unpaid", "incomplete", "incomplete_expired", "paused"):
            with self.subTest(status=status), self.assertRaises(HTTPException) as caught:
                self._require(status)
            self.assertEqual(caught.exception.status_code, 403)

    def test_wrong_product_and_foreign_customer_do_not_count(self) -> None:
        self.subs(_sub("active", authorized=False), _sub("active", customer=OTHER))
        self.assert_status(_token(), 403)

    def test_configured_price_or_product_ids_count(self) -> None:
        self.subs(_sub("active", authorized=False))
        with mock.patch.object(settings, "stripe_authorized_price_ids", "price_other"):
            self.assert_status(_token(), None)
        subscriptions.clear_cache()
        with mock.patch.object(settings, "stripe_authorized_product_ids", "prod_other"):
            self.assert_status(_token(), None)

    def test_legacy_inline_product_is_recognized_by_name(self) -> None:
        self.product_retrieve.return_value = {"name": "LexShield Professional", "metadata": {}}
        self.subs(_sub("active", authorized=False))
        self.assert_status(_token(), None)

    def test_best_subscription_wins(self) -> None:
        self.subs(_sub("canceled", sid="sub_old"), _sub("active", sid="sub_new"))
        self.assertEqual(subscriptions.access_state(CUSTOMER).subscription_id, "sub_new")

    def test_analyze_past_due_is_rejected_before_any_work(self) -> None:
        self.subs(_sub("past_due"))
        response = self.client.post(
            "/analyze",
            files={"file": ("contract.txt", b"not a real contract", "text/plain")},
            headers=_auth(_token()),
        )
        self.assertEqual(response.status_code, 402)

    def test_status_advertises_access_model(self) -> None:
        self.assertEqual(self.client.get("/billing/status").json()["access_model"], "stripe-subscription-v1")

    def test_me_reports_state(self) -> None:
        self.subs(_sub("past_due"))
        body = self.client.get("/billing/me", headers=_auth(_token())).json()
        self.assertEqual(body["status"], "past_due")
        self.assertFalse(body["access"])
        self.assertTrue(body["payment_required"])
        self.assertTrue(body["has_subscription"])


class LifecycleTests(_StripeCase):
    def test_cancellation_at_period_end_keeps_access_then_stops(self) -> None:
        self.subs(_sub("active", cancel_at_period_end=True))
        self.assert_status(_token(), None)
        state = subscriptions.access_state(CUSTOMER)
        self.assertTrue(state.cancel_at_period_end)
        self.assertEqual(state.current_period_end, 1_900_000_000)
        self.subs(_sub("canceled"))
        with self.later(PAST_CACHE):
            self.assert_status(_token(), 403)

    def test_immediate_cancellation_stops_access_within_cache_bound(self) -> None:
        self.subs(_sub("active"))
        self.assert_status(_token(), None)
        self.subs(_sub("canceled"))
        with self.later(PAST_CACHE):
            self.assert_status(_token(), 403)

    def test_failed_payment_then_recovery(self) -> None:
        self.subs(_sub("past_due"))
        self.assert_status(_token(), 402)
        # The customer can still renew the token to reach the portal and fix the card.
        renewed = self.client.post("/billing/refresh", headers=_auth(_token(age=TOKEN_TTL_SECONDS + 60)))
        self.assertEqual(renewed.status_code, 200)
        self.assertTrue(renewed.json()["subscription"]["payment_required"])
        self.subs(_sub("active"))
        with self.later(PAST_CACHE):
            self.assert_status(renewed.json()["access_token"], None)
        self.create.assert_not_called()


class CacheAndOutageTests(_StripeCase):
    def test_cache_reuses_lookup_within_ttl(self) -> None:
        self.subs(_sub("active"))
        subscriptions.access_state(CUSTOMER)
        subscriptions.access_state(CUSTOMER)
        self.assertEqual(self.subscriptions_list.call_count, 1)
        subscriptions.access_state(CUSTOMER, fresh=True)
        self.assertEqual(self.subscriptions_list.call_count, 2)

    def test_cache_ttl_is_capped_at_ten_minutes(self) -> None:
        settings.subscription_cache_seconds = 86_400
        self.assertEqual(subscriptions.cache_seconds(), 600)
        settings.subscription_cache_seconds = 0
        self.subs(_sub("active"))
        subscriptions.access_state(CUSTOMER)
        subscriptions.access_state(CUSTOMER)
        self.assertEqual(self.subscriptions_list.call_count, 2)

    def test_outage_inside_cache_window_keeps_last_answer(self) -> None:
        self.subs(_sub("active"))
        self.assert_status(_token(), None)
        self.subscriptions_list.side_effect = stripe.APIConnectionError("down")
        self.assert_status(_token(), None)

    def test_outage_with_expired_cache_is_temporary_not_a_cancellation(self) -> None:
        self.subs(_sub("active"))
        self.assert_status(_token(), None)
        self.subscriptions_list.side_effect = stripe.APIConnectionError("down")
        with self.later(PAST_CACHE):
            self.assert_status(_token(), 503)
            # Still unknown on retry: the positive answer is not extended.
            self.assert_status(_token(), 503)
            # Checkout must not be offered as if the subscription ended.
            self.assertEqual(self.client.post("/billing/checkout", headers=_auth(_token())).status_code, 503)
        self.create.assert_not_called()

    def test_outage_recovers_on_retry(self) -> None:
        self.subscriptions_list.side_effect = stripe.APIConnectionError("down")
        self.assert_status(_token(), 503)
        self.subscriptions_list.side_effect = None
        self.subs(_sub("active"))
        self.assert_status(_token(), None)

    def test_stripe_not_configured_never_grants_access(self) -> None:
        settings.stripe_secret_key = ""
        self.assert_status(_token(), 503)


class VerifyTests(_StripeCase):
    def _session(self, **overrides: object) -> dict:
        session = {
            "id": "cs_test_1",
            "mode": "subscription",
            "status": "complete",
            "metadata": PRODUCT_META,
            "created": int(time.time()) - 60,
            "customer": CUSTOMER,
            "subscription": "sub_test_1",
        }
        session.update(overrides)
        return session

    def _verify(self, session: dict, subscription: dict | None = None) -> object:
        sub = subscription or _sub("active")
        with mock.patch.object(stripe.checkout.Session, "retrieve", return_value=session), mock.patch.object(
            stripe.Subscription, "retrieve", return_value=sub
        ):
            return self.client.post("/billing/verify", json={"session_id": session["id"]})

    def test_fresh_session_for_active_subscription_issues_token(self) -> None:
        self.subs(_sub("active"))
        response = self._verify(self._session())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(customer_id_from_token(body["access_token"]), CUSTOMER)
        self.assertTrue(body["subscription"]["access"])

    def test_old_session_of_canceled_subscription_cannot_sign_in(self) -> None:
        self.subs(_sub("canceled"))
        old = self._session(created=int(time.time()) - 2 * 86_400)
        self.assertEqual(self._verify(old, _sub("canceled")).status_code, 410)

    def test_fresh_session_of_canceled_subscription_gives_no_access(self) -> None:
        self.subs(_sub("canceled"))
        response = self._verify(self._session(), _sub("canceled"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["subscription"]["access"])
        self.assert_status(response.json()["access_token"], 403)

    def test_rejects_wrong_product_foreign_customer_and_unpaid_sessions(self) -> None:
        cases = {
            "other product": (self._session(metadata={"product": "something"}), _sub("active"), 403),
            "payment mode": (self._session(mode="payment"), _sub("active"), 403),
            "not complete": (self._session(status="open"), _sub("active"), 402),
            "subscription of another customer": (self._session(), _sub("active", customer=OTHER), 403),
            "unauthorized subscription": (self._session(), _sub("active", authorized=False), 403),
        }
        for name, (session, sub, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self._verify(session, sub).status_code, expected)


class TokenStageTests(_StripeCase):
    def test_renewal_credential_renews_without_charge(self) -> None:
        self.subs(_sub("active"))
        response = self.client.post("/billing/refresh", headers=_auth(_token(age=TOKEN_TTL_SECONDS + 60)))
        self.assertEqual(response.status_code, 200)
        renewed = response.json()["access_token"]
        self.assertEqual(customer_id_from_token(renewed), CUSTOMER)
        self.assert_status(renewed, None)
        self.create.assert_not_called()
        self.open_sessions.assert_not_called()

    def test_renewal_credential_is_not_a_general_authorization(self) -> None:
        self.subs(_sub("active"))
        expired = _token(age=TOKEN_TTL_SECONDS + 60)
        self.assert_status(expired, 401)
        self.assertEqual(self.client.get("/billing/me", headers=_auth(expired)).status_code, 401)
        self.assertEqual(self.client.post("/billing/portal", headers=_auth(expired)).status_code, 401)

    def test_renewal_rechecks_stripe_and_does_not_extend_access(self) -> None:
        self.subs(_sub("active"))
        subscriptions.access_state(CUSTOMER)  # warm cache with the old answer
        for status in ("canceled", "incomplete_expired"):
            with self.subTest(status=status):
                self.subs(_sub(status))
                response = self.client.post("/billing/refresh", headers=_auth(_token()))
                self.assertEqual(response.status_code, 403)

    def test_renewed_token_for_unpaid_customer_still_has_no_access(self) -> None:
        self.subs(_sub("unpaid"))
        renewed = self.client.post("/billing/refresh", headers=_auth(_token())).json()["access_token"]
        self.assert_status(renewed, 403)

    def test_expired_forged_and_customerless_credentials_are_refused(self) -> None:
        self.subs(_sub("active"))
        for token in (
            _token(age=REFRESH_WINDOW_SECONDS + 60),
            _token(customer_id=None),
            _token()[:-2] + "00",
            CUSTOMER,
            "",
        ):
            with self.subTest(token=token[:20]):
                self.assertEqual(self.client.post("/billing/refresh", headers=_auth(token)).status_code, 401)

    def test_reused_renewal_credential_only_yields_tokens_for_the_same_customer(self) -> None:
        # Tokens are stateless: an old credential keeps working until its own window ends
        # (documented limitation), but it can never change customer or skip the Stripe check.
        self.subs(_sub("active"))
        old = _token(age=TOKEN_TTL_SECONDS + 60)
        first = self.client.post("/billing/refresh", headers=_auth(old)).json()["access_token"]
        second = self.client.post("/billing/refresh", headers=_auth(old)).json()["access_token"]
        self.assertEqual({customer_id_from_token(first), customer_id_from_token(second)}, {CUSTOMER})
        self.assertEqual(self.subscriptions_list.call_count, 2)

    def test_tokens_signed_with_another_secret_are_refused(self) -> None:
        self.subs(_sub("active"))
        token = _token()
        settings.access_token_secret = "rotated-secret"
        self.assertEqual(self.client.post("/billing/refresh", headers=_auth(token)).status_code, 401)
        self.assert_status(token, 401)


class CheckoutTests(_StripeCase):
    def _checkout(self, token: str | None = None, json: dict | None = None) -> object:
        return self.client.post("/billing/checkout", json=json or {}, headers=_auth(token) if token else {})

    def test_known_customer_with_subscription_to_use_or_fix_is_not_sold_another(self) -> None:
        for status in ("active", "trialing", "past_due", "unpaid", "incomplete", "paused"):
            with self.subTest(status=status):
                self.subs(_sub(status))
                self.assertEqual(self._checkout(_token()).status_code, 409)
        self.create.assert_not_called()

    def test_renewal_credential_identifies_customer_for_checkout(self) -> None:
        self.subs(_sub("active"))
        self.assertEqual(self._checkout(_token(age=TOKEN_TTL_SECONDS + 60)).status_code, 409)
        self.create.assert_not_called()

    def test_known_customer_without_subscription_reuses_the_stripe_customer(self) -> None:
        self.subs(_sub("canceled"))
        self.assertEqual(self._checkout(_token()).status_code, 200)
        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs["customer"], CUSTOMER)
        self.assertEqual(kwargs["subscription_data"], {"metadata": PRODUCT_META})
        self.assertTrue(kwargs["idempotency_key"])

    def test_open_checkout_session_is_reused(self) -> None:
        self.open_sessions.return_value = _list({"metadata": PRODUCT_META, "url": "https://checkout.stripe.com/c/pay/cs_test_open"})
        response = self._checkout(_token())
        self.assertEqual(response.json()["checkout_url"], "https://checkout.stripe.com/c/pay/cs_test_open")
        self.create.assert_not_called()

    def test_browser_supplied_ids_and_emails_are_ignored(self) -> None:
        response = self._checkout(json={"customer_id": OTHER, "subscription_id": "sub_x", "email": "victim@example.com"})
        self.assertEqual(response.status_code, 200)
        kwargs = self.create.call_args.kwargs
        self.assertNotIn("customer", kwargs)
        self.assertNotIn("customer_email", kwargs)
        self.customers.assert_not_called()
        self.subscriptions_list.assert_not_called()

    def test_anonymous_checkout_reveals_nothing_about_emails(self) -> None:
        self.customers.return_value = _list({"id": CUSTOMER})
        self.subs(_sub("active"))
        responses = {self._checkout(json={"email": e}).status_code for e in ("owner@example.com", "nobody@example.com")}
        self.assertEqual(responses, {200})
        self.customers.assert_not_called()

    def test_repeated_requests_share_an_idempotency_key(self) -> None:
        self.subs(_sub("canceled"))
        self._checkout(_token())
        self._checkout(_token())
        self.assertEqual(len({c.kwargs["idempotency_key"] for c in self.create.call_args_list}), 1)

    def test_simultaneous_requests_are_serialized_per_customer(self) -> None:
        active = 0
        peak = 0
        guard = threading.Lock()

        def slow_lookup(*args: object, **kwargs: object) -> SimpleNamespace:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return _list(_sub("canceled"))

        self.subscriptions_list.side_effect = slow_lookup
        threads = [threading.Thread(target=self._checkout, args=(_token(),)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak, 1)
        self.assertEqual(len({c.kwargs["idempotency_key"] for c in self.create.call_args_list}), 1)

    def test_stripe_lookup_failure_does_not_create_checkout(self) -> None:
        self.subscriptions_list.side_effect = stripe.APIConnectionError("down")
        self.assertEqual(self._checkout(_token()).status_code, 503)
        self.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
