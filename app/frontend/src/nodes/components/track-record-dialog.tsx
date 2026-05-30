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
  AnalystRollup,
  AnalystTickerCell,
  TrackRecordResponse,
  TrackRecordRow,
  getTrackRecord,
} from '@/services/track-record-api';
import { Loader2, RotateCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

// One-stop "how is the PM doing?" view. Same data the PM sees in its prompt
// (PR #65), but as a structured dialog so the user can audit the learning loop
// without reading the agent's reasoning prose. Closes the visibility gap
// between *the PM learns from its history* and *the user can see it learn*.

interface Props {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  flowId: number | null;
  holdingPeriod?: 'day' | 'swing' | 'position' | 'long_term';
}

const outcomeClass = (o: TrackRecordRow['outcome']) => {
  if (o === 'WIN') return 'text-emerald-500';
  if (o === 'LOSS') return 'text-red-500';
  if (o === 'OPEN') return 'text-yellow-500';
  return 'text-muted-foreground';
};

const pct = (n: number | null | undefined, sign = false) => {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  const v = Math.abs(n) < 0.005 ? 0 : n;
  return `${sign && v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
};

const usd = (n?: number | null) =>
  typeof n === 'number' ? `$${n.toFixed(2)}` : '—';

// Slightly green/red tinted backgrounds for hit-rate cells so the eye finds
// patterns fast: where are the strong winners, where are the underperformers?
const rateClass = (rate: number) => {
  if (rate >= 70) return 'text-emerald-500 font-semibold';
  if (rate >= 55) return 'text-emerald-400';
  if (rate <= 30) return 'text-red-500 font-semibold';
  if (rate <= 45) return 'text-red-400';
  return 'text-muted-foreground';
};

export function TrackRecordDialog({ isOpen, onOpenChange, flowId, holdingPeriod = 'position' }: Props) {
  const [data, setData] = useState<TrackRecordResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (flowId == null) return;
    setLoading(true);
    try {
      const r = await getTrackRecord(flowId, holdingPeriod);
      setData(r);
    } finally {
      setLoading(false);
    }
  }, [flowId, holdingPeriod]);

  useEffect(() => {
    if (isOpen) refresh();
  }, [isOpen, refresh]);

  const summary = data?.summary;
  const recent = summary?.recent ?? [];
  const symbols = Array.from(new Set(recent.map((r) => r.ticker)));
  const tickerNames = useTickerNames(symbols);

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

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="flex flex-col w-[95vw] max-w-[1300px] h-[90vh]">
        <DialogHeader>
          <div className="flex items-center justify-between gap-4">
            <DialogTitle className="text-xl flex items-center gap-3">
              Track Record
              <span className="inline-flex items-center rounded-md border border-border bg-muted px-2 py-0.5 text-xs font-normal text-muted-foreground">
                horizon: {data?.holding_days ?? '—'}d
              </span>
            </DialogTitle>
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
              {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5 mr-1.5" />}
              {loading ? 'Loading' : 'Refresh'}
            </Button>
          </div>
        </DialogHeader>

        <div className="flex-1 min-h-0 overflow-y-auto pr-1 pt-2 space-y-6">
          {!summary || (summary.overall.wins + summary.overall.losses + summary.overall.open) === 0 ? (
            <div className="rounded-md border border-border bg-node/60 p-4 text-sm text-muted-foreground">
              No track record yet — run the flow at least once so decisions land in the wiki, then come back here once
              they've had time to mature (positions: 30 days, swing: 7).
            </div>
          ) : (
            <>
              {/* Overall stats */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 rounded-lg border border-border p-4">
                {stat('WINs', String(summary.overall.wins), summary.overall.wins ? 'pos' : 'muted')}
                {stat('LOSSes', String(summary.overall.losses), summary.overall.losses ? 'neg' : 'muted')}
                {stat('OPEN', String(summary.overall.open), 'muted')}
                {stat(
                  'Hit rate (weighted)',
                  summary.overall.wins + summary.overall.losses
                    ? `${summary.overall.hit_rate_weighted.toFixed(1)}%`
                    : '—',
                  summary.overall.hit_rate_weighted >= 50 ? 'pos' : 'neg',
                )}
              </div>

              {/* Mandatory Adjustments — auto-rules the PM is shown.
                  This is what the PM is being told to do *imperatively* —
                  the hit-rate tables below are the data that derived them. */}
              {summary.rules && summary.rules.length > 0 && (
                <section>
                  <h3 className="text-base font-semibold text-primary mb-2 flex items-center gap-2">
                    Mandatory Adjustments
                    <span className="text-muted-foreground text-sm font-normal">
                      (auto-derived rules the PM must follow)
                    </span>
                  </h3>
                  <div className="rounded-md border border-border bg-node/40 p-3 space-y-2">
                    {summary.rules.map((r, i) => {
                      // Colour the chip by rule kind: lone-winner is the most
                      // actionable contrarian signal, down/up weight are direct
                      // adjustments to a single (analyst, ticker) cell.
                      const chipClass =
                        r.kind === 'lone_winner'
                          ? 'bg-amber-500/15 text-amber-500 border-amber-500/30'
                          : r.kind === 'up_weight'
                            ? 'bg-emerald-500/15 text-emerald-500 border-emerald-500/30'
                            : 'bg-red-500/15 text-red-500 border-red-500/30';
                      const label =
                        r.kind === 'lone_winner' ? 'LONE WINNER' : r.kind === 'up_weight' ? 'TRUST' : 'DOWN-WEIGHT';
                      return (
                        <div key={i} className="flex items-start gap-3 text-sm">
                          <span
                            className={cn(
                              'inline-flex items-center rounded-md border px-2 py-0.5 text-[10px] font-semibold tracking-wide whitespace-nowrap mt-0.5',
                              chipClass,
                            )}
                            title={`${r.n} closed calls · ${r.hit_rate.toFixed(1)}% weighted`}
                          >
                            {label}
                          </span>
                          <span className="text-primary leading-snug">{r.text}</span>
                        </div>
                      );
                    })}
                  </div>
                </section>
              )}

              {/* Per-analyst */}
              {Object.keys(summary.analysts).length > 0 && (
                <section>
                  <h3 className="text-base font-semibold text-primary mb-2">Per-analyst</h3>
                  <div className="rounded-md border border-border overflow-hidden">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Analyst</TableHead>
                          <TableHead className="text-right">W / L</TableHead>
                          <TableHead className="text-right">Raw</TableHead>
                          <TableHead className="text-right">Weighted</TableHead>
                          <TableHead className="text-right">Avg win</TableHead>
                          <TableHead className="text-right">Avg loss</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {Object.entries(summary.analysts)
                          .sort((a, b) => b[1].hit_rate_weighted - a[1].hit_rate_weighted)
                          .map(([analyst, c]) => (
                            <TableRow key={analyst}>
                              <TableCell className="font-medium text-primary">{analyst}</TableCell>
                              <TableCell className="text-right tabular-nums">
                                {c.wins} / {c.losses}
                              </TableCell>
                              <TableCell className="text-right tabular-nums text-muted-foreground">
                                {c.hit_rate.toFixed(1)}%
                              </TableCell>
                              <TableCell className={cn('text-right tabular-nums', rateClass(c.hit_rate_weighted))}>
                                {c.hit_rate_weighted.toFixed(1)}%
                              </TableCell>
                              <TableCell className="text-right tabular-nums text-emerald-500">
                                {pct(c.avg_win, true)}
                              </TableCell>
                              <TableCell className="text-right tabular-nums text-red-500">{pct(c.avg_loss)}</TableCell>
                            </TableRow>
                          ))}
                      </TableBody>
                    </Table>
                  </div>
                </section>
              )}

              {/* Per-(analyst, ticker) — the actionable pattern signal */}
              {summary.analyst_tickers.length > 0 && (
                <section>
                  <h3 className="text-base font-semibold text-primary mb-2">
                    Per-(analyst, ticker)
                    <span className="text-muted-foreground text-sm font-normal ml-2">
                      (≥ 2 closed calls — sorted by how lopsided)
                    </span>
                  </h3>
                  <div className="rounded-md border border-border overflow-hidden">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Analyst</TableHead>
                          <TableHead>Ticker</TableHead>
                          <TableHead className="text-right">W / L</TableHead>
                          <TableHead className="text-right">Weighted hit rate</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {summary.analyst_tickers.map((c: AnalystTickerCell, i) => (
                          <TableRow key={`${c.analyst}-${c.ticker}-${i}`}>
                            <TableCell className="font-medium text-primary">{c.analyst}</TableCell>
                            <TableCell className="text-primary">{formatTicker(c.ticker, tickerNames)}</TableCell>
                            <TableCell className="text-right tabular-nums">
                              {c.wins} / {c.losses}
                            </TableCell>
                            <TableCell className={cn('text-right tabular-nums', rateClass(c.hit_rate_weighted))}>
                              {c.hit_rate_weighted.toFixed(1)}%
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </section>
              )}

              {/* Recent decisions */}
              <section>
                <h3 className="text-base font-semibold text-primary mb-2">
                  Recent decisions
                  <span className="text-muted-foreground text-sm font-normal ml-2">({recent.length})</span>
                </h3>
                <div className="rounded-md border border-border overflow-hidden">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Date</TableHead>
                        <TableHead>Ticker</TableHead>
                        <TableHead>Analyst</TableHead>
                        <TableHead>Signal</TableHead>
                        <TableHead className="text-right">Conf</TableHead>
                        <TableHead className="text-right">Entry</TableHead>
                        <TableHead className="text-right">Exit</TableHead>
                        <TableHead className="text-right">Return</TableHead>
                        <TableHead>Outcome</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {recent.map((r, i) => (
                        <TableRow key={`${r.date}-${r.ticker}-${r.analyst}-${i}`}>
                          <TableCell className="text-xs text-muted-foreground">{r.date}</TableCell>
                          <TableCell className="text-primary">{formatTicker(r.ticker, tickerNames)}</TableCell>
                          <TableCell>{r.analyst}</TableCell>
                          <TableCell
                            className={cn(
                              'lowercase',
                              r.signal === 'bullish' && 'text-emerald-500',
                              r.signal === 'bearish' && 'text-red-500',
                            )}
                          >
                            {r.signal}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">{r.confidence}%</TableCell>
                          <TableCell className="text-right tabular-nums">{usd(r.entry_price)}</TableCell>
                          <TableCell className="text-right tabular-nums">{usd(r.exit_price)}</TableCell>
                          <TableCell className="text-right tabular-nums">{pct(r.return_pct, true)}</TableCell>
                          <TableCell className={cn('font-medium', outcomeClass(r.outcome))}>{r.outcome}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </section>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
