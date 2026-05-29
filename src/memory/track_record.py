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


def analyst_hit_rates(outcomes) -> dict[str, dict]:
    """Per-analyst rollup over CLOSED outcomes only. {analyst: {wins, losses, n, hit_rate, avg_win, avg_loss}}."""
    by: dict[str, dict] = {}
    for o in outcomes or []:
        if o.get("outcome") not in ("WIN", "LOSS"):
            continue
        a = o.get("analyst") or "?"
        cell = by.setdefault(a, {"wins": 0, "losses": 0, "win_returns": [], "loss_returns": []})
        if o["outcome"] == "WIN":
            cell["wins"] += 1
            cell["win_returns"].append(o.get("return_pct") or 0)
        else:
            cell["losses"] += 1
            cell["loss_returns"].append(o.get("return_pct") or 0)
    for a, cell in by.items():
        n = cell["wins"] + cell["losses"]
        cell["n"] = n
        cell["hit_rate"] = round(cell["wins"] / n * 100, 1) if n else 0.0
        cell["avg_win"] = round(sum(cell["win_returns"]) / len(cell["win_returns"]), 2) if cell["win_returns"] else 0.0
        cell["avg_loss"] = round(sum(cell["loss_returns"]) / len(cell["loss_returns"]), 2) if cell["loss_returns"] else 0.0
        # Pop the lists — keep the dict compact for prompt rendering.
        cell.pop("win_returns"); cell.pop("loss_returns")
    return by


def render_track_record_block(outcomes, *, max_rows: int = 15) -> str:
    """Markdown block for the PM prompt. Empty string when there's nothing to show."""
    rows = [o for o in (outcomes or []) if o.get("outcome") in ("WIN", "LOSS", "OPEN")]
    if not rows:
        return ""

    hit_rates = analyst_hit_rates(outcomes)
    overall_wins = sum(r.get("outcome") == "WIN" for r in rows)
    overall_losses = sum(r.get("outcome") == "LOSS" for r in rows)
    overall_open = sum(r.get("outcome") == "OPEN" for r in rows)
    closed = overall_wins + overall_losses

    lines: list[str] = []
    lines.append("## Track Record (your past calls vs. actual outcomes)")
    lines.append("")
    lines.append("Use this to calibrate. Look for systematic biases: tickers where you")
    lines.append("are repeatedly wrong, signals that don't pay off, overconfident calls")
    lines.append("that mis-fire. When the evidence disagrees with the historical track")
    lines.append("record, take the track record more seriously.")
    lines.append("")
    if closed:
        rate = round(overall_wins / closed * 100, 1)
        lines.append(
            f"**Overall (closed):** {overall_wins}W / {overall_losses}L  ·  hit rate **{rate}%**  ·  {overall_open} OPEN"
        )
    else:
        lines.append(f"**Overall:** 0 closed yet, {overall_open} OPEN")

    if hit_rates:
        lines.append("")
        lines.append("**Per-analyst hit rate (closed only):**")
        for a, cell in sorted(hit_rates.items(), key=lambda kv: -kv[1]["hit_rate"]):
            lines.append(
                f"- {a}: {cell['wins']}W/{cell['losses']}L ({cell['hit_rate']}%) · avg win +{cell['avg_win']}% / avg loss {cell['avg_loss']}%"
            )

    lines.append("")
    lines.append("**Recent decisions:**")
    lines.append("")
    lines.append("| Date | Ticker | Analyst | Signal | Conf | Entry | Exit | Return | Outcome |")
    lines.append("|------|--------|---------|--------|------|-------|------|--------|---------|")
    for o in rows[:max_rows]:
        ret = o.get("return_pct") or 0.0
        # `-0.0` rounds happen for OPEN trades that haven't moved yet — show 0.0
        # so we don't render "+-0.0%".
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
