"""Turn a rule-based analyst's computed metrics into an extensive Markdown report.

The quantitative analysts (technicals, fundamentals, sentiment, valuation, growth)
don't call an LLM — they compute a signal plus a structured details dict. This helper
runs one LLM pass to write that up as a readable report, so their output reads like
the LLM "persona" analysts'. Fail-open: if the model is unavailable (no key, rate
limit, …) it returns the metrics as a fenced JSON block, so a model problem never
breaks the analyst or the run.
"""

import json
import logging

logger = logging.getLogger(__name__)

_PROMPT = (
    "You are {analyst}, a quantitative equity analyst. Below is your *computed* analysis "
    "for {ticker}: an overall signal of **{signal}** at {confidence}% confidence, plus the "
    "underlying metrics.\n\n{instructions}\n\nExplain what the metrics mean and why they "
    "support the signal. Do NOT change the signal or confidence, and do not invent numbers "
    "beyond what is given.\n\nComputed analysis (JSON):\n{data}"
)


def _dump(data) -> str:
    try:
        return json.dumps(data, indent=2, default=str)
    except Exception:
        return str(data)


def write_analyst_report(agent_id, display_name, ticker, signal, confidence, analysis_data, state) -> str:
    """Return an extensive Markdown report for a computed analysis (fail-open to JSON)."""
    fallback = f"```json\n{_dump(analysis_data)}\n```"
    try:
        from src.llm.models import get_model
        from src.utils.llm import ANALYST_REPORT_INSTRUCTIONS, get_agent_model_config
        from src.utils.token_usage import UsageCallback

        model_name, model_provider = get_agent_model_config(state, agent_id)
        api_keys = None
        request = (state.get("metadata", {}) or {}).get("request")
        if request and hasattr(request, "api_keys"):
            api_keys = request.api_keys

        model = get_model(model_name, model_provider, api_keys)
        if model is None:
            return fallback

        prompt = _PROMPT.format(
            analyst=display_name,
            ticker=ticker,
            signal=signal,
            confidence=confidence,
            instructions=ANALYST_REPORT_INSTRUCTIONS,
            data=_dump(analysis_data),
        )
        result = model.invoke(prompt, config={"callbacks": [UsageCallback(model_provider, model_name)]})
        report = (getattr(result, "content", "") or "").strip()
        return report or fallback
    except Exception as e:  # never break the analyst over a report
        logger.warning("Analyst report generation failed (%s); using raw metrics", e)
        return fallback
