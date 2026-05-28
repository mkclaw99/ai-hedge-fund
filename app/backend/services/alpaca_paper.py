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
# Alpaca's market-data API (free tier accessible with the same paper key).
_DATA_BASE = "https://data.alpaca.markets/v2"


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
                "last_equity": _float(a.get("last_equity")),
                "buying_power": _float(a.get("buying_power")),
                "portfolio_value": _float(a.get("portfolio_value")),
                "long_market_value": _float(a.get("long_market_value")),
                "short_market_value": _float(a.get("short_market_value")),
                "status": a.get("status"),
                "pattern_day_trader": bool(a.get("pattern_day_trader")),
            }
        if r.status_code in (401, 403):
            return {"connected": False, "paper": True, "reason": "Alpaca rejected the paper credentials (401/403)."}
        return {"connected": False, "paper": True, "reason": f"Alpaca returned HTTP {r.status_code}"}
    except Exception as e:
        logger.warning("alpaca paper /account failed: %s", e)
        return {"connected": False, "paper": True, "reason": f"Could not reach paper-api: {e}"}


def place_order(api_keys: dict | None, *, symbol: str, side: str, qty) -> dict:
    """Submit a *market, day* order on the PAPER account.

    Returns ``{ok, symbol, side, qty, id?, status?, error?}``. Fail-open: an
    invalid input or network problem returns ``ok=False`` with an error string;
    callers continue with the next order. **All paths route through the
    hard-coded paper host** — there is no way to land on the live account.
    """
    out = {"symbol": str(symbol or "").upper(), "side": str(side or "").lower(), "qty": qty}
    h = _headers(api_keys)
    if not h:
        return {**out, "ok": False, "error": "no paper credentials"}
    if out["side"] not in ("buy", "sell"):
        return {**out, "ok": False, "error": f"invalid side: {side}"}
    try:
        qty_int = int(float(qty))
    except (TypeError, ValueError):
        return {**out, "ok": False, "error": f"invalid qty: {qty}"}
    if qty_int <= 0:
        return {**out, "ok": False, "error": "qty must be > 0"}
    if not out["symbol"]:
        return {**out, "ok": False, "error": "empty symbol"}
    body = {
        "symbol": out["symbol"],
        "qty": str(qty_int),
        "side": out["side"],
        "type": "market",
        "time_in_force": "day",
    }
    try:
        r = requests.post(f"{_PAPER_BASE}/orders", headers=h, json=body, timeout=15)
        if r.status_code in (200, 201):
            o = r.json() or {}
            return {
                **out,
                "ok": True,
                "id": o.get("id"),
                "status": o.get("status"),
                "qty": o.get("qty") or qty_int,
            }
        try:
            msg = (r.json() or {}).get("message") or r.text
        except Exception:
            msg = r.text
        return {**out, "ok": False, "error": f"Alpaca {r.status_code}: {(msg or '')[:200]}"}
    except Exception as e:
        return {**out, "ok": False, "error": str(e)[:200]}


def get_latest_prices(api_keys: dict | None, tickers) -> dict:
    """Batch-fetch the latest trade price for each ticker. Returns ``{symbol: price}``
    for symbols Alpaca returned a trade for; others are simply omitted (caller
    should skip those tickers). Fail-open."""
    h = _headers(api_keys)
    syms = [str(t).upper() for t in (tickers or []) if t]
    if not h or not syms:
        return {}
    try:
        url = f"{_DATA_BASE}/stocks/trades/latest?symbols={','.join(syms)}"
        r = requests.get(url, headers=h, timeout=10)
        if r.status_code != 200:
            return {}
        data = (r.json() or {}).get("trades") or {}
        out: dict[str, float] = {}
        for sym, trade in data.items():
            p = (trade or {}).get("p")
            if p is None:
                continue
            try:
                out[sym.upper()] = float(p)
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:
        logger.warning("alpaca data /trades/latest failed: %s", e)
        return {}


def get_orders(api_keys: dict | None, status: str = "all", limit: int = 50) -> list[dict]:
    """Recent paper-account orders (filled, open, cancelled, …). Empty on failure."""
    h = _headers(api_keys)
    if not h:
        return []
    try:
        r = requests.get(
            f"{_PAPER_BASE}/orders",
            headers=h,
            params={"status": status, "limit": limit, "direction": "desc"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        return [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "qty": _float(o.get("qty")),
                "filled_qty": _float(o.get("filled_qty")),
                "filled_avg_price": _float(o.get("filled_avg_price")),
                "status": o.get("status"),
                "type": o.get("type"),
                "submitted_at": o.get("submitted_at"),
                "filled_at": o.get("filled_at"),
            }
            for o in (r.json() or [])
        ]
    except Exception as e:
        logger.warning("alpaca paper /orders failed: %s", e)
        return []


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
