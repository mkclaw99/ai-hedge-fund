"""Persistent LLM response cache.

Wraps every LangChain LLM call with a SQLite-backed cache so:

- Identical (prompt, model) pairs across runs are served instantly from disk —
  no token spend, no rate-limit budget consumed (the cache hit short-circuits
  *before* LangChain's `InMemoryRateLimiter` even sees the call).
- Within a single run, re-invocations of the same analyst on the same data
  short-circuit (helpful when nothing upstream has changed between calls).

We deliberately don't depend on ``langchain-community`` — its ``SQLiteCache``
would do the same thing, but pulling in the whole package for one 60-line file
isn't worth it. The contract we implement is :class:`BaseCache` from
``langchain_core``; once installed via :func:`set_llm_cache`, every LLM
provider (OpenAI, Anthropic, Gemini, Ollama, …) goes through it.

Reuse boundaries — be aware before celebrating big cost savings:

- ``_inject_self_memory`` adds the analyst's prior runs to its prompt, so
  prompts diverge after the first run on a ticker. The cache helps the
  *first* re-run before memory grows; later re-runs miss.
- ``_inject_upstream_prose`` (PR #50) adds upstream analysts' memos at
  runtime, so downstream calls are unique per run. Cache misses there are
  expected and correct.
- Backtests sweep dates; each date is a different prompt, so each date is
  cached independently (good if the same date is replayed; useless across
  dates).

Fail-open at every layer: a corrupt DB or unexpected error returns ``None``
on lookup and a no-op on update, so the run continues uncached rather than
crashing. Set ``HEDGE_LLM_CACHE=disabled`` (or any falsy value: ``0``,
``false``, ``no``) to skip cache setup entirely.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence

from langchain_core.caches import BaseCache, RETURN_VAL_TYPE
from langchain_core.outputs import Generation

logger = logging.getLogger(__name__)


def _default_db_path() -> Path:
    # Project-local default — keeps the cache visible alongside the code and
    # easy to wipe (`rm .cache/llm.sqlite`) when prompts change shape and old
    # cached results would be stale.
    return Path.cwd() / ".cache" / "llm.sqlite"


class SqliteLLMCache(BaseCache):
    """Tiny SQLite-backed implementation of LangChain's BaseCache."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite + threads: stdlib sqlite3 forbids cross-thread connection use
        # unless ``check_same_thread=False``. The backend's run_in_executor and
        # LangChain's async helpers happily cross threads, so we open one
        # connection per call inside a lock — simple and correct, slightly
        # slower than connection-pooling but the cache is the fast path.
        self._lock = threading.Lock()
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS llm_cache (prompt TEXT, llm TEXT, generations TEXT, PRIMARY KEY (prompt, llm))"
            )
            conn.commit()

    def lookup(self, prompt: str, llm_string: str) -> RETURN_VAL_TYPE | None:
        try:
            with self._lock, sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT generations FROM llm_cache WHERE prompt = ? AND llm = ?",
                    (prompt, llm_string),
                ).fetchone()
            if not row:
                return None
            raw = json.loads(row[0])
            # JSON serialization stores Generations as plain dicts; rebuild.
            return [Generation(**g) for g in raw]
        except Exception as e:
            logger.debug("llm cache lookup failed (returning miss): %s", e)
            return None

    def update(self, prompt: str, llm_string: str, return_val: RETURN_VAL_TYPE) -> None:
        try:
            payload = json.dumps(
                [
                    {"text": g.text, "generation_info": getattr(g, "generation_info", None) or None}
                    for g in (return_val or [])
                ]
            )
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO llm_cache (prompt, llm, generations) VALUES (?, ?, ?)",
                    (prompt, llm_string, payload),
                )
                conn.commit()
        except Exception as e:
            logger.debug("llm cache update failed (continuing uncached): %s", e)

    def clear(self, **_: Any) -> None:
        try:
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM llm_cache")
                conn.commit()
        except Exception as e:
            logger.warning("llm cache clear failed: %s", e)

    def size(self) -> int:
        try:
            with self._lock, sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0


_INSTALLED = False


def _flag_off(value: str) -> bool:
    return value.strip().lower() in {"", "0", "false", "no", "off", "disabled"}


def init_llm_cache(db_path: str | None = None) -> SqliteLLMCache | None:
    """Install the global LLM cache. Idempotent + fail-open.

    Honors ``HEDGE_LLM_CACHE``:
        - unset or empty → on, default path.
        - "disabled" / "0" / "false" → off, returns None.
        - any other value → on, used as the SQLite file path.
    """
    global _INSTALLED
    if _INSTALLED:
        return None

    env = os.environ.get("HEDGE_LLM_CACHE")
    if env is not None and _flag_off(env):
        logger.info("LLM cache disabled via HEDGE_LLM_CACHE")
        _INSTALLED = True  # don't re-attempt
        return None

    try:
        from langchain_core.globals import set_llm_cache

        path = db_path or (env if env else None) or _default_db_path()
        cache = SqliteLLMCache(path)
        set_llm_cache(cache)
        _INSTALLED = True
        logger.info("LLM cache installed at %s (entries: %d)", path, cache.size())
        return cache
    except Exception as e:
        logger.warning("LLM cache setup failed (continuing without cache): %s", e)
        _INSTALLED = True
        return None
