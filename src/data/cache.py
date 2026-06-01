"""Cache for Financial Datasets API responses.

A two-tier cache:

- **In-memory** (this class) — fast, per-process, merges/dedupes rows by a key
  field. Always present.
- **Persistent** (optional ``backend``) — a :class:`SQLiteCache` write-through
  so data survives process restarts and is shared across the CLI, the web app,
  and the backtester. Attached only to the global singleton from
  :func:`get_cache`; constructing ``Cache()`` directly stays pure in-memory.
"""

from __future__ import annotations

import os
import re
import time

from src.data.freshness import is_fresh

# Old range-keyed cache entries (pre-ticker-bucket era) look like:
#   prices            → "AAPL_2024-01-01_2026-05-30"
#   financial_metrics → "AAPL_ttm_2024-01-01_10"
#   insider_trades    → "AAPL_2024-01-01_2026-05-30_1000"
#   company_news      → "AAPL_2024-01-01_2026-05-30_1000"
# All four embed at least one YYYY-MM-DD. The new ticker-bucket keys are
# date-less: "AAPL" or "AAPL_ttm". We detect "legacy" by the presence of a
# date.
_LEGACY_DATE_RE = re.compile(r"_\d{4}-\d{2}-\d{2}")


class Cache:
    """In-memory cache for API responses, optionally backed by a persistent store.

    Args:
        backend: an object with ``get(data_type, key)`` / ``set(data_type, key, rows)``
                 (e.g. :class:`src.data.persistent_cache.SQLiteCache`). When None,
                 the cache is purely in-memory.
    """

    def __init__(self, backend=None):
        self._prices_cache: dict[str, list[dict[str, any]]] = {}
        self._financial_metrics_cache: dict[str, list[dict[str, any]]] = {}
        self._line_items_cache: dict[str, list[dict[str, any]]] = {}
        self._insider_trades_cache: dict[str, list[dict[str, any]]] = {}
        self._company_news_cache: dict[str, list[dict[str, any]]] = {}
        self._backend = backend
        # When each (data_type, key) was fetched, for freshness revalidation.
        self._fetched_at: dict[tuple[str, str], float] = {}

    def _merge_data(self, existing: list[dict] | None, new_data: list[dict], key_field: str) -> list[dict]:
        """Merge existing and new data, avoiding duplicates based on a key field."""
        if not existing:
            return new_data

        # Create a set of existing keys for O(1) lookup
        existing_keys = {item[key_field] for item in existing}

        # Only add items that don't exist yet
        merged = existing.copy()
        merged.extend([item for item in new_data if item[key_field] not in existing_keys])
        return merged

    # ------------------------------------------------------------------
    # Generic tier-aware get/set used by every typed accessor below
    # ------------------------------------------------------------------

    def _get(self, data_type: str, store: dict, key: str) -> list[dict[str, any]] | None:
        """In-memory first; fall back to the persistent backend and warm memory.

        A stale volatile in-memory entry (as-of today, aged past the freshness
        window) is dropped so the backend — or a fresh fetch — can supply newer
        data. Immutable (past as-of) entries never go stale.
        """
        if key in store:
            age = time.time() - self._fetched_at.get((data_type, key), 0.0)
            if is_fresh(key, age):
                return store[key]
            del store[key]
            self._fetched_at.pop((data_type, key), None)
        if self._backend is not None:
            rows = self._backend.get(data_type, key)
            if rows is not None:
                store[key] = rows  # warm the in-memory tier
                self._fetched_at[(data_type, key)] = time.time()
                return rows
        return None

    def _set(self, data_type: str, store: dict, key: str, data: list[dict], key_field: str) -> None:
        """Merge into memory, then write the merged result through to the backend."""
        merged = self._merge_data(store.get(key), data, key_field)
        store[key] = merged
        self._fetched_at[(data_type, key)] = time.time()
        if self._backend is not None:
            self._backend.set(data_type, key, merged)

    # ------------------------------------------------------------------
    # Migration (one-shot, lazy): roll legacy range-keyed entries up into
    # the new per-ticker bucket. Called from api.py on first access for a
    # ticker. Each ticker pays the migration cost once.
    # ------------------------------------------------------------------

    def _migrate_legacy_keys(
        self,
        data_type: str,
        store: dict,
        bucket_key: str,
        ticker: str,
        key_field: str,
        period: str | None = None,
    ) -> None:
        """Merge any legacy ``{ticker}[_…]_DATE…`` entries into *bucket_key*.

        Args:
            data_type: cache table name ('prices', 'financial_metrics', ...).
            store: the in-memory dict for this data_type.
            bucket_key: the new ticker-bucket key (e.g. 'AAPL' or 'AAPL_ttm').
            ticker: the ticker symbol used as the prefix.
            key_field: dedup key for the rows (e.g. 'time' for prices).
            period: optional second component to constrain matches when the
                bucket key is ``ticker_period`` (so we don't merge a
                ``AAPL_annual_*`` legacy entry into the ``AAPL_ttm`` bucket).

        Best-effort: any error during migration is swallowed so a read
        path is never broken by stale legacy data.
        """
        if self._backend is None:
            return
        # Track which buckets have already been migrated this process.
        if not hasattr(self, "_migrated"):
            self._migrated = set()
        if (data_type, bucket_key) in self._migrated:
            return
        self._migrated.add((data_type, bucket_key))
        try:
            all_keys = self._backend.keys_for(data_type)
            # Prefix match + must contain a date (otherwise it's already a
            # new-format bucket key, or a free-form key we shouldn't touch).
            prefix = f"{ticker}_{period}_" if period else f"{ticker}_"
            legacy = [
                k for k in all_keys
                if k != bucket_key
                and k.startswith(prefix)
                and _LEGACY_DATE_RE.search(k)
            ]
            if not legacy:
                return
            merged: list[dict] = list(store.get(bucket_key) or [])
            for k in legacy:
                rows = self._backend.get(data_type, k)
                if rows:
                    merged = self._merge_data(merged, rows, key_field)
            if merged:
                store[bucket_key] = merged
                self._fetched_at[(data_type, bucket_key)] = time.time()
                self._backend.set(data_type, bucket_key, merged)
            for k in legacy:
                self._backend.delete(data_type, k)
        except Exception:
            # Migration is best-effort. Leave the legacy entries alone if
            # something goes wrong — they'll keep working under the old
            # range-key freshness path until we get another chance.
            pass

    # ------------------------------------------------------------------
    # Typed accessors (public API — unchanged signatures)
    # ------------------------------------------------------------------

    def get_prices(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached price data if available."""
        return self._get("prices", self._prices_cache, ticker)

    def set_prices(self, ticker: str, data: list[dict[str, any]]):
        """Append new price data to cache."""
        self._set("prices", self._prices_cache, ticker, data, key_field="time")

    def evict_prices(self, ticker: str) -> None:
        """Drop both the in-process and the persistent price entry for a ticker.

        Used by the decoupled trade-tick path (``refresh_prices=True``) so the
        next ``get_prices`` call re-fetches today's bar from the provider chain
        instead of handing back yesterday's cached "latest". The persistent
        SQLite backend (if wired) is also wiped for the same key — otherwise
        a fresh provider fetch would just re-hydrate from disk on the next
        process restart and re-serve the stale entry.
        """
        bucket_key = str(ticker).upper()
        self._prices_cache.pop(bucket_key, None)
        if self._backend is not None:
            try:
                self._backend.delete("prices", bucket_key)
            except Exception:
                # Best-effort: an evict failure shouldn't break the tick.
                # In-memory eviction above already takes effect for this
                # process; persistent-layer staleness is a process-restart-
                # only concern.
                pass

    def get_financial_metrics(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached financial metrics if available."""
        return self._get("financial_metrics", self._financial_metrics_cache, ticker)

    def set_financial_metrics(self, ticker: str, data: list[dict[str, any]]):
        """Append new financial metrics to cache."""
        self._set("financial_metrics", self._financial_metrics_cache, ticker, data, key_field="report_period")

    def get_line_items(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached line items if available."""
        return self._get("line_items", self._line_items_cache, ticker)

    def set_line_items(self, ticker: str, data: list[dict[str, any]]):
        """Append new line items to cache."""
        self._set("line_items", self._line_items_cache, ticker, data, key_field="report_period")

    def get_insider_trades(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached insider trades if available."""
        return self._get("insider_trades", self._insider_trades_cache, ticker)

    def set_insider_trades(self, ticker: str, data: list[dict[str, any]]):
        """Append new insider trades to cache."""
        self._set("insider_trades", self._insider_trades_cache, ticker, data, key_field="filing_date")

    def get_company_news(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached company news if available."""
        return self._get("company_news", self._company_news_cache, ticker)

    def set_company_news(self, ticker: str, data: list[dict[str, any]]):
        """Append new company news to cache."""
        self._set("company_news", self._company_news_cache, ticker, data, key_field="date")


# Global cache instance (lazily built with a persistent backend)
_cache: Cache | None = None


def get_cache() -> Cache:
    """Get the global cache singleton, backed by SQLite unless disabled.

    Set ``FD_CACHE_PERSIST=0`` to use a pure in-memory cache (e.g. in tests or
    ephemeral environments).
    """
    global _cache
    if _cache is None:
        backend = None
        if os.environ.get("FD_CACHE_PERSIST", "1") != "0":
            try:
                from src.data.persistent_cache import SQLiteCache
                backend = SQLiteCache()
            except Exception:  # never let cache setup break data access
                backend = None
        _cache = Cache(backend=backend)
    return _cache
