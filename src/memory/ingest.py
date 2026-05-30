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
from pathlib import Path

from src.memory.models import Insight
from src.memory.store import WikiMemory

logger = logging.getLogger(__name__)

# Agent ids that are not analysts and should not become wiki insights.
_NON_ANALYST = ("risk_management", "portfolio_manager")
_ID_SUFFIX = re.compile(r"_[a-z0-9]{6}$")  # unique node-id suffix used by the app

# Name used for the Portfolio Manager's own decision insights in the wiki.
PM_ANALYST = "Portfolio Manager"

# Maps a PM decision action to the stance it implies, so decisions read back like
# any other contributor's signal.
_ACTION_STANCE = {
    "buy": "bullish",
    "cover": "bullish",
    "sell": "bearish",
    "short": "bearish",
    "hold": "neutral",
}


def is_enabled() -> bool:
    """Wiki capture is on unless explicitly disabled via env."""
    return os.environ.get("WIKI_MEMORY_ENABLED", "1") != "0"


def flow_root(flow_slug: str | None) -> str | None:
    """Wiki directory for a flow's memory namespace, e.g. ``wiki/flow-12``.

    Returns None for a falsy slug so callers fall back to the default global
    wiki (``$WIKI_MEMORY_DIR`` or ``./wiki``) — used by the CLI and any path that
    doesn't carry a flow id.
    """
    if not flow_slug:
        return None
    return str(Path("wiki") / flow_slug)


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


def ingest_decisions(
    decisions: dict[str, dict],
    *,
    end_date: str | None = None,
    run_id: str | None = None,
    root: str | None = None,
    rules_by_ticker: dict[str, list[str]] | None = None,
) -> int:
    """Ingest the Portfolio Manager's final decisions as its own wiki insights.

    Analyst signals are captured by :func:`ingest_run`; the PM writes *decisions*
    (buy/sell/hold) instead, which that path skips. Capturing them here gives the
    PM a memory of its own past calls to read back on later runs.

    Args:
        decisions: ``{ticker: {action, quantity, confidence, reasoning}}``
        end_date:  analysis as-of date; defaults to today.
        run_id:    groups this run's insights; defaults to a random id.
        root:      wiki dir override (else $WIKI_MEMORY_DIR or ./wiki).
        rules_by_ticker: optional ``{ticker: [rule_text, ...]}`` of the
            Mandatory Adjustment rules that were active when this PM decision
            was made. Surfaced in the wiki frontmatter + a Rules Applied
            markdown section so per-day audit is greppable.

    Returns the number of decisions written (0 if disabled or nothing to write).
    Never raises.
    """
    if not is_enabled() or not decisions:
        return 0
    try:
        day = (end_date or _date.today().isoformat())[:10]
        rid = run_id or uuid.uuid4().hex[:8]
        rules_by_ticker = rules_by_ticker or {}
        insights: list[Insight] = []
        for ticker, payload in decisions.items():
            if not isinstance(payload, dict):
                continue
            action = str(payload.get("action") or "").lower()
            if not action:
                continue
            qty = payload.get("quantity")
            base_reasoning = str(payload.get("reasoning") or "").strip()
            reasoning = f"Decided {action}" + (f" {qty}" if qty else "")
            if base_reasoning:
                reasoning += f" — {base_reasoning}"
            ticker_upper = str(ticker).upper()
            insights.append(Insight(
                ticker=ticker_upper,
                analyst=PM_ANALYST,
                signal=_ACTION_STANCE.get(action, "neutral"),
                confidence=float(payload.get("confidence") or 0.0),
                reasoning=reasoning,
                date=day,
                run_id=rid,
                rules_applied=list(rules_by_ticker.get(ticker_upper, [])),
            ))
        if not insights:
            return 0
        return WikiMemory(root).ingest(insights, run_id=rid)
    except Exception as exc:  # fail-open: never break a run
        logger.warning("WikiMemory decision ingest skipped due to error: %s", exc)
        return 0


def read_back(tickers: list[str], *, analyst: str | None = None, root: str | None = None) -> str:
    """Return a compact prior-research digest for *tickers* (or "" on any issue).

    When *analyst* is given, returns only that analyst's own latest stance per
    ticker (individual memory); otherwise the full cross-analyst digest.
    """
    if not is_enabled() or not tickers:
        return ""
    try:
        return WikiMemory(root).render_context_for_prompt(tickers, analyst=analyst)
    except Exception as exc:  # fail-open
        logger.warning("WikiMemory read-back skipped due to error: %s", exc)
        return ""


def read_latest_signals(tickers: list[str], *, root: str | None = None) -> dict[str, dict[str, dict]]:
    """Rehydrate ``analyst_signals`` from the wiki for a replay-strategy run.

    Shape mirrors what live analysts populate in ``state["data"]["analyst_signals"]``:
    ``{agent_key: {ticker: {signal, confidence, reasoning}}}``. The PM filters on
    the ``risk_management_agent`` prefix and otherwise treats every key as an
    analyst — so we synthesize a stable key from the normalized analyst name
    (e.g. ``"Warren Buffett" → "warren_buffett_agent"``). Risk-manager entries
    in memory (if any) are dropped — risk lives in the fresh run, not the wiki.

    Returns ``{}`` for any unhappy path (no tickers, no root, empty wiki,
    unexpected shape). Callers should treat ``{}`` as "no prior signals" and
    fall through to default PM behaviour.
    """
    if not is_enabled() or not tickers or not root:
        return {}
    try:
        wiki = WikiMemory(root)
        out: dict[str, dict[str, dict]] = {}
        for t in tickers:
            ctx = wiki.query_ticker(t)
            for analyst_name, ins in (ctx.latest_by_analyst or {}).items():
                key = analyst_name.lower().replace(" ", "_") + "_agent"
                # Risk recomputes live — never replayed from memory.
                # The PM's own past decisions show up under "Portfolio Manager" too;
                # excluding them prevents the replay from feeding the PM its own
                # prior signal as if it were an analyst's opinion (feedback loop).
                # The PM still reads its past decisions via the `prior research`
                # read_back path — same as a normal run — so the info isn't lost.
                if key.startswith("risk_management_agent") or key.startswith("portfolio_manager"):
                    continue
                out.setdefault(key, {})[t] = {
                    "signal": ins.signal,
                    "confidence": int(ins.confidence),
                    "reasoning": ins.reasoning,
                }
        return out
    except Exception as exc:
        logger.warning("WikiMemory replay-signals read skipped due to error: %s", exc)
        return {}
