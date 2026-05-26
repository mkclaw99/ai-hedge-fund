"""Alpaca market-data client — an alternative price/news source.

Implements the same interface as :class:`v2.data.client.FDClient` for the
methods Alpaca supports, so it satisfies the :class:`v2.data.protocol.DataClient`
protocol and can be dropped into the event-study / backtest price path::

    from v2.data import AlpacaClient
    from v2.event_study import compute_car

    with AlpacaClient() as alpaca:
        result = compute_car(["AAPL", "MSFT"], alpaca)

Scope
-----
Alpaca's *Market Data API* (``data.alpaca.markets``) provides **prices**
(OHLCV bars) and **news**. It does **not** provide fundamentals, earnings,
insider trades, or company facts — those methods return ``[]`` / ``None``,
which the ``DataClient`` protocol explicitly permits. For fundamentals,
keep using :class:`FDClient`.

Credentials (read from the environment, override via constructor args):
    - ``ALPACA_KEY``         (or ``APCA_API_KEY_ID``)      — API key id, e.g. ``PK...``
    - ``ALPACA_SECRET_KEY``  (or ``APCA_API_SECRET_KEY``)  — API secret
    - ``ALPACA_DATA_URL``    — optional data-API base override

Both a key id **and** a secret are required; Alpaca rejects requests
(HTTP 403) if either is missing.
"""

from __future__ import annotations

import logging
import os
import time

import requests

from v2.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    FinancialMetrics,
    InsiderTrade,
    Price,
)

logger = logging.getLogger(__name__)


class AlpacaClient:
    """Alpaca Market Data API client (prices + news).

    Usage::

        with AlpacaClient() as alpaca:
            prices = alpaca.get_prices("AAPL", "2024-01-01", "2024-12-31")
    """

    DATA_URL = "https://data.alpaca.markets"
    _RETRY_DELAYS = (5, 15, 30)

    # interval -> Alpaca timeframe unit suffix
    _UNIT_MAP = {
        "minute": "Min", "min": "Min",
        "hour": "Hour",
        "day": "Day",
        "week": "Week",
        "month": "Month",
    }

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        data_url: str | None = None,
        feed: str = "iex",
        adjustment: str = "split",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("ALPACA_KEY") or os.environ.get("APCA_API_KEY_ID", "")
        self._api_secret = api_secret or os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")
        self._data_url = (data_url or os.environ.get("ALPACA_DATA_URL") or self.DATA_URL).rstrip("/")
        self._feed = feed              # iex (free) | sip (paid) | otc
        self._adjustment = adjustment  # raw | split | dividend | all
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
            "accept": "application/json",
        })

    @property
    def is_configured(self) -> bool:
        """True only when both a key id and a secret are present."""
        return bool(self._api_key and self._api_secret)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> AlpacaClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP session."""
        self._session.close()

    # ------------------------------------------------------------------
    # Prices  (DataClient)
    # ------------------------------------------------------------------

    def get_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: str = "day",
        interval_multiplier: int = 1,
        **kwargs,
    ) -> list[Price]:
        """Fetch OHLCV bars, mapped to the shared :class:`Price` model.

        Paginates transparently over Alpaca's ``next_page_token`` and returns
        bars in chronological order (Alpaca sorts ascending by default).
        Returns ``[]`` on any failure — never raises.
        """
        params = {
            "timeframe": self._timeframe(interval, interval_multiplier),
            "start": start_date,
            "end": end_date,
            "limit": 10_000,
            "adjustment": kwargs.get("adjustment", self._adjustment),
            "feed": kwargs.get("feed", self._feed),
        }
        raw = self._get_paginated(f"/v2/stocks/{ticker}/bars", params, items_key="bars")
        return [p for p in (self._bar_to_price(b) for b in raw) if p is not None]

    # ------------------------------------------------------------------
    # News  (DataClient)
    # ------------------------------------------------------------------

    def get_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]:
        """Fetch company news, mapped to the shared :class:`CompanyNews` model.

        Alpaca caps each page at 50 articles; this paginates up to *limit*.
        """
        params: dict = {
            "symbols": ticker,
            "end": end_date,
            "limit": min(limit, 50),
            "sort": "desc",
            "include_content": "false",
        }
        if start_date is not None:
            params["start"] = start_date

        raw = self._get_paginated("/v1beta1/news", params, items_key="news", max_items=limit)
        out: list[CompanyNews] = []
        for item in raw:
            try:
                out.append(CompanyNews(
                    ticker=ticker,
                    title=item["headline"],
                    source=item.get("source") or "alpaca",
                    date=item.get("created_at"),
                    url=item.get("url"),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------
    # Unsupported by Alpaca — return empty/None per the DataClient contract.
    # Use FDClient for fundamentals, earnings, insider trades, company facts.
    # ------------------------------------------------------------------

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]:
        """Not provided by Alpaca — returns ``[]``."""
        return []

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[InsiderTrade]:
        """Not provided by Alpaca — returns ``[]``."""
        return []

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        """Not provided by Alpaca — returns ``None``."""
        return None

    def get_earnings(self, ticker: str) -> Earnings | None:
        """Not provided by Alpaca — returns ``None``."""
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def _timeframe(cls, interval: str, multiplier: int) -> str:
        """Map (interval, multiplier) to an Alpaca timeframe, e.g. ('day', 1) -> '1Day'."""
        unit = cls._UNIT_MAP.get(interval.lower(), "Day")
        return f"{multiplier}{unit}"

    @staticmethod
    def _bar_to_price(bar: dict) -> Price | None:
        """Map one Alpaca bar (o/h/l/c/v/t) to a :class:`Price`. None if malformed."""
        try:
            return Price(
                open=bar["o"],
                high=bar["h"],
                low=bar["l"],
                close=bar["c"],
                volume=int(bar["v"]),
                time=bar["t"],
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _get_paginated(
        self,
        path: str,
        params: dict,
        *,
        items_key: str,
        max_items: int | None = None,
    ) -> list[dict]:
        """GET *path*, following ``next_page_token`` until exhausted or *max_items*."""
        items: list[dict] = []
        token: str | None = None
        while True:
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            resp = self._request("GET", path, params=page_params)
            if resp is None:
                break
            body = resp.json()
            items.extend(body.get(items_key) or [])
            token = body.get("next_page_token")
            if not token:
                break
            if max_items is not None and len(items) >= max_items:
                break
        return items[:max_items] if max_items is not None else items

    def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> requests.Response | None:
        """HTTP request with retry on 429. Never raises."""
        url = self._data_url + path
        for attempt, delay in enumerate((*self._RETRY_DELAYS, None)):
            try:
                resp = self._session.request(method, url, timeout=self._timeout, **kwargs)
            except requests.RequestException as exc:
                logger.warning("Request error on %s %s: %s", method, path, exc)
                return None

            if resp.status_code == 429 and delay is not None:
                logger.info(
                    "Rate limited (429), retrying in %ds (attempt %d/%d)",
                    delay, attempt + 1, len(self._RETRY_DELAYS),
                )
                time.sleep(delay)
                continue

            if resp.status_code >= 400:
                logger.warning("%s %s returned %d", method, path, resp.status_code)
                return None

            return resp

        logger.warning(
            "Rate limit exhausted after %d retries on %s %s",
            len(self._RETRY_DELAYS), method, path,
        )
        return None
