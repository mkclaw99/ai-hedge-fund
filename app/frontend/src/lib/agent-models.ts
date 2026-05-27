// A per-agent model selection as sent to the backend in `agent_models`.
export interface AgentModelEntry {
  agent_id: string;
  model_name?: string;
  model_provider?: string;
}

/**
 * Pick a flow-wide default model from the per-agent selections.
 *
 * Agents the user didn't explicitly assign a model to are omitted from `agent_models`,
 * and the backend then falls back to its gpt-4.1 / OpenAI default — which errors when
 * no OpenAI key is configured. Passing this as the run's global `model_name` /
 * `model_provider` makes those unset agents inherit a model the user actually chose
 * (e.g. the one set on the Portfolio Manager) instead of OpenAI.
 *
 * Returns the most frequently chosen model, or `{}` if none are configured.
 */
export function primaryAgentModel(
  agentModels: AgentModelEntry[],
): { model_name?: string; model_provider?: string } {
  const counts = new Map<string, { model_name: string; model_provider: string; n: number }>();
  for (const m of agentModels) {
    if (!m.model_name || !m.model_provider) continue;
    const key = `${m.model_provider}::${m.model_name}`;
    const e = counts.get(key) ?? { model_name: m.model_name, model_provider: m.model_provider, n: 0 };
    e.n += 1;
    counts.set(key, e);
  }
  let best: { model_name: string; model_provider: string; n: number } | null = null;
  for (const e of counts.values()) if (!best || e.n > best.n) best = e;
  return best ? { model_name: best.model_name, model_provider: best.model_provider } : {};
}
