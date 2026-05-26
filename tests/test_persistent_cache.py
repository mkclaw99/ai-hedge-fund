"""Tests for the SQLite persistent cache and its wiring into Cache."""

import pytest

from src.data.cache import Cache
from src.data.persistent_cache import SQLiteCache


@pytest.fixture
def db(tmp_path):
    return tmp_path / "fd_cache.db"


# ---------------------------------------------------------------------------
# SQLiteCache directly
# ---------------------------------------------------------------------------

def test_set_get_roundtrip(db):
    c = SQLiteCache(db)
    rows = [{"time": "2024-01-01", "close": 150.0}]
    c.set("prices", "AAPL_2024-01-01_2024-01-31", rows)
    assert c.get("prices", "AAPL_2024-01-01_2024-01-31") == rows


def test_get_miss_returns_none(db):
    assert SQLiteCache(db).get("prices", "NOPE") is None


def test_persists_across_instances(db):
    SQLiteCache(db).set("financial_metrics", "AAPL", [{"report_period": "2024-Q1", "revenue": 1000}])
    # a brand-new instance (simulating a process restart) sees the data
    again = SQLiteCache(db)
    assert again.get("financial_metrics", "AAPL")[0]["revenue"] == 1000


def test_stats_and_clear(db):
    c = SQLiteCache(db)
    c.set("prices", "AAPL", [{"time": "t1"}])
    c.set("prices", "MSFT", [{"time": "t1"}])
    c.set("company_news", "AAPL", [{"date": "d1"}])
    stats = c.stats()
    assert stats["prices"] == 2 and stats["company_news"] == 1 and stats["total"] == 3
    assert c.clear("prices") == 2
    assert c.stats().get("prices", 0) == 0 and c.stats()["total"] == 1
    assert c.clear() == 1
    assert c.stats().get("total", 0) == 0


def test_ttl_expiry(db):
    c = SQLiteCache(db, ttl_seconds=0)   # everything is immediately stale
    c.set("prices", "AAPL", [{"time": "t1"}])
    assert c.get("prices", "AAPL") is None   # expired


def test_set_never_raises_on_bad_payload(db):
    c = SQLiteCache(db)
    c.set("prices", "AAPL", [{"x": object()}])  # not JSON-serializable -> logged, no raise
    assert c.get("prices", "AAPL") is None


# ---------------------------------------------------------------------------
# Cache wired to a backend (write-through + restart warm)
# ---------------------------------------------------------------------------

def test_cache_write_through_and_reload(db):
    backend = SQLiteCache(db)
    c1 = Cache(backend=backend)
    c1.set_prices("AAPL_2024_2024", [{"time": "2024-01-01", "close": 150.0}])

    # a fresh in-memory Cache sharing the same backend loads from disk on miss
    c2 = Cache(backend=SQLiteCache(db))
    assert c2.get_prices("AAPL_2024_2024")[0]["close"] == 150.0


def test_cache_merge_persists(db):
    backend = SQLiteCache(db)
    c = Cache(backend=backend)
    c.set_prices("AAPL", [{"time": "2024-01-01", "close": 150.0}])
    c.set_prices("AAPL", [{"time": "2024-01-01", "close": 999.0},
                          {"time": "2024-01-02", "close": 155.0}])
    # merged + deduped by 'time', and the merged result is what persisted
    assert backend.get("prices", "AAPL") == c.get_prices("AAPL")
    assert len(backend.get("prices", "AAPL")) == 2


def test_cache_without_backend_is_pure_memory(db):
    c = Cache()  # default: no backend
    assert c._backend is None
    c.set_prices("AAPL", [{"time": "t1"}])
    assert c.get_prices("AAPL") == [{"time": "t1"}]
