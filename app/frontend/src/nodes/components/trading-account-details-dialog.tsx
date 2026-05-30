import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { formatTicker, useTickerNames } from '@/lib/ticker-names';
import {
  getPaperAccount,
  getPaperOrders,
  getPaperPositions,
  getPortfolioHistory,
  PaperAccount,
  PaperOrder,
  PaperPosition,
  PortfolioHistory,
  PortfolioHistoryPeriod,
} from '@/services/trading-api';
import { Loader2, RotateCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { EquityChart } from './equity-chart';

const usd = (n?: number) =>
  typeof n === 'number'
    ? n.toLocaleString(undefined, { style: 'currency', currency: 'USD' })
    : '—';

const pct = (n?: number) =>
  typeof n === 'number'
    ? `${n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}%`
    : '—';

const orderStatusClass = (status?: string) => {
  const s = (status || '').toLowerCase();
  if (s === 'filled') return 'text-emerald-500';
  if (s === 'partially_filled' || s === 'new' || s === 'accepted' || s === 'pending_new') return 'text-yellow-500';
  if (s === 'canceled' || s === 'cancelled' || s === 'rejected' || s === 'expired') return 'text-red-500';
  return 'text-muted-foreground';
};

interface Props {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}

export function TradingAccountDetailsDialog({ isOpen, onOpenChange }: Props) {
  const [account, setAccount] = useState<PaperAccount | null>(null);
  const [positions, setPositions] = useState<PaperPosition[]>([]);
  const [orders, setOrders] = useState<PaperOrder[]>([]);
  const [loading, setLoading] = useState(false);
  // Equity-over-time state — separate so changing the period doesn't
  // re-fetch positions/orders. Period defaults to 1M, a reasonable
  // "where am I lately" view that covers most paper-trading sessions.
  const [history, setHistory] = useState<PortfolioHistory | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [period, setPeriod] = useState<PortfolioHistoryPeriod>('1M');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [a, p, o] = await Promise.all([
        getPaperAccount(),
        getPaperPositions(),
        getPaperOrders('all', 50),
      ]);
      setAccount(a);
      // Biggest position first (by absolute market value).
      setPositions([...p].sort((x, y) => Math.abs(y.market_value) - Math.abs(x.market_value)));
      setOrders(o); // backend returns newest-first
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshHistory = useCallback(async (p: PortfolioHistoryPeriod) => {
    setHistoryLoading(true);
    try {
      setHistory(await getPortfolioHistory(p));
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen) refresh();
  }, [isOpen, refresh]);

  // Fetch history when the dialog opens or the period changes — keyed
  // on `isOpen && period` so we don't refetch on every render.
  useEffect(() => {
    if (isOpen) refreshHistory(period);
  }, [isOpen, period, refreshHistory]);

  // Ticker labels: "Coherent Corp (COHR)" across the dialog.
  const allSymbols = Array.from(
    new Set([
      ...positions.map((p) => p.symbol).filter(Boolean),
      ...orders.map((o) => o.symbol).filter(Boolean),
    ]),
  );
  const tickerNames = useTickerNames(allSymbols);

  // Performance summary.
  const dayPnl =
    typeof account?.equity === 'number' && typeof account?.last_equity === 'number'
      ? account.equity - account.last_equity
      : undefined;
  const dayPnlPct =
    typeof dayPnl === 'number' && account?.last_equity
      ? dayPnl / account.last_equity
      : undefined;
  const unrealizedPnl = positions.reduce((s, p) => s + (p.unrealized_pl || 0), 0);
  const longValue = positions.filter((p) => p.qty > 0).reduce((s, p) => s + (p.market_value || 0), 0);
  const shortValue = positions.filter((p) => p.qty < 0).reduce((s, p) => s + (p.market_value || 0), 0);

  const stat = (label: string, value: string, tone: 'normal' | 'pos' | 'neg' | 'muted' = 'normal') => (
    <div>
      <div
        className={cn(
          'text-2xl font-semibold tabular-nums',
          tone === 'pos' && 'text-emerald-500',
          tone === 'neg' && 'text-red-500',
          tone === 'muted' && 'text-muted-foreground',
          tone === 'normal' && 'text-primary',
        )}
      >
        {value}
      </div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );

  const ts = (s?: string) => (s ? new Date(s).toLocaleString() : '—');

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="flex flex-col w-[95vw] max-w-[1300px] h-[90vh]">
        <DialogHeader>
          <div className="flex items-center justify-between gap-4">
            <DialogTitle className="text-xl flex items-center gap-3">
              Trading Account — Details
              <span className="inline-flex items-center rounded-md border border-yellow-500/40 bg-yellow-500/10 px-2 py-0.5 text-xs font-semibold text-yellow-500">
                PAPER
              </span>
            </DialogTitle>
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
              {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5 mr-1.5" />}
              {loading ? 'Loading' : 'Refresh'}
            </Button>
          </div>
        </DialogHeader>

        <div className="flex-1 min-h-0 overflow-y-auto pr-1 pt-2 space-y-6">
          {/* Account summary */}
          {!account?.connected ? (
            <div className="rounded-md border border-border bg-node/60 p-3 text-sm text-muted-foreground">
              {account?.reason || 'Not connected to Alpaca Paper.'}
            </div>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4 rounded-lg border border-border p-4">
              {stat('Equity', usd(account.equity))}
              {stat('Cash', usd(account.cash))}
              {stat('Buying power', usd(account.buying_power))}
              {stat(
                'Day P&L',
                typeof dayPnl === 'number' ? `${dayPnl >= 0 ? '+' : ''}${usd(Math.abs(dayPnl))} (${pct(dayPnlPct)})` : '—',
                typeof dayPnl === 'number' ? (dayPnl >= 0 ? 'pos' : 'neg') : 'muted',
              )}
              {stat(
                'Unrealized P&L',
                `${unrealizedPnl >= 0 ? '+' : ''}${usd(Math.abs(unrealizedPnl))}`,
                unrealizedPnl === 0 ? 'muted' : unrealizedPnl > 0 ? 'pos' : 'neg',
              )}
              {stat('Status', (account.status || '—').toLowerCase())}
            </div>
          )}

          {/* Performance over time — Alpaca's portfolio/history series, shown
              as an inline-SVG equity curve. Period buttons change `period`
              state which triggers a re-fetch via the dedicated effect. The
              max-drawdown stat is computed client-side from the same samples
              so the user can eyeball "worst loss from a peak" without the
              backend computing it on every request. */}
          {account?.connected && (() => {
            const samples = history?.samples ?? [];
            const startEq = samples[0]?.equity;
            const endEq = samples[samples.length - 1]?.equity;
            const baseEq = history?.base_value || startEq || 0;
            const change = endEq != null && startEq != null ? endEq - startEq : undefined;
            const changePct = change != null && startEq ? change / startEq : undefined;
            // Max drawdown over the visible window — peak-to-trough as a
            // negative percentage; matches how almost every broker shows it.
            let peak = startEq ?? 0;
            let maxDd = 0;
            for (const s of samples) {
              if (s.equity > peak) peak = s.equity;
              const dd = peak > 0 ? (s.equity - peak) / peak : 0;
              if (dd < maxDd) maxDd = dd;
            }
            const tone: 'pos' | 'neg' | 'muted' =
              change == null ? 'muted' : change >= 0 ? 'pos' : 'neg';
            return (
              <section>
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-base font-semibold text-primary flex items-center gap-2">
                    Performance over time
                    {historyLoading && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
                  </h3>
                  {/* Period selector — same options Alpaca supports.
                      Tooltip explains each button so the user doesn't have
                      to guess (matches the "self-explanatory UI" rule). */}
                  <div className="flex items-center gap-1">
                    {(['1D', '5D', '1M', '3M', '1A', 'all'] as PortfolioHistoryPeriod[]).map((p) => (
                      <Button
                        key={p}
                        variant={period === p ? 'default' : 'outline'}
                        size="sm"
                        className="h-7 px-2 text-xs"
                        title={
                          p === '1D' ? 'Today (hourly)' :
                          p === '5D' ? 'Last 5 days (hourly)' :
                          p === '1M' ? 'Last 1 month (daily)' :
                          p === '3M' ? 'Last 3 months (daily)' :
                          p === '1A' ? 'Last year (daily)' :
                          'All time (daily)'
                        }
                        onClick={() => setPeriod(p)}
                        disabled={historyLoading}
                      >
                        {p === 'all' ? 'All' : p}
                      </Button>
                    ))}
                  </div>
                </div>

                {/* Summary tiles: start / end / change / max drawdown */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 rounded-lg border border-border p-3 mb-3">
                  {stat('Start', usd(startEq), 'muted')}
                  {stat('Current', usd(endEq), 'normal')}
                  {stat(
                    'Change',
                    change != null && changePct != null
                      ? `${change >= 0 ? '+' : ''}${usd(Math.abs(change))} (${pct(changePct)})`
                      : '—',
                    tone,
                  )}
                  {stat(
                    'Max drawdown',
                    samples.length > 1 ? pct(maxDd) : '—',
                    maxDd < 0 ? 'neg' : 'muted',
                  )}
                </div>

                {/* The chart itself */}
                <div className="rounded-md border border-border p-2 bg-node/40">
                  <EquityChart samples={samples} baseValue={baseEq} />
                </div>
              </section>
            );
          })()}

          {/* Positions */}
          <section>
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-base font-semibold text-primary">
                Positions <span className="text-muted-foreground text-sm font-normal">({positions.length})</span>
              </h3>
              <div className="text-xs text-muted-foreground tabular-nums">
                Long {usd(longValue)} {shortValue !== 0 && <>· Short {usd(Math.abs(shortValue))}</>}
              </div>
            </div>
            {positions.length === 0 ? (
              <div className="rounded-md border border-border p-6 text-center text-sm text-muted-foreground">
                No open positions.
              </div>
            ) : (
              <div className="rounded-md border border-border overflow-hidden">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Symbol</TableHead>
                      <TableHead className="text-right">Qty</TableHead>
                      <TableHead className="text-right">Avg entry</TableHead>
                      <TableHead className="text-right">Current</TableHead>
                      <TableHead className="text-right">Market value</TableHead>
                      <TableHead className="text-right">Unrealized P&L</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {positions.map((p) => (
                      <TableRow key={p.symbol}>
                        <TableCell className="font-medium text-primary">{formatTicker(p.symbol, tickerNames)}</TableCell>
                        <TableCell className={cn('text-right tabular-nums', p.qty < 0 && 'text-yellow-500')}>{p.qty}</TableCell>
                        <TableCell className="text-right tabular-nums">{usd(p.avg_entry_price)}</TableCell>
                        <TableCell className="text-right tabular-nums">{usd(p.current_price)}</TableCell>
                        <TableCell className="text-right tabular-nums">{usd(p.market_value)}</TableCell>
                        <TableCell
                          className={cn(
                            'text-right tabular-nums',
                            p.unrealized_pl > 0 && 'text-emerald-500',
                            p.unrealized_pl < 0 && 'text-red-500',
                          )}
                        >
                          {p.unrealized_pl >= 0 ? '+' : ''}{usd(p.unrealized_pl)}{' '}
                          <span className="text-xs text-muted-foreground">({pct(p.unrealized_plpc)})</span>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </section>

          {/* Recent orders */}
          <section>
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-base font-semibold text-primary">
                Recent orders <span className="text-muted-foreground text-sm font-normal">({orders.length})</span>
              </h3>
            </div>
            {orders.length === 0 ? (
              <div className="rounded-md border border-border p-6 text-center text-sm text-muted-foreground">
                No orders yet.
              </div>
            ) : (
              <div className="rounded-md border border-border overflow-hidden">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Symbol</TableHead>
                      <TableHead>Side</TableHead>
                      <TableHead className="text-right">Qty</TableHead>
                      <TableHead className="text-right">Filled</TableHead>
                      <TableHead className="text-right">Fill price</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Submitted</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {orders.map((o) => (
                      <TableRow key={o.id}>
                        <TableCell className="font-medium text-primary">{formatTicker(o.symbol, tickerNames)}</TableCell>
                        <TableCell className={cn('uppercase font-medium', o.side === 'buy' ? 'text-emerald-500' : 'text-yellow-500')}>{o.side}</TableCell>
                        <TableCell className="text-right tabular-nums">{o.qty}</TableCell>
                        <TableCell className="text-right tabular-nums">{o.filled_qty || '—'}</TableCell>
                        <TableCell className="text-right tabular-nums">{o.filled_avg_price ? usd(o.filled_avg_price) : '—'}</TableCell>
                        <TableCell className={cn('lowercase', orderStatusClass(o.status))}>{o.status || '—'}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">{ts(o.submitted_at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}
