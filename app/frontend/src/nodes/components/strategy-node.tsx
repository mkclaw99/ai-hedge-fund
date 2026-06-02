import { useReactFlow, type NodeProps } from '@xyflow/react';
import { BarChart3, History, Loader2, Lock, Play, Sigma, Square, Target, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { Button } from '@/components/ui/button';
import { CardContent } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useFlowContext } from '@/contexts/flow-context';
import { useLayoutContext } from '@/contexts/layout-context';
import { useNodeContext } from '@/contexts/node-context';
import { useFlowConnection } from '@/hooks/use-flow-connection';
import { addStateChangeListener, getNodeInternalState, useNodeState } from '@/hooks/use-node-state';
import { primaryAgentModel } from '@/lib/agent-models';
import { cn } from '@/lib/utils';
import { getFlowMemory } from '@/services/memory-api';
import { type StrategyNode as StrategyNodeT } from '../types';
import { NodeShell } from './node-shell';
import { TrackRecordDialog } from './track-record-dialog';

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

// Translate a bar count + frequency into a wall-clock label so the Strategy
// node can show "Linked Forecaster: 5min × 288 bars (~24h)" — letting the
// user spot a holding-period mismatch (months vs hours) at a glance. Mirrors
// the backend's _human_horizon in portfolio_manager.py.
function horizonLabel(barCount: number | undefined, frequency: 'day' | 'hour' | '5min' | '1min' | undefined): string {
  const n = barCount && Number.isFinite(barCount) ? Math.max(0, Math.trunc(barCount)) : 0;
  if (!n) return '0 bars';
  const f = frequency || 'day';
  if (f === 'day') return `~${n} trading day${n === 1 ? '' : 's'}`;
  if (f === 'hour') {
    const days = n / 6.5;
    return `~${n}h (≈${days.toFixed(1)}d)`;
  }
  if (f === '5min') {
    const mins = n * 5;
    const hrs = mins / 60;
    if (hrs >= 6.5) return `~${hrs.toFixed(1)}h (≈${(hrs / 6.5).toFixed(2)}d)`;
    return `~${mins} min (~${hrs.toFixed(1)}h)`;
  }
  if (f === '1min') {
    const hrs = n / 60;
    if (hrs >= 6.5) return `~${hrs.toFixed(1)}h (≈${(hrs / 6.5).toFixed(2)}d)`;
    return `~${n} min`;
  }
  return `${n} × ${f}`;
}

// Frequency labels match the Forecaster node's FREQ_OPTIONS so the badge
// reads the same words as the source-of-truth dropdown.
const FREQ_LABEL: Record<string, string> = {
  day: 'Daily',
  hour: 'Hourly',
  '5min': '5-Minute',
  '1min': '1-Minute',
};

// Holding-period vs forecaster-horizon mismatch detection. Intraday holding
// (day-trade) + multi-day forecaster horizon, or position/long-term holding
// + intraday forecaster horizon, are both "the forecaster isn't telling you
// what you need to know" cases — surface a hint, don't override the user.
function horizonMismatch(holdingPeriod: string, freq?: string): string | null {
  if (!freq) return null;
  const intraday = freq === '1min' || freq === '5min' || freq === 'hour';
  if ((holdingPeriod === 'position' || holdingPeriod === 'long_term') && intraday) {
    return 'Forecaster runs intraday but holding period is multi-week+ — its directional call is entry-timing, not thesis.';
  }
  if (holdingPeriod === 'day' && freq === 'day') {
    return 'Forecaster runs at daily resolution but holding period is day-trade — consider switching the Forecaster to Hourly/5-Minute.';
  }
  return null;
}

export function StrategyNode({ data, selected, id, isConnectable }: NodeProps<StrategyNodeT>) {
  const [style, setStyle] = useNodeState<string>(id, 'style', 'value');
  const [sizingRule, setSizingRule] = useNodeState<string>(id, 'sizingRule', 'conviction_weighted');
  const [maxPositionPct, setMaxPositionPct] = useNodeState<string>(id, 'maxPositionPct', '15');
  const [maxSectorPct, setMaxSectorPct] = useNodeState<string>(id, 'maxSectorPct', '40');
  const [holdingPeriod, setHoldingPeriod] = useNodeState<string>(id, 'holdingPeriod', 'position');
  const [stopLossPct, setStopLossPct] = useNodeState<string>(id, 'stopLossPct', '');
  const [takeProfitPct, setTakeProfitPct] = useNodeState<string>(id, 'takeProfitPct', '');
  const [allowStocks, setAllowStocks] = useNodeState<boolean>(id, 'allowStocks', true);
  // Options default ON — you asked for "access all derivatives related to the
  // companies in scope" when you spec'd the Strategy node, so the toggle
  // matches that intent on first add. The cost is one Alpaca-options call per
  // ticker per run; flip it off if you want stock-only runs.
  const [allowOptions, setAllowOptions] = useNodeState<boolean>(id, 'allowOptions', true);
  // ETFs default OFF — currently a *hint* to the PM (no discovery API yet),
  // so leaving it on adds prompt noise for nothing.
  const [allowEtfs, setAllowEtfs] = useNodeState<boolean>(id, 'allowEtfs', false);
  const [note, setNote] = useNodeState<string>(id, 'note', '');
  // Decoupled trade-tick throttles. Without these, an `hourly`-or-faster
  // tradeSchedule on the Trading Account would re-fire the PM on every
  // tick whether anything material has changed or not. The PM skip
  // predicate (see src/agents/portfolio_manager.py:_should_skip_ticker)
  // reads these to drop tickers that aren't worth a fresh LLM call.
  const [minDecisionIntervalMinutes, setMinDecisionIntervalMinutes] = useNodeState<string>(id, 'minDecisionIntervalMinutes', '30');
  const [priceMoveThresholdPct, setPriceMoveThresholdPct] = useNodeState<string>(id, 'priceMoveThresholdPct', '1');
  const [maxSignalAgeHours, setMaxSignalAgeHours] = useNodeState<string>(id, 'maxSignalAgeHours', '168');

  // Replay-strategy wiring: re-run the PM on cached analyst signals from the
  // flow's wiki without re-running analysts (no LLM analyst calls, no theme
  // resolve). Only enabled when there's prior memory to replay.
  const { currentFlowId } = useFlowContext();
  const { getAllAgentModels } = useNodeContext();
  const { getNodes, getEdges } = useReactFlow();
  const { setBottomPanelTab, expandBottomPanel } = useLayoutContext();
  const flowId = currentFlowId?.toString() || null;
  const { canRun, isProcessing, runFlow, runBacktest, stopFlow } = useFlowConnection(flowId);
  const [memoryTickers, setMemoryTickers] = useState<string[]>([]);
  const [replayError, setReplayError] = useState<string | null>(null);

  // Backtest controls — replay the strategy day-by-day over a historical window
  // through the full graph (analysts re-evaluate each day). Defaults: last 30
  // trading days; user can shrink/grow before running. Saved per-node so the
  // window survives reloads via the existing useNodeState persistence.
  const today = new Date().toISOString().slice(0, 10);
  const thirtyAgo = new Date(Date.now() - 30 * 86_400_000).toISOString().slice(0, 10);
  const [backtestStartDate, setBacktestStartDate] = useNodeState<string>(id, 'backtestStartDate', thirtyAgo);
  const [backtestEndDate, setBacktestEndDate] = useNodeState<string>(id, 'backtestEndDate', today);
  // Per-day analyst lookback for the backtest. Default 252 calendar days
  // (~178 trading days) covers Technicals' largest rolling window
  // (`mom_6m` = 126 trading days). Lower it to make backtests cheaper if
  // your flow has no Technicals + no RM, or to deliberately starve them
  // for an apples-to-apples comparison against an older 30-day run.
  const [backtestLookbackDays, setBacktestLookbackDays] = useNodeState<string>(id, 'backtestLookbackDays', '252');
  const [backtestError, setBacktestError] = useState<string | null>(null);
  const [trackRecordOpen, setTrackRecordOpen] = useState(false);
  // "Clear wiki for backtest range" — opt-in destructive op so re-running
  // a backtest on the same window doesn't double-feed the PM track-record.
  const [isClearingWiki, setIsClearingWiki] = useState(false);
  const [clearWikiMsg, setClearWikiMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  // Force a re-render of the Strategy node when any Forecaster-side knob is
  // turned (context len / prediction len / bar frequency live in the
  // node-internal-state store, not in React Flow's node payload). Without
  // this, the "Linked Forecaster" badge would freeze on whatever the
  // forecaster was set to when the Strategy node mounted.
  const [, bumpStateVersion] = useState(0);
  useEffect(() => {
    return addStateChangeListener(() => bumpStateVersion((v) => v + 1));
  }, []);

  // Reactive read of EVERY connected Forecaster node's settings. Both
  // Chronos-2 (`forecaster_*` ids) and Toto-2.0 (`toto_forecaster_*` ids)
  // share the `forecaster-node` type, so a flow can carry one or both.
  // The badge below lists every one with its backbone + horizon;
  // mismatch warning fires when ANY of them drifts from holding-period.
  // handleReplay/handleBacktest pass the first forecaster's settings
  // through the per-flow `forecaster_*` request fields — backward-compat
  // with the single-forecaster request shape. Per-backbone overrides
  // would require new request fields; out of scope for this PR.
  type LinkedForecaster = {
    id: string;
    backbone: 'chronos2' | 'toto2';
    backboneLabel: 'Chronos-2' | 'Toto-2.0';
    ctx: number | undefined;
    pred: number | undefined;
    freq: 'day' | 'hour' | '5min' | '1min' | undefined;
  };
  const linkedForecasters: LinkedForecaster[] = (() => {
    const out: LinkedForecaster[] = [];
    for (const f of getNodes()) {
      if (f.type !== 'forecaster-node') continue;
      const s = getNodeInternalState(f.id) as any;
      const isToto = f.id.startsWith('toto_forecaster');
      out.push({
        id: f.id,
        backbone: isToto ? 'toto2' : 'chronos2',
        backboneLabel: isToto ? 'Toto-2.0' : 'Chronos-2',
        ctx: typeof s?.forecasterContextLen === 'number' ? s.forecasterContextLen : undefined,
        pred: typeof s?.forecasterPredictionLen === 'number' ? s.forecasterPredictionLen : undefined,
        freq: s?.forecasterBarFrequency,
      });
    }
    return out;
  })();
  // First forecaster — Replay/Backtest source for per-flow forecaster_*
  // request fields. Kept as `linkedForecaster` so existing call sites
  // (further down) work unchanged.
  const linkedForecaster = linkedForecasters[0] ?? null;
  // Per-forecaster mismatch fires inside the badge JSX below — no need
  // for an aggregate at this scope.

  // Simons-driven detection. When a Jim Simons node has an edge into this
  // Strategy node, it owns the StrategyConfig — the manual fields below
  // are visibly inert ("Driven by Jim Simons" banner, lock icons), and
  // buildStrategyConfig returns Simons's persisted recommendedStrategy
  // instead of the typed-in values. Unplug Simons → manual values come
  // straight back unchanged (they were never overwritten, just shadowed).
  const simonsOverride = useMemo<{ nodeId: string; strategy: any; updatedAt: string | null } | null>(() => {
    const edges = getEdges();
    const nodes = getNodes();
    const incomingSources = new Set(edges.filter(e => e.target === id).map(e => e.source));
    for (const n of nodes) {
      if (n.type !== 'jim-simons-node') continue;
      if (!incomingSources.has(n.id)) continue;
      const state = (getNodeInternalState(n.id) as any) || {};
      const strat = state.recommendedStrategy;
      if (!strat) continue;
      return {
        nodeId: n.id,
        strategy: strat,
        updatedAt: state.lastRefreshAt || null,
      };
    }
    return null;
  // Re-run on the state-bump version that already triggers the linkedForecaster
  // refresh — same listener covers Simons useNodeState writes.
  }, [getEdges, getNodes, id]);
  const isDrivenBySimons = simonsOverride !== null;

  // Parse a numeric-ish string into a float; non-finite values collapse to
  // undefined so they don't override Pydantic defaults on the backend.
  const parseNum = (v: any): number | undefined => {
    const x = parseFloat(v);
    return Number.isFinite(x) ? x : undefined;
  };

  // Refresh cached-ticker list when this flow comes into focus or when a
  // run finishes (`isProcessing` going false). Cheap call (one fetch).
  useEffect(() => {
    let cancelled = false;
    if (currentFlowId == null) {
      setMemoryTickers([]);
      return;
    }
    getFlowMemory(Number(currentFlowId))
      .then((mem) => {
        if (cancelled) return;
        setMemoryTickers((mem.tickers || []).map((t) => t.ticker).filter(Boolean));
      })
      .catch(() => setMemoryTickers([]));
    return () => { cancelled = true; };
  }, [currentFlowId, isProcessing]);

  const noMemory = memoryTickers.length === 0;
  const replayDisabled = !canRun || noMemory;

  const handleReplay = () => {
    setReplayError(null);
    const allNodes = getNodes();
    const pmNode = allNodes.find((n) => n.type === 'portfolio-manager-node');
    if (!pmNode) {
      setReplayError('No Portfolio Manager in the flow.');
      return;
    }
    if (memoryTickers.length === 0) {
      setReplayError('No cached signals — run the full flow at least once first.');
      return;
    }
    // Trading Account node — same opt-in / budget read as the full-flow handlePlay.
    const tradingNode = allNodes.find((n) => n.type === 'trading-account-node');
    const tradingState = (tradingNode ? getNodeInternalState(tradingNode.id) : null) as any;
    const placePaperOrders = !!tradingState?.autoTrade;
    const startingBudget = Number(tradingState?.startingBudget ?? 0) || undefined;

    // Risk Manager + Strategy configs — shared with the Backtest path.
    const riskManager = buildRiskManagerConfig();

    // Use only the PM's model — analysts won't run, so their model picks are irrelevant.
    const allAgentModels = getAllAgentModels(flowId);
    const pmModel = allAgentModels[pmNode.id];
    const agentModels = pmModel
      ? [{ agent_id: pmNode.id, model_name: pmModel.model_name, model_provider: pmModel.provider as any }]
      : [];
    const primary = primaryAgentModel(agentModels);

    const strategy = buildStrategyConfig();

    expandBottomPanel();
    setBottomPanelTab('output');

    // Forecaster knobs — when a Forecaster node is on the canvas, forward
    // its current settings so the run uses the user's chosen horizon /
    // frequency rather than the backend defaults (256/10/'day'). The PM's
    // `## Forecast Mandate` block reads these back from the produced
    // signals[ticker]['forecast'] payload. Replay skips analysts anyway, so
    // this only matters when the wiki happens to have stale forecaster
    // signals — kept for symmetry with Backtest, which DOES re-run them.
    runFlow({
      tickers: memoryTickers,
      // Only the PM node — analysts are skipped (signals come from the wiki).
      graph_nodes: [{ id: pmNode.id, type: pmNode.type, data: pmNode.data, position: pmNode.position }],
      graph_edges: [],
      agent_models: agentModels,
      model_name: primary.model_name,
      model_provider: primary.model_provider as any,
      place_paper_orders: placePaperOrders,
      starting_budget: startingBudget,
      forecaster_context_len: linkedForecaster?.ctx,
      forecaster_prediction_len: linkedForecaster?.pred,
      forecaster_bar_frequency: linkedForecaster?.freq,
      strategy,
      risk_manager: riskManager,
      skip_analysts: true,
    });
  };

  // Build the StrategyConfig from the current node state — shared by Replay
  // and Backtest. Avoids two slightly-divergent copies. When a Simons node
  // is wired in, its recommended strategy replaces the manual one wholesale
  // (per "Strategy mode = Replace"). The manual values remain in useNodeState
  // unchanged, so unwiring Simons restores them instantly.
  const buildStrategyConfig = () => {
    if (simonsOverride) {
      // The Simons recommendation is already in snake_case (matches the
      // backend StrategyConfig shape), so we forward it verbatim.
      return { ...simonsOverride.strategy };
    }
    return {
      style: style || undefined,
      sizing_rule: sizingRule || undefined,
      max_position_pct: parseNum(maxPositionPct),
      max_sector_pct: parseNum(maxSectorPct),
      holding_period: holdingPeriod || undefined,
      stop_loss_pct: parseNum(stopLossPct),
      take_profit_pct: parseNum(takeProfitPct),
      allow_stocks: allowStocks !== false,
      allow_options: !!allowOptions,
      allow_etfs: !!allowEtfs,
      note: note || undefined,
      // Decoupled trade-tick throttles — read by the PM skip predicate.
      min_decision_interval_minutes: parseNum(minDecisionIntervalMinutes),
      price_move_threshold_pct: parseNum(priceMoveThresholdPct),
      max_signal_age_hours: parseNum(maxSignalAgeHours),
    };
  };

  // Read the optional Risk Manager node's config from the canvas. None on the
  // canvas → undefined → backend uses defaults (= pre-PR behaviour).
  const buildRiskManagerConfig = () => {
    const allNodes = getNodes();
    const node = allNodes.find((n) => n.type === 'risk-manager-node');
    if (!node) return undefined;
    const s = getNodeInternalState(node.id) as any;
    return {
      limit_multiplier: parseNum(s?.limitMultiplier) ?? 1.0,
      disable_correlation_penalty: !!s?.disableCorrelationPenalty,
      disabled: !!s?.disabled,
    };
  };

  const handleBacktest = () => {
    setBacktestError(null);
    if (!backtestStartDate || !backtestEndDate) {
      setBacktestError('Pick a start and end date.');
      return;
    }
    if (backtestStartDate >= backtestEndDate) {
      setBacktestError('Start date must be before end date.');
      return;
    }
    if (memoryTickers.length === 0) {
      setBacktestError('No cached tickers — run the full flow once first.');
      return;
    }
    const allNodes = getNodes();
    // Backtest needs the FULL graph (analysts re-evaluate each day on that
    // day's data — Replay's "PM-only + cached signals" would defeat the
    // purpose). Take every non-resource agent node on the canvas.
    const agentNodes = allNodes.filter(
      (n) =>
        n.type !== 'memory-node' &&
        n.type !== 'research-area-node' &&
        n.type !== 'research-companies-node' &&
        n.type !== 'risk-manager-node' &&
        n.type !== 'strategy-node' &&
        n.type !== 'trading-account-node' &&
        n.type !== 'stock-analyzer-node' &&
        n.type !== 'portfolio-start-node',
    );
    if (!agentNodes.some((n) => n.type === 'portfolio-manager-node')) {
      setBacktestError('No Portfolio Manager in the flow.');
      return;
    }
    const days = Math.max(
      1,
      Math.round((Date.parse(backtestEndDate) - Date.parse(backtestStartDate)) / 86_400_000),
    );
    const ok = window.confirm(
      `Backtest from ${backtestStartDate} → ${backtestEndDate} (${days} day${days === 1 ? '' : 's'}) ` +
        `on ${memoryTickers.length} ticker${memoryTickers.length === 1 ? '' : 's'} via the full graph.\n\n` +
        `Each day re-runs every analyst + the PM. Expect several minutes per day. ` +
        `LLM-cache hits will reduce cost but not zero it.\n\nContinue?`,
    );
    if (!ok) return;

    const allAgentModels = getAllAgentModels(flowId);
    const agentModels = agentNodes
      .map((n) => {
        const m = allAgentModels[n.id];
        return m ? { agent_id: n.id, model_name: m.model_name, model_provider: m.provider as any } : null;
      })
      .filter((m): m is { agent_id: string; model_name: string; model_provider: any } => m !== null);
    const primary = primaryAgentModel(agentModels);

    const ids = new Set(agentNodes.map((n) => n.id));
    const allEdges = getEdges();
    const graph_edges = allEdges.filter((e) => ids.has(e.source) && ids.has(e.target));

    expandBottomPanel();
    setBottomPanelTab('output');

    runBacktest({
      tickers: memoryTickers,
      graph_nodes: agentNodes.map((n) => ({ id: n.id, type: n.type, data: n.data, position: n.position })),
      graph_edges,
      agent_models: agentModels,
      start_date: backtestStartDate,
      end_date: backtestEndDate,
      initial_capital: 100000,
      margin_requirement: 0,
      model_name: primary.model_name,
      model_provider: primary.model_provider as any,
      // Forecaster knobs — Backtest DOES re-run the analyst layer per-day,
      // so the Forecaster will fire with whatever horizon/frequency the
      // user picked on the canvas, not the backend default.
      forecaster_context_len: linkedForecaster?.ctx,
      forecaster_prediction_len: linkedForecaster?.pred,
      forecaster_bar_frequency: linkedForecaster?.freq,
      // Per-day analyst lookback. parseNum returns undefined for blank/NaN
      // so the backend default (252) takes over instead of getting None'd
      // and re-erroring downstream.
      backtest_lookback_days: parseNum(backtestLookbackDays),
      strategy: buildStrategyConfig(),
      risk_manager: buildRiskManagerConfig(),
      flow_id: currentFlowId ?? undefined,
    });
  };

  const handleClearBacktestWiki = async () => {
    setClearWikiMsg(null);
    if (!currentFlowId) {
      setClearWikiMsg({ kind: 'err', text: 'Save the flow first.' });
      return;
    }
    if (!backtestStartDate || !backtestEndDate) {
      setClearWikiMsg({ kind: 'err', text: 'Pick a start and end date first.' });
      return;
    }
    const ok = window.confirm(
      `Delete every wiki insight in this flow between ${backtestStartDate} and ${backtestEndDate}?\n\n` +
      `This is destructive and includes any LIVE-run insights that fell inside this window. ` +
      `There is no undo.`,
    );
    if (!ok) return;
    setIsClearingWiki(true);
    try {
      const API_BASE_URL = (import.meta as any).env?.VITE_API_URL || 'http://localhost:8001';
      const url = `${API_BASE_URL}/flows/${currentFlowId}/wiki/range?start=${encodeURIComponent(backtestStartDate)}&end=${encodeURIComponent(backtestEndDate)}`;
      const r = await fetch(url, { method: 'DELETE' });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${r.status}`);
      }
      const data = await r.json();
      setClearWikiMsg({ kind: 'ok', text: `Deleted ${data.deleted} wiki insight(s).` });
    } catch (e: any) {
      setClearWikiMsg({ kind: 'err', text: e?.message || 'Clear failed.' });
    } finally {
      setIsClearingWiki(false);
    }
  };

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

              {/* "Driven by Jim Simons" banner — only shown when a Simons
                  node is wired into this Strategy node. Per the user's
                  Replace-mode choice: Simons's recommendedStrategy fully
                  shadows the manual fields for the duration of the wiring.
                  The fields below show muted + lock icons so the inertness
                  is loud; unplug Simons in the canvas to take manual
                  control back. */}
              {isDrivenBySimons && (
                <div className="rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs flex flex-col gap-1">
                  <div className="flex items-center gap-2 text-amber-500">
                    <Sigma className="h-3.5 w-3.5" />
                    <span className="font-medium">Driven by Jim Simons</span>
                    {simonsOverride?.updatedAt && (
                      <span className="text-muted-foreground">
                        · updated {new Date(simonsOverride.updatedAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
                      </span>
                    )}
                  </div>
                  <div className="text-muted-foreground leading-snug">
                    The Simons node is publishing its recommended strategy through its
                    <span className="font-medium"> strategy </span>output handle. Your
                    typed-in fields are <span className="font-medium">shadowed</span> (preserved,
                    but not used). Disconnect Simons in the canvas to take manual control back.
                  </div>
                  {simonsOverride?.strategy && (
                    <div className="text-muted-foreground tabular-nums leading-tight">
                      Style: <span className="text-foreground">{simonsOverride.strategy.style}</span>
                      {' · '}
                      Sizing: <span className="text-foreground">{simonsOverride.strategy.sizing_rule}</span>
                      {' · '}
                      Max pos: <span className="text-foreground">{simonsOverride.strategy.max_position_pct}%</span>
                      {' · '}
                      Hold: <span className="text-foreground">{simonsOverride.strategy.holding_period}</span>
                      {' · '}
                      Min interval: <span className="text-foreground">{simonsOverride.strategy.min_decision_interval_minutes}m</span>
                    </div>
                  )}
                </div>
              )}

              {/* Replay strategy — re-runs the PM on cached analyst signals from
                  the wiki, so changing strategy params doesn't require re-running
                  the whole analyst layer. Disabled when no prior memory exists
                  for this flow (you need at least one full run first). */}
              <div className="flex flex-col gap-1">
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild>
                    <Button
                      type="button"
                      size="sm"
                      variant="default"
                      disabled={replayDisabled}
                      onClick={isProcessing ? stopFlow : handleReplay}
                      className="nodrag w-full justify-center gap-2"
                      aria-label="Re-run strategy on cached signals"
                    >
                      {isProcessing ? (
                        <>
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          Running — click to stop
                        </>
                      ) : (
                        <>
                          <Play className="h-3.5 w-3.5" />
                          Re-run strategy only
                        </>
                      )}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom" className="max-w-xs">
                    {noMemory
                      ? 'Run the full flow at least once first — no cached analyst signals to replay yet.'
                      : `Re-decide on the ${memoryTickers.length} cached ticker${memoryTickers.length === 1 ? '' : 's'} using these strategy params. Skips the analyst layer entirely — no LLM analyst calls, no theme re-research. Only the PM (and Risk Manager) run.`}
                  </TooltipContent>
                </Tooltip>
                {replayError && (
                  <div className="text-xs text-red-500">{replayError}</div>
                )}
              </div>

              {/* Backtest — replay the whole graph day-by-day over a date window
                  so you can measure the strategy against history. Defaults to
                  the last 30 days. Each day re-runs every analyst + the PM, so
                  this is expensive in time + tokens; a confirm dialog gates it. */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary">Backtest</div>
                <div className="grid grid-cols-2 gap-2">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <input
                        type="date"
                        aria-label="Backtest start date"
                        className="nodrag h-9 w-full rounded-md border border-border bg-node px-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                        value={backtestStartDate}
                        max={backtestEndDate}
                        onChange={(e) => setBacktestStartDate(e.target.value)}
                      />
                    </TooltipTrigger>
                    <TooltipContent side="bottom">From (start of the simulated period)</TooltipContent>
                  </Tooltip>
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <input
                        type="date"
                        aria-label="Backtest end date"
                        className="nodrag h-9 w-full rounded-md border border-border bg-node px-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                        value={backtestEndDate}
                        min={backtestStartDate}
                        max={today}
                        onChange={(e) => setBacktestEndDate(e.target.value)}
                      />
                    </TooltipTrigger>
                    <TooltipContent side="bottom">To (end of the simulated period)</TooltipContent>
                  </Tooltip>
                </div>

                {/* Lookback override — same row treatment as the dates.
                    Default 252 calendar days = Technicals' mom_6m floor.
                    Tooltip explains the cost-vs-coverage knob. */}
                <div className="flex items-center gap-2">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <label
                        htmlFor={`${id}-lookback`}
                        className="text-xs text-muted-foreground whitespace-nowrap"
                      >
                        Lookback per day (days)
                      </label>
                    </TooltipTrigger>
                    <TooltipContent side="bottom" className="max-w-xs">
                      How many calendar days of price history each analyst sees on every
                      simulated day. Default 252 (~1 yr) covers Technicals' largest rolling
                      window (mom_6m = 126 trading days). Below ~180 starves Technicals + the
                      Risk Manager's 30-day vol. Lower for a cheaper backtest only if you
                      know your flow has no Technicals/RM in it.
                    </TooltipContent>
                  </Tooltip>
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <input
                        id={`${id}-lookback`}
                        type="number"
                        min={7}
                        max={730}
                        step={1}
                        aria-label="Backtest lookback in calendar days"
                        className="nodrag h-9 w-24 rounded-md border border-border bg-node px-2 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                        value={backtestLookbackDays}
                        onChange={(e) => setBacktestLookbackDays(e.target.value.replace(/[^0-9]/g, ''))}
                      />
                    </TooltipTrigger>
                    <TooltipContent side="bottom">Calendar days. Min 7, max 730.</TooltipContent>
                  </Tooltip>
                </div>
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={!canRun || isProcessing || noMemory}
                      onClick={handleBacktest}
                      className="nodrag w-full justify-center gap-2"
                      aria-label="Backtest the strategy on the cached ticker universe"
                    >
                      <History className="h-3.5 w-3.5" />
                      Backtest strategy
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom" className="max-w-xs">
                    {noMemory
                      ? 'Run the full flow at least once first — backtest replays history on the cached universe.'
                      : `Replay every day from ${backtestStartDate} to ${backtestEndDate} through the full graph using these strategy params. Expensive — confirm before running.`}
                  </TooltipContent>
                </Tooltip>
                {backtestError && (
                  <div className="text-xs text-red-500">{backtestError}</div>
                )}
                {/* "Clear wiki for backtest range" — destructive, opt-in.
                    Without it, re-running the same backtest on the same date
                    window double-feeds the PM's track-record (it sees the
                    first run's decisions as if they were live history). */}
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={isClearingWiki || !currentFlowId}
                      onClick={handleClearBacktestWiki}
                      className="nodrag w-full justify-center gap-2 border-red-500/40 text-red-500 hover:bg-red-500/10 hover:text-red-500"
                      aria-label="Clear wiki entries in the backtest date range"
                    >
                      {isClearingWiki ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Trash2 className="h-3.5 w-3.5" />
                      )}
                      {isClearingWiki ? 'Clearing…' : 'Clear wiki for backtest range'}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom" className="max-w-xs">
                    Delete every analyst + PM insight in this flow's wiki between the
                    Backtest dates. Use before re-running a backtest on the same window
                    so the second run's PM doesn't see the first run's decisions as
                    track-record. Live-run insights inside the window are deleted too —
                    so don't click this unless you know which dates were simulation vs
                    real.
                  </TooltipContent>
                </Tooltip>
                {clearWikiMsg && (
                  <div className={cn(
                    "rounded-md border px-2 py-1.5 text-xs",
                    clearWikiMsg.kind === "ok"
                      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-500"
                      : "border-red-500/40 bg-red-500/10 text-red-500",
                  )}>{clearWikiMsg.text}</div>
                )}
              </div>

              {/* Track record — opens a dialog with the same data the PM sees
                  in its prompt (PR #65), so the user can audit the learning
                  loop visually without reading agent prose. */}
              <Tooltip delayDuration={200}>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={noMemory}
                    onClick={() => setTrackRecordOpen(true)}
                    className="nodrag w-full justify-center gap-2"
                    aria-label="View the strategy's track record"
                  >
                    <BarChart3 className="h-3.5 w-3.5" />
                    View track record
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom" className="max-w-xs">
                  {noMemory
                    ? 'Run the full flow at least once first — track record needs decisions in the wiki.'
                    : 'See WIN/LOSS/OPEN per past decision + per-analyst and per-(analyst, ticker) hit rates. Same data the PM uses in its prompt.'}
                </TooltipContent>
              </Tooltip>

              {/* Editable strategy fields — wrapped in a single container
                  so the Simons-driven state can dim + disable them all at
                  once. `pointer-events-none` blocks edits; `opacity-60`
                  signals "this is what would run if you were in control".
                  Manual values stay in useNodeState unchanged, so unplugging
                  Simons restores them instantly. */}
              <div
                className={cn(
                  'flex flex-col gap-4 transition-opacity',
                  isDrivenBySimons && 'opacity-60 pointer-events-none select-none',
                )}
                aria-disabled={isDrivenBySimons || undefined}
              >
              {/* Style + sizing rule */}
              <div className="grid grid-cols-2 gap-3">
                <div className="flex flex-col gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <div className="text-subtitle text-primary flex items-center gap-1">
                        Style {isDrivenBySimons && <Lock className="h-3 w-3" />}
                      </div>
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

              {/* Linked Forecasters — surfaces every connected Time Series
                  Forecaster node's backbone + horizon. Chronos-2 and
                  Toto-2.0 both spawn the same `forecaster-node` type, so a
                  flow can have one of each. Helps spot horizon/holding
                  mismatches and double-feed signals to the PM. */}
              <div className="flex flex-col gap-1">
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild>
                    <div className="text-subtitle text-primary">
                      Linked Forecaster{linkedForecasters.length > 1 ? 's' : ''}
                      {linkedForecasters.length > 1 && (
                        <span className="ml-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                          ×{linkedForecasters.length}
                        </span>
                      )}
                    </div>
                  </TooltipTrigger>
                  <TooltipContent side="right" className="max-w-xs">
                    Every Forecaster node on this canvas. Each backbone's bar frequency
                    × prediction length sets the horizon the PM reads in its
                    `## Forecast Mandate` block. With two backbones wired, the PM
                    sees both fans; useful for cross-validation.
                  </TooltipContent>
                </Tooltip>
                {linkedForecasters.length === 0 ? (
                  <div className="rounded-md border border-border bg-node px-2 py-1.5 text-xs text-muted-foreground">
                    No Forecaster node wired — PM won't see any time-series forecast.
                  </div>
                ) : (
                  <div className="flex flex-col gap-1.5">
                    {linkedForecasters.map((lf) => {
                      const localMismatch = horizonMismatch(holdingPeriod, lf.freq);
                      return (
                        <div key={lf.id} className="flex flex-col gap-1 rounded-md border border-border bg-node px-2 py-1.5 text-xs">
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-medium">{lf.backboneLabel}</span>
                            <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                              ctx {lf.ctx ?? '?'}
                            </span>
                          </div>
                          <div>
                            <span className="text-muted-foreground">Frequency:</span>{' '}
                            <span className="font-medium">{FREQ_LABEL[lf.freq ?? 'day'] ?? lf.freq ?? '—'}</span>
                            {' · '}
                            <span className="text-muted-foreground">Horizon:</span>{' '}
                            <span className="font-medium">
                              {lf.pred ?? '?'} bars ({horizonLabel(lf.pred, lf.freq)})
                            </span>
                          </div>
                          {localMismatch && (
                            <div className="text-amber-500">⚠ {localMismatch}</div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
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

              {/* Decoupled trade-tick throttles — only meaningful when the
                  Trading Account's tradeSchedule != off. The PM skip predicate
                  uses these to drop tickers that aren't worth a fresh LLM call:
                  PM re-fires only if at least one of (time since last trade
                  exceeds min interval) / (price moved more than threshold) /
                  (stop or take-profit crossed) is true. */}
              <div className="flex flex-col gap-1">
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild>
                    <div className="text-subtitle text-primary">Trade-tick throttle</div>
                  </TooltipTrigger>
                  <TooltipContent side="right" className="max-w-xs">
                    Limits how often the PM re-decides per ticker on decoupled
                    trade ticks. Each tick drops tickers that haven't moved
                    materially and aren't at a stop/target — keeps LLM cost
                    under control at 5-min cadence. No-op when Trade Schedule
                    is off.
                  </TooltipContent>
                </Tooltip>
                <div className="grid grid-cols-3 gap-2">
                  <div className="flex flex-col gap-1">
                    <Tooltip delayDuration={200}>
                      <TooltipTrigger asChild>
                        <div className="text-xs text-muted-foreground">Min interval (min)</div>
                      </TooltipTrigger>
                      <TooltipContent side="bottom" className="max-w-xs">
                        Don't re-decide on a ticker if its last fill is more recent
                        than this many minutes (unless price moved past the threshold).
                      </TooltipContent>
                    </Tooltip>
                    <input
                      type="number" min={0} max={1440} step={5}
                      aria-label="Minimum decision interval in minutes"
                      className={NUM_CLS}
                      value={minDecisionIntervalMinutes}
                      onChange={(e) => setMinDecisionIntervalMinutes(e.target.value.replace(/[^0-9]/g, ''))}
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <Tooltip delayDuration={200}>
                      <TooltipTrigger asChild>
                        <div className="text-xs text-muted-foreground">Price move (%)</div>
                      </TooltipTrigger>
                      <TooltipContent side="bottom" className="max-w-xs">
                        If price moved more than this since the last fill, PM
                        re-decides even inside the min-interval window.
                      </TooltipContent>
                    </Tooltip>
                    <input
                      type="number" min={0} max={50} step={0.5}
                      aria-label="Price move threshold percent"
                      className={NUM_CLS}
                      value={priceMoveThresholdPct}
                      onChange={(e) => setPriceMoveThresholdPct(e.target.value.replace(/[^0-9.]/g, ''))}
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <Tooltip delayDuration={200}>
                      <TooltipTrigger asChild>
                        <div className="text-xs text-muted-foreground">Max signal age (h)</div>
                      </TooltipTrigger>
                      <TooltipContent side="bottom" className="max-w-xs">
                        Bail out (all-hold) when the average age of cached analyst
                        signals exceeds this many hours. Default 168 = 7 days, so
                        a `weekly` analyst schedule isn't blocked. Lower it for
                        more cautious trading on stale theses.
                      </TooltipContent>
                    </Tooltip>
                    <input
                      type="number" min={1} max={720} step={1}
                      aria-label="Max signal age in hours"
                      className={NUM_CLS}
                      value={maxSignalAgeHours}
                      onChange={(e) => setMaxSignalAgeHours(e.target.value.replace(/[^0-9]/g, ''))}
                    />
                  </div>
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

              </div>
              {/* /editable-strategy-fields wrapper */}

              <div className="text-xs text-muted-foreground">
                Wire: Fundamental Companies → Strategy → Portfolio Manager.
                {isDrivenBySimons && (
                  <span className="block mt-1 text-amber-500/80">
                    (Jim Simons is currently driving — see banner above.)
                  </span>
                )}
              </div>
            </div>
          </div>
        </CardContent>
      </NodeShell>
      <TrackRecordDialog
        isOpen={trackRecordOpen}
        onOpenChange={setTrackRecordOpen}
        flowId={currentFlowId}
        holdingPeriod={holdingPeriod as any}
      />
    </TooltipProvider>
  );
}
