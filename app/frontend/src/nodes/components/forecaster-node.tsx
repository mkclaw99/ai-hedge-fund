// Custom node for the Time Series Forecaster analyst.
//
// Renders an inline SVG fan chart per ticker on the node body: recent
// history → forecast median with a translucent q10/q90 band. Click a
// ticker chip to switch.
//
// Deliberately has no Model selector. The forecast is produced by
// Amazon Chronos-2 (loaded locally from the HuggingFace cache); there
// is no per-flow LLM choice involved, so showing an LLM picker would
// imply a dependency that doesn't exist.
//
// Data path: the backend agent (src/agents/forecaster.py) appends a
// ```forecast-data``` JSON fence to the per-ticker analysis Markdown
// that already rides the SSE 'analysis' channel. We parse it out of the
// per-ticker messages stored in node-context — no SSE schema change.

import { type NodeProps } from '@xyflow/react';
import { LineChart } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { CardContent } from '@/components/ui/card';
import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { cn } from '@/lib/utils';
import { type AgentNode } from '../types';
import { getStatusColor } from '../utils';
import { AgentOutputDialog } from './agent-output-dialog';
import { NodeShell } from './node-shell';

interface ForecastPayload {
  history: number[];
  q10: number[];
  q50: number[];
  q90: number[];
  horizon_days: number;
}

// Pulls the latest `forecast-data` JSON fence out of an analysis Markdown
// blob. Returns null if absent or unparseable — the chart then falls back
// to its placeholder state.
const FENCE_RE = /```forecast-data\s*\n([\s\S]*?)\n```/;
function extractForecast(md?: string | null): ForecastPayload | null {
  if (!md) return null;
  const m = md.match(FENCE_RE);
  if (!m) return null;
  try {
    const obj = JSON.parse(m[1]);
    if (!Array.isArray(obj.history) || !Array.isArray(obj.q50)) return null;
    return obj as ForecastPayload;
  } catch {
    return null;
  }
}

export function ForecasterNode({
  data,
  selected,
  id,
  isConnectable,
}: NodeProps<AgentNode>) {
  const { currentFlowId } = useFlowContext();
  const { getAgentNodeDataForFlow } = useNodeContext();

  const agentNodeData = getAgentNodeDataForFlow(currentFlowId?.toString() || null);
  const nodeData = agentNodeData[id] || {
    status: 'IDLE',
    ticker: null,
    message: '',
    messages: [],
    lastUpdated: 0,
  };
  const status = nodeData.status;
  const isInProgress = status === 'IN_PROGRESS';
  const [isDialogOpen, setIsDialogOpen] = useState(false);

  // Build a {ticker → latest forecast} map by scanning messages newest-first
  // and taking the first parseable payload per ticker.
  const forecastsByTicker = useMemo<Record<string, ForecastPayload>>(() => {
    const out: Record<string, ForecastPayload> = {};
    const msgs = nodeData.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (!m.ticker) continue;
      if (out[m.ticker]) continue;
      const fc = extractForecast(m.analysis?.[m.ticker]);
      if (fc) out[m.ticker] = fc;
    }
    return out;
  }, [nodeData.messages]);

  const tickers = useMemo(() => Object.keys(forecastsByTicker).sort(), [forecastsByTicker]);
  const [activeTicker, setActiveTicker] = useState<string | null>(null);
  useEffect(() => {
    if (!activeTicker && tickers.length) setActiveTicker(tickers[0]);
    if (activeTicker && tickers.length && !tickers.includes(activeTicker)) setActiveTicker(tickers[0] ?? null);
  }, [tickers, activeTicker]);

  const activeForecast = activeTicker ? forecastsByTicker[activeTicker] : null;

  return (
    <NodeShell
      id={id}
      selected={selected}
      isConnectable={isConnectable}
      icon={<LineChart className="h-5 w-5" />}
      iconColor={getStatusColor(status)}
      name={data.name || 'Time Series Forecaster'}
      description={data.description}
      status={status}
    >
      <CardContent className="p-0">
        <div className="border-t border-border p-3">
          <div className="flex flex-col gap-2">
            <div className="text-subtitle text-primary flex items-center gap-1">Status</div>
            <div className={cn(
              'text-foreground text-xs rounded p-2 border border-status',
              isInProgress ? 'gradient-animation' : getStatusColor(status),
            )}>
              <span className="capitalize">{status.toLowerCase().replace(/_/g, ' ')}</span>
            </div>
            {nodeData.message && (
              <div className="text-foreground text-subtitle">
                {nodeData.message !== 'Done' && nodeData.message}
                {nodeData.ticker && <span className="ml-1">({nodeData.ticker})</span>}
              </div>
            )}

            {/* Forecast chart — the entire reason this node exists. */}
            <div className="text-subtitle text-primary flex items-center justify-between gap-1 mt-1">
              <span>Forecast</span>
              {/* Make the model that *actually* produces the forecast
                  visible right where the user would otherwise expect an
                  LLM picker. This is a fixed-model node by design. */}
              <span
                className="text-[10px] uppercase tracking-wide text-muted-foreground"
                title="Amazon Chronos-2 — 120M-param probabilistic time-series foundation model. Runs locally on cached weights; no API key required."
              >
                Chronos-2
              </span>
            </div>
            {activeForecast ? (
              <>
                <ForecastFanChart forecast={activeForecast} />
                <ForecastSummary forecast={activeForecast} />
                {tickers.length > 1 && (
                  <div className="flex flex-wrap gap-1">
                    {tickers.map((t) => (
                      <button
                        key={t}
                        onClick={() => setActiveTicker(t)}
                        className={cn(
                          'px-1.5 py-0.5 rounded text-[10px] tabular-nums border transition-colors',
                          t === activeTicker
                            ? 'bg-primary/20 border-primary/40 text-primary'
                            : 'border-border text-muted-foreground hover:bg-node/50',
                        )}
                      >
                        {t}
                      </button>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <div className="text-foreground text-xs rounded p-2 border border-border border-dashed text-muted-foreground text-center">
                {status === 'IDLE'
                  ? 'Run the flow to generate a forecast.'
                  : isInProgress
                    ? 'Forecasting…'
                    : 'No forecast available.'}
              </div>
            )}
          </div>
        </div>
        <AgentOutputDialog
          isOpen={isDialogOpen}
          onOpenChange={setIsDialogOpen}
          name={data.name || 'Time Series Forecaster'}
          nodeId={id}
          flowId={currentFlowId?.toString() || null}
        />
      </CardContent>
    </NodeShell>
  );
}

// ---------------------------------------------------------------------------
// Inline SVG fan chart. No chart library — same approach as equity-chart.tsx.
// Renders: history line (gray), q50 forecast line (tone-coloured),
// translucent q10/q90 band, dashed "today" separator between hist and fc.
// ---------------------------------------------------------------------------

function ForecastFanChart({ forecast }: { forecast: ForecastPayload }) {
  const { history, q10, q50, q90 } = forecast;
  const W = 220;
  const H = 90;
  const padX = 4;
  const padY = 6;

  // Y-domain spans every drawn point — history extremes + all three
  // quantile trajectories — padded 4% so lines don't kiss the frame.
  const all = [...history, ...q10, ...q50, ...q90];
  const minV = Math.min(...all);
  const maxV = Math.max(...all);
  const yPad = (maxV - minV) * 0.04 || 1;
  const yMin = minV - yPad;
  const yMax = maxV + yPad;
  const yRange = yMax - yMin || 1;

  const total = history.length + q50.length;
  const xAt = (i: number) => padX + (i / (total - 1)) * (W - 2 * padX);
  const yAt = (v: number) => padY + (1 - (v - yMin) / yRange) * (H - 2 * padY);

  // Connect history to forecast by repeating the last historical point as
  // the first forecast anchor — otherwise there's a visual seam.
  const histPath = history.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i).toFixed(2)} ${yAt(v).toFixed(2)}`).join(' ');
  const fcStart = history.length - 1;
  const lastHist = history[history.length - 1];

  const q50Pts = [lastHist, ...q50];
  const q50Path = q50Pts.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAt(v).toFixed(2)}`).join(' ');

  const q10Pts = [lastHist, ...q10];
  const q90Pts = [lastHist, ...q90];
  const bandTop = q90Pts.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAt(v).toFixed(2)}`).join(' ');
  const bandBot = q10Pts
    .slice()
    .reverse()
    .map((v, i) => `L ${xAt(fcStart + (q10Pts.length - 1 - i)).toFixed(2)} ${yAt(v).toFixed(2)}`)
    .join(' ');
  const bandPath = `${bandTop} ${bandBot} Z`;

  // Tone: green if median ends above last close, red below, neutral grey
  // for a sub-1% move (matches the agent's `_NEUTRAL_PCT` threshold).
  const endQ50 = q50[q50.length - 1];
  const pct = ((endQ50 - lastHist) / lastHist) * 100;
  const tone: 'pos' | 'neg' | 'neu' = Math.abs(pct) < 1 ? 'neu' : pct > 0 ? 'pos' : 'neg';
  const stroke = tone === 'pos' ? '#10b981' : tone === 'neg' ? '#ef4444' : '#94a3b8';
  const fill = tone === 'pos' ? 'rgba(16,185,129,0.16)' : tone === 'neg' ? 'rgba(239,68,68,0.16)' : 'rgba(148,163,184,0.16)';

  const sepX = xAt(fcStart);

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} className="rounded border border-border bg-node/40">
      {/* q10/q90 fan */}
      <path d={bandPath} fill={fill} stroke="none" />
      {/* history line */}
      <path d={histPath} fill="none" stroke="currentColor" strokeWidth={1.2} opacity={0.55} />
      {/* q50 forecast line */}
      <path d={q50Path} fill="none" stroke={stroke} strokeWidth={1.6} />
      {/* today separator */}
      <line
        x1={sepX}
        y1={padY}
        x2={sepX}
        y2={H - padY}
        stroke="currentColor"
        strokeWidth={0.5}
        strokeDasharray="2,2"
        opacity={0.4}
      />
    </svg>
  );
}

function ForecastSummary({ forecast }: { forecast: ForecastPayload }) {
  const last = forecast.history[forecast.history.length - 1];
  const q10 = forecast.q10[forecast.q10.length - 1];
  const q50 = forecast.q50[forecast.q50.length - 1];
  const q90 = forecast.q90[forecast.q90.length - 1];
  const fmt = (n: number) => `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`;
  const pct10 = ((q10 - last) / last) * 100;
  const pct50 = ((q50 - last) / last) * 100;
  const pct90 = ((q90 - last) / last) * 100;
  return (
    <div className="text-[10px] text-muted-foreground tabular-nums leading-tight grid grid-cols-4 gap-1">
      <div>
        <div className="text-[9px] uppercase tracking-wide">Now</div>
        <div className="text-foreground">{last.toFixed(2)}</div>
      </div>
      <div>
        <div className="text-[9px] uppercase tracking-wide">Q10</div>
        <div className="text-red-500/80">{fmt(pct10)}</div>
      </div>
      <div>
        <div className="text-[9px] uppercase tracking-wide">Q50</div>
        <div className={pct50 >= 0 ? 'text-emerald-500' : 'text-red-500'}>{fmt(pct50)}</div>
      </div>
      <div>
        <div className="text-[9px] uppercase tracking-wide">Q90</div>
        <div className="text-emerald-500/80">{fmt(pct90)}</div>
      </div>
    </div>
  );
}
