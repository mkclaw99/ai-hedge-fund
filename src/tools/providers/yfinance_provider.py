"""Yahoo Finance — the universal fallback.

Covers all six data types (prices, financial metrics, line items, insider
trades, news, market cap) by adapting Yahoo Finance's DataFrames/dicts to
the shared :mod:`src.data.models` shapes.

A note on trust:

* **Prices, market cap, news** — schemas are equivalent or trivially
  derivable; substitution is lossless.

* **Financial metrics, line items** — Yahoo's field names don't map 1:1 to
  Financial Datasets's GAAP labels. We map the most-used fields explicitly
  (see ``_METRIC_INFO_FIELDS`` and ``_LINE_ITEM_MAP``) and leave the rest
  as ``None``. That's deliberate: analysts that rely on a specific
  unmapped field will see ``None`` and either branch on it or output a
  "missing data" verdict — preferable to silently feeding them
  yfinance's equivalent-named-but-different-quantity value.

* **Insider trades** — Yahoo only exposes the most recent ~20 transactions
  without filing dates as ISO timestamps; we map what we have.

Yahoo Finance is scraped, no key, no rate limit announced — but
intermittent blockers / shape changes do happen. Treat its outputs as
"best effort, last resort."
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    InsiderTrade,
    LineItem,
    Price,
)

logger = logging.getLogger(__name__)


# yfinance Ticker.info keys we map directly into FinancialMetrics. Yahoo's
# `info` is roughly TTM. We mostly leave growth/per-share/turnover fields
# unmapped (set to None) because Yahoo's equivalents either don't exist or
# carry different definitions — better to under-report than to mis-report.
_METRIC_INFO_FIELDS: dict[str, str] = {
    "market_cap": "marketCap",
    "enterprise_value": "enterpriseValue",
    "price_to_earnings_ratio": "trailingPE",
    "price_to_book_ratio": "priceToBook",
    "price_to_sales_ratio": "priceToSalesTrailing12Months",
    "enterprise_value_to_ebitda_ratio": "enterpriseToEbitda",
    "enterprise_value_to_revenue_ratio": "enterpriseToRevenue",
    "peg_ratio": "trailingPegRatio",
    "gross_margin": "grossMargins",
    "operating_margin": "operatingMargins",
    "net_margin": "profitMargins",
    "return_on_equity": "returnOnEquity",
    "return_on_assets": "returnOnAssets",
    "current_ratio": "currentRatio",
    "quick_ratio": "quickRatio",
    "debt_to_equity": "debtToEquity",
    "revenue_growth": "revenueGrowth",
    "earnings_growth": "earningsGrowth",
    "earnings_per_share": "trailingEps",
    "book_value_per_share": "bookValue",
    "payout_ratio": "payoutRatio",
}


# Map FD line-item names to the Yahoo Finance DataFrame row labels.
# Each value is (statement, row_label) where statement is one of
# 'financials', 'balance_sheet', 'cashflow'.
_LINE_ITEM_MAP: dict[str, tuple[str, str]] = {
    # Income statement
    "revenue": ("financials", "Total Revenue"),
    "total_revenue": ("financials", "Total Revenue"),
    "cost_of_revenue": ("financials", "Cost Of Revenue"),
    "gross_profit": ("financials", "Gross Profit"),
    "operating_income": ("financials", "Operating Income"),
    "operating_expense": ("financials", "Operating Expense"),
    "ebit": ("financials", "EBIT"),
    "ebitda": ("financials", "EBITDA"),
    "net_income": ("financials", "Net Income"),
    "interest_expense": ("financials", "Interest Expense"),
    "research_and_development": ("financials", "Research And Development"),
    "earnings_per_share": ("financials", "Diluted EPS"),
    "diluted_average_shares": ("financials", "Diluted Average Shares"),
    "outstanding_shares": ("financials", "Diluted Average Shares"),
    "shares_outstanding": ("financials", "Diluted Average Shares"),
    # Balance sheet
    "total_assets": ("balance_sheet", "Total Assets"),
    "total_liabilities": ("balance_sheet", "Total Liabilities Net Minority Interest"),
    "current_assets": ("balance_sheet", "Current Assets"),
    "current_liabilities": ("balance_sheet", "Current Liabilities"),
    "cash_and_equivalents": ("balance_sheet", "Cash And Cash Equivalents"),
    "inventory": ("balance_sheet", "Inventory"),
    "total_debt": ("balance_sheet", "Total Debt"),
    "long_term_debt": ("balance_sheet", "Long Term Debt"),
    "shareholders_equity": ("balance_sheet", "Stockholders Equity"),
    "stockholders_equity": ("balance_sheet", "Stockholders Equity"),
    "working_capital": ("balance_sheet", "Working Capital"),
    "retained_earnings": ("balance_sheet", "Retained Earnings"),
    "goodwill_and_intangible_assets": ("balance_sheet", "Goodwill And Other Intangible Assets"),
    # Cash flow
    "operating_cash_flow": ("cashflow", "Operating Cash Flow"),
    "free_cash_flow": ("cashflow", "Free Cash Flow"),
    "capital_expenditure": ("cashflow", "Capital Expenditure"),
    "depreciation_and_amortization": ("cashflow", "Depreciation And Amortization"),
    "dividends_and_other_cash_distributions": ("cashflow", "Cash Dividends Paid"),
    "issuance_or_purchase_of_equity_shares": ("cashflow", "Issuance Of Capital Stock"),
}


def _safe_float(v: Any) -> float | None:
    """Convert Yahoo's numpy/decimal/NaN soup into a clean ``float | None``."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN check (NaN != NaN)
        return None
    return f


class YFinanceProvider:
    """Yahoo Finance via the ``yfinance`` package."""

    name = "yfinance"

    def __init__(self) -> None:
        # Lazy import — yfinance is heavy and may not be available in every
        # deployment. If it's missing, every method short-circuits to empty
        # and the chain returns whatever the previous provider had.
        try:
            import yfinance as yf  # noqa: F401
            self._yf = yf
        except Exception as e:
            logger.warning("yfinance unavailable, fallback provider disabled: %s", e)
            self._yf = None

    @property
    def _available(self) -> bool:
        return self._yf is not None

    def _ticker(self, symbol: str):
        return self._yf.Ticker(symbol)

    # ------------------------------------------------------------------
    def get_prices(self, ticker: str, start_date: str, end_date: str) -> list[Price]:
        if not self._available:
            return []
        try:
            # auto_adjust=False keeps raw OHLC (matching FD's behaviour);
            # actions=False drops the dividend/split columns.
            hist = self._yf.Ticker(ticker).history(
                start=start_date, end=end_date, interval="1d",
                auto_adjust=False, actions=False,
            )
        except Exception as e:
            logger.debug("yfinance prices failed for %s: %s", ticker, e)
            return []
        if hist is None or hist.empty:
            return []
        out: list[Price] = []
        for ts, row in hist.iterrows():
            try:
                out.append(Price(
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row.get("Volume") or 0),
                    time=ts.strftime("%Y-%m-%d"),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------
    def get_financial_metrics(
        self, ticker: str, end_date: str, period: str = "ttm", limit: int = 10,
    ) -> list[FinancialMetrics]:
        """One TTM row built from ``Ticker.info``; ``limit`` is honoured but
        Yahoo doesn't expose historical TTM series so we only ever return 1.
        """
        if not self._available:
            return []
        try:
            info = self._ticker(ticker).info
        except Exception as e:
            logger.debug("yfinance info failed for %s: %s", ticker, e)
            return []
        if not info:
            return []

        kwargs: dict[str, Any] = {
            "ticker": ticker,
            "report_period": end_date,
            "period": "ttm",  # info is TTM regardless of caller request
            "currency": info.get("financialCurrency") or info.get("currency") or "USD",
        }
        # Set every metric field to None first so we materialise the full
        # model shape (Pydantic v2 requires all declared fields).
        for fld in FinancialMetrics.model_fields:
            if fld not in kwargs:
                kwargs[fld] = None
        # Then map the ones we know.
        for fd_field, yf_key in _METRIC_INFO_FIELDS.items():
            kwargs[fd_field] = _safe_float(info.get(yf_key))
        try:
            return [FinancialMetrics(**kwargs)]
        except Exception as e:
            logger.debug("yfinance metric model build failed for %s: %s", ticker, e)
            return []

    # ------------------------------------------------------------------
    def search_line_items(
        self, ticker: str, line_items: list[str], end_date: str,
        period: str = "ttm", limit: int = 10,
    ) -> list[LineItem]:
        """Build ``LineItem`` rows from Yahoo's quarterly/annual statements.

        Yahoo statements come as DataFrames with dates as columns. We line
        items up against the requested fields and return up to ``limit``
        rows newest-first. Fields we don't have a mapping for are left
        unset on the LineItem (which has ``extra="allow"``, so callers see
        them as missing rather than as wrong values).
        """
        if not self._available:
            return []
        try:
            t = self._ticker(ticker)
            quarterly = period in ("quarterly", "q", "qtr")
            statements = {
                "financials": (t.quarterly_financials if quarterly else t.financials),
                "balance_sheet": (t.quarterly_balance_sheet if quarterly else t.balance_sheet),
                "cashflow": (t.quarterly_cashflow if quarterly else t.cashflow),
            }
        except Exception as e:
            logger.debug("yfinance statements failed for %s: %s", ticker, e)
            return []

        # Yahoo orders columns newest → oldest. Iterate to build per-period rows.
        any_df = next((df for df in statements.values() if df is not None and not df.empty), None)
        if any_df is None:
            return []
        periods = list(any_df.columns)[:limit]

        currency = "USD"
        try:
            currency = self._ticker(ticker).info.get("financialCurrency") or "USD"
        except Exception:
            pass

        out: list[LineItem] = []
        for col in periods:
            row: dict[str, Any] = {
                "ticker": ticker,
                "report_period": col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col),
                "period": "quarterly" if quarterly else "annual",
                "currency": currency,
            }
            for field in line_items:
                mapping = _LINE_ITEM_MAP.get(field.lower())
                if mapping is None:
                    row[field] = None
                    continue
                stmt_name, label = mapping
                df = statements.get(stmt_name)
                if df is None or df.empty or label not in df.index or col not in df.columns:
                    row[field] = None
                    continue
                row[field] = _safe_float(df.at[label, col])
            try:
                out.append(LineItem(**row))
            except Exception:
                continue
        return out

    # ------------------------------------------------------------------
    def get_insider_trades(
        self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000,
    ) -> list[InsiderTrade]:
        if not self._available:
            return []
        try:
            df = self._ticker(ticker).insider_transactions
        except Exception as e:
            logger.debug("yfinance insider_transactions failed for %s: %s", ticker, e)
            return []
        if df is None or df.empty:
            return []
        # Yahoo's columns: Insider, Position, Most Recent Transaction, Shares, Value, Ownership, Start Date
        # Names vary across yfinance versions — be defensive.
        try:
            df = df.reset_index()
        except Exception:
            return []
        out: list[InsiderTrade] = []
        for _, row in df.iterrows():
            try:
                shares = _safe_float(row.get("Shares"))
                value = _safe_float(row.get("Value"))
                txn_date = row.get("Start Date") or row.get("Most Recent Transaction")
                txn_iso = str(txn_date)[:10] if txn_date is not None else None
                if start_date and txn_iso and txn_iso < start_date:
                    continue
                if end_date and txn_iso and txn_iso > end_date:
                    continue
                price = (value / shares) if (shares and value) else None
                out.append(InsiderTrade(
                    ticker=ticker,
                    issuer=row.get("Insider"),
                    name=row.get("Insider"),
                    title=row.get("Position"),
                    is_board_director=None,
                    transaction_date=txn_iso,
                    transaction_shares=shares,
                    transaction_price_per_share=price,
                    transaction_value=value,
                    shares_owned_before_transaction=None,
                    shares_owned_after_transaction=None,
                    security_title=None,
                    filing_date=txn_iso or end_date,
                ))
                if len(out) >= limit:
                    break
            except Exception:
                continue
        return out

    # ------------------------------------------------------------------
    def get_company_news(
        self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000,
    ) -> list[CompanyNews]:
        if not self._available:
            return []
        try:
            news = self._ticker(ticker).news
        except Exception as e:
            logger.debug("yfinance news failed for %s: %s", ticker, e)
            return []
        if not news:
            return []
        out: list[CompanyNews] = []
        for item in news[:limit]:
            content = item.get("content") if isinstance(item, dict) else None
            # yfinance switched to a nested {"content": {...}} shape; handle both.
            data = content if isinstance(content, dict) else item if isinstance(item, dict) else {}
            ts = data.get("pubDate") or data.get("providerPublishTime")
            date_iso = ""
            try:
                if isinstance(ts, (int, float)):
                    date_iso = datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
                elif isinstance(ts, str):
                    date_iso = ts[:10]
            except Exception:
                pass
            if start_date and date_iso and date_iso < start_date:
                continue
            if end_date and date_iso and date_iso > end_date:
                continue
            link_obj = data.get("canonicalUrl") or data.get("clickThroughUrl") or {}
            url = link_obj.get("url") if isinstance(link_obj, dict) else (link_obj or "")
            provider = data.get("provider") or {}
            source = provider.get("displayName") if isinstance(provider, dict) else (data.get("publisher") or "yahoo")
            try:
                out.append(CompanyNews(
                    ticker=ticker,
                    title=data.get("title") or "",
                    source=source or "yahoo",
                    date=date_iso or end_date,
                    url=url or "",
                ))
            except Exception:
                continue
        return out

    # ------------------------------------------------------------------
    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        if not self._available:
            return None
        try:
            info = self._ticker(ticker).info
        except Exception:
            return None
        return _safe_float((info or {}).get("marketCap"))
