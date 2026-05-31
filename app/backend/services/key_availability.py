"""Decide which cloud-LLM providers actually have a key configured.

Keys can live in two places:
  * **Environment variables** (``ANTHROPIC_API_KEY``, ``GOOGLE_API_KEY``, …) —
    the legacy path, still the simplest for ops.
  * **The ``api_keys`` DB table**, updated via Settings → API Keys in the UI.

Only providers whose key exists in *at least one* of those sources are
considered "available". The ``/language-models/`` endpoint uses this to
filter out models that would error at runtime, so the user doesn't pick
a model they can't use.

Local providers (LM Studio, Ollama) need no key — their availability is
handled by their own discovery services.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from app.backend.database import SessionLocal
from app.backend.repositories.api_key_repository import ApiKeyRepository

logger = logging.getLogger(__name__)


# Provider display-name → list of env-var names that count as "configured".
# First match wins. Empty list = local provider, no key needed.
_PROVIDER_ENV_KEYS: dict[str, list[str]] = {
    "Anthropic": ["ANTHROPIC_API_KEY"],
    "DeepSeek": ["DEEPSEEK_API_KEY"],
    "Google": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    "Groq": ["GROQ_API_KEY"],
    "Kimi": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
    "OpenAI": ["OPENAI_API_KEY"],
    "OpenRouter": ["OPENROUTER_API_KEY"],
    "xAI": ["XAI_API_KEY"],
    "Azure OpenAI": ["AZURE_OPENAI_API_KEY"],
    "GigaChat": ["GIGACHAT_API_KEY", "GIGACHAT_CREDENTIALS"],
    "Ollama": [],     # local
    "LM Studio": [],  # local
    "Meta": ["META_API_KEY"],
    "Alibaba": ["ALIBABA_API_KEY", "DASHSCOPE_API_KEY"],
    "Mistral": ["MISTRAL_API_KEY"],
}

# A provider can also store its key in the DB under any of these aliases —
# the Settings UI uses these names verbatim. Matches case-insensitively.
_PROVIDER_DB_ALIASES: dict[str, list[str]] = {
    "Anthropic": ["anthropic"],
    "DeepSeek": ["deepseek"],
    "Google": ["google", "gemini"],
    "Groq": ["groq"],
    "Kimi": ["kimi", "moonshot"],
    "OpenAI": ["openai"],
    "OpenRouter": ["openrouter"],
    "xAI": ["xai"],
    "Azure OpenAI": ["azure", "azure_openai", "azure openai"],
    "GigaChat": ["gigachat"],
    "Meta": ["meta"],
    "Alibaba": ["alibaba", "dashscope"],
    "Mistral": ["mistral"],
}


def _db_providers_with_keys() -> set[str]:
    """Lowercased set of provider names currently active in the DB."""
    try:
        db = SessionLocal()
        try:
            repo = ApiKeyRepository(db)
            rows = repo.get_all_api_keys(include_inactive=False)
            return {str(r.provider).strip().lower() for r in rows if (r.key_value or "").strip()}
        finally:
            db.close()
    except Exception as e:
        # If the DB is unavailable, fall back to env-only — better than
        # hiding every model. Log so an operator notices.
        logger.warning("api-key DB read failed; falling back to env: %s", e)
        return set()


def available_providers() -> set[str]:
    """Display-names of providers that have a usable key (env *or* DB),
    plus the local providers that never need one."""
    db_aliases = _db_providers_with_keys()
    out: set[str] = set()
    for name, env_keys in _PROVIDER_ENV_KEYS.items():
        if not env_keys:
            # Local — always "available" from a keys perspective; the
            # discovery service decides if it's reachable.
            out.add(name)
            continue
        if any((os.environ.get(k) or "").strip() for k in env_keys):
            out.add(name)
            continue
        aliases = _PROVIDER_DB_ALIASES.get(name, [name.lower()])
        if any(a in db_aliases for a in aliases):
            out.add(name)
    return out


def filter_models_by_available_keys(models: Iterable[dict]) -> list[dict]:
    """Drop any model whose ``provider`` has no key configured."""
    avail = available_providers()
    return [m for m in models if m.get("provider") in avail]
