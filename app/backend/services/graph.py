import asyncio
import json
import logging
import re
import uuid
from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

logger = logging.getLogger(__name__)

from app.backend.services.agent_service import create_agent_function
from src.agents.portfolio_manager import portfolio_management_agent
from src.agents.risk_manager import risk_management_agent
from src.main import start
from src.utils.analysts import ANALYST_CONFIG
from src.graph.state import AgentState
from src.memory import flow_root, ingest_decisions, ingest_run


def extract_base_agent_key(unique_id: str) -> str:
    """
    Extract the base agent key from a unique node ID.
    
    Args:
        unique_id: The unique node ID with suffix (e.g., "warren_buffett_abc123")
    
    Returns:
        The base agent key (e.g., "warren_buffett")
    """
    # For agent nodes, remove the last underscore and 6-character suffix
    parts = unique_id.split('_')
    if len(parts) >= 2:
        last_part = parts[-1]
        # If the last part is a 6-character alphanumeric string, it's likely our suffix
        if len(last_part) == 6 and re.match(r'^[a-z0-9]+$', last_part):
            return '_'.join(parts[:-1])
    return unique_id  # Return original if no suffix pattern found


# Helper function to create the agent graph
def create_graph(graph_nodes: list, graph_edges: list) -> tuple[StateGraph, dict[str, list[str]]]:
    """Create the workflow + return the upstream map.

    The upstream map (target_agent_id → list of upstream agent_ids) reflects the
    user's wiring intent. The DAG re-routes analyst→PM edges through risk_manager
    for execution order; the upstream map preserves the *original* wiring so the
    prose can flow downstream (analyst → Buffett → PM) at prompt-build time.
    """
    graph = StateGraph(AgentState)
    graph.add_node("start_node", start)

    # Get analyst nodes from the configuration
    analyst_nodes = {key: (f"{key}_agent", config["agent_func"]) for key, config in ANALYST_CONFIG.items()}
    
    # Extract agent IDs from graph structure
    agent_ids = [node.id for node in graph_nodes]
    agent_ids_set = set(agent_ids)
    
    # Track which nodes are portfolio managers for special handling
    portfolio_manager_nodes = set()
    
    # Add agent nodes
    for unique_agent_id in agent_ids:
        base_agent_key = extract_base_agent_key(unique_agent_id)
        
        # Track portfolio manager nodes for special handling (before ANALYST_CONFIG check)
        if base_agent_key == "portfolio_manager":
            portfolio_manager_nodes.add(unique_agent_id)
            continue
            
        # Skip if the base agent key is not in our analyst configuration
        if base_agent_key not in ANALYST_CONFIG:
            continue
            
        node_name, node_func = analyst_nodes[base_agent_key]
        agent_function = create_agent_function(node_func, unique_agent_id)
        graph.add_node(unique_agent_id, agent_function)
    
    # Add portfolio manager nodes and their corresponding risk managers
    risk_manager_nodes = {}  # Map portfolio manager ID to risk manager ID
    for portfolio_manager_id in portfolio_manager_nodes:
        portfolio_manager_function = create_agent_function(portfolio_management_agent, portfolio_manager_id)
        graph.add_node(portfolio_manager_id, portfolio_manager_function)
        
        # Create unique risk manager for this portfolio manager
        suffix = portfolio_manager_id.split('_')[-1]
        risk_manager_id = f"risk_management_agent_{suffix}"
        risk_manager_nodes[portfolio_manager_id] = risk_manager_id
        
        # Add the risk manager node
        risk_manager_function = create_agent_function(risk_management_agent, risk_manager_id)
        graph.add_node(risk_manager_id, risk_manager_function)

    # Build connections based on React Flow graph structure
    nodes_with_incoming_edges = set()
    nodes_with_outgoing_edges = set()
    direct_to_portfolio_managers = {}  # Map analyst ID to portfolio manager ID for direct connections
    # Upstream map: for every agent, which other agents' MEMOS feed it. The user
    # wires the flow analyst→Buffett→PM and expects the prose to ride along. We
    # capture every agent→agent edge here (regardless of how LangGraph re-routes
    # it for execution order) so downstream agents can read upstream prose at
    # prompt-build time. Risk-manager re-routing for analyst→PM edges only
    # changes the DAG; the user's intent (analyst feeds PM) is preserved here.
    upstream_map: dict[str, list[str]] = {}

    for edge in graph_edges:
        # Only consider edges between agent nodes (not from stock tickers)
        if edge.source in agent_ids_set and edge.target in agent_ids_set:
            source_base_key = extract_base_agent_key(edge.source)
            target_base_key = extract_base_agent_key(edge.target)

            nodes_with_incoming_edges.add(edge.target)
            nodes_with_outgoing_edges.add(edge.source)

            upstream_map.setdefault(edge.target, []).append(edge.source)

            # Check if this is a direct connection from analyst to portfolio manager
            if (source_base_key in ANALYST_CONFIG and
                source_base_key != "portfolio_manager" and
                target_base_key == "portfolio_manager"):
                # Don't add direct edge to portfolio manager - we'll route through risk manager
                direct_to_portfolio_managers[edge.source] = edge.target
            else:
                # Add edge between agent nodes (but not direct to portfolio managers)
                graph.add_edge(edge.source, edge.target)
    
    # Connect start_node to nodes that don't have incoming edges from other agents
    for agent_id in agent_ids:
        if agent_id not in nodes_with_incoming_edges:
            base_agent_key = extract_base_agent_key(agent_id)
            if base_agent_key in ANALYST_CONFIG and base_agent_key != "portfolio_manager":
                graph.add_edge("start_node", agent_id)
    
    # Connect analysts that have direct connections to portfolio managers to their corresponding risk managers
    for analyst_id, portfolio_manager_id in direct_to_portfolio_managers.items():
        risk_manager_id = risk_manager_nodes[portfolio_manager_id]
        graph.add_edge(analyst_id, risk_manager_id)

    # Ensure each risk manager has at least one inbound edge. In a normal flow the
    # analysts feed it (via the loop above). In a *replay-strategy* run the frontend
    # sends only the PM in `graph_nodes` — no analysts, so no analyst→risk_manager
    # edges, and the risk_manager would never fire. Adding start_node as a parent
    # is safe in both modes: LangGraph fan-in waits for *all* parents, so when
    # analysts are wired the rm still waits for them; when they're not, start is
    # the only parent and rm fires immediately.
    fed_risk_managers = {
        risk_manager_nodes[pm_id] for pm_id in direct_to_portfolio_managers.values()
    }
    for risk_manager_id in risk_manager_nodes.values():
        if risk_manager_id not in fed_risk_managers:
            graph.add_edge("start_node", risk_manager_id)

    # Connect each risk manager to its corresponding portfolio manager
    for portfolio_manager_id, risk_manager_id in risk_manager_nodes.items():
        graph.add_edge(risk_manager_id, portfolio_manager_id)
    
    # Connect portfolio managers to END
    for portfolio_manager_id in portfolio_manager_nodes:
        graph.add_edge(portfolio_manager_id, END)

    # Set the entry point to the start node
    graph.set_entry_point("start_node")
    return graph, upstream_map


async def run_graph_async(graph, portfolio, tickers, start_date, end_date, model_name, model_provider, request=None, flow_id=None, research_materials=None, upstream_map=None):
    """Async wrapper for run_graph to work with asyncio."""
    # Use run_in_executor to run the synchronous function in a separate thread
    # so it doesn't block the event loop
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: run_graph(graph, portfolio, tickers, start_date, end_date, model_name, model_provider, request, flow_id, research_materials, upstream_map))  # Use default executor
    return result


def run_graph(
    graph: StateGraph,
    portfolio: dict,
    tickers: list[str],
    start_date: str,
    end_date: str,
    model_name: str,
    model_provider: str,
    request=None,
    flow_id=None,
    research_materials=None,
    upstream_map: dict[str, list[str]] | None = None,
) -> dict:
    """
    Run the graph with the given portfolio, tickers,
    start date, end date, show reasoning, model name,
    and model provider.
    """
    # Each flow keeps its own research memory namespace (wiki/flow-<id>). An
    # **unsaved** flow has no id, so there's nothing stable to scope its memory
    # to — persisting it anywhere either (a) bleeds into other unsaved-flow
    # runs (a shared "default" slug) or (b) leaks to the global wiki. Both
    # break the "flows are self-contained" guarantee, so unsaved flows skip
    # wiki persistence entirely. Once the user saves, future runs get their
    # own slug and start accumulating memory for real.
    flow_slug = f"flow-{flow_id}" if flow_id is not None else None
    root = flow_root(flow_slug)  # → None for unsaved flows
    run_id = uuid.uuid4().hex[:8]
    seen_signals: set[tuple[str, str]] = set()

    def _ingest_new(signals: dict) -> None:
        if root is None:
            # Unsaved flow: drop on the floor rather than writing somewhere
            # that would be shared with other runs.
            return
        new: dict[str, dict] = {}
        for agent_id, per_ticker in (signals or {}).items():
            if not isinstance(per_ticker, dict):
                continue
            for ticker, payload in per_ticker.items():
                key = (str(agent_id), str(ticker))
                if key in seen_signals:
                    continue
                seen_signals.add(key)
                new.setdefault(agent_id, {})[ticker] = payload
        if new:
            ingest_run(new, end_date=end_date, run_id=run_id, root=root)

    # Replay-strategy mode: re-decide on cached analyst signals without re-running
    # the LLM analyst layer. We hydrate `analyst_signals` straight from this flow's
    # wiki so the PM sees the latest stances from prior runs. Falls back to an
    # empty dict if the wiki is missing/empty — the PM will still run and hold,
    # which is the right outcome for "no signals available".
    seeded_signals: dict[str, dict] = {}
    skip_analysts = bool(request is not None and getattr(request, "skip_analysts", False))
    if skip_analysts and root is not None:
        try:
            from src.memory import read_latest_signals as _rls
            seeded_signals = _rls(tickers, root=root) or {}
            for agent_id, per_ticker in seeded_signals.items():
                for t in per_ticker.keys():
                    seen_signals.add((str(agent_id), str(t)))
        except Exception as e:
            logger.warning("replay-strategy: signal hydration failed (%s); PM will see empty signals", e)
            seeded_signals = {}

    inputs = {
        "messages": [
            HumanMessage(content="Make trading decisions based on the provided data.")
        ],
        "data": {
            "tickers": tickers,
            "portfolio": portfolio,
            "start_date": start_date,
            "end_date": end_date,
            "analyst_signals": seeded_signals,
        },
        "metadata": {
            "show_reasoning": False,
            "model_name": model_name,
            "model_provider": model_provider,
            "flow_slug": flow_slug,
            "research_materials": research_materials,  # Research-area grounding for all agents
            "request": request,  # Pass the request for agent-specific model access
            "upstream_map": upstream_map or {},  # Wiring intent — drives upstream-prose injection
            # Strategy node config (see schemas.StrategyConfig). The PM reads it as a
            # ## Strategy Mandate block; the Trading Account enforces max_position_pct.
            # None when no Strategy node is wired — the run behaves as before.
            "strategy": (
                request.strategy.model_dump() if request is not None and getattr(request, "strategy", None) else None
            ),
            # Risk Manager node config (see schemas.RiskManagerConfig). When None,
            # the risk_manager agent uses its hardcoded defaults (= pre-PR behaviour).
            "risk_manager": (
                request.risk_manager.model_dump() if request is not None and getattr(request, "risk_manager", None) else None
            ),
        },
    }

    # stream_mode="values" yields the full accumulating state after each step; the
    # last chunk equals what graph.invoke() would have returned.
    result: dict = {}
    for chunk in graph.stream(inputs, stream_mode="values"):
        result = chunk
        _ingest_new(chunk.get("data", {}).get("analyst_signals", {}))

    # Final pass for any signals not seen in a yielded chunk, then the PM's decisions.
    _ingest_new(result.get("data", {}).get("analyst_signals", {}))
    messages = result.get("messages") or []
    if messages:
        decisions = parse_hedge_fund_response(messages[-1].content)
        if isinstance(decisions, dict):
            # Same isolation rule as `_ingest_new` — unsaved flows skip PM-decision
            # persistence so they can't bleed into another run's wiki.
            if root is not None:
                ingest_decisions(decisions, end_date=end_date, run_id=run_id, root=root)
            # Wire PM decisions through to Alpaca PAPER orders when explicitly enabled
            # by the Trading Account node (fail-open: a problem with one order never
            # breaks the run).
            if request is not None and getattr(request, "place_paper_orders", False):
                _place_paper_orders_for_decisions(decisions, request)

    return result


# Open new position vs close existing — they're sized differently below.
#   "buy" / "short"  → OPEN  (budget-aware sizing, takes BUY/SELL side respectively)
#   "sell" / "cover" → CLOSE (bounded by what's actually held)
_OPENING_SIDE = {"buy": "buy", "short": "sell"}
_CLOSING_SIDE = {"sell": "sell", "cover": "buy"}


def _safe_progress(*args, **kwargs):
    try:
        from src.utils.progress import progress
        progress.update_status(*args, **kwargs)
    except Exception:
        pass


def _place_paper_orders_for_decisions(decisions: dict, request) -> None:
    """Submit the PM's per-ticker decisions as market day-orders on Alpaca PAPER,
    sized by the Trading Account node's Starting Budget × confidence.

    Sizing:
      • OPEN (buy / short): position $ = min(starting_budget, buying_power) /
        N_open_actions × (confidence/100); qty = floor(position / price).
      • CLOSE (sell / cover): qty = min(PM qty, held qty). PM qty of 0 → skip.

    Hard-coded paper host (in ``alpaca_paper.py``) + paper-only credentials, so
    there's no path to a LIVE account. Each order is logged via ``progress`` so
    the user sees it in the Output panel. Fail-open throughout.
    """
    try:
        from app.backend.services import alpaca_paper

        api_keys = getattr(request, "api_keys", None) or {}
        if not (api_keys.get("ALPACA_PAPER_API_KEY_ID") and api_keys.get("ALPACA_PAPER_SECRET_KEY")):
            _safe_progress("trading_account", None, "Auto-trade skipped: ALPACA_PAPER credentials not set in Settings")
            return

        # Categorize decisions into opens vs closes (skip hold / unknown / non-dict).
        opens: list[tuple[str, str, dict]] = []   # (ticker, action, dec)
        closes: list[tuple[str, str, dict]] = []
        for ticker, dec in (decisions or {}).items():
            if not isinstance(dec, dict):
                continue
            action = str(dec.get("action") or "").lower()
            if action in _OPENING_SIDE:
                opens.append((str(ticker).upper(), action, dec))
            elif action in _CLOSING_SIDE:
                closes.append((str(ticker).upper(), action, dec))

        # Account snapshot (used for both budget and held positions).
        account = alpaca_paper.get_account(api_keys)
        positions = {
            str(p.get("symbol") or "").upper(): float(p.get("qty") or 0)
            for p in alpaca_paper.get_positions(api_keys)
        }
        buying_power = float(account.get("buying_power") or 0)
        starting_budget = float(getattr(request, "starting_budget", None) or 0)
        available = min(starting_budget, buying_power) if starting_budget > 0 else buying_power

        # Strategy node's hard cap on any single position (% of *portfolio_value*,
        # not buying power — leverage shouldn't widen the cap). When set, no open
        # is allowed to deploy more than this fraction of equity, regardless of
        # what the budget-÷-N math would otherwise compute.
        portfolio_value = float(account.get("portfolio_value") or account.get("equity") or 0)
        max_position_pct = None
        try:
            strategy = getattr(request, "strategy", None)
            if strategy is not None and getattr(strategy, "max_position_pct", None) is not None:
                max_position_pct = float(strategy.max_position_pct)
        except Exception:
            max_position_pct = None
        per_position_cap = (
            (max_position_pct / 100.0) * portfolio_value
            if max_position_pct and portfolio_value > 0
            else None
        )

        # Prices for opens (Alpaca market data, batched, paper creds OK).
        prices = (
            alpaca_paper.get_latest_prices(api_keys, [t for t, _, _ in opens]) if opens else {}
        )
        per_position = (available / len(opens)) if (opens and available > 0) else 0.0

        # Build sized orders. (ticker, side, qty, action, debug_msg)
        sized: list[tuple[str, str, int, str, str]] = []

        for ticker, action, dec in opens:
            confidence = float(dec.get("confidence") or 0) / 100.0
            if confidence <= 0:
                confidence = 0.5  # safe default when the PM didn't provide one
            price = prices.get(ticker)
            if not price or price <= 0:
                _safe_progress("trading_account", ticker, f"Skipped {action}: no latest price available")
                continue
            allocation = per_position * confidence
            # Apply Strategy's max-position cap (% of portfolio value). When the
            # budget-÷-N math would deploy more, clamp to the cap and note it.
            capped_note = ""
            if per_position_cap is not None and allocation > per_position_cap:
                capped_note = f" capped @{max_position_pct:g}% of portfolio (${per_position_cap:,.0f})"
                allocation = per_position_cap
            qty = int(allocation // price)
            debug = f"budget ${allocation:,.0f} × ${price:,.2f}/share → {qty}{capped_note}"
            if qty <= 0:
                _safe_progress("trading_account", ticker, f"Skipped {action}: {debug} (qty would be 0)")
                continue
            sized.append((ticker, _OPENING_SIDE[action], qty, action, debug))

        for ticker, action, dec in closes:
            held = positions.get(ticker, 0)
            try:
                pm_qty = int(float(dec.get("quantity") or 0))
            except (TypeError, ValueError):
                pm_qty = 0
            if pm_qty <= 0:
                _safe_progress("trading_account", ticker, f"Skipped {action}: PM quantity is 0")
                continue
            if action == "sell":
                cap = max(0, int(held))   # only sell long shares we actually hold
            else:  # cover
                cap = max(0, int(-held))  # only cover short shares we actually hold
            if cap <= 0:
                _safe_progress("trading_account", ticker, f"Skipped {action}: no held position to close")
                continue
            qty = min(pm_qty, cap)
            sized.append((ticker, _CLOSING_SIDE[action], qty, action, f"held {int(held)}, PM {pm_qty} → {qty}"))

        # Submit each independently.
        placed = 0
        for ticker, side, qty, action, debug in sized:
            _safe_progress("trading_account", ticker, f"Placing {side.upper()} {qty} ({action} · {debug})…")
            res = alpaca_paper.place_order(api_keys, symbol=ticker, side=side, qty=qty)
            if res.get("ok"):
                placed += 1
                _safe_progress(
                    "trading_account",
                    ticker,
                    f"Placed {side.upper()} {res.get('qty')} ({res.get('status','submitted')})",
                )
            else:
                _safe_progress(
                    "trading_account",
                    ticker,
                    f"Order failed: {res.get('error', 'unknown error')}",
                )

        _safe_progress(
            "trading_account",
            None,
            f"Auto-trade done: {placed} placed, {len(sized) - placed} failed, "
            f"{len(opens) + len(closes) - len(sized)} skipped"
            + (f" · budget ${available:,.0f}" if opens else ""),
        )
    except Exception as e:  # never break the run
        _safe_progress("trading_account", None, f"Auto-trade aborted: {e}")


def parse_hedge_fund_response(response):
    """Parses a JSON string and returns a dictionary."""
    try:
        return json.loads(response)
    except json.JSONDecodeError as e:
        print(f"JSON decoding error: {e}\nResponse: {repr(response)}")
        return None
    except TypeError as e:
        print(f"Invalid response type (expected string, got {type(response).__name__}): {e}")
        return None
    except Exception as e:
        print(f"Unexpected error while parsing response: {e}\nResponse: {repr(response)}")
        return None
