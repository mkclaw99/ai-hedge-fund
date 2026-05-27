"""Shared run logic for research-area flows.

Both the SSE `/run` route and the background scheduler need the same preparation
(hydrate API keys, resolve a theme into a universe via the analyst MCP, merge the
PDF brief into the materials grounding). `resolve_run` does that shared prep; the
route then streams its own progress, while `execute_research_run` runs the whole
thing headless for the scheduler.
"""

import logging

from app.backend.services.analyst_mcp import is_enabled as analyst_enabled  # noqa: F401
from app.backend.services.api_key_service import ApiKeyService
from app.backend.services.graph import create_graph, run_graph_async
from app.backend.services.materials import load_brief
from app.backend.services.portfolio import create_portfolio
from app.backend.services.research_area import discover_universe

logger = logging.getLogger(__name__)

_MAX_MATERIALS_CHARS = 8_000  # bound what gets injected into every agent prompt


def merge_materials(notes: str | None, brief: str | None) -> str:
    """Combine pasted notes + the distilled PDF brief into one bounded grounding string."""
    parts = [p.strip() for p in (notes, brief) if p and p.strip()]
    return "\n\n".join(parts)[:_MAX_MATERIALS_CHARS]


def _provider_str(request_data) -> str:
    mp = request_data.model_provider
    return mp.value if hasattr(mp, "value") else mp


async def resolve_run(request_data, db) -> dict:
    """Hydrate keys, resolve the theme into tickers, and assemble materials.

    Mutates request_data (api_keys, tickers). Returns
    ``{tickers, materials, discovery, error}``.
    """
    if not request_data.api_keys:
        request_data.api_keys = ApiKeyService(db).get_api_keys_dict()

    tickers = request_data.tickers
    discovery = None
    if request_data.research_theme and not tickers:
        discovery = await discover_universe(
            request_data.research_theme,
            max_companies=request_data.research_max_companies or 10,
        )
        if discovery.get("error") or not discovery.get("tickers"):
            reason = discovery.get("error") or "all candidates were foreign, delisted, or name-mismatched"
            return {"tickers": [], "materials": "", "discovery": discovery, "error": reason}
        tickers = discovery["tickers"]
        request_data.tickers = tickers

    materials = merge_materials(request_data.research_materials, load_brief(request_data.flow_id))
    return {"tickers": tickers, "materials": materials, "discovery": discovery, "error": None}


async def execute_research_run(request_data, db) -> dict:
    """Run a research flow end-to-end, headless (used by the scheduler).

    Returns ``{result, discovery, tickers, error}``. Fail-open: ``error`` set on a
    prep failure or empty universe.
    """
    resolved = await resolve_run(request_data, db)
    if resolved["error"]:
        return {"result": None, "discovery": resolved["discovery"], "tickers": [], "error": resolved["error"]}

    tickers = resolved["tickers"]
    portfolio = create_portfolio(
        request_data.initial_cash, request_data.margin_requirement, tickers, request_data.portfolio_positions
    )
    graph = create_graph(graph_nodes=request_data.graph_nodes, graph_edges=request_data.graph_edges).compile()
    result = await run_graph_async(
        graph=graph,
        portfolio=portfolio,
        tickers=tickers,
        start_date=request_data.start_date,
        end_date=request_data.end_date,
        model_name=request_data.model_name,
        model_provider=_provider_str(request_data),
        request=request_data,
        flow_id=request_data.flow_id,
        research_materials=resolved["materials"],
    )
    return {"result": result, "discovery": resolved["discovery"], "tickers": tickers, "error": None}
