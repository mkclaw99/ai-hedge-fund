import { useReactFlow, type NodeProps } from '@xyflow/react';
import { History, Loader2, Play, Square, Target } from 'lucide-react';
import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { CardContent } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useFlowContext } from '@/contexts/flow-context';
import { useLayoutContext } from '@/contexts/layout-context';
import { useNodeContext } from '@/contexts/node-context';
import { useFlowConnection } from '@/hooks/use-flow-connection';
import { getNodeInternalState, useNodeState } from '@/hooks/use-node-state';
import { primaryAgentModel } from '@/lib/agent-models';
import { getFlowMemory } from '@/services/memory-api';
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
  // Options default ON — you asked for "access all derivatives related to the
  // companies in scope" when you spec'd the Strategy node, so the toggle
  // matches that intent on first add. The cost is one Alpaca-options call per
  // ticker per run; flip it off if you want stock-only runs.
  const [allowOptions, setAllowOptions] = useNodeState<boolean>(id, 'allowOptions', true);
  // ETFs default OFF — currently a *hint* to the PM (no discovery API yet),
  // so leaving it on adds prompt noise for nothing.
  const [allowEtfs, setAllowEtfs] = useNodeState<boolean>(id, 'allowEtfs', false);
  const [note, setNote] = useNodeState<string>(id, 'note', '');

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
  const [backtestError, setBacktestError] = useState<string | null>(null);

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
      strategy,
      risk_manager: riskManager,
      skip_analysts: true,
    });
  };

  // Build the StrategyConfig from the current node state — shared by Replay
  // and Backtest. Avoids two slightly-divergent copies.
  const buildStrategyConfig = () => ({
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
  });

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
      strategy: buildStrategyConfig(),
      risk_manager: buildRiskManagerConfig(),
    });
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
              </div>

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
