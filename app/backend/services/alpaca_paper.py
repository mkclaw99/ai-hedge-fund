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


def place_option_order(api_keys: dict | None, *, symbol_occ: str, side: str, qty) -> dict:
    """Submit a *market, day* option order on the PAPER account.

    ``symbol_occ`` is the OCC contract symbol from a Derivatives summary (e.g.
    ``AVAV260529C00205000``). Alpaca's options API uses the same ``/v2/orders``
    endpoint as stocks; the contract identity rides on the ``symbol`` field.

    ``side`` is ``"buy"`` (buy-to-open a long contract) or ``"sell"`` (sell-to-open
    a short contract). Closing trades are a follow-up — this v1 only opens.

    Returns ``{ok, symbol, side, qty, id?, status?, error?}``. Fail-open: invalid
    input or any Alpaca error (including missing options permission level on the
    paper account) returns ``ok=False`` with a human-readable error string. All
    paths remain inside the hard-coded paper host.
    """
    out = {
        "symbol": str(symbol_occ or "").upper(),
        "side": str(side or "").lower(),
        "qty": qty,
        "is_option": True,
    }
    h = _headers(api_keys)
    if not h:
        return {**out, "ok": False, "error": "no paper credentials"}
    if out["side"] not in ("buy", "sell"):
        return {**out, "ok": False, "error": f"invalid side: {side}"}
    if not out["symbol"]:
        return {**out, "ok": False, "error": "empty OCC symbol"}
    try:
        qty_int = int(float(qty))
    except (TypeError, ValueError):
        return {**out, "ok": False, "error": f"invalid qty: {qty}"}
    if qty_int <= 0:
        return {**out, "ok": False, "error": "qty must be > 0 contracts"}
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
            return {**out, "ok": True, "id": o.get("id"), "status": o.get("status"), "qty": o.get("qty") or qty_int}
        try:
            msg = (r.json() or {}).get("message") or r.text
        except Exception:
            msg = r.text
        # Friendlier error for the most common rejection reason.
        if r.status_code == 403 and "option" in (msg or "").lower():
            msg = (msg or "") + " — your paper account may not be options-enabled. Set the level on Alpaca's dashboard."
        return {**out, "ok": False, "error": f"Alpaca {r.status_code}: {(msg or '')[:240]}"}
    except Exception as e:
        return {**out, "ok": False, "error": str(e)[:240]}


def reset_account(api_keys: dict | None) -> dict:
    """Reset the PAPER account back to the $100,000 starting balance.

    POSTs to Alpaca's paper-only ``/v2/account/actions/reset`` endpoint —
    the same action the user can trigger from the Alpaca dashboard's
    "Reset" button. Wipes positions, cancels open orders, and resets cash
    and equity to $100k. Hard-coded to the paper host (line 21) so there
    is no path to a live account.

    Returns ``{ok, reason?}``. Fail-open: missing keys, network blip, or
    a non-2xx response all return ``ok=False`` with a human-readable
    reason; the caller surfaces that string to the user instead of
    crashing the request. Alpaca returns ``200`` with an empty body on
    success, so a successful return is ``{"ok": True}``.

    Reality check (verified 2026-06-01 against two fresh paper accounts,
    PA35AJ6HEDN3 and PA3LX5ABCWHF): Alpaca returns **404 across the board**
    for standard Trading API keys. The dashboard's Reset button goes
    through a separate Broker-API path that isn't exposed to user keys.
    The frontend treats 404 as the cue to open the dashboard in a new
    tab; we keep the service POST in place so a future Alpaca change
    would Just Work without touching the UI.
    """
    h = _headers(api_keys)
    if not h:
        return {"ok": False, "reason": "Set ALPACA_PAPER_API_KEY_ID and ALPACA_PAPER_SECRET_KEY in Settings → API Keys."}
    try:
        r = requests.post(f"{_PAPER_BASE}/account/actions/reset", headers=h, timeout=20)
        if r.status_code in (200, 201, 204):
            return {"ok": True}
        if r.status_code in (401, 403):
            return {"ok": False, "reason": "Alpaca rejected the paper credentials (401/403)."}
        if r.status_code == 404:
            return {"ok": False, "reason": "Reset endpoint not available for this paper account (404). Use the Alpaca dashboard instead."}
        try:
            msg = (r.json() or {}).get("message") or r.text
        except Exception:
            msg = r.text
        return {"ok": False, "reason": f"Alpaca {r.status_code}: {(msg or '')[:200]}"}
    except Exception as e:
        logger.warning("alpaca paper /account/actions/reset failed: %s", e)
        return {"ok": False, "reason": f"Could not reach paper-api: {e}"}


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


def get_portfolio_history(
    api_keys: dict | None,
    *,
    period: str = "1M",
    timeframe: str | None = None,
) -> dict:
    """Equity time-series for the paper account.

    Returns ``{connected, period, timeframe, base_value, samples}`` where
    ``samples`` is ``[{ts, equity, profit_loss, profit_loss_pct}, ...]`` in
    chronological order. Empty samples + ``connected=False`` on any failure.

    Period strings understood by Alpaca: ``1D``, ``5D``, ``7D``, ``1M``,
    ``3M``, ``6M``, ``1A``, ``5A``, ``all``. Timeframe defaults to ``1H``
    for sub-week periods and ``1D`` otherwise — picked to keep the sample
    count under a few hundred so the SVG chart stays cheap to draw.
    """
    h = _headers(api_keys)
    if not h:
        return {"connected": False, "paper": True, "samples": [], "period": period, "timeframe": timeframe or "1D"}
    if timeframe is None:
        timeframe = "1H" if period in ("1D", "5D", "7D") else "1D"
    try:
        r = requests.get(
            f"{_PAPER_BASE}/account/portfolio/history",
            headers=h,
            params={"period": period, "timeframe": timeframe, "extended_hours": "false"},
            timeout=10,
        )
        if r.status_code != 200:
            return {
                "connected": False, "paper": True, "samples": [],
                "period": period, "timeframe": timeframe,
                "reason": f"Alpaca returned HTTP {r.status_code}",
            }
        body = r.json() or {}
        timestamps = body.get("timestamp") or []
        equity = body.get("equity") or []
        pnl = body.get("profit_loss") or []
        pnl_pct = body.get("profit_loss_pct") or []
        base = _float(body.get("base_value"))
        samples = []
        for i, ts in enumerate(timestamps):
            eq = equity[i] if i < len(equity) else None
            if eq is None:
                continue  # Alpaca pads with nulls during market closures
            samples.append({
                "ts": int(ts),                                # unix seconds
                "equity": _float(eq),
                "profit_loss": _float(pnl[i]) if i < len(pnl) else 0.0,
                "profit_loss_pct": _float(pnl_pct[i]) if i < len(pnl_pct) else 0.0,
            })
        return {
            "connected": True, "paper": True,
            "period": period, "timeframe": timeframe,
            "base_value": base,
            "samples": samples,
        }
    except Exception as e:
        logger.warning("alpaca paper /account/portfolio/history failed: %s", e)
        return {
            "connected": False, "paper": True, "samples": [],
            "period": period, "timeframe": timeframe,
            "reason": f"Could not reach paper-api: {e}",
        }


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
