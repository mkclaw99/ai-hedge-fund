import json
import time
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate

from src.graph.state import AgentState, show_agent_reasoning
from pydantic import BaseModel, Field
from typing_extensions import Literal
from src.utils.progress import progress
from src.utils.llm import call_llm
from src.memory import flow_root, read_back
from src.memory.ingest import normalize_analyst_name


class PortfolioDecision(BaseModel):
    # Stock actions: buy/sell/short/cover/hold operate on the underlying ticker.
    # Option actions (only used when the Strategy node has options enabled):
    #   buy_call  — buy-to-open a long call    (cost: ask × 100 × qty)
    #   buy_put   — buy-to-open a long put     (cost: ask × 100 × qty)
    #   sell_call — sell-to-open a short call  (premium: bid × 100 × qty)
    #   sell_put  — sell-to-open a short put   (premium: bid × 100 × qty)
    # For option actions, `quantity` is the number of contracts (each = 100 shares
    # of underlying exposure) and `option_contract` is the OCC symbol picked from
    # the ## Derivatives block in the prompt.
    action: Literal["buy", "sell", "short", "cover", "hold", "buy_call", "buy_put", "sell_call", "sell_put"]
    quantity: int = Field(description="Number of shares (stock) or contracts (options) to trade")
    confidence: int = Field(description="Confidence 0-100")
    reasoning: str = Field(description="Reasoning for the decision")
    option_contract: str | None = Field(
        default=None,
        description="OCC contract symbol picked from the ## Derivatives block. Required for option actions, ignored for stock actions.",
    )


class PortfolioManagerOutput(BaseModel):
    decisions: dict[str, PortfolioDecision] = Field(description="Dictionary of ticker to trading decisions")


##### Portfolio Management Agent #####
def portfolio_management_agent(state: AgentState, agent_id: str = "portfolio_manager"):
    """Makes final trading decisions and generates orders for multiple tickers"""

    portfolio = state["data"]["portfolio"]
    analyst_signals = state["data"]["analyst_signals"]
    tickers = state["data"]["tickers"]

    position_limits = {}
    current_prices = {}
    max_shares = {}
    signals_by_ticker = {}
    for ticker in tickers:
        progress.update_status(agent_id, ticker, "Processing analyst signals")

        # Find the corresponding risk manager for this portfolio manager
        if agent_id.startswith("portfolio_manager_"):
            suffix = agent_id.split('_')[-1]
            risk_manager_id = f"risk_management_agent_{suffix}"
        else:
            risk_manager_id = "risk_management_agent"  # Fallback for CLI

        risk_data = analyst_signals.get(risk_manager_id, {}).get(ticker, {})
        position_limits[ticker] = risk_data.get("remaining_position_limit", 0.0)
        current_prices[ticker] = float(risk_data.get("current_price", 0.0))

        # Calculate maximum shares allowed based on position limit and price
        if current_prices[ticker] > 0:
            max_shares[ticker] = int(position_limits[ticker] // current_prices[ticker])
        else:
            max_shares[ticker] = 0

        # Compress analyst signals to {sig, conf}
        ticker_signals = {}
        for agent, signals in analyst_signals.items():
            if not agent.startswith("risk_management_agent") and ticker in signals:
                sig = signals[ticker].get("signal")
                conf = signals[ticker].get("confidence")
                if sig is not None and conf is not None:
                    ticker_signals[agent] = {"sig": sig, "conf": conf}
        signals_by_ticker[ticker] = ticker_signals

    state["data"]["current_prices"] = current_prices

    progress.update_status(agent_id, None, "Generating trading decisions")

    result = generate_trading_decision(
        tickers=tickers,
        signals_by_ticker=signals_by_ticker,
        current_prices=current_prices,
        max_shares=max_shares,
        portfolio=portfolio,
        agent_id=agent_id,
        state=state,
    )
    message = HumanMessage(
        content=json.dumps({ticker: decision.model_dump() for ticker, decision in result.decisions.items()}),
        name=agent_id,
    )

    if state["metadata"]["show_reasoning"]:
        show_agent_reasoning({ticker: decision.model_dump() for ticker, decision in result.decisions.items()},
                             "Portfolio Manager")

    progress.update_status(agent_id, None, "Done")

    return {
        "messages": state["messages"] + [message],
        "data": state["data"],
    }


def compute_allowed_actions(
        tickers: list[str],
        current_prices: dict[str, float],
        max_shares: dict[str, int],
        portfolio: dict[str, float],
) -> dict[str, dict[str, int]]:
    """Compute allowed actions and max quantities for each ticker deterministically."""
    allowed = {}
    cash = float(portfolio.get("cash", 0.0))
    positions = portfolio.get("positions", {}) or {}
    margin_requirement = float(portfolio.get("margin_requirement", 0.5))
    margin_used = float(portfolio.get("margin_used", 0.0))
    equity = float(portfolio.get("equity", cash))

    for ticker in tickers:
        price = float(current_prices.get(ticker, 0.0))
        pos = positions.get(
            ticker,
            {"long": 0, "long_cost_basis": 0.0, "short": 0, "short_cost_basis": 0.0},
        )
        long_shares = int(pos.get("long", 0) or 0)
        short_shares = int(pos.get("short", 0) or 0)
        max_qty = int(max_shares.get(ticker, 0) or 0)

        # Start with zeros
        actions = {"buy": 0, "sell": 0, "short": 0, "cover": 0, "hold": 0}

        # Long side
        if long_shares > 0:
            actions["sell"] = long_shares
        if cash > 0 and price > 0:
            max_buy_cash = int(cash // price)
            max_buy = max(0, min(max_qty, max_buy_cash))
            if max_buy > 0:
                actions["buy"] = max_buy

        # Short side
        if short_shares > 0:
            actions["cover"] = short_shares
        if price > 0 and max_qty > 0:
            if margin_requirement <= 0.0:
                # If margin requirement is zero or unset, only cap by max_qty
                max_short = max_qty
            else:
                available_margin = max(0.0, (equity / margin_requirement) - margin_used)
                max_short_margin = int(available_margin // price)
                max_short = max(0, min(max_qty, max_short_margin))
            if max_short > 0:
                actions["short"] = max_short

        # Hold always valid
        actions["hold"] = 0

        # Prune zero-capacity actions to reduce tokens, keep hold
        pruned = {"hold": 0}
        for k, v in actions.items():
            if k != "hold" and v > 0:
                pruned[k] = v

        allowed[ticker] = pruned

    return allowed


def _compact_signals(
    signals_by_ticker: dict[str, dict],
    *,
    calibrations: dict | None = None,
) -> dict[str, dict]:
    """Keep only ``{agent: {sig, conf, [cal_conf, hit_rate, n]}}`` and drop empty agents.

    When ``calibrations`` is provided (a ``{(analyst_name, ticker): (rate, n)}``
    dict from :func:`src.memory.track_record.confidence_calibrations`), each
    signal is enriched with the *effective* confidence after applying the
    historical hit rate as a Bayesian-smoothed multiplier — and the original
    raw confidence stays in ``conf`` so the PM can see both. Signals on
    (analyst, ticker) pairs without enough data simply have no calibration
    fields; the PM falls back to the raw number as usual.

    Why both fields rather than overwriting: the analyst memo the PM also
    reads is keyed on the analyst's own self-reported confidence; if we
    secretly mutated the number, the memo's "I'm 85% confident" would no
    longer match the JSON's ``conf=20``, which is confusing both for the
    LLM and for a human reading the prompt during debugging. Two fields,
    one truth.
    """
    out: dict[str, dict] = {}
    for t, agents in signals_by_ticker.items():
        if not agents:
            out[t] = {}
            continue
        compact: dict[str, dict] = {}
        for agent, payload in agents.items():
            sig = payload.get("sig") or payload.get("signal")
            conf = payload.get("conf") if "conf" in payload else payload.get("confidence")
            if sig is None or conf is None:
                continue
            entry: dict = {"sig": sig, "conf": int(conf)}
            if calibrations is not None:
                analyst_name = normalize_analyst_name(agent)
                pair = calibrations.get((analyst_name, t))
                if pair is not None:
                    rate, n = pair
                    cal_conf = max(0, min(100, int(round(int(conf) * rate))))
                    if cal_conf != int(conf):
                        entry["cal_conf"] = cal_conf
                        entry["hist_rate"] = round(rate * 100, 1)
                        entry["hist_n"] = n
            compact[agent] = entry
        out[t] = compact
    return out


def generate_trading_decision(
        tickers: list[str],
        signals_by_ticker: dict[str, dict],
        current_prices: dict[str, float],
        max_shares: dict[str, int],
        portfolio: dict[str, float],
        agent_id: str,
        state: AgentState,
) -> PortfolioManagerOutput:
    """Get decisions from the LLM with deterministic constraints and a minimal prompt."""

    # Deterministic constraints
    allowed_actions_full = compute_allowed_actions(tickers, current_prices, max_shares, portfolio)

    # Pre-fill pure holds to avoid sending them to the LLM at all
    prefilled_decisions: dict[str, PortfolioDecision] = {}
    tickers_for_llm: list[str] = []
    for t in tickers:
        aa = allowed_actions_full.get(t, {"hold": 0})
        # If only 'hold' key exists, there is no trade possible
        if set(aa.keys()) == {"hold"}:
            prefilled_decisions[t] = PortfolioDecision(
                action="hold", quantity=0, confidence=100.0, reasoning="No valid trade available"
            )
        else:
            tickers_for_llm.append(t)

    if not tickers_for_llm:
        return PortfolioManagerOutput(decisions=prefilled_decisions)

    # ------------------------------------------------------------------
    # Closing the track-record → PM learning loop.
    #
    # We compute the per-(analyst, ticker) outcomes ONCE up here and reuse
    # them three ways:
    #   (1) confidence calibration — rewrite each analyst's confidence
    #       in the signals JSON the PM consumes (raw conf stays alongside).
    #   (2) Mandatory Adjustments block — imperative rules ("Down-weight
    #       X on Y", "SIDE WITH lone winner Z") rendered into the prompt.
    #   (3) Track Record block — the existing W/L/OPEN rollup the PM
    #       already reads for general calibration.
    # All three share the same outcomes list to keep the picture consistent
    # — there is exactly one source of truth for "what's happened so far."
    # ------------------------------------------------------------------
    strategy = (state.get("metadata", {}) or {}).get("strategy")
    outcomes = _compute_outcomes_for_pm(strategy, tickers_for_llm, state)

    # Confidence calibrations are O(outcomes) to build once and O(1) per
    # lookup during signal compaction.
    from src.memory.track_record import confidence_calibrations as _confidence_calibrations
    calibrations = _confidence_calibrations(outcomes) if outcomes else {}

    # Build compact payloads only for tickers sent to LLM
    compact_signals = _compact_signals(
        {t: signals_by_ticker.get(t, {}) for t in tickers_for_llm},
        calibrations=calibrations,
    )
    compact_allowed = {t: allowed_actions_full[t] for t in tickers_for_llm}

    # Read-back: the full cross-analyst history for this flow, including the PM's
    # own past decisions (fail-open, may be empty). Unlike analyst nodes — which
    # read only their own prior calls — the PM reads everything, because synthesis
    # is its job. This is the flywheel: the decision compounds on the flow's wiki.
    # Unsaved flow ⇒ no flow_slug ⇒ no scoped wiki to read from. Skip rather than
    # falling through to the global default, which would pull in other flows.
    flow_slug = (state.get("metadata", {}) or {}).get("flow_slug")
    root = flow_root(flow_slug)
    prior = read_back(tickers_for_llm, root=root) if root else ""

    # Strategy node config — declares trading rules (style, sizing, caps, etc.).
    # When wired, the PM reads it as part of its mandate. When absent, an
    # empty block is rendered and the PM uses default behaviour (back-compat).
    strategy_block = _render_strategy_block(strategy)

    # Derivatives — when the Strategy node enables options, fetch a compact
    # per-ticker options summary from Alpaca and inject it. Fail-open: any
    # data problem renders a one-line "no derivatives" note.
    derivatives_block = _render_derivatives_block(strategy, tickers_for_llm, state)

    # Mandatory Adjustments — auto-generated imperative rules derived from
    # (analyst, ticker) cells where the historical pattern is unambiguous
    # (≥ 3 closed calls, ≥ 70% one way). These go ABOVE the Track Record
    # block in the prompt so the PM reads the actionable instructions
    # before the raw stats.
    from src.memory.track_record import derive_pm_rules, render_pm_rules_block, render_track_record_block
    pm_rules = derive_pm_rules(outcomes) if outcomes else []
    rules_block = render_pm_rules_block(pm_rules)

    # Stash the rules per ticker on state so the post-graph wiki-ingest path
    # can surface them on each PM insight's frontmatter. Tagged short text
    # ("[lone_winner] Technical on COHR (n=4, 100%)") so per-day audit is
    # greppable — no LLM emission, deterministic from the same outcomes the
    # rules block was rendered from.
    rules_for_state: dict[str, list[str]] = {}
    for r in pm_rules:
        tag = (
            f"[{r['kind']}] {r['analyst']} on {r['ticker']} "
            f"(n={r['n']}, {r['hit_rate']}%)"
        )
        rules_for_state.setdefault(r["ticker"].upper(), []).append(tag)
    if rules_for_state:
        state.setdefault("data", {})["pm_rules_applied"] = rules_for_state

    # Track record — the broader W/L/OPEN rollup. Same outcomes list as the
    # rules block, so the rules and the stats agree by construction.
    track_record_block = render_track_record_block(outcomes) if outcomes else ""
    if track_record_block:
        track_record_block = track_record_block + "\n"

    # Add option actions to every ticker's allowed set when Strategy permits
    # options. Each contract = 100 shares of underlying exposure; cap qty at a
    # conservative 5 contracts per opening trade (PM is free to ask for fewer).
    # We don't pre-filter for "optionable" here — the PM picks an OCC from the
    # ## Derivatives block, and a non-optionable ticker simply has no symbols
    # there to pick, so the PM won't emit an option action for it.
    options_enabled = bool(isinstance(strategy, dict) and strategy.get("allow_options"))
    if options_enabled:
        for t in tickers_for_llm:
            allowed_actions_full.setdefault(t, {"hold": 0})
            for opt_action in ("buy_call", "buy_put", "sell_call", "sell_put"):
                allowed_actions_full[t][opt_action] = 5

    # Refresh the compact view after option enrichment.
    compact_allowed = {t: allowed_actions_full[t] for t in tickers_for_llm if t in allowed_actions_full}

    # Minimal prompt template — Strategy + Derivatives + Rules + Track Record slots.
    template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a portfolio manager.\n"
                "Inputs per ticker: analyst signals and allowed actions with max qty (already validated).\n"
                "Signal fields:\n"
                "  • `sig`    — the analyst's directional call (bullish / bearish / neutral).\n"
                "  • `conf`   — the analyst's self-reported confidence (0-100).\n"
                "  • `cal_conf` (when present) — the same confidence AFTER reweighting by the "
                "analyst's HISTORICAL hit rate on this ticker. Treat `cal_conf` as the operative "
                "value when present; the unweighted `conf` is shown for context only.\n"
                "  • `hist_rate` / `hist_n` — the hit rate and sample size behind `cal_conf`.\n"
                "You may also receive prior accumulated research from earlier runs as background — "
                "weigh it, but the current signals take precedence.\n"
                "If a `## Strategy Mandate` block is present, follow it: it sets your trading style, "
                "sizing rule, caps, holding period, and which instruments you may consider.\n"
                "If a `## Mandatory Adjustments Based on Past Performance` block is present, those "
                "rules were derived MECHANICALLY from the (analyst, ticker) cells where the "
                "historical pattern is unambiguous. Follow them. They override your usual instinct "
                "to weight every analyst equally.\n"
                "If a `## Track Record` block is present, it shows how your past calls actually "
                "played out (closed WIN/LOSS + still-OPEN positions, per-analyst hit rates). "
                "Use it to calibrate further. When today's signals contradict a strong historical "
                "pattern, weigh the pattern.\n"
                "If a `## Derivatives` block is present (options enabled), you may also place OPTION "
                "orders on the underlying tickers:\n"
                "  • `buy_call` / `buy_put` — buy-to-open (cost ≈ ask × 100 × qty).\n"
                "  • `sell_call` / `sell_put` — sell-to-open (collect bid × 100 × qty).\n"
                "For option actions, `quantity` is **contracts** (each = 100 shares of underlying) "
                "and `option_contract` MUST be set to one of the OCC symbols shown in the Derivatives "
                "block for that ticker (e.g. \"AVAV260529C00205000\"). Do not invent OCC symbols — "
                "pick from what's shown. For stock actions, leave `option_contract` null.\n"
                "Pick one allowed action per ticker and a quantity ≤ the max. "
                "Keep reasoning very concise (max 100 chars). No cash or margin math. Return JSON only."
            ),
            (
                "human",
                "Signals:\n{signals}\n\n"
                "Allowed:\n{allowed}\n\n"
                "Prior research:\n{prior}\n\n"
                "{strategy}{derivatives}{rules}{track_record}"
                "Format:\n"
                "{{\n"
                '  "decisions": {{\n'
                '    "TICKER": {{"action":"...","quantity":int,"confidence":int,"reasoning":"...","option_contract":null}}\n'
                "  }}\n"
                "}}"
            ),
        ]
    )

    prompt_data = {
        "signals": json.dumps(compact_signals, separators=(",", ":"), ensure_ascii=False),
        "allowed": json.dumps(compact_allowed, separators=(",", ":"), ensure_ascii=False),
        "prior": prior or "(none on record)",
        "strategy": strategy_block,
        "derivatives": derivatives_block,
        "rules": rules_block + ("\n" if rules_block else ""),
        "track_record": track_record_block,
    }
    prompt = template.invoke(prompt_data)

    # Default factory fills remaining tickers as hold if the LLM fails
    def create_default_portfolio_output():
        # start from prefilled
        decisions = dict(prefilled_decisions)
        for t in tickers_for_llm:
            decisions[t] = PortfolioDecision(
                action="hold", quantity=0, confidence=0.0, reasoning="Default decision: hold"
            )
        return PortfolioManagerOutput(decisions=decisions)

    llm_out = call_llm(
        prompt=prompt,
        pydantic_model=PortfolioManagerOutput,
        agent_name=agent_id,
        state=state,
        default_factory=create_default_portfolio_output,
    )

    # Merge prefilled holds with LLM results
    merged = dict(prefilled_decisions)
    merged.update(llm_out.decisions)
    return PortfolioManagerOutput(decisions=merged)


def _render_strategy_block(strategy):
    """Format a StrategyConfig dict into a Markdown `## Strategy Mandate` block for
    the PM prompt. Empty string when no Strategy node was wired."""
    if not isinstance(strategy, dict):
        return ""
    bits: list[str] = ["## Strategy Mandate", ""]
    style = strategy.get("style")
    sizing = strategy.get("sizing_rule")
    if style or sizing:
        line = []
        if style:
            line.append(f"**Style:** {style.replace('_', ' ')}")
        if sizing:
            line.append(f"**Sizing:** {sizing.replace('_', ' ')}")
        bits.append("  ·  ".join(line))
    if strategy.get("max_position_pct") is not None:
        bits.append(f"**Max position size:** {strategy['max_position_pct']}% of portfolio (enforced by Trading Account)")
    if strategy.get("max_sector_pct") is not None:
        bits.append(f"**Max sector concentration:** {strategy['max_sector_pct']}% of portfolio (honour this in your decisions)")
    if strategy.get("holding_period"):
        bits.append(f"**Holding period:** {strategy['holding_period'].replace('_', ' ')}")
    if strategy.get("stop_loss_pct") is not None:
        bits.append(f"**Stop loss:** {strategy['stop_loss_pct']}% below entry (annotate sells when triggered)")
    if strategy.get("take_profit_pct") is not None:
        bits.append(f"**Take profit:** {strategy['take_profit_pct']}% above entry (annotate sells when triggered)")
    allowed_inst = []
    if strategy.get("allow_stocks", True):
        allowed_inst.append("stocks")
    if strategy.get("allow_options"):
        allowed_inst.append("options (data injected below; order placement TBD)")
    if strategy.get("allow_etfs"):
        allowed_inst.append("related ETFs (hint only — no discovery API)")
    if allowed_inst:
        bits.append(f"**Allowed instruments:** {', '.join(allowed_inst)}")
    note = (strategy.get("note") or "").strip()
    if note:
        bits.append("")
        bits.append("**Strategy note (verbatim):**")
        bits.append(f"> {note}")
    bits.append("")
    return "\n".join(bits)


def _render_derivatives_block(strategy, tickers, state):
    """When Strategy.allow_options is on, fetch per-ticker options summaries from
    Alpaca and render a `## Derivatives` block for the PM. Fail-open."""
    if not isinstance(strategy, dict) or not strategy.get("allow_options"):
        return ""
    try:
        from app.backend.services.derivatives import get_options_summary, summarize_for_prompt
    except Exception:
        return ""
    api_keys = None
    try:
        request = (state.get("metadata", {}) or {}).get("request")
        if request and hasattr(request, "api_keys"):
            api_keys = request.api_keys
    except Exception:
        api_keys = None
    lines = ["## Derivatives (options) — chain summary per ticker", ""]
    for t in tickers:
        try:
            summary = get_options_summary(t, api_keys)
            lines.append(summarize_for_prompt(summary, t))
        except Exception:
            lines.append(f"- **{t}**: options summary unavailable")
    lines.append("")
    return "\n".join(lines)


def _compute_outcomes_for_pm(strategy, tickers, state):
    """Compute per-(analyst, ticker) historical outcomes for the PM.

    Single source of truth, shared by:
      * the confidence-calibration lookup (rewrites signals JSON),
      * the Mandatory Adjustments rule block (imperative directives), and
      * the Track Record stats block (per-analyst rollup).

    Returns ``[]`` when there is no flow-scoped wiki, no tickers, or any
    error along the way. Fail-open: the PM degrades to "no track record"
    rather than crashing.

    Walk-forward correctness: passes ``state["data"]["end_date"]`` as
    ``today`` to ``compute_outcomes`` so during a backtest the PM at
    day N never sees outcomes derived from prices or insights dated
    after N. Without this the model would have lookahead access to the
    rest of the backtest range and any apparent "learning" would be the
    bias, not the loop. In live runs ``end_date`` is today and behaviour
    is unchanged.
    """
    try:
        from datetime import date as _date_cls

        from src.memory import flow_root
        from src.memory.track_record import (
            compute_outcomes,
            holding_days_from_strategy,
        )

        flow_slug = (state.get("metadata", {}) or {}).get("flow_slug")
        root = flow_root(flow_slug)
        if not root or not tickers:
            return []
        api_keys = None
        request = (state.get("metadata", {}) or {}).get("request")
        if request and hasattr(request, "api_keys"):
            api_keys = request.api_keys

        # Read backtest-current-date from state.data.end_date. Live runs set
        # this to today, backtests set it per simulated day.
        today_d = None
        as_of = (state.get("data", {}) or {}).get("end_date")
        if as_of:
            try:
                today_d = _date_cls.fromisoformat(str(as_of)[:10])
            except Exception:
                today_d = None

        return compute_outcomes(
            tickers, root,
            api_keys=api_keys,
            today=today_d,
            holding_days=holding_days_from_strategy(strategy),
        )
    except Exception:
        return []
