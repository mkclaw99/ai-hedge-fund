"""Hedge-side client for the `analyst` research platform, over its MCP server.

The hedge venv can't host an MCP client (its pinned fastapi/anyio conflicts with
the `mcp` SDK), so we shell out to ``analyst_mcp_helper.py`` using analyst's own
venv python — which does have `mcp`. The transport between helper and analyst is
genuine MCP over stdio; this module just orchestrates the subprocess.

Read-only and fail-open: any problem returns ``{"error": ...}`` rather than
raising, so a research run degrades gracefully when analyst is unavailable.

Configurable via env:
- ``ANALYST_MCP_PYTHON``  path to analyst's venv python
- ``ANALYST_MCP_CWD``     analyst backend dir (so `analyst.mcp_server` imports)
- ``ANALYST_MCP_DISABLED`` set to "1" to turn the integration off
"""

import asyncio
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PYTHON = "/Users/macclustera/Documents/analyst/backend/.venv/bin/python"
_DEFAULT_CWD = "/Users/macclustera/Documents/analyst/backend"
_HELPER = str(Path(__file__).with_name("analyst_mcp_helper.py"))


def is_enabled() -> bool:
    return os.environ.get("ANALYST_MCP_DISABLED", "0") != "1"


async def call_analyst_tool(tool: str, args: dict | None = None, *, timeout: float = 90.0) -> dict:
    """Call one analyst MCP tool and return its parsed JSON dict (or {"error": ...})."""
    if not is_enabled():
        return {"error": "analyst integration is disabled (ANALYST_MCP_DISABLED=1)"}

    python = os.environ.get("ANALYST_MCP_PYTHON", _DEFAULT_PYTHON)
    cwd = os.environ.get("ANALYST_MCP_CWD", _DEFAULT_CWD)
    if not Path(python).exists():
        return {"error": f"analyst venv python not found: {python}"}

    env = {**os.environ, "ANALYST_MCP_CWD": cwd}
    try:
        proc = await asyncio.create_subprocess_exec(
            python, _HELPER, tool, json.dumps(args or {}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"error": "analyst MCP call timed out"}
    except Exception as e:  # spawn failure
        logger.warning("analyst MCP spawn failed: %s", e)
        return {"error": f"analyst MCP spawn failed: {e}"}

    text = (out or b"").decode("utf-8", "replace").strip()
    if not text:
        tail = (err or b"").decode("utf-8", "replace")[-200:]
        return {"error": f"analyst MCP returned nothing ({tail})"}
    try:
        # Our JSON is the last stdout line (guard against stray prints).
        return json.loads(text.splitlines()[-1])
    except Exception as e:
        return {"error": f"analyst MCP returned bad JSON: {e}: {text[:200]}"}


async def list_themes() -> dict:
    """Investment themes: {"items": [{slug, name, status, node_count, company_count}], ...}."""
    return await call_analyst_tool("list_themes", {})


async def list_theme_companies(theme: str, limit: int = 100) -> dict:
    """Value-chain companies for a theme, ranked by exposure (with ticker/is_public)."""
    return await call_analyst_tool("list_theme_companies", {"theme": theme, "limit": limit})
