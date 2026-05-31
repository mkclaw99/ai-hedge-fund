"""Resolve stock tickers to company names, with a small on-disk cache.

Sources, in order: an in-memory cache → an on-disk JSON cache
(``wiki/_ticker_names.json``, gitignored) → Financial Datasets'
``/company/facts/`` endpoint. Tickers we can't resolve simply aren't
returned (callers fall back to showing the bare ticker). Fail-open
throughout — a network blip never breaks the UI.

The in-memory layer is lazy: the JSON file is read from disk *once* per
process, on the first ``resolve()`` call. Subsequent calls read from
memory, and we only touch the disk again to *persist new entries* after
a successful upstream fetch. Previously ``resolve()`` read the entire
file on every call — fine for a few entries, but a noticeable hot-path
allocation when the cache grows and the UI hits it from multiple
dialogs (memory, track record, agent-output, trading).
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

# In-memory cache. ``None`` until first ``resolve()`` triggers a load
# from disk; afterwards every call reads from this dict, and we only
# touch the disk again to persist newly-fetched names.
_cache: dict[str, str] | None = None


def _ensure_loaded() -> dict[str, str]:
    """Lazily populate the in-memory cache from disk on first access.

    Double-checked locking so concurrent first-callers don't both load.
    Returns the live dict so callers can read freely; mutations must go
    through ``_LOCK`` to stay consistent with writers.
    """
    global _cache
    if _cache is not None:
        return _cache
    with _LOCK:
        if _cache is None:
            try:
                _cache = json.loads(_PATH.read_text(encoding="utf-8"))
            except Exception:
                # Missing file, bad JSON, no permission — fall back to empty.
                # The cache will populate as resolve() hits FD for misses.
                _cache = {}
    return _cache


def _persist(snapshot: dict[str, str]) -> None:
    """Write *snapshot* to disk atomically. Best-effort — a failed
    persist just means we'll re-fetch the new names next process; the
    in-memory cache keeps serving them this session regardless."""
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
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

    cache = _ensure_loaded()

    out: dict[str, str] = {}
    miss: list[str] = []
    # Read under the lock — readers are quick, the lock is uncontended
    # in practice (FastAPI's executor pool runs requests in parallel but
    # this slice is microseconds).
    with _LOCK:
        for t in norm:
            # Clean on read too, so any pre-existing entries with corporate
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
            # Merge into the in-memory dict, snapshot under lock, persist
            # outside the lock — the atomic rename in _persist makes the
            # disk-side safe; we just don't want resolve()s blocking on
            # I/O while another thread is fetching.
            with _LOCK:
                cache.update(fetched)
                snapshot = dict(cache)
            _persist(snapshot)

    return out


def _reset_for_tests() -> None:
    """Drop the in-memory cache so the next resolve() reloads from disk.

    Tests that swap ``TICKER_NAMES_PATH`` need this — otherwise the
    module-level dict from a previous test bleeds into the new run.
    """
    global _cache
    with _LOCK:
        _cache = None
