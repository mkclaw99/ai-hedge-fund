import { type NodeProps } from '@xyflow/react';
import { Brain } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { CardContent } from '@/components/ui/card';
import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { FlowMemory, getFlowMemory } from '@/services/memory-api';
import { type MemoryNode as MemoryNodeType } from '../types';
import { MemoryDialog } from './memory-dialog';
import { NodeShell } from './node-shell';

export function MemoryNode({ data, selected, id, isConnectable }: NodeProps<MemoryNodeType>) {
  const { currentFlowId } = useFlowContext();
  const { getOutputNodeDataForFlow } = useNodeContext();

  // Derive a primitive signature of the latest run so we refetch when (and only
  // when) a run completes — depending on the object directly would loop.
  const flowKey = currentFlowId?.toString() || null;
  const outputNodeData = getOutputNodeDataForFlow(flowKey);
  const runSig = outputNodeData
    ? `${Object.keys(outputNodeData.decisions || {}).length}:${Object.keys(outputNodeData.analyst_signals || {}).length}`
    : '';

  const [memory, setMemory] = useState<FlowMemory | null>(null);
  const [loading, setLoading] = useState(false);
  const [showDetails, setShowDetails] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setMemory(await getFlowMemory(currentFlowId));
    } catch {
      setMemory({ flow_id: currentFlowId, tickers: [] });
    } finally {
      setLoading(false);
    }
  }, [currentFlowId]);

  // Load on mount / flow change, and again whenever a run completes.
  useEffect(() => {
    refresh();
  }, [refresh, runSig]);

  const tickers = memory?.tickers ?? [];
  const summary = (() => {
    if (loading && !memory) return 'Loading…';
    if (!tickers.length) return 'No memory yet';
    if (tickers.length === 1) {
      const t = tickers[0];
      return `${t.ticker}: ${t.consensus} · ${t.n_insights} insight(s)`;
    }
    const total = tickers.reduce((n, t) => n + t.n_insights, 0);
    return `${tickers.length} tickers · ${total} insight(s)`;
  })();

  return (
    <>
      <NodeShell
        id={id}
        selected={selected}
        isConnectable={isConnectable}
        icon={<Brain className="h-5 w-5" />}
        iconColor="text-purple-500"
        name={data.name || 'Memory'}
        description={data.description}
        // Standalone resource: memory is read/written automatically per flow, not
        // wired into the graph — so it has no connection handles.
        hasLeftHandle={false}
        hasRightHandle={false}
      >
        <CardContent className="p-0">
          <div className="border-t border-border p-3">
            <div className="flex flex-col gap-2">
              <div className="text-subtitle text-muted-foreground">Flow research memory</div>
              <div className="text-sm text-primary">{summary}</div>
              <Button
                variant="secondary"
                size="sm"
                className="mt-1 nodrag"
                onClick={(e) => {
                  e.stopPropagation();
                  refresh();
                  setShowDetails(true);
                }}
              >
                View memory
              </Button>
            </div>
          </div>
        </CardContent>
      </NodeShell>
      <MemoryDialog
        isOpen={showDetails}
        onOpenChange={setShowDetails}
        memory={memory}
        loading={loading}
      />
    </>
  );
}
