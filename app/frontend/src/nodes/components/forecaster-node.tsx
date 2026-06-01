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
import { LineChart, Loader2, Maximize2, RefreshCw } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';

import { CardContent } from '@/components/ui/card';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { useNodeState } from '@/hooks/use-node-state';
import { cn } from '@/lib/utils';
import { refreshForecaster } from '@/services/forecaster-api';
import { getFlowMemory } from '@/services/memory-api';
import { type AgentNode } from '../types';
import { getStatusColor } from '../utils';
import { AgentOutputDialog } from './agent-output-dialog';
import { NodeShell } from './node-shell';

// Chronos-2 hard limits (from the model card): context up to 8192,
// prediction up to 1024. Sensible defaults match the module constants
// in the backend agent so a freshly-dropped node behaves identically
// to the pre-config-feature version.
const CTX_MIN = 32;
const CTX_MAX = 8192;
const CTX_DEFAULT = 256;
const PRED_MIN = 1;
const PRED_MAX = 1024;
const PRED_DEFAULT = 10;

// Bar-frequency options. The unit applies to both context and prediction
// counts: 256 + 10 at 'hour' = 256 hours of context, 10-hour forecast.
// Intraday goes through yfinance which has hard period caps per interval:
// 1min ≤ 7 days, 5min ≤ 60 days, 1h ≤ 730 days — surfaced as tooltips
// so the user knows when their context_len would be silently truncated.
type BarFrequency = 'day' | 'hour' | '5min' | '1min';
const FREQ_OPTIONS: { value: BarFrequency; label: string; hint: string }[] = [
  { value: 'day', label: 'Daily', hint: 'One bar per trading day. Full history via the cached provider chain.' },
  { value: 'hour', label: 'Hourly', hint: 'One bar per trading hour (~7/day). Max ~730 days of history via yfinance.' },
  { value: '5min', label: '5-Minute', hint: 'One bar every 5 minutes (~78/day). Max 60 days of history via yfinance.' },
  { value: '1min', label: '1-Minute', hint: 'One bar per minute (~390/day). Max 7 days of history via yfinance.' },
];

// --- Types and parsing ----------------------------------------------------

interface ForecastPayload {
  history: number[];
  q10: number[];
  q25: number[];          // inner-band lower (50% PI)
  q50: number[];          // median
  q75: number[];          // inner-band upper (50% PI)
  q90: number[];          // outer-band upper (80% PI)
  confidence: number[];   // 0-100 per step (fan-width-derived; see backend)
  horizon_days: number;   // count of bars in the forecast (name stays for back-compat)
  frequency?: 'day' | 'hour' | '5min' | '1min'; // unit of each bar; undef → daily
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
      frequency: obj.frequency,
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
  const { getAgentNodeDataForFlow, updateAgentNode } = useNodeContext();

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

  // Per-node Chronos-2 length config — stored via useNodeState so the
  // Play-trigger node (portfolio-start / stock-analyzer) can read it
  // back via getNodeInternalState when assembling the run request, and
  // so the values survive flow reload via the standard internal_state
  // restore path.
  const [contextLen, setContextLen] = useNodeState<number>(id, 'forecasterContextLen', CTX_DEFAULT);
  const [predictionLen, setPredictionLen] = useNodeState<number>(id, 'forecasterPredictionLen', PRED_DEFAULT);
  const [barFrequency, setBarFrequency] = useNodeState<BarFrequency>(id, 'forecasterBarFrequency', 'day');
  const clampCtx = (n: number) => Math.max(CTX_MIN, Math.min(CTX_MAX, Math.round(n) || CTX_DEFAULT));
  const clampPred = (n: number) => Math.max(PRED_MIN, Math.min(PRED_MAX, Math.round(n) || PRED_DEFAULT));
  const activeFreq = FREQ_OPTIONS.find((o) => o.value === barFrequency) ?? FREQ_OPTIONS[0];

  // Live runtime forecasts: {ticker → latest forecast}, newest-first scan
  // of the SSE message history that NodeContext accumulates during a run.
  const runtimeForecasts = useMemo<Record<string, ForecastPayload>>(() => {
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

  // Wiki-rehydrated forecasts. NodeContext is purged on page reload (see
  // flow-tab-content.tsx — "Runtime execution data should start fresh"),
  // but the forecaster persists its full trajectory inside each Insight's
  // reasoning Markdown via the same `forecast-data` fence. We pull
  // /memory?flow_id=… on mount and parse the fence out per ticker so the
  // chart reappears after F5.
  const [rehydrated, setRehydrated] = useState<Record<string, ForecastPayload>>({});
  const flowIdNum = currentFlowId != null ? Number(currentFlowId) : null;
  useEffect(() => {
    if (flowIdNum == null) return;
    let cancelled = false;
    getFlowMemory(flowIdNum)
      .then((mem) => {
        if (cancelled) return;
        const next: Record<string, ForecastPayload> = {};
        for (const t of mem.tickers || []) {
          // The wiki keys analyst names via normalize_analyst_name —
          // 'forecaster_agent' → 'Forecaster' (not 'Time Series
          // Forecaster' as the bench shows). Match case-insensitively
          // so a future rename of either side doesn't silently break.
          const row = (t.analysts || []).find(
            (a) => a.analyst?.toLowerCase() === 'forecaster' || a.analyst?.toLowerCase() === 'time series forecaster',
          );
          const fc = extractForecast(row?.reasoning);
          if (fc) next[t.ticker] = fc;
        }
        setRehydrated(next);
      })
      .catch(() => {
        // Fail-open: no rehydration is the placeholder state, not an error.
      });
    return () => {
      cancelled = true;
    };
  }, [flowIdNum]);

  // Prefer live runtime data — it's always fresher than what the wiki
  // has from a prior run. Fall back to rehydrated wiki data when the
  // runtime stream is empty (post-reload, or before the first run on a
  // restored flow). Merge per-ticker so a partial new run still surfaces
  // the latest forecast for tickers it already produced.
  const forecastsByTicker = useMemo<Record<string, ForecastPayload>>(() => {
    return { ...rehydrated, ...runtimeForecasts };
  }, [rehydrated, runtimeForecasts]);

  const tickers = useMemo(() => Object.keys(forecastsByTicker).sort(), [forecastsByTicker]);
  const [activeTicker, setActiveTicker] = useState<string | null>(null);
  useEffect(() => {
    if (!activeTicker && tickers.length) setActiveTicker(tickers[0]);
    if (activeTicker && tickers.length && !tickers.includes(activeTicker)) setActiveTicker(tickers[0] ?? null);
  }, [tickers, activeTicker]);

  const activeForecast = activeTicker ? forecastsByTicker[activeTicker] : null;

  // Stand-alone refresh: POST /forecaster/refresh with the tickers we
  // already have data for and the current Chronos-2 settings, then
  // dispatch synthetic Done messages so the chart updates immediately
  // (same channel SSE-driven runs use). Cheap path: no LLM, no other
  // analysts, no PM. Disabled while in-flight or when there are no
  // tickers to refresh (i.e., the node has never had a prior run).
  const [isRefreshing, setIsRefreshing] = useState(false);
  const flowIdStr = currentFlowId?.toString() || null;
  const handleRefresh = async () => {
    if (isRefreshing || tickers.length === 0) return;
    setIsRefreshing(true);
    try {
      const flowIdNumLocal = currentFlowId != null ? Number(currentFlowId) : undefined;
      const resp = await refreshForecaster({
        tickers,
        flow_id: flowIdNumLocal,
        forecaster_context_len: contextLen,
        forecaster_prediction_len: predictionLen,
        forecaster_bar_frequency: barFrequency,
      });
      // Mirror each ticker's reasoning into a synthetic Done message,
      // same shape NodeContext rehydration uses post-reload. The
      // existing runtimeForecasts useMemo picks the fence out and the
      // chart re-renders. ISO timestamp keeps de-duplication honest
      // across rapid clicks (Date.now is per-call distinct).
      const now = new Date().toISOString();
      for (const [t, sig] of Object.entries(resp.signals || {})) {
        if (!sig?.reasoning) continue;
        updateAgentNode(flowIdStr, id, {
          timestamp: `${now}#${t}`,
          message: 'Done',
          ticker: t,
          analysis: sig.reasoning,
        });
      }
      // Mark the node COMPLETE so its status pill reflects the refresh.
      updateAgentNode(flowIdStr, id, 'COMPLETE');
    } catch (e) {
      console.error('Forecaster refresh failed:', e);
    } finally {
      setIsRefreshing(false);
    }
  };

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

            {/* Chronos-2 settings — bar frequency picker + context/prediction
                lengths. Length units adapt to the frequency: 256 + 10 at
                'Hourly' = 256 hours of context, 10-hour forecast. The
                Daily path runs through the cached provider chain; intraday
                hits yfinance directly with hard period caps per interval
                (1m≤7d, 5m≤60d, 1h≤730d). */}
            <TooltipProvider>
              <div className="text-subtitle text-primary flex items-center gap-1 mt-1">Chronos-2 Settings</div>
              <Tooltip>
                <TooltipTrigger asChild>
                  <div className="flex flex-col gap-0.5">
                    <label className="text-[10px] uppercase tracking-wide text-muted-foreground">
                      Bar frequency
                    </label>
                    <select
                      value={barFrequency}
                      onChange={(e) => setBarFrequency(e.target.value as BarFrequency)}
                      className="w-full rounded border border-border bg-node/60 px-2 py-1 text-sm text-foreground focus:outline-none focus:border-primary/50"
                    >
                      {FREQ_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </div>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-[280px] text-xs">
                  {activeFreq.hint}
                </TooltipContent>
              </Tooltip>
              <div className="grid grid-cols-2 gap-2">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className="flex flex-col gap-0.5">
                      <label className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        Context bars
                      </label>
                      <input
                        type="number"
                        min={CTX_MIN}
                        max={CTX_MAX}
                        step={1}
                        value={contextLen}
                        onChange={(e) => setContextLen(Number(e.target.value) || CTX_DEFAULT)}
                        onBlur={(e) => setContextLen(clampCtx(Number(e.target.value)))}
                        className="w-full rounded border border-border bg-node/60 px-2 py-1 text-sm tabular-nums text-foreground focus:outline-none focus:border-primary/50"
                      />
                    </div>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="max-w-[260px] text-xs">
                    Past bars Chronos-2 reads as context. Range {CTX_MIN}–{CTX_MAX}.
                    More context generally improves the forecast but costs a slower
                    forward pass. Intraday frequencies clip to available history
                    (1m≤7d, 5m≤60d, 1h≤730d).
                  </TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className="flex flex-col gap-0.5">
                      <label className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        Prediction bars
                      </label>
                      <input
                        type="number"
                        min={PRED_MIN}
                        max={PRED_MAX}
                        step={1}
                        value={predictionLen}
                        onChange={(e) => setPredictionLen(Number(e.target.value) || PRED_DEFAULT)}
                        onBlur={(e) => setPredictionLen(clampPred(Number(e.target.value)))}
                        className="w-full rounded border border-border bg-node/60 px-2 py-1 text-sm tabular-nums text-foreground focus:outline-none focus:border-primary/50"
                      />
                    </div>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="max-w-[260px] text-xs">
                    Bars to forecast forward. Range {PRED_MIN}–{PRED_MAX}.
                    Longer horizons produce wider fans (lower confidence) — Chronos
                    is most accurate at the short end.
                  </TooltipContent>
                </Tooltip>
              </div>
            </TooltipProvider>

            <div className="text-subtitle text-primary flex items-center justify-between gap-1 mt-1">
              <span>Forecast</span>
              <div className="flex items-center gap-2">
                {/* Refresh-this-node-only. Disabled while in-flight or
                    before the first run (tickers come from prior data). */}
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        onClick={handleRefresh}
                        disabled={isRefreshing || tickers.length === 0}
                        className={cn(
                          'flex items-center justify-center h-5 w-5 rounded border border-border text-muted-foreground transition-colors',
                          'hover:text-foreground hover:border-primary/40',
                          'disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:text-muted-foreground disabled:hover:border-border',
                        )}
                        aria-label="Refresh forecast"
                      >
                        {isRefreshing ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <RefreshCw className="h-3 w-3" />
                        )}
                      </button>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="max-w-[240px] text-xs">
                      {tickers.length === 0
                        ? 'Run the flow once to set the tickers, then refresh updates only this node.'
                        : `Refresh forecast for ${tickers.length} ticker${tickers.length === 1 ? '' : 's'} — runs Chronos-2 only, no other agents.`}
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <span
                  className="text-[10px] uppercase tracking-wide text-muted-foreground"
                  title="Amazon Chronos-2 — 120M-param probabilistic time-series foundation model. Runs locally on cached weights; no API key required."
                >
                  Chronos-2
                </span>
              </div>
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
          controls={{
            barFrequency,
            setBarFrequency,
            contextLen,
            setContextLen,
            predictionLen,
            setPredictionLen,
            onRefresh: handleRefresh,
            isRefreshing,
            canRefresh: tickers.length > 0,
            tickerCount: tickers.length,
          }}
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

// Shared settings shape — passed by ForecasterNode to the dialog so the
// dialog can mirror the per-node Chronos-2 controls and edit them too.
// Values are sourced from useNodeState in the parent; setters here just
// pipe back into that hook, so state stays single-source and changes are
// visible in both UIs immediately.
interface ChronosControls {
  barFrequency: BarFrequency;
  setBarFrequency: (v: BarFrequency) => void;
  contextLen: number;
  setContextLen: (v: number) => void;
  predictionLen: number;
  setPredictionLen: (v: number) => void;
  // Refresh-this-node-only. Same handler the node body uses, surfaced
  // in the dialog so the user doesn't have to close+reopen to refresh.
  onRefresh: () => void;
  isRefreshing: boolean;
  canRefresh: boolean;  // false when there are no tickers yet
  tickerCount: number;  // for the tooltip
}

interface DetailDialogProps {
  isOpen: boolean;
  onOpenChange: (v: boolean) => void;
  tickers: string[];
  activeTicker: string | null;
  onTickerChange: (t: string) => void;
  forecastsByTicker: Record<string, ForecastPayload>;
  controls: ChronosControls;
}

function ForecastDetailDialog({ isOpen, onOpenChange, tickers, activeTicker, onTickerChange, forecastsByTicker, controls }: DetailDialogProps) {
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
          <DialogTitle className="flex items-center gap-2 text-primary text-xl">
            <LineChart className="h-5 w-5" />
            <span>Chronos-2 Forecast{activeTicker ? ` · ${activeTicker}` : ''}</span>
          </DialogTitle>
        </DialogHeader>
        <DialogChronosSettings controls={controls} />
        {forecast ? (
          <DetailBody forecast={forecast} tickers={tickers} activeTicker={activeTicker} onTickerChange={onTickerChange} />
        ) : (
          <div className="p-6 text-sm text-muted-foreground text-center">No forecast to display.</div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// Module-level clamps so the dialog and node-body inputs apply the same
// bounds. CTX_MIN/MAX etc come from the file's top — Chronos-2 model card.
const clampCtxValue = (n: number) => Math.max(CTX_MIN, Math.min(CTX_MAX, Math.round(n) || CTX_DEFAULT));
const clampPredValue = (n: number) => Math.max(PRED_MIN, Math.min(PRED_MAX, Math.round(n) || PRED_DEFAULT));

function DialogChronosSettings({ controls }: { controls: ChronosControls }) {
  const {
    barFrequency, setBarFrequency,
    contextLen, setContextLen,
    predictionLen, setPredictionLen,
    onRefresh, isRefreshing, canRefresh, tickerCount,
  } = controls;
  const activeFreq = FREQ_OPTIONS.find((o) => o.value === barFrequency) ?? FREQ_OPTIONS[0];
  return (
    <TooltipProvider>
      <div className="rounded border border-border bg-node/40 p-3">
        <div className="flex items-center justify-between mb-2">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">Chronos-2 Settings</div>
          {/* Refresh-this-node-only — same handler the node body uses. */}
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={onRefresh}
                disabled={isRefreshing || !canRefresh}
                className={cn(
                  'flex items-center gap-2 px-3 py-1 rounded border border-border text-sm transition-colors',
                  'hover:border-primary/40 hover:text-foreground',
                  'disabled:opacity-40 disabled:cursor-not-allowed',
                )}
                aria-label="Refresh forecast"
              >
                {isRefreshing ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="h-3.5 w-3.5" />
                )}
                <span>{isRefreshing ? 'Refreshing…' : 'Refresh forecast'}</span>
              </button>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-[320px] text-xs">
              {!canRefresh
                ? 'Run the flow once to set the tickers, then refresh updates only this node.'
                : `Re-runs Chronos-2 only on the current ${tickerCount} ticker${tickerCount === 1 ? '' : 's'} — no other agents, no PM. Updates the chart in place.`}
            </TooltipContent>
          </Tooltip>
        </div>
        <div className="grid grid-cols-3 gap-3">
          <Tooltip>
            <TooltipTrigger asChild>
              <div className="flex flex-col gap-1">
                <label className="text-xs uppercase tracking-wide text-muted-foreground">
                  Bar frequency
                </label>
                <select
                  value={barFrequency}
                  onChange={(e) => setBarFrequency(e.target.value as BarFrequency)}
                  className="w-full rounded border border-border bg-node/60 px-3 py-1.5 text-base text-foreground focus:outline-none focus:border-primary/50"
                >
                  {FREQ_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              </div>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-[320px] text-xs">
              {activeFreq.hint}
            </TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <div className="flex flex-col gap-1">
                <label className="text-xs uppercase tracking-wide text-muted-foreground">
                  Context bars
                </label>
                <input
                  type="number"
                  min={CTX_MIN}
                  max={CTX_MAX}
                  step={1}
                  value={contextLen}
                  onChange={(e) => setContextLen(Number(e.target.value) || CTX_DEFAULT)}
                  onBlur={(e) => setContextLen(clampCtxValue(Number(e.target.value)))}
                  className="w-full rounded border border-border bg-node/60 px-3 py-1.5 text-base tabular-nums text-foreground focus:outline-none focus:border-primary/50"
                />
              </div>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-[320px] text-xs">
              Past bars Chronos-2 reads as context. Range {CTX_MIN}–{CTX_MAX}.
              More context generally improves the forecast but costs a slower
              forward pass. Intraday frequencies clip to available history
              (1m≤7d, 5m≤60d, 1h≤730d).
            </TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <div className="flex flex-col gap-1">
                <label className="text-xs uppercase tracking-wide text-muted-foreground">
                  Prediction bars
                </label>
                <input
                  type="number"
                  min={PRED_MIN}
                  max={PRED_MAX}
                  step={1}
                  value={predictionLen}
                  onChange={(e) => setPredictionLen(Number(e.target.value) || PRED_DEFAULT)}
                  onBlur={(e) => setPredictionLen(clampPredValue(Number(e.target.value)))}
                  className="w-full rounded border border-border bg-node/60 px-3 py-1.5 text-base tabular-nums text-foreground focus:outline-none focus:border-primary/50"
                />
              </div>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-[320px] text-xs">
              Bars to forecast forward. Range {PRED_MIN}–{PRED_MAX}.
              Longer horizons produce wider fans (lower confidence) — Chronos
              is most accurate at the short end.
            </TooltipContent>
          </Tooltip>
        </div>
      </div>
    </TooltipProvider>
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
        <div className="flex flex-wrap gap-1.5">
          {tickers.map((tk) => (
            <button
              key={tk}
              onClick={() => onTickerChange(tk)}
              className={cn(
                'px-3 py-1 rounded text-sm font-medium tabular-nums border transition-colors',
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

      {/* Legend / glossary — split into bullet items so each idea reads
          on its own line; bigger text so it's actually readable. */}
      <ul className="text-sm text-muted-foreground leading-snug flex flex-wrap gap-x-6 gap-y-1">
        <li><span className="text-foreground">Inner band</span> — 50% prediction interval (q25–q75)</li>
        <li><span className="text-foreground">Outer band</span> — 80% prediction interval (q10–q90)</li>
        <li><span className="text-foreground">Confidence</span> — derived from fan width; available in the hover tooltip</li>
      </ul>
    </div>
  );
}

function Cell({ title, value, sub, className }: { title: string; value: string; sub?: string; className?: string }) {
  return (
    <div className="rounded border border-border p-3 bg-node/40">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{title}</div>
      <div
        className={cn('text-lg font-semibold mt-0.5', className)}
        style={className && className.startsWith('#') ? { color: className } : undefined}
      >
        {value}
      </div>
      {sub && <div className="text-xs text-muted-foreground mt-0.5">{sub}</div>}
    </div>
  );
}

// --- Detail two-panel chart -----------------------------------------------

function DetailChart({ forecast }: { forecast: ForecastPayload }) {
  // Responsive geometry: the viewBox matches the SVG's actual rendered
  // pixel dimensions, so 1 viewBox unit == 1 screen pixel and there's no
  // preserveAspectRatio letterboxing on wide screens. Measuring the SVG
  // itself (not its parent) avoids subtracting the tooltip-strip / border
  // by hand. Initial defaults are used until the first measurement lands.
  const [dims, setDims] = useState({ w: 1600, h: 700 });

  // Padding (fixed px) — sized for fontSize=20 axis labels (~19 px wide
  // for 6 chars, ~24 px tall). The conf strip stays at 180 px so it's
  // recognisable as a subplot; the price panel absorbs all leftover
  // height. Width pads stay constant because the y-axis label width
  // doesn't scale with chart width.
  const padL = 100;
  const padR = 24;
  const padTopPrice = 22;
  const padBottom = 50;
  const W = dims.w;
  const H = dims.h;
  // The price panel now fills the whole chart — there used to be a
  // confidence sub-plot below, but that was a 1D projection of the
  // upper chart's fan width (= q90 − q10). The same information lives
  // in the band the user already sees; the per-step confidence number
  // is surfaced in the hover tooltip instead. Dropping the sub-plot
  // roughly doubled the y-resolution of the price axis.
  const PRICE_H = Math.max(140, H - padTopPrice - padBottom);
  const FS_TICK = 20;
  const FS_LABEL = 20;

  // confidence is no longer plotted in this chart (its sub-panel was
  // a 1D projection of the q10/q90 band already visible above). It's
  // still read inside HoverTooltip for the per-step readout.
  const { history, q10, q25, q50, q75, q90 } = forecast;
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

  // Axis tick values — 5 evenly-spaced over the y-domain so the chart is
  // readable as a numeric reference, not just a shape.
  const priceTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => yMin + (yMax - yMin) * (1 - f));

  // Hover state — nearest day index (history span 0..histN-1, forecast histN..total-1).
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  // ResizeObserver on the SVG element itself. Re-fires on dialog resize,
  // window resize, even browser-zoom. setDims triggers a re-render which
  // recomputes every path coordinate against the new W/H.
  useEffect(() => {
    if (!svgRef.current) return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        setDims({ w: Math.max(400, Math.floor(width)), h: Math.max(220, Math.floor(height)) });
      }
    });
    ro.observe(svgRef.current);
    return () => ro.disconnect();
  }, []);

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    // Map client coords → viewBox coords via SVG's own CTM. Doing the
    // ratio with getBoundingClientRect ignores preserveAspectRatio
    // letterboxing: when the container's aspect doesn't match the
    // viewBox, the chart is rendered inside a smaller letterboxed
    // sub-rectangle, but rect.width still spans the whole SVG element —
    // the rect-based math therefore offsets the cursor by exactly the
    // letterbox margin. getScreenCTM().inverse() accounts for the
    // preserveAspectRatio transform and gives the true viewBox X.
    const pt = svg.createSVGPoint();
    pt.x = e.clientX;
    pt.y = e.clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return;
    const local = pt.matrixTransform(ctm.inverse());
    if (local.x < padL || local.x > W - padR) {
      setHoverIdx(null);
      return;
    }
    const frac = (local.x - padL) / innerW;
    const idx = Math.round(frac * (total - 1));
    setHoverIdx(Math.max(0, Math.min(total - 1, idx)));
  };

  const hoverX = hoverIdx != null ? xAt(hoverIdx) : null;
  const isHistHover = hoverIdx != null && hoverIdx < histN;
  const histIdx = isHistHover ? hoverIdx : null;
  const fcIdx = !isHistHover && hoverIdx != null ? hoverIdx - histN : null;

  // Time-axis label suffix adapts to bar frequency so 't+10' reads as
  // 10 days at 'day', 10 hours at 'hour', 10×5min at '5min', and so on.
  // Falls back to 't+N' on missing frequency (older payloads).
  const freqSuffix =
    forecast.frequency === 'hour' ? 'h'
    : forecast.frequency === '5min' ? '×5m'
    : forecast.frequency === '1min' ? 'm'
    : forecast.frequency === 'day' ? 'd'
    : '';
  const dayLabel = (i: number) => {
    const from = i - (histN - 1);
    if (from === 0) return 'now';
    const sign = from < 0 ? '' : '+';
    return `${sign}${from}${freqSuffix}`;
  };

  return (
    // flex-1 + min-h-0 lets this take all the remaining vertical space
    // inside the flex-column dialog. Without min-h-0 the flex item refuses
    // to shrink below content size and overflows the viewport. The
    // containerRef + ResizeObserver above measures this div so the SVG
    // viewBox can match it pixel-for-pixel — no preserveAspectRatio
    // letterboxing on wide-screen displays.
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

        {/* Price y-ticks + faint horizontal gridlines so values are
            readable as numbers, not just shape. dy=".35em" centres
            text vertically on the tick line at any font size. */}
        {priceTicks.map((p, i) => (
          <g key={i}>
            <line x1={padL} y1={yAtPrice(p)} x2={W - padR} y2={yAtPrice(p)} stroke="currentColor" strokeWidth={0.3} opacity={0.12} />
            <line x1={padL - 6} y1={yAtPrice(p)} x2={padL} y2={yAtPrice(p)} stroke="currentColor" strokeWidth={0.5} opacity={0.4} />
            <text x={padL - 10} y={yAtPrice(p)} dy=".35em" textAnchor="end" fontSize={FS_TICK} fill="currentColor" opacity={0.75}>
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

        {/* Today separator */}
        <line x1={sepX} y1={padTopPrice} x2={sepX} y2={padTopPrice + PRICE_H} stroke="currentColor" strokeWidth={0.5} strokeDasharray="2,2" opacity={0.4} />

        {/* X-axis labels — adaptive step so the bottom row stays
            readable at any chart size. Target ~15 labels; always show
            start / now / end as anchors. At 30+10 points the step is
            3 (≈ 13 labels); at 8192+1024 points the step is ~615
            (≈ 15 labels). */}
        {(() => {
          const step = Math.max(1, Math.ceil(total / 15));
          const anchors = new Set<number>([0, histN - 1, total - 1]);
          const indices: number[] = [];
          for (let i = 0; i < total; i++) {
            if (anchors.has(i) || i % step === 0) indices.push(i);
          }
          return indices.map((i) => (
            <text key={i} x={xAt(i)} y={H - 12} textAnchor="middle" fontSize={FS_TICK} fill="currentColor" opacity={0.75}>
              {dayLabel(i)}
            </text>
          ));
        })()}

        {/* Panel label — dy=".9em" pushes text under the zero-height
            baseline so it sits inside the panel rather than over the frame. */}
        <text x={padL + 8} y={padTopPrice} dy=".9em" fontSize={FS_LABEL} fill="currentColor" opacity={0.6}>price (USD)</text>

        {/* Hover overlay — vertical line spans the price panel only;
            the per-step confidence shows up in the tooltip strip below. */}
        {hoverX != null && (
          <>
            <line x1={hoverX} y1={padTopPrice} x2={hoverX} y2={padTopPrice + PRICE_H} stroke="currentColor" strokeWidth={0.6} opacity={0.5} />
            {histIdx != null && (
              <circle cx={hoverX} cy={yAtPrice(history[histIdx])} r={2.5} fill="currentColor" />
            )}
            {fcIdx != null && (
              <circle cx={hoverX} cy={yAtPrice(q50[fcIdx])} r={3} fill={stroke} />
            )}
          </>
        )}
      </svg>

      {/* Hover tooltip — rendered as HTML rather than SVG text for
          crisp typography. The strip has a *reserved* height even when
          empty so the SVG above doesn't reflow on hover-on/off (which
          previously caused the chart to visibly re-rasterise — i.e.
          the "jiggle"). nowrap + overflow-hidden cap a wide line so a
          long tooltip can't push the strip taller and re-trigger the
          reflow we're trying to avoid. */}
      <div className="h-7 mt-2 flex items-center text-sm tabular-nums overflow-hidden whitespace-nowrap">
        {hoverIdx != null && (
          <HoverTooltip
            forecast={forecast}
            hoverIdx={hoverIdx}
            histN={histN}
            dayLabel={dayLabel(hoverIdx)}
          />
        )}
      </div>
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
      <div className="flex items-center gap-3">
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
    <div className="flex items-center gap-3">
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
