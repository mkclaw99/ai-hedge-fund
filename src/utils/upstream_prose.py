"""Surface upstream analysts' prose to a downstream agent.

The flow editor lets the user wire analyst → analyst → PM. The DAG already
respects that ordering (`create_graph` adds the edges). What was missing: the
prose itself — every agent only got its own data, not the upstream's memo.

This helper builds a Markdown block from the upstream agents' `reasoning`
fields (the committee memos written by the upstream) so the downstream's
LLM call literally reads what came before. Wired in via `call_llm` (see
`_inject_upstream_prose` in `src/utils/llm.py`) so every LLM analyst and
the PM benefit without per-agent plumbing.

Fail-open: a missing map, an upstream that hasn't run yet, or any unexpected
shape returns "" — the call proceeds with whatever the agent's own prompt
already says.
"""

import re


def _normalize_agent_name(agent_id: str) -> str:
    """Turn "warren_buffett_a1b2c3" / "warren_buffett_agent" into "Warren Buffett"."""
    n = re.sub(r"_agent$", "", agent_id or "")
    n = re.sub(r"_[a-z0-9]{6}$", "", n)
    n = n.replace("_", " ").strip()
    return n.title() if n else (agent_id or "")


def build_upstream_block(state, agent_id, *, current_ticker=None):
    """Build a Markdown block of upstream analysts' full memos for the prompt.

    Args:
        state: AgentState dict (or anything dict-shaped with metadata/data).
        agent_id: the running agent's id (key into state["metadata"]["upstream_map"]).
        current_ticker: when set, only include upstream memos for this ticker; when
            None, include memos for every ticker the upstream has covered (used by
            whole-universe callers like the PM).

    Returns "" when there's no upstream wired, no upstream prose yet, or anything
    looks malformed.
    """
    try:
        meta = (state or {}).get("metadata", {}) or {}
        upstream_ids = (meta.get("upstream_map") or {}).get(agent_id, []) or []
        if not upstream_ids:
            return ""

        signals = ((state or {}).get("data", {}) or {}).get("analyst_signals", {}) or {}
        if current_ticker:
            tickers = [current_ticker]
        else:
            tickers = list(((state or {}).get("data", {}) or {}).get("tickers") or [])

        sections: list[str] = []
        for upid in upstream_ids:
            ups = signals.get(upid)
            if not isinstance(ups, dict):
                continue
            name = _normalize_agent_name(upid)
            for t in tickers:
                ins = ups.get(t)
                if not isinstance(ins, dict):
                    continue
                rsn = ins.get("reasoning") or ""
                if not rsn:
                    continue
                head_bits = [f"### {name} on {t}"]
                sig = ins.get("signal")
                conf = ins.get("confidence")
                if sig:
                    head_bits.append(f"— {sig}")
                if conf is not None:
                    head_bits.append(f"({conf}%)")
                sections.append(" ".join(head_bits) + "\n\n" + str(rsn))

        if not sections:
            return ""
        return (
            "## Upstream analyst memos — read these in full before forming your view\n\n"
            "The flow wires these analysts upstream of you. Their committee memos are "
            "below; treat them as colleagues' written opinions to integrate (or "
            "disagree with — explicitly), not as raw data.\n\n"
            + "\n\n---\n\n".join(sections)
        )
    except Exception:
        return ""
