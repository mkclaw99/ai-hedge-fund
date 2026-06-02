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
def refresh_simons(req: SimonsRefreshRequest, db: Session = Depends(get_db)):
    """Run Simons over `req.tickers` and return signals + recommended strategy.

    Wiki write piggybacks on the flow's wiki path so a reload picks up the
    fresh signals via the same rehydration path the forecaster uses (every
    analyst writes through the same ``ingest_run`` helper).

    Builds a real ``HedgeFundRequest`` so the hypothesis-driven loop's
    ``call_llm`` chain resolves the model the same way a full flow run
    would — including the pinned-default fallback from PR #111.
    """
    tickers = [str(t).strip().upper() for t in (req.tickers or []) if t and str(t).strip()]
    if not tickers:
        return {
            "signals": {},
            "recommended_strategy": recommended_strategy_for_cadence(req.simons_cadence or "5min"),
            "end_date": req.end_date or date.today().isoformat(),
        }

    end_date = (req.end_date or date.today().isoformat())[:10]

    # Hydrate API keys from DB if the request didn't carry them — same path
    # /hedge-fund/run takes. Without this the LLM has no GOOGLE_API_KEY
    # visible and the agent falls into its pure-numpy fallback.
    api_keys = dict(req.api_keys or {})
    if not api_keys:
        from app.backend.services.api_key_service import ApiKeyService
        api_keys = ApiKeyService(db).get_api_keys_dict() or {}

    # Build a proper HedgeFundRequest so `call_llm` resolves the model via
    # the same get_agent_model_config chain a full run uses.
    from app.backend.models.schemas import AgentModelConfig, GraphNode, HedgeFundRequest
    from app.backend.services.default_model import apply_default_model_fallback

    # Use a hex suffix instead of "_agent". normalize_analyst_name's
    # _ID_SUFFIX regex (`_[a-z0-9]{6}$`) strips a 6-char hex run cleanly,
    # leaving "jim_simons" → "Jim Simons". The literal "_agent" suffix
    # path collides for Simons specifically because "simons" is exactly 6
    # letters and matches the *same* random-suffix regex, normalizing the
    # name to "Jim" and slugging the wiki file as `…-jim.md` instead of
    # `…-jim-simons.md`. Other analysts (warren_buffett, peter_lynch) are
    # safe because their tails aren't 6 letters.
    sim_agent_id = f"jim_simons_{uuid.uuid4().hex[:6]}"
    agent_models = None
    if req.model_name and req.model_provider:
        agent_models = [AgentModelConfig(
            agent_id=sim_agent_id, model_name=req.model_name, model_provider=req.model_provider,
        )]
    agent_thinking_budgets = (
        {sim_agent_id: req.thinking_budget}
        if req.thinking_budget in {"off", "low", "medium", "high"} else None
    )
    full_req = HedgeFundRequest(
        tickers=tickers,
        graph_nodes=[GraphNode(id=sim_agent_id, type="jim-simons-node", data={}, position={"x": 0, "y": 0})],
        graph_edges=[],
        agent_models=agent_models,
        api_keys=api_keys,
        flow_id=req.flow_id,
        end_date=end_date,
        simons_cadence=req.simons_cadence,
        simons_bar_frequency=req.simons_bar_frequency,
        simons_lookback_bars=req.simons_lookback_bars,
        agent_thinking_budgets=agent_thinking_budgets,
    )
    # Pinned-default fallback (PR #111): if no per-agent model was sent and
    # the schema default routes to a keyless provider, sub in the pinned one.
    apply_default_model_fallback(full_req, db)

    state = {
        "messages": [],
        "data": {
            "tickers": tickers,
            "end_date": end_date,
            "analyst_signals": {},
        },
        "metadata": {
            "show_reasoning": False,
            "request": full_req,
            "api_keys": api_keys,
        },
    }

    try:
        jim_simons_agent(state, agent_id=sim_agent_id)
    except Exception as exc:
        logger.exception("simons refresh failed")
        return {
            "signals": {},
            "recommended_strategy": recommended_strategy_for_cadence(req.simons_cadence or "5min"),
            "end_date": end_date,
            "error": str(exc),
        }

    signals = state["data"].get("analyst_signals", {}).get(sim_agent_id, {})

    try:
        run_id = uuid.uuid4().hex[:8]
        ingest_run(
            {sim_agent_id: signals},
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
