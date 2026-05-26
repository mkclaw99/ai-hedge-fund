"""WikiMemory — a persistent, compounding research wiki on disk.

The store turns ephemeral per-run analyst signals into an accumulating wiki of
markdown files. Raw insights are written as immutable ``sources/`` pages; the
``tickers/`` and ``analysts/`` pages are *derived views* regenerated from those
sources on every ingest, so they're always consistent and idempotent.

Design notes
------------
- **Markdown on disk** — git-friendly, Obsidian-browsable. No database.
- **Sources are truth; wiki pages are views.** Re-ingesting the same run is a
  no-op (source files are keyed by date/ticker/analyst and overwritten).
- **No third-party deps** — a tiny hand-rolled YAML-frontmatter reader/writer
  keeps this importable anywhere in the project.
- **Fail-open by contract** — callers wrap ingest/read-back in try/except; this
  module avoids raising on bad input where it reasonably can.

Usage::

    from src.memory import WikiMemory, Insight

    wiki = WikiMemory()                 # defaults to ./wiki (or $WIKI_MEMORY_DIR)
    wiki.ingest([Insight(...)], run_id="r1")
    ctx = wiki.query_ticker("AAPL")
    print(wiki.render_context_for_prompt(["AAPL"]))
"""

from __future__ import annotations

import os
from datetime import date as _date
from pathlib import Path

from src.memory.models import Insight, TickerContext, _slugify

_TEMPLATE = Path(__file__).with_name("SCHEMA.template.md")
_DEFAULT_DIR = "wiki"


class WikiMemory:
    """A compounding markdown research wiki."""

    def __init__(self, root: str | os.PathLike | None = None) -> None:
        root = root or os.environ.get("WIKI_MEMORY_DIR") or _DEFAULT_DIR
        self.root = Path(root)
        self.sources = self.root / "sources"
        self.tickers = self.root / "tickers"
        self.analysts = self.root / "analysts"
        self._ensure_layout()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest(self, insights: list[Insight], *, run_id: str | None = None) -> int:
        """Write insights as immutable source pages and regenerate the wiki.

        Returns the number of insights written. Insights with a blank/None
        signal or ticker are skipped. Idempotent: re-ingesting the same run
        overwrites the same source files and regenerates identical pages.
        """
        valid = [i for i in insights if i.ticker and i.signal]
        if not valid:
            return 0

        for ins in valid:
            self._write_source(ins)

        # Regenerate derived views for the affected tickers/analysts + global index.
        tickers = sorted({i.ticker for i in valid})
        analysts = sorted({i.analyst for i in valid})
        for t in tickers:
            self._regenerate_ticker_page(t)
        for a in analysts:
            self._regenerate_analyst_page(a)
        self._regenerate_index()

        self._append_log(
            f"ingest | run {run_id or '?'} | {len(valid)} insights across "
            f"{len(tickers)} tickers ({', '.join(tickers)})"
        )
        return len(valid)

    def query_ticker(self, ticker: str) -> TickerContext:
        """Return the accumulated prior knowledge for *ticker*."""
        ticker = ticker.upper()
        insights = self._read_sources_for_ticker(ticker)
        ctx = TickerContext(ticker=ticker, n_insights=len(insights))
        ctx.n_runs = len({i.run_id for i in insights})
        # latest view per analyst (by date, then file order)
        for ins in sorted(insights, key=lambda i: i.date):
            ctx.latest_by_analyst[ins.analyst] = ins
        return ctx

    def render_context_for_prompt(self, tickers: list[str], *, max_reasoning: int = 160) -> str:
        """Compact markdown digest of prior research, for LLM prompt injection.

        Returns "" when there's nothing on record, so callers can cheaply skip
        adding an empty section.
        """
        blocks: list[str] = []
        for t in tickers:
            ctx = self.query_ticker(t)
            if not ctx.latest_by_analyst:
                continue
            lines = [
                f"{ctx.ticker}: prior consensus {ctx.consensus} "
                f"({len(ctx.bullish)} bull / {len(ctx.bearish)} bear / "
                f"{len(ctx.neutral)} neutral; {ctx.n_runs} run(s))."
            ]
            if ctx.has_disagreement:
                lines.append(
                    f"  Disagreement: bullish [{', '.join(ctx.bullish)}] vs "
                    f"bearish [{', '.join(ctx.bearish)}]."
                )
            # one terse line per analyst's latest stance
            for analyst, ins in ctx.latest_by_analyst.items():
                r = ins.reasoning.strip().replace("\n", " ")
                if len(r) > max_reasoning:
                    r = r[: max_reasoning - 1] + "…"
                lines.append(f"  - {analyst} ({ins.date}): {ins.signal} {int(ins.confidence)}% — {r}")
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    def lint(self) -> list[str]:
        """Health-check the wiki; return a list of human-readable findings."""
        findings: list[str] = []
        all_insights = self._read_all_sources()
        by_ticker: dict[str, list[Insight]] = {}
        for ins in all_insights:
            by_ticker.setdefault(ins.ticker, []).append(ins)

        if not all_insights:
            findings.append("Wiki is empty — no sources ingested yet.")
            return findings

        for ticker in sorted(by_ticker):
            ctx = self.query_ticker(ticker)
            if ctx.has_disagreement:
                findings.append(
                    f"{ticker}: unresolved disagreement — bullish "
                    f"[{', '.join(ctx.bullish)}] vs bearish [{', '.join(ctx.bearish)}]."
                )
            if ctx.n_insights == 1:
                findings.append(f"{ticker}: thin coverage — only 1 analyst on record.")

        # orphan tickers: an entity page with no source files (shouldn't happen,
        # but catches manual edits / stale files)
        for page in self.tickers.glob("*.md"):
            t = page.stem
            if t not in by_ticker:
                findings.append(f"{t}: orphan entity page with no sources.")

        return findings

    # ------------------------------------------------------------------
    # Layout / IO
    # ------------------------------------------------------------------

    def _ensure_layout(self) -> None:
        for d in (self.root, self.sources, self.tickers, self.analysts):
            d.mkdir(parents=True, exist_ok=True)
        schema = self.root / "SCHEMA.md"
        if not schema.exists():
            try:
                schema.write_text(_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                schema.write_text("# Research Wiki\n", encoding="utf-8")
        log = self.root / "log.md"
        if not log.exists():
            log.write_text("# Wiki Log\n\n", encoding="utf-8")

    def _write_source(self, ins: Insight) -> None:
        fm = _frontmatter({
            "ticker": ins.ticker,
            "analyst": ins.analyst,
            "signal": ins.signal,
            "confidence": int(ins.confidence),
            "date": ins.date,
            "run_id": ins.run_id,
        })
        body = (
            f"{fm}\n# {ins.ticker} — {ins.analyst} ({ins.date})\n\n"
            f"**Signal:** {ins.signal}  |  **Confidence:** {int(ins.confidence)}%\n\n"
            f"## Reasoning\n\n{ins.reasoning.strip() or '(none provided)'}\n\n"
            f"[[{ins.ticker}]] · [[{_slugify(ins.analyst)}|{ins.analyst}]]\n"
        )
        (self.sources / f"{ins.slug()}.md").write_text(body, encoding="utf-8")

    def _read_sources_for_ticker(self, ticker: str) -> list[Insight]:
        return [i for i in self._read_all_sources() if i.ticker == ticker.upper()]

    def _read_all_sources(self) -> list[Insight]:
        out: list[Insight] = []
        for f in self.sources.glob("*.md"):
            meta = _read_frontmatter(f.read_text(encoding="utf-8"))
            if not meta.get("ticker"):
                continue
            out.append(Insight(
                ticker=str(meta.get("ticker", "")).upper(),
                analyst=str(meta.get("analyst", "Unknown")),
                signal=str(meta.get("signal", "")).lower(),
                confidence=_to_float(meta.get("confidence")),
                reasoning=_read_reasoning(f.read_text(encoding="utf-8")),
                date=str(meta.get("date", "")),
                run_id=str(meta.get("run_id", "")),
            ))
        return out

    # ------------------------------------------------------------------
    # Derived page regeneration
    # ------------------------------------------------------------------

    def _regenerate_ticker_page(self, ticker: str) -> None:
        ticker = ticker.upper()
        ctx = self.query_ticker(ticker)
        insights = sorted(self._read_sources_for_ticker(ticker), key=lambda i: i.date, reverse=True)

        lines = [f"# {ticker}\n"]
        lines.append("## Synthesis\n")
        lines.append(
            f"Current consensus: **{ctx.consensus}** "
            f"({len(ctx.bullish)} bullish / {len(ctx.bearish)} bearish / "
            f"{len(ctx.neutral)} neutral across {ctx.n_runs} run(s), "
            f"{ctx.n_insights} insight(s)).\n"
        )

        if ctx.has_disagreement:
            lines.append("## ⚠️ Disagreements\n")
            lines.append(
                f"- Bullish: {', '.join(_wlink(_slugify(a), a) for a in ctx.bullish)}\n"
                f"- Bearish: {', '.join(_wlink(_slugify(a), a) for a in ctx.bearish)}\n"
            )

        lines.append("## Latest signals\n")
        lines.append("| Analyst | Signal | Conf | Date |")
        lines.append("|---|---|---|---|")
        for analyst, ins in sorted(ctx.latest_by_analyst.items()):
            lines.append(
                f"| {_wlink(_slugify(analyst), analyst)} | {ins.signal} | "
                f"{int(ins.confidence)}% | {ins.date} |"
            )
        lines.append("")

        lines.append("## History\n")
        for ins in insights:
            lines.append(f"- `{ins.date}` **{ins.analyst}** — {ins.signal} "
                         f"({int(ins.confidence)}%) · [[{ins.slug()}|source]]")
        lines.append("")
        (self.tickers / f"{ticker}.md").write_text("\n".join(lines), encoding="utf-8")

    def _regenerate_analyst_page(self, analyst: str) -> None:
        slug = _slugify(analyst)
        mine = [i for i in self._read_all_sources() if _slugify(i.analyst) == slug]
        latest_by_ticker: dict[str, Insight] = {}
        for ins in sorted(mine, key=lambda i: i.date):
            latest_by_ticker[ins.ticker] = ins

        lines = [f"# {analyst}\n", "Coverage page — latest stance per ticker.\n"]
        lines.append("| Ticker | Signal | Conf | Date |")
        lines.append("|---|---|---|---|")
        for ticker, ins in sorted(latest_by_ticker.items()):
            lines.append(f"| [[{ticker}]] | {ins.signal} | {int(ins.confidence)}% | {ins.date} |")
        lines.append("")
        (self.analysts / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")

    def _regenerate_index(self) -> None:
        all_insights = self._read_all_sources()
        tickers = sorted({i.ticker for i in all_insights})
        analysts = sorted({i.analyst for i in all_insights}, key=str.lower)

        lines = ["# Wiki Index\n", "Catalog of everything in the wiki.\n"]
        lines.append(f"**{len(tickers)} tickers · {len(analysts)} analysts · "
                     f"{len(all_insights)} insights.**\n")

        lines.append("## Tickers\n")
        for t in tickers:
            ctx = self.query_ticker(t)
            flag = " ⚠️" if ctx.has_disagreement else ""
            lines.append(f"- [[{t}]] — consensus {ctx.consensus}, "
                         f"{ctx.n_insights} insight(s){flag}")
        lines.append("")

        lines.append("## Analysts\n")
        for a in analysts:
            lines.append(f"- [[{_slugify(a)}|{a}]]")
        lines.append("")
        (self.root / "index.md").write_text("\n".join(lines), encoding="utf-8")

    def _append_log(self, detail: str) -> None:
        today = _date.today().isoformat()
        with (self.root / "log.md").open("a", encoding="utf-8") as f:
            f.write(f"## [{today}] {detail}\n")


# ---------------------------------------------------------------------------
# Tiny frontmatter helpers (no yaml dependency)
# ---------------------------------------------------------------------------

def _frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _read_frontmatter(text: str) -> dict:
    meta: dict = {}
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta


def _read_reasoning(text: str) -> str:
    marker = "## Reasoning"
    idx = text.find(marker)
    if idx == -1:
        return ""
    rest = text[idx + len(marker):]
    # stop at the next heading / wiki-link footer
    for stop in ("\n# ", "\n## ", "\n[["):
        cut = rest.find(stop)
        if cut != -1:
            rest = rest[:cut]
    return rest.strip()


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _wlink(slug: str, label: str) -> str:
    return f"[[{slug}|{label}]]"
