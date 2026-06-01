"""SQLite-backed persistent cache for Financial Datasets responses.

The in-memory ``Cache`` is fast but per-process: every backend restart, CLI
invocation, and backtest re-fetches the same data from the paid FD API. This
module adds a durable layer keyed by *(data_type, cache_key)* so historical
reports, filings, prices, metrics, and news are fetched **once** and reused by
every future run, process, and tool.

Why a separate file (stdlib ``sqlite3``, not SQLAlchemy): ``src/`` is imported
by the CLI, the app backend, and the backtester. Keeping the cache dependency
to the standard library avoids coupling the shared data layer to the app's ORM.

Point-in-time correctness
--------------------------
FD cache keys embed the request's date range (e.g. ``AAPL_2024-01-01_2024-03-01``).
Data for a *past* end date is immutable, so it can be cached forever. A request
whose key ends on *today* gets a key that changes when the date rolls over, so
stale "today" data is never served on a later day. An optional TTL guards the
intraday case for callers who want it; the default is permanent.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.data.freshness import is_fresh

logger = logging.getLogger(__name__)

class SQLiteCache:
    """Durable key/value store for merged FD response rows.

    Each entry is the full *merged* list of row dicts for a given
    ``(data_type, cache_key)`` — mirroring what the in-memory cache holds — so
    the persistent layer is a straight write-through of the in-memory value.
    """

    def __init__(self, db_path: str | os.PathLike | None = None, *, ttl_seconds: int | None = None) -> None:
        # Default path is `<repo>/cache/fd_cache.db` via src.paths.cache_base —
        # absolute, so the FD cache doesn't fragment across CWD-dependent stub
        # directories when uvicorn is launched from `app/frontend` or anywhere
        # other than the repo root. See src/paths.py.
        from src.paths import cache_base
        _default = str(cache_base() / "fd_cache.db")
        path = str(db_path or os.environ.get("FD_CACHE_DB") or _default)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Default TTL: permanent. Env override lets ops cap staleness globally.
        env_ttl = os.environ.get("FD_CACHE_TTL_SECONDS")
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else (int(env_ttl) if env_ttl else None)
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, data_type: str, key: str) -> list[dict] | None:
        """Return the cached rows for *(data_type, key)*, or None on miss/expiry."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload, updated_at FROM fd_cache WHERE data_type=? AND cache_key=?",
                    (data_type, key),
                ).fetchone()
            if row is None:
                return None
            payload, updated_at = row
            if self._is_expired(updated_at):
                return None
            # Freshness: serve immutable (past as-of) data forever; re-fetch
            # volatile (as-of today) data once its timestamp ages out, so newer
            # filings/prices/news are picked up.
            age = self._age_seconds(updated_at)
            if age is not None and not is_fresh(key, age):
                return None
            return json.loads(payload)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            logger.warning("SQLiteCache.get failed for %s/%s: %s", data_type, key, exc)
            return None

    def set(self, data_type: str, key: str, rows: list[dict]) -> None:
        """Upsert the (already-merged) rows for *(data_type, key)*. Never raises."""
        try:
            payload = json.dumps(rows)
            now = datetime.now(timezone.utc).isoformat()
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO fd_cache (data_type, cache_key, payload, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(data_type, cache_key) DO UPDATE SET "
                    "payload=excluded.payload, updated_at=excluded.updated_at",
                    (data_type, key, payload, now),
                )
        except (sqlite3.Error, TypeError) as exc:
            logger.warning("SQLiteCache.set failed for %s/%s: %s", data_type, key, exc)

    def stats(self) -> dict[str, int]:
        """Return entry counts per data_type (plus 'total')."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT data_type, COUNT(*) FROM fd_cache GROUP BY data_type"
                ).fetchall()
            out = {dt: n for dt, n in rows}
            out["total"] = sum(out.values())
            return out
        except sqlite3.Error as exc:
            logger.warning("SQLiteCache.stats failed: %s", exc)
            return {}

    def clear(self, data_type: str | None = None) -> int:
        """Delete all entries (or just one data_type). Returns rows deleted."""
        try:
            with self._lock, self._connect() as conn:
                if data_type:
                    cur = conn.execute("DELETE FROM fd_cache WHERE data_type=?", (data_type,))
                else:
                    cur = conn.execute("DELETE FROM fd_cache")
                return cur.rowcount
        except sqlite3.Error as exc:
            logger.warning("SQLiteCache.clear failed: %s", exc)
            return 0

    def keys_for(self, data_type: str) -> list[str]:
        """All cache_keys for a data_type. Used to migrate legacy range-keyed
        entries into per-ticker buckets — see Cache._migrate_range_keys."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT cache_key FROM fd_cache WHERE data_type=?",
                    (data_type,),
                ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.Error as exc:
            logger.warning("SQLiteCache.keys_for failed for %s: %s", data_type, exc)
            return []

    def delete(self, data_type: str, key: str) -> None:
        """Remove a single entry. Best-effort; never raises. Used by the
        migration to drop legacy range-keyed entries once they've been merged
        into the per-ticker bucket."""
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "DELETE FROM fd_cache WHERE data_type=? AND cache_key=?",
                    (data_type, key),
                )
        except sqlite3.Error as exc:
            logger.warning("SQLiteCache.delete failed for %s/%s: %s", data_type, key, exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")  # concurrent readers + one writer
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fd_cache ("
                "  data_type TEXT NOT NULL,"
                "  cache_key TEXT NOT NULL,"
                "  payload   TEXT NOT NULL,"
                "  updated_at TEXT NOT NULL,"
                "  PRIMARY KEY (data_type, cache_key)"
                ")"
            )

    def _age_seconds(self, updated_at: str) -> float | None:
        """Seconds since the entry was written, or None if the timestamp is bad."""
        try:
            ts = datetime.fromisoformat(updated_at)
            return (datetime.now(timezone.utc) - ts).total_seconds()
        except ValueError:
            return None

    def _is_expired(self, updated_at: str) -> bool:
        """Hard global cap (FD_CACHE_TTL_SECONDS); applies to every entry."""
        if self.ttl_seconds is None:
            return False
        age = self._age_seconds(updated_at)
        return age is not None and age > self.ttl_seconds
