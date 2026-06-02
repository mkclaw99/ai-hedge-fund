// Client for /forecaster/refresh — runs the Time Series Forecaster only,
// without the rest of the analyst bench or the PM. Cheap path that
// gives the user a fresh chart without paying for a whole flow run.

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

export interface ForecasterRefreshRequest {
  tickers: string[];
  flow_id?: number;
  end_date?: string;
  forecaster_context_len?: number;
  forecaster_prediction_len?: number;
  forecaster_bar_frequency?: 'day' | 'hour' | '5min' | '1min';
  // The forecaster node's own id (e.g. `forecaster_abc123` for Chronos,
  // `toto_forecaster_abc123` for Toto). The backend reads the prefix to
  // dispatch to the right backbone. Omitted → legacy Chronos path.
  agent_id?: string;
}

export interface ForecasterRefreshResponse {
  signals: Record<string, {
    signal: 'bullish' | 'bearish' | 'neutral';
    confidence: number;
    // Markdown blob including the ```forecast-data``` JSON fence the
    // ForecasterNode parses for its chart.
    reasoning: string;
  }>;
  end_date: string;
  error?: string;
}

export async function refreshForecaster(body: ForecasterRefreshRequest): Promise<ForecasterRefreshResponse> {
  const res = await fetch(`${API_BASE_URL}/forecaster/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`Forecaster refresh failed: ${res.status} ${res.statusText}`);
  }
  return res.json();
}
