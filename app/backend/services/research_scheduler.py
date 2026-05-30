"""Background scheduler: re-runs scheduled research areas on a cadence.

A research-area run persists its config in ``flow.data["researchRun"]`` (via
``persist_research_run``). The scheduler periodically finds flows whose cadence is
due and **replays** that config headless — re-discovering the universe via the
analyst MCP and re-running the analysts + PM — then stamps ``last_run``.

Guardrails (re-analysis is the costly choice): only ``hourly``/``daily``/``weekly``
cadences, default ``off``, and a flow is only eligible once it's been run manually
once (which is what arms it). Runs single-process; do not launch under ``--reload``
or multiple workers or cadences double-fire.
"""

import asyncio
import datetime
import logging
import os

from sqlalchemy.orm.attributes import flag_modified

from app.backend.database import SessionLocal
from app.backend.database.models import HedgeFundFlow
from app.backend.services.run_executor import execute_research_run

logger = logging.getLogger(__name__)

_POLL_SECONDS = 300  # how often the loop wakes
_INTERVALS = {"hourly": 3_600, "daily": 86_400, "weekly": 604_800}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def is_enabled() -> bool:
    return os.environ.get("RESEARCH_SCHEDULER_DISABLED", "0") != "1"


def persist_research_run(db, request_data) -> None:
    """Record a research run's config + cadence on its flow so the scheduler can replay it.

    Stores the request with ``tickers`` cleared (so a replay re-discovers a fresh
    universe) and without ``api_keys`` (secrets are re-hydrated at run time).
    """
    flow_id = getattr(request_data, "flow_id", None)
    if flow_id is None or not getattr(request_data, "research_theme", None):
        return
    try:
        dumped = request_data.model_dump(mode="json", exclude={"api_keys"})
        dumped["tickers"] = []  # force fresh discovery on replay
        flow = db.query(HedgeFundFlow).filter(HedgeFundFlow.id == flow_id).first()
        if not flow:
            return
        data = dict(flow.data or {})
        data["researchRun"] = {
            "request_data": dumped,
            "schedule": getattr(request_data, "research_schedule", None) or "off",
            "last_run": _now_iso(),
        }
        flow.data = data
        flag_modified(flow, "data")
        db.commit()
    except Exception as e:  # never break a run over scheduling bookkeeping
        logger.warning("persist_research_run failed for flow %s: %s", flow_id, e)
        db.rollback()


def _due_flows(db) -> list[tuple[int, dict]]:
    out = []
    now = _now()
    for flow in db.query(HedgeFundFlow).all():
        rr = (flow.data or {}).get("researchRun")
        if not rr:
            continue
        interval = _INTERVALS.get(rr.get("schedule", "off"))
        if not interval:
            continue
        last = rr.get("last_run")
        try:
            last_dt = datetime.datetime.fromisoformat(last) if last else None
        except (TypeError, ValueError):
            last_dt = None
        if last_dt and (now - last_dt).total_seconds() < interval:
            continue
        out.append((flow.id, rr))
    return out


def _stamp_last_run(flow_id: int) -> None:
    db = SessionLocal()
    try:
        flow = db.query(HedgeFundFlow).filter(HedgeFundFlow.id == flow_id).first()
        if flow:
            data = dict(flow.data or {})
            data.setdefault("researchRun", {})["last_run"] = _now_iso()
            flow.data = data
            flag_modified(flow, "data")
            db.commit()
    finally:
        db.close()


async def _run_due(flow_id: int, rr: dict) -> None:
    from app.backend.models.schemas import HedgeFundRequest

    db = SessionLocal()
    try:
        req = HedgeFundRequest(**rr["request_data"])
        result = await execute_research_run(req, db)
        if result.get("error"):
            logger.warning("scheduled research refresh skipped flow %s: %s", flow_id, result["error"])
        else:
            logger.info("scheduled research refresh flow %s → %d tickers", flow_id, len(result.get("tickers") or []))
    except Exception:
        logger.exception("scheduled research run failed for flow %s", flow_id)
    finally:
        db.close()
    _stamp_last_run(flow_id)  # stamp even on skip/failure to avoid hot-looping


async def run_due_once() -> int:
    """Run all currently-due scheduled research areas once. Returns how many fired."""
    db = SessionLocal()
    try:
        due = _due_flows(db)
    finally:
        db.close()
    for flow_id, rr in due:
        await _run_due(flow_id, rr)
    return len(due)


async def scheduler_loop() -> None:
    """Poll forever, running due research areas. Started from the FastAPI lifespan."""
    logger.info("research scheduler started (poll every %ds)", _POLL_SECONDS)
    while True:
        try:
            n = await run_due_once()
            if n:
                logger.info("research scheduler ran %d due flow(s)", n)
        except Exception:
            logger.exception("research scheduler tick failed")
        await asyncio.sleep(_POLL_SECONDS)
