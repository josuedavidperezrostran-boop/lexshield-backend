"""Checkout coordination across real processes, restarts and crashes.

Stripe is replaced by FakeStripe, kept in its own SQLite file so every process sees the same
sessions and idempotency keys, the way Stripe would. Nothing leaves the machine.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

SECRET = "unit-test-secret"
CUSTOMER = "cus_test_owner"


class FakeStripe:
    """Shared-file stand-in for Stripe Checkout sessions with idempotency."""

    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, customer TEXT, status TEXT, url TEXT, expires_at INTEGER)")
            conn.execute("CREATE TABLE IF NOT EXISTS idem (key TEXT PRIMARY KEY, session_id TEXT, params TEXT)")
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def create(self, **kwargs):  # type: ignore[no-untyped-def]
        import stripe

        key = kwargs.pop("idempotency_key", None)
        params = json.dumps(kwargs, sort_keys=True, default=str)
        time.sleep(0.05)  # widen the race window
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if key:
                row = conn.execute("SELECT session_id, params FROM idem WHERE key = ?", (key,)).fetchone()
                if row:
                    conn.execute("COMMIT")
                    if row[1] != params:
                        raise stripe.IdempotencyError("Keys for idempotent requests can only be used with the same parameters")
                    return self._session(row[0])
            sid = f"cs_test_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, 'open', ?, ?)",
                (sid, kwargs.get("customer"), f"https://checkout.stripe.com/c/pay/{sid}", kwargs.get("expires_at") or 0),
            )
            if key:
                conn.execute("INSERT INTO idem VALUES (?, ?, ?)", (key, sid, params))
            conn.execute("COMMIT")
            return self._session(sid)
        finally:
            conn.close()

    def _session(self, sid: str) -> SimpleNamespace:
        conn = self._conn()
        try:
            row = conn.execute("SELECT id, status, url, expires_at FROM sessions WHERE id = ?", (sid,)).fetchone()
        finally:
            conn.close()
        status = row[1]
        if status == "open" and row[3] and row[3] < time.time():
            status = "expired"
        return SimpleNamespace(id=row[0], status=status, url=row[2], metadata={"product": "lexshield-professional"})

    def retrieve(self, sid: str, **_):  # type: ignore[no-untyped-def]
        return self._session(sid)

    def list(self, customer=None, status=None, **_):  # type: ignore[no-untyped-def]
        conn = self._conn()
        try:
            ids = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE customer = ?", (customer,))]
        finally:
            conn.close()
        sessions = [self._session(i) for i in ids]
        return SimpleNamespace(data=[s for s in sessions if status is None or s.status == status])

    def count(self) -> int:
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            conn.close()

    def expire_all(self) -> None:
        conn = self._conn()
        try:
            conn.execute("UPDATE sessions SET status = 'expired'")
        finally:
            conn.close()


def _configure(store_path: str, fake: FakeStripe):  # type: ignore[no-untyped-def]
    """Point the app at the shared store and the fake Stripe. Returns the patchers to stop."""
    import stripe

    from app import billing, subscriptions
    from app.config import settings
    from app.store import SqliteStore, use_store

    settings.stripe_secret_key = "sk_test_unit_only"
    settings.access_token_secret = SECRET
    settings.environment = ""
    settings.dev_bypass_billing = False
    os.environ.pop("RENDER", None)
    subscriptions.clear_cache()
    use_store(SqliteStore(store_path))
    patchers = [
        mock.patch.object(stripe.checkout.Session, "create", side_effect=fake.create),
        mock.patch.object(stripe.checkout.Session, "retrieve", side_effect=fake.retrieve),
        mock.patch.object(stripe.checkout.Session, "list", side_effect=fake.list),
        mock.patch.object(stripe.Subscription, "list", return_value=SimpleNamespace(data=[])),
        mock.patch.object(settings, "stripe_price_id_test", "price_test_lexshield"),
    ]
    for patcher in patchers:
        patcher.start()
    return patchers


def _checkout_once(token: str) -> str:
    from fastapi import HTTPException

    from app import billing

    try:
        return billing.create_checkout_session(authorization=f"Bearer {token}").checkout_url
    except HTTPException as exc:
        return f"HTTP {exc.status_code}"


def _worker(store_path: str, fake_path: str, token: str, start, clock: float | None, results) -> None:  # type: ignore[no-untyped-def]
    _configure(store_path, FakeStripe(fake_path))
    start.wait(30)
    if clock is None:
        results.put(_checkout_once(token))
        return
    real_time = time.time
    offset = clock - real_time()
    with mock.patch("time.time", side_effect=lambda: real_time() + offset):
        results.put(_checkout_once(token))


def _run(store_path: str, fake_path: str, token: str, clocks: list[float | None]) -> list[str]:
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(store_path, fake_path, token, start, clock, results)) for clock in clocks]
    for proc in procs:
        proc.start()
    time.sleep(1.5)  # let every process import the app and block on the event
    start.set()
    out = [results.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join(60)
    return out


class CheckoutCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.auth import issue_access_token
        from app.config import settings

        self._dir = tempfile.TemporaryDirectory()
        self.store_path = os.path.join(self._dir.name, "store.db")
        self.fake_path = os.path.join(self._dir.name, "fake_stripe.db")
        self.fake = FakeStripe(self.fake_path)
        self._saved = (settings.stripe_secret_key, settings.access_token_secret, settings.environment, settings.dev_bypass_billing)
        self._patchers = _configure(self.store_path, self.fake)
        self.token = issue_access_token(CUSTOMER)

    def tearDown(self) -> None:
        from app.config import settings
        from app.store import use_store

        for patcher in self._patchers:
            patcher.stop()
        use_store(None)
        settings.stripe_secret_key, settings.access_token_secret, settings.environment, settings.dev_bypass_billing = self._saved
        self._dir.cleanup()

    def test_simultaneous_processes_share_one_session(self) -> None:
        urls = _run(self.store_path, self.fake_path, self.token, [None] * 8)
        self.assertEqual(len(set(urls)), 1, urls)
        self.assertTrue(urls[0].startswith("https://checkout.stripe.com/"))
        self.assertEqual(self.fake.count(), 1)

    def test_requests_straddling_the_old_ten_minute_boundary(self) -> None:
        boundary = (int(time.time()) // 600 + 1) * 600
        clocks: list[float | None] = [boundary - 0.05] * 4 + [boundary + 0.05] * 4
        urls = _run(self.store_path, self.fake_path, self.token, clocks)
        self.assertEqual(len(set(urls)), 1, urls)
        self.assertEqual(self.fake.count(), 1)

    def test_new_process_after_restart_reuses_the_session(self) -> None:
        first = _run(self.store_path, self.fake_path, self.token, [None])
        second = _run(self.store_path, self.fake_path, self.token, [None])
        self.assertEqual(first, second)
        self.assertEqual(self.fake.count(), 1)

    def test_crash_after_stripe_created_the_session(self) -> None:
        from app.store import SqliteStore, StoreUnavailable

        original = SqliteStore.set

        def fail_once(store, key, value, ttl):  # type: ignore[no-untyped-def]
            if key.startswith("checkout:session:") and not getattr(fail_once, "done", False):
                fail_once.done = True  # type: ignore[attr-defined]
                raise StoreUnavailable("crash before recording the session")
            return original(store, key, value, ttl)

        with mock.patch.object(SqliteStore, "set", fail_once):
            self.assertEqual(_checkout_once(self.token), "HTTP 503")
        retry = _checkout_once(self.token)
        self.assertTrue(retry.startswith("https://"))
        self.assertEqual(self.fake.count(), 1)

    def test_lost_store_still_finds_the_open_session_in_stripe(self) -> None:
        from app.store import SqliteStore, use_store

        first = _checkout_once(self.token)
        os.remove(self.store_path)
        use_store(SqliteStore(self.store_path))
        self.assertEqual(_checkout_once(self.token), first)
        self.assertEqual(self.fake.count(), 1)

    def test_expired_session_allows_a_new_attempt(self) -> None:
        first = _checkout_once(self.token)
        self.fake.expire_all()
        second = _checkout_once(self.token)
        self.assertNotEqual(first, second)
        self.assertEqual(self.fake.count(), 2)

    def test_lock_left_by_a_dead_process_expires(self) -> None:
        from app.store import get_store

        get_store().add(f"checkout:lock:{CUSTOMER}", "dead-process", 0.5)
        started = time.monotonic()
        self.assertTrue(_checkout_once(self.token).startswith("https://"))
        self.assertGreaterEqual(time.monotonic() - started, 0.4)

    def test_lock_held_too_long_answers_retry(self) -> None:
        from app import billing
        from app.store import get_store

        get_store().add(f"checkout:lock:{CUSTOMER}", "slow-process", 60)
        with mock.patch.object(billing, "CHECKOUT_LOCK_WAIT_SECONDS", 0.3):
            self.assertEqual(_checkout_once(self.token), "HTTP 503")
        self.assertEqual(self.fake.count(), 0)

    def test_store_outage_creates_nothing(self) -> None:
        from app.store import SqliteStore, StoreUnavailable

        with mock.patch.object(SqliteStore, "add", side_effect=StoreUnavailable("down")):
            self.assertEqual(_checkout_once(self.token), "HTTP 503")
        self.assertEqual(self.fake.count(), 0)

    def test_open_subscription_blocks_checkout_in_every_process(self) -> None:
        import stripe

        sub = {"id": "sub_1", "status": "active", "customer": CUSTOMER, "cancel_at_period_end": False,
               "metadata": {"product": "lexshield-professional"}, "items": {"data": []}}
        with mock.patch.object(stripe.Subscription, "list", return_value=SimpleNamespace(data=[sub])):
            self.assertEqual(_checkout_once(self.token), "HTTP 409")
        self.assertEqual(self.fake.count(), 0)

    def test_known_gap_anonymous_visitors_are_not_coordinated(self) -> None:
        # Documented, unresolved: without a token there is no identity to coordinate on.
        from app import billing

        billing.create_checkout_session(authorization=None)
        billing.create_checkout_session(authorization=None)
        self.assertEqual(self.fake.count(), 2)


if __name__ == "__main__":
    unittest.main()
