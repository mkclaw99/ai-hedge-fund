// Read-only client for the Track Record endpoint — same data the PM sees in
// its prompt, but as JSON instead of Markdown.

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

export interface TrackRecordRow {
  analyst: string;
  ticker: string;
  date: string;
  signal: string;
  confidence: number;
  reasoning_short: string;
  direction: number;        // +1 / -1 / 0
  entry_price: number | null;
  exit_price: number | null;
  return_pct: number | null;
  outcome: 'WIN' | 'LOSS' | 'OPEN' | 'SKIP';
}

export interface AnalystRollup {
  wins: number;
  losses: number;
  n: number;
  hit_rate: number;             // raw % over closed
  hit_rate_weighted: number;    // 60-day-half-life-weighted %
  avg_win: number;              // avg WIN return %
  avg_loss: number;             // avg LOSS return %
}

export interface AnalystTickerCell extends AnalystRollup {
  analyst: string;
  ticker: string;
}

export interface TrackRecordSummary {
  overall: {
    wins: number;
    losses: number;
    open: number;
    hit_rate: number;
    hit_rate_weighted: number;
  };
  analysts: Record<string, AnalystRollup>;
  analyst_tickers: AnalystTickerCell[];
  recent: TrackRecordRow[];
}

export interface TrackRecordResponse {
  flow_id: number | null;
  holding_days: number;
  summary: TrackRecordSummary;
}

export async function getTrackRecord(
  flowId: number,
  holdingPeriod?: 'day' | 'swing' | 'position' | 'long_term',
): Promise<TrackRecordResponse> {
  const params = new URLSearchParams({ flow_id: String(flowId) });
  if (holdingPeriod) params.set('holding_period', holdingPeriod);
  try {
    const r = await fetch(`${API_BASE_URL}/memory/track-record?${params.toString()}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()) as TrackRecordResponse;
  } catch {
    return {
      flow_id: flowId,
      holding_days: 30,
      summary: { overall: { wins: 0, losses: 0, open: 0, hit_rate: 0, hit_rate_weighted: 0 }, analysts: {}, analyst_tickers: [], recent: [] },
    };
  }
}
