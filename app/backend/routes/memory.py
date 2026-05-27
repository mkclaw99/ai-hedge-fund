"""Read-only endpoint exposing a flow's research memory to the UI.

The Memory node on the canvas calls this to render what the flow's wiki has
accumulated. Writes happen inside the run (see services/graph.py); this is
purely a view. Fail-open: any problem returns an empty payload rather than a 500,
so the node degrades gracefully.
"""

from typing import Optional

from fastapi import APIRouter

from src.memory import flow_root
from src.memory.ingest import PM_ANALYST
from src.memory.store import WikiMemory

router = APIRouter(prefix="/memory")


@router.get("")
async def get_flow_memory(flow_id: Optional[int] = None, tickers: Optional[str] = None):
    """Return the flow's accumulated research per ticker.

    Args:
        flow_id: the flow whose memory to read; ``None`` reads the shared "default"
                 namespace used by unsaved flows.
        tickers: optional comma-separated filter; when omitted, returns every
                 ticker the flow's wiki knows about.
    """
    slug = f"flow-{flow_id}" if flow_id is not None else "default"
    try:
        wiki = WikiMemory(flow_root(slug))
        requested = [t.strip().upper() for t in (tickers or "").split(",") if t.strip()]
        ticker_list = requested or wiki.list_tickers()

        out = []
        for t in ticker_list:
            ctx = wiki.query_ticker(t)
            if not ctx.latest_by_analyst:
                continue
            analysts, pm_decisions = [], []
            for name, ins in ctx.latest_by_analyst.items():
                row = {
                    "analyst": name,
                    "signal": ins.signal,
                    "confidence": int(ins.confidence),
                    "date": ins.date,
                    "reasoning": ins.reasoning,
                }
                (pm_decisions if name == PM_ANALYST else analysts).append(row)
            out.append({
                "ticker": ctx.ticker,
                "consensus": ctx.consensus,
                "bullish": ctx.bullish,
                "bearish": ctx.bearish,
                "neutral": ctx.neutral,
                "n_runs": ctx.n_runs,
                "n_insights": ctx.n_insights,
                "analysts": analysts,
                "pm_decisions": pm_decisions,
            })
        return {"flow_id": flow_id, "tickers": out}
    except Exception:
        # Fail-open: an empty/missing wiki is a normal state, not an error.
        return {"flow_id": flow_id, "tickers": []}
