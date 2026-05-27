"""Research-area endpoints: expose analyst's investment themes to the UI.

Read-only and fail-open — if analyst is unavailable the dropdown just comes back
empty rather than erroring.
"""

from fastapi import APIRouter

from app.backend.services import analyst_mcp

router = APIRouter(prefix="/research")


@router.get("/themes")
async def get_themes():
    """Investment themes from analyst (slug, name, company_count) for the dropdown."""
    res = await analyst_mcp.list_themes()
    if res.get("error"):
        return {"themes": [], "error": res["error"]}
    themes = [
        {
            "slug": t.get("slug"),
            "name": t.get("name"),
            "company_count": t.get("company_count"),
            "status": t.get("status"),
        }
        for t in res.get("items", [])
        if t.get("slug")
    ]
    return {"themes": themes}
