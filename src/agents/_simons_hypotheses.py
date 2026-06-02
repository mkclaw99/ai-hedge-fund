"""Hypothesis registry for the Jim Simons analyst's hypothesize-test-adjudicate loop.

Each hypothesis is a (name, llm_description, test_fn) triple. The LLM proposes a
subset of hypotheses to test for a given ticker right now (Stage 1, "hypothesize").
Each test runs deterministically on price/volume data and returns a structured
result (Stage 2, "test"). The LLM then adjudicates which passing hypothesis wins,
if any (Stage 3, "adjudicate"). The split keeps creativity LLM-side and rigor
code-side — same separation Renaissance ran for decades.

Each ``test_fn`` signature is:

    test(closes, bench_closes, volumes, frequency, lookback, end_date) -> dict

Return shape (all hypotheses use the same keys for the trace dialog):

    {
        "passed": bool,
        "value": float | None,    # the headline test statistic
        "threshold": float | None,# what value would pass
        "detail": str,            # one-line human-readable summary
    }

Why each hypothesis is in v1:

* mean_reversion / pairs_divergence — Renaissance's bread-and-butter stat-arb legs.
* vol_regime_shift — vol surges often precede reversals; tells the LLM when to
  trust mean reversion vs back off.
* gap_fade — overnight gaps that fail to follow through were a known Medallion edge.
* volume_signature — high-volume reversals; volume confirms or undermines the price
  signal.
* calendar_tilt — seasonality (day-of-week, turn-of-month) was an explicit Renaissance
  signal family.

Easy to extend: drop another entry into REGISTRY with a new test_fn. The LLM
prompt is built from REGISTRY, so adding a hypothesis automatically makes it
available to the proposer without prompt edits.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Callable

import numpy as np


# Bars-per-year for vol annualisation. Matches src/agents/jim_simons.py — keep
# in sync if the source ever moves to a shared constants module.
_BARS_PER_YEAR = {"day": 252, "hour": 252 * 6.5, "5min": 252 * 78, "1min": 252 * 390}


@dataclass(frozen=True)
class Hypothesis:
    """A testable pattern the LLM can request from the proposal stage.

    ``llm_description`` is what the proposer sees in its prompt — keep it
    crisp; the LLM is choosing from this list. ``test_fn`` is what runs in
    Stage 2; it MUST be deterministic and fast (no I/O, no network).
    """
    name: str
    llm_description: str
    test_fn: Callable[..., dict]


# --- tests --------------------------------------------------------------------

def _z_score(series: np.ndarray) -> float:
    """Last-value z-score against the rolling mean over the same window.
    Returns 0.0 on degenerate input (constant series, single bar)."""
    if series.size < 2:
        return 0.0
    mean = float(series.mean())
    std = float(series.std(ddof=0))
    if not np.isfinite(std) or std <= 0:
        return 0.0
    return (float(series[-1]) - mean) / std


def _realized_vol_pct(series: np.ndarray, frequency: str) -> float:
    """Annualised realised vol as a percent. Capped at 500% to keep the trace
    readable when a thin name produces a freak number."""
    if series.size < 3:
        return 0.0
    rets = np.diff(np.log(np.maximum(series, 1e-9)))
    sd = float(np.std(rets, ddof=0))
    if not np.isfinite(sd) or sd <= 0:
        return 0.0
    bpy = _BARS_PER_YEAR.get(frequency, 252)
    return float(min(500.0, sd * np.sqrt(bpy) * 100.0))


def test_mean_reversion(
    closes: np.ndarray, bench_closes: np.ndarray, volumes: np.ndarray | None,
    frequency: str, lookback: int, end_date: str, threshold: float = 2.0,
) -> dict:
    """Pass when last close is more than ``threshold`` standard deviations from
    the rolling MA — entry signal for a mean-reversion trade."""
    window = closes[-lookback:]
    z = _z_score(window)
    passed = abs(z) >= threshold
    direction = "fade-up (short)" if z > 0 else "fade-down (long)" if z < 0 else "no direction"
    return {
        "passed": bool(passed),
        "value": round(z, 3),
        "threshold": threshold,
        "detail": f"z = {z:+.2f}σ over {lookback} {frequency} bars → {direction}",
    }


def test_pairs_divergence(
    closes: np.ndarray, bench_closes: np.ndarray, volumes: np.ndarray | None,
    frequency: str, lookback: int, end_date: str, threshold: float = 2.0,
) -> dict:
    """Pass when the ticker's recent return diverges from SPY's by more than
    ``threshold`` σ of the spread distribution. Long the laggard, short the
    leader — the classic stat-arb pair leg."""
    n = min(closes.size, bench_closes.size, lookback)
    if n < 5:
        return {"passed": False, "value": None, "threshold": threshold,
                "detail": "insufficient overlapping history"}
    t = closes[-n:]
    b = bench_closes[-n:]
    if t[0] <= 0 or b[0] <= 0:
        return {"passed": False, "value": None, "threshold": threshold,
                "detail": "non-positive opening price"}
    t_ret = (t[-1] - t[0]) / t[0]
    b_ret = (b[-1] - b[0]) / b[0]
    spread = (t[1:] - t[:-1]) / np.maximum(t[:-1], 1e-9) - (b[1:] - b[:-1]) / np.maximum(b[:-1], 1e-9)
    sd = float(np.std(spread, ddof=0))
    if not np.isfinite(sd) or sd <= 0:
        return {"passed": False, "value": None, "threshold": threshold,
                "detail": "spread variance collapsed"}
    sigma = (t_ret - b_ret) / sd
    passed = abs(sigma) >= threshold
    direction = "ticker leading (short ticker / long SPY)" if sigma > 0 \
        else "ticker lagging (long ticker / short SPY)" if sigma < 0 else "in line"
    return {
        "passed": bool(passed),
        "value": round(float(sigma), 3),
        "threshold": threshold,
        "detail": f"spread = {sigma:+.2f}σ over {n} bars → {direction}",
    }


def test_vol_regime_shift(
    closes: np.ndarray, bench_closes: np.ndarray, volumes: np.ndarray | None,
    frequency: str, lookback: int, end_date: str,
    high_ratio: float = 1.5, low_ratio: float = 0.5,
) -> dict:
    """Pass when current short-term realised vol is materially higher or lower
    than its longer-term median — a regime shift the LLM may want to act on
    (vol spike → reversal candidate; vol crush → momentum candidate)."""
    # Short window: last ``lookback`` bars; baseline: 3× lookback for the
    # median. Both annualised so the ratio is dimensionless.
    short = closes[-lookback:]
    baseline_window = closes[-(lookback * 3):-lookback] if closes.size >= lookback * 4 else closes[:-lookback]
    if baseline_window.size < 5:
        return {"passed": False, "value": None, "threshold": high_ratio,
                "detail": "insufficient baseline window"}
    vol_short = _realized_vol_pct(short, frequency)
    vol_base = _realized_vol_pct(baseline_window, frequency)
    if vol_base <= 0:
        return {"passed": False, "value": None, "threshold": high_ratio,
                "detail": "baseline vol degenerate"}
    ratio = vol_short / vol_base
    passed = ratio >= high_ratio or ratio <= low_ratio
    regime = "vol spike" if ratio >= high_ratio else "vol crush" if ratio <= low_ratio else "vol stable"
    return {
        "passed": bool(passed),
        "value": round(ratio, 3),
        "threshold": high_ratio if ratio >= 1 else low_ratio,
        "detail": f"short vol {vol_short:.1f}% / baseline {vol_base:.1f}% = {ratio:.2f}x → {regime}",
    }


def test_gap_fade(
    closes: np.ndarray, bench_closes: np.ndarray, volumes: np.ndarray | None,
    frequency: str, lookback: int, end_date: str,
    min_gap_pct: float = 1.0, max_follow_pct: float = 0.3,
) -> dict:
    """Pass when there's a meaningful gap between the previous bar's close and
    the last bar's open-equivalent (= the bar before's last value), AND
    follow-through has been muted — the classic exhaustion-gap fade.

    Note: with close-only data we approximate the gap as the bar-to-bar
    return; with intraday data this is the actual bar gap. Imperfect but
    enough to filter the LLM's proposals when gap-like price action happened."""
    if closes.size < 3:
        return {"passed": False, "value": None, "threshold": min_gap_pct,
                "detail": "insufficient bars"}
    prev2 = float(closes[-3])
    prev = float(closes[-2])
    last = float(closes[-1])
    if prev2 <= 0 or prev <= 0:
        return {"passed": False, "value": None, "threshold": min_gap_pct,
                "detail": "non-positive close"}
    gap_pct = (prev - prev2) / prev2 * 100.0
    follow_pct = (last - prev) / prev * 100.0
    # Fade setup: gap is significant AND the follow-through is small or
    # opposite in sign (the gap "failed to continue").
    gap_significant = abs(gap_pct) >= min_gap_pct
    follow_weak = abs(follow_pct) <= max_follow_pct or np.sign(follow_pct) != np.sign(gap_pct)
    passed = gap_significant and follow_weak
    direction = ("fade up-gap (short)" if gap_pct > 0 else "fade down-gap (long)") if passed else "—"
    return {
        "passed": bool(passed),
        "value": round(gap_pct, 3),
        "threshold": min_gap_pct,
        "detail": f"gap {gap_pct:+.2f}%, follow-through {follow_pct:+.2f}% → {direction if passed else 'no setup'}",
    }


def test_volume_signature(
    closes: np.ndarray, bench_closes: np.ndarray, volumes: np.ndarray | None,
    frequency: str, lookback: int, end_date: str,
    vol_ratio_threshold: float = 2.5, min_price_change_pct: float = 0.5,
) -> dict:
    """Pass when the last bar's volume is materially above its rolling median
    AND the price move on that bar is large enough to suggest a real flow event
    (not just noise on a sleepy bar)."""
    if volumes is None or volumes.size < 5 or closes.size < 5:
        return {"passed": False, "value": None, "threshold": vol_ratio_threshold,
                "detail": "no volume series available"}
    n = min(volumes.size, closes.size, lookback)
    if n < 5:
        return {"passed": False, "value": None, "threshold": vol_ratio_threshold,
                "detail": "insufficient overlapping bars"}
    vols = volumes[-n:]
    cl = closes[-n:]
    median_vol = float(np.median(vols[:-1]))  # exclude current bar
    if median_vol <= 0:
        return {"passed": False, "value": None, "threshold": vol_ratio_threshold,
                "detail": "median volume is zero (illiquid name?)"}
    ratio = float(vols[-1]) / median_vol
    pct_change = (float(cl[-1]) - float(cl[-2])) / float(cl[-2]) * 100.0 if cl[-2] > 0 else 0.0
    passed = ratio >= vol_ratio_threshold and abs(pct_change) >= min_price_change_pct
    direction = ("volume-confirmed up (continuation long)" if pct_change > 0
                 else "volume-confirmed down (continuation short)") if passed else "—"
    return {
        "passed": bool(passed),
        "value": round(ratio, 2),
        "threshold": vol_ratio_threshold,
        "detail": f"vol = {ratio:.2f}x median, last-bar return {pct_change:+.2f}% → {direction if passed else 'noise / quiet bar'}",
    }


def test_calendar_tilt(
    closes: np.ndarray, bench_closes: np.ndarray, volumes: np.ndarray | None,
    frequency: str, lookback: int, end_date: str,
    min_effect_sigma: float = 1.0,
) -> dict:
    """Pass when today's calendar position has historically shown a meaningful
    return bias over the lookback window. Tests day-of-week (Monday vs the
    rest), turn-of-month (last/first 3 trading days), pre-holiday days.

    Crude implementation: we can't infer holidays from price bars alone, so we
    score the day-of-week effect only. Sufficient for a v1 — Renaissance's
    calendar legs went much deeper. The proposer LLM still gets to mention
    other calendar features in its rationale; only the test is narrow."""
    if closes.size < 30:
        return {"passed": False, "value": None, "threshold": min_effect_sigma,
                "detail": "need ≥30 bars for calendar inference"}
    try:
        today = _dt.date.fromisoformat(end_date[:10])
    except (TypeError, ValueError):
        today = _dt.date.today()
    dow = today.weekday()  # 0=Mon, 4=Fri
    # Compute bar-to-bar returns then partition by weekday assuming daily
    # cadence (one bar = one trading day). For intraday cadences this falls
    # back to "treat the whole window as one weekday" which means the test
    # is mostly a noop — accept that; calendar tilts are weak on intraday.
    if frequency != "day":
        return {"passed": False, "value": None, "threshold": min_effect_sigma,
                "detail": f"calendar tilt only meaningful on daily bars (current: {frequency})"}
    rets = np.diff(closes) / np.maximum(closes[:-1], 1e-9) * 100.0
    n = rets.size
    # Reconstruct each return's weekday by walking back from today, skipping
    # weekends. yfinance daily bars skip weekends already, so each return
    # corresponds to a trading day; just count backwards in trading-day units.
    weekdays: list[int] = []
    cur = today
    for _ in range(n):
        # Step back one trading day
        cur = cur - _dt.timedelta(days=1)
        while cur.weekday() >= 5:  # skip Sat/Sun
            cur = cur - _dt.timedelta(days=1)
        weekdays.append(cur.weekday())
    weekdays = list(reversed(weekdays))
    bucket = np.array([r for r, w in zip(rets, weekdays) if w == dow])
    other = np.array([r for r, w in zip(rets, weekdays) if w != dow])
    if bucket.size < 4 or other.size < 5:
        return {"passed": False, "value": None, "threshold": min_effect_sigma,
                "detail": "not enough same-weekday samples"}
    # Effect size: mean diff / SE of the difference. Conservative — no
    # multi-test correction since the LLM only requests this test sometimes.
    mean_b, mean_o = float(bucket.mean()), float(other.mean())
    se = float(np.sqrt(bucket.var(ddof=0) / bucket.size + other.var(ddof=0) / other.size))
    if se <= 0:
        return {"passed": False, "value": None, "threshold": min_effect_sigma,
                "detail": "variance collapsed"}
    effect_sigma = (mean_b - mean_o) / se
    passed = abs(effect_sigma) >= min_effect_sigma
    dow_name = ["Mon", "Tue", "Wed", "Thu", "Fri"][dow] if dow < 5 else "Weekend"
    direction = ("favourable" if effect_sigma > 0 else "unfavourable") if passed else "—"
    return {
        "passed": bool(passed),
        "value": round(float(effect_sigma), 2),
        "threshold": min_effect_sigma,
        "detail": (
            f"{dow_name} effect: {mean_b:+.2f}% vs other-day {mean_o:+.2f}%, "
            f"{effect_sigma:+.2f}σ → {direction if passed else 'no edge'}"
        ),
    }


# --- registry -----------------------------------------------------------------

REGISTRY: dict[str, Hypothesis] = {
    "mean_reversion": Hypothesis(
        name="mean_reversion",
        llm_description=(
            "Last close is an outlier (>2σ) from the recent rolling mean — entry signal "
            "for a fade. Bread-and-butter Medallion leg. Strongest when realized vol is "
            "stable (no regime shift)."
        ),
        test_fn=test_mean_reversion,
    ),
    "pairs_divergence": Hypothesis(
        name="pairs_divergence",
        llm_description=(
            "Ticker's recent return has diverged from SPY's by >2σ of the spread distribution. "
            "Classic stat-arb pair: long the laggard, short the leader. Market-neutral by construction."
        ),
        test_fn=test_pairs_divergence,
    ),
    "vol_regime_shift": Hypothesis(
        name="vol_regime_shift",
        llm_description=(
            "Current short-term realized vol differs >1.5x from its longer-term median. "
            "Vol spike → mean-reversion candidate; vol crush → momentum candidate. "
            "Useful for sizing/timing of other hypotheses."
        ),
        test_fn=test_vol_regime_shift,
    ),
    "gap_fade": Hypothesis(
        name="gap_fade",
        llm_description=(
            "Meaningful bar-to-bar gap (>1%) with muted follow-through — exhaustion gap. "
            "Setup: fade the gap direction. Works best on liquid names; thin tickers gap on "
            "nothing all the time."
        ),
        test_fn=test_gap_fade,
    ),
    "volume_signature": Hypothesis(
        name="volume_signature",
        llm_description=(
            "Last bar's volume is >2.5x rolling median AND price moved >0.5%. "
            "Real flow event (not just noise). Direction: continuation in the price direction."
        ),
        test_fn=test_volume_signature,
    ),
    "calendar_tilt": Hypothesis(
        name="calendar_tilt",
        llm_description=(
            "Today's day-of-week has historically shown a >1σ return bias on this ticker "
            "over the lookback. Weakest of the legs (low effect sizes), but free if you're "
            "already in a position; weights other signals up or down."
        ),
        test_fn=test_calendar_tilt,
    ),
}


def prompt_palette() -> str:
    """Markdown bullet list of hypotheses for the proposer prompt.

    Rendered once per agent call (cheap). The proposer picks 2-4 names from
    this list; anything not on it is silently dropped, so the LLM can't
    invent new tests (would be effectively code injection).
    """
    lines = []
    for h in REGISTRY.values():
        lines.append(f"- **{h.name}** — {h.llm_description}")
    return "\n".join(lines)


def run_test(
    name: str,
    *,
    closes: np.ndarray,
    bench_closes: np.ndarray,
    volumes: np.ndarray | None,
    frequency: str,
    lookback: int,
    end_date: str,
) -> dict:
    """Run a single hypothesis test by name. Unknown names return a uniform
    failure result so the proposer / adjudicator path stays consistent."""
    h = REGISTRY.get(name)
    if h is None:
        return {
            "passed": False, "value": None, "threshold": None,
            "detail": f"unknown hypothesis '{name}' (not in registry)",
        }
    try:
        return h.test_fn(
            closes=closes, bench_closes=bench_closes, volumes=volumes,
            frequency=frequency, lookback=lookback, end_date=end_date,
        )
    except Exception as e:  # fail open: never break the agent on a single test
        return {
            "passed": False, "value": None, "threshold": None,
            "detail": f"test raised {type(e).__name__}: {e}",
        }
