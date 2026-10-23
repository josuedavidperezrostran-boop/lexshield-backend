"""Email-verified checkout and what production refuses. Stripe, store and mail are simulated."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock

import stripe

from app import signup
from app.config import settings
from app.mail import MemoryMailer, use_mailer
from app.store import MemoryStore, SqliteStore, use_store
from test_subscription_access import _auth, _list, _StripeCase, _sub


class _SignupCase(_StripeCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = MemoryStore()
        self.mailer = MemoryMailer()
        use_store(self.store)
        use_mailer(self.mailer)
        self.addCleanup(use_mailer, None)
        # A tiny Stripe: customers by email, subscriptions by customer.
        self.by_email: dict[str, list[str]] = {}
        self.sub_status: dict[str, str] = {}
        self.created_customers: list[str] = []
        guard = threading.Lock()

        def list_customers(email=None, **_):  # type: ignore[no-untyped-def]
            return _list(*({"id": c} for c in self.by_email.get(email, [])))

        def create_customer(email=None, idempotency_key=None, **_):  # type: ignore[no-untyped-def]
            with guard:
                time.sleep(0.05)
                for cid in self.by_email.get(email, []):
                    return {"id": cid}  # what Stripe's idempotency would replay
                cid = f"cus_test_{len(self.created_customers) + 1}"
                self.created_customers.append(cid)
                self.by_email.setdefault(email, []).append(cid)
                return {"id": cid}

        def list_subs(customer=None, **_):  # type: ignore[no-untyped-def]
            status = self.sub_status.get(customer)
            return _list(_sub(status, customer=customer)) if status else _list()

        self.customers.side_effect = list_customers
        self.subscriptions_list.side_effect = list_subs
        self._patch(stripe.Customer, "create", side_effect=create_customer)
        self.create.side_effect = lambda **kw: SimpleNamespace(id="cs_test_1", url="https://checkout.stripe.com/c/pay/cs_test_1")
        self._patch(
            stripe.checkout.Session,
            "retrieve",
            return_value=SimpleNamespace(id="cs_test_1", status="open", url="https://checkout.stripe.com/c/pay/cs_test_1"),
        )

    def start(self, email: str, ip: str = "203.0.113.7") -> object:
        with mock.patch.object(signup, "_client_ip", return_value=ip):
            return self.client.post("/billing/checkout/start", json={"email": email})

    def confirm(self, code: str) -> object:
        with mock.patch.object(signup, "_client_ip", return_value="203.0.113.7"):
            return self.client.post("/billing/checkout/confirm", json={"code": code})

    def link(self, index: int = -1) -> str:
        return self.mailer.sent[index].text.split("#code=", 1)[0].rsplit("\n", 1)[-1]

    def code(self, index: int = -1) -> str:
        return self.mailer.sent[index].text.split("#code=", 1)[1].split()[0]


class EmailCheckoutTests(_SignupCase):
    def test_same_answer_for_new_and_existing_emails(self) -> None:
        self.by_email["owner@example.com"] = ["cus_test_owner"]
        self.sub_status["cus_test_owner"] = "active"
        new, existing = self.start("new@example.com"), self.start("owner@example.com")
        self.assertEqual((new.status_code, new.json()), (existing.status_code, existing.json()))
        self.assertEqual(new.status_code, 202)
        # The subscriber gets a sign-in link, never a way to pay again.
        self.assertTrue(self.link(0).endswith("/subscribe"))
        self.assertTrue(self.link(1).endswith("/recover"))

    def test_verified_email_gets_one_customer_a_session_and_checkout(self) -> None:
        self.start("new@example.com")
        response = self.confirm(self.code())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["checkout_url"], "https://checkout.stripe.com/c/pay/cs_test_1")
        self.assertEqual(self.created_customers, ["cus_test_1"])
        self.assertEqual(self.create.call_args.kwargs["customer"], "cus_test_1")
        self.assertEqual(self.client.get("/billing/me", headers=_auth(body["access_token"])).status_code, 200)
        self.assertEqual(self.confirm(self.code()).status_code, 400)  # single use

    def test_existing_customer_without_subscription_is_reused(self) -> None:
        self.by_email["back@example.com"] = ["cus_test_returning"]
        self.sub_status["cus_test_returning"] = "canceled"
        self.start("back@example.com")
        self.confirm(self.code())
        self.assertEqual(self.created_customers, [])
        self.assertEqual(self.create.call_args.kwargs["customer"], "cus_test_returning")

    def test_subscription_that_appears_before_confirmation_signs_in_without_checkout(self) -> None:
        self.start("new@example.com")
        code = self.code()
        self.by_email["new@example.com"] = ["cus_test_paid_elsewhere"]
        self.sub_status["cus_test_paid_elsewhere"] = "active"
        body = self.confirm(code).json()
        self.assertIsNone(body["checkout_url"])
        self.create.assert_not_called()

    def test_two_links_for_one_email_confirmed_at_once(self) -> None:
        self.start("race@example.com")
        self.start("race@example.com")
        codes = [self.code(0), self.code(1)]
        results: list[dict] = []
        threads = [threading.Thread(target=lambda c=c: results.append(self.confirm(c).json())) for c in codes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.created_customers), 1)
        self.assertEqual({r["checkout_url"] for r in results}, {"https://checkout.stripe.com/c/pay/cs_test_1"})
        self.assertEqual(self.create.call_count, 1)

    def test_expired_or_unknown_codes(self) -> None:
        self.start("new@example.com")
        code = self.code()
        with mock.patch("time.time", return_value=time.time() + signup.CODE_TTL_SECONDS + 1):
            self.assertEqual(self.confirm(code).status_code, 400)
        self.assertEqual(self.confirm("B" * 43).status_code, 400)
        self.create.assert_not_called()

    def test_limits(self) -> None:
        for i in range(signup.RECOVER_PER_EMAIL_PER_HOUR + 2):
            self.start("flood@example.com", ip=f"198.51.100.{i}")
        self.assertEqual(len(self.mailer.sent), signup.RECOVER_PER_EMAIL_PER_HOUR)
        codes = [self.start(f"u{i}@example.com", ip="192.0.2.50").status_code for i in range(signup.RECOVER_PER_IP_PER_HOUR + 1)]
        self.assertEqual(codes[-1], 429)


class ProductionRefusalTests(_SignupCase):
    """What a production deployment refuses, and that it then offers nothing it cannot do."""

    def setUp(self) -> None:
        super().setUp()
        use_mailer(None)
        use_store(None)
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        for name, value in (
            ("environment", "production"),
            ("stripe_secret_key", "sk_live_unit_only"),
            ("stripe_mode", "live"),
            ("stripe_price_id_live", "price_live_lexshield"),
            ("access_token_secret", "a-private-production-secret"),
            ("shared_store_url", f"sqlite:///{os.path.join(self._dir.name, 'store.db')}"),
            ("mail_backend", ""),
        ):
            self._patch(settings, name, new=value)

    def status(self) -> dict:
        return self.client.get("/billing/status").json()

    def assert_email_flows_refused(self) -> None:
        self.assertFalse(self.status()["recovery_available"])
        self.assertFalse(self.status()["new_checkout_available"])
        self.assertEqual(self.client.post("/billing/recover", json={"email": "a@example.com"}).status_code, 503)
        self.assertEqual(self.client.post("/billing/checkout/start", json={"email": "a@example.com"}).status_code, 503)

    def test_no_mail_provider_means_no_email_flows(self) -> None:
        self.assert_email_flows_refused()

    def test_development_mailers_are_refused(self) -> None:
        settings.mail_backend = f"directory:{self._dir.name}"
        self.assert_email_flows_refused()
        self.assertEqual(os.listdir(self._dir.name), ["store.db"])  # nothing written as "sent"

    def test_memory_or_missing_store_is_refused(self) -> None:
        for url in ("memory://", ""):
            with self.subTest(url=url):
                settings.shared_store_url = url
                self.assert_email_flows_refused()

    def test_sessions_fail_closed_without_a_store(self) -> None:
        from app.auth import issue_access_token

        settings.shared_store_url = "memory://"
        token = issue_access_token("cus_test_owner")
        self.assertEqual(self.client.get("/billing/me", headers=_auth(token)).status_code, 503)

    def test_flags_turn_on_only_when_everything_is_there(self) -> None:
        use_mailer(MemoryMailer())  # stands in for a real provider adapter in this test only
        self.assertTrue(self.status()["recovery_available"])
        self.assertTrue(self.status()["new_checkout_available"])
        settings.stripe_price_id_live = ""
        self.assertTrue(self.status()["recovery_available"])
        self.assertFalse(self.status()["new_checkout_available"])
        settings.access_token_secret = "change-me-in-production"
        self.assertFalse(self.status()["recovery_available"])


class CheckoutEdgeTests(_StripeCase):
    """Limits of the coordination: slow requests outliving the lock, and expired attempts."""

    def setUp(self) -> None:
        super().setUp()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        from test_checkout_coordination import FakeStripe

        self.fake = FakeStripe(os.path.join(self._dir.name, "fake.db"))
        use_store(SqliteStore(os.path.join(self._dir.name, "store.db")))
        self.create.side_effect = self.fake.create
        self._patch(stripe.checkout.Session, "retrieve", side_effect=self.fake.retrieve)
        self.open_sessions.side_effect = self.fake.list
        from app.auth import issue_access_token

        self.token = issue_access_token("cus_test_owner")

    def checkout(self) -> str:
        from app import billing

        return billing.create_checkout_session(authorization=f"Bearer {self.token}").checkout_url

    def test_lock_expiring_while_the_first_request_is_still_running(self) -> None:
        from app import billing

        slow_create = self.fake.create

        def slower(**kwargs):  # type: ignore[no-untyped-def]
            time.sleep(0.8)
            return slow_create(**kwargs)

        self.create.side_effect = slower
        urls: list[str] = []
        with mock.patch.object(billing, "CHECKOUT_LOCK_SECONDS", 0.2):
            first = threading.Thread(target=lambda: urls.append(self.checkout()))
            first.start()
            time.sleep(0.4)  # the first request's lock has expired; it is still inside Stripe
            second = threading.Thread(target=lambda: urls.append(self.checkout()))
            second.start()
            first.join()
            second.join()
        self.assertEqual(len(set(urls)), 1, urls)
        self.assertEqual(self.fake.count(), 1)

    def test_attempt_whose_session_expired_starts_fresh_instead_of_replaying(self) -> None:
        from app.store import get_store

        first = self.checkout()
        # As if the process had crashed before recording the session: only the attempt remains.
        get_store().delete("checkout:session:cus_test_owner")
        # After the session expired (60 min) but before the attempt key does (65 min).
        later = time.time() + 3600 + 120
        with mock.patch("time.time", return_value=later):
            second = self.checkout()
            self.assertEqual(self.fake.retrieve(second.rsplit("/", 1)[1]).status, "open")
        self.assertNotEqual(first, second)
        self.assertEqual(self.fake.count(), 2)


if __name__ == "__main__":
    import unittest

    unittest.main()
