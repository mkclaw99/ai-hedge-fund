"""Discover locally-loaded models from an LM Studio instance.

LM Studio (https://lmstudio.ai/) ships an OpenAI-compatible API on
``http://127.0.0.1:1234/v1`` by default. ``GET /v1/models`` returns
whatever model the user has loaded in the LM Studio UI. We surface those
as ``LLMModel(provider=LM_STUDIO)`` entries so they appear in the
``/language-models/`` list without needing any API key — when LM Studio
is running. When it isn't, we return an empty list (fail-open: the user
just doesn't see any local options).

Replaces the prior Ollama-based local discovery — the user runs LM
Studio, not Ollama.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return os.environ.get("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")


class LMStudioService:
    """Async LM Studio discovery. Stateless; safe to instantiate per request."""

    async def get_available_models(self) -> list[dict]:
        """Return models currently loaded in LM Studio, formatted for the API.

        Each row matches the shape ``language_models`` already returns for
        cloud entries: ``{display_name, model_name, provider}``. Returns an
        empty list when LM Studio isn't running or the response is
        unexpected — the dialog then just shows the cloud models.
        """
        url = f"{_base_url()}/models"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    return []
                payload = r.json() or {}
                data = payload.get("data") or []
        except Exception as e:
            # Connection refused, DNS, timeout — all expected when the
            # user simply doesn't have LM Studio running.
            logger.debug("LM Studio discovery unreachable at %s: %s", url, e)
            return []

        out: list[dict] = []
        for item in data:
            mid = (item or {}).get("id")
            if not mid:
                continue
            mid_s = str(mid)
            # Drop embedding/reranker models — they can't serve as chat
            # backends and would error on the first analyst prompt.
            # LM Studio's /v1/models doesn't always carry a 'type' field
            # so we go by name heuristic (covers the common families
            # like nomic-embed, qwen3-embedding, bge-rerank, …).
            low = mid_s.lower()
            if "embed" in low or "rerank" in low:
                continue
            # LM Studio model ids are repo-style (e.g. "qwen2.5-7b-instruct").
            # Show them as-is; users recognize them from the LM Studio UI.
            out.append({
                "display_name": mid_s,
                "model_name": mid_s,
                "provider": "LM Studio",
            })
        return out
