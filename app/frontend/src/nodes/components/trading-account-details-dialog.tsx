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
  PaperAccount,
  PaperOrder,
  PaperPosition,
} from '@/services/trading-api';
import { Loader2, RotateCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

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

  useEffect(() => {
    if (isOpen) refresh();
  }, [isOpen, refresh]);

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
