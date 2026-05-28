"""Resolve stock tickers to company names, with a small on-disk cache.

Sources, in order: an on-disk cache (``wiki/_ticker_names.json``, gitignored),
then Financial Datasets' ``/company/facts/`` endpoint. Tickers we can't resolve
simply aren't returned (callers fall back to showing the bare ticker). Fail-open
throughout — a network blip never breaks the UI.
"""

import json
import logging
import os
import threading
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()

_BASE = os.environ.get("WIKI_MEMORY_DIR") or "wiki"
_PATH = Path(os.environ.get("TICKER_NAMES_PATH") or (Path(_BASE) / "_ticker_names.json"))


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception:
        pass


def _clean(name: str | None) -> str | None:
    """Just trim — keep the corporate suffix (Apple Inc, Coherent Corp, …) verbatim."""
    if not name:
        return None
    s = str(name).strip().rstrip(",").strip()
    return s or None


def _fetch_name(ticker: str, api_key: str | None) -> str | None:
    headers = {"X-API-KEY": api_key} if api_key else {}
    try:
        r = requests.get(
            f"https://api.financialdatasets.ai/company/facts/?ticker={ticker}",
            headers=headers,
            timeout=10,
        )
        if r.status_code == 200:
            cf = (r.json() or {}).get("company_facts") or {}
            return _clean(cf.get("name"))
    except Exception as e:
        logger.warning("ticker name lookup failed for %s: %s", ticker, e)
    return None


def resolve(tickers, api_keys: dict | None = None) -> dict:
    """Return ``{ticker: name}`` for tickers we can resolve (others omitted)."""
    norm = [str(t).strip().upper() for t in (tickers or []) if t and str(t).strip()]
    if not norm:
        return {}

    with _LOCK:
        cache = _load()

    out: dict[str, str] = {}
    miss: list[str] = []
    for t in norm:
        # Clean on read too, so any pre-existing cache entries with corporate
        # suffixes also render cleanly without needing a rebuild.
        cached = _clean(cache.get(t))
        if cached:
            out[t] = cached
        else:
            miss.append(t)

    if miss:
        api_key = (api_keys or {}).get("FINANCIAL_DATASETS_API_KEY") or os.environ.get(
            "FINANCIAL_DATASETS_API_KEY"
        )
        fetched: dict[str, str] = {}
        for t in miss:
            name = _fetch_name(t, api_key)
            if name:
                fetched[t] = name
                out[t] = name
        if fetched:
            with _LOCK:
                merged = _load()
                merged.update(fetched)
                _save(merged)

    return out
