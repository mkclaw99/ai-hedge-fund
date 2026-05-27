"""The two research roles behind a Fundamental Research flow, both Gemini-driven.

1. ``fundamental_research`` — the topic researcher. Reads the theme + materials +
   analyst's research corpus under a *research mandate* and writes a fundamental
   research note (the understanding). It does NOT pick companies.
2. ``extract_companies`` — the company researcher. Reads that note + the analyst
   value chain under an *extraction mandate* and extracts/ranks the relevant public
   companies, validated against Financial Datasets.

Both fail open: the note falls back to "" and extraction falls back to the
rule-based value-chain discovery if Gemini returns nothing usable.
"""

import logging

from app.backend.services import analyst_mcp
from app.backend.services.research_area import discover_universe, validate_companies
from src.utils.token_usage import UsageCallback

logger = logging.getLogger(__name__)

_MODEL = "gemini-3.1-pro-preview"
_PROVIDER = "Google"

# Generous caps on what each researcher prompt carries. Each researcher is a single
# Gemini call (not the per-agent fan-out), so these are sized to comfortably hold a
# long brief + notes rather than to save tokens — effectively no limit for real
# documents, with a ceiling only to guard against pathological inputs.
_MAX_MATERIALS_CHARS = 60_000
_MAX_NOTE_CHARS = 60_000
_MAX_MANDATE_CHARS = 6_000

_RESEARCH_PROMPT = """You are a fundamental equity researcher. Write a thorough fundamental research
note on the investment theme below — the understanding a team needs BEFORE picking stocks. Cover:
the thesis, the structure / value chain of the area, demand drivers and catalysts, the key risks,
and what separates winners from losers. Be complete and specific; don't pad, but don't impose an
artificial length limit. Let the researcher's mandate shape the emphasis. Do NOT recommend specific
tickers to buy — a separate step selects companies.

Theme: {theme}

Researcher mandate (the lens driving this research):
{mandate}

Background materials provided by the user (weigh as context):
{materials}

Recent research on this theme (titles):
{research}

Write the note in markdown, no preamble."""

_EXTRACT_PROMPT = """You are a company-selection researcher. From the fundamental research note and
the candidate value chain below, extract the most relevant PUBLIC companies to invest in for this
theme, shaped by the extraction mandate.

Theme: {theme}

Extraction mandate (how to choose companies):
{mandate}

Fundamental research note (the understanding to build on):
{note}

Background materials:
{materials}

Candidate companies from the analyst value chain (name · ticker · exposure% · role):
{candidates}

Return STRICT JSON only, no prose outside it:
{{ "companies": [{{"name": "Company", "ticker": "TICKER", "rationale": "<=120 chars why it fits"}}] }}

Pick the ~{max} most relevant public companies. Prefer the candidate tickers, fix obvious ticker
errors, and you may add clearly-relevant public companies you know. Use real US-listed tickers
where possible. Do NOT invent tickers. JSON only."""


def _fmt_candidates(rows: list[dict]) -> str:
    return "\n".join(
        f"- {c.get('name')} · {c.get('ticker')} · {c.get('theme_exposure_pct')}% · {c.get('role')}"
        for c in rows[:80]
    ) or "(none available)"


async def fundamental_research(theme: str, *, materials: str = "", mandate: str = "", api_keys: dict | None = None) -> str:
    """The topic researcher: write a fundamental research note (markdown) or '' on failure."""
    sr = await analyst_mcp.call_analyst_tool("search_research", {"theme": theme, "since_days": 365, "limit": 15})
    research_items = sr.get("items", []) if not sr.get("error") else []
    try:
        from src.llm.models import get_model

        model = get_model(_MODEL, _PROVIDER, api_keys)
        if model is None:
            return ""
        prompt = _RESEARCH_PROMPT.format(
            theme=theme,
            mandate=(mandate or "(none specified — use sound general fundamentals)").strip()[:_MAX_MANDATE_CHARS],
            materials=(materials or "(none)").strip()[:_MAX_MATERIALS_CHARS],
            research="\n".join(f"- {r.get('title')}" for r in research_items[:15]) or "(none on file)",
        )
        result = model.invoke(prompt, config={"callbacks": [UsageCallback(_PROVIDER, _MODEL)]})
        return (getattr(result, "content", "") or "").strip()
    except Exception as e:
        logger.warning("fundamental_research failed: %s", e)
        return ""


async def extract_companies(theme: str, *, research_note: str = "", materials: str = "", mandate: str = "", max_companies: int = 10, api_keys: dict | None = None) -> dict:
    """The company researcher: extract a validated universe. Returns
    ``{theme, tickers, picked, dropped, error}``; falls back to rule-based discovery."""
    cos = await analyst_mcp.list_theme_companies(theme, limit=120)
    candidates = [c for c in cos.get("items", []) if c.get("is_public")] if not cos.get("error") else []

    parsed = None
    try:
        from src.llm.models import get_model
        from src.utils.llm import extract_json_from_response

        model = get_model(_MODEL, _PROVIDER, api_keys)
        if model is None:
            raise RuntimeError("extraction model unavailable")
        prompt = _EXTRACT_PROMPT.format(
            theme=theme,
            mandate=(mandate or "(none — pick the most theme-relevant, liquid public names)").strip()[:_MAX_MANDATE_CHARS],
            note=(research_note or "(no research note provided)").strip()[:_MAX_NOTE_CHARS],
            materials=(materials or "(none)").strip()[:_MAX_MATERIALS_CHARS],
            candidates=_fmt_candidates(candidates),
            max=max_companies,
        )
        result = model.invoke(prompt, config={"callbacks": [UsageCallback(_PROVIDER, _MODEL)]})
        parsed = extract_json_from_response(getattr(result, "content", "") or "")
    except Exception as e:
        logger.warning("extract_companies synthesis failed: %s", e)

    if not parsed or not isinstance(parsed, dict) or not parsed.get("companies"):
        logger.info("extract_companies falling back to rule-based discovery for '%s'", theme)
        return await discover_universe(theme, max_companies=max_companies)

    rows = [
        {"name": c.get("name") or "", "ticker": c.get("ticker"), "rationale": c.get("rationale")}
        for c in parsed["companies"] if isinstance(c, dict) and c.get("ticker")
    ]
    val = await validate_companies(rows, max_companies)
    rationale_by_name = {r["name"]: r.get("rationale") for r in rows}
    for p in val["picked"]:
        p["rationale"] = rationale_by_name.get(p.get("name"))
    return {
        "theme": theme,
        "tickers": [p["ticker"] for p in val["picked"]],
        "picked": val["picked"],
        "dropped": val["dropped"],
        "error": None,
    }
