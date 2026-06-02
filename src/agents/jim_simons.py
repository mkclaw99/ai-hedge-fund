"""Jim Simons / Renaissance-style analyst — hypothesis-driven loop.

The pure-quant first cut (PR #109) was "too dumb to be Simons" — it ran a
fixed z-score + fixed rule and emitted a signal. Renaissance's actual loop
was iterative: hypothesise testable patterns, run the stats to prove or
disprove each, decide based on what survived. This module implements that
loop with an LLM doing the creative + adjudicative work and pure-numpy
tests doing the statistical work.

Three stages per ticker, each gated by the previous:

  1. **Hypothesise** — LLM reads the ticker's recent context (price snapshot,
     vol regime, RS vs SPY, recent move) and picks 2-4 hypotheses from the
     registry that look promising right now.
  2. **Test** — deterministic numpy tests run on the picked hypotheses.
     Each returns ``{passed, value, threshold, detail}``. The LLM cannot
     invent new tests (the proposer prompt only exposes the registry);
     unknown names get a uniform fail result.
  3. **Adjudicate** — LLM reads the test results, picks the strongest
     passing hypothesis (or refuses to fire if none passed cleanly), and
     writes the Simons-voiced reasoning. The "we don't override the model"
     rule is enforced structurally: no test passes → adjudication is
     skipped and we emit neutral without a second LLM call.

The full trace (proposed hypotheses + test results + adjudication) is
embedded as a JSON fence in the reasoning markdown so the wiki preserves
it byte-identically. The frontend's SimonsTraceDialog parses the same
fence on rehydrate.

Persistent persona behaviour preserved from PR #109:
  * Neutrals skip the wiki write (track-record stays clean).
  * ``recommended_strategy_for_cadence`` still produces the StrategyConfig
    the Strategy node mirrors via the ``strategy`` output handle.
  * Cadence + bar frequency + lookback live on the node's useNodeState.

Fail-open contract: missing prices, LLM unavailable, missing API keys, or
unparseable LLM output all degrade gracefully — the agent falls back to the
pure-numpy mean-reversion rule (the original PR #109 behaviour) rather
than blocking the rest of the pipeline.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from src.agents._simons_hypotheses import (
    REGISTRY,
    prompt_palette,
    run_test,
)
from src.graph.state import AgentState, show_agent_reasoning
from src.utils.llm import call_llm
from src.utils.progress import progress

logger = logging.getLogger(__name__)

# --- module constants -------------------------------------------------------

_LOOKBACK_BARS = 20
_BENCHMARK = "SPY"
_MIN_HISTORY_BARS = 25
_VALID_FREQUENCIES = ("day", "hour", "5min", "1min")
_YFI_INTERVAL = {"hour": "1h", "5min": "5m", "1min": "1m"}
_YFI_PERIOD = {"1m": "7d", "5m": "60d", "1h": "730d"}
_BARS_PER_YEAR = {"day": 252, "hour": 252 * 6.5, "5min": 252 * 78, "1min": 252 * 390}

# Fence marker for the trace JSON. Same idiom Forecaster uses
# (```forecast-data```). Frontend's SimonsTraceDialog regex matches this.
_TRACE_FENCE_OPEN = "```simons-trace"
_TRACE_FENCE_CLOSE = "```"


# --- recommended strategy (carries the persona for the Strategy override) --

_CADENCE_MINUTES = {"off": 0, "1min": 1, "5min": 5, "15min": 15, "hourly": 60}


def cadence_minutes(cadence: str | None) -> int:
    if not cadence:
        return 5
    return _CADENCE_MINUTES.get(str(cadence).lower(), 5)


def recommended_strategy_for_cadence(cadence: str | None) -> dict[str, Any]:
    """Single source of truth for Simons-recommended StrategyConfig. Used by
    the analyst itself, the simons_executor, and the run-assembler paths so
    the Strategy override the user sees on the canvas matches what runs."""
    mins = max(5, cadence_minutes(cadence))
    return {
        "style": "mean_reversion",
        "sizing_rule": "risk_parity",
        "max_position_pct": 1.0,
        "max_sector_pct": 20.0,
        "holding_period": "day",
        "stop_loss_pct": None,
        "take_profit_pct": None,
        "allow_stocks": True,
        "allow_options": False,
        "allow_etfs": True,
        "note": (
            "Simons/Renaissance-style: hypothesis-driven loop. The LLM proposes "
            "testable patterns; numpy tests them; the LLM adjudicates the survivors. "
            "Many small uncorrelated bets, ~1%/name. Vol-scaled sizing. "
            "We don't override the model."
        ),
        "min_decision_interval_minutes": float(mins),
        "price_move_threshold_pct": 0.3,
        "max_signal_age_hours": max(1.0, mins * 12 / 60.0),
    }


# --- per-flow config resolution --------------------------------------------

def _resolve_frequency(state: AgentState) -> str:
    try:
        req = state.get("metadata", {}).get("request")
        if req is not None:
            f = getattr(req, "simons_bar_frequency", None) or getattr(req, "forecaster_bar_frequency", None)
            if f and str(f).lower() in _VALID_FREQUENCIES:
                return str(f).lower()
    except Exception:
        pass
    return "day"


def _resolve_cadence(state: AgentState) -> str:
    try:
        req = state.get("metadata", {}).get("request")
        if req is not None:
            c = getattr(req, "simons_cadence", None)
            if c and str(c).lower() in _CADENCE_MINUTES:
                return str(c).lower()
    except Exception:
        pass
    return "5min"


def _resolve_lookback(state: AgentState) -> int:
    try:
        req = state.get("metadata", {}).get("request")
        if req is not None:
            n = getattr(req, "simons_lookback_bars", None)
            if n is not None:
                return max(10, min(500, int(n)))
    except Exception:
        pass
    return _LOOKBACK_BARS


# --- price fetching ---------------------------------------------------------

def _fetch_closes_and_volumes(
    ticker: str, end_date: str, frequency: str, count: int,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Pull (closes, volumes) for the given window. yfinance throughout.
    Returns ``(empty, None)`` on failure so the caller can skip the ticker."""
    try:
        import yfinance as yf
    except Exception as e:
        logger.warning("yfinance unavailable for Simons: %s", e)
        return np.array([], dtype=np.float32), None
    try:
        if frequency == "day":
            try:
                end_dt = datetime.fromisoformat(end_date)
                start = (end_dt - timedelta(days=int(count * 1.6))).date().isoformat()
            except Exception:
                start = "1990-01-01"
            df = yf.download(
                tickers=ticker, start=start, end=end_date,
                progress=False, auto_adjust=True, threads=False,
            )
        else:
            interval = _YFI_INTERVAL.get(frequency)
            if interval is None:
                return np.array([], dtype=np.float32), None
            period = _YFI_PERIOD[interval]
            df = yf.download(
                tickers=ticker, period=period, interval=interval,
                progress=False, auto_adjust=True, threads=False,
            )
        if df is None or df.empty:
            return np.array([], dtype=np.float32), None
        close = df["Close"]
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        closes = pd.to_numeric(close, errors="coerce").dropna().tail(count).to_numpy(dtype=np.float32)
        vols: np.ndarray | None = None
        if "Volume" in df.columns:
            vol = df["Volume"]
            if hasattr(vol, "columns"):
                vol = vol.iloc[:, 0]
            vols = pd.to_numeric(vol, errors="coerce").dropna().tail(count).to_numpy(dtype=np.float32)
        return closes, vols
    except Exception as e:
        logger.warning("Simons price fetch failed for %s @ %s: %s", ticker, frequency, e)
        return np.array([], dtype=np.float32), None


# --- context builder (Stage 1 input) ---------------------------------------

def _build_ticker_context(
    *, ticker: str, closes: np.ndarray, bench: np.ndarray, volumes: np.ndarray | None,
    frequency: str, lookback: int,
) -> dict[str, Any]:
    """A compact, LLM-readable snapshot the proposer reads to pick hypotheses.
    Pre-computes the cheapest numbers so the LLM has real evidence to anchor on
    rather than picking hypotheses at random."""
    window = closes[-lookback:]
    last = float(window[-1])
    z = float((last - window.mean()) / max(window.std(ddof=0), 1e-9))
    rets = np.diff(np.log(np.maximum(window, 1e-9)))
    bpy = _BARS_PER_YEAR.get(frequency, 252)
    vol_pct = float(np.std(rets, ddof=0) * np.sqrt(bpy) * 100.0) if rets.size > 1 else 0.0
    rs_sigma: float | None = None
    if bench.size >= 5 and window.size >= 5:
        n = min(window.size, bench.size)
        t = window[-n:]; b = bench[-n:]
        if t[0] > 0 and b[0] > 0:
            spread = (t[1:] - t[:-1]) / np.maximum(t[:-1], 1e-9) - (b[1:] - b[:-1]) / np.maximum(b[:-1], 1e-9)
            sd = float(np.std(spread, ddof=0))
            if sd > 0:
                rs_sigma = float(((t[-1] - t[0]) / t[0] - (b[-1] - b[0]) / b[0]) / sd)
    pct_change_5b = float((window[-1] - window[-min(5, window.size)]) / window[-min(5, window.size)] * 100.0) if window.size >= 2 else 0.0
    vol_ratio = None
    if volumes is not None and volumes.size >= 5:
        med = float(np.median(volumes[-lookback:-1])) if volumes.size > lookback else float(np.median(volumes[:-1]))
        if med > 0:
            vol_ratio = round(float(volumes[-1]) / med, 2)
    return {
        "ticker": ticker,
        "frequency": frequency,
        "lookback_bars": lookback,
        "last_close": round(last, 4),
        "z_score_vs_ma": round(z, 2),
        "realized_vol_pct_annualised": round(vol_pct, 1),
        "rs_vs_spy_sigma": round(rs_sigma, 2) if rs_sigma is not None else None,
        "recent_5bar_change_pct": round(pct_change_5b, 2),
        "last_bar_volume_ratio_to_median": vol_ratio,
    }


# --- LLM prompts + schemas -------------------------------------------------

class ProposedHypothesis(BaseModel):
    name: str = Field(..., description="One of the registered hypothesis names.")
    rationale: str = Field(..., description="One short sentence: why this hypothesis fits this ticker right now.")


class HypothesesOutput(BaseModel):
    hypotheses: list[ProposedHypothesis] = Field(
        ..., description="Two to four hypotheses to test for this ticker.",
    )


class AdjudicationOutput(BaseModel):
    signal: str = Field(..., description="bullish, bearish, or neutral.")
    confidence: int = Field(..., description="0-100. Calibrate to how strongly the surviving evidence points.")
    winning_hypothesis: str | None = Field(
        None, description="The name of the hypothesis chosen as the basis for the signal. None if neutral.",
    )
    reasoning: str = Field(
        ..., description="Simons-voiced markdown: terse, statistical, refers to specific test results. No narrative prose.",
    )


def _hypothesize(
    *, ticker: str, context: dict[str, Any], agent_id: str, state: AgentState,
) -> list[dict[str, str]] | None:
    """Stage 1: ask the LLM to pick 2-4 hypotheses from the registry. Returns
    a list of ``{name, rationale}`` dicts, or None on failure (caller falls
    back to the pure-numpy default rule)."""
    palette = prompt_palette()
    template = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are Jim Simons. Pure quant — no fundamentals, no narratives, no story-based picks. "
            "Your only job here is to PICK testable hypotheses, not to fire signals. The numbers will "
            "be tested deterministically and you will adjudicate the survivors in a second call.\n\n"
            "Look at the ticker context below. Pick 2 to 4 hypotheses from the available list that look "
            "most promising for THIS ticker RIGHT NOW given the snapshot. Bias toward the ones the context "
            "numbers actually hint at — don't propose vol_regime_shift if vol looks stable. Don't propose "
            "more than 4: testing more hypotheses than necessary just adds noise.",
        ),
        (
            "human",
            "## Ticker context\n```json\n{context}\n```\n\n"
            "## Available hypotheses\n{palette}\n\n"
            "Return a JSON object: {{\"hypotheses\": [{{\"name\": \"...\", \"rationale\": \"...\"}}, ...]}}",
        ),
    ])
    prompt = template.invoke({"context": json.dumps(context, indent=2), "palette": palette})

    def _default_factory():
        # Fall back to the original rule's two legs if the LLM fails.
        return HypothesesOutput(hypotheses=[
            ProposedHypothesis(name="mean_reversion", rationale="default fallback (LLM unavailable)"),
            ProposedHypothesis(name="pairs_divergence", rationale="default fallback (LLM unavailable)"),
        ])
    try:
        out = call_llm(
            prompt=prompt, pydantic_model=HypothesesOutput,
            agent_name=agent_id, state=state, default_factory=_default_factory,
        )
    except Exception:
        logger.warning("Simons hypothesize call failed for %s", ticker, exc_info=True)
        return None
    if not out or not out.hypotheses:
        return None
    # Filter to valid registry names; drop dupes preserving order.
    seen: set[str] = set()
    cleaned: list[dict[str, str]] = []
    for h in out.hypotheses:
        if h.name in REGISTRY and h.name not in seen:
            seen.add(h.name)
            cleaned.append({"name": h.name, "rationale": h.rationale})
        if len(cleaned) >= 4:
            break
    return cleaned or None


def _adjudicate(
    *, ticker: str, context: dict[str, Any],
    proposed: list[dict[str, str]], results: list[dict[str, Any]],
    agent_id: str, state: AgentState,
) -> AdjudicationOutput | None:
    """Stage 3: pick the strongest passing hypothesis (or neutral). Only
    called when at least one hypothesis passed — otherwise the caller emits
    neutral directly without paying for a second LLM call."""
    template = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are Jim Simons. The hypothesis tests have run; here are the results. "
            "Pick the strongest passing hypothesis as the basis for your signal. "
            "Refuse to fire if nothing passed cleanly — 'we don't override the model'.\n\n"
            "Direction inference:\n"
            "  - mean_reversion: z > 0 → short (bearish); z < 0 → long (bullish).\n"
            "  - pairs_divergence: ticker leading → short; ticker lagging → long.\n"
            "  - vol_regime_shift: spike → favours reversal; crush → favours momentum.\n"
            "  - gap_fade: fade the gap direction.\n"
            "  - volume_signature: continuation in the price direction.\n"
            "  - calendar_tilt: favourable → bullish; unfavourable → bearish.\n\n"
            "Confidence: how strongly the surviving evidence points. A barely-passing test "
            "(value just past threshold) → 30-50. A clean pass with corroboration → 60-80. "
            "Anything above 80 needs unusual alignment.\n\n"
            "Reasoning style: terse, statistical, reference specific test values. No story. "
            "≤6 lines of markdown. The PM reads this verbatim in its memo.",
        ),
        (
            "human",
            "## Context\n```json\n{context}\n```\n\n"
            "## Hypotheses proposed\n{proposed}\n\n"
            "## Test results\n{results}\n\n"
            "Return JSON: {{\"signal\": \"...\", \"confidence\": int, \"winning_hypothesis\": \"...\", \"reasoning\": \"...\"}}",
        ),
    ])
    proposed_md = "\n".join([f"- **{h['name']}** — {h['rationale']}" for h in proposed])
    results_md = "\n".join([
        f"- **{r['name']}** → {'PASSED' if r['result']['passed'] else 'FAILED'}: {r['result']['detail']}"
        for r in results
    ])
    prompt = template.invoke({
        "context": json.dumps(context, indent=2),
        "proposed": proposed_md, "results": results_md,
    })

    def _default_factory():
        passing = [r for r in results if r["result"]["passed"]]
        if not passing:
            return AdjudicationOutput(signal="neutral", confidence=0, winning_hypothesis=None,
                                      reasoning="LLM unavailable; no test passed.")
        first = passing[0]
        return AdjudicationOutput(
            signal="neutral", confidence=30,
            winning_hypothesis=first["name"],
            reasoning=f"LLM unavailable; falling back to first passing test ({first['name']}). Manual review recommended.",
        )
    try:
        return call_llm(
            prompt=prompt, pydantic_model=AdjudicationOutput,
            agent_name=agent_id, state=state, default_factory=_default_factory,
        )
    except Exception:
        logger.warning("Simons adjudicate call failed for %s", ticker, exc_info=True)
        return None


# --- pure-numpy fallback (matches PR #109 behaviour) ----------------------

def _fallback_signal_from_context(context: dict[str, Any]) -> dict[str, Any]:
    """When the LLM is unavailable, degrade to the original mean-reversion rule
    so the rest of the pipeline still gets a signal. Returns the same shape
    the orchestrator emits."""
    z = float(context.get("z_score_vs_ma") or 0.0)
    if abs(z) < 2.0:
        return {
            "signal": "neutral", "confidence": 0,
            "reasoning": f"Fallback (no LLM): z={z:+.2f}σ below 2σ trigger.",
        }
    signal = "bullish" if z < 0 else "bearish"
    confidence = int(round(min(100.0, abs(z) / 3.0 * 100.0)))
    return {
        "signal": signal, "confidence": confidence,
        "reasoning": f"Fallback (no LLM): z={z:+.2f}σ → {signal} mean-reversion.",
    }


# --- agent entry point ----------------------------------------------------

def jim_simons_agent(state: AgentState, agent_id: str = "jim_simons_agent"):
    """Run the hypothesis-driven loop on each ticker. See module docstring for
    the contract; ``state["data"]["analyst_signals"][agent_id]`` is populated
    with the per-ticker {signal, confidence, reasoning, simons_trace} dicts."""
    data = state["data"]
    tickers = data["tickers"]
    end_date = data["end_date"]
    frequency = _resolve_frequency(state)
    lookback = _resolve_lookback(state)
    cadence = _resolve_cadence(state)

    progress.update_status(agent_id, None, f"Fetching {_BENCHMARK} benchmark")
    bench, _ = _fetch_closes_and_volumes(_BENCHMARK, end_date, frequency, max(lookback * 4, _MIN_HISTORY_BARS))

    signals: dict[str, dict] = {}
    for ticker in tickers:
        progress.update_status(agent_id, ticker, f"Fetching {frequency} bars")
        closes, volumes = _fetch_closes_and_volumes(
            ticker, end_date, frequency, max(lookback * 4, _MIN_HISTORY_BARS),
        )
        if closes.size == 0:
            progress.update_status(agent_id, ticker, "Failed: no bars available")
            continue
        if closes.size < _MIN_HISTORY_BARS:
            progress.update_status(agent_id, ticker, "Failed: insufficient history")
            continue

        context = _build_ticker_context(
            ticker=ticker, closes=closes, bench=bench, volumes=volumes,
            frequency=frequency, lookback=lookback,
        )

        # Stage 1 — hypothesise.
        progress.update_status(agent_id, ticker, "Hypothesising")
        proposed = _hypothesize(ticker=ticker, context=context, agent_id=agent_id, state=state)
        if proposed is None:
            # Total LLM failure — emit the pure-numpy fallback so the PM still
            # gets a signal. No trace (would be empty).
            fallback = _fallback_signal_from_context(context)
            if fallback["signal"] == "neutral":
                progress.update_status(agent_id, ticker, "Done (no signal)", analysis=fallback["reasoning"])
                continue
            signals[ticker] = {
                **fallback,
                "simons_trace": {"context": context, "fallback": True},
            }
            progress.update_status(agent_id, ticker, "Done (fallback)", analysis=fallback["reasoning"])
            continue

        # Stage 2 — run tests deterministically.
        progress.update_status(agent_id, ticker, "Testing hypotheses")
        results: list[dict[str, Any]] = []
        for h in proposed:
            r = run_test(
                h["name"], closes=closes, bench_closes=bench, volumes=volumes,
                frequency=frequency, lookback=lookback, end_date=end_date,
            )
            results.append({"name": h["name"], "rationale": h["rationale"], "result": r})

        # Stage 3 — adjudicate, only if at least one test passed. When nothing
        # passed the answer is known (neutral) without paying for an LLM call.
        any_passed = any(r["result"]["passed"] for r in results)
        if not any_passed:
            trace = _build_trace(
                ticker=ticker, frequency=frequency, lookback=lookback, end_date=end_date,
                context=context, proposed=proposed, results=results,
                adjudication={"signal": "neutral", "confidence": 0,
                              "winning_hypothesis": None,
                              "reasoning": "No hypothesis passed its statistical threshold. "
                                           "Neutral — we don't override the model."},
                skipped_llm_adjudicate=True,
            )
            reasoning_md = _render_reasoning("neutral", 0, None,
                                             "No hypothesis passed its statistical threshold. "
                                             "Neutral — we don't override the model.",
                                             trace)
            progress.update_status(agent_id, ticker, "Done (no entry)", analysis=reasoning_md)
            continue

        progress.update_status(agent_id, ticker, "Adjudicating")
        verdict = _adjudicate(
            ticker=ticker, context=context, proposed=proposed, results=results,
            agent_id=agent_id, state=state,
        )
        if verdict is None:
            verdict = AdjudicationOutput(
                signal="neutral", confidence=0, winning_hypothesis=None,
                reasoning="Adjudication call failed; no signal.",
            )

        trace = _build_trace(
            ticker=ticker, frequency=frequency, lookback=lookback, end_date=end_date,
            context=context, proposed=proposed, results=results,
            adjudication={
                "signal": verdict.signal, "confidence": int(verdict.confidence),
                "winning_hypothesis": verdict.winning_hypothesis,
                "reasoning": verdict.reasoning,
            },
            skipped_llm_adjudicate=False,
        )
        reasoning_md = _render_reasoning(
            verdict.signal, int(verdict.confidence), verdict.winning_hypothesis,
            verdict.reasoning, trace,
        )

        # Normalise the LLM's signal label — Gemini sometimes emits trader
        # jargon ("long"/"short") instead of the PM-expected vocabulary.
        # Map them through; anything we don't recognise collapses to neutral.
        signal_norm = {
            "long": "bullish", "buy": "bullish", "bullish": "bullish",
            "short": "bearish", "sell": "bearish", "bearish": "bearish",
            "neutral": "neutral", "hold": "neutral", "flat": "neutral", "": "neutral",
        }.get(str(verdict.signal).strip().lower(), "neutral")
        if signal_norm == "neutral":
            progress.update_status(agent_id, ticker, "Done (no entry)", analysis=reasoning_md)
            continue
        signals[ticker] = {
            "signal": signal_norm,
            "confidence": int(max(0, min(100, verdict.confidence))),
            "reasoning": reasoning_md,
            "simons_trace": trace,
        }
        progress.update_status(agent_id, ticker, "Done", analysis=reasoning_md)

    # Publish the recommended strategy regardless of fire-count. Strategy
    # override flows through this same channel.
    state.setdefault("data", {}).setdefault("simons", {})[
        "recommended_strategy"
    ] = recommended_strategy_for_cadence(cadence)

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(signals, "Jim Simons")
    state["data"]["analyst_signals"][agent_id] = signals
    message = HumanMessage(content=json.dumps(signals), name=agent_id)
    progress.update_status(agent_id, None, "Done")
    return {"messages": state["messages"] + [message], "data": data}


# --- trace + reasoning rendering ------------------------------------------

def _build_trace(
    *, ticker: str, frequency: str, lookback: int, end_date: str,
    context: dict[str, Any], proposed: list[dict[str, str]],
    results: list[dict[str, Any]], adjudication: dict[str, Any],
    skipped_llm_adjudicate: bool,
) -> dict[str, Any]:
    """Assemble the structured trace the frontend dialog renders. Stored in
    the wiki Insight's reasoning markdown inside a ```simons-trace fence so
    it rehydrates on flow reload (same idiom Forecaster uses)."""
    return {
        "ticker": ticker,
        "as_of": end_date,
        "frequency": frequency,
        "lookback_bars": lookback,
        "context": context,
        "hypotheses_proposed": proposed,
        "tests": [
            {
                "name": r["name"],
                "rationale": r["rationale"],
                "passed": r["result"]["passed"],
                "value": r["result"]["value"],
                "threshold": r["result"]["threshold"],
                "detail": r["result"]["detail"],
            }
            for r in results
        ],
        "adjudication": adjudication,
        "skipped_llm_adjudicate": skipped_llm_adjudicate,
    }


def _render_reasoning(
    signal: str, confidence: int, winning: str | None,
    reasoning_text: str, trace: dict[str, Any],
) -> str:
    """Markdown body of the wiki Insight. Header + adjudication prose + fenced
    JSON trace. The PM reads everything (the trace ends up as a labelled code
    block from its perspective — ignorable but auditable)."""
    winning_line = f"**Winning hypothesis:** `{winning}`\n\n" if winning else ""
    return (
        f"**Signal:** {signal.upper()} · **Confidence:** {confidence}%\n\n"
        f"{winning_line}"
        f"{reasoning_text.strip()}\n\n"
        f"{_TRACE_FENCE_OPEN}\n"
        f"{json.dumps(trace, indent=2)}\n"
        f"{_TRACE_FENCE_CLOSE}\n"
    )
