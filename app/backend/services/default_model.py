"""Server-side fallback to the user's pinned default LLM.

The Pydantic schema default for ``HedgeFundRequest.model_name`` is
``gpt-4.1`` and ``model_provider`` is ``OPENAI``. That made sense when
the project was OpenAI-first, but the user might:

  * have no OpenAI key configured (only ``GOOGLE_API_KEY``)
  * have just dropped a PM into a new flow — the per-node ``selectedModel``
    auto-seed is async, and clicking Play before it resolves sends a
    request with no per-PM model and no top-level override → the schema
    default kicks in → backend tries OpenAI → 401.

The frontend has a pinned global default (Settings → Models → ★) stored
in the ``api_keys`` table under ``__app_default_model__``. This helper
substitutes it into the request when the schema default would otherwise
route to a provider whose key isn't available — the user's own choice
wins over the legacy hardcoded default.

Per-agent ``agent_models`` entries are left untouched: an explicit
selection beats the global default, same as the existing precedence
inside ``HedgeFundRequest.get_agent_model_config``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Mirrors language_models.py:_DEFAULT_MODEL_KEY. Duplicated here rather
# than imported to avoid circular routes/services coupling.
_DEFAULT_MODEL_KEY = "__app_default_model__"

# Provider → env var name. Mirrors the chain in src/llm/models.py:get_model
# (Anthropic, OpenAI, Groq, Google, DeepSeek). We check whether the user has
# the matching key before letting the schema default route to a dead provider.
_PROVIDER_ENV = {
    "OpenAI": "OPENAI_API_KEY",
    "Anthropic": "ANTHROPIC_API_KEY",
    "Google": "GOOGLE_API_KEY",
    "Groq": "GROQ_API_KEY",
    "DeepSeek": "DEEPSEEK_API_KEY",
}


def _pinned_default(db: Session) -> tuple[str, str] | None:
    """Read the user's pinned default model from api_keys.

    Returns ``(provider, model_name)`` or ``None`` when nothing is pinned.
    Local import keeps this module loadable without dragging the repository
    into every import chain that touches schemas.
    """
    try:
        from app.backend.repositories.api_key_repository import ApiKeyRepository
        row = ApiKeyRepository(db).get_api_key_by_provider(_DEFAULT_MODEL_KEY)
        if not row or not row.key_value or "::" not in row.key_value:
            return None
        provider, model_name = row.key_value.split("::", 1)
        if not provider or not model_name:
            return None
        return provider, model_name
    except Exception:
        # Fail open — if the lookup breaks, leave the request alone and let
        # the existing OpenAI-or-error path handle it (same as pre-fix).
        logger.warning("pinned-default lookup failed", exc_info=True)
        return None


def _provider_has_key(provider: str | Any, api_keys: dict | None) -> bool:
    """True when the user has the API key for ``provider`` configured
    (either passed in via the request or visible on the env)."""
    p = provider.value if hasattr(provider, "value") else str(provider)
    env_var = _PROVIDER_ENV.get(p)
    if not env_var:
        # Unknown provider (e.g. LM Studio, Ollama — no key required); treat as
        # available so we don't force a substitution on local providers.
        return True
    if api_keys and api_keys.get(env_var):
        return True
    import os
    return bool(os.environ.get(env_var))


def apply_default_model_fallback(request, db: Session) -> None:
    """Mutate ``request`` in place: substitute the pinned default when the
    schema-default OpenAI route would otherwise fail.

    Triggers when **both**:
      * the top-level ``model_provider`` is one the user has no key for, AND
      * a pinned default exists (``__app_default_model__`` in api_keys).

    Per-agent ``agent_models`` entries are left alone — an explicit per-node
    selection always wins. This only affects the request-level fallback that
    ``HedgeFundRequest.get_agent_model_config`` returns when an agent has no
    per-node selection (the common path for a freshly-dropped PM whose
    auto-seed hadn't resolved before Play was clicked).
    """
    # Already covered? Per-agent or per-request model resolves to a working
    # provider — no need to touch anything.
    if _provider_has_key(request.model_provider, request.api_keys):
        return

    pinned = _pinned_default(db)
    if not pinned:
        return
    provider, model_name = pinned

    # Don't substitute if the pinned provider is also keyless — that would
    # just shift the failure mode. Better to let the original 401 fire so
    # the user sees a clear "OpenAI key missing" error and goes to Settings.
    if not _provider_has_key(provider, request.api_keys):
        return

    logger.info(
        "Substituting request-level model %s/%s → %s/%s (pinned default, no %s key)",
        request.model_provider, request.model_name, provider, model_name,
        getattr(request.model_provider, "value", request.model_provider),
    )
    request.model_name = model_name
    # Coerce string back to the ModelProvider enum so downstream code that
    # branches on enum values continues to work. Local import to avoid the
    # schemas → services → schemas cycle.
    try:
        from src.llm.models import ModelProvider
        request.model_provider = ModelProvider(provider)
    except (ValueError, ImportError):
        # Unknown provider name — leave as string; pydantic will coerce or
        # raise on the next access. Downstream `get_model` already accepts
        # strings via its `is_*()` helpers.
        request.model_provider = provider  # type: ignore[assignment]
