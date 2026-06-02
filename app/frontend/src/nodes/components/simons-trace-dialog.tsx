// Detailed trace viewer for the Jim Simons analyst's hypothesis-driven loop.
//
// Shows per-ticker:
//   * Context snapshot (the numbers the LLM saw before picking hypotheses)
//   * Proposed hypotheses with rationales
//   * Test results (passed/failed + value vs threshold + one-line detail)
//   * Adjudication (LLM's pick of winning hypothesis + reasoning prose)
//
// Click-through from the node body's inline mini-trace. Mirrors the
// Forecaster's detail-dialog pattern: large modal, tabbed by ticker, dense
// readable layout. Read-only — the trace is auditing/transparency only;
// editing the LLM's output isn't a thing.

import { CheckCircle2, Sigma, XCircle } from 'lucide-react';

import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import type { SimonsSignal, SimonsTrace } from '@/services/simons-api';

interface Props {
  isOpen: boolean;
  onOpenChange: (v: boolean) => void;
  tickers: string[];
  activeTicker: string | null;
  onTickerChange: (t: string) => void;
  signalsByTicker: Record<string, SimonsSignal>;
}

export function SimonsTraceDialog({
  isOpen, onOpenChange, tickers, activeTicker, onTickerChange, signalsByTicker,
}: Props) {
  const sig = activeTicker ? signalsByTicker[activeTicker] : null;
  const trace = sig?.simons_trace;

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent
        className="!max-w-[92vw] w-[92vw] !max-h-[90vh] h-[90vh] bg-node border border-border overflow-y-auto p-4 sm:p-6 flex flex-col gap-3"
        onClick={(e) => e.stopPropagation()}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-primary text-xl">
            <Sigma className="h-5 w-5" />
            <span>Simons trace{activeTicker ? ` · ${activeTicker}` : ''}</span>
          </DialogTitle>
        </DialogHeader>

        {/* Ticker tabs */}
        {tickers.length > 1 && (
          <div className="flex flex-wrap gap-1.5">
            {tickers.map((tk) => {
              const t = signalsByTicker[tk];
              const dot = t?.signal === 'bullish' ? 'bg-emerald-500'
                        : t?.signal === 'bearish' ? 'bg-red-500'
                        : 'bg-gray-500';
              return (
                <button
                  key={tk}
                  onClick={() => onTickerChange(tk)}
                  className={cn(
                    'flex items-center gap-2 px-3 py-1 rounded text-sm font-medium tabular-nums border transition-colors',
                    tk === activeTicker
                      ? 'bg-primary/20 border-primary/40 text-primary'
                      : 'border-border text-muted-foreground hover:bg-node/50',
                  )}
                >
                  <span className={cn('h-2 w-2 rounded-full', dot)} />
                  {tk}
                </button>
              );
            })}
          </div>
        )}

        {!sig ? (
          <div className="p-8 text-sm text-muted-foreground text-center">
            No signal data for this ticker yet. Click <span className="font-medium">Refresh now</span> on the node.
          </div>
        ) : !trace ? (
          // Wiki row exists but no structured trace — pre-hypothesis-loop wiki
          // entry (PR #109 era) OR a fallback signal where the LLM was unreachable.
          <LegacySignalCard signal={sig} />
        ) : (
          <TraceCard signal={sig} trace={trace} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function LegacySignalCard({ signal }: { signal: SimonsSignal }) {
  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-500">
        ⚠ No structured trace for this signal — wiki row is from before the hypothesis-loop rewrite, or
        the LLM was unreachable and Simons fell back to its pure-numpy rule. Refresh to regenerate with
        full trace.
      </div>
      <SignalHeader signal={signal} winning={null} />
      <ReasoningPanel reasoning={signal.reasoning} />
    </div>
  );
}

function TraceCard({ signal, trace }: { signal: SimonsSignal; trace: SimonsTrace }) {
  return (
    <div className="flex-1 min-h-0 flex flex-col gap-4 overflow-y-auto pr-1">
      <SignalHeader signal={signal} winning={trace.adjudication?.winning_hypothesis ?? null} />

      <Section title="Context (what the LLM saw)">
        <ContextGrid ctx={trace.context} frequency={trace.frequency} lookback={trace.lookback_bars} />
      </Section>

      <Section title={`Hypotheses proposed (${trace.hypotheses_proposed.length})`}>
        <ul className="flex flex-col gap-1 text-sm">
          {trace.hypotheses_proposed.map((h, i) => (
            <li key={`${h.name}-${i}`} className="rounded border border-border bg-node/40 px-3 py-2">
              <div className="font-medium text-foreground">{h.name}</div>
              <div className="text-muted-foreground text-xs leading-snug">{h.rationale}</div>
            </li>
          ))}
        </ul>
      </Section>

      <Section title={`Tests run (${trace.tests.length})`}>
        <table className="w-full text-sm tabular-nums">
          <thead className="text-[10px] uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="text-left py-1">Hypothesis</th>
              <th className="text-left py-1">Result</th>
              <th className="text-right py-1">Value</th>
              <th className="text-right py-1">Threshold</th>
              <th className="text-left py-1 pl-3">Detail</th>
            </tr>
          </thead>
          <tbody>
            {trace.tests.map((t, i) => (
              <tr key={`${t.name}-${i}`} className="border-t border-border/40">
                <td className="py-1.5 font-medium">{t.name}</td>
                <td className="py-1.5">
                  {t.passed ? (
                    <span className="inline-flex items-center gap-1 text-emerald-500"><CheckCircle2 className="h-3 w-3" /> PASSED</span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-red-500/80"><XCircle className="h-3 w-3" /> failed</span>
                  )}
                </td>
                <td className="text-right text-foreground">{t.value != null ? String(t.value) : '—'}</td>
                <td className="text-right text-muted-foreground">{t.threshold != null ? String(t.threshold) : '—'}</td>
                <td className="pl-3 text-muted-foreground text-xs">{t.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section
        title={`Adjudication${trace.skipped_llm_adjudicate ? ' (skipped — no hypothesis passed)' : ''}`}
      >
        <div className="rounded border border-border bg-node/40 p-3 text-sm">
          <div className="flex items-center gap-3 text-xs uppercase tracking-wide text-muted-foreground mb-2">
            <span>Signal: <span className="text-foreground">{trace.adjudication.signal}</span></span>
            <span>Confidence: <span className="text-foreground">{trace.adjudication.confidence}%</span></span>
            <span>
              Winning:{' '}
              <span className="text-foreground font-mono">
                {trace.adjudication.winning_hypothesis ?? '—'}
              </span>
            </span>
          </div>
          <div className="text-foreground whitespace-pre-wrap text-sm leading-snug">
            {trace.adjudication.reasoning}
          </div>
        </div>
      </Section>
    </div>
  );
}

function SignalHeader({
  signal, winning,
}: { signal: SimonsSignal; winning: string | null }) {
  const colour = signal.signal === 'bullish' ? 'text-emerald-500'
              : signal.signal === 'bearish' ? 'text-red-500'
              : 'text-muted-foreground';
  return (
    <div className="rounded border border-border bg-node/40 p-3 flex items-center justify-between gap-3">
      <div>
        <div className="text-xs uppercase tracking-wide text-muted-foreground">Signal</div>
        <div className={cn('text-lg font-semibold capitalize', colour)}>{signal.signal}</div>
      </div>
      <div>
        <div className="text-xs uppercase tracking-wide text-muted-foreground">Confidence</div>
        <div className="text-lg font-semibold tabular-nums">{signal.confidence}%</div>
      </div>
      <div>
        <div className="text-xs uppercase tracking-wide text-muted-foreground">Winning hypothesis</div>
        <div className="text-sm font-mono">{winning ?? '—'}</div>
      </div>
    </div>
  );
}

function ReasoningPanel({ reasoning }: { reasoning: string }) {
  return (
    <div className="rounded border border-border bg-node/40 p-3 text-sm whitespace-pre-wrap leading-snug">
      {reasoning}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-subtitle text-primary">{title}</div>
      {children}
    </div>
  );
}

function ContextGrid({
  ctx, frequency, lookback,
}: { ctx: Record<string, any>; frequency: string; lookback: number }) {
  // Render the structured context as small cells. Numbers we know about get
  // formatted; unknown keys are surfaced raw so future context additions
  // don't disappear.
  const known: { key: string; label: string; format: (v: any) => string }[] = [
    { key: 'last_close', label: 'Last close', format: (v) => `$${Number(v).toFixed(2)}` },
    { key: 'z_score_vs_ma', label: 'z vs MA', format: (v) => `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(2)}σ` },
    { key: 'realized_vol_pct_annualised', label: 'Realised vol', format: (v) => `${Number(v).toFixed(1)}%/yr` },
    { key: 'rs_vs_spy_sigma', label: 'RS vs SPY', format: (v) => v == null ? '—' : `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(2)}σ` },
    { key: 'recent_5bar_change_pct', label: '5-bar change', format: (v) => `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(2)}%` },
    { key: 'last_bar_volume_ratio_to_median', label: 'Volume ratio', format: (v) => v == null ? '—' : `${Number(v).toFixed(2)}x` },
  ];
  return (
    <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 text-sm tabular-nums">
      <div className="rounded border border-border p-2 col-span-1">
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Window</div>
        <div className="text-foreground">{lookback} × {frequency}</div>
      </div>
      {known.map((k) => (
        <div key={k.key} className="rounded border border-border p-2">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{k.label}</div>
          <div className="text-foreground">{k.format(ctx?.[k.key])}</div>
        </div>
      ))}
    </div>
  );
}
