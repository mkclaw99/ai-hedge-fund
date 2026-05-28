import asyncio
import json
import re
import uuid
from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

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
def create_graph(graph_nodes: list, graph_edges: list) -> StateGraph:
    """Create the workflow based on the React Flow graph structure."""
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
    
    for edge in graph_edges:
        # Only consider edges between agent nodes (not from stock tickers)
        if edge.source in agent_ids_set and edge.target in agent_ids_set:
            source_base_key = extract_base_agent_key(edge.source)
            target_base_key = extract_base_agent_key(edge.target)
            
            nodes_with_incoming_edges.add(edge.target)
            nodes_with_outgoing_edges.add(edge.source)
            
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
    
    # Connect each risk manager to its corresponding portfolio manager
    for portfolio_manager_id, risk_manager_id in risk_manager_nodes.items():
        graph.add_edge(risk_manager_id, portfolio_manager_id)
    
    # Connect portfolio managers to END
    for portfolio_manager_id in portfolio_manager_nodes:
        graph.add_edge(portfolio_manager_id, END)

    # Set the entry point to the start node
    graph.set_entry_point("start_node")
    return graph


async def run_graph_async(graph, portfolio, tickers, start_date, end_date, model_name, model_provider, request=None, flow_id=None, research_materials=None):
    """Async wrapper for run_graph to work with asyncio."""
    # Use run_in_executor to run the synchronous function in a separate thread
    # so it doesn't block the event loop
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: run_graph(graph, portfolio, tickers, start_date, end_date, model_name, model_provider, request, flow_id, research_materials))  # Use default executor
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
) -> dict:
    """
    Run the graph with the given portfolio, tickers,
    start date, end date, show reasoning, model name,
    and model provider.
    """
    # Each flow keeps its own research memory namespace (wiki/flow-<id>); runs
    # without a flow id (e.g. an unsaved flow) share a "default" namespace. The
    # slug rides in metadata so every agent reads/writes the right wiki.
    flow_slug = f"flow-{flow_id}" if flow_id is not None else "default"

    # Accumulate this run into the flow's research wiki (fail-open). We stream the
    # graph and ingest each analyst signal as soon as it appears, so a run that
    # completes some analysts but then stalls/fails later (e.g. rate limits on the
    # PM) still captures what finished — rather than the all-or-nothing ingest that
    # only fired after the whole graph completed. Each (agent, ticker) is ingested
    # once via `seen`; ingest_run is idempotent per insight anyway.
    root = flow_root(flow_slug)
    run_id = uuid.uuid4().hex[:8]
    seen_signals: set[tuple[str, str]] = set()

    def _ingest_new(signals: dict) -> None:
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

    inputs = {
        "messages": [
            HumanMessage(content="Make trading decisions based on the provided data.")
        ],
        "data": {
            "tickers": tickers,
            "portfolio": portfolio,
            "start_date": start_date,
            "end_date": end_date,
            "analyst_signals": {},
        },
        "metadata": {
            "show_reasoning": False,
            "model_name": model_name,
            "model_provider": model_provider,
            "flow_slug": flow_slug,
            "research_materials": research_materials,  # Research-area grounding for all agents
            "request": request,  # Pass the request for agent-specific model access
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
            ingest_decisions(decisions, end_date=end_date, run_id=run_id, root=root)
            # Wire PM decisions through to Alpaca PAPER orders when explicitly enabled
            # by the Trading Account node (fail-open: a problem with one order never
            # breaks the run).
            if request is not None and getattr(request, "place_paper_orders", False):
                _place_paper_orders_for_decisions(decisions, request)

    return result


# buy/cover → BUY; sell/short → SELL; hold (or anything else) → skipped.
_ACTION_TO_SIDE = {"buy": "buy", "cover": "buy", "sell": "sell", "short": "sell"}


def _place_paper_orders_for_decisions(decisions: dict, request) -> None:
    """Submit the PM's per-ticker decisions as market day-orders on Alpaca PAPER.

    Hard-coded paper host (in ``alpaca_paper.py``) + paper-only credentials, so
    there's no path to a LIVE account. Each order is logged via ``progress`` so
    the user sees it in the Output panel.
    """
    try:
        from app.backend.services import alpaca_paper
        from src.utils.progress import progress

        api_keys = getattr(request, "api_keys", None) or {}
        if not (api_keys.get("ALPACA_PAPER_API_KEY_ID") and api_keys.get("ALPACA_PAPER_SECRET_KEY")):
            try:
                progress.update_status("trading_account", None, "Auto-trade skipped: ALPACA_PAPER credentials not set in Settings")
            except Exception:
                pass
            return

        placed = 0
        skipped = 0
        for ticker, dec in (decisions or {}).items():
            if not isinstance(dec, dict):
                continue
            action = str(dec.get("action") or "").lower()
            qty = dec.get("quantity") or 0
            side = _ACTION_TO_SIDE.get(action)
            try:
                qty_num = float(qty)
            except (TypeError, ValueError):
                qty_num = 0
            if not side or qty_num <= 0:
                skipped += 1
                continue
            try:
                progress.update_status("trading_account", ticker, f"Placing {side.upper()} {int(qty_num)}…")
            except Exception:
                pass
            res = alpaca_paper.place_order(api_keys, symbol=ticker, side=side, qty=qty_num)
            msg = (
                f"Placed {res.get('side','').upper()} {res.get('qty')} ({res.get('status','submitted')})"
                if res.get("ok")
                else f"Order failed: {res.get('error', 'unknown error')}"
            )
            try:
                progress.update_status("trading_account", ticker, msg)
            except Exception:
                pass
            placed += 1 if res.get("ok") else 0
        try:
            progress.update_status("trading_account", None, f"Auto-trade done: {placed} placed, {skipped} skipped")
        except Exception:
            pass
    except Exception as e:  # never break the run
        try:
            from src.utils.progress import progress
            progress.update_status("trading_account", None, f"Auto-trade aborted: {e}")
        except Exception:
            pass


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
