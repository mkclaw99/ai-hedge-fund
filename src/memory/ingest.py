"""Run-level ingest helper — the single entry point both run paths call.

Converts a finished run's ``analyst_signals`` (the LangGraph chokepoint) into
``Insight`` objects and writes them to the wiki. Designed to be called inside a
try/except by the caller, but also fails open internally so a memory problem
can never break a hedge-fund run.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date as _date

from src.memory.models import Insight
from src.memory.store import WikiMemory

logger = logging.getLogger(__name__)

# Agent ids that are not analysts and should not become wiki insights.
_NON_ANALYST = ("risk_management", "portfolio_manager")
_ID_SUFFIX = re.compile(r"_[a-z0-9]{6}$")  # unique node-id suffix used by the app


def is_enabled() -> bool:
    """Wiki capture is on unless explicitly disabled via env."""
    return os.environ.get("WIKI_MEMORY_ENABLED", "1") != "0"


def normalize_analyst_name(agent_id: str) -> str:
    """Turn ``warren_buffett_agent`` / ``warren_buffett_a1b2c3`` into ``Warren Buffett``."""
    name = agent_id
    name = re.sub(r"_agent$", "", name)
    name = _ID_SUFFIX.sub("", name)
    name = name.replace("_", " ").strip()
    return name.title() if name else agent_id


def ingest_run(
    analyst_signals: dict[str, dict],
    *,
    end_date: str | None = None,
    run_id: str | None = None,
    root: str | None = None,
) -> int:
    """Ingest a finished run's analyst signals into the wiki.

    Args:
        analyst_signals: ``{agent_id: {ticker: {signal, confidence, reasoning}}}``
        end_date:        analysis as-of date; defaults to today.
        run_id:          groups this run's insights; defaults to a random id.
        root:            wiki dir override (else $WIKI_MEMORY_DIR or ./wiki).

    Returns the number of insights written (0 if disabled or nothing to write).
    Never raises.
    """
    if not is_enabled() or not analyst_signals:
        return 0
    try:
        day = (end_date or _date.today().isoformat())[:10]
        rid = run_id or uuid.uuid4().hex[:8]
        insights: list[Insight] = []
        for agent_id, per_ticker in analyst_signals.items():
            if any(tag in agent_id for tag in _NON_ANALYST):
                continue
            if not isinstance(per_ticker, dict):
                continue
            analyst = normalize_analyst_name(agent_id)
            for ticker, payload in per_ticker.items():
                if not isinstance(payload, dict):
                    continue
                signal = payload.get("signal")
                if not signal:
                    continue
                insights.append(Insight(
                    ticker=str(ticker).upper(),
                    analyst=analyst,
                    signal=str(signal).lower(),
                    confidence=float(payload.get("confidence") or 0.0),
                    reasoning=str(payload.get("reasoning") or ""),
                    date=day,
                    run_id=rid,
                ))
        if not insights:
            return 0
        return WikiMemory(root).ingest(insights, run_id=rid)
    except Exception as exc:  # fail-open: never break a run
        logger.warning("WikiMemory ingest skipped due to error: %s", exc)
        return 0


def read_back(tickers: list[str], *, root: str | None = None) -> str:
    """Return a compact prior-research digest for *tickers* (or "" on any issue)."""
    if not is_enabled() or not tickers:
        return ""
    try:
        return WikiMemory(root).render_context_for_prompt(tickers)
    except Exception as exc:  # fail-open
        logger.warning("WikiMemory read-back skipped due to error: %s", exc)
        return ""
