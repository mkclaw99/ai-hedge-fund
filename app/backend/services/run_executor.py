"""Shared run logic for research-area flows.

Both the SSE `/run` route and the background scheduler need the same preparation
(hydrate API keys, resolve a theme into a universe via the analyst MCP, merge the
PDF brief into the materials grounding). `resolve_run` does that shared prep; the
route then streams its own progress, while `execute_research_run` runs the whole
thing headless for the scheduler.
"""

import logging

from app.backend.services.api_key_service import ApiKeyService
from app.backend.services.graph import create_graph, run_graph_async
from app.backend.services.materials import load_brief, store_research_note
from app.backend.services.portfolio import create_portfolio
from app.backend.services import researcher

logger = logging.getLogger(__name__)

# High safety ceiling on what gets injected into EVERY agent prompt. This is the one
# materials cap that's multiplied across the agent fan-out (~15 analysts × every ticker),
# so it's not removed outright — but it's large enough to be effectively no limit for a
# real document (a full distilled brief + notes), only guarding against a pathological
# multi-MB injection that would blow the agents' context window and break the run.
_MAX_MATERIALS_CHARS = 100_000


def merge_materials(*parts: str | None) -> str:
    """Combine grounding pieces (notes, PDF brief, research note) into one bounded string."""
    kept = [p.strip() for p in parts if p and p.strip()]
    return "\n\n".join(kept)[:_MAX_MATERIALS_CHARS]


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
    pdf_brief = load_brief(request_data.flow_id)
    research_note = ""

    if request_data.research_theme and not tickers:
        base_materials = merge_materials(request_data.research_materials, pdf_brief)
        # Pass 1 — Fundamental Research: the topic researcher writes the research note.
        research_note = await researcher.fundamental_research(
            request_data.research_theme,
            materials=base_materials,
            mandate=request_data.research_mandate or "",
            api_keys=request_data.api_keys,
        )
        store_research_note(request_data.flow_id, research_note)
        # Pass 2 — Fundamental Companies: extract the validated universe from the note.
        ext = await researcher.extract_companies(
            request_data.research_theme,
            research_note=research_note,
            materials=base_materials,
            mandate=request_data.research_company_mandate or "",
            max_companies=request_data.research_max_companies or 10,
            api_keys=request_data.api_keys,
        )
        if ext.get("error") or not ext.get("tickers"):
            reason = ext.get("error") or "all candidates were foreign, delisted, or name-mismatched"
            return {"tickers": [], "materials": "", "discovery": {**ext, "research_note": research_note}, "error": reason}
        tickers = ext["tickers"]
        request_data.tickers = tickers
        discovery = {**ext, "research_note": research_note}

    # User-provided grounding (notes + PDF brief). The Fundamental Research note is
    # injected separately as shared FR memory for every role (see call_llm
    # _inject_fundamental_research), so it is NOT duplicated here.
    materials = merge_materials(request_data.research_materials, pdf_brief)
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
