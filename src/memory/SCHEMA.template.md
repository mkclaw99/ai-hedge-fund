# Research Wiki — Schema & Conventions

> This is the **schema layer**. It tells an LLM (or a human) how this wiki is
> organized and how to maintain it. Co-evolve it as the wiki grows.

This wiki is a persistent, compounding knowledge base maintained by the AI
hedge fund. Instead of re-deriving analysis on every run, each run's analyst
insights are **integrated** into evolving pages here. The wiki keeps getting
richer with every run.

## Three layers

1. **Raw sources** (`sources/`) — immutable. One markdown file per
   *(date, ticker, analyst)*, with YAML frontmatter and the analyst's verbatim
   reasoning. Never edited after creation; the source of truth.
2. **Wiki** (`tickers/`, `analysts/`) — derived, LLM/engine-maintained pages
   synthesized from the raw sources. Entity pages per ticker; coverage pages
   per analyst. Regenerated to stay consistent with the sources.
3. **Schema** (this file) — the conventions.

## Layout

```
wiki/
  SCHEMA.md            this file
  index.md             content catalog (generated)
  log.md               append-only chronological record
  sources/             immutable raw insights (one per ticker-analyst-run)
  tickers/<TICKER>.md  synthesized entity page per ticker
  analysts/<slug>.md   coverage page per analyst
```

## Entity page (`tickers/<TICKER>.md`) conventions

- **Synthesis** — current consensus across the latest view per analyst
  (bullish / bearish / neutral counts and net stance).
- **Disagreements** — flagged automatically when analysts currently hold
  opposing bullish/bearish views, with citations to source pages.
- **Latest signals** — one row per analyst (most recent view), linking to the
  analyst's coverage page and the source page.
- **History** — chronological list of all source insights for the ticker.

## Operations

- **Ingest** — a run finishes → its analyst signals become source pages and the
  ticker/analyst pages + index + log are regenerated/updated.
- **Query** — read `index.md`, drill into relevant pages, synthesize an answer.
  Good answers can be filed back as new pages so explorations compound.
- **Lint** — health-check: contradictions, stale claims, orphan pages, missing
  cross-references.

## Log format

Each entry starts with a consistent prefix so it's greppable:
`## [YYYY-MM-DD] <op> | <detail>` — e.g. `grep "^## \[" log.md | tail -5`.
