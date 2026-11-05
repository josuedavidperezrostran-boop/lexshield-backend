"""Absolute session limit, revocation and email recovery. Stripe, store and mail are simulated."""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from unittest import mock

import stripe

from app import recovery
from app.auth import SESSION_MAX_SECONDS, TOKEN_TTL_SECONDS, _sign, issue_access_token, parse_token
from app.mail import MemoryMailer, use_mailer
from app.store import MemoryStore, StoreUnavailable, use_store
from test_subscription_access import CUSTOMER, OTHER, _auth, _list, _StripeCase, _sub

DAY = 24 * 60 * 60


def _v2(customer: str = CUSTOMER, *, issued_ago: float = 0, auth_ago: float = 0, sid: str = "sid_test") -> str:
    now = time.time()
    payload = f"v2.{int(now - issued_ago)}.{int((now - auth_ago) * 1000)}.{sid}.{customer}"
    return f"{payload}.{_sign(payload)}"


class _Case(_StripeCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = MemoryStore()
        self.mailer = MemoryMailer()
        use_store(self.store)
        use_mailer(self.mailer)
        self.addCleanup(use_store, None)
        self.addCleanup(use_mailer, None)
        self.subs(_sub("active"))

    def me(self, token: str) -> int:
        return self.client.get("/billing/me", headers=_auth(token)).status_code

    def refresh(self, token: str) -> object:
        return self.client.post("/billing/refresh", headers=_auth(token))


class SessionLimitTests(_Case):
    def test_renewal_keeps_session_start_and_id(self) -> None:
        token = _v2(issued_ago=8 * DAY, auth_ago=40 * DAY)
        renewed = parse_token(self.refresh(token).json()["access_token"])
        original = parse_token(token)
        self.assertEqual(renewed.session_id, original.session_id)
        self.assertAlmostEqual(renewed.auth_at, original.auth_at, delta=0.01)

    def test_chained_renewals_stop_at_the_session_limit(self) -> None:
        token = issue_access_token(CUSTOMER)
        start = time.time()
        renewals = 0
        for step in range(1, 10):
            # Renew every 20 days, well inside every per-token window.
            with mock.patch("time.time", return_value=start + step * 20 * DAY):
                response = self.refresh(token)
                if response.status_code != 200:
                    self.assertEqual(response.status_code, 401)
                    self.assertGreaterEqual(step * 20 * DAY, SESSION_MAX_SECONDS)
                    break
                token = response.json()["access_token"]
                renewals += 1
        else:
            self.fail("renewals never stopped")
        self.assertEqual(renewals, SESSION_MAX_SECONDS // (20 * DAY))

    def test_current_token_past_session_limit_is_refused(self) -> None:
        token = _v2(issued_ago=1 * DAY, auth_ago=SESSION_MAX_SECONDS + DAY)
        self.assertEqual(self.me(token), 401)
        self.assertEqual(self.refresh(token).status_code, 401)

    def test_pre_session_tokens_count_the_limit_from_issue(self) -> None:
        legacy_payload = f"{int(time.time()) - 50 * DAY}.{CUSTOMER}"
        legacy = f"{legacy_payload}.{_sign(legacy_payload)}"
        renewed = self.refresh(legacy).json()["access_token"]
        with mock.patch("time.time", return_value=time.time() + 41 * DAY):
            self.assertEqual(self.refresh(renewed).status_code, 401)

    def test_session_start_cannot_be_moved_forward(self) -> None:
        # A token claiming a session that starts after its own issue time is forged or broken.
        self.assertEqual(self.me(_v2(issued_ago=5 * DAY, auth_ago=1 * DAY)), 401)

    def test_edited_session_fields_fail_the_signature(self) -> None:
        token = _v2(auth_ago=80 * DAY)
        parts = token.split(".")
        parts[2] = str(int(time.time() * 1000))
        self.assertEqual(self.me(".".join(parts)), 401)


class RevocationTests(_Case):
    def test_logout_revokes_only_that_session(self) -> None:
        mine, other_device = _v2(sid="sid_a"), _v2(sid="sid_b")
        self.assertEqual(self.client.post("/billing/logout", headers=_auth(mine)).status_code, 200)
        self.assertEqual(self.me(mine), 401)
        self.assertEqual(self.refresh(mine).status_code, 401)
        # A revoked token no longer identifies anyone, so it cannot open Checkout either.
        self.assertEqual(self.client.post("/billing/checkout", headers=_auth(mine)).status_code, 401)
        self.create.assert_not_called()
        self.assertEqual(self.me(other_device), 200)

    def test_logout_reaches_renewed_tokens_of_the_session(self) -> None:
        old = _v2(issued_ago=8 * DAY, auth_ago=8 * DAY, sid="sid_a")
        renewed = self.refresh(old).json()["access_token"]
        self.client.post("/billing/logout", headers=_auth(old))
        self.assertEqual(self.me(renewed), 401)

    def test_revoke_all_signs_out_every_session_and_legacy_token(self) -> None:
        legacy_payload = f"{int(time.time()) - DAY}.{CUSTOMER}"
        legacy = f"{legacy_payload}.{_sign(legacy_payload)}"
        a, b = _v2(sid="sid_a", auth_ago=10), _v2(sid="sid_b", auth_ago=20)
        other_customer = _v2(customer=OTHER, sid="sid_c", auth_ago=10)
        self.subscriptions_list.side_effect = lambda customer=None, **_: _list(_sub("active", customer=customer))
        self.assertEqual(self.client.post("/billing/sessions/revoke-all", headers=_auth(a)).status_code, 200)
        for token in (a, b, legacy):
            self.assertEqual(self.me(token), 401)
        self.assertEqual(self.me(other_customer), 200)
        # A session started after the revocation works.
        self.assertEqual(self.me(issue_access_token(CUSTOMER)), 200)

    def test_store_outage_fails_closed(self) -> None:
        token = _v2()
        with mock.patch.object(self.store, "get", side_effect=StoreUnavailable("down")):
            # Temporary, never 401: a 401 would make the browser discard a good token.
            self.assertEqual(self.me(token), 503)
            self.assertEqual(self.refresh(token).status_code, 503)


class RecoveryTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.customers.side_effect = lambda email=None, **_: _list({"id": CUSTOMER}) if email == "owner@example.com" else _list()

    def recover(self, email: str, ip: str = "203.0.113.5") -> object:
        with mock.patch.object(recovery, "_client_ip", return_value=ip):
            return self.client.post("/billing/recover", json={"email": email})

    def confirm(self, code: str, ip: str = "203.0.113.5") -> object:
        with mock.patch.object(recovery, "_client_ip", return_value=ip):
            return self.client.post("/billing/recover/confirm", json={"code": code})

    def code_from_mail(self) -> str:
        text = self.mailer.sent[-1].text
        return text.split("#code=", 1)[1].split()[0]

    def test_same_answer_for_known_and_unknown_emails(self) -> None:
        known, unknown = self.recover("owner@example.com"), self.recover("nobody@example.com")
        self.assertEqual((known.status_code, known.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(known.status_code, 202)
        self.assertEqual([m.to for m in self.mailer.sent], ["owner@example.com"])

    def test_link_signs_in_once_with_a_new_session(self) -> None:
        self.recover("Owner@Example.com")
        code = self.code_from_mail()
        self.assertIn("/recover#code=", self.mailer.sent[-1].text)
        first = self.confirm(code)
        self.assertEqual(first.status_code, 200)
        info = parse_token(first.json()["access_token"])
        self.assertEqual(info.customer_id, CUSTOMER)
        self.assertLess(time.time() - info.auth_at, 5)
        self.assertEqual(self.confirm(code).status_code, 400)

    def test_simultaneous_confirmations_succeed_once(self) -> None:
        self.recover("owner@example.com")
        code = self.code_from_mail()
        statuses: list[int] = []
        threads = [threading.Thread(target=lambda: statuses.append(self.confirm(code).status_code)) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(statuses), [200, 400, 400, 400, 400])

    def test_code_expires(self) -> None:
        self.recover("owner@example.com")
        code = self.code_from_mail()
        with mock.patch("time.time", return_value=time.time() + recovery.CODE_TTL_SECONDS + 1):
            self.assertEqual(self.confirm(code).status_code, 400)

    def test_wrong_or_malformed_codes(self) -> None:
        for code in ("x", "A" * 43, "../../etc/passwd"):
            with self.subTest(code=code):
                self.assertEqual(self.confirm(code).status_code, 400)

    def test_subscription_ended_before_confirmation(self) -> None:
        self.recover("owner@example.com")
        code = self.code_from_mail()
        self.subs(_sub("canceled"))
        self.assertEqual(self.confirm(code).status_code, 403)

    def test_email_without_open_subscription_gets_no_mail(self) -> None:
        self.subs(_sub("canceled"))
        self.assertEqual(self.recover("owner@example.com").status_code, 202)
        self.assertEqual(self.mailer.sent, [])

    def test_per_email_limit_drops_mail_silently(self) -> None:
        for _ in range(recovery.RECOVER_PER_EMAIL_PER_HOUR + 2):
            self.assertEqual(self.recover("owner@example.com", ip=f"198.51.100.{_}").status_code, 202)
        self.assertEqual(len(self.mailer.sent), recovery.RECOVER_PER_EMAIL_PER_HOUR)

    def test_per_ip_limit_is_the_same_for_every_email(self) -> None:
        codes = [self.recover(f"user{i}@example.com", ip="192.0.2.9").status_code for i in range(recovery.RECOVER_PER_IP_PER_HOUR + 1)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(self.recover("owner@example.com", ip="192.0.2.9").status_code, 429)

    def test_confirm_attempts_are_limited_per_ip(self) -> None:
        codes = [self.confirm("A" * 43, ip="192.0.2.10").status_code for _ in range(recovery.CONFIRM_PER_IP_PER_HOUR + 1)]
        self.assertEqual(codes[-1], 429)

    def test_missing_mail_or_store_is_unavailable_for_everyone(self) -> None:
        use_mailer(None)  # MAIL_BACKEND unset
        for email in ("owner@example.com", "nobody@example.com"):
            self.assertEqual(self.recover(email).status_code, 503)
        use_mailer(self.mailer)
        with mock.patch.object(self.store, "incr", side_effect=StoreUnavailable("down")):
            for email in ("owner@example.com", "nobody@example.com"):
                self.assertEqual(self.recover(email).status_code, 503)

    def test_stripe_metadata_is_not_used_and_codes_are_not_logged(self) -> None:
        with self.assertLogs("lexshield.recovery", level=logging.INFO) as logs, mock.patch.object(
            stripe.Customer, "modify"
        ) as modify:
            self.recover("owner@example.com")
            code = self.code_from_mail()
            self.confirm(code)
        modify.assert_not_called()
        joined = "\n".join(logs.output)
        self.assertNotIn(code, joined)
        self.assertNotIn("owner@example.com", joined)

    def test_recovery_after_session_limit_starts_a_fresh_session(self) -> None:
        expired = _v2(issued_ago=DAY, auth_ago=SESSION_MAX_SECONDS + DAY)
        self.assertEqual(self.refresh(expired).status_code, 401)
        self.recover("owner@example.com")
        token = self.confirm(self.code_from_mail()).json()["access_token"]
        self.assertEqual(self.me(token), 200)


if __name__ == "__main__":
    import unittest

    unittest.main()
