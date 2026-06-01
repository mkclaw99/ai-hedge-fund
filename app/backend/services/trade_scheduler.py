"""Background scheduler: fires trade ticks on the cadence the Trading
Account node carries (``tradeSchedule`` in its useNodeState bag).

Distinct loop from ``research_scheduler.py`` because the two clocks are
orthogonal — analyst refreshes are slow + LLM-expensive, trade ticks are
fast + slim. Sharing the loop would force the trade tick to walk every
flow's research config and vice versa.

Wakes every ``_POLL_SECONDS`` (60s). Cheap walk: one DB query for all
flows, in-memory filter by tradeSchedule + last_run. Market-hour gate
(``is_market_open``) skips ticks outside RTH so the trade executor
doesn't even need to be aware of after-hours timing.

The actual run path lives in ``trade_executor.execute_trade_tick`` —
this module is just the timer.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os

from app.backend.database import SessionLocal
from app.backend.database.models import HedgeFundFlow
from app.backend.services.market_hours import is_market_open
from app.backend.services.trade_executor import execute_trade_tick

logger = logging.getLogger(__name__)

_POLL_SECONDS = 60
_INTERVALS = {"5min": 300, "15min": 900, "hourly": 3600}


def is_enabled() -> bool:
    """Allow tests / dev runs to disable the loop via env."""
    return os.environ.get("TRADE_SCHEDULER_DISABLED", "0") != "1"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _trade_state(flow: HedgeFundFlow) -> tuple[str, dict | None]:
    """Pull (schedule, tradeRun) for a flow. tradeSchedule lives on the
    Trading Account node's internal_state, not on flow.data directly —
    so we walk flow.nodes for the first trading-account-node.

    Returns ``("off", None)`` when there's no Trading Account node, when
    its tradeSchedule is off/missing, or on any parse error."""
    nodes = flow.nodes or []
    ta_state: dict = {}
    for n in nodes:
        if (n.get("type") if isinstance(n, dict) else None) == "trading-account-node":
            ta_state = ((n.get("data") or {}).get("internal_state") or {}) if isinstance(n, dict) else {}
            break
    schedule = str(ta_state.get("tradeSchedule") or "off").lower()
    if schedule not in _INTERVALS:
        return "off", None
    tr = (flow.data or {}).get("tradeRun") if isinstance(flow.data, dict) else None
    return schedule, (tr or {})


def _due_flows(db) -> list[tuple[int, str]]:
    """Flows whose ``trade_schedule`` interval has elapsed since their
    last tick. Returns ``[(flow_id, schedule), ...]``."""
    out: list[tuple[int, str]] = []
    now = _now()
    for flow in db.query(HedgeFundFlow).all():
        schedule, tr = _trade_state(flow)
        if schedule == "off":
            continue
        interval = _INTERVALS[schedule]
        last = (tr or {}).get("last_run")
        try:
            last_dt = _dt.datetime.fromisoformat(last) if last else None
        except (TypeError, ValueError):
            last_dt = None
        if last_dt and (now - last_dt).total_seconds() < interval:
            continue
        out.append((flow.id, schedule))
    return out


async def run_due_ticks() -> int:
    """One iteration of the loop — fire every due flow once. Returns
    the count fired (incl. ones that bailed because of market-hour or
    daily cap; those still count as "checked")."""
    n = 0
    db = SessionLocal()
    try:
        due = _due_flows(db)
        if not due:
            return 0
        if not is_market_open():
            # Don't even pick up the lock — quiet skip during nights/weekends.
            return 0
        for flow_id, schedule in due:
            try:
                result = await execute_trade_tick(flow_id, db)
                if result.get("ok"):
                    logger.info("trade tick fired flow=%s schedule=%s decisions=%s",
                                flow_id, schedule, len(result.get("decisions") or {}))
                else:
                    logger.info("trade tick skipped flow=%s schedule=%s reason=%s",
                                flow_id, schedule, result.get("reason"))
                n += 1
            except Exception:
                logger.exception("trade tick failed for flow %s", flow_id)
    finally:
        db.close()
    return n


async def scheduler_loop() -> None:
    """Forever loop — fires due flows every _POLL_SECONDS."""
    logger.info("trade scheduler started (poll every %ds, intervals=%s)",
                _POLL_SECONDS, list(_INTERVALS.keys()))
    while True:
        try:
            n = await run_due_ticks()
            if n > 0:
                logger.debug("trade scheduler fired %d flow(s)", n)
        except Exception:
            logger.exception("trade scheduler tick failed")
        await asyncio.sleep(_POLL_SECONDS)
