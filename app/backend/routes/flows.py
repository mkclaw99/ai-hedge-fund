from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import List

from app.backend.database import get_db
from app.backend.repositories.flow_repository import FlowRepository
from app.backend.models.schemas import (
    FlowCreateRequest, 
    FlowUpdateRequest, 
    FlowResponse, 
    FlowSummaryResponse,
    ErrorResponse
)

router = APIRouter(prefix="/flows", tags=["flows"])


@router.post(
    "/",
    response_model=FlowResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def create_flow(request: FlowCreateRequest, db: Session = Depends(get_db)):
    """Create a new hedge fund flow"""
    try:
        repo = FlowRepository(db)
        flow = repo.create_flow(
            name=request.name,
            description=request.description,
            nodes=request.nodes,
            edges=request.edges,
            viewport=request.viewport,
            data=request.data,
            is_template=request.is_template,
            tags=request.tags
        )
        return FlowResponse.from_orm(flow)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create flow: {str(e)}")


@router.get(
    "/",
    response_model=List[FlowSummaryResponse],
    responses={
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def get_flows(include_templates: bool = True, db: Session = Depends(get_db)):
    """Get all flows (summary view)"""
    try:
        repo = FlowRepository(db)
        flows = repo.get_all_flows(include_templates=include_templates)
        return [FlowSummaryResponse.from_orm(flow) for flow in flows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve flows: {str(e)}")


@router.get(
    "/{flow_id}",
    response_model=FlowResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Flow not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def get_flow(flow_id: int, db: Session = Depends(get_db)):
    """Get a specific flow by ID"""
    try:
        repo = FlowRepository(db)
        flow = repo.get_flow_by_id(flow_id)
        if not flow:
            raise HTTPException(status_code=404, detail="Flow not found")
        return FlowResponse.from_orm(flow)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve flow: {str(e)}")


@router.put(
    "/{flow_id}",
    response_model=FlowResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Flow not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def update_flow(flow_id: int, request: FlowUpdateRequest, db: Session = Depends(get_db)):
    """Update an existing flow"""
    try:
        repo = FlowRepository(db)
        flow = repo.update_flow(
            flow_id=flow_id,
            name=request.name,
            description=request.description,
            nodes=request.nodes,
            edges=request.edges,
            viewport=request.viewport,
            data=request.data,
            is_template=request.is_template,
            tags=request.tags
        )
        if not flow:
            raise HTTPException(status_code=404, detail="Flow not found")
        return FlowResponse.from_orm(flow)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update flow: {str(e)}")


@router.delete(
    "/{flow_id}",
    responses={
        204: {"description": "Flow deleted successfully"},
        404: {"model": ErrorResponse, "description": "Flow not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def delete_flow(flow_id: int, db: Session = Depends(get_db)):
    """Delete a flow"""
    try:
        repo = FlowRepository(db)
        success = repo.delete_flow(flow_id)
        if not success:
            raise HTTPException(status_code=404, detail="Flow not found")
        return {"message": "Flow deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete flow: {str(e)}")


@router.post(
    "/{flow_id}/duplicate",
    response_model=FlowResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Flow not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def duplicate_flow(flow_id: int, new_name: str = None, db: Session = Depends(get_db)):
    """Create a copy of an existing flow"""
    try:
        repo = FlowRepository(db)
        flow = repo.duplicate_flow(flow_id, new_name)
        if not flow:
            raise HTTPException(status_code=404, detail="Flow not found")
        return FlowResponse.from_orm(flow)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to duplicate flow: {str(e)}")


@router.get(
    "/search/{name}",
    response_model=List[FlowSummaryResponse],
    responses={
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def search_flows(name: str, db: Session = Depends(get_db)):
    """Search flows by name"""
    try:
        repo = FlowRepository(db)
        flows = repo.get_flows_by_name(name)
        return [FlowSummaryResponse.from_orm(flow) for flow in flows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search flows: {str(e)}")


@router.delete("/{flow_id}/wiki/range")
async def delete_flow_wiki_range(
    flow_id: int,
    start: str,
    end: str,
    db: Session = Depends(get_db),
):
    """Delete every wiki insight in ``[start, end]`` for the given flow.

    Backstop for backtest re-runs on the same date window — without this,
    the second run's PM sees the first run's decisions as track-record and
    the simulation becomes a feedback loop on its own prior output.

    Date format ``YYYY-MM-DD`` inclusive on both ends. Returns
    ``{deleted: N}`` where N is the count of source files removed.
    Per-ticker and per-analyst derived pages are left alone — they
    re-derive on next ingest.

    Returns 404 when ``flow_id`` doesn't exist in the DB. We can't infer
    that from ``flow_root`` (which generates a path for any non-falsy
    slug — calling WikiMemory(root) would then silently mkdir() an empty
    tree for the non-existent flow), so we DB-check first."""
    repo = FlowRepository(db)
    if not repo.get_flow_by_id(flow_id):
        raise HTTPException(status_code=404, detail=f"Flow {flow_id} not found")
    from src.memory.ingest import flow_root
    from src.memory.store import WikiMemory
    root = flow_root(f"flow-{flow_id}")
    if root is None:
        raise HTTPException(status_code=404, detail=f"No wiki for flow {flow_id}")
    try:
        wiki = WikiMemory(root)
        n = wiki.delete_insights_in_range(start, end)
        return {"deleted": n, "start": start, "end": end}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete wiki range: {e}")