"""Bounded, terminating pagination for get_company_news.

The FD news endpoint caps limit at 10 and has no cursor; these tests prove the
date-window pagination always terminates (no hang) and respects bounds.
"""

import pytest

import src.data.cache as cache_mod
import src.tools.api as api


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setenv("FD_CACHE_PERSIST", "0")
    cache_mod._cache = None
    monkeypatch.setattr(api, "_cache", cache_mod.get_cache())


def _article(date, i):
    return {"ticker": "X", "title": f"news-{date}-{i}", "source": "s",
            "date": date, "url": f"http://x/{date}/{i}"}


# ---------------------------------------------------------------------------
# Termination guarantees (the bug that caused the hang)
# ---------------------------------------------------------------------------

def test_terminates_when_all_articles_share_one_date(fresh_cache, monkeypatch):
    """10 articles on the SAME date every page -> must stop at max_pages, not hang."""
    calls = {"n": 0}

    def always_same(url, headers, **k):
        calls["n"] += 1
        return _Resp({"news": [_article("2026-05-26", i) for i in range(10)]})

    monkeypatch.setattr(api, "_make_api_request", always_same)
    monkeypatch.setattr(api, "_NEWS_MAX_PAGES", 10)

    out = api.get_company_news("X", "2026-05-26", start_date="2026-01-01", limit=1000)
    assert calls["n"] <= 10            # bounded — did NOT loop forever
    assert len(out) == 10              # deduped to the unique articles


def test_page_size_never_exceeds_api_max(fresh_cache, monkeypatch):
    seen_urls = []

    def capture(url, headers, **k):
        seen_urls.append(url)
        return _Resp({"news": []})

    monkeypatch.setattr(api, "_make_api_request", capture)
    api.get_company_news("X", "2026-05-26", limit=1000)   # caller asks for 1000
    assert seen_urls and all("limit=10" in u and "limit=1000" not in u for u in seen_urls)


# ---------------------------------------------------------------------------
# Correct multi-page gathering
# ---------------------------------------------------------------------------

def test_pages_backwards_across_dates(fresh_cache, monkeypatch):
    # each page returns 10 articles on a distinct, decreasing date
    day = {"d": 26}

    def by_day(url, headers, **k):
        d = day["d"]
        day["d"] -= 2
        if d < 10:
            return _Resp({"news": []})
        date = f"2026-05-{d:02d}"
        return _Resp({"news": [_article(date, i) for i in range(10)]})

    monkeypatch.setattr(api, "_make_api_request", by_day)
    monkeypatch.setattr(api, "_NEWS_MAX_PAGES", 10)
    out = api.get_company_news("X", "2026-05-26", start_date="2026-05-01", limit=1000)
    assert len(out) > 10                       # gathered across multiple pages
    assert len({a.date for a in out}) > 1      # multiple distinct dates


def test_respects_limit(fresh_cache, monkeypatch):
    monkeypatch.setattr(api, "_make_api_request",
                        lambda url, headers, **k: _Resp({"news": [_article("2026-05-26", i) for i in range(10)]}))
    monkeypatch.setattr(api, "_NEWS_MAX_PAGES", 10)
    out = api.get_company_news("X", "2026-05-26", start_date="2026-01-01", limit=5)
    assert len(out) == 5                       # truncated to caller's limit


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_http_error_returns_empty_and_not_cached(fresh_cache, monkeypatch):
    calls = {"n": 0}

    def err(url, headers, **k):
        calls["n"] += 1
        return _Resp({"error": "Invalid limit"}, status=400)

    monkeypatch.setattr(api, "_make_api_request", err)
    assert api.get_company_news("X", "2026-05-26", limit=1000) == []
    api.get_company_news("X", "2026-05-26", limit=1000)   # second call
    assert calls["n"] == 2                     # errors are not cached -> retried
