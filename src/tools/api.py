"""Public market-data API used by every analyst, the risk manager, and the
backtester. Delegates to a :class:`~src.tools.providers.chain.ProviderChain`
so a single-vendor outage no longer takes the app down.

Layout::

    caller (agent)
        └→ src.tools.api.get_prices(...)
              ├→ src.data.cache  (cache key kept stable across providers)
              └→ ProviderChain[FD, Alpaca, yfinance]  (first non-empty wins)

The cache layer sits in front of the chain so a previous run's data keeps
working even when every upstream vendor is unreachable. The cache key is
intentionally provider-agnostic — once a result is cached, it's just data.
Logging at the chain level tells you which provider served a given fresh
fetch (look for "served by alpaca" / "served by yfinance" in the backend
log if you want to see fallback in action).

Behaviour-preserving notes:

* Function signatures match the pre-refactor versions exactly — the
  agents and routes don't need any change.
* The cache keys are unchanged, so existing cached entries keep working.
* A successful empty result (no data, but the upstream said so cleanly)
  is still cached so we don't re-fetch forever.
"""
import datetime
import logging
import os
import time

import pandas as pd

logger = logging.getLogger(__name__)

from src.data.cache import get_cache
from src.data.freshness import default_volatile_ttl
from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)
from src.tools.providers import build_default_chain

# Module-level cache; constructed once.
_cache = get_cache()

# Per-bucket "I last asked the provider and got no new data" timestamp.
# Prevents hammering when the user repeatedly asks for the same
# `end_date=today` and there's nothing new (weekends, after-hours, etc.).
# Same TTL as the freshness module's volatile window.
_tail_checked_at: dict[tuple[str, str], float] = {}


def _tail_in_cooldown(data_type: str, bucket_key: str) -> bool:
    """True when we've recently asked for the tail and got nothing new."""
    last = _tail_checked_at.get((data_type, bucket_key))
    if last is None:
        return False
    return (time.time() - last) <= default_volatile_ttl()


def _mark_tail_checked(data_type: str, bucket_key: str) -> None:
    _tail_checked_at[(data_type, bucket_key)] = time.time()


def _chain_with_key(api_key: str | None):
    """Build a chain seeded with the caller's FD key (and env-var alpaca keys).

    The caller passes only the FD key today; Alpaca + yfinance pick up their
    own credentials (or lack thereof) from the environment. We don't take a
    full ``api_keys`` dict here because nothing in the existing callers has
    one — keeps the refactor a strict drop-in.
    """
    api_keys = {"FINANCIAL_DATASETS_API_KEY": api_key} if api_key else {}
    return build_default_chain(api_keys)


def get_prices(ticker: str, start_date: str, end_date: str, api_key: str = None) -> list[Price]:
    """Fetch OHLCV bars for ``ticker``.

    Uses a per-ticker accumulating bucket: each call merges its result
    into a single ``ticker``-keyed cache entry and returns the slice
    matching ``[start_date, end_date]``. Re-asking a different sub-range
    of the same ticker is served from cache; only the *missing tail*
    (or head) is re-fetched from the provider chain. A cooldown prevents
    hammering when ``end_date`` is today and the tail has nothing new.
    """
    bucket_key = ticker
    _cache._migrate_legacy_keys("prices", _cache._prices_cache, bucket_key, ticker, "time")

    cached_rows = _cache.get_prices(bucket_key) or []
    today_iso = datetime.date.today().isoformat()
    end_is_future = end_date >= today_iso

    if cached_rows:
        times = [r["time"] for r in cached_rows]
        cached_min = min(times)
        cached_max = max(times)
        head_covered = start_date >= cached_min
        # The tail is covered if either:
        #   * cached_max already reaches end_date, OR
        #   * end_date is in the future AND we've already polled the
        #     provider recently with no new data (cooldown).
        tail_covered = (cached_max >= end_date) or (end_is_future and _tail_in_cooldown("prices", bucket_key))
        if head_covered and tail_covered:
            return [Price(**r) for r in cached_rows if start_date <= r["time"] <= end_date]

    # Need to fetch — full requested range. Provider returns whatever's
    # available; _merge_data dedups by `time`, so overlap with cache is
    # cheap and the bucket only grows. We deliberately don't try to
    # narrow the request to just the gap: providers handle range queries
    # natively, but they reject zero-width or backwards ranges, so
    # constructing a precise gap interval is fragile across vendors.
    prices = _chain_with_key(api_key).get_prices(ticker, start_date, end_date)
    if prices:
        _cache.set_prices(bucket_key, [p.model_dump() for p in prices])
        cached_rows = _cache.get_prices(bucket_key) or []
    else:
        # No new tail: enter cooldown so we don't refetch on every call.
        _mark_tail_checked("prices", bucket_key)

    return [Price(**r) for r in cached_rows if start_date <= r["time"] <= end_date]


def get_financial_metrics(
    ticker: str,
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str = None,
) -> list[FinancialMetrics]:
    """Fetch financial metrics. Yahoo can serve a single-row TTM fallback when
    Financial Datasets is unavailable; analysts that depend on multi-row
    historical metrics will see a shorter list rather than empty.

    Per-(ticker, period) accumulating bucket: rows merge across calls
    (dedup by ``report_period``). A request returns the latest ``limit``
    rows where ``report_period <= end_date``; the provider is re-asked
    only when we don't have enough rows to satisfy the request.
    """
    bucket_key = f"{ticker}_{period}"
    _cache._migrate_legacy_keys(
        "financial_metrics", _cache._financial_metrics_cache,
        bucket_key, ticker, "report_period", period=period,
    )

    def _filtered(rows: list[dict]) -> list[FinancialMetrics]:
        eligible = [r for r in rows if (r.get("report_period") or "") <= end_date]
        # Latest first by report_period — most analysts only inspect the head.
        eligible.sort(key=lambda r: r.get("report_period") or "", reverse=True)
        return [FinancialMetrics(**m) for m in eligible[:limit]]

    cached_rows = _cache.get_financial_metrics(bucket_key) or []
    enough = sum(1 for r in cached_rows if (r.get("report_period") or "") <= end_date) >= limit
    if enough or (cached_rows and _tail_in_cooldown("financial_metrics", bucket_key)):
        return _filtered(cached_rows)

    metrics = _chain_with_key(api_key).get_financial_metrics(
        ticker, end_date, period=period, limit=limit,
    )
    if metrics:
        _cache.set_financial_metrics(bucket_key, [m.model_dump() for m in metrics])
        cached_rows = _cache.get_financial_metrics(bucket_key) or []
    else:
        _mark_tail_checked("financial_metrics", bucket_key)
    return _filtered(cached_rows)


def search_line_items(
    ticker: str,
    line_items: list[str],
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str = None,
) -> list[LineItem]:
    """Look up specific line items across the income/balance/cashflow statements.

    Not cached today (matches the previous behaviour — line-item requests
    can vary by ``line_items`` content and the cache layer would need a
    list-aware key). The provider chain handles fallback to yfinance, with
    fields we don't have a mapping for left as ``None`` so analysts can
    detect missing values explicitly.
    """
    return _chain_with_key(api_key).search_line_items(
        ticker, line_items, end_date, period=period, limit=limit,
    )


def _filter_by_date(rows: list[dict], date_field: str, start_date: str | None, end_date: str) -> list[dict]:
    """Return rows whose *date_field* falls in ``[start_date, end_date]``,
    newest first. ``start_date is None`` ⇒ no lower bound."""
    out = [
        r for r in rows
        if (start_date is None or (r.get(date_field) or "") >= start_date)
        and (r.get(date_field) or "") <= end_date
    ]
    out.sort(key=lambda r: r.get(date_field) or "", reverse=True)
    return out


def get_insider_trades(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str = None,
) -> list[InsiderTrade]:
    """Fetch insider trades for the date window.

    Per-ticker accumulating bucket (dedup by ``filing_date``). Refetch
    only when the cache doesn't already cover the requested tail or
    has fewer rows than ``limit`` in the window.
    """
    bucket_key = ticker
    _cache._migrate_legacy_keys(
        "insider_trades", _cache._insider_trades_cache,
        bucket_key, ticker, "filing_date",
    )

    cached_rows = _cache.get_insider_trades(bucket_key) or []
    filtered = _filter_by_date(cached_rows, "filing_date", start_date, end_date)
    today_iso = datetime.date.today().isoformat()
    cached_max = max((r.get("filing_date") or "" for r in cached_rows), default="")
    tail_covered = cached_max >= end_date or (end_date >= today_iso and _tail_in_cooldown("insider_trades", bucket_key))
    if tail_covered and len(filtered) >= min(limit, len(cached_rows)):
        return [InsiderTrade(**t) for t in filtered[:limit]]

    trades = _chain_with_key(api_key).get_insider_trades(
        ticker, end_date, start_date=start_date, limit=limit,
    )
    if trades:
        _cache.set_insider_trades(bucket_key, [t.model_dump() for t in trades])
        cached_rows = _cache.get_insider_trades(bucket_key) or []
    else:
        _mark_tail_checked("insider_trades", bucket_key)
    filtered = _filter_by_date(cached_rows, "filing_date", start_date, end_date)
    return [InsiderTrade(**t) for t in filtered[:limit]]


def get_company_news(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str = None,
) -> list[CompanyNews]:
    """Fetch company news.

    Per-ticker accumulating bucket (dedup by ``date``). Same refresh
    discipline as insider trades.
    """
    bucket_key = ticker
    _cache._migrate_legacy_keys(
        "company_news", _cache._company_news_cache,
        bucket_key, ticker, "date",
    )

    cached_rows = _cache.get_company_news(bucket_key) or []
    filtered = _filter_by_date(cached_rows, "date", start_date, end_date)
    today_iso = datetime.date.today().isoformat()
    cached_max = max((r.get("date") or "" for r in cached_rows), default="")
    tail_covered = cached_max >= end_date or (end_date >= today_iso and _tail_in_cooldown("company_news", bucket_key))
    if tail_covered and len(filtered) >= min(limit, len(cached_rows)):
        return [CompanyNews(**n) for n in filtered[:limit]]

    news = _chain_with_key(api_key).get_company_news(
        ticker, end_date, start_date=start_date, limit=limit,
    )
    if news:
        _cache.set_company_news(bucket_key, [n.model_dump() for n in news])
        cached_rows = _cache.get_company_news(bucket_key) or []
    else:
        _mark_tail_checked("company_news", bucket_key)
    filtered = _filter_by_date(cached_rows, "date", start_date, end_date)
    return [CompanyNews(**n) for n in filtered[:limit]]


def get_market_cap(
    ticker: str,
    end_date: str,
    api_key: str = None,
) -> float | None:
    """Market cap for ``end_date``. The chain prefers FD's company-facts /
    financial-metrics path; yfinance reads ``Ticker.info['marketCap']`` as
    a current-snapshot fallback (not historical-accurate, but better than
    no figure for ratio-based analysts)."""
    return _chain_with_key(api_key).get_market_cap(ticker, end_date)


def prices_to_df(prices: list[Price]) -> pd.DataFrame:
    """Convert prices to a DataFrame."""
    df = pd.DataFrame([p.model_dump() for p in prices])
    df["Date"] = pd.to_datetime(df["time"])
    df.set_index("Date", inplace=True)
    numeric_cols = ["open", "close", "high", "low", "volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.sort_index(inplace=True)
    return df


def get_price_data(ticker: str, start_date: str, end_date: str, api_key: str = None) -> pd.DataFrame:
    prices = get_prices(ticker, start_date, end_date, api_key=api_key)
    return prices_to_df(prices)
