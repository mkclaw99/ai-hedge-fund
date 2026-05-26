"""WikiMemory — a persistent, compounding research wiki for the AI hedge fund.

Analyst insights from each run are integrated into an accumulating markdown
wiki (raw sources → synthesized ticker/analyst pages), and fed back into later
runs so knowledge compounds instead of being re-derived every time.
"""

from src.memory.ingest import ingest_run, is_enabled, normalize_analyst_name, read_back
from src.memory.models import Insight, TickerContext
from src.memory.store import WikiMemory

__all__ = [
    "Insight",
    "TickerContext",
    "WikiMemory",
    "ingest_run",
    "is_enabled",
    "normalize_analyst_name",
    "read_back",
]
