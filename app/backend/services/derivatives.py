"""Derivatives-data access for the Strategy node.

The Strategy node lets the user declare which instruments the PM is allowed
to consider (stocks / options / ETFs). When **options** is on, we pull the
options chain for every ticker in the run universe from Alpaca's free
options-data endpoint and feed a compact summary into the PM's prompt.

Why a compact summary, not the whole chain: a typical chain has dozens of
expiries × hundreds of strikes — feeding it raw would torch the context
window and drown the signal in numbers. The PM needs to *know what's
available and how priced* (is this name optionable? what's IV? what's a
sensible ATM call/put?), not analyse every strike. The summary is shaped
for that decision: optionable y/n · expiry count · nearest expiry's
ATM call+put · 30-day IV estimate · OI total · put/call ratio.

ETFs: the toggle is currently a *hint* to the PM (no free ETF-discovery
API on hand). Futures: out of scope for single-name equities.

Fail-open: any failure returns ``{"optionable": False, "reason": ...}``
so a missing options entitlement / network blip never breaks the run.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

# Alpaca's free options data API. Uses the same paper credentials as the
# Trading Account — no separate sign-up.
_DATA_BASE = "https://data.alpaca.markets/v1beta1"


def _headers(api_keys):
    keys = api_keys or {}
    kid = keys.get("ALPACA_PAPER_API_KEY_ID") or os.getenv("ALPACA_PAPER_API_KEY_ID")
    sec = keys.get("ALPACA_PAPER_SECRET_KEY") or os.getenv("ALPACA_PAPER_SECRET_KEY")
    if not kid or not sec:
        return None
    return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _nearest_expiry_chain(snapshots):
    """From Alpaca's `{"snapshots": {OCC_SYMBOL: {...}}}`, return a list of (symbol, snap)
    for the contracts that share the soonest expiry. Falls back to all if dates can't be
    parsed. OCC symbols are formatted like ``AAPL250620C00170000`` — the YYMMDD chunk
    comes right after the underlying root.
    """
    if not isinstance(snapshots, dict) or not snapshots:
        return []

    # Parse expiry from the OCC symbol's YYMMDD chunk; resilient to oddities.
    def _expiry(sym: str):
        try:
            # Skip the leading alpha root, then 6 digits = YYMMDD.
            i = 0
            while i < len(sym) and sym[i].isalpha():
                i += 1
            ymd = sym[i:i + 6]
            if len(ymd) != 6 or not ymd.isdigit():
                return None
            yy = int(ymd[:2]); mm = int(ymd[2:4]); dd = int(ymd[4:6])
            return date(2000 + yy, mm, dd)
        except Exception:
            return None

    today = date.today()
    by_expiry: dict[date, list] = {}
    for sym, snap in snapshots.items():
        e = _expiry(sym)
        if e is None or e < today:
            continue
        by_expiry.setdefault(e, []).append((sym, snap))
    if not by_expiry:
        return []
    nearest = min(by_expiry.keys())
    return by_expiry[nearest]


def _atm_call_put(chain, *, spot):
    """Pick the ATM call and put from a single-expiry chain.

    OCC strike is the last 8 chars of the symbol divided by 1000 (Alpaca's encoding).
    Returns ``(call, put, expiry_iso)`` where each is ``{strike, bid, ask, iv}`` or None.
    """
    def _strike(sym):
        try:
            return int(sym[-8:]) / 1000.0
        except Exception:
            return None

    calls, puts = [], []
    expiry_iso = None
    for sym, snap in chain:
        side = "call" if "C" in sym[-9:-8] else ("put" if "P" in sym[-9:-8] else None)
        if not side:
            continue
        strike = _strike(sym)
        if strike is None:
            continue
        quote = (snap or {}).get("latestQuote") or {}
        greeks = (snap or {}).get("greeks") or {}
        entry = {
            "symbol": sym,
            "strike": strike,
            "bid": _f(quote.get("bp")),
            "ask": _f(quote.get("ap")),
            "iv": _f((snap or {}).get("impliedVolatility")) or _f(greeks.get("impliedVolatility")),
        }
        (calls if side == "call" else puts).append(entry)
        # Read expiry off any symbol once.
        if expiry_iso is None:
            try:
                i = 0
                while i < len(sym) and sym[i].isalpha():
                    i += 1
                ymd = sym[i:i + 6]
                expiry_iso = f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}"
            except Exception:
                pass

    if not calls and not puts:
        return None, None, expiry_iso

    if spot is None or spot <= 0:
        # No spot: pick the median-strike call/put.
        calls.sort(key=lambda e: e["strike"]); puts.sort(key=lambda e: e["strike"])
        c = calls[len(calls) // 2] if calls else None
        p = puts[len(puts) // 2] if puts else None
    else:
        c = min(calls, key=lambda e: abs(e["strike"] - spot)) if calls else None
        p = min(puts, key=lambda e: abs(e["strike"] - spot)) if puts else None
    return c, p, expiry_iso


def get_options_summary(ticker: str, api_keys) -> dict:
    """Compact, prompt-friendly options-chain summary for one ticker.

    Returns a dict shaped for the PM prompt. Fail-open: any error path returns
    ``{"optionable": False, "reason": "..."}`` so the run never breaks over an
    options data hiccup.
    """
    sym = str(ticker or "").upper()
    if not sym:
        return {"optionable": False, "reason": "empty ticker"}
    h = _headers(api_keys)
    if not h:
        return {"optionable": False, "reason": "no Alpaca paper credentials"}

    # 1. Get current spot for ATM picking — reuse the existing latest-trade endpoint.
    spot = None
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/trades/latest?symbols={sym}",
            headers=h, timeout=8,
        )
        if r.status_code == 200:
            spot = _f(((r.json() or {}).get("trades") or {}).get(sym, {}).get("p"))
    except Exception:
        spot = None

    # 2. Pull the chain (Alpaca returns a paginated `snapshots` dict). One page is
    # plenty for a summary — we only care about the nearest expiry.
    try:
        r = requests.get(
            f"{_DATA_BASE}/options/snapshots/{sym}",
            headers=h, timeout=15,
        )
    except Exception as e:
        return {"optionable": False, "reason": f"chain fetch failed: {e}"}

    if r.status_code == 404:
        return {"optionable": False, "reason": "no chain (likely non-optionable)"}
    if r.status_code in (401, 403):
        return {"optionable": False, "reason": "options data forbidden (paper plan?)"}
    if r.status_code != 200:
        return {"optionable": False, "reason": f"Alpaca {r.status_code}"}

    try:
        payload = r.json() or {}
    except Exception:
        return {"optionable": False, "reason": "non-JSON response"}

    snapshots = payload.get("snapshots") or {}
    if not snapshots:
        return {"optionable": False, "reason": "empty snapshots"}

    # 3. Distill: expiry count, ATM call/put on the nearest expiry, rough IV.
    expiries = set()
    iv_samples = []
    oi_total = 0
    call_oi = put_oi = 0
    for sym_occ, snap in snapshots.items():
        try:
            i = 0
            while i < len(sym_occ) and sym_occ[i].isalpha():
                i += 1
            expiries.add(sym_occ[i:i + 6])
        except Exception:
            pass
        iv = _f((snap or {}).get("impliedVolatility")) or _f(((snap or {}).get("greeks") or {}).get("impliedVolatility"))
        if iv is not None:
            iv_samples.append(iv)
        oi = _f(((snap or {}).get("openInterest")) or 0, default=0) or 0
        oi_total += oi
        side_char = sym_occ[-9:-8]
        if side_char == "C":
            call_oi += oi
        elif side_char == "P":
            put_oi += oi

    chain = _nearest_expiry_chain(snapshots)
    atm_call, atm_put, expiry_iso = _atm_call_put(chain, spot=spot)

    median_iv = None
    if iv_samples:
        iv_samples.sort()
        median_iv = iv_samples[len(iv_samples) // 2]

    return {
        "optionable": True,
        "spot": spot,
        "expiry_count": len(expiries),
        "nearest_expiry": expiry_iso,
        "open_interest_total": int(oi_total),
        "put_call_oi_ratio": round(put_oi / call_oi, 2) if call_oi > 0 else None,
        "iv_median": round(median_iv, 4) if median_iv is not None else None,
        "atm_call": atm_call,
        "atm_put": atm_put,
    }


def summarize_for_prompt(summary: dict, ticker: str) -> str:
    """Render a one-paragraph human-readable line for the PM prompt."""
    if not summary or not summary.get("optionable"):
        return f"- **{ticker}**: not optionable ({(summary or {}).get('reason', 'unknown')})"
    bits = [f"- **{ticker}**: optionable"]
    if summary.get("expiry_count"):
        bits.append(f"{summary['expiry_count']} expiries")
    if summary.get("nearest_expiry"):
        bits.append(f"nearest {summary['nearest_expiry']}")
    if summary.get("iv_median") is not None:
        bits.append(f"median IV {summary['iv_median'] * 100:.1f}%")
    if summary.get("open_interest_total"):
        bits.append(f"OI {summary['open_interest_total']:,}")
    if summary.get("put_call_oi_ratio") is not None:
        bits.append(f"P/C OI {summary['put_call_oi_ratio']:.2f}")
    line = "; ".join(bits)
    c = summary.get("atm_call") or {}
    p = summary.get("atm_put") or {}
    if c or p:
        atm = []
        if c:
            atm.append(f"ATM call @ {c.get('strike')}: bid {c.get('bid')}/ask {c.get('ask')}")
        if p:
            atm.append(f"ATM put @ {p.get('strike')}: bid {p.get('bid')}/ask {p.get('ask')}")
        line += " — " + " · ".join(atm)
    return line
