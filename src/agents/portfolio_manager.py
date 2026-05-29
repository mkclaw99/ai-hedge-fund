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


class PortfolioDecision(BaseModel):
    action: Literal["buy", "sell", "short", "cover", "hold"]
    quantity: int = Field(description="Number of shares to trade")
    confidence: int = Field(description="Confidence 0-100")
    reasoning: str = Field(description="Reasoning for the decision")


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


def _compact_signals(signals_by_ticker: dict[str, dict]) -> dict[str, dict]:
    """Keep only {agent: {sig, conf}} and drop empty agents."""
    out = {}
    for t, agents in signals_by_ticker.items():
        if not agents:
            out[t] = {}
            continue
        compact = {}
        for agent, payload in agents.items():
            sig = payload.get("sig") or payload.get("signal")
            conf = payload.get("conf") if "conf" in payload else payload.get("confidence")
            if sig is not None and conf is not None:
                compact[agent] = {"sig": sig, "conf": conf}
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

    # Build compact payloads only for tickers sent to LLM
    compact_signals = _compact_signals({t: signals_by_ticker.get(t, {}) for t in tickers_for_llm})
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
    strategy = (state.get("metadata", {}) or {}).get("strategy")
    strategy_block = _render_strategy_block(strategy)

    # Derivatives — when the Strategy node enables options, fetch a compact
    # per-ticker options summary from Alpaca and inject it. Fail-open: any
    # data problem renders a one-line "no derivatives" note.
    derivatives_block = _render_derivatives_block(strategy, tickers_for_llm, state)

    # Minimal prompt template — now with Strategy + Derivatives slots.
    template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a portfolio manager.\n"
                "Inputs per ticker: analyst signals and allowed actions with max qty (already validated).\n"
                "You may also receive prior accumulated research from earlier runs as background — "
                "weigh it, but the current signals take precedence.\n"
                "If a `## Strategy Mandate` block is present, follow it: it sets your trading style, "
                "sizing rule, caps, holding period, and which instruments you may consider. "
                "If a `## Derivatives` block is present (options enabled), you may reason about "
                "option-based positions, but **only place stock orders for now** (option order routing "
                "is not yet wired). Use derivative info as context — e.g. high IV suggests selling vol.\n"
                "Pick one allowed action per ticker and a quantity ≤ the max. "
                "Keep reasoning very concise (max 100 chars). No cash or margin math. Return JSON only."
            ),
            (
                "human",
                "Signals:\n{signals}\n\n"
                "Allowed:\n{allowed}\n\n"
                "Prior research:\n{prior}\n\n"
                "{strategy}{derivatives}"
                "Format:\n"
                "{{\n"
                '  "decisions": {{\n'
                '    "TICKER": {{"action":"...","quantity":int,"confidence":int,"reasoning":"..."}}\n'
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
