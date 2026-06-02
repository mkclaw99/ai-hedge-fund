// Custom node for the Jim Simons / Renaissance-style quant analyst.
//
// Two right-side handles instead of the usual single one:
//   * `signal`   → flows to the Portfolio Manager (standard analyst signal)
//   * `strategy` → flows to the Strategy node (recommended StrategyConfig
//                  replaces the manual config when wired)
//
// Independent timer: a cadence dropdown (off / 1min / 5min / 15min / hourly)
// drives the backend `simons_scheduler` — when set, Simons refreshes its
// signals on its own clock without triggering the PM. The PM picks up the
// freshest signal on its next trade tick (or the next full play).
//
// Universe: not self-contained — Simons reads tickers from whichever input
// node is wired upstream (Stock Input or Portfolio Input). Per the user's
// "Pull from connected input node only" choice. The "Refresh now" button
// reads tickers from the same upstream node before firing /simons/refresh.
//
// The signal generation is now LLM-driven via a hypothesis-test-adjudicate
// loop (see src/agents/jim_simons.py for the architecture). The model picker
// below selects which LLM runs the proposer + adjudicator stages. Without
// a model picked, the backend falls into the pinned-default chain (and then
// the pure-numpy fallback if nothing is reachable at all).

import { useReactFlow, type NodeProps } from '@xyflow/react';
import { Brain, ExternalLink, Loader2, Maximize2, RefreshCw, Sigma } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { CardContent } from '@/components/ui/card';
import { ModelSelector } from '@/components/ui/llm-selector';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { getDefaultModel, getModels, LanguageModel } from '@/data/models';
import { getNodeInternalState, useNodeState } from '@/hooks/use-node-state';
import { cn } from '@/lib/utils';
import { getFlowMemory } from '@/services/memory-api';
import { fireSimonsTick, refreshSimons, type SimonsRecommendedStrategy, type SimonsSignal } from '@/services/simons-api';
import { type JimSimonsNode } from '../types';
import { getStatusColor } from '../utils';
import { ThinkingBudgetField } from './agent-node';
import { NodeShell } from './node-shell';
import { SimonsTraceDialog } from './simons-trace-dialog';

// Cadence + bar frequency vocabularies — kept in lock-step with the backend
// so a UI label change here doesn't silently fall out of sync with the
// scheduler's _INTERVALS / _VALID_FREQUENCIES.
type SimonsCadence = 'off' | '1min' | '5min' | '15min' | 'hourly';
const CADENCE_OPTIONS: { value: SimonsCadence; label: string; hint: string }[] = [
  { value: 'off',    label: 'Off (manual only)', hint: "Simons only fires when the flow is played. No independent clock." },
  { value: '1min',   label: 'Every minute',      hint: "Truest to Medallion's intraday cadence. Requires the input bar frequency to be 1-Minute too — and rapid yfinance calls." },
  { value: '5min',   label: 'Every 5 minutes',   hint: "Medallion's intraday-signal midpoint. Sensible default for serious quant use." },
  { value: '15min',  label: 'Every 15 minutes',  hint: "Quieter cadence — fewer ticks, signals stay fresh enough for a 5-min bar PM tick." },
  { value: 'hourly', label: 'Hourly',            hint: "Closest analogue to the public RIEF fund's cadence. Cheapest." },
];

type BarFrequency = 'day' | 'hour' | '5min' | '1min';
const FREQ_OPTIONS: { value: BarFrequency; label: string; hint: string }[] = [
  { value: 'day',   label: 'Daily',     hint: "One bar per trading day. Cheap, low-noise, slow signal cadence." },
  { value: 'hour',  label: 'Hourly',    hint: "One bar per trading hour (~7/day). Good middle ground." },
  { value: '5min',  label: '5-Minute',  hint: "78 bars/day. Renaissance's intraday-seasonality sweet spot." },
  { value: '1min',  label: '1-Minute',  hint: "390 bars/day. Closest to high-frequency mode. Heaviest yfinance load." },
];

// Module constants. Lookback is the rolling window over which z-score, vol,
// and RS are computed; defaults to 20 bars (matches src/agents/jim_simons.py).
const LOOKBACK_MIN = 10;
const LOOKBACK_MAX = 500;
const LOOKBACK_DEFAULT = 20;

export function JimSimonsNode({
  data,
  selected,
  id,
  isConnectable,
}: NodeProps<JimSimonsNode>) {
  const { currentFlowId } = useFlowContext();
  const { getAgentNodeDataForFlow, setAgentModel, getAgentModel } = useNodeContext();
  const { getNodes, getEdges } = useReactFlow();

  // Standard status display, same shape every analyst uses so the play /
  // run-progress overlays light up correctly.
  const agentNodeData = getAgentNodeDataForFlow(currentFlowId?.toString() || null);
  const nodeData = agentNodeData[id] || {
    status: 'IDLE', ticker: null, message: '', messages: [], lastUpdated: 0,
  };
  const status = nodeData.status;
  const isInProgress = status === 'IN_PROGRESS';

  // Persisted config — same useNodeState pattern as forecaster-node so the
  // values survive flow reload and the scheduler can read them out of
  // ``flow.nodes[*].data.internal_state`` on the backend.
  const [cadence, setCadence] = useNodeState<SimonsCadence>(id, 'simonsSchedule', 'off');
  const [barFrequency, setBarFrequency] = useNodeState<BarFrequency>(id, 'simonsBarFrequency', 'day');
  const [lookbackBars, setLookbackBars] = useNodeState<number>(id, 'simonsLookbackBars', LOOKBACK_DEFAULT);
  const clampLookback = (n: number) => Math.max(LOOKBACK_MIN, Math.min(LOOKBACK_MAX, Math.round(n) || LOOKBACK_DEFAULT));

  // LLM picker for the hypothesis loop. Same shape agent-node + PM use
  // (selectedModel persisted via useNodeState, availableModels plain useState
  // per PR #110). The auto-seed on a fresh node falls in below.
  const [selectedModel, setSelectedModel] = useNodeState<LanguageModel | null>(id, 'selectedModel', null);
  const [availableModels, setAvailableModels] = useState<LanguageModel[]>([]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [models, defaultModel] = await Promise.all([getModels(), getDefaultModel()]);
        if (cancelled) return;
        setAvailableModels(models);
        if (!defaultModel) return;
        const persisted = getNodeInternalState(id);
        if (persisted && persisted.selectedModel) return;
        setSelectedModel(defaultModel);
      } catch (e) {
        console.error('Simons: failed to load models', e);
      }
    })();
    return () => { cancelled = true; };
  }, [id, setSelectedModel]);

  // Push the selected model into nodeContext so the run assembler picks it
  // up in `agent_models` — same pattern PM uses. Stale-comparison so we don't
  // dispatch on every render.
  useEffect(() => {
    const fid = currentFlowId?.toString() || null;
    const current = getAgentModel(fid, id);
    if (selectedModel !== current) setAgentModel(fid, id, selectedModel);
  }, [selectedModel, id, currentFlowId, setAgentModel, getAgentModel]);

  // Trace dialog state. Opens when the user clicks a per-ticker chip or the
  // Maximize button next to the Signals header.
  const [isTraceOpen, setIsTraceOpen] = useState(false);
  const [activeTraceTicker, setActiveTraceTicker] = useState<string | null>(null);

  // Local-only state — the freshest signals we just pulled and the
  // recommended strategy mirror. These also survive scheduler ticks via
  // the wiki rehydration below.
  const [liveSignals, setLiveSignals] = useState<Record<string, SimonsSignal>>({});
  const [recommendedStrategy, setRecommendedStrategy] = useNodeState<SimonsRecommendedStrategy | null>(id, 'recommendedStrategy', null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [lastRefreshAt, setLastRefreshAt] = useNodeState<string | null>(id, 'lastRefreshAt', null);

  // Sanity warning: 1-min cadence with daily bar frequency is a bug-shaped
  // misconfiguration (one stale signal every minute). Inline so the user
  // sees it next to the controls that caused it. Doesn't block — Simons
  // still runs, just produces a notably-stale signal.
  const cadenceFreqMismatch = useMemo(() => {
    if (cadence === '1min' && barFrequency !== '1min') return '1-min cadence with non-1-min bars produces stale signals every tick — consider matching them.';
    if (cadence === '5min' && barFrequency === 'day') return "5-min cadence with daily bars: Simons will re-evaluate the same daily close until tomorrow's bar lands.";
    return null;
  }, [cadence, barFrequency]);

  // Walk the canvas to figure out (a) which input node feeds Simons (so
  // we can read tickers from it), and (b) whether Simons is wired to a
  // Strategy node (so we can show "Driving Strategy" in the UI). Both
  // are reactive — adding/removing edges re-renders the readouts.
  const upstreamTickers = useMemo<string[]>(() => {
    const edges = getEdges();
    const nodes = getNodes();
    const incomingSources = new Set(edges.filter(e => e.target === id).map(e => e.source));
    const seen = new Set<string>();
    const out: string[] = [];
    for (const n of nodes) {
      if (!incomingSources.has(n.id)) continue;
      const state = (getNodeInternalState(n.id) as any) || {};
      if (n.type === 'stock-analyzer-node' && typeof state.tickers === 'string') {
        for (const t of state.tickers.split(',')) {
          const upper = t.trim().toUpperCase();
          if (upper && !seen.has(upper)) { seen.add(upper); out.push(upper); }
        }
      } else if (n.type === 'portfolio-start-node' && Array.isArray(state.positions)) {
        for (const pos of state.positions) {
          const upper = String(pos?.ticker || '').trim().toUpperCase();
          if (upper && !seen.has(upper)) { seen.add(upper); out.push(upper); }
        }
      }
    }
    return out;
    // Recompute on every render since edges/nodes/state can change at any
    // time — the array build is cheap (small N), no memo dep tracking pain.
  }, [getEdges, getNodes, id, nodeData.lastUpdated]);

  const wiringStatus = useMemo(() => {
    const edges = getEdges();
    const nodes = getNodes();
    const outgoing = edges.filter(e => e.source === id);
    const downstreamTypes = new Set(
      outgoing.map(e => nodes.find(n => n.id === e.target)?.type).filter(Boolean) as string[],
    );
    return {
      drivesPM: downstreamTypes.has('portfolio-manager-node'),
      drivesStrategy: downstreamTypes.has('strategy-node'),
    };
  }, [getEdges, getNodes, id, nodeData.lastUpdated]);

  // Rehydrate from the flow's wiki on mount so Simons-recommended strategy
  // and last signals don't disappear on page reload. Falls back gracefully
  // when there's no flow_id (un-saved flow) or no Simons rows in the wiki.
  const flowIdNum = currentFlowId != null ? Number(currentFlowId) : null;
  useEffect(() => {
    if (flowIdNum == null) return;
    let cancelled = false;
    getFlowMemory(flowIdNum)
      .then((mem) => {
        if (cancelled) return;
        const next: Record<string, SimonsSignal> = {};
        for (const t of (mem.tickers || [])) {
          const row = (t.analysts || []).find(
            (a: any) => a.analyst?.toLowerCase() === 'jim simons',
          );
          if (!row) continue;
          next[t.ticker] = {
            signal: row.signal as 'bullish' | 'bearish' | 'neutral',
            confidence: Number(row.confidence) || 0,
            reasoning: row.reasoning || '',
            simons_trace: extractTraceFromReasoning(row.reasoning || ''),
          };
        }
        if (Object.keys(next).length) setLiveSignals(next);
      })
      .catch(() => {
        // Fail-open: no rehydrate is the placeholder state, not an error.
      });
    return () => { cancelled = true; };
  }, [flowIdNum]);

  const handleRefresh = useCallback(async () => {
    if (isRefreshing) return;
    setRefreshError(null);
    if (upstreamTickers.length === 0) {
      setRefreshError('No upstream input — connect a Stock Input or Portfolio Input first.');
      return;
    }
    setIsRefreshing(true);
    try {
      // Read the per-node thinking budget out of useNodeState bag directly
      // (the ThinkingBudgetField below reads/writes it via useNodeState too).
      const persisted = (getNodeInternalState(id) as any) || {};
      const thinkingBudget = persisted.thinkingBudget;
      const resp = await refreshSimons({
        tickers: upstreamTickers,
        flow_id: flowIdNum ?? undefined,
        simons_cadence: cadence,
        simons_bar_frequency: barFrequency,
        simons_lookback_bars: lookbackBars,
        model_name: selectedModel?.model_name,
        model_provider: selectedModel?.provider,
        thinking_budget: ['off', 'low', 'medium', 'high'].includes(thinkingBudget) ? thinkingBudget : undefined,
      });
      if (resp.error) {
        setRefreshError(resp.error);
      } else {
        setLiveSignals(resp.signals || {});
        setRecommendedStrategy(resp.recommended_strategy || null);
        setLastRefreshAt(new Date().toISOString());
      }
    } catch (e: any) {
      setRefreshError(e?.message || 'Refresh failed.');
    } finally {
      setIsRefreshing(false);
    }
  }, [
    isRefreshing, upstreamTickers, flowIdNum, cadence, barFrequency, lookbackBars,
    selectedModel, id, setRecommendedStrategy, setLastRefreshAt,
  ]);

  // Fire a backend scheduled-tick on demand — useful when the user wants
  // to see the wiki + persistence path fire (the /refresh path bypasses
  // the scheduler bookkeeping). Only meaningful when the flow is saved.
  const handleManualTick = useCallback(async () => {
    if (flowIdNum == null) {
      setRefreshError('Save the flow first — tick fires against a flow id.');
      return;
    }
    setRefreshError(null);
    setIsRefreshing(true);
    try {
      const resp = await fireSimonsTick(flowIdNum);
      if (!resp.ok && resp.reason) setRefreshError(`Tick skipped: ${resp.reason}`);
      // The wiki write happens server-side — pull memory to surface it.
      const mem = await getFlowMemory(flowIdNum);
      const next: Record<string, SimonsSignal> = {};
      for (const t of (mem.tickers || [])) {
        const row = (t.analysts || []).find(
          (a: any) => a.analyst?.toLowerCase() === 'jim simons',
        );
        if (!row) continue;
        next[t.ticker] = {
          signal: row.signal as 'bullish' | 'bearish' | 'neutral',
          confidence: Number(row.confidence) || 0,
          reasoning: row.reasoning || '',
          simons_trace: extractTraceFromReasoning(row.reasoning || ''),
        };
      }
      if (Object.keys(next).length) setLiveSignals(next);
      setLastRefreshAt(new Date().toISOString());
    } catch (e: any) {
      setRefreshError(e?.message || 'Tick failed.');
    } finally {
      setIsRefreshing(false);
    }
  }, [flowIdNum, setLastRefreshAt]);

  const tickerCount = useMemo(() => Object.keys(liveSignals).length, [liveSignals]);
  const cadenceLabel = CADENCE_OPTIONS.find(o => o.value === cadence)?.label ?? cadence;
  const freqLabel = FREQ_OPTIONS.find(o => o.value === barFrequency)?.label ?? barFrequency;
  const lastRefreshLabel = lastRefreshAt
    ? new Date(lastRefreshAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : '—';

  return (
    <TooltipProvider>
      <NodeShell
        id={id}
        selected={selected}
        isConnectable={isConnectable}
        icon={<Sigma className="h-5 w-5" />}
        iconColor={getStatusColor(status)}
        name={data.name || 'Jim Simons'}
        description={data.description}
        status={status}
        width="w-80"
        // Two named handles instead of one — see NodeShell for the rendering
        // logic. Edges record which output they came from via `sourceHandle`
        // so the run-assembler can tell signal-edge from strategy-edge.
        rightHandles={[
          { id: 'signal', label: 'signal → PM', top: 38 },
          { id: 'strategy', label: 'strategy → Strategy', top: 72 },
        ]}
      >
        <CardContent className="p-0">
          <div className="border-t border-border p-3">
            <div className="flex flex-col gap-3">

              {/* Status pill + wiring readout. Loud red when nothing
                  downstream is wired — Simons with no outputs is a noop. */}
              <div className="flex flex-col gap-1">
                <div className="text-subtitle text-primary">Status</div>
                <div className={cn(
                  'text-foreground text-xs rounded p-2 border border-status',
                  isInProgress ? 'gradient-animation' : getStatusColor(status),
                )}>
                  <span className="capitalize">{status.toLowerCase().replace(/_/g, ' ')}</span>
                </div>
                <div className="flex flex-wrap gap-1 text-[10px] uppercase tracking-wide">
                  <span className={cn(
                    'rounded border px-1 py-0.5',
                    wiringStatus.drivesPM ? 'border-emerald-500/40 text-emerald-500' : 'border-amber-500/40 text-amber-500',
                  )}>
                    {wiringStatus.drivesPM ? '✓ PM wired' : '⚠ PM not wired'}
                  </span>
                  <span className={cn(
                    'rounded border px-1 py-0.5',
                    wiringStatus.drivesStrategy ? 'border-emerald-500/40 text-emerald-500' : 'border-muted text-muted-foreground',
                  )}>
                    {wiringStatus.drivesStrategy ? '✓ Driving Strategy' : 'Strategy not wired'}
                  </span>
                </div>
              </div>

              {/* Upstream ticker preview — pulled from wired input node. */}
              <div className="flex flex-col gap-1">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className="text-subtitle text-primary flex items-center gap-1">
                      Universe (from upstream)
                    </div>
                  </TooltipTrigger>
                  <TooltipContent side="right" className="max-w-xs">
                    Simons pulls tickers from whichever input node is wired upstream
                    (Stock Input or Portfolio Input). Independent of those nodes' Play
                    button — but it does need at least one wired in to know what to
                    analyse.
                  </TooltipContent>
                </Tooltip>
                {upstreamTickers.length === 0 ? (
                  <div className="rounded border border-red-500/40 bg-red-500/10 px-2 py-1.5 text-xs text-red-500">
                    No upstream input node — wire a Stock Input or Portfolio Input to the left handle.
                  </div>
                ) : (
                  <div className="rounded border border-border bg-node/40 px-2 py-1 text-xs tabular-nums leading-tight">
                    <div className="text-muted-foreground">{upstreamTickers.length} ticker{upstreamTickers.length === 1 ? '' : 's'}:</div>
                    <div className="whitespace-pre-wrap break-words">{upstreamTickers.join(', ')}</div>
                  </div>
                )}
              </div>

              {/* LLM picker. The hypothesis loop runs two LLM calls per
                  ticker (propose + adjudicate). Without a model picked the
                  backend falls into the pinned-default chain (and then the
                  pure-numpy fallback if nothing's reachable). */}
              <div className="flex flex-col gap-1">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className="text-subtitle text-primary">Model (hypothesis loop)</div>
                  </TooltipTrigger>
                  <TooltipContent side="right" className="max-w-xs">
                    The LLM that runs Simons's propose + adjudicate stages. Pure-numpy
                    tests run regardless. Leave on Auto to inherit the pinned default
                    (Settings → Models → ★). Without ANY model reachable, Simons falls
                    back to its rule-based behaviour (z-score &gt; 2σ → fire).
                  </TooltipContent>
                </Tooltip>
                <ModelSelector
                  models={availableModels}
                  value={selectedModel?.model_name || ''}
                  onChange={setSelectedModel}
                  placeholder="Auto"
                />
                {selectedModel?.provider === 'Google' && (
                  <ThinkingBudgetField id={id} />
                )}
              </div>

              {/* Cadence (the signal clock) */}
              <div className="flex flex-col gap-1">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className="text-subtitle text-primary">Signal cadence</div>
                  </TooltipTrigger>
                  <TooltipContent side="right" className="max-w-xs">
                    How often the backend's <b>simons_scheduler</b> re-runs Simons on
                    the upstream universe. Independent of the PM's trade tick — Simons
                    refreshes the cached signal; the PM picks it up on its own cadence.
                    Off = Simons only fires when the flow is played manually.
                  </TooltipContent>
                </Tooltip>
                <select
                  value={cadence}
                  onChange={(e) => setCadence(e.target.value as SimonsCadence)}
                  className="nodrag w-full rounded border border-border bg-node/60 px-2 py-1 text-sm text-foreground focus:outline-none focus:border-primary/50"
                >
                  {CADENCE_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              </div>

              {/* Bar frequency + lookback */}
              <div className="grid grid-cols-2 gap-2">
                <div className="flex flex-col gap-1">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary">Bar frequency</div>
                    </TooltipTrigger>
                    <TooltipContent side="bottom" className="max-w-xs">
                      Bar resolution Simons reads to compute z-score / vol / RS.
                      Renaissance's intraday signals lived on 5-min slices; daily
                      bars are the cheapest / slowest option.
                    </TooltipContent>
                  </Tooltip>
                  <select
                    value={barFrequency}
                    onChange={(e) => setBarFrequency(e.target.value as BarFrequency)}
                    className="nodrag w-full rounded border border-border bg-node/60 px-2 py-1 text-sm text-foreground focus:outline-none focus:border-primary/50"
                  >
                    {FREQ_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </div>
                <div className="flex flex-col gap-1">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary">Lookback bars</div>
                    </TooltipTrigger>
                    <TooltipContent side="bottom" className="max-w-xs">
                      Rolling window for z-score + vol + RS. Default 20 bars — long
                      enough for vol estimation, short enough to react to regime
                      changes. Range {LOOKBACK_MIN}-{LOOKBACK_MAX}.
                    </TooltipContent>
                  </Tooltip>
                  <input
                    type="number"
                    min={LOOKBACK_MIN}
                    max={LOOKBACK_MAX}
                    step={1}
                    value={lookbackBars}
                    onChange={(e) => setLookbackBars(Number(e.target.value) || LOOKBACK_DEFAULT)}
                    onBlur={(e) => setLookbackBars(clampLookback(Number(e.target.value)))}
                    className="nodrag w-full rounded border border-border bg-node/60 px-2 py-1 text-sm tabular-nums text-foreground focus:outline-none focus:border-primary/50"
                  />
                </div>
              </div>

              {cadenceFreqMismatch && (
                <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-xs text-amber-500">
                  ⚠ {cadenceFreqMismatch}
                </div>
              )}

              {/* Refresh row. Two actions:
                    * Refresh now — runs /simons/refresh synchronously (cheap,
                      doesn't touch the scheduler counter).
                    * Manual tick — fires the scheduler path so persistence +
                      strategy-override write also happen. */}
              <div className="flex flex-col gap-1">
                <div className="flex gap-2">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        onClick={handleRefresh}
                        disabled={isRefreshing || upstreamTickers.length === 0}
                        className={cn(
                          'flex-1 flex items-center justify-center gap-2 rounded border px-3 py-1.5 text-sm transition-colors',
                          'border-border hover:border-primary/40 hover:text-foreground',
                          'disabled:opacity-40 disabled:cursor-not-allowed',
                        )}
                      >
                        {isRefreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                        <span>{isRefreshing ? 'Running…' : 'Refresh now'}</span>
                      </button>
                    </TooltipTrigger>
                    <TooltipContent side="bottom" className="max-w-xs">
                      Re-run Simons over the upstream tickers right now. Cheap, no LLM.
                      Doesn't tick the scheduler counter — for that use "Manual tick".
                    </TooltipContent>
                  </Tooltip>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        onClick={handleManualTick}
                        disabled={isRefreshing || flowIdNum == null}
                        className={cn(
                          'flex items-center justify-center gap-2 rounded border px-3 py-1.5 text-sm transition-colors',
                          'border-border hover:border-primary/40 hover:text-foreground',
                          'disabled:opacity-40 disabled:cursor-not-allowed',
                        )}
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                        <span>Manual tick</span>
                      </button>
                    </TooltipTrigger>
                    <TooltipContent side="bottom" className="max-w-xs">
                      Fire one Simons tick through the scheduler path — same code the
                      timer runs, with the daily counter + persistence. Useful to test
                      the wiring out-of-hours (bypasses the market-hour gate).
                    </TooltipContent>
                  </Tooltip>
                </div>
                {refreshError && (
                  <div className="text-xs text-red-500">{refreshError}</div>
                )}
                <div className="text-[10px] text-muted-foreground">
                  Clock: <span className="text-foreground">{cadenceLabel}</span>
                  {' · '}
                  Bars: <span className="text-foreground">{freqLabel}</span>
                  {' · '}
                  Last refresh: <span className="text-foreground">{lastRefreshLabel}</span>
                </div>
              </div>

              {/* Signals readout. Click a ticker row → open the trace
                  dialog scoped to that ticker. Top-right Maximize opens
                  the dialog at the first ticker. */}
              <div className="flex flex-col gap-1">
                <div className="text-subtitle text-primary flex items-center justify-between">
                  <span>Signals</span>
                  {tickerCount > 0 && (
                    <button
                      type="button"
                      onClick={() => {
                        const first = Object.keys(liveSignals)[0];
                        setActiveTraceTicker(first);
                        setIsTraceOpen(true);
                      }}
                      title="Open full trace"
                      className="flex items-center justify-center h-5 w-5 rounded border border-border text-muted-foreground hover:text-foreground hover:border-primary/40"
                    >
                      <Maximize2 className="h-3 w-3" />
                    </button>
                  )}
                </div>
                {tickerCount === 0 ? (
                  <div className="rounded border border-border border-dashed px-2 py-2 text-xs text-muted-foreground text-center">
                    {upstreamTickers.length === 0
                      ? 'Connect an input node to see signals.'
                      : status === 'COMPLETE'
                        ? 'Run complete — no ticker crossed any hypothesis threshold (no entry).'
                        : 'No signals yet — click Refresh now.'}
                  </div>
                ) : (
                  <div className="flex flex-col gap-1 max-h-40 overflow-y-auto pr-1">
                    {Object.entries(liveSignals).map(([ticker, sig]) => {
                      const dot = sig.signal === 'bullish' ? 'bg-emerald-500'
                                : sig.signal === 'bearish' ? 'bg-red-500'
                                : 'bg-gray-500';
                      // Prefer the new hypothesis-loop trace's winning name;
                      // fall back to the legacy z-score summary if the row
                      // is from before the loop existed.
                      const winning = sig.simons_trace?.adjudication?.winning_hypothesis;
                      const z = sig.simons?.z_score;
                      const subtitle = winning
                        ? `via ${winning}`
                        : z != null ? `z=${z.toFixed(2)}σ` : '';
                      return (
                        <button
                          key={ticker}
                          type="button"
                          onClick={() => { setActiveTraceTicker(ticker); setIsTraceOpen(true); }}
                          className="nodrag flex items-center justify-between gap-2 text-xs tabular-nums rounded px-1.5 py-1 hover:bg-node/60 text-left"
                          title={sig.simons_trace ? 'Click for full trace' : 'No structured trace (pre-hypothesis-loop or fallback)'}
                        >
                          <div className="flex items-center gap-2 min-w-0">
                            <span className={cn('h-2 w-2 rounded-full', dot)} />
                            <span className="font-medium">{ticker}</span>
                            <span className="text-muted-foreground capitalize">{sig.signal}</span>
                            {subtitle && <span className="text-[10px] text-muted-foreground truncate">{subtitle}</span>}
                          </div>
                          <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                            <span>{sig.confidence}%</span>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* Recommended strategy mirror — shows what Simons is currently
                  pushing into the Strategy node. The Strategy node also
                  surfaces this with a "Driven by Jim Simons" banner. */}
              {recommendedStrategy && (
                <div className="flex flex-col gap-1 rounded border border-border bg-node/40 px-2 py-1.5 text-xs">
                  <div className="text-subtitle text-primary flex items-center gap-1">
                    <Brain className="h-3 w-3" /> Driving Strategy
                  </div>
                  <div className="text-muted-foreground tabular-nums leading-tight">
                    Style: <span className="text-foreground">{recommendedStrategy.style}</span>
                    {' · '}
                    Sizing: <span className="text-foreground">{recommendedStrategy.sizing_rule}</span>
                    {' · '}
                    Max pos: <span className="text-foreground">{recommendedStrategy.max_position_pct}%</span>
                    {' · '}
                    Hold: <span className="text-foreground">{recommendedStrategy.holding_period}</span>
                  </div>
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </NodeShell>
      <SimonsTraceDialog
        isOpen={isTraceOpen}
        onOpenChange={setIsTraceOpen}
        tickers={Object.keys(liveSignals)}
        activeTicker={activeTraceTicker}
        onTickerChange={setActiveTraceTicker}
        signalsByTicker={liveSignals}
      />
    </TooltipProvider>
  );
}

// Extract the ```simons-trace fenced JSON out of a wiki-stored reasoning
// markdown blob. Returns undefined when no trace fence is present (e.g.
// PR-#109 era rows or fallback signals). Same idiom Forecaster uses.
const SIMONS_TRACE_FENCE_RE = /```simons-trace\s*\n([\s\S]*?)\n```/;
function extractTraceFromReasoning(md: string | null | undefined) {
  if (!md) return undefined;
  const m = md.match(SIMONS_TRACE_FENCE_RE);
  if (!m) return undefined;
  try {
    return JSON.parse(m[1]);
  } catch {
    return undefined;
  }
}
