"""Process-wide absolute paths anchored to the repo, not the CWD.

Several wiki and cache defaults used to be relative strings (``"wiki"``,
``"cache/fd_cache.db"``) and relied on the process being launched from
the repo root. When ``uvicorn`` was started from ``app/frontend`` instead
(easy mistake — that's where the dev server lives), the backend silently
created empty stub directories at ``app/frontend/wiki/flow-N/materials/``
and reported every uploaded PDF as missing, even though the real data
sat untouched at ``<repo>/wiki/flow-N/materials/``.

This module centralises the repo-root computation so that bug class is
gone for good. Existing env overrides (``WIKI_MEMORY_DIR``,
``TOKEN_USAGE_PATH``, ``TICKER_NAMES_PATH``) keep working — they were
already absolute when set; they just used to fall back to a relative
default. Now the fallback is absolute too.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Repo root, computed from this file's location (CWD-independent).

    Lives at ``<repo>/src/paths.py``, so one ``.parent`` reaches ``src/``
    and another reaches the repo. Stable regardless of where the Python
    interpreter happened to be launched from.
    """
    return Path(__file__).resolve().parent.parent


def wiki_base() -> Path:
    """Absolute base dir for the wiki memory tree.

    Honors ``$WIKI_MEMORY_DIR`` (for tests, sandboxed runs, multi-instance
    deployments); otherwise defaults to ``<repo>/wiki``.
    """
    env = os.environ.get("WIKI_MEMORY_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return repo_root() / "wiki"


def cache_base() -> Path:
    """Absolute base dir for the FD / persistent caches.

    Honors ``$HEDGE_CACHE_DIR`` for tests / multi-instance deployments;
    otherwise defaults to ``<repo>/cache``.
    """
    env = os.environ.get("HEDGE_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return repo_root() / "cache"


def llm_cache_base() -> Path:
    """Absolute base dir for the LangChain SQLite LLM cache (``.cache/llm.sqlite``).

    Honors ``$HEDGE_LLM_CACHE_DIR`` for tests; otherwise ``<repo>/.cache``.
    The hidden-dot prefix is preserved so existing wipe instructions
    (``rm .cache/llm.sqlite``) keep working when run from the repo root.
    """
    env = os.environ.get("HEDGE_LLM_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return repo_root() / ".cache"
