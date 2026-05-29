"""Data-provider Protocol — the contract every vendor implements.

The shape mirrors the public functions in :mod:`src.tools.api` (which is
what the agents call). Each method returns the same Pydantic model the
existing FD path returns, so callers don't care which vendor served the
data. Returning ``[]`` (or ``None`` for scalars) signals "I don't have
this" and the :class:`~chain.ProviderChain` falls through to the next
provider.

Providers MUST be exception-safe — a vendor outage should manifest as an
empty result, never as a bubbled-up exception. The chain treats both ``[]``
and a raised exception as "miss," but logging is cleaner when the provider
swallows its own errors and tells the chain what kind of miss it was.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)


@runtime_checkable
class DataProvider(Protocol):
    """The minimal interface every data vendor implements.

    The chain walks providers in order; the first one returning a non-empty
    result wins. A provider that doesn't support a given data type returns
    ``[]`` / ``None`` (see ``AlpacaProvider`` — no fundamentals).
    """

    #: Short human-readable name, used in INFO logs ("served by alpaca").
    name: str

    def get_prices(self, ticker: str, start_date: str, end_date: str) -> list[Price]:
        """Return daily OHLCV bars for ``ticker`` in ``[start_date, end_date]``."""
        ...

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]:
        """Return up to ``limit`` financial-metrics rows ending at ``end_date``."""
        ...

    def search_line_items(
        self,
        ticker: str,
        line_items: list[str],
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[LineItem]:
        """Return up to ``limit`` line-item rows containing the requested fields."""
        ...

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[InsiderTrade]:
        """Return insider transactions filed between ``start_date`` and ``end_date``."""
        ...

    def get_company_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]:
        """Return company news articles in the date window."""
        ...

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        """Return market capitalisation as of ``end_date``, or ``None``."""
        ...
