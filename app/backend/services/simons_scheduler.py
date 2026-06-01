"""Background scheduler for the Jim Simons analyst.

Third clock in the architecture (alongside the analyst clock and the trade
clock). Wakes every ``_POLL_SECONDS``, walks every flow, fires a Simons-only
tick on any Simons node whose ``simonsSchedule`` interval has elapsed since
its last run.

Distinct loop from ``trade_scheduler`` and ``research_scheduler`` because
the three clocks are orthogonal:

  * research clock — slow, LLM-heavy (theme researcher refresh)
  * analyst+trade clock — fast PM-only ticks on cached signals
  * Simons clock — pure numerical, LLM-free, refreshes Simons signals
                   without triggering the PM

The actual tick path lives in ``simons_executor.execute_simons_tick``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os

from app.backend.database import SessionLocal
from app.backend.database.models import HedgeFundFlow
from app.backend.services.market_hours import is_market_open
from app.backend.services.simons_executor import execute_simons_tick

logger = logging.getLogger(__name__)

_POLL_SECONDS = 60
# Simons supports 1-min cadence (truest to Medallion); the others mirror
# the trade scheduler's lexicon so the two clocks compose at the same
# breakpoints. "off" excluded — that's the "Simons isn't on its own clock"
# state, in which case the node only runs as part of a normal flow play.
_INTERVALS = {"1min": 60, "5min": 300, "15min": 900, "hourly": 3600}


def is_enabled() -> bool:
    """Allow tests / dev runs to disable the loop via env."""
    return os.environ.get("SIMONS_SCHEDULER_DISABLED", "0") != "1"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _due_simons_for_flow(flow: HedgeFundFlow) -> list[tuple[str, str]]:
    """Return ``[(simons_node_id, cadence), ...]`` for every Simons node on
    this flow whose interval has elapsed since its last tick. Empty list
    when no Simons node is configured (the normal case for most flows)."""
    out: list[tuple[str, str]] = []
    now = _now()
    runs = (flow.data or {}).get("simonsRun") if isinstance(flow.data, dict) else {}
    runs = runs or {}
    for n in (flow.nodes or []):
        if not isinstance(n, dict) or n.get("type") != "jim-simons-node":
            continue
        node_id = n.get("id")
        if not node_id:
            continue
        internal_state = (n.get("data") or {}).get("internal_state") or {}
        cadence = str(internal_state.get("simonsSchedule") or "off").lower()
        if cadence not in _INTERVALS:
            continue
        interval = _INTERVALS[cadence]
        last = (runs.get(node_id) or {}).get("last_run")
        try:
            last_dt = _dt.datetime.fromisoformat(last) if last else None
        except (TypeError, ValueError):
            last_dt = None
        if last_dt and (now - last_dt).total_seconds() < interval:
            continue
        out.append((node_id, cadence))
    return out


async def run_due_ticks() -> int:
    """One iteration of the loop — fire every due Simons node once. Returns
    the count of flows actually touched (whether they fired or skipped)."""
    n = 0
    db = SessionLocal()
    try:
        flows_with_due: list[tuple[int, list[tuple[str, str]]]] = []
        for flow in db.query(HedgeFundFlow).all():
            due = _due_simons_for_flow(flow)
            if due:
                flows_with_due.append((flow.id, due))
        if not flows_with_due:
            return 0
        if not is_market_open():
            # Quiet skip during nights/weekends — no log spam.
            return 0
        for flow_id, due in flows_with_due:
            try:
                result = await execute_simons_tick(flow_id, db)
                if result.get("ok"):
                    logger.info(
                        "simons tick fired flow=%s nodes=%s",
                        flow_id, [r.get("node_id") for r in (result.get("results") or [])],
                    )
                else:
                    logger.debug(
                        "simons tick skipped flow=%s reasons=%s",
                        flow_id, [(r.get("node_id"), r.get("reason")) for r in (result.get("results") or [])],
                    )
                n += 1
            except Exception:
                logger.exception("simons tick failed for flow %s", flow_id)
    finally:
        db.close()
    return n


async def scheduler_loop() -> None:
    """Forever loop — fires due Simons nodes every _POLL_SECONDS."""
    logger.info(
        "simons scheduler started (poll every %ds, intervals=%s)",
        _POLL_SECONDS, list(_INTERVALS.keys()),
    )
    while True:
        try:
            n = await run_due_ticks()
            if n > 0:
                logger.debug("simons scheduler fired %d flow(s)", n)
        except Exception:
            logger.exception("simons scheduler tick failed")
        await asyncio.sleep(_POLL_SECONDS)
