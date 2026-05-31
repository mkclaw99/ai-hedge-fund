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
from threading import Lock

import numpy as np
import pandas as pd
from langchain_core.messages import HumanMessage

from src.graph.state import AgentState, show_agent_reasoning
from src.tools.api import get_prices, prices_to_df
from src.utils.analyst_report import write_analyst_report
from src.utils.api_key import get_api_key_from_state
from src.utils.progress import progress

logger = logging.getLogger(__name__)

# Tunables — defaults chosen for a swing-trade horizon. These are
# agent-level constants rather than node-config so the PM sees a stable
# signal shape regardless of which flow embeds the forecaster.
_MODEL_ID = "amazon/chronos-2"
_CONTEXT_LEN = 256              # last N daily closes fed to the model
_PRED_LEN = 10                  # trading-day horizon (~2 weeks)
_QUANTILES = [0.1, 0.5, 0.9]    # probabilistic fan width
_NEUTRAL_PCT = 1.0              # |q50_pct| below this → neutral

_pipeline = None
_pipeline_lock = Lock()


def _load_pipeline():
    """Lazy, process-wide singleton load of Chronos-2.

    All imports are local so a missing chronos / torch install doesn't
    break the rest of the app on import. Returns None on any failure;
    callers must treat that as "skip the analyst, keep the pipeline".
    """
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline
        try:
            from chronos import Chronos2Pipeline  # lazy import
        except Exception as e:
            logger.warning("chronos-forecasting not importable: %s", e)
            return None
        try:
            # device_map="auto" picks CUDA → MPS → CPU in that order; on
            # Apple Silicon you get MPS for free, on CPU-only it still works
            # (slow but usable for ~10-step forecasts on a handful of tickers).
            _pipeline = Chronos2Pipeline.from_pretrained(_MODEL_ID, device_map="auto")
        except Exception as e:
            logger.warning("Chronos-2 load failed: %s", e)
            return None
    return _pipeline


def forecaster_agent(state: AgentState, agent_id: str = "forecaster_agent"):
    """Run Chronos-2 on each ticker and emit a directional signal."""
    data = state["data"]
    tickers = data["tickers"]
    end_date = data["end_date"]
    api_key = get_api_key_from_state(state, "FINANCIAL_DATASETS_API_KEY")

    # Collect price history per ticker first. Fast, and lets us bail out
    # cleanly if the model is unavailable without paying the load cost.
    series_by_ticker: dict[str, np.ndarray] = {}
    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Fetching price history")
        # Pull ~CONTEXT_LEN trading days. Overshoot by 1.6× on calendar days
        # to cover weekends/holidays; we trim back to CONTEXT_LEN below.
        try:
            start = (datetime.fromisoformat(end_date) - timedelta(days=int(_CONTEXT_LEN * 1.6))).date().isoformat()
        except Exception:
            start = end_date
        prices = get_prices(ticker=ticker, start_date=start, end_date=end_date, api_key=api_key)
        if not prices:
            progress.update_status(agent_id, ticker, "Failed: no price history")
            continue
        df = prices_to_df(prices)
        if df.empty or "close" not in df.columns:
            progress.update_status(agent_id, ticker, "Failed: empty price frame")
            continue
        closes = pd.to_numeric(df["close"], errors="coerce").dropna().tail(_CONTEXT_LEN).to_numpy(dtype=np.float32)
        if closes.size < 30:
            # Chronos can technically handle short context, but a forecast
            # off ~one month of history is noise. Drop it.
            progress.update_status(agent_id, ticker, "Failed: insufficient history")
            continue
        series_by_ticker[ticker] = closes

    if not series_by_ticker:
        state["data"]["analyst_signals"][agent_id] = {}
        progress.update_status(agent_id, None, "Done")
        return {"messages": state["messages"], "data": data}

    # Load the model once per process. First call downloads ~480 MB and
    # warms the cache; subsequent calls are ~free.
    for t in series_by_ticker:
        progress.update_status(agent_id, t, "Loading Chronos-2")
    pipeline = _load_pipeline()
    if pipeline is None:
        for t in series_by_ticker:
            progress.update_status(agent_id, t, "Skipped: Chronos-2 unavailable")
        state["data"]["analyst_signals"][agent_id] = {}
        progress.update_status(agent_id, None, "Done")
        return {"messages": state["messages"], "data": data}

    # Batch every ticker into one predict_quantiles call. We use the array-
    # based API (rather than predict_df) so we don't fight Chronos's date-
    # frequency inference — daily equity series skip weekends/holidays,
    # which trips the validator. The values are what we care about anyway.
    for t in series_by_ticker:
        progress.update_status(agent_id, t, "Running forecast")
    tickers_ordered = list(series_by_ticker.keys())
    inputs = [series_by_ticker[t] for t in tickers_ordered]
    try:
        quantile_tensors, _means = pipeline.predict_quantiles(
            inputs,
            prediction_length=_PRED_LEN,
            quantile_levels=_QUANTILES,
        )
    except Exception as e:
        logger.warning("Chronos-2 forecast failed: %s", e)
        for t in series_by_ticker:
            progress.update_status(agent_id, t, "Failed: forecast error")
        state["data"]["analyst_signals"][agent_id] = {}
        progress.update_status(agent_id, None, "Done")
        return {"messages": state["messages"], "data": data}

    signals: dict[str, dict] = {}
    for ticker, qt in zip(tickers_ordered, quantile_tensors):
        last_close = float(series_by_ticker[ticker][-1])
        # qt shape: (1, prediction_length, num_quantiles). The leading dim
        # is the (per-series) batch axis from Chronos's internal padding.
        # Quantile order matches _QUANTILES (= [0.1, 0.5, 0.9]); use the
        # final forecast step.
        try:
            arr = qt.detach().cpu().numpy() if hasattr(qt, "detach") else np.asarray(qt)
            arr = np.squeeze(arr, axis=0) if arr.ndim == 3 else arr
            q10 = float(arr[-1, 0])
            q50 = float(arr[-1, 1])
            q90 = float(arr[-1, 2])
        except Exception as e:
            logger.warning("Chronos-2 unparseable output for %s: %s", ticker, e)
            continue
        signals[ticker] = _build_signal(last_close, q10, q50, q90)
        progress.update_status(agent_id, ticker, "Done", analysis=write_analyst_report(
            agent_id, "Time Series Forecaster", ticker,
            signals[ticker]["signal"], signals[ticker]["confidence"],
            signals[ticker], state,
        ))

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(signals, "Time Series Forecaster")
    state["data"]["analyst_signals"][agent_id] = signals
    message = HumanMessage(content=json.dumps(signals), name=agent_id)
    progress.update_status(agent_id, None, "Done")
    return {"messages": state["messages"] + [message], "data": data}


def _build_signal(last: float, q10: float, q50: float, q90: float) -> dict:
    """Map a forecast fan to a ``{signal, confidence, reasoning}`` dict.

    Direction rule (cheapest first):
      • Quantiles unanimous up (q10 > last) → bullish; same-side bound past
        zero adds confidence.
      • Quantiles unanimous down (q90 < last) → bearish; symmetric.
      • Median directional by ≥ _NEUTRAL_PCT % with the fan straddling
        current price → directional but weaker.
      • Otherwise neutral.

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
            "model": _MODEL_ID,
            "horizon_days": _PRED_LEN,
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
