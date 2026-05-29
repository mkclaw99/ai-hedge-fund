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

import pandas as pd

logger = logging.getLogger(__name__)

from src.data.cache import get_cache
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
    """Fetch OHLCV bars for ``ticker``. Falls back across providers on miss."""
    cache_key = f"{ticker}_{start_date}_{end_date}"
    cached_data = _cache.get_prices(cache_key)
    if cached_data is not None:
        return [Price(**price) for price in cached_data]
    prices = _chain_with_key(api_key).get_prices(ticker, start_date, end_date)
    # Only cache when we got something — an "everyone returned empty" outcome
    # may be a transient upstream issue; re-fetching next call is cheap and
    # gives the user a chance once credits/connectivity return.
    if prices:
        _cache.set_prices(cache_key, [p.model_dump() for p in prices])
    return prices


def get_financial_metrics(
    ticker: str,
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
    api_key: str = None,
) -> list[FinancialMetrics]:
    """Fetch financial metrics. Yahoo can serve a single-row TTM fallback when
    Financial Datasets is unavailable; analysts that depend on multi-row
    historical metrics will see a shorter list rather than empty."""
    cache_key = f"{ticker}_{period}_{end_date}_{limit}"
    cached_data = _cache.get_financial_metrics(cache_key)
    if cached_data is not None:
        return [FinancialMetrics(**metric) for metric in cached_data]
    metrics = _chain_with_key(api_key).get_financial_metrics(
        ticker, end_date, period=period, limit=limit,
    )
    if metrics:
        _cache.set_financial_metrics(cache_key, [m.model_dump() for m in metrics])
    return metrics


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


def get_insider_trades(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str = None,
) -> list[InsiderTrade]:
    """Fetch insider trades for the date window."""
    cache_key = f"{ticker}_{start_date or 'none'}_{end_date}_{limit}"
    cached_data = _cache.get_insider_trades(cache_key)
    if cached_data is not None:
        return [InsiderTrade(**trade) for trade in cached_data]
    trades = _chain_with_key(api_key).get_insider_trades(
        ticker, end_date, start_date=start_date, limit=limit,
    )
    if trades:
        _cache.set_insider_trades(cache_key, [t.model_dump() for t in trades])
    return trades


def get_company_news(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
    api_key: str = None,
) -> list[CompanyNews]:
    """Fetch company news. Cached the same way the legacy FD-only path did."""
    cache_key = f"{ticker}_{start_date or 'none'}_{end_date}_{limit}"
    cached_data = _cache.get_company_news(cache_key)
    if cached_data is not None:
        return [CompanyNews(**news) for news in cached_data]
    news = _chain_with_key(api_key).get_company_news(
        ticker, end_date, start_date=start_date, limit=limit,
    )
    if news:
        _cache.set_company_news(cache_key, [n.model_dump() for n in news])
    return news


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
