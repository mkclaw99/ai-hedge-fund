"""Standalone refresh endpoint for the Time Series Forecaster.

The full ``/hedge-fund/run`` flow runs every analyst + the PM + (optionally)
the auto-trader. That's expensive and not what the user wants when they
just clicked the refresh button on the Chronos-2 node — they want a
fresh forecast for the same tickers, nothing else. This route runs
``forecaster_agent`` against a minimal state and returns the signals.

It also writes the signals to the wiki via ``ingest_run`` so the
post-reload rehydration path (forecaster-node.tsx + /memory?flow_id) picks
up the new forecast even if the user reloads before kicking off a full
run.

No SSE: the Chronos-2 forward pass is fast (sub-second per ticker once
the model is warm), so a plain POST + spinner is sufficient. FastAPI
runs sync routes in a thread pool, so this doesn't block other requests.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from fastapi import APIRouter

from app.backend.models.schemas import ForecasterRefreshRequest
from src.agents.forecaster import forecaster_agent
from src.memory.ingest import flow_root, ingest_run

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/forecaster")


@router.post("/refresh")
def refresh_forecaster(req: ForecasterRefreshRequest):
    """Run the forecaster only, persist via wiki, return per-ticker signals."""
    tickers = [str(t).strip().upper() for t in (req.tickers or []) if t and str(t).strip()]
    if not tickers:
        return {"signals": {}, "end_date": req.end_date or date.today().isoformat()}

    end_date = (req.end_date or date.today().isoformat())[:10]
    # The forecaster derives its own context window from end_date; this
    # start_date just satisfies any code path that peeks at it. The agent
    # itself recomputes the window from context_len.
    start_date = (date.today() - timedelta(days=800)).isoformat()

    state = {
        "messages": [],
        "data": {
            "tickers": tickers,
            "start_date": start_date,
            "end_date": end_date,
            "analyst_signals": {},
        },
        "metadata": {
            "show_reasoning": False,
            # The forecaster's _resolve_lengths/_resolve_frequency read
            # forecaster_* off this object via getattr — passing the
            # request itself keeps the wiring identical to the full-run
            # path.
            "request": req,
            "api_keys": req.api_keys or {},
        },
    }

    try:
        forecaster_agent(state, agent_id="forecaster_agent")
    except Exception as exc:
        logger.exception("forecaster refresh failed")
        return {"signals": {}, "end_date": end_date, "error": str(exc)}

    signals = state["data"].get("analyst_signals", {}).get("forecaster_agent", {})

    # Persist to wiki so a page reload still surfaces the new forecast
    # via the rehydration path (ForecasterNode parses the
    # forecast-data fence out of the Insight reasoning).
    try:
        run_id = uuid.uuid4().hex[:8]
        ingest_run(
            {"forecaster_agent": signals},
            end_date=end_date,
            run_id=run_id,
            root=flow_root(f"flow-{req.flow_id}") if req.flow_id is not None else None,
        )
    except Exception:
        # Wiki write is best-effort — the response is the source of truth
        # for the in-progress UI refresh; persistence is the bonus.
        logger.warning("forecaster refresh wiki ingest skipped", exc_info=True)

    return {"signals": signals, "end_date": end_date}
