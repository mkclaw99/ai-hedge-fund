"""Time-series forecaster analyst — Amazon Chronos-2 over recent daily closes.

Loads ``amazon/chronos-2`` (120M params, encoder-only T5-style foundation model)
once per process and runs a probabilistic forecast on each ticker's last ~year
of daily closes. Maps the q10 / q50 / q90 fan at the forecast horizon into a
standard bullish / bearish / neutral signal so the PM can consume the result
just like any other analyst.

Fails open: missing torch/chronos, no price history, or a model error all
return without writing a signal — the rest of the pipeline keeps running.
The model file (~480 MB) is cached by HuggingFace under
``~/.cache/huggingface/hub`` and only downloaded once.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from langchain_core.messages import HumanMessage

from src.agents._forecaster_backbones import (
    BackboneResult,
    run as run_backbone,
)
from src.graph.state import AgentState, show_agent_reasoning
from src.tools.api import get_prices, prices_to_df
from src.utils.api_key import get_api_key_from_state
from src.utils.progress import progress


# Backbone resolution from the agent_id. The frontend node-mappings.ts
# spawns two distinct registry keys (`forecaster` vs `toto_forecaster`)
# but both produce the same `forecaster-node` UI component. The agent_id
# carries the disambiguation through to the backend so a single agent
# function dispatches to either Chronos-2 or Toto-2.0 without forking
# every code path (registry, route, frontend) by backbone.
def _resolve_backbone(agent_id: str) -> str:
    """Map an agent_id (e.g. 'forecaster_abc123' or 'toto_forecaster_abc123')
    to a backbone name understood by ``_forecaster_backbones.run``.
    Defaults to chronos2 — the historical backbone — for unrecognised ids
    so existing wiki rows + saved flows keep behaving the same."""
    if agent_id.startswith("toto_forecaster"):
        return "toto2"
    return "chronos2"


def _backbone_display_name(backbone: str) -> str:
    """Human-readable label for trace / report headers. Mirrors the
    BackboneResult.model_name values so a stale failure path (where we
    never got a result) still produces consistent UI text."""
    return {
        "chronos2": "Amazon Chronos-2",
        "toto2": "Datadog Toto-2.0",
    }.get(backbone, backbone)

logger = logging.getLogger(__name__)

# Tunables — defaults chosen for a swing-trade horizon. These are
# agent-level constants rather than node-config so the PM sees a stable
# signal shape regardless of which flow embeds the forecaster.
_MODEL_ID = "amazon/chronos-2"
_CONTEXT_LEN = 256              # last N daily closes fed to the model
_PRED_LEN = 10                  # trading-day horizon (~2 weeks)
# Quantile contract documented in _forecaster_backbones.BackboneResult:
# every backbone returns five levels — q10/q25/q50/q75/q90 — so the
# nested-band fan chart idiom (inner 50% interval, outer 80% interval) is
# uniform across backbones. Signal mapping below still uses only
# q10/q50/q90 at the horizon end so the PM's track-record stays comparable
# across both backbones and across versions.
_NEUTRAL_PCT = 1.0              # |q50_pct| below this → neutral

# Bar-frequency knob. Daily goes through the standard provider chain
# (FD → Alpaca → Yahoo, cached). Intraday bypasses the chain and pulls
# from yfinance directly — it's the only free source with reliable
# intraday history; Alpaca-tier limits make it unreliable for free
# accounts. yfinance period caps per interval are hard ceilings: 1m=7d,
# 5m=60d, 1h=730d. Asking for more than that on intraday is silently
# truncated to whatever fits.
_VALID_FREQUENCIES = ("day", "hour", "5min", "1min")
_YFI_INTERVAL = {"hour": "1h", "5min": "5m", "1min": "1m"}
_YFI_PERIOD = {"1m": "7d", "5m": "60d", "1h": "730d"}
# Friendly singular-unit label used in the report/UI.
_FREQ_UNIT = {"day": "trading day", "hour": "hour", "5min": "5-min bar", "1min": "minute"}

# History context shown alongside the forecast in the node's inline chart.
# Kept short on purpose — the chart panel is ~180 px wide and 30 trading
# days is plenty to read direction off the most recent close. We have the
# full ~256-step input regardless; this is just what we send to the UI.
_CHART_HISTORY_MIN = 30        # never less than this (sane daily default)
_CHART_HISTORY_RATIO = 5       # show ~5× the forecast horizon as context
# Hard cap matches Chronos-2's max context length: when the user sets
# Context Bars = 8192 they want the chart to *show* what the model saw,
# not a truncated slice. Payload at 8192 floats ≈ 50–100 KB; the
# frontend renders an SVG polyline of that length without trouble and
# uses adaptive x-axis label density so labels don't overrun.
_CHART_HISTORY_HARD_MAX = 8192
# Fence marker the frontend's ForecasterNode parses out of the per-ticker
# analysis Markdown. Bracketed so a Markdown viewer still renders the
# block as a labelled code fence if a human opens the raw analysis.
_CHART_FENCE_OPEN = "```forecast-data"
_CHART_FENCE_CLOSE = "```"


def _resolve_lengths(state: AgentState) -> tuple[int, int]:
    """Read per-flow Chronos-2 lengths from the request, clamped to model limits.

    Returns (context_len, prediction_len). Module-default fallback when
    the request didn't carry the values (e.g. older clients or a
    backtest where the ForecasterNode wasn't reachable from the Play
    trigger). Clamp bounds match the Chronos-2 model card.
    """
    ctx, pred = _CONTEXT_LEN, _PRED_LEN
    try:
        req = state.get("metadata", {}).get("request")
        if req is not None:
            c = getattr(req, "forecaster_context_len", None)
            p = getattr(req, "forecaster_prediction_len", None)
            if c is not None:
                ctx = max(32, min(8192, int(c)))
            if p is not None:
                pred = max(1, min(1024, int(p)))
    except Exception:  # never let config parsing break the run
        pass
    return ctx, pred


def _resolve_frequency(state: AgentState) -> str:
    """Read per-flow bar frequency from the request. Default: 'day'."""
    try:
        req = state.get("metadata", {}).get("request")
        if req is not None:
            f = getattr(req, "forecaster_bar_frequency", None)
            if f and str(f).lower() in _VALID_FREQUENCIES:
                return str(f).lower()
    except Exception:
        pass
    return "day"


def _fetch_bars(ticker: str, end_date: str, frequency: str, count: int, api_key: str | None) -> np.ndarray:
    """Return up to *count* close-price bars for *ticker* at the given frequency.

    All frequencies (daily included) go through **yfinance directly**.
    The provider chain (FD → Alpaca → Yahoo) is fine for ~3-year ranges,
    but the forecaster wants *deep* history (Chronos-2's max context is
    8192 bars ≈ 32 years daily). Financial Datasets' free-tier caps at
    ~750 bars / ~3 years, and because the chain treats any non-empty
    response as success it never falls through to yfinance. So we skip
    the chain entirely for the forecaster path and use yfinance, which
    has unbounded daily history. Returns an empty array on any failure —
    the caller treats that as "skip this ticker".
    """
    # api_key is unused now (yfinance needs no key) but kept in the
    # signature so all callers stay simple.
    _ = api_key

    if frequency == "day":
        try:
            import yfinance as yf
            # 1.6× overshoot on calendar days for weekends/holidays;
            # yfinance returns all daily bars in [start, end] — much
            # more than the ~3-year cap FD imposes.
            try:
                end_dt = datetime.fromisoformat(end_date)
                start = (end_dt - timedelta(days=int(count * 1.6))).date().isoformat()
            except Exception:
                # Bad end_date string — fall back to a wide window
                # yfinance can fill (since-1990 covers most equities).
                start = "1990-01-01"
            df = yf.download(
                tickers=ticker,
                start=start,
                end=end_date,
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if df is None or df.empty:
                return np.array([], dtype=np.float32)
            close = df["Close"]
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            return pd.to_numeric(close, errors="coerce").dropna().tail(count).to_numpy(dtype=np.float32)
        except Exception as e:
            logger.warning("daily fetch failed for %s: %s", ticker, e)
            return np.array([], dtype=np.float32)

    # --- Intraday via yfinance ---------------------------------------------
    interval = _YFI_INTERVAL.get(frequency)
    if interval is None:
        return np.array([], dtype=np.float32)
    try:
        import yfinance as yf  # lazy import — already a dep but keep startup cheap
        period = _YFI_PERIOD[interval]
        df = yf.download(
            tickers=ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
            threads=False,  # avoid multi-ticker MultiIndex columns
        )
        if df is None or df.empty:
            return np.array([], dtype=np.float32)
        # yfinance may return MultiIndex columns (when threads=True or multi
        # tickers); squeeze handles both cases.
        close = df["Close"]
        if hasattr(close, "columns"):  # DataFrame, not Series
            close = close.iloc[:, 0]
        return pd.to_numeric(close, errors="coerce").dropna().tail(count).to_numpy(dtype=np.float32)
    except Exception as e:
        logger.warning("intraday fetch (%s) failed for %s: %s", frequency, ticker, e)
        return np.array([], dtype=np.float32)


def forecaster_agent(state: AgentState, agent_id: str = "forecaster_agent"):
    """Run the configured forecaster backbone on each ticker and emit a
    directional signal.

    Backbone is resolved from the agent_id:
      * ``forecaster_*``       → Chronos-2 (the original v1 backbone)
      * ``toto_forecaster_*``  → Toto-2.0  (the new optional backbone)

    Both produce the same ``BackboneResult`` shape, so the rest of this
    function — signal mapping, confidence trajectory, chart fence,
    reasoning markdown — is backbone-agnostic.
    """
    data = state["data"]
    tickers = data["tickers"]
    end_date = data["end_date"]
    api_key = get_api_key_from_state(state, "FINANCIAL_DATASETS_API_KEY")
    context_len, prediction_len = _resolve_lengths(state)
    frequency = _resolve_frequency(state)
    backbone = _resolve_backbone(agent_id)
    model_display = _backbone_display_name(backbone)

    # Collect price history per ticker first. Fast, and lets us bail out
    # cleanly if the model is unavailable without paying the load cost.
    series_by_ticker: dict[str, np.ndarray] = {}
    for ticker in tickers:
        progress.update_status(agent_id, ticker, f"Fetching {frequency} bars")
        closes = _fetch_bars(ticker, end_date, frequency, context_len, api_key)
        if closes.size == 0:
            progress.update_status(agent_id, ticker, "Failed: no bars available")
            continue
        if closes.size < 30:
            # Both backbones can technically handle short context, but a
            # forecast off ~30 bars is noise at any frequency.
            progress.update_status(agent_id, ticker, "Failed: insufficient history")
            continue
        series_by_ticker[ticker] = closes

    if not series_by_ticker:
        state["data"]["analyst_signals"][agent_id] = {}
        progress.update_status(agent_id, None, "Done")
        return {"messages": state["messages"], "data": data}

    # Per-ticker dispatch through the backbone module. Each backbone owns
    # its own lazy-init + caching; the agent only sees ``BackboneResult``s
    # (or None for "skip this ticker"). One pass per ticker rather than
    # batched: Toto's `forecast(...)` takes a single (batch=1, var=1) input
    # at a time, and Chronos's batch advantage is marginal for the small
    # ticker counts hedge-fund flows use (~5).
    signals: dict[str, dict] = {}
    for ticker, closes in series_by_ticker.items():
        progress.update_status(agent_id, ticker, f"Running {model_display}")
        result: BackboneResult | None = run_backbone(backbone, closes, prediction_len=prediction_len)
        if result is None:
            progress.update_status(agent_id, ticker, f"Skipped: {model_display} unavailable")
            continue
        q10_traj, q25_traj, q50_traj, q75_traj, q90_traj = (
            result.q10, result.q25, result.q50, result.q75, result.q90,
        )
        confidence_traj = result.confidence
        last_close = float(closes[-1])
        # Signal mapping uses the final step's outer quantiles only — q25/q75
        # widen the inner band on the chart but don't change the directional
        # signal the PM sees, so track-record stays consistent across versions.
        signals[ticker] = _build_signal(
            last_close, q10_traj[-1], q50_traj[-1], q90_traj[-1],
            horizon_days=prediction_len, frequency=frequency,
            model_name=result.model_name,
        )
        # Per-ticker chart payload, embedded into the analysis Markdown so
        # the existing SSE 'analysis' channel carries it through without a
        # schema change. ForecasterNode parses the fence; other components
        # see it as a labelled code block and ignore it.
        # Chart history matches what Chronos actually saw: show the full
        # context_len (clamped to what the provider returned and to a
        # hard ceiling). Users explicitly choose Context Bars to feed
        # the model; the chart should reflect that choice rather than
        # truncating to a small slice. _CHART_HISTORY_MIN keeps a sane
        # floor for unconfigured runs.
        chart_history_n = min(
            len(closes),
            max(_CHART_HISTORY_MIN, context_len),
            _CHART_HISTORY_HARD_MAX,
        )
        history = [float(x) for x in closes[-chart_history_n:].tolist()]
        chart = json.dumps({
            "history": [round(x, 4) for x in history],
            "q10": [round(x, 4) for x in q10_traj],
            "q25": [round(x, 4) for x in q25_traj],
            "q50": [round(x, 4) for x in q50_traj],
            "q75": [round(x, 4) for x in q75_traj],
            "q90": [round(x, 4) for x in q90_traj],
            "confidence": confidence_traj,
            "horizon_days": prediction_len,
            # Frequency travels alongside horizon_days so the chart can
            # label the time axis correctly (10 'bars' means 10 days at
            # 'day', 10 hours at 'hour', 10 minutes at '1min', etc.).
            "frequency": frequency,
            # Which backbone produced these numbers. Lets the frontend show
            # a "Chronos-2" / "Toto-2.0" badge per ticker — useful when two
            # forecaster nodes are wired into the same flow.
            "backbone": backbone,
            "model_name": result.model_name,
        })
        # Hand-rolled report — no LLM call. The forecaster's whole point is
        # Chronos-2; routing the structured reasoning dict through an LLM
        # for prose adds latency, cost, an API-key dependency, and confuses
        # the user about *what* model is forecasting. The reasoning dict
        # already says everything in plain numbers; we render it directly.
        report = _render_report(signals[ticker])
        analysis = f"{report}\n\n{_CHART_FENCE_OPEN}\n{chart}\n{_CHART_FENCE_CLOSE}\n"
        # Stash the structured reasoning dict under `forecast` BEFORE we
        # overwrite `reasoning` below. The PM reads this to render its
        # `## Forecast Mandate` block (horizon + per-ticker quantiles +
        # drift + fan width). Without this copy, the only thing the PM
        # ever saw from Chronos was {signal, confidence} — losing the
        # entire predictive distribution to a UI-side fence parse.
        signals[ticker]["forecast"] = signals[ticker].get("reasoning")
        # Swap the structured reasoning dict for the Markdown blob we just
        # built — same content as the SSE analysis, fence and all. Two
        # reasons:
        #   1. ingest_run stringifies whatever reasoning is, so a dict
        #      becomes Python repr in the wiki — unparseable garbage.
        #      A Markdown string is human-readable AND machine-parseable
        #      (the fence survives intact).
        #   2. Letting the wiki carry the chart payload is what makes
        #      ForecasterNode able to rehydrate the chart on page reload —
        #      it reads /memory?flow_id and parses the same fence.
        signals[ticker]["reasoning"] = analysis
        progress.update_status(agent_id, ticker, "Done", analysis=analysis)

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(signals, f"Time Series Forecaster ({model_display})")
    state["data"]["analyst_signals"][agent_id] = signals
    message = HumanMessage(content=json.dumps(signals), name=agent_id)
    progress.update_status(agent_id, None, "Done")
    return {"messages": state["messages"] + [message], "data": data}


def _build_signal(
    last: float,
    q10: float,
    q50: float,
    q90: float,
    *,
    horizon_days: int = _PRED_LEN,
    frequency: str = "day",
    model_name: str = _MODEL_ID,
) -> dict:
    """Map a forecast fan to a ``{signal, confidence, reasoning}`` dict.

    Direction rule (cheapest first):
      • Quantiles unanimous up (q10 > last) → bullish; same-side bound past
        zero adds confidence.
      • Quantiles unanimous down (q90 < last) → bearish; symmetric.
      • Median directional by ≥ _NEUTRAL_PCT % with the fan straddling
        current price → directional but weaker.
      • Otherwise neutral.

    ``model_name`` flows into the reasoning dict so the PM and the wiki
    both reflect which backbone produced this fan.

    Confidence (0-100) blends magnitude and quantile agreement:
      • magnitude — |q50 pct change| / 10, capped at 1 (a 10% move = full).
      • agreement — how far the same-side bound (q10 for bull, q90 for
        bear) sits past zero, as a fraction of the q50 move.

    Both factors weighted 50/50 so a small but unanimous move and a large
    but split move land around the same mid-confidence.
    """
    if last <= 0:
        return {"signal": "neutral", "confidence": 0, "reasoning": {"error": "non-positive last close"}}

    pct_q10 = (q10 - last) / last * 100.0
    pct_q50 = (q50 - last) / last * 100.0
    pct_q90 = (q90 - last) / last * 100.0

    if abs(pct_q50) < _NEUTRAL_PCT or (pct_q10 < 0 < pct_q90 and abs(pct_q50) < 3):
        signal = "neutral"
        confidence = 50
        rule = "Fan straddles current price → neutral"
    elif pct_q50 > 0:
        signal = "bullish"
        magnitude = min(1.0, abs(pct_q50) / 10.0)
        agreement = max(0.0, min(1.0, pct_q10 / pct_q50))
        confidence = round((0.5 * magnitude + 0.5 * agreement) * 100)
        rule = (
            "Quantiles unanimous up → strong bullish"
            if pct_q10 > 0
            else "Median up, q10 still below current → bullish with reduced confidence"
        )
    else:
        signal = "bearish"
        magnitude = min(1.0, abs(pct_q50) / 10.0)
        agreement = max(0.0, min(1.0, pct_q90 / pct_q50))
        confidence = round((0.5 * magnitude + 0.5 * agreement) * 100)
        rule = (
            "Quantiles unanimous down → strong bearish"
            if pct_q90 < 0
            else "Median down, q90 still above current → bearish with reduced confidence"
        )

    return {
        "signal": signal,
        "confidence": max(0, min(100, int(confidence))),
        "reasoning": {
            "model": model_name,
            "horizon_days": horizon_days,
            "frequency": frequency,
            "last_close": round(last, 4),
            "forecast_end": {
                "q10": round(q10, 4),
                "q50": round(q50, 4),
                "q90": round(q90, 4),
            },
            "pct_change": {
                "q10": round(pct_q10, 2),
                "q50": round(pct_q50, 2),
                "q90": round(pct_q90, 2),
            },
            "rule": rule,
        },
    }


def _render_report(sig: dict) -> str:
    """Render a structured forecaster signal as a Markdown summary.

    Hand-rolled (no LLM) on purpose — the reasoning dict already carries
    everything a reader needs in plain numbers, and routing this through
    an LLM picker would imply the *forecast* depends on the LLM, which it
    doesn't. The forecast is whichever backbone fired; this is just its
    readout. The model name in the header comes from the backbone's own
    ``model_name`` so a Toto-2.0 fan reads "Datadog Toto-2.0" not
    "Amazon Chronos-2".
    """
    r = sig.get("reasoning", {}) or {}
    fc = r.get("forecast_end", {}) or {}
    pct = r.get("pct_change", {}) or {}
    last = r.get("last_close")
    horizon = r.get("horizon_days", _PRED_LEN)
    freq = r.get("frequency", "day")
    unit = _FREQ_UNIT.get(freq, "step")
    unit_plural = unit + ("" if unit.endswith("s") else "s")
    signal = str(sig.get("signal", "neutral")).upper()
    confidence = int(sig.get("confidence", 0))
    model_name = r.get("model") or "Amazon Chronos-2"
    # One-line backbone description so the report is self-describing about
    # which model produced these numbers. Falls back to the raw name when
    # the backbone is unknown — keeps the report rendering robust to
    # future additions.
    model_blurb = {
        "Amazon Chronos-2":
            "120M-param probabilistic time-series foundation model, run locally on cached weights.",
        "Datadog Toto-2.0":
            "313M-param decoder-only TS foundation model (Datadog observability pretraining; Apache 2.0).",
    }.get(model_name, "")

    def _pct(v) -> str:
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "—"
        return f"{'+' if v >= 0 else ''}{v:.2f}%"

    def _px(v) -> str:
        try:
            return f"${float(v):.2f}"
        except (TypeError, ValueError):
            return "—"

    return (
        f"**Signal:** {signal} · **Confidence:** {confidence}% · "
        f"**Horizon:** {horizon} {unit_plural}\n\n"
        f"**Model:** {model_name}{' — ' + model_blurb if model_blurb else ''} "
        f"**Bar frequency:** {freq}.\n\n"
        f"**At horizon end** vs last close ({_px(last)}):\n"
        f"- Lower bound (q10): {_pct(pct.get('q10'))} ({_px(fc.get('q10'))})\n"
        f"- Median (q50): {_pct(pct.get('q50'))} ({_px(fc.get('q50'))})\n"
        f"- Upper bound (q90): {_pct(pct.get('q90'))} ({_px(fc.get('q90'))})\n\n"
        f"**Rule:** {r.get('rule', '—')}\n\n"
        f"The fan chart on the node body shows the most recent history and the "
        f"per-step q10/q50/q90 trajectory."
    )
