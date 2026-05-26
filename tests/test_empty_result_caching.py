"""Empty FD responses are cached so they aren't re-fetched on every run."""

import pytest

import src.data.cache as cache_mod
import src.tools.api as api


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


@pytest.fixture
def fresh_inmemory_cache(monkeypatch):
    """Reset the global cache to a pure in-memory instance for each test."""
    monkeypatch.setenv("FD_CACHE_PERSIST", "0")
    cache_mod._cache = None
    fresh = cache_mod.get_cache()
    monkeypatch.setattr(api, "_cache", fresh)
    return fresh


# ---------------------------------------------------------------------------
# Cache-level: an empty list round-trips as a hit (not a miss)
# ---------------------------------------------------------------------------

def test_cache_stores_empty_list_as_hit():
    c = cache_mod.Cache()
    c.set_prices("AAPL_k", [])
    assert c.get_prices("AAPL_k") == []          # hit (empty), not None (miss)
    assert c.get_prices("AAPL_k") is not None


# ---------------------------------------------------------------------------
# api-level: a 200-empty response is fetched once, then served from cache
# ---------------------------------------------------------------------------

def test_empty_news_cached_after_one_fetch(fresh_inmemory_cache, monkeypatch):
    calls = {"n": 0}

    def fake_request(*a, **k):
        calls["n"] += 1
        return _FakeResp({"news": []})           # 200 with no rows

    monkeypatch.setattr(api, "_make_api_request", fake_request)

    r1 = api.get_company_news("ZZZ", "2026-05-22", start_date="2026-05-15")
    r2 = api.get_company_news("ZZZ", "2026-05-22", start_date="2026-05-15")
    assert r1 == [] and r2 == []
    assert calls["n"] == 1                        # second call served from cache


def test_empty_insider_trades_cached(fresh_inmemory_cache, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(api, "_make_api_request",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or _FakeResp({"insider_trades": []})))
    api.get_insider_trades("ZZZ", "2026-05-22", start_date="2026-05-15")
    api.get_insider_trades("ZZZ", "2026-05-22", start_date="2026-05-15")
    assert calls["n"] == 1


def test_api_error_is_not_cached(fresh_inmemory_cache, monkeypatch):
    calls = {"n": 0}

    def failing(*a, **k):
        calls["n"] += 1
        return _FakeResp({"detail": "error"}, status=500)   # non-200

    monkeypatch.setattr(api, "_make_api_request", failing)
    api.get_company_news("ZZZ", "2026-05-22", start_date="2026-05-15")
    api.get_company_news("ZZZ", "2026-05-22", start_date="2026-05-15")
    assert calls["n"] == 2                          # errors re-fetch (not cached)
