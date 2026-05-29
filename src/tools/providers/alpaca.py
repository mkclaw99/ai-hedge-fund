"""Alpaca Market Data API — prices and news only.

Ported from ``v2/data/alpaca_client.py`` (the original lives there as an
isolated experiment). Alpaca's data API doesn't carry fundamentals,
earnings, insider trades, or market cap — those methods return ``[]`` /
``None`` so the chain falls through to ``YFinanceProvider`` cleanly.

Credentials come from the constructor (api_key / api_secret), with
environment fallback. Both ``ALPACA_PAPER_*`` and the legacy ``ALPACA_*`` /
``APCA_API_*`` env vars are accepted — the data API uses the same auth as
trading, so paper-account credentials work for read-only data lookups.
"""
from __future__ import annotations

import logging
import os

import requests

from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)

logger = logging.getLogger(__name__)


class AlpacaProvider:
    """Alpaca market-data client wrapped as a :class:`DataProvider`."""

    name = "alpaca"

    DATA_URL = "https://data.alpaca.markets"
    _UNIT_MAP = {
        "minute": "Min", "min": "Min",
        "hour": "Hour", "day": "Day", "week": "Week", "month": "Month",
    }

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        feed: str = "iex",
        adjustment: str = "split",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = (
            api_key
            or os.environ.get("ALPACA_PAPER_API_KEY_ID")
            or os.environ.get("ALPACA_KEY")
            or os.environ.get("APCA_API_KEY_ID", "")
        )
        self._api_secret = (
            api_secret
            or os.environ.get("ALPACA_PAPER_SECRET_KEY")
            or os.environ.get("ALPACA_SECRET_KEY")
            or os.environ.get("APCA_API_SECRET_KEY", "")
        )
        self._feed = feed              # iex (free) | sip (paid)
        self._adjustment = adjustment
        self._timeout = timeout

    @property
    def _configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @property
    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
            "accept": "application/json",
        }

    # ------------------------------------------------------------------
    def get_prices(self, ticker: str, start_date: str, end_date: str) -> list[Price]:
        if not self._configured:
            return []
        params = {
            "timeframe": "1Day",
            "start": start_date,
            "end": end_date,
            "limit": 10_000,
            "adjustment": self._adjustment,
            "feed": self._feed,
        }
        prices: list[Price] = []
        url = f"{self.DATA_URL}/v2/stocks/{ticker}/bars"
        next_token: str | None = None
        for _ in range(20):  # bounded pagination — 200k bars max
            if next_token:
                params["page_token"] = next_token
            try:
                r = requests.get(url, headers=self._headers, params=params, timeout=self._timeout)
            except requests.RequestException as e:
                logger.debug("alpaca prices request failed for %s: %s", ticker, e)
                return []
            if r.status_code != 200:
                return []
            try:
                body = r.json()
            except Exception:
                return []
            for b in body.get("bars") or []:
                try:
                    prices.append(Price(
                        open=float(b["o"]), high=float(b["h"]), low=float(b["l"]),
                        close=float(b["c"]), volume=int(b["v"]),
                        time=b["t"][:10],  # YYYY-MM-DDTHH:MM:SSZ → YYYY-MM-DD
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            next_token = body.get("next_page_token")
            if not next_token:
                break
        return prices

    # ------------------------------------------------------------------
    def get_company_news(
        self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000,
    ) -> list[CompanyNews]:
        if not self._configured:
            return []
        params: dict = {
            "symbols": ticker,
            "end": end_date,
            "limit": min(limit, 50),  # Alpaca caps each page at 50
            "sort": "desc",
            "include_content": "false",
        }
        if start_date:
            params["start"] = start_date
        url = f"{self.DATA_URL}/v1beta1/news"
        out: list[CompanyNews] = []
        next_token: str | None = None
        while len(out) < limit:
            if next_token:
                params["page_token"] = next_token
            try:
                r = requests.get(url, headers=self._headers, params=params, timeout=self._timeout)
            except requests.RequestException as e:
                logger.debug("alpaca news request failed for %s: %s", ticker, e)
                break
            if r.status_code != 200:
                break
            try:
                body = r.json()
            except Exception:
                break
            for item in body.get("news") or []:
                try:
                    out.append(CompanyNews(
                        ticker=ticker,
                        title=item["headline"],
                        source=item.get("source") or "alpaca",
                        date=item.get("created_at", "")[:10],
                        url=item.get("url") or "",
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            next_token = body.get("next_page_token")
            if not next_token:
                break
        return out[:limit]

    # ------------------------------------------------------------------
    # Alpaca's data API doesn't expose fundamentals → empty / None so the
    # chain falls through cleanly to yfinance.
    # ------------------------------------------------------------------
    def get_financial_metrics(self, ticker, end_date, period="ttm", limit=10) -> list[FinancialMetrics]:
        return []

    def search_line_items(self, ticker, line_items, end_date, period="ttm", limit=10) -> list[LineItem]:
        return []

    def get_insider_trades(self, ticker, end_date, start_date=None, limit=1000) -> list[InsiderTrade]:
        return []

    def get_market_cap(self, ticker, end_date) -> float | None:
        return None
