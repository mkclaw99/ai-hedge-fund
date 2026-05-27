"""Research-area endpoints: analyst themes + PDF "information base" materials.

Read-only theme listing is fail-open (empty dropdown if analyst is down). Materials
upload extracts a PDF, distills it into a wiki brief, and stores both.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.backend.database import get_db
from app.backend.services import analyst_mcp, materials
from app.backend.services.api_key_service import ApiKeyService

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


@router.post("/materials")
async def upload_materials(
    flow_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a PDF information base: extract text, distill a brief, store both in
    the flow's wiki. Returns the brief for immediate display.

    Analysis (text extraction + Gemini distillation) runs once per PDF: a re-upload
    of the identical file returns the cached brief without calling Gemini again. A
    changed/replacement PDF (different content) is re-analyzed.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    # Same PDF already analyzed for this flow? Return the cached brief, no re-distill.
    digest = materials.source_hash(data)
    if materials.load_source_hash(flow_id) == digest:
        status = materials.materials_status(flow_id)
        if status.get("has_brief"):
            return {
                "filename": status.get("filename"),
                "brief": status.get("brief"),
                "cached": True,
            }

    try:
        text = materials.extract_pdf_text(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    api_keys = ApiKeyService(db).get_api_keys_dict()
    brief = materials.distill_brief(text, api_keys=api_keys)
    materials.store_materials(
        flow_id,
        source_text=text,
        brief=brief,
        filename=file.filename or "upload.pdf",
        content_hash=digest,
    )
    return {"filename": file.filename, "source_chars": len(text), "brief": brief, "cached": False}


@router.get("/materials")
async def get_materials(flow_id: int):
    """Current materials status for a flow (whether a brief exists + filename + brief)."""
    return materials.materials_status(flow_id)
