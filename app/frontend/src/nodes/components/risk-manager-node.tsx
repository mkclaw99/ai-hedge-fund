import { type NodeProps } from '@xyflow/react';
import { Shield } from 'lucide-react';

import { CardContent } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useNodeState } from '@/hooks/use-node-state';
import { type RiskManagerNode as RiskManagerNodeT } from '../types';
import { NodeShell } from './node-shell';

/**
 * Risk Manager node — exposes the previously-hidden volatility/correlation limits.
 *
 * Previously the Risk Manager was auto-spawned for every Portfolio Manager with
 * hardcoded parameters: you couldn't tune the position cap multiplier, turn off
 * the correlation penalty, or disable risk filtering entirely. This makes it
 * configurable.
 *
 * Back-compat: when no Risk Manager node is on the canvas, the backend auto-
 * spawns one with the *current* defaults (multiplier 1.0, correlation penalty
 * on, never disabled), so existing flows behave exactly as before.
 */
export function RiskManagerNode({ data, selected, id, isConnectable }: NodeProps<RiskManagerNodeT>) {
  const [limitMultiplier, setLimitMultiplier] = useNodeState<string>(id, 'limitMultiplier', '1.0');
  const [disableCorrelationPenalty, setDisableCorrelationPenalty] = useNodeState<boolean>(id, 'disableCorrelationPenalty', false);
  const [disabled, setDisabled] = useNodeState<boolean>(id, 'disabled', false);

  const ToggleRow = ({
    on, onChange, label, tooltip,
  }: { on: boolean; onChange: (v: boolean) => void; label: string; tooltip: string }) => (
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
        icon={<Shield className="h-5 w-5" />}
        iconColor="text-purple-500"
        name={data.name || 'Risk Manager'}
        description={data.description}
        hasLeftHandle={false}
        hasRightHandle={false}
        width="w-80"
      >
        <CardContent className="p-0">
          <div className="border-t border-border p-3">
            <div className="flex flex-col gap-4">

              {/* Multiplier */}
              <div className="flex flex-col gap-1">
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild>
                    <div className="text-subtitle text-primary">Position cap multiplier</div>
                  </TooltipTrigger>
                  <TooltipContent side="right" className="max-w-xs">
                    Coefficient applied to the volatility×correlation-adjusted position
                    limit. <strong>1.0</strong> is the historical default. <strong>0.5</strong>
                    is conservative (every position cap halved). <strong>1.5</strong>+ is
                    aggressive (looser caps; rely on Strategy's hard cap instead).
                  </TooltipContent>
                </Tooltip>
                <input
                  type="number" min={0.1} max={5} step={0.1}
                  aria-label="Position cap multiplier"
                  className="nodrag h-9 w-24 rounded-md border border-border bg-node px-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  value={limitMultiplier}
                  onChange={(e) => setLimitMultiplier(e.target.value.replace(/[^0-9.]/g, ''))}
                  disabled={disabled}
                />
              </div>

              {/* Toggles */}
              <div className="flex flex-col gap-2">
                <ToggleRow
                  on={!disableCorrelationPenalty}
                  onChange={(v) => setDisableCorrelationPenalty(!v)}
                  label="Correlation penalty"
                  tooltip="When on, tickers highly correlated with existing positions get smaller caps (avoids concentrated bets on the same factor). When off, correlation is ignored and the cap is pure-volatility."
                />
                <ToggleRow
                  on={!disabled}
                  onChange={(v) => setDisabled(!v)}
                  label="Risk filtering enabled"
                  tooltip="When off, the Risk Manager passes through ${cash} for every ticker — no volatility-based limit. Strategy's Max position % is then the only cap. Use when you want Strategy alone to drive sizing."
                />
              </div>

              <div className="text-xs text-muted-foreground">
                Auto-spawned even without this node on the canvas — drop one in only to
                tune the defaults.
              </div>
            </div>
          </div>
        </CardContent>
      </NodeShell>
    </TooltipProvider>
  );
}
