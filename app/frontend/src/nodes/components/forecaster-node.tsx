// Custom node for the Time Series Forecaster analyst.
//
// Inline preview on the node body shows a compact nested-band fan chart;
// clicking it opens ForecastDetailDialog — a larger two-panel view with
// axes, hover tooltip, and a confidence-over-time subplot.
//
// Two backbones share this component:
//   * Amazon Chronos-2 (120M, encoder-only) — the original v1 backbone
//   * Datadog Toto-2.0 (313M, decoder-only patched transformer) — added in
//     the second-forecaster PR. Requires the optional install via
//     scripts/install_toto.sh; without it the node still renders but
//     refresh shows "unavailable".
// Selection is keyed off the agent_id prefix (`forecaster_*` vs
// `toto_forecaster_*`). Sidebar palette spawns one of each. Both produce
// the same `forecast-data` JSON fence so chart/dialog/rehydrate paths
// don't fork by backbone.
//
// Data path: the backend agent (src/agents/forecaster.py) appends a
// ```forecast-data``` JSON fence to the per-ticker analysis Markdown
// that already rides the SSE 'analysis' channel. We parse it out of the
// per-ticker messages stored in node-context — no SSE schema change.

import { type NodeProps, useNodes, useReactFlow } from '@xyflow/react';
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

// Backbone detection from the node id. The sidebar palette creates either
// a `forecaster_xxx` id (Chronos-2) or a `toto_forecaster_xxx` id (Toto-2.0);
// the prefix carries the backbone choice through. UI labels read this map.
type Backbone = 'chronos2' | 'toto2';
function backboneFromId(id: string): Backbone {
  return id.startsWith('toto_forecaster') ? 'toto2' : 'chronos2';
}
const BACKBONE_INFO: Record<Backbone, { label: string; blurb: string }> = {
  chronos2: {
    label: 'Chronos-2',
    blurb: 'Amazon Chronos-2 — 120M-param probabilistic time-series foundation model. Runs locally on cached weights; no API key required.',
  },
  toto2: {
    label: 'Toto-2.0',
    blurb: 'Datadog Toto-2.0-313m — 313M-param decoder-only patched transformer (Apache 2.0). Trained on observability + synthetic data, so equity prices are out-of-distribution. Requires the optional install via scripts/install_toto.sh.',
  },
};

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
  // Needed to find the OTHER forecaster node on the canvas for the
  // overlay (Chronos sees Toto, Toto sees Chronos). React Flow's
  // `getNodes()` is stable across renders for this purpose.
  const { getNodes } = useReactFlow();

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

  // Which backbone this node is wired to. Derived from the node id at
  // mount time (frontends don't usually rename their own id, so this is
  // stable for the lifetime of the node — no useMemo needed).
  const backbone: Backbone = backboneFromId(id);
  const modelLabel = BACKBONE_INFO[backbone].label;
  const modelBlurb = BACKBONE_INFO[backbone].blurb;

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
  // chart reappears after F5. We also rehydrate the OTHER backbone's
  // forecasts here in the same pass (cheap — one query, both analyst
  // names checked) so the overlay survives reload too.
  const [rehydrated, setRehydrated] = useState<Record<string, ForecastPayload>>({});
  const [overlayRehydrated, setOverlayRehydrated] = useState<Record<string, ForecastPayload>>({});
  const flowIdNum = currentFlowId != null ? Number(currentFlowId) : null;
  // Backbone → wiki analyst-name aliases. The slug after
  // normalize_analyst_name strips `_agent` and the random suffix, then
  // title-cases. `forecaster_xxxxxx` → "Forecaster"; `toto_forecaster_xxxxxx`
  // → "Toto Forecaster". The legacy "Time Series Forecaster" alias is
  // kept for pre-rename wiki rows.
  const ownNames = backbone === 'toto2'
    ? ['toto forecaster']
    : ['forecaster', 'time series forecaster'];
  const otherNames = backbone === 'toto2'
    ? ['forecaster', 'time series forecaster']
    : ['toto forecaster'];
  // Stable join so React's dep array on the effect below doesn't see a
  // new array literal every render and re-fire the fetch.
  const ownNamesKey = ownNames.join('|');
  const otherNamesKey = otherNames.join('|');
  useEffect(() => {
    if (flowIdNum == null) return;
    let cancelled = false;
    getFlowMemory(flowIdNum)
      .then((mem) => {
        if (cancelled) return;
        const ownNext: Record<string, ForecastPayload> = {};
        const otherNext: Record<string, ForecastPayload> = {};
        const matches = (a: string | undefined, names: string[]) =>
          !!a && names.includes(a.toLowerCase());
        for (const t of mem.tickers || []) {
          for (const row of (t.analysts || [])) {
            if (matches(row.analyst, ownNames)) {
              const fc = extractForecast(row?.reasoning);
              if (fc) ownNext[t.ticker] = fc;
            } else if (matches(row.analyst, otherNames)) {
              const fc = extractForecast(row?.reasoning);
              if (fc) otherNext[t.ticker] = fc;
            }
          }
        }
        setRehydrated(ownNext);
        setOverlayRehydrated(otherNext);
      })
      .catch(() => {
        // Fail-open: no rehydration is the placeholder state, not an error.
      });
    return () => {
      cancelled = true;
    };
  }, [flowIdNum, ownNamesKey, otherNamesKey]);

  // Prefer live runtime data — it's always fresher than what the wiki
  // has from a prior run. Fall back to rehydrated wiki data when the
  // runtime stream is empty (post-reload, or before the first run on a
  // restored flow). Merge per-ticker so a partial new run still surfaces
  // the latest forecast for tickers it already produced.
  const forecastsByTicker = useMemo<Record<string, ForecastPayload>>(() => {
    return { ...rehydrated, ...runtimeForecasts };
  }, [rehydrated, runtimeForecasts]);

  // Find the OTHER forecaster node on the canvas (different backbone) and
  // pull its forecasts in two ways:
  //
  //   * Runtime: read its node-context messages and parse the fence — same
  //     idiom this node uses for itself but via a different node id.
  //   * Rehydrated: already loaded above in the wiki query that fetched both
  //     analyst names.
  //
  // The runtime path catches the case where the user just clicked Refresh
  // on the OTHER forecaster — that node's messages update, this node's
  // overlay updates without needing a wiki round-trip.
  // Use xyflow's `useNodes()` (not `getNodes()` from useReactFlow) so this
  // memo re-fires when nodes are added/removed on the canvas. `getNodes`
  // is a stable function reference — depending on it in useMemo means the
  // memo is computed once at mount and never updates if the user drops
  // the *other* forecaster later. `useNodes()` returns the live array, so
  // any structural change drives a re-render and re-computes this.
  const liveNodes = useNodes();
  const otherForecasterId = useMemo<string | null>(() => {
    for (const n of liveNodes) {
      if (n.type !== 'forecaster-node') continue;
      if (n.id === id) continue;
      if (backboneFromId(n.id) === backbone) continue;  // same backbone, not an overlay candidate
      return n.id;
    }
    return null;
  }, [liveNodes, id, backbone]);
  const otherBackbone: Backbone = backbone === 'toto2' ? 'chronos2' : 'toto2';
  const otherForecasterRuntime = useMemo<Record<string, ForecastPayload>>(() => {
    if (!otherForecasterId) return {};
    const otherData = agentNodeData[otherForecasterId];
    if (!otherData) return {};
    const out: Record<string, ForecastPayload> = {};
    const msgs = otherData.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (!m.ticker) continue;
      if (out[m.ticker]) continue;
      const fc = extractForecast(m.analysis?.[m.ticker]);
      if (fc) out[m.ticker] = fc;
    }
    return out;
    // agentNodeData is recreated each render by getAgentNodeDataForFlow;
    // depending on `agentNodeData[otherForecasterId]?.lastUpdated` would be
    // marginally tighter but isn't worth the readability cost.
  }, [agentNodeData, otherForecasterId]);
  const overlayByTicker = useMemo<Record<string, ForecastPayload>>(() => {
    if (!otherForecasterId) return {};
    return { ...overlayRehydrated, ...otherForecasterRuntime };
  }, [otherForecasterId, overlayRehydrated, otherForecasterRuntime]);

  const tickers = useMemo(() => Object.keys(forecastsByTicker).sort(), [forecastsByTicker]);
  const [activeTicker, setActiveTicker] = useState<string | null>(null);
  useEffect(() => {
    if (!activeTicker && tickers.length) setActiveTicker(tickers[0]);
    if (activeTicker && tickers.length && !tickers.includes(activeTicker)) setActiveTicker(tickers[0] ?? null);
  }, [tickers, activeTicker]);

  const activeForecast = activeTicker ? forecastsByTicker[activeTicker] : null;
  // Overlay for the currently-active ticker. Skipped (undefined) when:
  //   - no other forecaster node on canvas
  //   - other forecaster hasn't produced for this ticker yet
  //   - other forecaster's bar frequency differs (overlay would be misleading)
  const activeOverlay = (() => {
    if (!activeTicker || !otherForecasterId) return undefined;
    const o = overlayByTicker[activeTicker];
    if (!o) return undefined;
    if (activeForecast && o.frequency && activeForecast.frequency && o.frequency !== activeForecast.frequency) {
      // Different frequencies — overlay would compare 5-min bars vs daily;
      // x-axis would lie. Skip and surface a notice in the dialog instead.
      return undefined;
    }
    return o;
  })();
  const overlayFreqMismatch = !!(activeForecast && activeTicker && otherForecasterId
    && overlayByTicker[activeTicker]
    && overlayByTicker[activeTicker].frequency
    && activeForecast.frequency
    && overlayByTicker[activeTicker].frequency !== activeForecast.frequency);
  const overlayLabel = BACKBONE_INFO[otherBackbone].label;

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
        agent_id: id,  // backend dispatches to the right backbone via prefix
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

            {/* Backbone settings — bar frequency picker + context/prediction
                lengths. Length units adapt to the frequency: 256 + 10 at
                'Hourly' = 256 hours of context, 10-hour forecast. The
                Daily path runs through the cached provider chain; intraday
                hits yfinance directly with hard period caps per interval
                (1m≤7d, 5m≤60d, 1h≤730d). Same UI for Chronos and Toto —
                both honour the same context/prediction/frequency contract. */}
            <TooltipProvider>
              <div className="text-subtitle text-primary flex items-center gap-1 mt-1">{modelLabel} Settings</div>
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
                    Past bars {modelLabel} reads as context. Range {CTX_MIN}–{CTX_MAX}.
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
                        : `Refresh forecast for ${tickers.length} ticker${tickers.length === 1 ? '' : 's'} — runs ${modelLabel} only, no other agents.`}
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <span
                  className="text-[10px] uppercase tracking-wide text-muted-foreground"
                  title={modelBlurb}
                >
                  {modelLabel}
                </span>
              </div>
            </div>
            {activeForecast ? (
              <>
                {/* Inline preview — click-through to detail dialog.
                    When another forecaster is on the canvas, its fan is
                    overlaid (dashed q50, lighter band) and a small chip
                    in the top-left identifies it. Mismatched bar frequency
                    surfaces a separate amber chip. */}
                <button
                  type="button"
                  onClick={() => setIsDetailOpen(true)}
                  title="Open detailed forecast"
                  className="group relative w-full rounded border border-border bg-node/40 hover:border-primary/40 transition-colors"
                >
                  <InlineFanChart forecast={activeForecast} overlay={activeOverlay} />
                  {activeOverlay && (
                    <span
                      className="absolute top-1 left-1 px-1 py-0.5 rounded bg-card/80 text-[8px] uppercase tracking-wide text-muted-foreground border border-border pointer-events-none"
                      title={`Dashed line: ${overlayLabel} forecast for this ticker`}
                    >
                      +{overlayLabel}
                    </span>
                  )}
                  {overlayFreqMismatch && !activeOverlay && (
                    <span
                      className="absolute top-1 left-1 px-1 py-0.5 rounded bg-amber-500/15 text-[8px] uppercase tracking-wide text-amber-500 border border-amber-500/40 pointer-events-none"
                      title={`${overlayLabel} is on a different bar frequency — overlay suppressed`}
                    >
                      ⚠ {overlayLabel} freq differs
                    </span>
                  )}
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
          overlayByTicker={overlayByTicker}
          overlayLabel={overlayLabel}
          overlayFreqMismatch={overlayFreqMismatch}
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
            modelLabel,
          }}
        />
      </CardContent>
    </NodeShell>
  );
}

// --- Inline fan chart (compact, on node body) -----------------------------

function InlineFanChart({
  forecast, overlay,
}: { forecast: ForecastPayload; overlay?: ForecastPayload }) {
  const { history, q10, q25, q50, q75, q90 } = forecast;
  const W = 220;
  const H = 90;
  const padX = 4;
  const padY = 6;

  // Y-axis spans BOTH fans' values when an overlay is present, so the two
  // q50 lines and both fans sit in the same coordinate space. History is
  // shared (it's the same price series) — we only take it from the primary.
  const overlayPoints = overlay
    ? [...overlay.q10, ...overlay.q50, ...overlay.q90]
    : [];
  const all = [...history, ...q10, ...q50, ...q90, ...overlayPoints];
  const minV = Math.min(...all);
  const maxV = Math.max(...all);
  const yPad = (maxV - minV) * 0.04 || 1;
  const yMin = minV - yPad;
  const yMax = maxV + yPad;
  const yRange = yMax - yMin || 1;

  // X-axis is shared; overlay's forecast length may differ. We always
  // anchor on the primary's history + horizon for the chart bounds and
  // trim the overlay to the primary's horizon if it's longer. Mismatched
  // horizons would otherwise stretch one fan to a wrong x-step.
  const horizonLen = q50.length;
  const overlayTrim = overlay ? Math.min(overlay.q50.length, horizonLen) : 0;
  const total = history.length + horizonLen;
  const xAt = (i: number) => padX + (i / (total - 1)) * (W - 2 * padX);
  const yAt = (v: number) => padY + (1 - (v - yMin) / yRange) * (H - 2 * padY);

  const histPath = history.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i).toFixed(2)} ${yAt(v).toFixed(2)}`).join(' ');
  const fcStart = history.length - 1;
  const lastHist = history[history.length - 1];

  const bandPath = (lo: number[], hi: number[], n: number) => {
    const loSlice = lo.slice(0, n);
    const hiSlice = hi.slice(0, n);
    const top = [lastHist, ...hiSlice].map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAt(v).toFixed(2)}`).join(' ');
    const bot = [lastHist, ...loSlice]
      .slice()
      .reverse()
      .map((v, i, arr) => `L ${xAt(fcStart + (arr.length - 1 - i)).toFixed(2)} ${yAt(v).toFixed(2)}`)
      .join(' ');
    return `${top} ${bot} Z`;
  };
  const linePath = (vals: number[], n: number) => {
    const pts = [lastHist, ...vals.slice(0, n)];
    return pts.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAt(v).toFixed(2)}`).join(' ');
  };

  const t = tone(forecast);
  const stroke = TONE_STROKE[t];
  const sepX = xAt(fcStart);

  // Overlay tone — also colored by the OTHER backbone's own direction so
  // disagreement is visible at a glance: if Chronos is green-q50-up and
  // Toto is red-q50-down, the chart literally shows one line trending up
  // and one trending down. Same alpha scaling for the bands to keep the
  // primary's reading dominant.
  const ot = overlay ? tone(overlay) : 'neu';
  const overlayStroke = overlay ? TONE_STROKE[ot] : undefined;

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} className="block">
      {/* Overlay rendered FIRST so the primary fan paints on top. */}
      {overlay && overlay.q50.length > 0 && (
        <>
          <path d={bandPath(overlay.q10, overlay.q90, overlayTrim)} fill={TONE_FILL_OUTER[ot]} opacity={0.5} stroke="none" />
          <path d={bandPath(overlay.q25, overlay.q75, overlayTrim)} fill={TONE_FILL_INNER[ot]} opacity={0.5} stroke="none" />
          <path d={linePath(overlay.q50, overlayTrim)} fill="none" stroke={overlayStroke} strokeWidth={1.6} strokeDasharray="3,2" opacity={0.85} />
        </>
      )}
      <path d={bandPath(q10, q90, horizonLen)} fill={TONE_FILL_OUTER[t]} stroke="none" />
      <path d={bandPath(q25, q75, horizonLen)} fill={TONE_FILL_INNER[t]} stroke="none" />
      <path d={histPath} fill="none" stroke="currentColor" strokeWidth={1.2} opacity={0.55} />
      <path d={linePath(q50, horizonLen)} fill="none" stroke={stroke} strokeWidth={1.6} />
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
  modelLabel: string;   // "Chronos-2" or "Toto-2.0" — drives dialog headers
}

interface DetailDialogProps {
  isOpen: boolean;
  onOpenChange: (v: boolean) => void;
  tickers: string[];
  activeTicker: string | null;
  onTickerChange: (t: string) => void;
  forecastsByTicker: Record<string, ForecastPayload>;
  // Optional overlay payload — the OTHER forecaster's fan for the same
  // tickers. Drawn as a dashed-q50 + lighter fan behind the primary.
  // Missing entries = no overlay for that ticker (other forecaster
  // hasn't produced it yet, or isn't on the canvas).
  overlayByTicker?: Record<string, ForecastPayload>;
  overlayLabel?: string;             // "Toto-2.0" when viewing Chronos, etc.
  overlayFreqMismatch?: boolean;     // skip overlay + show warning when true
  controls: ChronosControls;
}

// Per-field stale state. The dropdown for a stale field gets a yellow
// ring + ⚠, only the fields that actually drifted are marked. Context_len
// is intentionally not part of staleness (yfinance hard-caps intraday
// history, so requested vs returned bar count rarely matches one-to-one
// even on a fresh run — would false-positive every time).
type StaleInfo = {
  any: boolean;
  freq: boolean;
  pred: boolean;
};

function computeStale(forecast: ForecastPayload | null, controls: ChronosControls): StaleInfo {
  if (!forecast) return { any: false, freq: false, pred: false };
  const freq = (forecast.frequency ?? 'day') !== controls.barFrequency;
  const pred = forecast.horizon_days !== controls.predictionLen;
  return { any: freq || pred, freq, pred };
}

function ForecastDetailDialog({
  isOpen, onOpenChange, tickers, activeTicker, onTickerChange,
  forecastsByTicker, overlayByTicker, overlayLabel, overlayFreqMismatch,
  controls,
}: DetailDialogProps) {
  const forecast = activeTicker ? forecastsByTicker[activeTicker] : null;
  const overlay = activeTicker && overlayByTicker ? overlayByTicker[activeTicker] : undefined;
  // Mismatched bar frequency makes the overlay misleading (x-axis would
  // compare daily bars to 5-min bars). Skip the overlay paint in that
  // case; the warning chip below tells the user why.
  const effectiveOverlay = overlayFreqMismatch ? undefined : overlay;
  // Stale-chart detection: the chart's axes + cell labels reflect whatever
  // the LAST RUN produced (frequency, prediction_len). If the user has since
  // changed those settings in the dropdowns above, the chart is out of date
  // — and there's no way to tell visually, because the labels still say
  // whatever the prior run was. Surface this gap explicitly so the user knows
  // a Refresh is needed.
  const stale = computeStale(forecast, controls);
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
            <span>{controls.modelLabel} Forecast{activeTicker ? ` · ${activeTicker}` : ''}</span>
          </DialogTitle>
        </DialogHeader>
        <DialogChronosSettings controls={controls} stale={stale} />
        {stale.any && forecast && (
          <StaleChartBanner
            currentFreq={controls.barFrequency}
            currentPred={controls.predictionLen}
            chartFreq={(forecast.frequency ?? 'day') as BarFrequency}
            chartPred={forecast.horizon_days}
          />
        )}
        {forecast ? (
          // (3) chart fade when stale — opacity + grayscale + pointer-events
          // off so a user trying to read the labels gets a visual "this is
          // outdated" rather than a clean chart that lies. CSS transition so
          // it fades back smoothly after a Refresh completes.
          <div
            className={cn(
              "flex-1 min-h-0 transition-[opacity,filter] duration-300",
              stale.any && "opacity-50 grayscale pointer-events-none select-none",
            )}
            aria-busy={stale.any}
          >
            <DetailBody
              forecast={forecast}
              overlay={effectiveOverlay}
              primaryLabel={controls.modelLabel}
              overlayLabel={overlayLabel}
              overlayFreqMismatch={overlayFreqMismatch}
              tickers={tickers}
              activeTicker={activeTicker}
              onTickerChange={onTickerChange}
            />
          </div>
        ) : (
          <div className="p-6 text-sm text-muted-foreground text-center">No forecast to display.</div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// Inline banner shown above the chart when the user's Settings have drifted
// from the run that produced the chart. The chart x-axis ticks and "N-d Q10"
// metric cells reflect *forecast.frequency* and *forecast.horizon_days* — not
// the dropdowns above. Without this, picking "1-Minute" in the dropdown
// silently leaves the chart on its previous Daily axes and the user sees
// inconsistent labels with no signal that a Refresh is needed.
function StaleChartBanner({
  currentFreq, currentPred, chartFreq, chartPred,
}: {
  currentFreq: BarFrequency;
  currentPred: number;
  chartFreq: BarFrequency;
  chartPred: number;
}) {
  const freqLabel = (f: BarFrequency) => FREQ_OPTIONS.find((o) => o.value === f)?.label ?? f;
  return (
    <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-500 flex items-start gap-2">
      <span aria-hidden>⚠</span>
      <span>
        <span className="font-medium">Chart is stale.</span>{' '}
        Settings show <span className="font-medium">{freqLabel(currentFreq)}</span> ·{' '}
        <span className="font-medium">{currentPred}</span> prediction bars, but the chart was rendered from{' '}
        <span className="font-medium">{freqLabel(chartFreq)}</span> ·{' '}
        <span className="font-medium">{chartPred}</span>. Click{' '}
        <span className="font-medium">Refresh forecast</span> to regenerate with the current settings.
      </span>
    </div>
  );
}

// Module-level clamps so the dialog and node-body inputs apply the same
// bounds. CTX_MIN/MAX etc come from the file's top — Chronos-2 model card.
const clampCtxValue = (n: number) => Math.max(CTX_MIN, Math.min(CTX_MAX, Math.round(n) || CTX_DEFAULT));
const clampPredValue = (n: number) => Math.max(PRED_MIN, Math.min(PRED_MAX, Math.round(n) || PRED_DEFAULT));

function DialogChronosSettings({ controls, stale }: { controls: ChronosControls; stale?: StaleInfo }) {
  const {
    barFrequency, setBarFrequency,
    contextLen, setContextLen,
    predictionLen, setPredictionLen,
    onRefresh, isRefreshing, canRefresh, tickerCount,
  } = controls;
  const activeFreq = FREQ_OPTIONS.find((o) => o.value === barFrequency) ?? FREQ_OPTIONS[0];
  // (1) Per-field stale ring: only the dropdowns whose value drifted from
  // the rendered chart get the amber ring + ⚠. Context-len is excluded
  // from staleness (see computeStale).
  const freqStale = !!stale?.freq;
  const predStale = !!stale?.pred;
  const anyStale = !!stale?.any;
  // (3) Amber Refresh button when stale: same control, but swaps from
  // outline → amber border + amber text + filled icon. Disabled (during
  // refresh / before tickers exist) still wins visually.
  const refreshClasses = cn(
    'flex items-center gap-2 px-3 py-1 rounded border text-sm transition-colors',
    'disabled:opacity-40 disabled:cursor-not-allowed',
    anyStale && !isRefreshing && canRefresh
      ? 'border-amber-500/60 text-amber-500 bg-amber-500/10 hover:bg-amber-500/15 hover:border-amber-500'
      : 'border-border hover:border-primary/40 hover:text-foreground',
  );
  // Per-control wrapper that adds the amber ring + the small ⚠ tag next to
  // the label when stale. Mirrors how StaleChartBanner phrases the issue.
  const FieldWrap = ({ label, isStale, children, tooltip }: {
    label: string;
    isStale: boolean;
    children: React.ReactNode;
    tooltip: React.ReactNode;
  }) => (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="flex flex-col gap-1">
          <label className="text-xs uppercase tracking-wide text-muted-foreground flex items-center gap-1">
            <span>{label}</span>
            {isStale && (
              <span
                aria-label="Setting changed since last refresh"
                className="text-amber-500 normal-case tracking-normal text-[10px] font-medium"
              >⚠ pending</span>
            )}
          </label>
          <div
            className={cn(
              "rounded transition-shadow",
              isStale && "ring-1 ring-amber-500/60",
            )}
          >
            {children}
          </div>
        </div>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-[320px] text-xs">
        {tooltip}
      </TooltipContent>
    </Tooltip>
  );
  return (
    <TooltipProvider>
      <div className="rounded border border-border bg-node/40 p-3">
        <div className="flex items-center justify-between mb-2">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">{controls.modelLabel} Settings</div>
          {/* Refresh-this-node-only — same handler the node body uses. */}
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={onRefresh}
                disabled={isRefreshing || !canRefresh}
                className={refreshClasses}
                aria-label="Refresh forecast"
              >
                {isRefreshing ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="h-3.5 w-3.5" />
                )}
                <span>{isRefreshing ? 'Refreshing…' : (anyStale ? 'Refresh forecast — pending changes' : 'Refresh forecast')}</span>
              </button>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-[320px] text-xs">
              {!canRefresh
                ? 'Run the flow once to set the tickers, then refresh updates only this node.'
                : `Re-runs ${controls.modelLabel} only on the current ${tickerCount} ticker${tickerCount === 1 ? '' : 's'} — no other agents, no PM. Updates the chart in place.`}
            </TooltipContent>
          </Tooltip>
        </div>
        <div className="grid grid-cols-3 gap-3">
          <FieldWrap label="Bar frequency" isStale={freqStale} tooltip={activeFreq.hint}>
            <select
              value={barFrequency}
              onChange={(e) => setBarFrequency(e.target.value as BarFrequency)}
              className="w-full rounded border border-border bg-node/60 px-3 py-1.5 text-base text-foreground focus:outline-none focus:border-primary/50"
            >
              {FREQ_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </FieldWrap>
          <FieldWrap
            label="Context bars"
            isStale={false}
            tooltip={
              <>
                Past bars {controls.modelLabel} reads as context. Range {CTX_MIN}–{CTX_MAX}.
                More context generally improves the forecast but costs a slower
                forward pass. Intraday frequencies clip to available history
                (1m≤7d, 5m≤60d, 1h≤730d).
              </>
            }
          >
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
          </FieldWrap>
          <FieldWrap
            label="Prediction bars"
            isStale={predStale}
            tooltip={
              <>
                Bars to forecast forward. Range {PRED_MIN}–{PRED_MAX}.
                Longer horizons produce wider fans (lower confidence) — Chronos
                is most accurate at the short end.
              </>
            }
          >
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
          </FieldWrap>
        </div>
      </div>
    </TooltipProvider>
  );
}

function DetailBody({
  forecast, overlay, primaryLabel, overlayLabel, overlayFreqMismatch,
  tickers, activeTicker, onTickerChange,
}: {
  forecast: ForecastPayload;
  overlay?: ForecastPayload;
  primaryLabel?: string;
  overlayLabel?: string;
  overlayFreqMismatch?: boolean;
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
  // Overlay end-of-horizon stats — shown in a parallel row when both
  // fans are available so the user can compare numerically, not just
  // visually. We trim to the overlay's actual horizon (capped by primary's).
  const oEndIdx = overlay ? Math.min(overlay.q50.length, forecast.q50.length) - 1 : -1;
  const oQ10 = overlay && oEndIdx >= 0 ? overlay.q10[oEndIdx] : null;
  const oQ50 = overlay && oEndIdx >= 0 ? overlay.q50[oEndIdx] : null;
  const oQ90 = overlay && oEndIdx >= 0 ? overlay.q90[oEndIdx] : null;

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

      {/* Frequency mismatch — overlay was suppressed because the two
          forecasters are on different bar frequencies. We don't even try
          to overlay 5-min on daily; tell the user why. */}
      {overlayFreqMismatch && (
        <div className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-500">
          ⚠ {overlayLabel ?? 'Other forecaster'} is on a different bar frequency — overlay suppressed.
          Match Bar Frequency on both forecaster nodes to compare them in the same chart.
        </div>
      )}

      {/* Header summary — primary row + (when overlay exists) a parallel
          row with the other backbone's numbers. Same column layout so
          eyes track left-to-right. Swatch in the header row mirrors the
          line style on the chart (solid = primary, dashed = overlay). */}
      <div className="flex flex-col gap-1.5">
        {primaryLabel && (
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground flex items-center gap-2">
            <span className="inline-block w-4 h-0.5 bg-current" />
            <span>{primaryLabel}</span>
          </div>
        )}
        <div className="grid grid-cols-6 gap-2 text-xs tabular-nums">
          <Cell title="Last close" value={`$${last.toFixed(2)}`} />
          <Cell title={`${forecast.horizon_days}-d Q10`} value={fmtPct(endQ10, last)} className="text-red-500/90" sub={`$${endQ10.toFixed(2)}`} />
          <Cell title={`${forecast.horizon_days}-d Q50`} value={fmtPct(endQ50, last)} className={TONE_STROKE[t]} sub={`$${endQ50.toFixed(2)}`} />
          <Cell title={`${forecast.horizon_days}-d Q90`} value={fmtPct(endQ90, last)} className="text-emerald-500/90" sub={`$${endQ90.toFixed(2)}`} />
          <Cell title="Confidence (day 1)" value={`${startConf}%`} />
          <Cell title={`Confidence (day ${forecast.horizon_days})`} value={`${endConf}%`} />
        </div>
        {overlay && oQ10 != null && oQ50 != null && oQ90 != null && (
          <>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground flex items-center gap-2 mt-1">
              <span className="inline-block w-4 border-t-2 border-dashed border-current" />
              <span>{overlayLabel ?? 'overlay'}</span>
            </div>
            <div className="grid grid-cols-6 gap-2 text-xs tabular-nums">
              <Cell title="Last close" value={`$${last.toFixed(2)}`} />
              <Cell title={`${overlay.horizon_days}-d Q10`} value={fmtPct(oQ10, last)} className="text-red-500/90" sub={`$${oQ10.toFixed(2)}`} />
              <Cell title={`${overlay.horizon_days}-d Q50`} value={fmtPct(oQ50, last)} className={TONE_STROKE[tone(overlay)]} sub={`$${oQ50.toFixed(2)}`} />
              <Cell title={`${overlay.horizon_days}-d Q90`} value={fmtPct(oQ90, last)} className="text-emerald-500/90" sub={`$${oQ90.toFixed(2)}`} />
              <Cell title="Conf (day 1)" value={`${overlay.confidence[0] ?? 0}%`} />
              <Cell title={`Conf (day ${overlay.horizon_days})`} value={`${overlay.confidence[overlay.confidence.length - 1] ?? 0}%`} />
            </div>
          </>
        )}
      </div>

      <DetailChart
        forecast={forecast}
        overlay={overlay}
        primaryLabel={primaryLabel}
        overlayLabel={overlayLabel}
      />

      {/* Legend / glossary — split into bullet items so each idea reads
          on its own line; bigger text so it's actually readable. */}
      <ul className="text-sm text-muted-foreground leading-snug flex flex-wrap gap-x-6 gap-y-1">
        <li><span className="text-foreground">Inner band</span> — 50% prediction interval (q25–q75)</li>
        <li><span className="text-foreground">Outer band</span> — 80% prediction interval (q10–q90)</li>
        <li><span className="text-foreground">Confidence</span> — derived from fan width; available in the hover tooltip</li>
        {overlay && (
          <li><span className="text-foreground">Dashed line + lighter fan</span> — overlay backbone's q50 / 80% PI</li>
        )}
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

function DetailChart({
  forecast, overlay, primaryLabel, overlayLabel,
}: {
  forecast: ForecastPayload;
  overlay?: ForecastPayload;
  primaryLabel?: string;
  overlayLabel?: string;
}) {
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

  // Overlay length is trimmed to the primary's horizon so both fans share
  // the x-axis. We pad short overlays to the same array length so path
  // construction is symmetric; longer overlays get truncated.
  const overlayLen = overlay ? Math.min(overlay.q50.length, fcN) : 0;

  const innerW = W - padL - padR;
  const xAt = (i: number) => padL + (i / (total - 1)) * innerW;

  // Price y-axis — domain spans every drawn point ACROSS BOTH FANS so
  // they share a common scale. Without this, the overlay would be
  // visually rescaled to the primary's y-range and look misleadingly
  // tight/wide depending on which fan is more uncertain.
  const overlayPts = overlay
    ? [...overlay.q10.slice(0, overlayLen), ...overlay.q50.slice(0, overlayLen), ...overlay.q90.slice(0, overlayLen)]
    : [];
  const allPrice = [...history, ...q10, ...q50, ...q90, ...overlayPts];
  const minP = Math.min(...allPrice);
  const maxP = Math.max(...allPrice);
  const padPrice = (maxP - minP) * 0.04 || 1;
  const yMin = minP - padPrice;
  const yMax = maxP + padPrice;
  const yRange = yMax - yMin || 1;
  const yAtPrice = (v: number) => padTopPrice + (1 - (v - yMin) / yRange) * PRICE_H;

  const t = tone(forecast);
  const stroke = TONE_STROKE[t];
  // Overlay tone — independent of the primary, so an "agreement" run paints
  // both q50 lines in the same color, while a "disagreement" run paints
  // them in opposing colors. The line style (solid vs dashed) tells you
  // which model is which.
  const ot = overlay ? tone(overlay) : 'neu';
  const overlayStroke = overlay ? TONE_STROKE[ot] : undefined;

  const fcStart = histN - 1;
  const lastHist = history[histN - 1];
  const sepX = xAt(fcStart);

  // Paths
  const histPath = history.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i).toFixed(2)} ${yAtPrice(v).toFixed(2)}`).join(' ');

  // bandPath takes an optional `len` so we can trim the overlay fan to the
  // primary's horizon when they differ. Length defaults to fcN (the
  // primary's horizon) when omitted — back-compat with all existing call
  // sites that didn't pass it.
  const bandPath = (lo: number[], hi: number[], len: number = fcN) => {
    const loSlice = lo.slice(0, len);
    const hiSlice = hi.slice(0, len);
    const top = [lastHist, ...hiSlice].map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAtPrice(v).toFixed(2)}`).join(' ');
    const bot = [lastHist, ...loSlice]
      .slice()
      .reverse()
      .map((v, i, arr) => `L ${xAt(fcStart + (arr.length - 1 - i)).toFixed(2)} ${yAtPrice(v).toFixed(2)}`)
      .join(' ');
    return `${top} ${bot} Z`;
  };
  const linePath = (vals: number[], len: number = fcN) => {
    const pts = [lastHist, ...vals.slice(0, len)];
    return pts.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(fcStart + i).toFixed(2)} ${yAtPrice(v).toFixed(2)}`).join(' ');
  };

  const q50Path = linePath(q50);

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

        {/* Overlay fan rendered FIRST so the primary paints on top. Lower
            alpha + dashed q50 stroke distinguish it from the primary. */}
        {overlay && overlayLen > 0 && (
          <>
            <path d={bandPath(overlay.q10, overlay.q90, overlayLen)} fill={TONE_FILL_OUTER[ot]} opacity={0.45} stroke="none" />
            <path d={bandPath(overlay.q25, overlay.q75, overlayLen)} fill={TONE_FILL_INNER[ot]} opacity={0.45} stroke="none" />
            <path d={linePath(overlay.q50, overlayLen)} fill="none" stroke={overlayStroke} strokeWidth={2.0} strokeDasharray="6,4" opacity={0.85} />
          </>
        )}
        {/* Nested bands + median + history (primary) */}
        <path d={bandPath(q10, q90)} fill={TONE_FILL_OUTER[t]} stroke="none" />
        <path d={bandPath(q25, q75)} fill={TONE_FILL_INNER[t]} stroke="none" />
        <path d={histPath} fill="none" stroke="currentColor" strokeWidth={1.2} opacity={0.65} />
        <path d={q50Path} fill="none" stroke={stroke} strokeWidth={2.0} />

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
            {/* Second q50 dot for the overlay backbone at the same step,
                drawn only when the overlay extends that far. Hollow ring so
                primary and overlay markers don't collide visually. */}
            {fcIdx != null && overlay && fcIdx < overlayLen && (
              <circle cx={hoverX} cy={yAtPrice(overlay.q50[fcIdx])} r={4.5} fill="none" stroke={overlayStroke} strokeWidth={1.5} />
            )}
          </>
        )}

        {/* Legend chip — top-right of the price panel. Only when an
            overlay is present (single-fan view stays clean). Solid
            swatch for primary, dashed for overlay, mirroring the line
            styles below. */}
        {overlay && (primaryLabel || overlayLabel) && (
          <g transform={`translate(${W - padR - 12}, ${padTopPrice + 16})`}>
            <rect x={-180} y={-18} width={180} height={42} rx={4} fill="currentColor" opacity={0.05} />
            {/* Primary swatch — solid stroke */}
            <line x1={-170} y1={-6} x2={-150} y2={-6} stroke={stroke} strokeWidth={2.5} />
            <text x={-145} y={-6} dy=".35em" fontSize={Math.max(12, FS_TICK - 4)} fill="currentColor">{primaryLabel ?? 'this'}</text>
            {/* Overlay swatch — dashed */}
            <line x1={-170} y1={12} x2={-150} y2={12} stroke={overlayStroke} strokeWidth={2.5} strokeDasharray="6,4" />
            <text x={-145} y={12} dy=".35em" fontSize={Math.max(12, FS_TICK - 4)} fill="currentColor">{overlayLabel ?? 'overlay'}</text>
          </g>
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
