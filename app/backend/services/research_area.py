"""Theme → tradable universe discovery for research areas.

Given an analyst theme slug, pull its value-chain companies (via the analyst MCP),
then normalize + validate their tickers against Financial Datasets so the hedge
analysts only run on companies that actually resolve to a tradable US security
*whose name matches*. analyst's value-chain tickers are unvalidated and often
wrong / delisted / foreign / private (e.g. ``SNCR`` is labelled "Sierra Nevada"
but resolves to *Synchronoss*), so the name-match guard prevents analysing the
wrong company. Returns the picked universe + dropped (with reasons) so the choice
is transparent.
"""

import asyncio
import datetime
import logging
import os
import re

import requests

from app.backend.services import analyst_mcp
from src.tools.api import get_prices

logger = logging.getLogger(__name__)

_EXCH_PREFIX = re.compile(r"^[A-Z]+:")          # "NASDAQ:AVAV" -> "AVAV"
_FOREIGN_SUFFIX = re.compile(r"\.[A-Z]{2,}$")   # ".AX" ".SW" ".DE" ".SS" ".L"
_US_SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")  # allow class shares like BRK.B
_MAX_VALIDATIONS = 40  # cap FD calls per discovery

_STOP = {
    "inc", "corp", "corporation", "co", "ltd", "llc", "plc", "group", "holdings",
    "the", "systems", "system", "technologies", "technology", "company", "ag",
    "sa", "nv", "gmbh", "and", "&",
}


def _normalize_ticker(raw: str) -> str | None:
    if not raw:
        return None
    t = _EXCH_PREFIX.sub("", raw.strip().upper())
    if _FOREIGN_SUFFIX.search(t):
        return None
    return t if _US_SYMBOL.match(t) else None


def _name_tokens(name: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {t for t in toks if t not in _STOP and len(t) > 1}


def _names_match(a: str, b: str) -> bool:
    ta, tb = _name_tokens(a), _name_tokens(b)
    return bool(ta and tb and (ta & tb))


def _currently_trades(ticker: str) -> bool:
    """True if the ticker has a price bar in the last ~45 days (filters out
    delisted/acquired names that Financial Datasets still has historical facts
    for, e.g. FLIR, MXIM, XLNX)."""
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=45)).isoformat()
    try:
        return bool(get_prices(ticker, start, today.isoformat()))
    except Exception as e:
        logger.warning("FD recent-price check failed for %s: %s", ticker, e)
        return False


def _fd_facts(ticker: str) -> dict | None:
    """Company facts from Financial Datasets (name + market cap), or None."""
    key = os.environ.get("FINANCIAL_DATASETS_API_KEY")
    headers = {"X-API-KEY": key} if key else {}
    try:
        r = requests.get(
            f"https://api.financialdatasets.ai/company/facts/?ticker={ticker}",
            headers=headers, timeout=15,
        )
        if r.status_code == 200:
            cf = (r.json() or {}).get("company_facts") or {}
            if cf.get("name"):
                return cf
    except Exception as e:
        logger.warning("FD facts failed for %s: %s", ticker, e)
    return None


def _validate(rows: list[dict], max_companies: int) -> dict:
    """Blocking: walk exposure-ranked rows, keep name-matched FD-covered tickers."""
    picked, dropped, seen = [], [], set()
    attempts = 0
    for r in rows:
        if len(picked) >= max_companies or attempts >= _MAX_VALIDATIONS:
            break
        raw, name = r.get("ticker"), r.get("name") or ""
        norm = _normalize_ticker(raw or "")
        if not norm:
            dropped.append({"ticker": raw, "name": name, "reason": "no US ticker (foreign/private/blank)"})
            continue
        if norm in seen:
            continue
        seen.add(norm)
        attempts += 1
        if not _currently_trades(norm):
            dropped.append({"ticker": norm, "name": name, "reason": "not currently trading (delisted/acquired/no data)"})
            continue
        facts = _fd_facts(norm)
        if not facts:
            dropped.append({"ticker": norm, "name": name, "reason": "no Financial Datasets coverage"})
            continue
        fd_name = facts.get("name") or ""
        if not _names_match(name, fd_name):
            dropped.append({"ticker": norm, "name": name,
                            "reason": f"resolves to a different company ({fd_name})"})
            continue
        picked.append({
            "ticker": norm, "name": name, "fd_name": fd_name,
            "exposure": r.get("theme_exposure_pct"), "role": r.get("role"),
            "country": r.get("country"),
        })
    return {"picked": picked, "dropped": dropped}


async def discover_universe(theme: str, *, max_companies: int = 10) -> dict:
    """Resolve a theme to a validated, tradable universe.

    Returns ``{theme, tickers, picked, dropped, error}``. Fail-open: ``error`` is
    set (and tickers empty) when analyst is unavailable.
    """
    res = await analyst_mcp.list_theme_companies(theme, limit=200)
    if res.get("error"):
        return {"theme": theme, "tickers": [], "picked": [], "dropped": [], "error": res["error"]}

    rows = [r for r in res.get("items", []) if r.get("is_public")]
    rows.sort(key=lambda r: (r.get("theme_exposure_pct") or 0), reverse=True)

    # FD validation is blocking (requests) — keep it off the event loop.
    out = await asyncio.to_thread(_validate, rows, max_companies)
    return {
        "theme": theme,
        "tickers": [p["ticker"] for p in out["picked"]],
        "picked": out["picked"],
        "dropped": out["dropped"],
        "error": None,
    }
