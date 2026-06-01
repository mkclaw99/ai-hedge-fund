"""Decoupled trade-tick execution path.

The classical flow run re-executes every analyst (LLM-heavy, slow). The
trade tick is the **fast loop**: it reads the cached analyst signals from
the wiki (`read_latest_signals(..., with_date=True)`), re-fetches prices
fresh, then runs Risk → PM → Trading Account. No persona analysts fire.

One LLM call (the PM) per tick instead of N analysts × N tickers.

Wiring is the same shape the Strategy node's "Replay" button already
uses (``skip_analysts=True``); the new bits are:

  • ``refresh_prices=True``  — evict the per-ticker price cache so the RM
                               and PM see today's close, not yesterday's
                               cached "latest" entry
  • Market-hour gate inside ``execute_trade_tick`` (manual route bypasses)
  • Per-flow asyncio.Lock so two ticks can't trample each other
  • Per-day tick-counter cap so a misconfigured 5-min schedule can't
    burn $50/day of LLM calls without the user noticing

Persists each tick's outcome to ``flow.data.tradeRun`` so the scheduler
knows when to fire next and so the UI can show "last tick fired N min
ago".
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.backend.database.models import HedgeFundFlow
from app.backend.models.schemas import HedgeFundRequest
from app.backend.services.api_key_service import ApiKeyService
from app.backend.services.graph import (
    create_graph,
    parse_hedge_fund_response,
    run_graph,
)
from app.backend.services.market_hours import is_market_open
from app.backend.services.portfolio import create_portfolio

logger = logging.getLogger(__name__)

# Per-flow lock so a slow tick (network hiccup, big PM prompt) can't be
# overlapped by the next scheduler wake. Keyed by flow id. The dict is
# global because asyncio.Lock objects must be created in an event loop
# context but persist across iterations.
_flow_locks: dict[int, asyncio.Lock] = {}


def _lock_for(flow_id: int) -> asyncio.Lock:
    lock = _flow_locks.get(flow_id)
    if lock is None:
        lock = asyncio.Lock()
        _flow_locks[flow_id] = lock
    return lock


# Hard daily cap. A misconfigured 5-min schedule that somehow keeps firing
# outside market hours (it shouldn't — gate blocks that — but defence in
# depth) could otherwise rack up real money in LLM calls. 200 ticks/day is
# generous: 5-min schedule × 6.5h market open = 78 ticks. Anything past
# that is wrong.
_MAX_TICKS_PER_DAY = 200


def _utc_today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def _node_internal_state(flow: HedgeFundFlow, node_type: str) -> dict | None:
    """Find the first node of ``node_type`` on the flow and return its
    persisted ``internal_state`` (the useNodeState bag). None if absent."""
    for n in (flow.nodes or []):
        if (n.get("type") if isinstance(n, dict) else None) == node_type:
            data = (n.get("data") or {}) if isinstance(n, dict) else {}
            return data.get("internal_state") or {}
    return None


def _pm_node(flow: HedgeFundFlow) -> dict | None:
    for n in (flow.nodes or []):
        if (n.get("type") if isinstance(n, dict) else None) == "portfolio-manager-node":
            return n
    return None


def _tickers_from_wiki(flow_id: int) -> list[str]:
    """The universe to trade on this tick. Derived from the flow's wiki
    (whatever the most recent analyst run wrote). Returns ``[]`` if no
    prior run — the tick is a no-op in that case (logged + persisted)."""
    try:
        from src.memory.ingest import flow_root
        from src.memory.store import WikiMemory
        root = flow_root(f"flow-{flow_id}")
        if not root:
            return []
        wiki = WikiMemory(root)
        return sorted({str(t).upper() for t in (wiki.list_tickers() or [])})
    except Exception:
        return []


def _persist_tick(db: Session, flow: HedgeFundFlow, *, ok: bool, info: str | None = None) -> None:
    """Update ``flow.data.tradeRun`` with the last-run timestamp + counter.

    Counter resets each UTC day so the cap rolls over at midnight (a
    natural break — market is closed at UTC midnight regardless of US
    DST). Stores ``last_status`` so the UI can show "last tick: ok at
    15:32 ET" vs "last tick: skipped — outside market hours"."""
    data = dict(flow.data or {})
    tr = dict(data.get("tradeRun") or {})
    today = _utc_today()
    if tr.get("counter_date") != today:
        tr["counter_date"] = today
        tr["ticks_today"] = 0
    tr["ticks_today"] = int(tr.get("ticks_today", 0)) + (1 if ok else 0)
    tr["last_run"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    tr["last_status"] = info or ("ok" if ok else "skipped")
    data["tradeRun"] = tr
    flow.data = data
    db.add(flow)
    db.commit()


async def execute_trade_tick(
    flow_id: int,
    db: Session,
    *,
    manual: bool = False,
) -> dict[str, Any]:
    """Run one trade tick on the given flow.

    ``manual=True`` bypasses the market-hour gate (so the user can
    POST /trading/tick/<id> from the UI to test the loop). The schedule
    gate (``trade_schedule != off``) is still required — a tick on a
    flow whose Trading Account hasn't enabled trading is meaningless.
    The daily cap is also enforced for manual ticks so a "click 1000
    times" doesn't run away.
    """
    lock = _lock_for(flow_id)
    if lock.locked():
        return {"ok": False, "reason": "another tick is already running for this flow"}

    async with lock:
        flow: HedgeFundFlow | None = db.query(HedgeFundFlow).filter(HedgeFundFlow.id == flow_id).first()
        if flow is None:
            return {"ok": False, "reason": f"flow {flow_id} not found"}

        ta_state = _node_internal_state(flow, "trading-account-node") or {}
        trade_schedule = str(ta_state.get("tradeSchedule") or "off").lower()
        if trade_schedule == "off":
            _persist_tick(db, flow, ok=False, info="skipped: tradeSchedule=off")
            return {"ok": False, "reason": "Trading Account has tradeSchedule=off"}

        # Market-hour gate — auto only.
        if not manual and not is_market_open():
            _persist_tick(db, flow, ok=False, info="skipped: market closed")
            return {"ok": False, "reason": "market closed"}

        # Daily cap.
        prior = (flow.data or {}).get("tradeRun") or {}
        if prior.get("counter_date") == _utc_today() and int(prior.get("ticks_today", 0)) >= _MAX_TICKS_PER_DAY:
            _persist_tick(db, flow, ok=False, info=f"skipped: hit max {_MAX_TICKS_PER_DAY} ticks today")
            return {"ok": False, "reason": f"daily cap of {_MAX_TICKS_PER_DAY} ticks hit"}

        # PM node must exist.
        pm_node = _pm_node(flow)
        if pm_node is None:
            _persist_tick(db, flow, ok=False, info="skipped: no PM in flow")
            return {"ok": False, "reason": "no portfolio-manager-node in flow"}

        # Universe = whatever the wiki has from prior analyst runs. No
        # tickers → tick is a quiet no-op (don't error; the analyst
        # layer may simply not have run yet for a brand-new flow).
        tickers = _tickers_from_wiki(flow_id)
        if not tickers:
            _persist_tick(db, flow, ok=False, info="skipped: no tickers in wiki")
            return {"ok": False, "reason": "no tickers in wiki — run the full flow once first"}

        # Configs from sidecar nodes.
        strategy_cfg = _node_internal_state(flow, "strategy-node") or {}
        risk_cfg = _node_internal_state(flow, "risk-manager-node") or {}
        starting_budget = float(ta_state.get("startingBudget") or 100000)
        auto_trade = bool(ta_state.get("autoTrade"))

        # Build the slim request. Same shape as Strategy node's "Replay"
        # button, plus refresh_prices=True and trade_schedule echo.
        # The PM's saved selectedModel (via useNodeState) doubles as the
        # request-level default — without it the PM falls into the
        # OpenAI default and errors out on a missing key when the user
        # only has GOOGLE_API_KEY set.
        api_keys = ApiKeyService(db).get_api_keys_dict()
        pm_selected = ((pm_node.get("data") or {}).get("internal_state") or {}).get("selectedModel") or {}
        pm_model_name = pm_selected.get("model_name") or "gemini-3.1-pro-preview"
        pm_model_provider = pm_selected.get("provider") or "Google"
        agent_models = [{
            "agent_id": pm_node.get("id"),
            "model_name": pm_model_name,
            "model_provider": pm_model_provider,
        }]
        request = HedgeFundRequest(
            tickers=tickers,
            graph_nodes=[{
                "id": pm_node.get("id"),
                "type": pm_node.get("type"),
                "data": pm_node.get("data") or {},
                "position": pm_node.get("position") or {"x": 0, "y": 0},
            }],
            graph_edges=[],
            agent_models=agent_models,
            model_name=pm_model_name,
            model_provider=pm_model_provider,
            api_keys=api_keys,
            initial_cash=starting_budget,
            flow_id=flow_id,
            place_paper_orders=auto_trade,
            starting_budget=starting_budget,
            strategy=_safe_strategy_config(strategy_cfg),
            risk_manager=_safe_risk_config(risk_cfg),
            skip_analysts=True,
            trade_schedule=trade_schedule,
            refresh_prices=True,
        )

        # Execute.
        try:
            graph, upstream_map = create_graph(
                graph_nodes=request.graph_nodes,
                graph_edges=request.graph_edges,
            )
            compiled = graph.compile()
            portfolio = create_portfolio(starting_budget, 0.0, tickers, None)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: run_graph(
                    compiled, portfolio, tickers,
                    start_date=request.get_start_date(),
                    end_date=request.end_date,
                    model_name=request.model_name or "gemini-3.1-pro-preview",
                    model_provider=request.model_provider or "Google",
                    request=request,
                    flow_id=flow_id,
                    research_materials=None,
                    upstream_map=upstream_map,
                ),
            )
            # LangGraph state carries an accumulating list of messages; the
            # PM's final decisions are JSON-encoded on the *last* message's
            # .content (same shape the existing /hedge-fund/run route reads).
            messages = result.get("messages", []) or []
            decisions = None
            if messages:
                last = messages[-1]
                content = getattr(last, "content", None) or (last if isinstance(last, str) else None)
                if content:
                    decisions = parse_hedge_fund_response(content)
            _persist_tick(db, flow, ok=True, info=f"ok ({len(decisions or {})} decisions)")
            return {"ok": True, "decisions": decisions or {}, "tickers": tickers}
        except Exception as exc:
            logger.exception("trade tick failed for flow %s", flow_id)
            _persist_tick(db, flow, ok=False, info=f"error: {type(exc).__name__}: {exc}")
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _safe_strategy_config(state: dict) -> Any | None:
    """Project the trading-account-node's view of Strategy fields into the
    StrategyConfig pydantic shape. None when no Strategy node is wired."""
    if not state:
        return None
    try:
        from app.backend.models.schemas import StrategyConfig

        def f(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None
        return StrategyConfig(
            style=state.get("style") or None,
            sizing_rule=state.get("sizingRule") or None,
            max_position_pct=f(state.get("maxPositionPct")),
            max_sector_pct=f(state.get("maxSectorPct")),
            holding_period=state.get("holdingPeriod") or None,
            stop_loss_pct=f(state.get("stopLossPct")),
            take_profit_pct=f(state.get("takeProfitPct")),
            allow_stocks=state.get("allowStocks") is not False,
            allow_options=bool(state.get("allowOptions")),
            allow_etfs=bool(state.get("allowEtfs")),
            note=state.get("note") or None,
            min_decision_interval_minutes=f(state.get("minDecisionIntervalMinutes")),
            price_move_threshold_pct=f(state.get("priceMoveThresholdPct")),
            max_signal_age_hours=f(state.get("maxSignalAgeHours")),
        )
    except Exception:
        return None


def _safe_risk_config(state: dict) -> Any | None:
    if not state:
        return None
    try:
        from app.backend.models.schemas import RiskManagerConfig

        def f(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None
        return RiskManagerConfig(
            limit_multiplier=f(state.get("limitMultiplier")) or 1.0,
            disable_correlation_penalty=bool(state.get("disableCorrelationPenalty")),
            disabled=bool(state.get("disabled")),
        )
    except Exception:
        return None
