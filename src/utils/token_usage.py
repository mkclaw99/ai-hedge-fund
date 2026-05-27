"""Cumulative LLM token-usage tracking, surfaced in the app's Settings.

A single chokepoint records token usage from every LLM call (agents via ``call_llm``,
plus the PDF distiller and the researchers). Usage is attributed by provider+model and
persisted to a small JSON file so the Settings → Token Usage view survives restarts.

Capture works even through structured output: ``UsageCallback`` is attached to each
``invoke(...)`` and reads ``usage_metadata`` off the raw LLM response in ``on_llm_end``
(which fires before any output parsing).
"""

import json
import os
import threading
from pathlib import Path

from langchain_core.callbacks.base import BaseCallbackHandler

_LOCK = threading.Lock()

# Stored next to the WikiMemory (which is gitignored); override with TOKEN_USAGE_PATH.
_BASE = os.environ.get("WIKI_MEMORY_DIR") or "wiki"
_PATH = Path(os.environ.get("TOKEN_USAGE_PATH") or (Path(_BASE) / "token_usage.json"))


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception:
        pass


def record_usage(provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
    """Add one call's token counts to the cumulative store (fail-open, thread-safe)."""
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    if not (input_tokens or output_tokens):
        return
    key = f"{provider or '?'}::{model or '?'}"
    with _LOCK:
        data = _load()
        entry = data.get(key) or {
            "provider": provider or "?",
            "model": model or "?",
            "input_tokens": 0,
            "output_tokens": 0,
            "calls": 0,
        }
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        entry["calls"] += 1
        data[key] = entry
        _save(data)


def get_usage() -> dict:
    """Return cumulative usage as ``{models: [...], totals: {...}}`` (sorted by total)."""
    with _LOCK:
        data = _load()
    rows = []
    for r in data.values():
        r = dict(r)
        r["total_tokens"] = int(r.get("input_tokens", 0)) + int(r.get("output_tokens", 0))
        rows.append(r)
    rows.sort(key=lambda r: r["total_tokens"], reverse=True)
    totals = {
        "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "calls": sum(r.get("calls", 0) for r in rows),
    }
    return {"models": rows, "totals": totals}


def reset_usage() -> None:
    """Clear all recorded usage."""
    with _LOCK:
        _save({})


def _extract_usage(response) -> tuple[int, int]:
    """Pull (input, output) token counts from a LangChain LLMResult, across providers."""
    usage = None
    try:
        gen = response.generations[0][0]
        msg = getattr(gen, "message", None)
        if msg is not None:
            usage = getattr(msg, "usage_metadata", None)
            if usage is None:
                meta = getattr(msg, "response_metadata", None) or {}
                usage = meta.get("usage") or meta.get("token_usage")
    except Exception:
        usage = None
    if not usage:
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or llm_output.get("usage")
    if not usage:
        return 0, 0
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return inp, out


class UsageCallback(BaseCallbackHandler):
    """Attach to an ``invoke(...)`` to record that call's token usage. Fail-open."""

    def __init__(self, provider: str, model: str):
        self.provider = str(provider) if provider else "?"
        self.model = str(model) if model else "?"

    def on_llm_end(self, response, **kwargs) -> None:
        try:
            inp, out = _extract_usage(response)
            record_usage(self.provider, self.model, inp, out)
        except Exception:
            pass
