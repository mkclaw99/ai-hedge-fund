"""Read-only endpoints exposing a flow's research memory to the UI.

* ``GET /memory`` — the Memory node on the canvas reads this to render the
  flow's accumulated wiki (per-ticker stance, analysts, PM decisions).
* ``GET /memory/track-record`` — the Strategy node's Track Record dialog
  reads this; scores past decisions against actual price moves and returns
  a structured summary (overall hit rate, per-analyst, per-(analyst,ticker),
  recent rows) — same data the PM sees in its prompt, but as JSON.

Both endpoints fail-open: any problem returns an empty payload rather than a
500, so the dialog degrades gracefully when the wiki is empty or unreachable.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.backend.database import get_db
from app.backend.services.api_key_service import ApiKeyService
from src.memory import flow_root
from src.memory.ingest import PM_ANALYST
from src.memory.store import WikiMemory
from src.memory.track_record import (
    compute_outcomes,
    holding_days_from_strategy,
    track_record_summary,
)

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


@router.get("/track-record")
async def get_track_record(
    flow_id: Optional[int] = None,
    holding_days: int = 30,
    holding_period: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Score past decisions against actual price moves.

    Args:
        flow_id: scope to this flow's wiki. Unset → empty payload (we never
                 fall through to the global default — same isolation rule as
                 the rest of the memory pipeline).
        holding_days: explicit horizon (days). Default 30.
        holding_period: alternative — pass "day" / "swing" / "position" /
                 "long_term" and we resolve to the matching day count. Takes
                 precedence over ``holding_days`` if both are sent.

    Returns ``{flow_id, holding_days, summary}`` where ``summary`` is the
    structured rollup the frontend's TrackRecordDialog renders. Fail-open.
    """
    empty = {"flow_id": flow_id, "holding_days": holding_days, "summary": track_record_summary([])}
    if flow_id is None:
        return empty
    try:
        slug = f"flow-{flow_id}"
        root = flow_root(slug)
        if not root:
            return empty
        # Resolve holding period → days (if provided) so the dialog matches
        # whatever the Strategy node would actually inject into the prompt.
        if holding_period:
            holding_days = holding_days_from_strategy({"holding_period": holding_period})
        wiki = WikiMemory(root)
        tickers = wiki.list_tickers()
        if not tickers:
            return empty
        api_keys = ApiKeyService(db).get_api_keys_dict()
        outcomes = compute_outcomes(
            tickers, root,
            api_keys=api_keys,
            holding_days=holding_days,
        )
        return {
            "flow_id": flow_id,
            "holding_days": holding_days,
            "summary": track_record_summary(outcomes),
        }
    except Exception:
        return empty
