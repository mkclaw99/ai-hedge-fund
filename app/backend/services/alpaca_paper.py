"""Read-only Alpaca **paper-trading** client.

Hard-coded to ``paper-api.alpaca.markets`` and gated by paper-specific credentials
(``ALPACA_PAPER_API_KEY_ID`` + ``ALPACA_PAPER_SECRET_KEY``), so there is no path
from this module to a LIVE Alpaca account. Anything more than reads (orders,
cancellations, …) is intentionally absent for now — this v1 is account status
only.

Fail-open: a missing key or network blip returns ``{"connected": False, ...}``;
callers render that as "not connected" rather than crashing.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

# Hard-coded paper endpoint — never expose this module to the live host.
_PAPER_BASE = "https://paper-api.alpaca.markets/v2"


def _headers(api_keys: dict | None) -> dict | None:
    keys = api_keys or {}
    kid = keys.get("ALPACA_PAPER_API_KEY_ID") or os.getenv("ALPACA_PAPER_API_KEY_ID")
    sec = keys.get("ALPACA_PAPER_SECRET_KEY") or os.getenv("ALPACA_PAPER_SECRET_KEY")
    if not kid or not sec:
        return None
    return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}


def _float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def get_account(api_keys: dict | None) -> dict:
    """Fetch the paper account summary (cash, equity, buying power, …)."""
    h = _headers(api_keys)
    if not h:
        return {
            "connected": False,
            "paper": True,
            "reason": "Set ALPACA_PAPER_API_KEY_ID and ALPACA_PAPER_SECRET_KEY in Settings → API Keys.",
        }
    try:
        r = requests.get(f"{_PAPER_BASE}/account", headers=h, timeout=10)
        if r.status_code == 200:
            a = r.json() or {}
            return {
                "connected": True,
                "paper": True,
                "account_number": a.get("account_number"),
                "currency": a.get("currency") or "USD",
                "cash": _float(a.get("cash")),
                "equity": _float(a.get("equity")),
                "buying_power": _float(a.get("buying_power")),
                "portfolio_value": _float(a.get("portfolio_value")),
                "status": a.get("status"),
                "pattern_day_trader": bool(a.get("pattern_day_trader")),
            }
        if r.status_code in (401, 403):
            return {"connected": False, "paper": True, "reason": "Alpaca rejected the paper credentials (401/403)."}
        return {"connected": False, "paper": True, "reason": f"Alpaca returned HTTP {r.status_code}"}
    except Exception as e:
        logger.warning("alpaca paper /account failed: %s", e)
        return {"connected": False, "paper": True, "reason": f"Could not reach paper-api: {e}"}


def get_positions(api_keys: dict | None) -> list[dict]:
    """List current paper-account positions (empty list on any failure)."""
    h = _headers(api_keys)
    if not h:
        return []
    try:
        r = requests.get(f"{_PAPER_BASE}/positions", headers=h, timeout=10)
        if r.status_code != 200:
            return []
        return [
            {
                "symbol": p.get("symbol"),
                "qty": _float(p.get("qty")),
                "avg_entry_price": _float(p.get("avg_entry_price")),
                "market_value": _float(p.get("market_value")),
                "unrealized_pl": _float(p.get("unrealized_pl")),
                "unrealized_plpc": _float(p.get("unrealized_plpc")),
                "current_price": _float(p.get("current_price")),
            }
            for p in (r.json() or [])
        ]
    except Exception as e:
        logger.warning("alpaca paper /positions failed: %s", e)
        return []
