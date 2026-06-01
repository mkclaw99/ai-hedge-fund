// Effective-strategy resolver, shared by all run-assembler call sites.
//
// Two sources can produce the run's StrategyConfig:
//   1. The Strategy node's own useNodeState (camelCase keys from the UI).
//   2. A Jim Simons node's recommendedStrategy (snake_case, pushed when
//      Simons is wired into the Strategy node via the `strategy` handle).
//
// The Simons override wins by user choice ("Strategy mode = Replace"). This
// helper mirrors the backend's `_simons_strategy_override` in
// app/backend/services/trade_executor.py so a manual play and a scheduled
// trade tick agree on which strategy was in effect. Returning undefined
// (no Strategy node on the canvas) lets the backend keep its existing
// behaviour of running without a strategy mandate.

import type { Edge } from '@xyflow/react';
import { getNodeInternalState } from '@/hooks/use-node-state';

export interface EffectiveStrategy {
  style?: string;
  sizing_rule?: string;
  max_position_pct?: number;
  max_sector_pct?: number;
  holding_period?: string;
  stop_loss_pct?: number;
  take_profit_pct?: number;
  allow_stocks?: boolean;
  allow_options?: boolean;
  allow_etfs?: boolean;
  note?: string;
  min_decision_interval_minutes?: number;
  price_move_threshold_pct?: number;
  max_signal_age_hours?: number;
}

interface NodeLike { id: string; type?: string }

function num(v: any): number | undefined {
  const x = parseFloat(v);
  return Number.isFinite(x) ? x : undefined;
}

/** Resolve the effective StrategyConfig for a run, or undefined when no
 *  Strategy node exists on the canvas. `reachableIds` lets the caller limit
 *  the search to nodes downstream of the play trigger — same convention
 *  the existing run-assemblers use for the rest of their checks. */
export function resolveEffectiveStrategy(
  allNodes: NodeLike[],
  allEdges: Edge[],
  reachableIds: Set<string>,
): EffectiveStrategy | undefined {
  const strategyNode = allNodes.find(
    (n) => reachableIds.has(n.id) && n.type === 'strategy-node',
  );
  if (!strategyNode) return undefined;

  // Simons override — any Simons node with an edge into Strategy wins.
  // Multiple Simons → Strategy edges would each push their own
  // recommendedStrategy; we take the first one we find. Real flows have
  // one Simons node; this is just defensive.
  const simonsForStrategy = allNodes.find(
    (n) =>
      n.type === 'jim-simons-node' &&
      allEdges.some((e) => e.source === n.id && e.target === strategyNode.id),
  );
  if (simonsForStrategy) {
    const simonsState = getNodeInternalState(simonsForStrategy.id) as any;
    const rec = simonsState?.recommendedStrategy;
    if (rec && typeof rec === 'object') {
      // Already in snake_case — backend StrategyConfig shape — forward verbatim.
      return { ...rec };
    }
  }

  // Manual Strategy node config — translate from camelCase useNodeState
  // keys to the snake_case StrategyConfig the backend expects.
  const s = getNodeInternalState(strategyNode.id) as any;
  if (!s) return undefined;
  return {
    style: s.style || undefined,
    sizing_rule: s.sizingRule || undefined,
    max_position_pct: num(s.maxPositionPct),
    max_sector_pct: num(s.maxSectorPct),
    holding_period: s.holdingPeriod || undefined,
    stop_loss_pct: num(s.stopLossPct),
    take_profit_pct: num(s.takeProfitPct),
    allow_stocks: s.allowStocks !== false,
    allow_options: !!s.allowOptions,
    allow_etfs: !!s.allowEtfs,
    note: s.note || undefined,
    min_decision_interval_minutes: num(s.minDecisionIntervalMinutes),
    price_move_threshold_pct: num(s.priceMoveThresholdPct),
    max_signal_age_hours: num(s.maxSignalAgeHours),
  };
}
