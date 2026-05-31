from fastapi import APIRouter, HTTPException

from app.backend.models.schemas import ErrorResponse
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
