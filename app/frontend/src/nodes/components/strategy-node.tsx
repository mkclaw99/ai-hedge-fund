import { type NodeProps } from '@xyflow/react';
import { Target } from 'lucide-react';

import { CardContent } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useNodeState } from '@/hooks/use-node-state';
import { type StrategyNode as StrategyNodeT } from '../types';
import { NodeShell } from './node-shell';

/**
 * Strategy node — declares the rules of the trading game for this flow.
 *
 * Today the Portfolio Manager picks actions based on analyst votes + a hardcoded
 * sizing/risk recipe. That makes it impossible to express *what kind of trader*
 * you are (momentum vs value, swing vs long-term, allow options or stocks-only,
 * etc.) — every flow looks the same underneath. This node makes the strategy
 * first-class: its fields ride on `state.metadata.strategy`, the PM reads them
 * as a `## Strategy Mandate` block before deciding, and the Trading Account
 * code-enforces `maxPositionPct` when placing paper orders.
 *
 * Wiring: any reachable Strategy node from the Fundamental Research entry is
 * picked up — `handlePlay` scans for it. One per flow is enough; if more are
 * wired the first one found wins.
 */

const STYLE_OPTIONS = [
  { value: 'value', label: 'Value' },
  { value: 'growth', label: 'Growth' },
  { value: 'momentum', label: 'Momentum' },
  { value: 'mean_reversion', label: 'Mean-Reversion' },
  { value: 'event_driven', label: 'Event-Driven' },
  { value: 'income', label: 'Income / Yield' },
] as const;

const SIZING_OPTIONS = [
  { value: 'equal_weight', label: 'Equal weight' },
  { value: 'conviction_weighted', label: 'Conviction-weighted' },
  { value: 'risk_parity', label: 'Risk-parity (inverse vol)' },
  { value: 'fixed_dollar', label: 'Fixed dollars / position' },
] as const;

const HOLDING_OPTIONS = [
  { value: 'day', label: 'Day trade' },
  { value: 'swing', label: 'Swing (days–weeks)' },
  { value: 'position', label: 'Position (weeks–months)' },
  { value: 'long_term', label: 'Long-term (months–years)' },
] as const;

const SELECT_CLS =
  'nodrag h-9 w-full rounded-md border border-border bg-node px-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring';
const NUM_CLS =
  'nodrag h-9 w-20 rounded-md border border-border bg-node px-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring';

export function StrategyNode({ data, selected, id, isConnectable }: NodeProps<StrategyNodeT>) {
  const [style, setStyle] = useNodeState<string>(id, 'style', 'value');
  const [sizingRule, setSizingRule] = useNodeState<string>(id, 'sizingRule', 'conviction_weighted');
  const [maxPositionPct, setMaxPositionPct] = useNodeState<string>(id, 'maxPositionPct', '15');
  const [maxSectorPct, setMaxSectorPct] = useNodeState<string>(id, 'maxSectorPct', '40');
  const [holdingPeriod, setHoldingPeriod] = useNodeState<string>(id, 'holdingPeriod', 'position');
  const [stopLossPct, setStopLossPct] = useNodeState<string>(id, 'stopLossPct', '');
  const [takeProfitPct, setTakeProfitPct] = useNodeState<string>(id, 'takeProfitPct', '');
  const [allowStocks, setAllowStocks] = useNodeState<boolean>(id, 'allowStocks', true);
  const [allowOptions, setAllowOptions] = useNodeState<boolean>(id, 'allowOptions', false);
  const [allowEtfs, setAllowEtfs] = useNodeState<boolean>(id, 'allowEtfs', false);
  const [note, setNote] = useNodeState<string>(id, 'note', '');

  const ToggleRow = ({
    on, onChange, label, tooltip, badge,
  }: { on: boolean; onChange: (v: boolean) => void; label: string; tooltip: string; badge?: string }) => (
    <Tooltip delayDuration={200}>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={() => onChange(!on)}
          className="nodrag flex items-center justify-between gap-2 rounded-md border border-border bg-node px-2 py-1.5 text-xs hover:bg-accent"
          aria-pressed={on}
          aria-label={label}
        >
          <span className="font-medium">{label}</span>
          <span className="flex items-center gap-1.5">
            {badge && <span className="text-[10px] uppercase tracking-wide text-muted-foreground">{badge}</span>}
            <span
              className={
                'inline-block h-4 w-7 rounded-full transition-colors ' +
                (on ? 'bg-emerald-500' : 'bg-muted')
              }
            >
              <span
                className={
                  'block h-3 w-3 rounded-full bg-white transition-transform mt-0.5 ' +
                  (on ? 'translate-x-3.5 ml-0.5' : 'translate-x-0.5')
                }
              />
            </span>
          </span>
        </button>
      </TooltipTrigger>
      <TooltipContent side="right" className="max-w-xs">{tooltip}</TooltipContent>
    </Tooltip>
  );

  return (
    <TooltipProvider>
      <NodeShell
        id={id}
        selected={selected}
        isConnectable={isConnectable}
        icon={<Target className="h-5 w-5" />}
        iconColor="text-blue-500"
        name={data.name || 'Strategy'}
        description={data.description}
        width="w-96"
      >
        <CardContent className="p-0">
          <div className="border-t border-border p-3">
            <div className="flex flex-col gap-4">

              {/* Style + sizing rule */}
              <div className="grid grid-cols-2 gap-3">
                <div className="flex flex-col gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary">Style</div>
                    </TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      The trading style the PM should embody — shapes how it weighs analyst signals
                      and what trades it considers (e.g. momentum favours recent winners,
                      mean-reversion the opposite).
                    </TooltipContent>
                  </Tooltip>
                  <select className={SELECT_CLS} value={style} onChange={(e) => setStyle(e.target.value)} aria-label="Trading style">
                    {STYLE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </div>
                <div className="flex flex-col gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary">Sizing rule</div>
                    </TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      How to distribute the budget across decisions. Equal-weight ignores
                      conviction; conviction-weighted scales with confidence; risk-parity
                      shrinks positions in high-vol names; fixed-$ caps every position at the
                      same dollar amount.
                    </TooltipContent>
                  </Tooltip>
                  <select className={SELECT_CLS} value={sizingRule} onChange={(e) => setSizingRule(e.target.value)} aria-label="Sizing rule">
                    {SIZING_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </div>
              </div>

              {/* Position + sector caps */}
              <div className="grid grid-cols-2 gap-3">
                <div className="flex flex-col gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary">Max position %</div>
                    </TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      Hard cap on any single position as a % of portfolio. Enforced by the
                      Trading Account when placing paper orders, and read by the PM.
                    </TooltipContent>
                  </Tooltip>
                  <input
                    type="number" min={1} max={100}
                    aria-label="Max position percent"
                    className={NUM_CLS} value={maxPositionPct}
                    onChange={(e) => setMaxPositionPct(e.target.value.replace(/[^0-9]/g, ''))}
                  />
                </div>
                <div className="flex flex-col gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary">Max sector %</div>
                    </TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      Soft cap on any single sector as a % of portfolio. LLM-honoured for now
                      (no sector taxonomy enforcement yet).
                    </TooltipContent>
                  </Tooltip>
                  <input
                    type="number" min={1} max={100}
                    aria-label="Max sector percent"
                    className={NUM_CLS} value={maxSectorPct}
                    onChange={(e) => setMaxSectorPct(e.target.value.replace(/[^0-9]/g, ''))}
                  />
                </div>
              </div>

              {/* Holding period + stop/target */}
              <div className="grid grid-cols-3 gap-3">
                <div className="flex flex-col gap-1 col-span-3">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary">Holding period</div>
                    </TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      How long you intend to hold positions. Day-trade flips intraday;
                      long-term tolerates drawdowns for compounding. Read by the PM as
                      context for sizing and reversal tolerance.
                    </TooltipContent>
                  </Tooltip>
                  <select className={SELECT_CLS} value={holdingPeriod} onChange={(e) => setHoldingPeriod(e.target.value)} aria-label="Holding period">
                    {HOLDING_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </div>
                <div className="flex flex-col gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary">Stop loss %</div>
                    </TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      Exit threshold below entry. Optional — leave blank to skip.
                    </TooltipContent>
                  </Tooltip>
                  <input
                    type="number" min={0} max={50} placeholder="—"
                    aria-label="Stop loss percent"
                    className={NUM_CLS} value={stopLossPct}
                    onChange={(e) => setStopLossPct(e.target.value.replace(/[^0-9.]/g, ''))}
                  />
                </div>
                <div className="flex flex-col gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary">Take profit %</div>
                    </TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      Exit threshold above entry. Optional — leave blank to skip.
                    </TooltipContent>
                  </Tooltip>
                  <input
                    type="number" min={0} max={500} placeholder="—"
                    aria-label="Take profit percent"
                    className={NUM_CLS} value={takeProfitPct}
                    onChange={(e) => setTakeProfitPct(e.target.value.replace(/[^0-9.]/g, ''))}
                  />
                </div>
              </div>

              {/* Instrument universe — what the PM is allowed to trade */}
              <div className="flex flex-col gap-1">
                <div className="text-subtitle text-primary">Instruments</div>
                <div className="grid grid-cols-1 gap-2">
                  <ToggleRow
                    on={allowStocks}
                    onChange={setAllowStocks}
                    label="Common stock"
                    tooltip="Trade the underlying common shares. The default."
                  />
                  <ToggleRow
                    on={allowOptions}
                    onChange={setAllowOptions}
                    label="Options"
                    badge="LIVE DATA"
                    tooltip="Pull each ticker's options chain (via Alpaca) and let the PM consider option strategies — covered calls, protective puts, spreads. Data flows into the PM prompt; order placement for options is a follow-up (paper stock orders only for now)."
                  />
                  <ToggleRow
                    on={allowEtfs}
                    onChange={setAllowEtfs}
                    label="Related ETFs"
                    badge="HINT ONLY"
                    tooltip="Tell the PM it may substitute or hedge via sector/factor ETFs. No ETF discovery API yet — the PM names ETFs from training, you decide whether to execute."
                  />
                </div>
              </div>

              {/* Free-text strategy note */}
              <div className="flex flex-col gap-1">
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild>
                    <div className="text-subtitle text-primary">Strategy note</div>
                  </TooltipTrigger>
                  <TooltipContent side="right" className="max-w-xs">
                    Free text the PM reads verbatim as part of its mandate. Use it for
                    nuance the dropdowns can't capture (entry rules, what to avoid, when to
                    raise cash, etc.).
                  </TooltipContent>
                </Tooltip>
                <textarea
                  className="nodrag flex min-h-[64px] w-full rounded-md border border-border bg-node px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  placeholder={'e.g. "Long-only growth on cheap-drone names; willing to ride 20% drawdowns; trim half at +50%, all at +100%; no overnight options exposure."'}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              </div>

              <div className="text-xs text-muted-foreground">
                Wire: Fundamental Companies → Strategy → Portfolio Manager.
              </div>
            </div>
          </div>
        </CardContent>
      </NodeShell>
    </TooltipProvider>
  );
}
