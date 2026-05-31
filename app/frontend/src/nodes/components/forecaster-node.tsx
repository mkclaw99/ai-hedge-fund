// Custom node for the Time Series Forecaster analyst.
//
// Inline preview on the node body shows a compact nested-band fan chart;
// clicking it opens ForecastDetailDialog — a larger two-panel view with
// axes, hover tooltip, and a confidence-over-time subplot. The forecast
// is produced by Amazon Chronos-2 (loaded locally from the HuggingFace
// cache); no LLM picker, no API key.
//
// Data path: the backend agent (src/agents/forecaster.py) appends a
// ```forecast-data``` JSON fence to the per-ticker analysis Markdown
// that already rides the SSE 'analysis' channel. We parse it out of the
// per-ticker messages stored in node-context — no SSE schema change.

import { type NodeProps } from '@xyflow/react';
import { LineChart, Maximize2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';

import { CardContent } from '@/components/ui/card';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { cn } from '@/lib/utils';
import { type AgentNode } from '../types';
import { getStatusColor } from '../utils';
import { AgentOutputDialog } from './agent-output-dialog';
import { NodeShell } from './node-shell';

// --- Types and parsing ----------------------------------------------------

interface ForecastPayload {
  history: number[];
  q10: number[];
  q25: number[];          // inner-band lower (50% PI)
  q50: number[];          // median
  q75: number[];          // inner-band upper (50% PI)
  q90: number[];          // outer-band upper (80% PI)
  confidence: number[];   // 0-100 per step (fan-width-derived; see backend)
  horizon_days: number;
}

const FENCE_RE = /```forecast-data\s*\n([\s\S]*?)\n```/;

// Older agent versions emitted only q10/q50/q90 and no confidence array.
// We fall back to the wider band for the inner band and synthesise a flat
// confidence so the dialog renders something legible rather than crashing.
function extractForecast(md?: string | null): ForecastPayload | null {
  if (!md) return null;
  const m = md.match(FENCE_RE);
  if (!m) return null;
  try {
    const obj = JSON.parse(m[1]);
    if (!Array.isArray(obj.history) || !Array.isArray(obj.q50)) return null;
    return {
      history: obj.history,
      q10: obj.q10,
      q25: Array.isArray(obj.q25) ? obj.q25 : obj.q10,   // fallback: inner = outer
      q50: obj.q50,
      q75: Array.isArray(obj.q75) ? obj.q75 : obj.q90,
      q90: obj.q90,
      confidence: Array.isArray(obj.confidence) ? obj.confidence : obj.q50.map(() => 50),
      horizon_days: obj.horizon_days ?? obj.q50.length,
    };
  } catch {
    return null;
  }
}

// Reused tone palette. Threshold matches the agent's _NEUTRAL_PCT (1%) so
// the chart colour agrees with the directional signal it ends up emitting.
function tone(forecast: ForecastPayload): 'pos' | 'neg' | 'neu' {
  const last = forecast.history[forecast.history.length - 1];
  const end = forecast.q50[forecast.q50.length - 1];
  const pct = ((end - last) / last) * 100;
  return Math.abs(pct) < 1 ? 'neu' : pct > 0 ? 'pos' : 'neg';
}

const TONE_STROKE = { pos: '#10b981', neg: '#ef4444', neu: '#94a3b8' };
const TONE_FILL_INNER = {
  pos: 'rgba(16,185,129,0.28)',
  neg: 'rgba(239,68,68,0.28)',
  neu: 'rgba(148,163,184,0.28)',
};
const TONE_FILL_OUTER = {
  pos: 'rgba(16,185,129,0.12)',
  neg: 'rgba(239,68,68,0.12)',
  neu: 'rgba(148,163,184,0.12)',
};

// --- Node component -------------------------------------------------------

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
  const [isOutputDialogOpen, setIsOutputDialogOpen] = useState(false);
  const [isDetailOpen, setIsDetailOpen] = useState(false);

  // {ticker → latest forecast}, newest-first scan of message history.
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

            <div className="text-subtitle text-primary flex items-center justify-between gap-1 mt-1">
              <span>Forecast</span>
              <span
                className="text-[10px] uppercase tracking-wide text-muted-foreground"
                title="Amazon Chronos-2 — 120M-param probabilistic time-series foundation model. Runs locally on cached weights; no API key required."
              >
                Chronos-2
              </span>
            </div>
            {activeForecast ? (
              <>
                {/* Inline preview — click-through to detail dialog. */}
                <button
                  type="button"
                  onClick={() => setIsDetailOpen(true)}
                  title="Open detailed forecast"
                  className="group relative w-full rounded border border-border bg-node/40 hover:border-primary/40 transition-colors"
                >
                  <InlineFanChart forecast={activeForecast} />
                  <Maximize2 className="absolute top-1 right-1 h-3 w-3 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity" />
                </button>
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
          isOpen={isOutputDialogOpen}
          onOpenChange={setIsOutputDialogOpen}
          name={data.name || 'Time Series Forecaster'}
          nodeId={id}
          flowId={currentFlowId?.toString() || null}
        />
        <ForecastDetailDialog
          isOpen={isDetailOpen}
          onOpenChange={setIsDetailOpen}
          tickers={tickers}
          activeTicker={activeTicker}
          onTickerChange={setActiveTicker}
          forecastsByTicker={forecastsByTicker}
        />
      </CardContent>
    </NodeShell>
  );
}

// --- Inline fan chart (compact, on node body) -----------------------------

function InlineFanChart({ forecast }: { forecast: ForecastPayload }) {
  const { history, q10, q25, q50, q75, q90 } = forecast;
  const W = 220;
  const H = 90;
  const padX = 4;
  const padY = 6;

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

  const histPath = history.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i).toFixed(2)} ${yAt(v).toFixed(2)}`).join(' ');
  const fcStart = history.length - 1;
  const lastHist = history[history.length - 1];

  const bandPath = (lo: number[], hi: number[]) => {
    const top = [lastHist, ...hi].map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAt(v).toFixed(2)}`).join(' ');
    const bot = [lastHist, ...lo]
      .slice()
      .reverse()
      .map((v, i, arr) => `L ${xAt(fcStart + (arr.length - 1 - i)).toFixed(2)} ${yAt(v).toFixed(2)}`)
      .join(' ');
    return `${top} ${bot} Z`;
  };

  const q50Pts = [lastHist, ...q50];
  const q50Path = q50Pts.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAt(v).toFixed(2)}`).join(' ');

  const t = tone(forecast);
  const stroke = TONE_STROKE[t];
  const sepX = xAt(fcStart);

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} className="block">
      <path d={bandPath(q10, q90)} fill={TONE_FILL_OUTER[t]} stroke="none" />
      <path d={bandPath(q25, q75)} fill={TONE_FILL_INNER[t]} stroke="none" />
      <path d={histPath} fill="none" stroke="currentColor" strokeWidth={1.2} opacity={0.55} />
      <path d={q50Path} fill="none" stroke={stroke} strokeWidth={1.6} />
      <line x1={sepX} y1={padY} x2={sepX} y2={H - padY} stroke="currentColor" strokeWidth={0.5} strokeDasharray="2,2" opacity={0.4} />
    </svg>
  );
}

function ForecastSummary({ forecast }: { forecast: ForecastPayload }) {
  const last = forecast.history[forecast.history.length - 1];
  const q10 = forecast.q10[forecast.q10.length - 1];
  const q50 = forecast.q50[forecast.q50.length - 1];
  const q90 = forecast.q90[forecast.q90.length - 1];
  const conf = forecast.confidence[forecast.confidence.length - 1] ?? 0;
  const fmt = (n: number) => `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`;
  return (
    <div className="text-[10px] text-muted-foreground tabular-nums leading-tight grid grid-cols-5 gap-1">
      <Stat label="Now" value={last.toFixed(2)} />
      <Stat label="Q10" value={fmt(((q10 - last) / last) * 100)} cls="text-red-500/80" />
      <Stat label="Q50" value={fmt(((q50 - last) / last) * 100)} cls={((q50 - last) / last) >= 0 ? 'text-emerald-500' : 'text-red-500'} />
      <Stat label="Q90" value={fmt(((q90 - last) / last) * 100)} cls="text-emerald-500/80" />
      <Stat label="Conf" value={`${conf}%`} />
    </div>
  );
}

function Stat({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wide">{label}</div>
      <div className={cls ?? 'text-foreground'}>{value}</div>
    </div>
  );
}

// --- Detail dialog --------------------------------------------------------

interface DetailDialogProps {
  isOpen: boolean;
  onOpenChange: (v: boolean) => void;
  tickers: string[];
  activeTicker: string | null;
  onTickerChange: (t: string) => void;
  forecastsByTicker: Record<string, ForecastPayload>;
}

function ForecastDetailDialog({ isOpen, onOpenChange, tickers, activeTicker, onTickerChange, forecastsByTicker }: DetailDialogProps) {
  const forecast = activeTicker ? forecastsByTicker[activeTicker] : null;
  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent
        // Fill the screen with a small breathing margin. The chart inside
        // grows to match: its viewBox is sized for this footprint so the
        // extra space shows more detail rather than being a blow-up.
        className="!max-w-[96vw] w-[96vw] !max-h-[92vh] h-[92vh] bg-node border border-border overflow-y-auto p-4 sm:p-6 flex flex-col gap-3"
        onClick={(e) => e.stopPropagation()}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-primary">
            <LineChart className="h-4 w-4" />
            <span>Chronos-2 Forecast{activeTicker ? ` · ${activeTicker}` : ''}</span>
          </DialogTitle>
        </DialogHeader>
        {forecast ? (
          <DetailBody forecast={forecast} tickers={tickers} activeTicker={activeTicker} onTickerChange={onTickerChange} />
        ) : (
          <div className="p-6 text-sm text-muted-foreground text-center">No forecast to display.</div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function DetailBody({
  forecast,
  tickers,
  activeTicker,
  onTickerChange,
}: {
  forecast: ForecastPayload;
  tickers: string[];
  activeTicker: string | null;
  onTickerChange: (t: string) => void;
}) {
  // Header stats — same numbers the node-body strip shows, expanded.
  const last = forecast.history[forecast.history.length - 1];
  const endQ10 = forecast.q10[forecast.q10.length - 1];
  const endQ50 = forecast.q50[forecast.q50.length - 1];
  const endQ90 = forecast.q90[forecast.q90.length - 1];
  const endConf = forecast.confidence[forecast.confidence.length - 1] ?? 0;
  const startConf = forecast.confidence[0] ?? 0;
  const fmtPct = (a: number, b: number) => `${a >= b ? '+' : ''}${(((a - b) / b) * 100).toFixed(2)}%`;
  const t = tone(forecast);

  return (
    // flex-1 + min-h-0 so this fills the DialogContent's flex column,
    // which lets the chart child claim all leftover height. Without
    // min-h-0 flex items refuse to shrink below their content size.
    <div className="flex-1 min-h-0 flex flex-col gap-3 text-foreground">
      {/* Ticker selector */}
      {tickers.length > 1 && (
        <div className="flex flex-wrap gap-1">
          {tickers.map((tk) => (
            <button
              key={tk}
              onClick={() => onTickerChange(tk)}
              className={cn(
                'px-2 py-0.5 rounded text-xs tabular-nums border transition-colors',
                tk === activeTicker
                  ? 'bg-primary/20 border-primary/40 text-primary'
                  : 'border-border text-muted-foreground hover:bg-node/50',
              )}
            >
              {tk}
            </button>
          ))}
        </div>
      )}

      {/* Header summary */}
      <div className="grid grid-cols-6 gap-2 text-xs tabular-nums">
        <Cell title="Last close" value={`$${last.toFixed(2)}`} />
        <Cell title={`${forecast.horizon_days}-d Q10`} value={fmtPct(endQ10, last)} className="text-red-500/90" sub={`$${endQ10.toFixed(2)}`} />
        <Cell title={`${forecast.horizon_days}-d Q50`} value={fmtPct(endQ50, last)} className={TONE_STROKE[t]} sub={`$${endQ50.toFixed(2)}`} />
        <Cell title={`${forecast.horizon_days}-d Q90`} value={fmtPct(endQ90, last)} className="text-emerald-500/90" sub={`$${endQ90.toFixed(2)}`} />
        <Cell title="Confidence (day 1)" value={`${startConf}%`} />
        <Cell title={`Confidence (day ${forecast.horizon_days})`} value={`${endConf}%`} />
      </div>

      <DetailChart forecast={forecast} />

      <p className="text-[11px] text-muted-foreground leading-snug">
        Inner band = 50% prediction interval (q25-q75). Outer band = 80% prediction interval (q10-q90).
        Confidence is the precision of the predictive distribution at each step
        (narrow fan = confident); it decays as the horizon extends because the fan widens with time.
      </p>
    </div>
  );
}

function Cell({ title, value, sub, className }: { title: string; value: string; sub?: string; className?: string }) {
  return (
    <div className="rounded border border-border p-2 bg-node/40">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{title}</div>
      <div className={cn('text-sm font-medium', className)} style={className && className.startsWith('#') ? { color: className } : undefined}>{value}</div>
      {sub && <div className="text-[10px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

// --- Detail two-panel chart -----------------------------------------------

function DetailChart({ forecast }: { forecast: ForecastPayload }) {
  // Geometry. Two stacked panels share x; price on top (taller), confidence
  // strip below. ViewBox sized for a wide-screen dialog (~2.3:1 aspect)
  // so it doesn't get letterboxed against a 96vw-wide container — the
  // chart fills the surface rather than being a small sprite in a big
  // dialog. Generous left/bottom padding for axis labels.
  const W = 1600;
  const PRICE_H = 460;
  const CONF_H = 180;
  const padL = 72;
  const padR = 16;
  const padTopPrice = 16;
  const padBetween = 26;
  const padBottom = 34;
  const H = padTopPrice + PRICE_H + padBetween + CONF_H + padBottom;

  const { history, q10, q25, q50, q75, q90, confidence } = forecast;
  const histN = history.length;
  const fcN = q50.length;
  const total = histN + fcN;

  const innerW = W - padL - padR;
  const xAt = (i: number) => padL + (i / (total - 1)) * innerW;

  // Price y-axis — domain spans every drawn point, padded 4%.
  const allPrice = [...history, ...q10, ...q50, ...q90];
  const minP = Math.min(...allPrice);
  const maxP = Math.max(...allPrice);
  const padPrice = (maxP - minP) * 0.04 || 1;
  const yMin = minP - padPrice;
  const yMax = maxP + padPrice;
  const yRange = yMax - yMin || 1;
  const yAtPrice = (v: number) => padTopPrice + (1 - (v - yMin) / yRange) * PRICE_H;

  // Confidence y-axis — fixed 0..100.
  const confTop = padTopPrice + PRICE_H + padBetween;
  const yAtConf = (v: number) => confTop + (1 - v / 100) * CONF_H;

  const t = tone(forecast);
  const stroke = TONE_STROKE[t];

  const fcStart = histN - 1;
  const lastHist = history[histN - 1];
  const sepX = xAt(fcStart);

  // Paths
  const histPath = history.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i).toFixed(2)} ${yAtPrice(v).toFixed(2)}`).join(' ');

  const bandPath = (lo: number[], hi: number[]) => {
    const top = [lastHist, ...hi].map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAtPrice(v).toFixed(2)}`).join(' ');
    const bot = [lastHist, ...lo]
      .slice()
      .reverse()
      .map((v, i, arr) => `L ${xAt(fcStart + (arr.length - 1 - i)).toFixed(2)} ${yAtPrice(v).toFixed(2)}`)
      .join(' ');
    return `${top} ${bot} Z`;
  };

  const q50Path = [lastHist, ...q50].map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAtPrice(v).toFixed(2)}`).join(' ');

  // Confidence area path (fills below the line).
  const confLinePoints = confidence.map((c, i) => `${xAt(fcStart + 1 + i).toFixed(2)},${yAtConf(c).toFixed(2)}`);
  const confArea = confidence.length
    ? `M ${xAt(fcStart + 1).toFixed(2)} ${yAtConf(0).toFixed(2)} L ${confLinePoints.join(' L ')} L ${xAt(fcStart + confidence.length).toFixed(2)} ${yAtConf(0).toFixed(2)} Z`
    : '';
  const confLine = confidence.length ? `M ${confLinePoints.join(' L ')}` : '';

  // Axis tick values
  const priceTicks = [yMax, (yMax + yMin) / 2, yMin];

  // Hover state — nearest day index (history span 0..histN-1, forecast histN..total-1).
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const xPx = ((e.clientX - rect.left) / rect.width) * W;
    if (xPx < padL || xPx > W - padR) {
      setHoverIdx(null);
      return;
    }
    const frac = (xPx - padL) / innerW;
    const idx = Math.round(frac * (total - 1));
    setHoverIdx(Math.max(0, Math.min(total - 1, idx)));
  };

  const hoverX = hoverIdx != null ? xAt(hoverIdx) : null;
  const isHistHover = hoverIdx != null && hoverIdx < histN;
  const histIdx = isHistHover ? hoverIdx : null;
  const fcIdx = !isHistHover && hoverIdx != null ? hoverIdx - histN : null;

  const dayLabel = (i: number) => {
    const fromToday = i - (histN - 1);
    if (fromToday === 0) return 'today';
    if (fromToday < 0) return `t${fromToday}`;
    return `t+${fromToday}`;
  };

  return (
    // flex-1 + min-h-0 lets this take all the remaining vertical space
    // inside the flex-column dialog. Without min-h-0 the flex item refuses
    // to shrink below content size and overflows the viewport.
    <div className="flex-1 min-h-0 rounded border border-border bg-node/40 p-2 flex flex-col">
      <svg
        ref={svgRef}
        width="100%"
        height="100%"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        className="block text-foreground flex-1 min-h-0"
        onMouseMove={onMove}
        onMouseLeave={() => setHoverIdx(null)}
      >
        {/* Price panel frame */}
        <line x1={padL} y1={padTopPrice} x2={padL} y2={padTopPrice + PRICE_H} stroke="currentColor" strokeWidth={0.5} opacity={0.3} />
        <line x1={padL} y1={padTopPrice + PRICE_H} x2={W - padR} y2={padTopPrice + PRICE_H} stroke="currentColor" strokeWidth={0.5} opacity={0.3} />

        {/* Price y-ticks */}
        {priceTicks.map((p, i) => (
          <g key={i}>
            <line x1={padL - 4} y1={yAtPrice(p)} x2={padL} y2={yAtPrice(p)} stroke="currentColor" strokeWidth={0.5} opacity={0.4} />
            <text x={padL - 6} y={yAtPrice(p) + 3} textAnchor="end" fontSize={15} fill="currentColor" opacity={0.7}>
              {p.toFixed(2)}
            </text>
          </g>
        ))}

        {/* Last-close baseline */}
        <line
          x1={padL}
          y1={yAtPrice(lastHist)}
          x2={W - padR}
          y2={yAtPrice(lastHist)}
          stroke="currentColor"
          strokeWidth={0.5}
          strokeDasharray="3,3"
          opacity={0.25}
        />

        {/* Nested bands + median + history */}
        <path d={bandPath(q10, q90)} fill={TONE_FILL_OUTER[t]} stroke="none" />
        <path d={bandPath(q25, q75)} fill={TONE_FILL_INNER[t]} stroke="none" />
        <path d={histPath} fill="none" stroke="currentColor" strokeWidth={1.2} opacity={0.65} />
        <path d={q50Path} fill="none" stroke={stroke} strokeWidth={1.6} />

        {/* Today separator (spans both panels) */}
        <line x1={sepX} y1={padTopPrice} x2={sepX} y2={confTop + CONF_H} stroke="currentColor" strokeWidth={0.5} strokeDasharray="2,2" opacity={0.4} />

        {/* Confidence panel frame + grid */}
        <line x1={padL} y1={confTop} x2={padL} y2={confTop + CONF_H} stroke="currentColor" strokeWidth={0.5} opacity={0.3} />
        <line x1={padL} y1={confTop + CONF_H} x2={W - padR} y2={confTop + CONF_H} stroke="currentColor" strokeWidth={0.5} opacity={0.3} />
        {[0, 25, 50, 75, 100].map((v) => (
          <g key={v}>
            <line x1={padL - 4} y1={yAtConf(v)} x2={padL} y2={yAtConf(v)} stroke="currentColor" strokeWidth={0.5} opacity={0.4} />
            <line x1={padL} y1={yAtConf(v)} x2={W - padR} y2={yAtConf(v)} stroke="currentColor" strokeWidth={0.3} opacity={0.15} />
            <text x={padL - 6} y={yAtConf(v) + 3} textAnchor="end" fontSize={15} fill="currentColor" opacity={0.7}>
              {v}
            </text>
          </g>
        ))}

        {/* Confidence area + line */}
        {confArea && <path d={confArea} fill={TONE_FILL_INNER[t]} stroke="none" />}
        {confLine && <path d={confLine} fill="none" stroke={stroke} strokeWidth={1.6} />}

        {/* X-axis day labels — every 5 days from t-30 to t+10 */}
        {Array.from({ length: total }, (_, i) => i)
          .filter((i) => i === 0 || i === histN - 1 || i === total - 1 || (histN - 1 - i) % 10 === 0 || (i - (histN - 1)) % 5 === 0)
          .map((i) => (
            <text key={i} x={xAt(i)} y={H - 8} textAnchor="middle" fontSize={15} fill="currentColor" opacity={0.7}>
              {dayLabel(i)}
            </text>
          ))}

        {/* Panel labels */}
        <text x={padL + 4} y={padTopPrice + 10} fontSize={15} fill="currentColor" opacity={0.6}>price (USD)</text>
        <text x={padL + 4} y={confTop + 10} fontSize={15} fill="currentColor" opacity={0.6}>confidence (0-100)</text>

        {/* Hover overlay */}
        {hoverX != null && (
          <>
            <line x1={hoverX} y1={padTopPrice} x2={hoverX} y2={confTop + CONF_H} stroke="currentColor" strokeWidth={0.6} opacity={0.5} />
            {histIdx != null && (
              <circle cx={hoverX} cy={yAtPrice(history[histIdx])} r={2.5} fill="currentColor" />
            )}
            {fcIdx != null && (
              <>
                <circle cx={hoverX} cy={yAtPrice(q50[fcIdx])} r={3} fill={stroke} />
                <circle cx={hoverX} cy={yAtConf(confidence[fcIdx] ?? 0)} r={3} fill={stroke} />
              </>
            )}
          </>
        )}
      </svg>

      {/* Hover tooltip — rendered as HTML rather than SVG text for crisp typography. */}
      {hoverIdx != null && (
        <HoverTooltip
          forecast={forecast}
          hoverIdx={hoverIdx}
          histN={histN}
          dayLabel={dayLabel(hoverIdx)}
        />
      )}
    </div>
  );
}

function HoverTooltip({
  forecast,
  hoverIdx,
  histN,
  dayLabel,
}: {
  forecast: ForecastPayload;
  hoverIdx: number;
  histN: number;
  dayLabel: string;
}) {
  const isHist = hoverIdx < histN;
  const last = forecast.history[histN - 1];
  if (isHist) {
    const price = forecast.history[hoverIdx];
    const delta = ((price - last) / last) * 100;
    return (
      <div className="mt-2 flex items-center gap-3 text-[11px] tabular-nums">
        <span className="text-muted-foreground">{dayLabel}</span>
        <span><span className="text-muted-foreground">price</span> ${price.toFixed(2)}</span>
        <span className={delta >= 0 ? 'text-emerald-500' : 'text-red-500'}>
          {delta >= 0 ? '+' : ''}{delta.toFixed(2)}% vs last close
        </span>
      </div>
    );
  }
  const fcIdx = hoverIdx - histN;
  const q10 = forecast.q10[fcIdx];
  const q25 = forecast.q25[fcIdx];
  const q50 = forecast.q50[fcIdx];
  const q75 = forecast.q75[fcIdx];
  const q90 = forecast.q90[fcIdx];
  const conf = forecast.confidence[fcIdx] ?? 0;
  const pct = (v: number) => `${v - last >= 0 ? '+' : ''}${(((v - last) / last) * 100).toFixed(2)}%`;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] tabular-nums">
      <span className="text-muted-foreground">{dayLabel}</span>
      <span><span className="text-muted-foreground">q10</span> ${q10.toFixed(2)} <span className="text-red-500/80">({pct(q10)})</span></span>
      <span><span className="text-muted-foreground">q25</span> ${q25.toFixed(2)}</span>
      <span><span className="text-muted-foreground">q50</span> ${q50.toFixed(2)} <span className={q50 >= last ? 'text-emerald-500' : 'text-red-500'}>({pct(q50)})</span></span>
      <span><span className="text-muted-foreground">q75</span> ${q75.toFixed(2)}</span>
      <span><span className="text-muted-foreground">q90</span> ${q90.toFixed(2)} <span className="text-emerald-500/80">({pct(q90)})</span></span>
      <span><span className="text-muted-foreground">conf</span> {conf}%</span>
    </div>
  );
}
