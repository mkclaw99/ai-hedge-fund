"""Freshness policy for cached Financial Datasets entries.

Every cached object is timestamped (when it was fetched). This module decides,
from that timestamp and the request's as-of date, whether a cached entry is
still valid or whether a newer version might exist and it should be re-fetched.

The rule is point-in-time aware:

- **Immutable** — a request whose as-of (end) date is strictly in the past.
  History doesn't change ("AAPL metrics as of 2024-03-01" is fixed), so the
  entry is valid forever regardless of age.
- **Volatile** — a request whose as-of date is today (or undatable). New
  filings, prices, and news can still arrive, so the entry is only trusted
  within a freshness window (``volatile_ttl``); past that it's revalidated.

The as-of date is read from the cache key, which embeds the request's dates
(e.g. ``AAPL_2024-01-01_2024-03-01`` or ``AAPL_ttm_2026-05-26_10``) — the
latest date in the key is the as-of date.
"""

from __future__ import annotations

import datetime
import os
import re

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Default window during which a volatile (as-of today) entry is trusted.
_DEFAULT_VOLATILE_TTL = 6 * 60 * 60  # 6 hours


def default_volatile_ttl() -> int:
    """Volatile freshness window in seconds (env override: FD_CACHE_VOLATILE_TTL_SECONDS)."""
    raw = os.environ.get("FD_CACHE_VOLATILE_TTL_SECONDS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_VOLATILE_TTL


def parse_as_of(cache_key: str) -> str | None:
    """Return the latest YYYY-MM-DD found in *cache_key* (the request's as-of date)."""
    dates = _DATE_RE.findall(cache_key or "")
    return max(dates) if dates else None


def is_immutable(cache_key: str, today: str | None = None) -> bool:
    """True when the as-of date is strictly before *today* (data can't change)."""
    today = today or datetime.date.today().isoformat()
    as_of = parse_as_of(cache_key)
    return as_of is not None and as_of < today


def is_fresh(
    cache_key: str,
    age_seconds: float,
    *,
    volatile_ttl: int | None = None,
    today: str | None = None,
) -> bool:
    """Whether a cached entry should still be served.

    Immutable (past as-of) → always fresh. Volatile (today) → fresh only
    while younger than *volatile_ttl*, after which a newer version may
    exist and it should be re-fetched.

    Date-less keys (e.g. ``"AAPL"``, ``"AAPL_ttm"``) are treated as
    **accumulated history buckets** — the bucket itself never expires;
    its trailing edge is refreshed by the api.py accessor's own
    tail-refresh logic, not by invalidating the whole bucket every
    volatile_ttl seconds. Without this exemption a 36-year price bucket
    would be thrown away every 6 hours, defeating the point of caching.
    """
    if is_immutable(cache_key, today):
        return True
    # No date in key ⇒ ticker-only bucket; managed by caller.
    if parse_as_of(cache_key) is None:
        return True
    ttl = default_volatile_ttl() if volatile_ttl is None else volatile_ttl
    return age_seconds <= ttl
