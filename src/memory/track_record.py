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
        # Newest first; cap per ticker.
        ins.sort(key=lambda i: i.date, reverse=True)
        ins = ins[:max_per_ticker]
        by_ticker_insights[t] = ins
        for i in ins:
            d = _parse_date(i.date)
            if d is not None and (earliest is None or d < earliest):
                earliest = d

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


def track_record_summary(outcomes, *, today: Optional[_date] = None) -> dict:
    """Structured summary for the frontend Track Record dialog — same data
    the PM reads, but as JSON instead of Markdown. Empty when no outcomes."""
    if not outcomes:
        return {
            "overall": {"wins": 0, "losses": 0, "open": 0, "hit_rate": 0.0, "hit_rate_weighted": 0.0},
            "analysts": {},
            "analyst_tickers": [],
            "recent": [],
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
    }
