"""Financial Datasets — the original (and still preferred) data source.

This is the existing logic from ``src.tools.api`` lifted into a provider
shape. Two behaviour-preserving changes:

* The 429 backoff is unchanged (60s, 90s, 120s…), so heavily-rate-limited
  tickers still wait rather than skip — falling through to yfinance on a
  429 would be much faster but would also mean yfinance starts serving
  data the user didn't ask for. We only fall through on non-rate-limit
  errors (402 credit, 5xx, network).

* All methods return ``[]`` / ``None`` on any failure — the chain treats
  that as "miss" and tries the next provider.
"""
from __future__ import annotations

import datetime
import logging
import os
import time

import requests

from src.data.models import (
    CompanyFactsResponse,
    CompanyNews,
    CompanyNewsResponse,
    FinancialMetrics,
    FinancialMetricsResponse,
    InsiderTrade,
    InsiderTradeResponse,
    LineItem,
    LineItemResponse,
    Price,
    PriceResponse,
)

logger = logging.getLogger(__name__)

_NEWS_PAGE_SIZE = 10
_NEWS_MAX_PAGES = int(os.environ.get("FD_NEWS_MAX_PAGES", "10"))


def _make_api_request(url: str, headers: dict, method: str = "GET", json_data: dict = None, max_retries: int = 3) -> requests.Response:
    """Identical to the historical request helper — preserves the 429 backoff."""
    for attempt in range(max_retries + 1):
        if method.upper() == "POST":
            response = requests.post(url, headers=headers, json=json_data)
        else:
            response = requests.get(url, headers=headers)
        if response.status_code == 429 and attempt < max_retries:
            delay = 60 + (30 * attempt)
            logger.warning("Rate limited (429). Attempt %d/%d. Waiting %ds…", attempt + 1, max_retries + 1, delay)
            time.sleep(delay)
            continue
        return response


class FDProvider:
    """Financial Datasets HTTP client wrapped as a :class:`DataProvider`."""

    name = "financial_datasets"

    def __init__(self, api_key: str | None = None) -> None:
        # Same precedence as before: explicit arg → env var → empty (FD accepts
        # unauth'd requests with stricter rate limits).
        self._api_key = api_key or os.environ.get("FINANCIAL_DATASETS_API_KEY") or ""

    def _headers(self) -> dict:
        return {"X-API-KEY": self._api_key} if self._api_key else {}

    # ------------------------------------------------------------------
    def get_prices(self, ticker: str, start_date: str, end_date: str) -> list[Price]:
        url = (
            f"https://api.financialdatasets.ai/prices/?ticker={ticker}"
            f"&interval=day&interval_multiplier=1&start_date={start_date}&end_date={end_date}"
        )
        response = _make_api_request(url, self._headers())
        if response.status_code != 200:
            return []
        try:
            return PriceResponse(**response.json()).prices
        except Exception as e:
            logger.warning("FD: failed to parse price response for %s: %s", ticker, e)
            return []

    # ------------------------------------------------------------------
    def get_financial_metrics(
        self, ticker: str, end_date: str, period: str = "ttm", limit: int = 10,
    ) -> list[FinancialMetrics]:
        url = (
            f"https://api.financialdatasets.ai/financial-metrics/?ticker={ticker}"
            f"&report_period_lte={end_date}&limit={limit}&period={period}"
        )
        response = _make_api_request(url, self._headers())
        if response.status_code != 200:
            return []
        try:
            return FinancialMetricsResponse(**response.json()).financial_metrics
        except Exception as e:
            logger.warning("FD: failed to parse financial metrics response for %s: %s", ticker, e)
            return []

    # ------------------------------------------------------------------
    def search_line_items(
        self, ticker: str, line_items: list[str], end_date: str,
        period: str = "ttm", limit: int = 10,
    ) -> list[LineItem]:
        url = "https://api.financialdatasets.ai/financials/search/line-items"
        body = {
            "tickers": [ticker], "line_items": line_items,
            "end_date": end_date, "period": period, "limit": limit,
        }
        response = _make_api_request(url, self._headers(), method="POST", json_data=body)
        if response.status_code != 200:
            return []
        try:
            results = LineItemResponse(**response.json()).search_results
        except Exception as e:
            logger.warning("FD: failed to parse line items response for %s: %s", ticker, e)
            return []
        return (results or [])[:limit]

    # ------------------------------------------------------------------
    def get_insider_trades(
        self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000,
    ) -> list[InsiderTrade]:
        all_trades: list[InsiderTrade] = []
        current_end_date = end_date
        while True:
            url = f"https://api.financialdatasets.ai/insider-trades/?ticker={ticker}&filing_date_lte={current_end_date}"
            if start_date:
                url += f"&filing_date_gte={start_date}"
            url += f"&limit={limit}"
            response = _make_api_request(url, self._headers())
            if response.status_code != 200:
                break
            try:
                trades = InsiderTradeResponse(**response.json()).insider_trades
            except Exception as e:
                logger.warning("FD: failed to parse insider trades for %s: %s", ticker, e)
                break
            if not trades:
                break
            all_trades.extend(trades)
            if not start_date or len(trades) < limit:
                break
            current_end_date = min(t.filing_date for t in trades).split("T")[0]
            if current_end_date <= start_date:
                break
        return all_trades

    # ------------------------------------------------------------------
    def get_company_news(
        self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000,
    ) -> list[CompanyNews]:
        page_size = min(limit, _NEWS_PAGE_SIZE)
        max_pages = min(-(-limit // page_size), _NEWS_MAX_PAGES)
        all_news: list[CompanyNews] = []
        seen: set = set()
        current_end_date = end_date
        for _ in range(max_pages):
            url = f"https://api.financialdatasets.ai/news/?ticker={ticker}&end_date={current_end_date}"
            if start_date:
                url += f"&start_date={start_date}"
            url += f"&limit={page_size}"
            response = _make_api_request(url, self._headers())
            if response.status_code != 200:
                break
            try:
                company_news = CompanyNewsResponse(**response.json()).news
            except Exception as e:
                logger.warning("FD: failed to parse news for %s: %s", ticker, e)
                break
            if not company_news:
                break
            for news in company_news:
                ident = news.url or (news.date, news.title)
                if ident not in seen:
                    seen.add(ident)
                    all_news.append(news)
            if len(all_news) >= limit or len(company_news) < page_size:
                break
            oldest = min(n.date for n in company_news)[:10]
            try:
                next_end = (datetime.date.fromisoformat(oldest) - datetime.timedelta(days=1)).isoformat()
            except ValueError:
                break
            if next_end >= current_end_date:
                break
            current_end_date = next_end
            if start_date and current_end_date < start_date:
                break
        return all_news[:limit]

    # ------------------------------------------------------------------
    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        """Use ``/company/facts`` for today; fall back to latest financial metric."""
        if end_date == datetime.datetime.now().strftime("%Y-%m-%d"):
            url = f"https://api.financialdatasets.ai/company/facts/?ticker={ticker}"
            response = _make_api_request(url, self._headers())
            if response.status_code == 200:
                try:
                    cap = CompanyFactsResponse(**response.json()).company_facts.market_cap
                except Exception:
                    cap = None
                if cap:
                    return cap
        metrics = self.get_financial_metrics(ticker, end_date)
        if not metrics:
            return None
        return metrics[0].market_cap
