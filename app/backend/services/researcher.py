"""Fundamental researcher — the research role that drives a Fundamental Research area.

Given a theme + the user's materials + a researcher *mandate*, it reads analyst's data
(value-chain companies + research corpus — pure-DB reads via the MCP bridge), then uses
Gemini to (1) write a fundamental research note (the *understanding*) and (2) extract/rank
the relevant companies (the *universe*). Proposed tickers are validated against Financial
Datasets (reusing research_area's guards). Fail-open: on any failure it falls back to the
rule-based discovery so a run never breaks.
"""

import logging

from app.backend.services import analyst_mcp
from app.backend.services.research_area import discover_universe, validate_companies

logger = logging.getLogger(__name__)

_MODEL = "gemini-3.1-pro-preview"
_PROVIDER = "Google"

_PROMPT = """You are a fundamental equity researcher building an investable universe for a theme.

Theme: {theme}

Researcher mandate (your lens — let it shape which companies you pick and what you emphasize):
{mandate}

Background materials provided by the user (weigh as context):
{materials}

Candidate companies from the analyst value chain (name · ticker · theme-exposure% · role):
{candidates}

Recent research on this theme (titles):
{research}

Produce STRICT JSON only, no prose outside it:
{{
  "research_note": "~300-500 word fundamental note: thesis, value chain, key public players, catalysts, risks — shaped by the mandate",
  "companies": [{{"name": "Company", "ticker": "TICKER", "rationale": "<=120 chars why it fits the theme+mandate"}}]
}}

Rules: pick the 10-20 most relevant PUBLIC companies for the theme + mandate. Prefer the
candidate tickers but fix obvious ticker errors and you may add clearly-relevant public
companies you know. Use real US-listed tickers where possible. Do NOT invent tickers. JSON only."""


def _fmt_candidates(rows: list[dict]) -> str:
    out = [
        f"- {c.get('name')} · {c.get('ticker')} · {c.get('theme_exposure_pct')}% · {c.get('role')}"
        for c in rows[:80]
    ]
    return "\n".join(out) or "(none available)"


async def research(theme: str, *, materials: str = "", mandate: str = "", max_companies: int = 10, api_keys: dict | None = None) -> dict:
    """Run the fundamental researcher. Returns
    ``{theme, tickers, picked, dropped, research_note, error}``."""
    # 1. analyst pure-DB inputs (fail-open)
    cos = await analyst_mcp.list_theme_companies(theme, limit=120)
    candidates = [c for c in cos.get("items", []) if c.get("is_public")] if not cos.get("error") else []
    sr = await analyst_mcp.call_analyst_tool("search_research", {"theme": theme, "since_days": 365, "limit": 15})
    research_items = sr.get("items", []) if not sr.get("error") else []

    # 2. Gemini synthesis → {research_note, companies}
    parsed = None
    try:
        from src.llm.models import get_model
        from src.utils.llm import extract_json_from_response

        model = get_model(_MODEL, _PROVIDER, api_keys)
        if model is None:
            raise RuntimeError("research model unavailable")
        prompt = _PROMPT.format(
            theme=theme,
            mandate=(mandate or "(none specified — use sound general fundamentals)").strip()[:1500],
            materials=(materials or "(none)").strip()[:6000],
            candidates=_fmt_candidates(candidates),
            research="\n".join(f"- {r.get('title')}" for r in research_items[:15]) or "(none on file)",
        )
        content = getattr(model.invoke(prompt), "content", "") or ""
        parsed = extract_json_from_response(content)
    except Exception as e:
        logger.warning("researcher synthesis failed: %s", e)

    # 3. Fall back to rule-based discovery if the researcher produced nothing usable.
    if not parsed or not isinstance(parsed, dict) or not parsed.get("companies"):
        logger.info("researcher falling back to rule-based discovery for theme '%s'", theme)
        d = await discover_universe(theme, max_companies=max_companies)
        d["research_note"] = (parsed or {}).get("research_note", "") if isinstance(parsed, dict) else ""
        return d

    note = (parsed.get("research_note") or "").strip()
    rows = [
        {"name": c.get("name") or "", "ticker": c.get("ticker"), "rationale": c.get("rationale")}
        for c in parsed["companies"] if isinstance(c, dict) and c.get("ticker")
    ]
    val = await validate_companies(rows, max_companies)

    # Carry each company's rationale onto the validated picks (matched by name).
    rationale_by_name = {r["name"]: r.get("rationale") for r in rows}
    for p in val["picked"]:
        p["rationale"] = rationale_by_name.get(p.get("name"))

    return {
        "theme": theme,
        "tickers": [p["ticker"] for p in val["picked"]],
        "picked": val["picked"],
        "dropped": val["dropped"],
        "research_note": note,
        "error": None,
    }
