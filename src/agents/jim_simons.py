"""Jim Simons / Renaissance-style quantitative analyst.

Faithful to the Medallion playbook: no fundamentals, no narratives, no LLM.
Reads price/volume only, hunts for statistically significant short-horizon
patterns (mean reversion the bread-and-butter, intraday seasonality, relative
strength vs the market), and emits a directional signal per ticker. Also
publishes a *recommended StrategyConfig* — many small uncorrelated bets,
~1% per-position cap, short holding period, no chart stops — that flows
through the node's `strategy` output handle into the Strategy node when wired.

What it computes per ticker (cheapest first):
  * **z-score** of last close vs N-bar moving average — primary mean-reversion
    signal; |z| > 2 fires.
  * **realized volatility** (annualised) — sizing input + part of the
    reasoning string.
  * **RS vs SPY** — relative-strength residual over the same window so a
    name being beat by the market gets flagged even when its own z-score
    is benign.

Decision rule (no LLM, no story):
  * z < −2 → bullish (oversold; mean-reversion long).
  * z > +2 → bearish (overbought; mean-reversion short).
  * |z| ≤ 2 → neutral.
  * confidence = min(|z| / 3, 1.0) × 100, capped at 100.

The reasoning string is terse and statistical — no narrative, no "we think
…" prose. Simons's voice: "z=−2.3, vol=18%/yr, RS=−1.1σ vs SPY.
Mean-reversion long. Hold ≤3 bars. Kill if z reverts past 0."

Fails open: missing prices, empty universe, all-flat-vol all skip the
ticker without raising. The rest of the pipeline keeps running.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from langchain_core.messages import HumanMessage

from src.graph.state import AgentState, show_agent_reasoning
from src.utils.progress import progress

logger = logging.getLogger(__name__)

# Tunables — chosen for the Medallion midpoint (1.5 days to ~1.5 weeks
# average holding). These are agent-level constants rather than node-config
# so the PM sees a stable signal shape regardless of which flow embeds Simons.
_LOOKBACK_BARS = 20            # bars used for the z-score / vol / RS window
_Z_THRESHOLD = 2.0             # |z| above this fires a signal
_BENCHMARK = "SPY"             # market proxy for the relative-strength leg
_MIN_HISTORY_BARS = 25         # below this, vol estimates aren't meaningful

# Valid bar frequencies — same lexicon the forecaster uses, kept in lock-step
# so a "Daily" Simons node behaves like a "Daily" forecaster node (provider
# chain via FD → Alpaca → Yahoo; intraday via yfinance with the published
# period caps).
_VALID_FREQUENCIES = ("day", "hour", "5min", "1min")
_YFI_INTERVAL = {"hour": "1h", "5min": "5m", "1min": "1m"}
_YFI_PERIOD = {"1m": "7d", "5m": "60d", "1h": "730d"}
# Bars-per-year for vol annualisation. Trading-day approximations: 252 daily
# (~78 5-min × 252 = 19,656 5-min bars, etc.). Hour assumes ~6.5 RTH hours/day.
_BARS_PER_YEAR = {"day": 252, "hour": 252 * 6.5, "5min": 252 * 78, "1min": 252 * 390}

# Static recommended StrategyConfig — Simons's playbook applied to the
# StrategyConfig shape the PM already reads. min_decision_interval_minutes
# is overridden dynamically to track whatever cadence the node is set to,
# but everything else here is fixed by the persona. The PM and Trading
# Account read this as if the user typed it into the Strategy node.
def _build_recommended_strategy(*, cadence_minutes: int) -> dict[str, Any]:
    return {
        "style": "mean_reversion",
        "sizing_rule": "risk_parity",        # vol-scaled — Simons auto-shrinks in vol surges
        "max_position_pct": 1.0,             # "never over-weight one bet"
        "max_sector_pct": 20.0,              # soft floor on sector concentration
        "holding_period": "day",             # short — Medallion midpoint
        "stop_loss_pct": None,               # risk via sizing, not chart stops
        "take_profit_pct": None,
        "allow_stocks": True,
        "allow_options": False,
        "allow_etfs": True,
        "note": (
            "Simons/Renaissance-style: mean reversion + intraday seasonality. "
            "No narratives, no fundamentals. Many small uncorrelated bets, ~1%/name. "
            "Vol-scaled sizing. Kill decaying signals. We don't override the model."
        ),
        # Cadence-driven throttles.
        "min_decision_interval_minutes": float(cadence_minutes),
        "price_move_threshold_pct": 0.3,
        # Max signal age = cadence × 12. At 5-min cadence → 1h; at hourly → 12h.
        # Short by design: stale Simons signals aren't worth holding through.
        "max_signal_age_hours": max(1.0, cadence_minutes * 12 / 60.0),
    }


# Cadence string → minutes. Matches the trade scheduler's _INTERVALS lexicon
# (5min / 15min / hourly) plus a 1min entry for the truest-to-Medallion mode.
# "off" → 0 means "Simons isn't on its own clock"; the flow only runs Simons
# when a normal play is triggered.
_CADENCE_MINUTES = {"off": 0, "1min": 1, "5min": 5, "15min": 15, "hourly": 60}


def cadence_minutes(cadence: str | None) -> int:
    """Translate a cadence string into the throttle-minutes value Simons publishes
    into its recommended StrategyConfig. Defaults to 5 (the Medallion midpoint
    for intraday signal cadence) for unknown strings rather than 0 — a zero
    interval would make the PM re-fire on every tick, defeating the throttle."""
    if not cadence:
        return 5
    return _CADENCE_MINUTES.get(str(cadence).lower(), 5)


def recommended_strategy_for_cadence(cadence: str | None) -> dict[str, Any]:
    """Public hook used by both the analyst itself and the simons_executor /
    run-assembler paths. Single source of truth so the Strategy override the
    user sees on the canvas and the strategy the run uses are byte-identical."""
    return _build_recommended_strategy(cadence_minutes=max(5, cadence_minutes(cadence)))


# --- price fetching -------------------------------------------------------

def _resolve_frequency(state: AgentState) -> str:
    """Per-flow bar frequency. Mirrors forecaster._resolve_frequency so a
    flow that pins both the forecaster and Simons to '5min' gets matched
    bar timings. Default 'day'."""
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
    """Per-flow Simons cadence — defaults to '5min'. Used to compute the
    recommended StrategyConfig that lands on the Strategy node."""
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
    """Per-flow lookback override. Defaults to the module constant. Clamped
    to [10, 500] so a malformed value can't blow up the vol/z math."""
    try:
        req = state.get("metadata", {}).get("request")
        if req is not None:
            n = getattr(req, "simons_lookback_bars", None)
            if n is not None:
                return max(10, min(500, int(n)))
    except Exception:
        pass
    return _LOOKBACK_BARS


def _fetch_closes(ticker: str, end_date: str, frequency: str, count: int) -> np.ndarray:
    """Pull `count` close bars at `frequency`. yfinance throughout — same
    rationale the forecaster gives: FD's free-tier capped at ~750 bars,
    yfinance has deeper history for daily and is the only free source for
    intraday. Returns an empty array on any failure; the caller treats
    that as "skip this ticker"."""
    try:
        import yfinance as yf  # lazy — already a dep, but keeps cold-start cheap
    except Exception as e:  # yfinance missing → all tickers skip cleanly
        logger.warning("yfinance unavailable for Simons: %s", e)
        return np.array([], dtype=np.float32)

    try:
        if frequency == "day":
            # ~1.6× overshoot on calendar days for weekends/holidays.
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
                return np.array([], dtype=np.float32)
            period = _YFI_PERIOD[interval]
            df = yf.download(
                tickers=ticker, period=period, interval=interval,
                progress=False, auto_adjust=True, threads=False,
            )
        if df is None or df.empty:
            return np.array([], dtype=np.float32)
        close = df["Close"]
        if hasattr(close, "columns"):  # multi-ticker DataFrame collapse
            close = close.iloc[:, 0]
        return pd.to_numeric(close, errors="coerce").dropna().tail(count).to_numpy(dtype=np.float32)
    except Exception as e:
        logger.warning("Simons price fetch failed for %s @ %s: %s", ticker, frequency, e)
        return np.array([], dtype=np.float32)


# --- signal math ----------------------------------------------------------

def _z_score(series: np.ndarray) -> float:
    """Last close's z-score against the rolling MA over the same window.
    Returns 0.0 when the std collapses (constant series, single bar)."""
    if series.size < 2:
        return 0.0
    mean = float(series.mean())
    std = float(series.std(ddof=0))
    if not np.isfinite(std) or std <= 0:
        return 0.0
    return (float(series[-1]) - mean) / std


def _realized_vol_pct(series: np.ndarray, frequency: str) -> float:
    """Annualised realised vol expressed as a percent. Log returns; bars-per-year
    chosen by frequency. Caps at 500% to keep the reasoning string readable
    if a thin name produces a freak number."""
    if series.size < 3:
        return 0.0
    rets = np.diff(np.log(series))
    sd = float(np.std(rets, ddof=0))
    if not np.isfinite(sd) or sd <= 0:
        return 0.0
    bars_per_year = _BARS_PER_YEAR.get(frequency, 252)
    vol = sd * np.sqrt(bars_per_year) * 100.0
    return min(500.0, vol)


def _relative_strength_sigma(ticker_series: np.ndarray, benchmark_series: np.ndarray) -> float:
    """Z-score of the ticker's last-period return vs the benchmark's last-period
    return, measured in σ of the spread distribution. Both series clipped to
    matched length on the tail — yfinance can return one extra bar for the
    benchmark on a partial day so equal lengths aren't guaranteed.

    Returns 0.0 when the benchmark series is too short, too flat, or has a
    non-positive last price. Standard Simons-style residual: the leg flags
    a name that's been beat by the market even when its own absolute move
    is benign."""
    if ticker_series.size < 5 or benchmark_series.size < 5:
        return 0.0
    n = min(ticker_series.size, benchmark_series.size)
    t = ticker_series[-n:]
    b = benchmark_series[-n:]
    if t[0] <= 0 or b[0] <= 0:
        return 0.0
    t_ret = (t[-1] - t[0]) / t[0]
    b_ret = (b[-1] - b[0]) / b[0]
    spread = (t[1:] - t[:-1]) / np.maximum(t[:-1], 1e-9) - (b[1:] - b[:-1]) / np.maximum(b[:-1], 1e-9)
    sd = float(np.std(spread, ddof=0))
    if not np.isfinite(sd) or sd <= 0:
        return 0.0
    diff = t_ret - b_ret
    return float(diff / sd)


def _build_signal(
    *, z: float, vol_pct: float, rs_sigma: float, frequency: str, lookback: int
) -> dict:
    """Map the three numbers into the standard {signal, confidence, reasoning}
    shape the PM consumes. Confidence is bounded 0-100 and weighted toward
    z magnitude — RS is a supporting tie-breaker, not a primary driver."""
    if abs(z) < _Z_THRESHOLD:
        signal = "neutral"
        confidence = 0
        rule = f"|z|={abs(z):.2f} below {_Z_THRESHOLD:.1f}σ trigger — no entry"
    elif z < 0:
        signal = "bullish"  # oversold → mean-reversion long
        confidence = int(round(min(100.0, abs(z) / 3.0 * 100.0)))
        # RS adds confirmation: if name lagging the market (-RS) AND z is
        # negative, the mean-reversion thesis strengthens; if RS is strongly
        # positive (already outperforming) we trim 20 confidence.
        if rs_sigma > 0.5:
            confidence = max(0, confidence - 20)
        rule = (
            f"z={z:.2f}σ vs {lookback}-bar MA — mean-reversion long. "
            f"RS={rs_sigma:+.2f}σ vs {_BENCHMARK}."
        )
    else:
        signal = "bearish"
        confidence = int(round(min(100.0, abs(z) / 3.0 * 100.0)))
        if rs_sigma < -0.5:
            confidence = max(0, confidence - 20)
        rule = (
            f"z={z:.2f}σ vs {lookback}-bar MA — mean-reversion short. "
            f"RS={rs_sigma:+.2f}σ vs {_BENCHMARK}."
        )

    reasoning_md = (
        f"**z-score:** {z:+.2f}σ over {lookback} {frequency} bars · "
        f"**vol:** {vol_pct:.1f}%/yr · "
        f"**RS vs {_BENCHMARK}:** {rs_sigma:+.2f}σ\n\n"
        f"**Rule:** {rule}\n\n"
        f"**Kill condition:** signal reverts past zero, vol regime shifts, "
        f"or hit rate decays. We don't override the model."
    )
    return {
        "signal": signal,
        "confidence": int(max(0, min(100, confidence))),
        "reasoning": reasoning_md,
        # Structured payload — kept alongside the markdown so a future
        # downstream consumer (e.g. a per-ticker chart on the node) can
        # read the numbers without parsing prose.
        "simons": {
            "z_score": round(float(z), 4),
            "realized_vol_pct": round(float(vol_pct), 2),
            "rs_vs_benchmark_sigma": round(float(rs_sigma), 4),
            "lookback_bars": int(lookback),
            "frequency": frequency,
            "threshold": _Z_THRESHOLD,
        },
    }


# --- agent entry point ----------------------------------------------------

def jim_simons_agent(state: AgentState, agent_id: str = "jim_simons_agent"):
    """Run Simons-style stats on each ticker and emit a directional signal.

    Mirrors the forecaster agent's contract:
      * reads `state["data"]["tickers"]` and `state["data"]["end_date"]`
      * writes `state["data"]["analyst_signals"][agent_id]`
      * progress.update_status per ticker for the SSE stream
      * fails open on missing prices / unsupported frequency
    """
    data = state["data"]
    tickers = data["tickers"]
    end_date = data["end_date"]
    frequency = _resolve_frequency(state)
    lookback = _resolve_lookback(state)
    cadence = _resolve_cadence(state)

    # Fetch the benchmark series once — re-used in every ticker's RS leg.
    progress.update_status(agent_id, None, f"Fetching {_BENCHMARK} benchmark")
    bench_closes = _fetch_closes(_BENCHMARK, end_date, frequency, max(lookback * 2, _MIN_HISTORY_BARS))

    signals: dict[str, dict] = {}
    for ticker in tickers:
        progress.update_status(agent_id, ticker, f"Fetching {frequency} bars")
        closes = _fetch_closes(ticker, end_date, frequency, max(lookback * 2, _MIN_HISTORY_BARS))
        if closes.size == 0:
            progress.update_status(agent_id, ticker, "Failed: no bars available")
            continue
        if closes.size < _MIN_HISTORY_BARS:
            progress.update_status(agent_id, ticker, "Failed: insufficient history")
            continue

        progress.update_status(agent_id, ticker, "Computing signal")
        window = closes[-lookback:]
        z = _z_score(window)
        vol_pct = _realized_vol_pct(window, frequency)
        rs_sigma = _relative_strength_sigma(window, bench_closes[-lookback:] if bench_closes.size >= lookback else bench_closes)
        sig = _build_signal(z=z, vol_pct=vol_pct, rs_sigma=rs_sigma, frequency=frequency, lookback=lookback)
        # Skip neutrals from the wiki write — Simons's principle is many
        # small bets, but writing "no entry" insights every cadence floods
        # the track-record view with noise. The PM still infers "no signal"
        # from the absence of a row, same as any other analyst that didn't fire.
        if sig["signal"] == "neutral":
            progress.update_status(agent_id, ticker, "Done (no signal)", analysis=sig["reasoning"])
            continue
        signals[ticker] = sig
        progress.update_status(agent_id, ticker, "Done", analysis=sig["reasoning"])

    # Always publish the recommended strategy on the state's metadata so
    # the run executor / trade executor can pick it up — even if no tickers
    # fired, the strategy override (style / sizing / cadence) is still valid.
    state.setdefault("data", {}).setdefault("simons", {})["recommended_strategy"] = recommended_strategy_for_cadence(cadence)

    if state["metadata"].get("show_reasoning"):
        show_agent_reasoning(signals, "Jim Simons")
    state["data"]["analyst_signals"][agent_id] = signals
    message = HumanMessage(content=json.dumps(signals), name=agent_id)
    progress.update_status(agent_id, None, "Done")
    return {"messages": state["messages"] + [message], "data": data}
