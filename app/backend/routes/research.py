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

_MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB


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
    the flow's wiki. Returns the brief for immediate display."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF too large (max 25 MB)")
    try:
        text = materials.extract_pdf_text(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    api_keys = ApiKeyService(db).get_api_keys_dict()
    brief = materials.distill_brief(text, api_keys=api_keys)
    materials.store_materials(flow_id, source_text=text, brief=brief, filename=file.filename or "upload.pdf")
    return {"filename": file.filename, "source_chars": len(text), "brief": brief}


@router.get("/materials")
async def get_materials(flow_id: int):
    """Current materials status for a flow (whether a brief exists + filename + brief)."""
    return materials.materials_status(flow_id)
