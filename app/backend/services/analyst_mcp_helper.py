"""MCP-client bridge to the `analyst` research platform.

Run this with the **analyst** venv python (which ships the `mcp` SDK) — the
hedge venv can't host an MCP client because its pinned fastapi/anyio conflicts
with `mcp`'s anyio>=4.5. Usage:

    <analyst-venv>/bin/python analyst_mcp_helper.py <tool> '<json-args>'

It connects to analyst's read-only stdio MCP server (`analyst.mcp_server.server`),
calls one tool, and prints the tool's JSON result on stdout. All diagnostics go
to stderr so stdout stays a single clean JSON line.
"""

import json
import os
import sys


def _eprint(*a):
    print(*a, file=sys.stderr)


async def _run(tool: str, args: dict):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cwd = os.environ.get("ANALYST_MCP_CWD") or os.getcwd()
    # The server runs under the same (analyst) python that runs this helper.
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "analyst.mcp_server.server"],
        cwd=cwd,
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)

            # Prefer the JSON text content (the dict the tool returned).
            payload = None
            for c in (result.content or []):
                text = getattr(c, "text", None)
                if text:
                    try:
                        payload = json.loads(text)
                        break
                    except Exception:
                        pass
            if payload is None:
                payload = getattr(result, "structuredContent", None)
            # FastMCP can wrap a bare value as {"result": ...}; unwrap it.
            if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
                payload = payload["result"]
            return payload


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: helper <tool> [json-args]"}))
        return
    tool = sys.argv[1]
    try:
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else {}
    except Exception as e:
        print(json.dumps({"error": f"bad args json: {e}"}))
        return
    try:
        import anyio

        payload = anyio.run(_run, tool, args)
        print(json.dumps(payload if payload is not None else {"error": "empty result"}))
    except Exception as e:  # fail-open: caller treats {"error": ...} gracefully
        _eprint("helper error:", repr(e))
        print(json.dumps({"error": f"analyst mcp call failed: {e}"}))


if __name__ == "__main__":
    main()
