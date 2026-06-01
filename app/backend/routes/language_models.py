from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.backend.database import get_db
from app.backend.models.schemas import ErrorResponse
from app.backend.repositories.api_key_repository import ApiKeyRepository
from app.backend.services.key_availability import (
    available_providers,
    filter_models_by_available_keys,
)
from app.backend.services.lm_studio_service import LMStudioService
from src.llm.models import get_models_list

router = APIRouter(prefix="/language-models")

# Stateless discovery client for the locally-running LM Studio instance,
# which now replaces the old Ollama integration as the local-model
# source.
_lm_studio = LMStudioService()

# Magic provider name we hijack to stash the user's chosen default LLM in
# the existing api_keys key/value table. Hidden from Settings → API Keys
# (the UI iterates a hardcoded FINANCIAL/LLM list, not all rows), so this
# never leaks into the secrets UI. Format: ``<provider>::<model_name>``,
# e.g. ``Google::gemini-3.1-pro-preview``. A new table or settings.json
# would be overkill for one nullable string.
_DEFAULT_MODEL_KEY = "__app_default_model__"


class DefaultModelBody(BaseModel):
    """PUT body for /language-models/default. Both fields required."""
    provider: str
    model_name: str


@router.get(
    path="/",
    responses={
        200: {"description": "List of available language models"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def get_language_models():
    """Return the LLM models the user can actually run.

    Two layers:
      * Cloud models from ``api_models.json`` — filtered down to providers
        whose API key is set (env var *or* Settings → API Keys DB entry).
        We deliberately hide models that would error at runtime so the
        picker isn't a minefield.
      * Local models discovered live from LM Studio's
        OpenAI-compatible ``/v1/models`` endpoint. Empty list when LM
        Studio isn't running.
    """
    try:
        cloud = filter_models_by_available_keys(get_models_list())
        local = await _lm_studio.get_available_models()
        return {"models": cloud + local}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve models: {str(e)}")


@router.get(
    path="/providers",
    responses={
        200: {"description": "List of available model providers"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def get_language_model_providers():
    """Group available models by provider. Same filter as `/` so the
    provider list doesn't promise providers without keys."""
    try:
        models = filter_models_by_available_keys(get_models_list())
        local = await _lm_studio.get_available_models()
        for m in local:
            models.append(m)

        providers: dict[str, dict] = {}
        for model in models:
            provider_name = model["provider"]
            entry = providers.setdefault(provider_name, {"name": provider_name, "models": []})
            entry["models"].append({
                "display_name": model["display_name"],
                "model_name": model["model_name"],
            })

        return {"providers": list(providers.values()), "available": sorted(available_providers())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve providers: {str(e)}")


@router.get("/default")
async def get_default_model(db: Session = Depends(get_db)):
    """The model new agent nodes adopt when no per-node selection is set.

    Returns ``{provider, model_name}`` when one has been pinned, else
    ``{provider: null, model_name: null}``. The frontend's ``getDefaultModel``
    falls back to a hardcoded chain (``gemini-3.1-pro-preview`` → any Google
    → first available) when this is unset, so a fresh install behaves as
    before."""
    row = ApiKeyRepository(db).get_api_key_by_provider(_DEFAULT_MODEL_KEY)
    if not row or not row.key_value or "::" not in row.key_value:
        return {"provider": None, "model_name": None}
    provider, model_name = row.key_value.split("::", 1)
    return {"provider": provider, "model_name": model_name}


@router.put("/default")
async def set_default_model(body: DefaultModelBody, db: Session = Depends(get_db)):
    """Pin a model as the per-flow default for newly-added agent nodes.

    Stored in the existing api_keys table under a magic provider name —
    a single key/value, not a secret, no schema change. Returns the new
    state so the frontend can update its in-memory cache without a re-fetch."""
    if not body.provider or not body.model_name:
        raise HTTPException(status_code=400, detail="provider and model_name are required")
    repo = ApiKeyRepository(db)
    repo.create_or_update_api_key(
        provider=_DEFAULT_MODEL_KEY,
        key_value=f"{body.provider}::{body.model_name}",
        description="Default LLM model for new agent nodes (managed by Settings → Models)",
        is_active=True,
    )
    return {"provider": body.provider, "model_name": body.model_name}


@router.delete("/default")
async def clear_default_model(db: Session = Depends(get_db)):
    """Unpin the default; new agent nodes fall back to the hardcoded chain."""
    ApiKeyRepository(db).delete_api_key(_DEFAULT_MODEL_KEY)
    return {"provider": None, "model_name": None}
