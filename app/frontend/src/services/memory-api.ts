// Client for the read-only flow-memory endpoint that backs the Memory node.
const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

export interface MemoryAnalystRow {
  analyst: string;
  signal: string;
  confidence: number;
  date: string;
  reasoning: string;
}

export interface MemoryTicker {
  ticker: string;
  consensus: string;
  bullish: string[];
  bearish: string[];
  neutral: string[];
  n_runs: number;
  n_insights: number;
  analysts: MemoryAnalystRow[];
  pm_decisions: MemoryAnalystRow[];
}

export interface FlowMemory {
  flow_id: number | null;
  tickers: MemoryTicker[];
}

/**
 * Fetch the accumulated research memory for a flow.
 * @param flowId  the flow whose memory to read (null → shared "default" namespace)
 * @param tickers optional comma-separated filter; omit to get everything on record
 */
export async function getFlowMemory(
  flowId: number | null,
  tickers?: string,
): Promise<FlowMemory> {
  const params = new URLSearchParams();
  if (flowId != null) params.set('flow_id', String(flowId));
  if (tickers) params.set('tickers', tickers);

  const res = await fetch(`${API_BASE_URL}/memory?${params.toString()}`);
  if (!res.ok) {
    throw new Error(`Failed to load flow memory: ${res.status}`);
  }
  return res.json();
}
