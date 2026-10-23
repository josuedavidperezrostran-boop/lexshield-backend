"""Shared store atomicity, expiry and restarts. SQLite cases use real separate processes."""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import time
import unittest
from unittest import mock

from app.config import settings
from app.store import MemoryStore, SqliteStore, StoreUnavailable, get_store, use_store


def _take_worker(path: str, key: str, results: "multiprocessing.Queue[str | None]") -> None:
    results.put(SqliteStore(path).take(key))


def _add_worker(path: str, key: str, value: str, results: "multiprocessing.Queue[bool]") -> None:
    results.put(SqliteStore(path).add(key, value, 60))


def _incr_worker(path: str, key: str, times: int) -> None:
    store = SqliteStore(path)
    for _ in range(times):
        store.incr(key, 60)


def _run_processes(target, args_list):  # type: ignore[no-untyped-def]
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=target, args=args) for args in args_list]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        if proc.exitcode != 0:
            raise AssertionError(f"worker exited with {proc.exitcode}")


class _Contract:
    def make(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def test_add_only_once_until_expiry(self) -> None:
        store = self.make()
        self.assertTrue(store.add("k", "a", 10))
        self.assertFalse(store.add("k", "b", 10))
        self.assertEqual(store.get("k"), "a")
        with mock.patch("time.time", return_value=time.time() + 11):
            self.assertIsNone(store.get("k"))
            self.assertTrue(store.add("k", "c", 10))

    def test_take_returns_once(self) -> None:
        store = self.make()
        store.set("code", "cus_1", 10)
        self.assertEqual(store.take("code"), "cus_1")
        self.assertIsNone(store.take("code"))

    def test_take_ignores_expired(self) -> None:
        store = self.make()
        store.set("code", "cus_1", 10)
        with mock.patch("time.time", return_value=time.time() + 11):
            self.assertIsNone(store.take("code"))

    def test_incr_counts_and_resets_after_ttl(self) -> None:
        store = self.make()
        self.assertEqual([store.incr("n", 10) for _ in range(3)], [1, 2, 3])
        with mock.patch("time.time", return_value=time.time() + 11):
            self.assertEqual(store.incr("n", 10), 1)

    def test_delete_if_only_for_owner(self) -> None:
        store = self.make()
        store.add("lock", "owner-a", 10)
        self.assertFalse(store.delete_if("lock", "owner-b"))
        self.assertTrue(store.delete_if("lock", "owner-a"))
        self.assertIsNone(store.get("lock"))


class MemoryStoreTests(_Contract, unittest.TestCase):
    def make(self) -> MemoryStore:
        return MemoryStore()


class SqliteStoreTests(_Contract, unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "store.db")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def make(self) -> SqliteStore:
        return SqliteStore(self.path)

    def test_survives_restart(self) -> None:
        self.make().set("revoked", "1", 60)
        self.assertEqual(SqliteStore(self.path).get("revoked"), "1")

    def test_take_is_single_use_across_processes(self) -> None:
        self.make().set("code", "cus_1", 60)
        ctx = multiprocessing.get_context("spawn")
        results = ctx.Queue()
        _run_processes(_take_worker, [(self.path, "code", results) for _ in range(6)])
        values = [results.get(timeout=10) for _ in range(6)]
        self.assertEqual(values.count("cus_1"), 1)
        self.assertEqual(values.count(None), 5)

    def test_add_has_one_winner_across_processes(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        results = ctx.Queue()
        _run_processes(_add_worker, [(self.path, "lock", f"p{i}", results) for i in range(6)])
        outcomes = [results.get(timeout=10) for _ in range(6)]
        self.assertEqual(outcomes.count(True), 1)

    def test_incr_is_exact_across_processes(self) -> None:
        _run_processes(_incr_worker, [(self.path, "n", 25) for _ in range(4)])
        self.assertEqual(self.make().incr("n", 60), 101)

    def test_unwritable_path_reports_unavailable(self) -> None:
        store = self.make()
        os.chmod(self.path, 0o444)
        try:
            with self.assertRaises(StoreUnavailable):
                store.set("k", "v", 10)
        finally:
            os.chmod(self.path, 0o666)


class StoreSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (settings.shared_store_url, settings.environment)
        use_store(None)

    def tearDown(self) -> None:
        settings.shared_store_url, settings.environment = self._saved

    def test_production_refuses_memory_and_missing_store(self) -> None:
        settings.environment = "production"
        for url in ("", "memory://"):
            with self.subTest(url=url):
                settings.shared_store_url = url
                with self.assertRaises(StoreUnavailable):
                    get_store()

    def test_redis_is_not_available_yet(self) -> None:
        settings.shared_store_url = "redis://localhost:6379/0"
        with self.assertRaises(StoreUnavailable):
            get_store()


if __name__ == "__main__":
    unittest.main()
