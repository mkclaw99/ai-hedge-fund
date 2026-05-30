"""Compute outcomes of past decisions for self-evaluation.

Every PM decision and analyst signal is persisted in the flow's wiki with a
date, a direction (bullish/bearish or buy/sell/short/cover/etc.), and a
reasoning. This module looks up the actual price movement since each decision
and computes whether the call paid off — feeding the resulting track record
back into the PM's prompt so it can calibrate confidence against history.

Outcome semantics (per insight):
    direction = +1 if signal is bullish-ish (bullish / buy / cover / sell_put …)
              = -1 if signal is bearish-ish (bearish / sell / short / buy_put …)
              =  0 otherwise (neutral / hold — skipped).
    horizon_days = strategy.holding_period mapping (day=1, swing=7, position=30,
                   long_term=180), default 30.
    entry_price = first close on or after insight.date.
    exit_price  = first close on or after min(insight.date + horizon, today).
    return_pct  = sign(direction) * (exit - entry) / entry  (signed)
    outcome     = "WIN" if closed and return_pct > 0
                  "LOSS" if closed and return_pct <= 0
                  "OPEN" if insight.date + horizon is still in the future

Hit-rate sums over closed trades only — OPEN positions don't get counted yet.
Fail-open at every layer: a missing price, an unparseable date, an unknown
signal — that insight is skipped with no exception bubbling up.
"""

from __future__ import annotations

import logging
from datetime import date as _date, datetime, timedelta
from typing import Optional

from src.memory.ingest import is_enabled
from src.memory.models import Insight
from src.memory.store import WikiMemory

logger = logging.getLogger(__name__)

# How many days to wait before we score a directional call. Mapped from the
# Strategy node's holding_period dropdown. Sensible defaults; the PM can read
# the same Strategy block, so the horizon picking stays consistent end-to-end.
_HOLDING_DAYS: dict[str, int] = {
    "day": 1,
    "swing": 7,
    "position": 30,
    "long_term": 180,
}
_DEFAULT_HOLDING_DAYS = 30

# Map every signal/action we know about to a directional sign. Anything not
# listed (e.g. "hold", "neutral", unknown) becomes 0 and the insight is skipped.
_BULLISH = {"bullish", "buy", "cover", "buy_call", "sell_put"}
_BEARISH = {"bearish", "sell", "short", "buy_put", "sell_call"}


def holding_days_from_strategy(strategy) -> int:
    """Return the holding-period horizon in days from a Strategy config dict."""
    period = (strategy or {}).get("holding_period") if isinstance(strategy, dict) else None
    return _HOLDING_DAYS.get(str(period or "").lower(), _DEFAULT_HOLDING_DAYS)


def _direction(signal_or_action: str) -> int:
    s = str(signal_or_action or "").lower()
    if s in _BULLISH:
        return 1
    if s in _BEARISH:
        return -1
    return 0


def _parse_date(s: str) -> Optional[_date]:
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _price_lookup_table(tickers, *, earliest: _date, latest: _date, api_keys):
    """Build {(ticker, date): close} by fetching each ticker's range once."""
    try:
        from src.tools.api import get_prices
    except Exception:
        return {}

    fd_key = None
    if api_keys:
        fd_key = api_keys.get("FINANCIAL_DATASETS_API_KEY")

    table: dict[tuple[str, _date], float] = {}
    for t in tickers:
        try:
            bars = get_prices(t, earliest.isoformat(), latest.isoformat(), api_key=fd_key)
        except Exception as e:
            logger.debug("track_record: price fetch failed for %s: %s", t, e)
            continue
        for b in bars or []:
            d = _parse_date(getattr(b, "time", "") or "")
            close = getattr(b, "close", None)
            if d is not None and close is not None:
                try:
                    table[(t, d)] = float(close)
                except (TypeError, ValueError):
                    continue
    return table


def _closest_close(table, ticker, target: _date, *, max_offset: int = 10):
    """Closest available close to ``target``. Slides forward first (target's
    own close, or the next trading day if target is a weekend/holiday), then
    falls back to the most-recent prior close. Why bidirectional: insights
    arrive dated *today* / *yesterday* but Financial Datasets prices typically
    end at the most recent closed session — forward-only would return None for
    every recent insight and the whole track record would collapse to empty.
    Decisions were made on the prior day's close anyway, so this matches reality.
    """
    # Forward first (covers the common case: insight on Monday, close that day).
    for off in range(max_offset + 1):
        p = table.get((ticker, target + timedelta(days=off)))
        if p is not None:
            return p
    # Backward fallback (covers insights dated today when today's close isn't out).
    for off in range(1, max_offset + 1):
        p = table.get((ticker, target - timedelta(days=off)))
        if p is not None:
            return p
    return None


def compute_outcomes(
    tickers,
    root,
    *,
    api_keys=None,
    today: Optional[_date] = None,
    holding_days: int = _DEFAULT_HOLDING_DAYS,
    max_per_ticker: int = 50,
) -> list[dict]:
    """For every past insight on these tickers, compute the outcome.

    Returns a list of dicts, newest first, capped at ``max_per_ticker`` per
    ticker so the result fits comfortably into a prompt. Each dict:

        { analyst, ticker, date, signal, confidence, reasoning_short,
          direction, entry_price, exit_price, return_pct, outcome }

    where ``outcome`` ∈ {"WIN","LOSS","OPEN","SKIP"}. SKIP entries
    are returned only when the caller passed an insight that has no directional
    meaning (held / neutral), to keep counts honest — they don't get rendered.
    """
    if not is_enabled() or not tickers or root is None:
        return []
    try:
        wiki = WikiMemory(root)
    except Exception:
        return []
    today = today or _date.today()
    tickers = [str(t).upper() for t in tickers]

    # Gather insights from every ticker (private helper but we live in the
    # memory module, so it's fine).
    by_ticker_insights: dict[str, list[Insight]] = {}
    earliest: Optional[_date] = None
    for t in tickers:
        try:
            ins = wiki._read_sources_for_ticker(t)  # noqa: SLF001
        except Exception:
            continue
        if not ins:
            continue
        # Drop insights dated AFTER ``today``. Defends walk-forward
        # backtests against lookahead bias: the wiki may contain insights
        # from later backtest days (because a prior backtest already wrote
        # them, or because two backtests share a flow). At backtest-day N
        # the PM must only see insights dated ≤ N. In live runs ``today``
        # is real today and every dated insight passes — this is a no-op.
        ins = [i for i in ins if (_parse_date(i.date) or _date(1900, 1, 1)) <= today]
        if not ins:
            continue
        # Newest first.
        ins.sort(key=lambda i: i.date, reverse=True)
        # Use the full history when picking the price-fetch window — we want
        # the cache key to stay stable across calls regardless of
        # ``max_per_ticker`` (which is a display cap). A floating earliest
        # date produces new cache keys on every change, and when the upstream
        # API is unavailable (rate-limited, out of credits) those fresh keys
        # silently return empty and the whole track record collapses.
        for i in ins:
            d = _parse_date(i.date)
            if d is not None and (earliest is None or d < earliest):
                earliest = d
        by_ticker_insights[t] = ins[:max_per_ticker]

    if not by_ticker_insights or earliest is None:
        return []

    # Fetch prices once per ticker over the full needed range (cached by
    # `src.tools.api`'s SQLite layer, so repeat calls within a run are free).
    table = _price_lookup_table(
        list(by_ticker_insights.keys()),
        earliest=earliest,
        latest=today,
        api_keys=api_keys,
    )

    results: list[dict] = []
    for t, ins_list in by_ticker_insights.items():
        for i in ins_list:
            d = _parse_date(i.date)
            if d is None:
                continue
            direction = _direction(i.signal)
            if direction == 0:
                results.append(_skip_row(i))
                continue
            target = d + timedelta(days=holding_days)
            closed = target <= today
            eval_date = target if closed else today
            entry = _closest_close(table, t, d)
            exit_ = _closest_close(table, t, eval_date)
            if entry is None or exit_ is None or entry <= 0:
                continue
            raw = (exit_ - entry) / entry
            ret = raw * (1 if direction > 0 else -1)
            outcome = "OPEN" if not closed else ("WIN" if ret > 0 else "LOSS")
            results.append({
                "analyst": i.analyst,
                "ticker": t,
                "date": i.date,
                "signal": i.signal,
                "confidence": int(i.confidence),
                "reasoning_short": (i.reasoning or "").replace("\n", " ").strip()[:120],
                "direction": direction,
                "entry_price": round(entry, 2),
                "exit_price": round(exit_, 2),
                "return_pct": round(ret * 100, 2),
                "outcome": outcome,
            })

    results.sort(key=lambda r: r.get("date", ""), reverse=True)
    return results


def _skip_row(i: Insight) -> dict:
    return {
        "analyst": i.analyst, "ticker": i.ticker, "date": i.date,
        "signal": i.signal, "confidence": int(i.confidence),
        "reasoning_short": "", "direction": 0, "entry_price": None,
        "exit_price": None, "return_pct": None, "outcome": "SKIP",
    }


# Half-life for time-weighting (days). A 60-day-old call has half the weight of
# a fresh one; a 120-day-old call carries 25%. Picked to be generous — fully
# discarding stale calls loses signal — but the half-life is short enough that
# a model whose calls improve quickly sees its recent record dominate.
_HALF_LIFE_DAYS = 60


def _weight_for_age(age_days: int, half_life: int = _HALF_LIFE_DAYS) -> float:
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life)


def _rollup(outcomes, *, group_key, today: Optional[_date] = None) -> dict:
    """Group closed outcomes by ``group_key(outcome) -> hashable`` and compute
    {wins, losses, n, hit_rate, hit_rate_weighted, avg_win, avg_loss} per group.

    Weighted hit rate uses an exponential half-life on the insight's age so a
    model whose calls improve over time sees its recent record dominate.
    """
    today = today or _date.today()
    by: dict = {}
    for o in outcomes or []:
        if o.get("outcome") not in ("WIN", "LOSS"):
            continue
        key = group_key(o)
        if key is None:
            continue
        d = _parse_date(o.get("date") or "")
        age = (today - d).days if d else 0
        w = _weight_for_age(age)
        cell = by.setdefault(key, {
            "wins": 0, "losses": 0,
            "wins_w": 0.0, "losses_w": 0.0,
            "win_returns": [], "loss_returns": [],
        })
        ret = o.get("return_pct") or 0
        if o["outcome"] == "WIN":
            cell["wins"] += 1
            cell["wins_w"] += w
            cell["win_returns"].append(ret)
        else:
            cell["losses"] += 1
            cell["losses_w"] += w
            cell["loss_returns"].append(ret)
    for key, cell in by.items():
        n = cell["wins"] + cell["losses"]
        n_w = cell["wins_w"] + cell["losses_w"]
        cell["n"] = n
        cell["hit_rate"] = round(cell["wins"] / n * 100, 1) if n else 0.0
        cell["hit_rate_weighted"] = round(cell["wins_w"] / n_w * 100, 1) if n_w > 0 else 0.0
        cell["avg_win"] = round(sum(cell["win_returns"]) / len(cell["win_returns"]), 2) if cell["win_returns"] else 0.0
        cell["avg_loss"] = round(sum(cell["loss_returns"]) / len(cell["loss_returns"]), 2) if cell["loss_returns"] else 0.0
        # Keep the cell compact for prompt rendering — drop the per-trade lists.
        cell.pop("win_returns"); cell.pop("loss_returns")
        cell.pop("wins_w"); cell.pop("losses_w")
    return by


def analyst_hit_rates(outcomes, *, today: Optional[_date] = None) -> dict[str, dict]:
    """Per-analyst rollup over CLOSED outcomes only."""
    return _rollup(outcomes, group_key=lambda o: o.get("analyst") or "?", today=today)


def analyst_ticker_hit_rates(outcomes, *, today: Optional[_date] = None, min_n: int = 2) -> dict[tuple[str, str], dict]:
    """Per-(analyst, ticker) rollup — the real learning signal. ``min_n``
    filters out single-data-point cells (one win or one loss is noise, not a
    pattern)."""
    full = _rollup(outcomes, group_key=lambda o: (o.get("analyst") or "?", o.get("ticker") or "?"), today=today)
    return {k: v for k, v in full.items() if v["n"] >= min_n}


def render_track_record_block(outcomes, *, max_rows: int = 15, today: Optional[_date] = None) -> str:
    """Markdown block for the PM prompt. Empty string when there's nothing to show.

    Shows both raw and time-weighted hit rates (recent calls weighed more),
    plus per-(analyst, ticker) cells where the signal is real (≥ 2 closed
    calls). The PM uses this to calibrate: which analysts misfire on which
    specific tickers, and whether the model has been improving over time.
    """
    rows = [o for o in (outcomes or []) if o.get("outcome") in ("WIN", "LOSS", "OPEN")]
    if not rows:
        return ""

    today = today or _date.today()
    hit_rates = analyst_hit_rates(outcomes, today=today)
    at_cells = analyst_ticker_hit_rates(outcomes, today=today, min_n=2)
    overall_wins = sum(r.get("outcome") == "WIN" for r in rows)
    overall_losses = sum(r.get("outcome") == "LOSS" for r in rows)
    overall_open = sum(r.get("outcome") == "OPEN" for r in rows)
    closed = overall_wins + overall_losses

    # Weighted overall: collapse all closed outcomes into one rollup.
    overall_w = _rollup(outcomes, group_key=lambda o: "__overall__", today=today).get("__overall__", {})

    lines: list[str] = []
    lines.append("## Track Record (your past calls vs. actual outcomes)")
    lines.append("")
    lines.append("Use this to calibrate. Look for systematic biases: tickers where you")
    lines.append("are repeatedly wrong, signals that don't pay off, overconfident calls")
    lines.append("that mis-fire. When the evidence disagrees with the historical track")
    lines.append("record, take the track record more seriously. Time-weighted hit rate")
    lines.append("emphasises recent calls (60-day half-life).")
    lines.append("")
    if closed:
        raw = round(overall_wins / closed * 100, 1)
        weighted = overall_w.get("hit_rate_weighted", raw)
        lines.append(
            f"**Overall (closed):** {overall_wins}W / {overall_losses}L  ·  raw **{raw}%** · time-weighted **{weighted}%**  ·  {overall_open} OPEN"
        )
    else:
        lines.append(f"**Overall:** 0 closed yet, {overall_open} OPEN")

    if hit_rates:
        lines.append("")
        lines.append("**Per-analyst (closed):**")
        for a, cell in sorted(hit_rates.items(), key=lambda kv: -kv[1]["hit_rate_weighted"]):
            lines.append(
                f"- {a}: {cell['wins']}W/{cell['losses']}L · raw {cell['hit_rate']}% · weighted **{cell['hit_rate_weighted']}%** · avg win +{cell['avg_win']}% / avg loss {cell['avg_loss']}%"
            )

    if at_cells:
        lines.append("")
        lines.append("**Per-(analyst, ticker) — patterns to look at (≥ 2 closed calls):**")
        # Sort by spread from 50% so the lopsided cells (strong winners or losers)
        # bubble up — those are the actionable patterns.
        sorted_cells = sorted(
            at_cells.items(),
            key=lambda kv: -abs(kv[1]["hit_rate_weighted"] - 50),
        )
        for (a, t), cell in sorted_cells[:12]:
            lines.append(
                f"- {a} on {t}: {cell['wins']}W/{cell['losses']}L · weighted {cell['hit_rate_weighted']}%"
            )

    lines.append("")
    lines.append("**Recent decisions:**")
    lines.append("")
    lines.append("| Date | Ticker | Analyst | Signal | Conf | Entry | Exit | Return | Outcome |")
    lines.append("|------|--------|---------|--------|------|-------|------|--------|---------|")
    for o in rows[:max_rows]:
        ret = o.get("return_pct") or 0.0
        if abs(ret) < 0.005:
            ret = 0.0
        sign = "+" if ret >= 0 else ""
        lines.append(
            f"| {o['date']} | {o['ticker']} | {o['analyst']} | {o['signal']} | {o.get('confidence', '?')}% | "
            f"${o['entry_price']} | ${o['exit_price']} | {sign}{ret}% | {o['outcome']} |"
        )
    if len(rows) > max_rows:
        lines.append(f"\n_({len(rows) - max_rows} older rows omitted.)_")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Closing the learning loop: turn observable outcomes into ACTIONABLE inputs
# for the PM.
#
# The Track Record block alone tells the PM "Valuation Analyst is 0/7 on COHR"
# but the PM still tends to weight Valuation's bearish call — LLMs are bad at
# consuming raw stats and applying them as constraints. The two functions
# below close that gap by translating the same data into shapes the PM can
# act on:
#
#  * ``derive_pm_rules`` produces explicit imperative rules ("Down-weight
#    Valuation Analyst on COHR") that get rendered into the prompt as a
#    Mandatory Adjustments block — LLMs follow imperative bullets much
#    better than they apply tables.
#  * ``confidence_calibrations`` builds a lookup from past hit rate to
#    a Bayesian-smoothed effective rate that we use to rewrite each
#    analyst's reported confidence at PM aggregation time — so the
#    signals JSON the PM sees already reflects history, not just the
#    analyst's self-reported number.
#
# Both are derived from the same ``outcomes`` list and intended to be used
# together: rules tell the PM what to do, calibration makes sure the
# inputs that survive into the decision agree with the rules.
# ---------------------------------------------------------------------------

# Bayesian smoothing for small-sample calibration. With α=4 and a prior of
# 0.5, a 0/2 cell yields effective rate 0.33 (not 0); a 0/7 cell yields 0.18;
# a 7/0 cell yields 0.82. The prior dominates at low n; the data dominates
# as n grows. Picked to be forgiving — we never want a single ticker's worth
# of bad luck to permanently silence an analyst.
_CAL_PRIOR = 0.5
_CAL_ALPHA = 4
# Minimum sample size before we surface a (analyst, ticker) cell as a *rule*
# (rules are imperative; we want them grounded in real signal). Calibration
# uses a lower threshold (min_n=2) because confidence rewriting fades to a
# no-op smoothly via the Bayesian prior anyway.
_RULE_MIN_N = 3
_CAL_MIN_N = 2

# How "lopsided" a cell has to be to become a rule. Weighted hit-rate
# thresholds; below ``_LOSS_PCT`` we emit a down-weight rule, above
# ``_WIN_PCT`` an up-weight rule. The 30/70 band leaves a wide "noisy
# middle" alone — we only call out cells where the signal is unambiguous.
_LOSS_PCT = 30
_WIN_PCT = 70


def derive_pm_rules(
    outcomes,
    *,
    today: Optional[_date] = None,
    min_n: int = _RULE_MIN_N,
    loss_pct: float = _LOSS_PCT,
    win_pct: float = _WIN_PCT,
) -> list[dict]:
    """Generate imperative rules from per-(analyst, ticker) outcomes.

    Each rule is a dict ``{kind, analyst, ticker, n, hit_rate, text}`` where
    ``kind`` is one of ``"down_weight"``, ``"up_weight"``, ``"lone_winner"``.
    The ``text`` field is the markdown bullet the PM prompt renders verbatim;
    it uses imperative language ("DO NOT", "SIDE WITH IT") because that's
    what LLMs follow.

    No rules are emitted when nothing crosses the lopsidedness threshold —
    a returning empty list is correct, not a degenerate state.
    """
    at = analyst_ticker_hit_rates(outcomes, today=today, min_n=min_n)
    if not at:
        return []
    rules: list[dict] = []
    for (analyst, ticker), cell in at.items():
        if cell["n"] < min_n:
            continue
        # Exclude the Portfolio Manager from rule generation entirely. The PM is
        # a meta-signal, not an independent signal source — its decisions appear
        # in the prior-research block, not in the signals JSON. Surfacing it as
        # a "trust X on Y" or "lone winner" rule produces a circular feedback
        # loop where the PM is told to side with itself (observed in the
        # walk-forward test of 2026-04-15: a 3W/0L PM cell flagged "Portfolio
        # Manager on COHR" as the lone winner, telling the PM to follow itself).
        # Calibration likewise: there's no analyst-signal entry to multiply.
        if analyst == "Portfolio Manager":
            continue
        hr = cell["hit_rate_weighted"]
        if hr <= loss_pct:
            rules.append({
                "kind": "down_weight",
                "analyst": analyst, "ticker": ticker,
                "n": cell["n"], "hit_rate": hr,
                "text": (
                    f"**Down-weight {analyst} on {ticker}** — historical "
                    f"{cell['wins']}W/{cell['losses']}L ({hr}% weighted). "
                    f"When {analyst} is the sole source of a directional call on "
                    f"{ticker}, DO NOT act on it alone — require corroboration "
                    f"from at least one other analyst with a positive track record "
                    f"on {ticker}."
                ),
            })
        elif hr >= win_pct:
            rules.append({
                "kind": "up_weight",
                "analyst": analyst, "ticker": ticker,
                "n": cell["n"], "hit_rate": hr,
                "text": (
                    f"**Trust {analyst} on {ticker}** — historical "
                    f"{cell['wins']}W/{cell['losses']}L ({hr}% weighted). "
                    f"When {analyst} takes a strong directional view on {ticker}, "
                    f"weight it heavily — even when other analysts disagree."
                ),
            })

    # Detect lone-winner patterns: on a ticker where ≥ 2 analysts have been
    # collectively wrong, if exactly one analyst has been right, that's a
    # contrarian signal the PM should bias toward. From the 10-day backtest:
    # Technical Analyst on COHR was 6W/0L while four value-style analysts were
    # 0W/26L on the same ticker — the PM should side with Technical there.
    by_ticker: dict[str, list[dict]] = {}
    for r in rules:
        by_ticker.setdefault(r["ticker"], []).append(r)
    for ticker, ticker_rules in by_ticker.items():
        winners = [r for r in ticker_rules if r["kind"] == "up_weight"]
        losers = [r for r in ticker_rules if r["kind"] == "down_weight"]
        if len(winners) == 1 and len(losers) >= 2:
            w = winners[0]
            loser_names = ", ".join(r["analyst"] for r in losers)
            rules.append({
                "kind": "lone_winner",
                "analyst": w["analyst"], "ticker": ticker,
                "n": w["n"], "hit_rate": w["hit_rate"],
                "text": (
                    f"**On {ticker}, only {w['analyst']} has been right** "
                    f"({w['hit_rate']}% weighted) — {loser_names} have been "
                    f"collectively wrong. If {w['analyst']} disagrees with the "
                    f"consensus on {ticker}, SIDE WITH IT."
                ),
            })
    # Sort: lone winners first (most actionable), then strong losers, then strong winners.
    _order = {"lone_winner": 0, "down_weight": 1, "up_weight": 2}
    rules.sort(key=lambda r: (_order.get(r["kind"], 99), -r["n"]))
    return rules


def render_pm_rules_block(rules) -> str:
    """Markdown for the PM prompt. Empty string when there are no rules."""
    if not rules:
        return ""
    lines: list[str] = []
    lines.append("## Mandatory Adjustments Based on Past Performance")
    lines.append("")
    lines.append(
        "Your past calls have been scored against actual price moves. The rules "
        "below were derived mechanically from the (analyst, ticker) cells where "
        "the historical pattern is unambiguous (≥ 3 closed calls, ≥ 70% one way). "
        "Follow them. They are not suggestions — when today's signals contradict "
        "a rule, weight the rule unless today's evidence is overwhelming."
    )
    lines.append("")
    for r in rules:
        lines.append(f"- {r['text']}")
    lines.append("")
    return "\n".join(lines)


def confidence_calibrations(
    outcomes,
    *,
    today: Optional[_date] = None,
    min_n: int = _CAL_MIN_N,
    alpha: int = _CAL_ALPHA,
    prior: float = _CAL_PRIOR,
) -> dict[tuple[str, str], tuple[float, int]]:
    """Build a ``(analyst, ticker) -> (effective_rate, n)`` lookup for PM
    confidence rewriting.

    ``effective_rate`` is in ``[0, 1]``. Uses Bayesian smoothing so small
    samples never collapse to 0 or 1: ``(wins + α·prior) / (n + α)``.
    Cells with ``n < min_n`` are simply absent from the dict — callers
    fall back to raw confidence.

    Why a dict and not a function: the PM call processes O(tickers × analysts)
    signals; a precomputed dict is O(1) per lookup instead of O(outcomes) per
    lookup, and it lets us return the count ``n`` so the PM prompt can show
    the user how much data backs each adjustment.
    """
    raw = _rollup(
        outcomes,
        group_key=lambda o: (o.get("analyst") or "?", o.get("ticker") or "?"),
        today=today,
    )
    out: dict[tuple[str, str], tuple[float, int]] = {}
    for (analyst, ticker), cell in raw.items():
        if cell["n"] < min_n:
            continue
        rate = (cell["wins"] + alpha * prior) / (cell["n"] + alpha)
        out[(analyst, ticker)] = (rate, cell["n"])
    return out


def calibrated_confidence(
    analyst: str, ticker: str, raw_conf: float,
    calibrations: dict[tuple[str, str], tuple[float, int]] | None,
) -> tuple[int, Optional[float], Optional[int]]:
    """Return ``(effective_conf_int, effective_rate_0to1 | None, n | None)``.

    When we have calibration data for this ``(analyst, ticker)``, the
    effective confidence is ``raw_conf * effective_rate`` clipped to
    ``[0, 100]``. With no data, returns ``raw_conf`` unchanged and rate/n
    as ``None`` so callers can branch on "did calibration apply?".
    """
    raw_int = max(0, min(100, int(round(raw_conf or 0))))
    if not calibrations:
        return (raw_int, None, None)
    pair = calibrations.get((analyst, ticker))
    if pair is None:
        return (raw_int, None, None)
    rate, n = pair
    effective = max(0, min(100, int(round(raw_int * rate))))
    return (effective, rate, n)


def track_record_summary(outcomes, *, today: Optional[_date] = None) -> dict:
    """Structured summary for the frontend Track Record dialog — same data
    the PM reads, but as JSON instead of Markdown. Empty when no outcomes.

    Includes ``rules`` so the UI can show the same Mandatory Adjustments the
    PM is currently following. Users seeing a rule they disagree with can
    intervene by reading the underlying cell or by overriding via UI flags.
    """
    if not outcomes:
        return {
            "overall": {"wins": 0, "losses": 0, "open": 0, "hit_rate": 0.0, "hit_rate_weighted": 0.0},
            "analysts": {},
            "analyst_tickers": [],
            "recent": [],
            "rules": [],
        }
    today = today or _date.today()
    rows = [o for o in outcomes if o.get("outcome") in ("WIN", "LOSS", "OPEN")]
    wins = sum(r.get("outcome") == "WIN" for r in rows)
    losses = sum(r.get("outcome") == "LOSS" for r in rows)
    open_ = sum(r.get("outcome") == "OPEN" for r in rows)
    closed = wins + losses
    overall_w = _rollup(outcomes, group_key=lambda o: "__overall__", today=today).get("__overall__", {})
    at = analyst_ticker_hit_rates(outcomes, today=today, min_n=2)
    return {
        "overall": {
            "wins": wins,
            "losses": losses,
            "open": open_,
            "hit_rate": round(wins / closed * 100, 1) if closed else 0.0,
            "hit_rate_weighted": overall_w.get("hit_rate_weighted", 0.0),
        },
        "analysts": analyst_hit_rates(outcomes, today=today),
        # Flatten the tuple keys for JSON.
        "analyst_tickers": [
            {"analyst": a, "ticker": t, **cell}
            for (a, t), cell in sorted(at.items(), key=lambda kv: -abs(kv[1]["hit_rate_weighted"] - 50))
        ],
        "recent": rows[:50],
        "rules": derive_pm_rules(outcomes, today=today),
    }
