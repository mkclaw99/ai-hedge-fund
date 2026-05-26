"""Offline tests for WikiMemory — no network, no LLM, tmp dir only."""

from __future__ import annotations

import pytest

from src.memory import Insight, WikiMemory, ingest_run, normalize_analyst_name, read_back


@pytest.fixture
def wiki(tmp_path):
    return WikiMemory(tmp_path / "wiki")


def _ins(ticker, analyst, signal, conf=70, reasoning="because", date="2026-05-26", run="r1"):
    return Insight(ticker=ticker, analyst=analyst, signal=signal, confidence=conf,
                   reasoning=reasoning, date=date, run_id=run)


# ---------------------------------------------------------------------------
# Layout & ingest
# ---------------------------------------------------------------------------

def test_layout_and_schema_created(wiki, tmp_path):
    assert (tmp_path / "wiki" / "SCHEMA.md").exists()
    assert (tmp_path / "wiki" / "sources").is_dir()
    assert (tmp_path / "wiki" / "tickers").is_dir()


def test_ingest_writes_all_layers(wiki):
    n = wiki.ingest([_ins("AAPL", "Warren Buffett", "bullish")], run_id="r1")
    assert n == 1
    assert (wiki.sources / "2026-05-26-AAPL-warren-buffett.md").exists()
    assert (wiki.tickers / "AAPL.md").exists()
    assert (wiki.analysts / "warren-buffett.md").exists()
    assert (wiki.root / "index.md").exists()
    entity = (wiki.tickers / "AAPL.md").read_text()
    assert "bullish" in entity and "Warren Buffett" in entity
    log = (wiki.root / "log.md").read_text()
    assert "ingest" in log and "AAPL" in log


def test_ingest_skips_invalid(wiki):
    assert wiki.ingest([_ins("", "X", "bullish"), _ins("AAPL", "X", "")], run_id="r") == 0


def test_idempotent_reingest(wiki):
    wiki.ingest([_ins("AAPL", "Warren Buffett", "bullish")], run_id="r1")
    wiki.ingest([_ins("AAPL", "Warren Buffett", "bullish")], run_id="r1")
    # same source file, regenerated identical page; one source on record
    assert len(list(wiki.sources.glob("*.md"))) == 1
    assert wiki.query_ticker("AAPL").n_insights == 1


# ---------------------------------------------------------------------------
# Synthesis, contradictions, query
# ---------------------------------------------------------------------------

def test_consensus_and_disagreement(wiki):
    wiki.ingest([
        _ins("AAPL", "Warren Buffett", "bullish"),
        _ins("AAPL", "Michael Burry", "bearish"),
        _ins("AAPL", "Peter Lynch", "bullish"),
    ], run_id="r1")
    ctx = wiki.query_ticker("AAPL")
    assert ctx.consensus == "bullish"           # 2 bull vs 1 bear
    assert ctx.has_disagreement is True
    assert set(ctx.bullish) == {"Warren Buffett", "Peter Lynch"}
    entity = (wiki.tickers / "AAPL.md").read_text()
    assert "Disagreements" in entity
    index = (wiki.root / "index.md").read_text()
    assert "⚠️" in index


def test_latest_view_supersedes_prior(wiki):
    wiki.ingest([_ins("AAPL", "Warren Buffett", "bearish", date="2026-05-20", run="r1")], run_id="r1")
    wiki.ingest([_ins("AAPL", "Warren Buffett", "bullish", date="2026-05-26", run="r2")], run_id="r2")
    ctx = wiki.query_ticker("AAPL")
    assert ctx.n_insights == 2 and ctx.n_runs == 2
    assert ctx.latest_by_analyst["Warren Buffett"].signal == "bullish"   # newest wins
    assert ctx.consensus == "bullish"


def test_query_empty_ticker(wiki):
    ctx = wiki.query_ticker("ZZZZ")
    assert ctx.n_insights == 0 and ctx.consensus == "neutral"


# ---------------------------------------------------------------------------
# Read-back digest & lint
# ---------------------------------------------------------------------------

def test_render_context_for_prompt(wiki):
    assert wiki.render_context_for_prompt(["AAPL"]) == ""        # nothing yet
    wiki.ingest([
        _ins("AAPL", "Warren Buffett", "bullish", reasoning="Wide moat, strong FCF"),
        _ins("AAPL", "Michael Burry", "bearish", reasoning="Overvalued vs history"),
    ], run_id="r1")
    digest = wiki.render_context_for_prompt(["AAPL"])
    assert "AAPL: prior consensus" in digest
    assert "Disagreement" in digest
    assert "Warren Buffett" in digest and "moat" in digest


def test_lint_flags_disagreement_and_thin(wiki):
    wiki.ingest([_ins("MSFT", "Cathie Wood", "bullish")], run_id="r1")              # thin
    wiki.ingest([
        _ins("AAPL", "Warren Buffett", "bullish"),
        _ins("AAPL", "Michael Burry", "bearish"),
    ], run_id="r1")
    findings = " ".join(wiki.lint())
    assert "disagreement" in findings.lower()
    assert "thin coverage" in findings.lower()


def test_lint_empty(wiki):
    assert "empty" in " ".join(wiki.lint()).lower()


# ---------------------------------------------------------------------------
# Run-level helper (the chokepoint both run paths call)
# ---------------------------------------------------------------------------

def test_ingest_run_normalizes_and_skips_non_analysts(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_MEMORY_ENABLED", "1")
    root = str(tmp_path / "wiki")
    signals = {
        "warren_buffett_a1b2c3": {"AAPL": {"signal": "bullish", "confidence": 80, "reasoning": "moat"}},
        "michael_burry_agent": {"AAPL": {"signal": "bearish", "confidence": 60, "reasoning": "value"}},
        "risk_management_agent_a1b2c3": {"AAPL": {"current_price": 200}},   # not an analyst
        "portfolio_manager_a1b2c3": {"AAPL": {"action": "buy"}},           # not an analyst
    }
    n = ingest_run(signals, end_date="2026-05-26", run_id="run1", root=root)
    assert n == 2                                  # only the two analysts
    wiki = WikiMemory(root)
    ctx = wiki.query_ticker("AAPL")
    assert set(ctx.latest_by_analyst) == {"Warren Buffett", "Michael Burry"}
    # read_back reflects the just-ingested run
    assert "consensus" in read_back(["AAPL"], root=root)


def test_ingest_run_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_MEMORY_ENABLED", "0")
    n = ingest_run({"warren_buffett_agent": {"AAPL": {"signal": "bullish", "confidence": 1}}},
                   root=str(tmp_path / "wiki"))
    assert n == 0


def test_normalize_analyst_name():
    assert normalize_analyst_name("warren_buffett_agent") == "Warren Buffett"
    assert normalize_analyst_name("michael_burry_a1b2c3") == "Michael Burry"
    assert normalize_analyst_name("news_sentiment_agent") == "News Sentiment"


def test_ingest_run_never_raises_on_garbage(tmp_path):
    # malformed payloads must not raise
    assert ingest_run({"x_agent": {"AAPL": "not-a-dict"}}, root=str(tmp_path / "w")) == 0
    assert ingest_run({"x_agent": "not-a-dict"}, root=str(tmp_path / "w")) == 0
