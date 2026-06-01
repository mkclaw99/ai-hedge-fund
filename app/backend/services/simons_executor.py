"""Single-tick executor for the Jim Simons analyst.

A *third clock* on top of the two-clock architecture (analyst clock + trade
clock). The Simons scheduler wakes on its own cadence and calls this
executor once per due flow. Each tick:

  1. Pulls tickers from the upstream input node (Stock Input / Portfolio Input)
     persisted on the flow itself — Simons doesn't carry its own universe,
     it reads whatever the wired-in input node has.
  2. Runs ``jim_simons_agent`` directly (no graph, no LangGraph) — Simons is
     LLM-free so we don't need the executor scaffolding.
  3. Writes per-ticker signals to the flow's wiki via ``ingest_run`` — so the
     PM picks them up on its next trade tick exactly as it would any other
     analyst's cached signal.
  4. Persists the recommended StrategyConfig + last-run timestamp into
     ``flow.data.simonsRun`` so the UI can show "last refreshed N min ago"
     and so the run-assembler can echo the same strategy override into a
     live full-flow run.

Per-flow ``asyncio.Lock`` so a slow tick can't overlap the next scheduler
wake. Daily tick-counter cap so a misconfigured 1-min cadence can't burn
through yfinance rate limits — Simons is LLM-free, so the cap is about
upstream-provider hygiene, not money.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.backend.database.models import HedgeFundFlow
from app.backend.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)

# Per-flow lock; same idiom as trade_executor._flow_locks. Keyed by flow id;
# the dict survives across iterations because asyncio.Lock objects must be
# created in an event-loop context.
_flow_locks: dict[int, asyncio.Lock] = {}


def _lock_for(flow_id: int) -> asyncio.Lock:
    lock = _flow_locks.get(flow_id)
    if lock is None:
        lock = asyncio.Lock()
        _flow_locks[flow_id] = lock
    return lock


# Hard daily cap. Simons is LLM-free so cost isn't the issue; this is about
# avoiding hammering yfinance with bad requests when a flow is misconfigured.
# 1-min cadence × 6.5 RTH hours × N tickers gets there quickly; cap leaves
# headroom for ~6 tickers at 1-min for a full day.
_MAX_TICKS_PER_DAY = 600


def _utc_today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def _find_node(flow: HedgeFundFlow, node_type: str) -> dict | None:
    for n in (flow.nodes or []):
        if isinstance(n, dict) and n.get("type") == node_type:
            return n
    return None


def _all_nodes_of_type(flow: HedgeFundFlow, node_type: str) -> list[dict]:
    return [n for n in (flow.nodes or []) if isinstance(n, dict) and n.get("type") == node_type]


def _tickers_from_inputs(flow: HedgeFundFlow, simons_node_id: str) -> list[str]:
    """Walk the flow's edges backward from the Simons node and read tickers
    from whatever input node feeds it. Supports stock-analyzer-node (comma
    string) and portfolio-start-node (array of {ticker, quantity, tradePrice}).

    Returns ``[]`` when no input node is wired — the tick is then a quiet
    no-op (logged + persisted as skipped)."""
    edges = flow.edges or []
    incoming = [e for e in edges if isinstance(e, dict) and e.get("target") == simons_node_id]
    upstream_ids = {e.get("source") for e in incoming if e.get("source")}
    if not upstream_ids:
        return []
    out: list[str] = []
    for n in (flow.nodes or []):
        if not isinstance(n, dict) or n.get("id") not in upstream_ids:
            continue
        state = (n.get("data") or {}).get("internal_state") or {}
        if n.get("type") == "stock-analyzer-node":
            raw = str(state.get("tickers") or "")
            out.extend([t.strip().upper() for t in raw.split(",") if t.strip()])
        elif n.get("type") == "portfolio-start-node":
            for pos in (state.get("positions") or []):
                if isinstance(pos, dict) and pos.get("ticker"):
                    out.append(str(pos.get("ticker")).strip().upper())
    # Dedup while preserving order — first-seen wins.
    seen = set()
    deduped: list[str] = []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def _persist_tick(
    db: Session,
    flow: HedgeFundFlow,
    *,
    simons_node_id: str,
    ok: bool,
    info: str | None = None,
    recommended_strategy: dict | None = None,
) -> None:
    """Update ``flow.data.simonsRun[node_id]`` with the last-run timestamp,
    daily counter, and (when present) the recommended StrategyConfig.

    Keyed by Simons node id because a flow could in principle have more than
    one Simons node — though the typical case is exactly one. Counter resets
    each UTC day so the daily cap rolls over at midnight."""
    data = dict(flow.data or {})
    all_runs = dict(data.get("simonsRun") or {})
    entry = dict(all_runs.get(simons_node_id) or {})
    today = _utc_today()
    if entry.get("counter_date") != today:
        entry["counter_date"] = today
        entry["ticks_today"] = 0
    entry["ticks_today"] = int(entry.get("ticks_today", 0)) + (1 if ok else 0)
    entry["last_run"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    entry["last_status"] = info or ("ok" if ok else "skipped")
    if recommended_strategy is not None:
        entry["recommended_strategy"] = recommended_strategy
    all_runs[simons_node_id] = entry
    data["simonsRun"] = all_runs
    flow.data = data
    db.add(flow)
    db.commit()


def _run_simons_sync(
    *,
    tickers: list[str],
    end_date: str,
    api_keys: dict,
    cadence: str,
    bar_frequency: str | None,
    lookback_bars: int | None,
    flow_id: int | None,
) -> dict[str, Any]:
    """Synchronous Simons run + wiki write. Wrapped in run_in_executor by
    the async caller because the agent does blocking yfinance HTTP calls."""
    # Local import to keep cold-start cheap and avoid pulling pydantic models
    # into the scheduler's import path.
    from src.agents.jim_simons import jim_simons_agent, recommended_strategy_for_cadence
    from src.memory.ingest import flow_root, ingest_run

    # Build a minimal request-like object so the agent's _resolve_* getattr
    # calls find the right fields. SimpleNamespace keeps it duck-typed
    # without bringing pydantic in.
    from types import SimpleNamespace
    req = SimpleNamespace(
        simons_cadence=cadence,
        simons_bar_frequency=bar_frequency,
        simons_lookback_bars=lookback_bars,
    )

    state: dict[str, Any] = {
        "messages": [],
        "data": {
            "tickers": tickers,
            "end_date": end_date,
            "analyst_signals": {},
        },
        "metadata": {
            "show_reasoning": False,
            "request": req,
            "api_keys": api_keys or {},
        },
    }
    jim_simons_agent(state, agent_id="jim_simons_agent")
    signals = state["data"].get("analyst_signals", {}).get("jim_simons_agent", {})

    # Persist signals to the flow's wiki so the PM sees them on its next
    # trade tick. Same shape ingest_run accepts for any analyst.
    try:
        run_id = uuid.uuid4().hex[:8]
        ingest_run(
            {"jim_simons_agent": signals},
            end_date=end_date,
            run_id=run_id,
            root=flow_root(f"flow-{flow_id}") if flow_id is not None else None,
        )
    except Exception:
        logger.warning("Simons wiki ingest skipped", exc_info=True)

    return {
        "signals": signals,
        "recommended_strategy": recommended_strategy_for_cadence(cadence),
    }


async def execute_simons_tick(
    flow_id: int,
    db: Session,
    *,
    manual: bool = False,
) -> dict[str, Any]:
    """Run one Simons tick on the given flow.

    ``manual=True`` bypasses the market-hour gate so a user can POST
    /simons/tick/<id> to test the wiring. The cadence gate
    (``simonsSchedule != off``) still applies — a tick on a flow whose
    Simons node hasn't enabled the schedule is meaningless.
    """
    # Market-hour gate (auto only) — local import so the unit-tests can stub it.
    from app.backend.services.market_hours import is_market_open

    lock = _lock_for(flow_id)
    if lock.locked():
        return {"ok": False, "reason": "another Simons tick is already running for this flow"}

    async with lock:
        flow: HedgeFundFlow | None = db.query(HedgeFundFlow).filter(HedgeFundFlow.id == flow_id).first()
        if flow is None:
            return {"ok": False, "reason": f"flow {flow_id} not found"}

        simons_nodes = _all_nodes_of_type(flow, "jim-simons-node")
        if not simons_nodes:
            return {"ok": False, "reason": "no jim-simons-node in flow"}

        # One scheduler tick can drive multiple Simons nodes if the flow has
        # more than one (rare). Each runs independently with its own cadence /
        # counter — same idiom the trade tick uses for multiple decisions.
        results: list[dict[str, Any]] = []
        for simons_node in simons_nodes:
            node_id = simons_node.get("id")
            internal_state = (simons_node.get("data") or {}).get("internal_state") or {}
            cadence = str(internal_state.get("simonsSchedule") or "off").lower()
            if cadence == "off":
                _persist_tick(db, flow, simons_node_id=node_id, ok=False,
                              info="skipped: simonsSchedule=off")
                results.append({"node_id": node_id, "ok": False, "reason": "simonsSchedule=off"})
                continue

            if not manual and not is_market_open():
                _persist_tick(db, flow, simons_node_id=node_id, ok=False,
                              info="skipped: market closed")
                results.append({"node_id": node_id, "ok": False, "reason": "market closed"})
                continue

            prior = ((flow.data or {}).get("simonsRun") or {}).get(node_id) or {}
            if (prior.get("counter_date") == _utc_today()
                    and int(prior.get("ticks_today", 0)) >= _MAX_TICKS_PER_DAY):
                _persist_tick(db, flow, simons_node_id=node_id, ok=False,
                              info=f"skipped: hit max {_MAX_TICKS_PER_DAY} ticks today")
                results.append({"node_id": node_id, "ok": False,
                                "reason": f"daily cap of {_MAX_TICKS_PER_DAY} hit"})
                continue

            tickers = _tickers_from_inputs(flow, node_id)
            if not tickers:
                _persist_tick(db, flow, simons_node_id=node_id, ok=False,
                              info="skipped: no upstream input with tickers")
                results.append({"node_id": node_id, "ok": False,
                                "reason": "no upstream input node with tickers — wire a Stock Input or Portfolio Input"})
                continue

            api_keys = ApiKeyService(db).get_api_keys_dict()
            end_date = _dt.date.today().isoformat()
            bar_frequency = internal_state.get("simonsBarFrequency") or None
            try:
                lookback_bars = int(internal_state.get("simonsLookbackBars")) if internal_state.get("simonsLookbackBars") else None
            except (TypeError, ValueError):
                lookback_bars = None

            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: _run_simons_sync(
                        tickers=tickers,
                        end_date=end_date,
                        api_keys=api_keys,
                        cadence=cadence,
                        bar_frequency=bar_frequency,
                        lookback_bars=lookback_bars,
                        flow_id=flow_id,
                    ),
                )
                sig_count = len(result.get("signals") or {})
                _persist_tick(
                    db, flow, simons_node_id=node_id, ok=True,
                    info=f"ok ({sig_count} signals)",
                    recommended_strategy=result.get("recommended_strategy"),
                )
                results.append({
                    "node_id": node_id, "ok": True,
                    "signals": sig_count, "tickers": tickers,
                })
            except Exception as exc:
                logger.exception("Simons tick failed for flow %s node %s", flow_id, node_id)
                _persist_tick(db, flow, simons_node_id=node_id, ok=False,
                              info=f"error: {type(exc).__name__}: {exc}")
                results.append({"node_id": node_id, "ok": False,
                                "reason": f"{type(exc).__name__}: {exc}"})

        # Aggregate: scheduler treats any-ok as ok (counts toward "fired" log line).
        any_ok = any(r.get("ok") for r in results)
        return {"ok": any_ok, "results": results}
