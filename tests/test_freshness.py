"""Tests for timestamp-based cache freshness validation."""

import pytest

from src.data.cache import Cache
from src.data.freshness import is_fresh, is_immutable, parse_as_of
from src.data.persistent_cache import SQLiteCache

TODAY = "2026-05-27"
PAST = "2020-01-01"


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------

def test_parse_as_of_takes_latest_date():
    assert parse_as_of("AAPL_2024-01-01_2024-03-01") == "2024-03-01"
    assert parse_as_of("AAPL_ttm_2026-05-26_10") == "2026-05-26"
    assert parse_as_of("AAPL") is None


def test_is_immutable_past_vs_today():
    assert is_immutable(f"AAPL_2024-01-01_2024-03-01", today=TODAY) is True
    assert is_immutable(f"AAPL_2026-05-20_{TODAY}", today=TODAY) is False   # as-of today
    assert is_immutable("AAPL", today=TODAY) is False                       # undatable


def test_is_fresh_immutable_always_fresh_even_when_old():
    # past as-of, huge age, zero ttl -> still fresh (history doesn't change)
    assert is_fresh(f"X_{PAST}_{PAST}", age_seconds=10**9, volatile_ttl=0, today=TODAY) is True


def test_is_fresh_volatile_respects_ttl():
    key = f"X_2026-05-20_{TODAY}"   # as-of today -> volatile
    assert is_fresh(key, age_seconds=10, volatile_ttl=60, today=TODAY) is True    # within window
    assert is_fresh(key, age_seconds=120, volatile_ttl=60, today=TODAY) is False  # aged out


# ---------------------------------------------------------------------------
# SQLiteCache honors freshness
# ---------------------------------------------------------------------------

def test_sqlite_immutable_served_even_with_zero_volatile_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_CACHE_VOLATILE_TTL_SECONDS", "0")
    c = SQLiteCache(tmp_path / "fd.db")
    key = f"AAPL_2024-01-01_2024-03-01"   # past as-of -> immutable
    c.set("prices", key, [{"time": "2024-02-01", "close": 1.0}])
    assert c.get("prices", key) is not None     # immutable: served despite ttl=0


def test_sqlite_volatile_goes_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_CACHE_VOLATILE_TTL_SECONDS", "0")
    c = SQLiteCache(tmp_path / "fd.db")
    key = f"AAPL_2026-05-20_{TODAY}"            # as-of today -> volatile
    c.set("prices", key, [{"time": TODAY, "close": 1.0}])
    # ttl=0 means any age is stale -> revalidate (miss)
    assert c.get("prices", key) is None


# ---------------------------------------------------------------------------
# In-memory tier honors freshness
# ---------------------------------------------------------------------------

def test_inmemory_volatile_stale_falls_through(monkeypatch):
    monkeypatch.setenv("FD_CACHE_VOLATILE_TTL_SECONDS", "0")
    c = Cache()  # pure in-memory
    key = f"AAPL_2026-05-20_{TODAY}"
    c.set_prices(key, [{"time": TODAY, "close": 1.0}])
    assert c.get_prices(key) is None            # volatile + ttl 0 -> stale miss


def test_inmemory_immutable_stays(monkeypatch):
    monkeypatch.setenv("FD_CACHE_VOLATILE_TTL_SECONDS", "0")
    c = Cache()
    key = "AAPL_2024-01-01_2024-03-01"
    c.set_prices(key, [{"time": "2024-02-01", "close": 1.0}])
    assert c.get_prices(key) == [{"time": "2024-02-01", "close": 1.0}]   # immutable served
