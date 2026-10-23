"""Shared key-value store for state that must hold across processes and restarts.

Used for one-time recovery codes, rate limits, session revocation and Checkout coordination.
Every operation is atomic on its own; callers never read-then-write.

Adapters (SHARED_STORE_URL):
- "sqlite:///path/to/file.db": atomic across processes on one host; survives restarts when the
  file is on persistent disk. Not shared between hosts.
- "memory://": one process only, lost on restart. Tests and local development; refused in
  production.
- "redis://...": not available yet. It needs the redis client dependency, which is pending
  approval; until then the store reports itself as not configured.
In production an unset or memory store is "not configured" and every caller fails closed.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Protocol

from .config import settings


class StoreUnavailable(Exception):
    """The shared store is missing, misconfigured or failing. Never treat as success."""


class SharedStore(Protocol):
    def add(self, key: str, value: str, ttl: float) -> bool:
        """Set key only if absent (or expired). True when this call created it."""

    def get(self, key: str) -> Optional[str]: ...

    def set(self, key: str, value: str, ttl: float) -> None: ...

    def take(self, key: str) -> Optional[str]:
        """Return and delete in one step; a second take of the same key gets None."""

    def delete(self, key: str) -> None: ...

    def delete_if(self, key: str, value: str) -> bool:
        """Delete only while the key still holds value (lock release)."""

    def incr(self, key: str, ttl: float) -> int:
        """Add one; a new or expired counter starts at 1 and expires ttl seconds later."""


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str, now: float) -> Optional[str]:
        item = self._data.get(key)
        if item is None:
            return None
        if item[1] <= now:
            del self._data[key]
            return None
        return item[0]

    def add(self, key: str, value: str, ttl: float) -> bool:
        with self._lock:
            now = time.time()
            if self._live(key, now) is not None:
                return False
            self._data[key] = (value, now + ttl)
            return True

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._live(key, time.time())

    def set(self, key: str, value: str, ttl: float) -> None:
        with self._lock:
            self._data[key] = (value, time.time() + ttl)

    def take(self, key: str) -> Optional[str]:
        with self._lock:
            value = self._live(key, time.time())
            self._data.pop(key, None)
            return value

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def delete_if(self, key: str, value: str) -> bool:
        with self._lock:
            if self._live(key, time.time()) == value:
                del self._data[key]
                return True
            return False

    def incr(self, key: str, ttl: float) -> int:
        with self._lock:
            now = time.time()
            current = self._live(key, now)
            if current is None:
                self._data[key] = ("1", now + ttl)
                return 1
            value, expires = self._data[key]
            self._data[key] = (str(int(value) + 1), expires)
            return int(value) + 1


class SqliteStore:
    """Each call is one IMMEDIATE transaction, so concurrent processes serialize on the file."""

    def __init__(self, path: str) -> None:
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)"
            )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _run(self, fn):  # type: ignore[no-untyped-def]
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise StoreUnavailable(type(exc).__name__) from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn, time.time())
            conn.execute("COMMIT")
            return result
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise StoreUnavailable(type(exc).__name__) from exc
        finally:
            conn.close()

    def add(self, key: str, value: str, ttl: float) -> bool:
        def op(conn: sqlite3.Connection, now: float) -> bool:
            conn.execute("DELETE FROM kv WHERE key = ? AND expires_at <= ?", (key, now))
            cur = conn.execute("INSERT OR IGNORE INTO kv VALUES (?, ?, ?)", (key, value, now + ttl))
            return cur.rowcount == 1

        return self._run(op)

    def get(self, key: str) -> Optional[str]:
        def op(conn: sqlite3.Connection, now: float) -> Optional[str]:
            row = conn.execute("SELECT value FROM kv WHERE key = ? AND expires_at > ?", (key, now)).fetchone()
            return row[0] if row else None

        return self._run(op)

    def set(self, key: str, value: str, ttl: float) -> None:
        self._run(
            lambda conn, now: conn.execute(
                "INSERT INTO kv VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, expires_at = excluded.expires_at",
                (key, value, now + ttl),
            )
        )

    def take(self, key: str) -> Optional[str]:
        def op(conn: sqlite3.Connection, now: float) -> Optional[str]:
            row = conn.execute(
                "DELETE FROM kv WHERE key = ? RETURNING value, expires_at", (key,)
            ).fetchone()
            return row[0] if row and row[1] > now else None

        return self._run(op)

    def delete(self, key: str) -> None:
        self._run(lambda conn, now: conn.execute("DELETE FROM kv WHERE key = ?", (key,)))

    def delete_if(self, key: str, value: str) -> bool:
        def op(conn: sqlite3.Connection, now: float) -> bool:
            cur = conn.execute("DELETE FROM kv WHERE key = ? AND value = ? AND expires_at > ?", (key, value, now))
            return cur.rowcount == 1

        return self._run(op)

    def incr(self, key: str, ttl: float) -> int:
        def op(conn: sqlite3.Connection, now: float) -> int:
            conn.execute("DELETE FROM kv WHERE key = ? AND expires_at <= ?", (key, now))
            row = conn.execute(
                "INSERT INTO kv VALUES (?, '1', ?) ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1 RETURNING value",
                (key, now + ttl),
            ).fetchone()
            return int(row[0])

        return self._run(op)


_override: Optional[SharedStore] = None
_cached: tuple[str, SharedStore] | None = None
_cache_lock = threading.Lock()


def use_store(store: Optional[SharedStore]) -> None:
    """Tests inject an adapter; None returns to SHARED_STORE_URL."""
    global _override
    _override = store


def get_store() -> SharedStore:
    global _cached
    if _override is not None:
        return _override
    url = settings.shared_store_url.strip()
    production = settings.is_production()
    if not url or url == "memory://":
        if production:
            raise StoreUnavailable("SHARED_STORE_URL is not configured for production.")
        url = "memory://"
    if url.startswith("redis://") or url.startswith("rediss://"):
        raise StoreUnavailable("The Redis adapter is not installed yet (pending dependency approval).")
    with _cache_lock:
        if _cached and _cached[0] == url:
            return _cached[1]
        if url == "memory://":
            store: SharedStore = MemoryStore()
        elif url.startswith("sqlite:///"):
            store = SqliteStore(url[len("sqlite:///"):])
        else:
            raise StoreUnavailable("Unsupported SHARED_STORE_URL scheme.")
        _cached = (url, store)
        return store
