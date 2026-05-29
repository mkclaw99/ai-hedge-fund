"""Walk a list of :class:`~base.DataProvider`s in order and return the first
non-empty result. Hides exceptions, logs which vendor served each call.

Why a chain instead of inline ``try/except`` per data type in ``api.py``:
the agents call the same six methods over and over (prices, metrics, news,
…). Centralising the fallback logic here keeps ``api.py`` thin and makes
behaviour symmetric across data types — a fix to the fall-through rule
applies to every method, not just the one we remember to patch.
"""
from __future__ import annotations

import logging
from typing import Callable, TypeVar

from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)
from src.tools.providers.base import DataProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ProviderChain:
    """Ordered list of providers; each call tries them in turn."""

    def __init__(self, providers: list[DataProvider]) -> None:
        self.providers = providers

    # ------------------------------------------------------------------
    # Internal walker — shared by the list-returning methods.
    # ------------------------------------------------------------------
    def _walk_list(
        self,
        method_name: str,
        ticker: str,
        call: Callable[[DataProvider], list[T]],
    ) -> list[T]:
        """Try each provider; return the first non-empty list. Last resort: []."""
        for p in self.providers:
            try:
                result = call(p)
            except Exception as e:
                logger.debug("provider %s.%s(%s) raised: %s", p.name, method_name, ticker, e)
                continue
            if result:
                logger.info("%s for %s served by %s (%d rows)", method_name, ticker, p.name, len(result))
                return result
        logger.info("%s for %s: no provider returned data", method_name, ticker)
        return []

    # ------------------------------------------------------------------
    # Public surface — same six methods every provider implements.
    # ------------------------------------------------------------------
    def get_prices(self, ticker: str, start_date: str, end_date: str) -> list[Price]:
        return self._walk_list(
            "get_prices", ticker,
            lambda p: p.get_prices(ticker, start_date, end_date),
        )

    def get_financial_metrics(
        self, ticker: str, end_date: str, period: str = "ttm", limit: int = 10,
    ) -> list[FinancialMetrics]:
        return self._walk_list(
            "get_financial_metrics", ticker,
            lambda p: p.get_financial_metrics(ticker, end_date, period=period, limit=limit),
        )

    def search_line_items(
        self, ticker: str, line_items: list[str], end_date: str,
        period: str = "ttm", limit: int = 10,
    ) -> list[LineItem]:
        return self._walk_list(
            "search_line_items", ticker,
            lambda p: p.search_line_items(ticker, line_items, end_date, period=period, limit=limit),
        )

    def get_insider_trades(
        self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000,
    ) -> list[InsiderTrade]:
        return self._walk_list(
            "get_insider_trades", ticker,
            lambda p: p.get_insider_trades(ticker, end_date, start_date=start_date, limit=limit),
        )

    def get_company_news(
        self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000,
    ) -> list[CompanyNews]:
        return self._walk_list(
            "get_company_news", ticker,
            lambda p: p.get_company_news(ticker, end_date, start_date=start_date, limit=limit),
        )

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        """Scalar variant — first non-``None`` wins."""
        for p in self.providers:
            try:
                cap = p.get_market_cap(ticker, end_date)
            except Exception as e:
                logger.debug("provider %s.get_market_cap(%s) raised: %s", p.name, ticker, e)
                continue
            if cap is not None:
                logger.info("get_market_cap for %s served by %s ($%s)", ticker, p.name, cap)
                return cap
        return None
