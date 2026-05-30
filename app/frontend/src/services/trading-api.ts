// Client for the read-only Alpaca PAPER trading endpoints (backs the Trading Account node).
const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

export interface PaperAccount {
  connected: boolean;
  paper?: boolean;
  reason?: string;
  account_number?: string;
  currency?: string;
  cash?: number;
  equity?: number;
  last_equity?: number;
  buying_power?: number;
  portfolio_value?: number;
  long_market_value?: number;
  short_market_value?: number;
  status?: string;
  pattern_day_trader?: boolean;
}

export interface PaperOrder {
  id: string;
  symbol: string;
  side: string;
  qty: number;
  filled_qty: number;
  filled_avg_price: number;
  status: string;
  type: string;
  submitted_at: string;
  filled_at?: string;
}

export interface PaperPosition {
  symbol: string;
  qty: number;
  avg_entry_price: number;
  market_value: number;
  unrealized_pl: number;
  unrealized_plpc: number;
  current_price: number;
}

export async function getPaperAccount(): Promise<PaperAccount> {
  try {
    const r = await fetch(`${API_BASE_URL}/trading/paper/account`);
    if (!r.ok) return { connected: false, paper: true, reason: `HTTP ${r.status}` };
    return (await r.json()) as PaperAccount;
  } catch (e: any) {
    return { connected: false, paper: true, reason: e?.message || 'fetch failed' };
  }
}

export async function getPaperOrders(status: string = 'all', limit: number = 50): Promise<PaperOrder[]> {
  try {
    const r = await fetch(`${API_BASE_URL}/trading/paper/orders?status=${encodeURIComponent(status)}&limit=${limit}`);
    if (!r.ok) return [];
    const data = await r.json();
    return (data?.orders ?? []) as PaperOrder[];
  } catch { return []; }
}

export async function getPaperPositions(): Promise<PaperPosition[]> {
  try {
    const r = await fetch(`${API_BASE_URL}/trading/paper/positions`);
    if (!r.ok) return [];
    const data = await r.json();
    return (data?.positions ?? []) as PaperPosition[];
  } catch {
    return [];
  }
}

// ── Equity-over-time ─────────────────────────────────────────────────
// Powers the Performance chart in the Trading Account → Details dialog.

export type PortfolioHistoryPeriod = '1D' | '5D' | '1M' | '3M' | '1A' | 'all';

export interface PortfolioHistorySample {
  ts: number;                // unix seconds
  equity: number;
  profit_loss: number;
  profit_loss_pct: number;   // 0.0123 = +1.23%
}

export interface PortfolioHistory {
  connected: boolean;
  paper?: boolean;
  reason?: string;
  period: string;
  timeframe: string;
  base_value?: number;
  samples: PortfolioHistorySample[];
}

export async function getPortfolioHistory(period: PortfolioHistoryPeriod = '1M'): Promise<PortfolioHistory> {
  try {
    const r = await fetch(
      `${API_BASE_URL}/trading/paper/portfolio-history?period=${encodeURIComponent(period)}`,
    );
    if (!r.ok) return { connected: false, paper: true, reason: `HTTP ${r.status}`, period, timeframe: '1D', samples: [] };
    return (await r.json()) as PortfolioHistory;
  } catch (e: any) {
    return { connected: false, paper: true, reason: e?.message || 'fetch failed', period, timeframe: '1D', samples: [] };
  }
}
