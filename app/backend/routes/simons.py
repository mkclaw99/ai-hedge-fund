"""Routes for the Jim Simons analyst.

Two endpoints:

  * ``POST /simons/refresh`` — run Simons synchronously over an arbitrary
    ticker list (no flow context needed). Mirrors ``/forecaster/refresh``:
    used by the node body's "Refresh" button so the user can re-run Simons
    without kicking off a full flow.

  * ``POST /simons/tick/{flow_id}`` — fire a Simons tick on a specific
    flow, bypassing the market-hour gate (``manual=True``). The cadence
    schedule still applies (so a node with ``simonsSchedule=off`` returns
    a "skipped" reason instead of running). Mirrors
    ``/trading/tick/{flow_id}`` for the trade scheduler.

No SSE — Simons is sub-second per ticker (numpy + a small yfinance pull),
so a plain POST + spinner is sufficient.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.backend.database import get_db
from app.backend.models.schemas import SimonsRefreshRequest
from app.backend.services.simons_executor import execute_simons_tick
from src.agents.jim_simons import jim_simons_agent, recommended_strategy_for_cadence
from src.memory.ingest import flow_root, ingest_run

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/simons")


@router.post("/refresh")
def refresh_simons(req: SimonsRefreshRequest):
    """Run Simons over `req.tickers` and return signals + recommended strategy.

    Wiki write piggybacks on the flow's wiki path so a reload picks up the
    fresh signals via the same rehydration path the forecaster uses (every
    analyst writes through the same ``ingest_run`` helper).
    """
    tickers = [str(t).strip().upper() for t in (req.tickers or []) if t and str(t).strip()]
    if not tickers:
        return {
            "signals": {},
            "recommended_strategy": recommended_strategy_for_cadence(req.simons_cadence or "5min"),
            "end_date": req.end_date or date.today().isoformat(),
        }

    end_date = (req.end_date or date.today().isoformat())[:10]
    state = {
        "messages": [],
        "data": {
            "tickers": tickers,
            "end_date": end_date,
            "analyst_signals": {},
        },
        "metadata": {
            "show_reasoning": False,
            "request": req,
            "api_keys": req.api_keys or {},
        },
    }

    try:
        jim_simons_agent(state, agent_id="jim_simons_agent")
    except Exception as exc:
        logger.exception("simons refresh failed")
        return {
            "signals": {},
            "recommended_strategy": recommended_strategy_for_cadence(req.simons_cadence or "5min"),
            "end_date": end_date,
            "error": str(exc),
        }

    signals = state["data"].get("analyst_signals", {}).get("jim_simons_agent", {})

    try:
        run_id = uuid.uuid4().hex[:8]
        ingest_run(
            {"jim_simons_agent": signals},
            end_date=end_date,
            run_id=run_id,
            root=flow_root(f"flow-{req.flow_id}") if req.flow_id is not None else None,
        )
    except Exception:
        logger.warning("simons refresh wiki ingest skipped", exc_info=True)

    return {
        "signals": signals,
        "recommended_strategy": recommended_strategy_for_cadence(req.simons_cadence or "5min"),
        "end_date": end_date,
    }


@router.post("/tick/{flow_id}")
async def manual_simons_tick(flow_id: int, db: Session = Depends(get_db)):
    """Fire one Simons tick on the given flow, manually. Same path the
    scheduler uses, but with ``manual=True`` so the market-hour gate is
    bypassed (useful to test the wiring out-of-hours)."""
    try:
        result = await execute_simons_tick(flow_id, db, manual=True)
        return result
    except Exception as exc:
        logger.exception("manual simons tick failed for flow %s", flow_id)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
