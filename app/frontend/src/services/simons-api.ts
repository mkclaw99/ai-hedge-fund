// Client for /simons/refresh and /simons/tick — mirrors forecaster-api.ts.
// Runs the Jim Simons analyst only (no other agents, no PM, no LLM); used
// by the node body's "Refresh now" button so the user can re-run Simons
// without firing the whole flow.

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

export interface SimonsRefreshRequest {
  tickers: string[];
  flow_id?: number;
  end_date?: string;
  simons_cadence?: 'off' | '1min' | '5min' | '15min' | 'hourly';
  simons_bar_frequency?: 'day' | 'hour' | '5min' | '1min';
  simons_lookback_bars?: number;
}

// Shape matches StrategyConfig (snake_case) so the Strategy node can render
// it directly without remapping. Numbers come through as numbers; nulls
// for stop_loss_pct / take_profit_pct mean "Simons doesn't use chart stops".
export interface SimonsRecommendedStrategy {
  style?: string;
  sizing_rule?: string;
  max_position_pct?: number;
  max_sector_pct?: number;
  holding_period?: string;
  stop_loss_pct?: number | null;
  take_profit_pct?: number | null;
  allow_stocks?: boolean;
  allow_options?: boolean;
  allow_etfs?: boolean;
  note?: string;
  min_decision_interval_minutes?: number;
  price_move_threshold_pct?: number;
  max_signal_age_hours?: number;
}

export interface SimonsSignal {
  signal: 'bullish' | 'bearish' | 'neutral';
  confidence: number;
  reasoning: string;
  simons?: {
    z_score: number;
    realized_vol_pct: number;
    rs_vs_benchmark_sigma: number;
    lookback_bars: number;
    frequency: string;
    threshold: number;
  };
}

export interface SimonsRefreshResponse {
  signals: Record<string, SimonsSignal>;
  recommended_strategy: SimonsRecommendedStrategy;
  end_date: string;
  error?: string;
}

export async function refreshSimons(body: SimonsRefreshRequest): Promise<SimonsRefreshResponse> {
  const res = await fetch(`${API_BASE_URL}/simons/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`Simons refresh failed: ${res.status} ${res.statusText}`);
  }
  return res.json();
}

export interface SimonsTickResponse {
  ok: boolean;
  reason?: string;
  results?: Array<{
    node_id?: string;
    ok: boolean;
    reason?: string;
    signals?: number;
    tickers?: string[];
  }>;
}

export async function fireSimonsTick(flowId: number): Promise<SimonsTickResponse> {
  const res = await fetch(`${API_BASE_URL}/simons/tick/${flowId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) {
    throw new Error(`Simons tick failed: ${res.status} ${res.statusText}`);
  }
  return res.json();
}
