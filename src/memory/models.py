"""Data models for the WikiMemory store.

Plain dataclasses (no extra deps) describing the unit of knowledge the wiki
accumulates: a single analyst's view on a single ticker from a single run.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Insight:
    """One analyst's view on one ticker from one run — the atom of the wiki.

    These map directly to ``state["data"]["analyst_signals"][agent][ticker]``
    produced by the LangGraph agents, plus run-level context (date, run_id).
    """

    ticker: str
    analyst: str                 # human-readable, e.g. "Warren Buffett"
    signal: str                  # "bullish" | "bearish" | "neutral"
    confidence: float            # 0-100
    reasoning: str
    date: str                    # analysis as-of date, YYYY-MM-DD
    run_id: str                  # groups all insights from one run

    def slug(self) -> str:
        """Stable file slug for this insight's immutable source page."""
        return f"{self.date}-{self.ticker}-{_slugify(self.analyst)}"


@dataclass
class TickerContext:
    """Accumulated prior knowledge about one ticker, returned by query.

    Used both for read-back (feeding the Portfolio Manager) and for answering
    questions about a ticker's evolving thesis.
    """

    ticker: str
    latest_by_analyst: dict[str, Insight] = field(default_factory=dict)
    n_insights: int = 0
    n_runs: int = 0

    @property
    def bullish(self) -> list[str]:
        return [a for a, i in self.latest_by_analyst.items() if i.signal == "bullish"]

    @property
    def bearish(self) -> list[str]:
        return [a for a, i in self.latest_by_analyst.items() if i.signal == "bearish"]

    @property
    def neutral(self) -> list[str]:
        return [a for a, i in self.latest_by_analyst.items() if i.signal == "neutral"]

    @property
    def consensus(self) -> str:
        """Net stance across the latest view per analyst."""
        b, r = len(self.bullish), len(self.bearish)
        if b > r:
            return "bullish"
        if r > b:
            return "bearish"
        return "neutral"

    @property
    def has_disagreement(self) -> bool:
        """True when analysts currently hold opposing bullish/bearish views."""
        return bool(self.bullish) and bool(self.bearish)


def _slugify(text: str) -> str:
    """Lowercase, hyphenated, filesystem-safe slug."""
    out = []
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " _-/":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "untitled"
