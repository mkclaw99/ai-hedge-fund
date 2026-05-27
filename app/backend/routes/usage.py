"""Token-usage endpoints for the Settings → Token Usage view."""

from fastapi import APIRouter

from src.utils import token_usage

router = APIRouter(prefix="/usage")


@router.get("")
async def get_usage():
    """Cumulative LLM token usage by provider+model, plus totals."""
    return token_usage.get_usage()


@router.post("/reset")
async def reset_usage():
    """Clear all recorded token usage."""
    token_usage.reset_usage()
    return {"ok": True}
