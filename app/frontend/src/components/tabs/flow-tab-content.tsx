import { Flow } from '@/components/Flow';
import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { useTabsContext } from '@/contexts/tabs-context';
import { extractBaseAgentKey } from '@/data/node-mappings';
import { setNodeInternalState, setCurrentFlowId as setNodeStateFlowId } from '@/hooks/use-node-state';
import { cn } from '@/lib/utils';
import { flowService } from '@/services/flow-service';
import { getFlowMemory } from '@/services/memory-api';
import { Flow as FlowType } from '@/types/flow';
import { useEffect } from 'react';

// Import the flow connection manager to check if flow is actively running

interface FlowTabContentProps {
  flow: FlowType;
  className?: string;
}

export function FlowTabContent({ flow, className }: FlowTabContentProps) {
  const { loadFlow } = useFlowContext();
  const { activeTabId } = useTabsContext();
  const { getAgentNodeDataForFlow, updateAgentNode, setOutputNodeData } = useNodeContext();

  // Enhanced load function that restores both use-node-state and node context data
  const loadFlowWithCompleteState = async (flowToLoad: FlowType) => {
    try {
      const flowId = flowToLoad.id.toString();

      // First, set the flow ID for node state isolation
      setNodeStateFlowId(flowId);

      console.log(`[FlowTabContent] Loading flow ${flowId}, preserving all state (configuration + runtime)`);

      // Load the flow using the basic context function (handles React Flow state)
      await loadFlow(flowToLoad);

      // Then restore internal states for each node (use-node-state data)
      if (flowToLoad.nodes) {
        flowToLoad.nodes.forEach((node: any) => {
          if (node.data?.internal_state) {
            setNodeInternalState(node.id, node.data.internal_state);
          }
        });
      }

      // Rehydrate runtime data from the wiki (deliberately scoped: only
      // when NodeContext is empty for this flow, so an active run or
      // already-warm tab session isn't trampled). This makes agent
      // statuses and per-ticker reasoning survive a page reload — the
      // run results have always been persisted via ingest_run, the
      // frontend just didn't read them back.
      await rehydrateFromMemory(flowToLoad, flowId);
    } catch (error) {
      console.error('Failed to load flow with complete state:', error);
      throw error;
    }
  };

  const rehydrateFromMemory = async (flowToLoad: FlowType, flowId: string) => {
    // Skip when any node already carries messages — that means either an
    // active run is streaming or a prior tab activation already
    // rehydrated. Either way the in-memory state is fresher than the
    // wiki and overwriting would erase live progress.
    const existing = getAgentNodeDataForFlow(flowId);
    const hasRuntimeData = Object.values(existing).some((d) => (d.messages?.length ?? 0) > 0);
    if (hasRuntimeData) {
      console.log(`[FlowTabContent] Flow ${flowId} already has runtime data; skipping rehydrate.`);
      return;
    }

    try {
      const flowIdNum = Number(flowId);
      if (!Number.isFinite(flowIdNum)) return;
      const memory = await getFlowMemory(flowIdNum);
      const tickersMem = memory.tickers ?? [];
      if (tickersMem.length === 0) return;

      // Wiki rows are keyed by `normalize_analyst_name(agent_id)` —
      // 'forecaster_agent' → 'Forecaster', 'warren_buffett_agent' →
      // 'Warren Buffett', etc. To match a flow node back to its row,
      // strip the unique id suffix and derive the same name.
      const wikiNameFromKey = (key: string) =>
        key
          .replace(/_agent$/, '')
          .replace(/_/g, ' ')
          .split(' ')
          .filter(Boolean)
          .map((w) => w[0].toUpperCase() + w.slice(1))
          .join(' ');

      const aliasesFor = (key: string): string[] => {
        const base = wikiNameFromKey(key);
        // Forecaster gets a hand-coded alias because its display name
        // ("Time Series Forecaster") doesn't derive from the agent_id —
        // any future analyst with the same disconnect should be added
        // here.
        if (key === 'forecaster') return [base, 'Time Series Forecaster'];
        return [base];
      };

      // Synthesize a stable timestamp from the wiki date so updateAgentNode's
      // de-duplication doesn't append the same message twice on a re-fetch.
      const tsFor = (date: string, ticker: string) => `${date}T00:00:00.000Z#${ticker}`;

      // Build analyst_signals shape for OutputNode + bottom panel consumers.
      const analystSignals: Record<string, Record<string, unknown>> = {};
      const decisions: Record<string, unknown> = {};

      // Forecaster canvas nodes carry two backbones internally (Chronos-2
      // and Toto-2.0). Rehydrate from BOTH wiki analyst names ("Forecaster"
      // / "Toto Forecaster") and dispatch each backbone's rows to the
      // synthesised per-backbone agent_id so the forecaster-node's
      // per-backbone reader picks them up. We dispatch both backbones'
      // rows regardless of the canvas's current backbones selection so
      // toggling Toto on after a Chronos-only run still surfaces the
      // existing wiki data instantly.
      const FORECASTER_ROW_ALIASES: Record<'chronos2' | 'toto2', string[]> = {
        chronos2: ['forecaster', 'time series forecaster'],
        toto2: ['toto forecaster'],
      };
      const forecasterAgentIdFor = (canvasId: string, b: 'chronos2' | 'toto2'): string => {
        const suffix = canvasId.replace(/^toto_forecaster_|^forecaster_/, '');
        return b === 'toto2' ? `toto_forecaster_${suffix}` : `forecaster_${suffix}`;
      };

      for (const node of flowToLoad.nodes ?? []) {
        const baseKey = extractBaseAgentKey(node.id);

        // Forecaster path — special-cased before the generic analyst
        // path because one canvas node maps to two wiki analyst names.
        if (node.type === 'forecaster-node') {
          let anyDispatched = false;
          for (const b of ['chronos2', 'toto2'] as const) {
            const aid = forecasterAgentIdFor(node.id, b);
            const aliases = FORECASTER_ROW_ALIASES[b];
            let dispatchedThisBackbone = 0;
            for (const t of tickersMem) {
              const row = (t.analysts ?? []).find((a) => aliases.includes((a.analyst ?? '').toLowerCase()));
              if (!row?.reasoning) continue;
              updateAgentNode(flowId, aid, {
                timestamp: tsFor(row.date, t.ticker),
                message: 'Done',
                ticker: t.ticker,
                analysis: row.reasoning,
              });
              dispatchedThisBackbone += 1;
              // analyst_signals key for OutputNode mirror.
              const agentKey = `${b === 'toto2' ? 'toto_forecaster' : 'forecaster'}_agent`;
              analystSignals[agentKey] = analystSignals[agentKey] || {};
              (analystSignals[agentKey] as Record<string, unknown>)[t.ticker] = {
                signal: row.signal,
                confidence: row.confidence,
                reasoning: row.reasoning,
              };
            }
            if (dispatchedThisBackbone > 0) {
              updateAgentNode(flowId, aid, 'COMPLETE');
              anyDispatched = true;
            }
          }
          // Drive the canvas-node id status too so the NodeShell pill
          // shows COMPLETE on first load (combinedStatus rolls up per-
          // backbone, but the legacy code paths that read the canvas id
          // directly still expect a status on it).
          if (anyDispatched) updateAgentNode(flowId, node.id, 'COMPLETE');
          continue;
        }

        // Portfolio Manager path — pull pm_decisions per ticker.
        if (baseKey === 'portfolio_manager') {
          const messages: any[] = [];
          for (const t of tickersMem) {
            const dec = (t.pm_decisions ?? [])[0];
            if (!dec?.reasoning) continue;
            messages.push({
              timestamp: tsFor(dec.date, t.ticker),
              message: 'Done',
              ticker: t.ticker,
              analysis: dec.reasoning,
            });
            decisions[t.ticker] = {
              action:
                dec.signal === 'bullish' ? 'buy' : dec.signal === 'bearish' ? 'sell' : 'hold',
              confidence: dec.confidence,
              reasoning: dec.reasoning,
            };
          }
          if (messages.length > 0) {
            for (const m of messages) updateAgentNode(flowId, node.id, m);
            updateAgentNode(flowId, node.id, 'COMPLETE');
          }
          continue;
        }

        // Analyst path — match the node's base key to one of the
        // wiki's analyst-name aliases, then dispatch a synthetic
        // 'Done' message per ticker that carries the full reasoning
        // Markdown. The ForecasterNode and AgentOutputDialog both
        // read this exact channel, so no further plumbing needed.
        const aliases = aliasesFor(baseKey).map((s) => s.toLowerCase());
        let dispatched = 0;
        for (const t of tickersMem) {
          const row = (t.analysts ?? []).find((a) => aliases.includes((a.analyst ?? '').toLowerCase()));
          if (!row?.reasoning) continue;
          updateAgentNode(flowId, node.id, {
            timestamp: tsFor(row.date, t.ticker),
            message: 'Done',
            ticker: t.ticker,
            analysis: row.reasoning,
          });
          dispatched += 1;
          // Mirror into analyst_signals for OutputNode etc.
          const agentKey = `${baseKey}_agent`;
          analystSignals[agentKey] = analystSignals[agentKey] || {};
          (analystSignals[agentKey] as Record<string, unknown>)[t.ticker] = {
            signal: row.signal,
            confidence: row.confidence,
            reasoning: row.reasoning,
          };
        }
        if (dispatched > 0) updateAgentNode(flowId, node.id, 'COMPLETE');
      }

      // Output panel rehydration — only seed if either side has content.
      if (Object.keys(decisions).length > 0 || Object.keys(analystSignals).length > 0) {
        setOutputNodeData(flowId, {
          decisions,
          analyst_signals: analystSignals,
        });
      }
    } catch (e) {
      // Fail-open: a missing wiki, network blip, or schema drift just
      // leaves the freshly-loaded flow in its default empty state.
      console.warn('[FlowTabContent] rehydrate from memory failed:', e);
    }
  };

  // Fetch the latest flow state when this tab becomes active
  useEffect(() => {
    const isThisTabActive = activeTabId === `flow-${flow.id}`;
    
    if (isThisTabActive) {
      const fetchAndLoadFlow = async () => {
        try {
          // Fetch the latest flow data from the backend
          const latestFlow = await flowService.getFlow(flow.id);
          // Load the fresh flow data with complete state restoration
          await loadFlowWithCompleteState(latestFlow);
        } catch (error) {
          console.error('Failed to fetch latest flow state:', error);
          // Fallback to loading the cached flow data with complete state restoration
          await loadFlowWithCompleteState(flow);
        }
      };

      fetchAndLoadFlow();
    }
  }, [activeTabId, flow.id, flow, loadFlow]);

  return (
    <div className={cn("h-full w-full", className)}>
      <Flow />
    </div>
  );
} 