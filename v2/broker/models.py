"""Pydantic models for the Alpaca Trading API (account, positions, orders).

Alpaca returns numeric fields as JSON strings (e.g. ``"cash": "1000.50"``);
Pydantic coerces these to ``float`` automatically. All models use
``extra="ignore"`` for forward-compatibility with new response fields.
"""

from __future__ import annotations

from pydantic import BaseModel


_IGNORE = {"extra": "ignore"}


class AlpacaAccount(BaseModel):
    """Account snapshot from ``GET /v2/account``."""

    model_config = _IGNORE

    id: str | None = None
    account_number: str | None = None
    status: str | None = None
    currency: str | None = None
    cash: float | None = None
    portfolio_value: float | None = None
    equity: float | None = None
    last_equity: float | None = None
    buying_power: float | None = None
    long_market_value: float | None = None
    short_market_value: float | None = None
    pattern_day_trader: bool | None = None
    trading_blocked: bool | None = None
    account_blocked: bool | None = None


class AlpacaPosition(BaseModel):
    """Open position from ``GET /v2/positions``."""

    model_config = _IGNORE

    symbol: str
    qty: float | None = None
    side: str | None = None
    avg_entry_price: float | None = None
    market_value: float | None = None
    cost_basis: float | None = None
    current_price: float | None = None
    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None
    asset_class: str | None = None


class AlpacaOrder(BaseModel):
    """Order from ``GET /v2/orders``."""

    model_config = _IGNORE

    id: str
    symbol: str | None = None
    side: str | None = None
    qty: float | None = None
    filled_qty: float | None = None
    type: str | None = None
    time_in_force: str | None = None
    status: str | None = None
    submitted_at: str | None = None
    filled_at: str | None = None
    limit_price: float | None = None
    filled_avg_price: float | None = None
