import { type NodeProps } from '@xyflow/react';
import { Brain } from 'lucide-react';
import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { CardContent } from '@/components/ui/card';
import { ModelSelector } from '@/components/ui/llm-selector';
import { ThinkingBudgetField } from './agent-node';
import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { getDefaultModel, getModels, LanguageModel } from '@/data/models';
import { addStateChangeListener, getNodeInternalState, useNodeState } from '@/hooks/use-node-state';
import { useOutputNodeConnection } from '@/hooks/use-output-node-connection';
import { cn } from '@/lib/utils';
import { type PortfolioManagerNode } from '../types';
import { getStatusColor } from '../utils';
import { InvestmentReportDialog } from './investment-report-dialog';
import { NodeShell } from './node-shell';

export function PortfolioManagerNode({
  data,
  selected,
  id,
  isConnectable,
}: NodeProps<PortfolioManagerNode>) {
  const { currentFlowId } = useFlowContext();
  const { getAgentNodeDataForFlow, setAgentModel, getAgentModel, getOutputNodeDataForFlow } = useNodeContext();

  // Get agent node data for the current flow
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

  // Use persistent state hooks
  const [availableModels, setAvailableModels] = useNodeState<LanguageModel[]>(
    id,
    'availableModels',
    []
  );
  const [selectedModel, setSelectedModel] = useNodeState<LanguageModel | null>(
    id,
    'selectedModel',
    null
  );

  // Load models on mount. See the agent-node.tsx version of this effect for
  // the full write-up. Short version: cannot distinguish "fresh node" from
  // "saved node, rehydrate hasn't landed yet" at mount-time. Subscribe to
  // state-manager changes; if anything for this node arrives within 1500ms
  // treat it as a rehydrate (= don't stomp the user's saved pick), else seed.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let unsubscribe: (() => void) | undefined;

    const isFreshNode = () => {
      const persisted = getNodeInternalState(id);
      return !persisted || !('selectedModel' in persisted);
    };

    const loadModels = async () => {
      try {
        const [models, defaultModel] = await Promise.all([getModels(), getDefaultModel()]);
        if (cancelled) return;
        setAvailableModels(models);
        if (!defaultModel) return;
        if (!isFreshNode()) return;

        await new Promise<void>((resolve) => {
          let resolved = false;
          const finish = () => {
            if (resolved) return;
            resolved = true;
            if (timer) clearTimeout(timer);
            unsubscribe?.();
            resolve();
          };
          timer = setTimeout(finish, 1500);
          unsubscribe = addStateChangeListener(() => {
            if (!isFreshNode()) finish();
          });
        });
        if (cancelled) return;
        if (isFreshNode()) {
          setSelectedModel(defaultModel);
        }
      } catch (error) {
        console.error('Failed to load models:', error);
      }
    };
    loadModels();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      unsubscribe?.();
    };
  }, [setAvailableModels, id]);

  // Update the node context when the model changes
  useEffect(() => {
    const flowId = currentFlowId?.toString() || null;
    const currentContextModel = getAgentModel(flowId, id);
    if (selectedModel !== currentContextModel) {
      setAgentModel(flowId, id, selectedModel);
    }
  }, [selectedModel, id, currentFlowId, setAgentModel, getAgentModel]);

  const handleModelChange = (model: LanguageModel | null) => {
    setSelectedModel(model);
  };
  
  const outputNodeData = getOutputNodeDataForFlow(currentFlowId?.toString() || null);

  // Get connected agent IDs
  const { connectedAgentIds } = useOutputNodeConnection(id);

  return (
    <>
      <NodeShell
        id={id}
        selected={selected}
        isConnectable={isConnectable}
        icon={<Brain className="h-5 w-5" />}
        iconColor={getStatusColor(status)}
        name={data.name || 'Portfolio Manager'}
        description={data.description}
        // The PM emits decisions out to the right — wired to a Trading Account
        // node (and any future downstream consumer). Without this handle, no
        // edge can attach to its output side.
        hasRightHandle={true}
        status={status}
      >
        <CardContent className="p-0">
          <div className="border-t border-border p-3">
            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary flex items-center gap-1">
                  Status
                </div>

                <div
                  className={cn(
                    'text-foreground text-xs rounded p-2 border border-status',
                    isInProgress ? 'gradient-animation' : getStatusColor(status)
                  )}
                >
                  <span className="capitalize">
                    {status.toLowerCase().replace(/_/g, ' ')}
                  </span>
                </div>
              </div>
              <div className='flex flex-col gap-2'>
                {outputNodeData && (
                  <Button
                    size="sm"
                    onClick={() => setIsDialogOpen(true)}
                  >
                    View Investment Report
                  </Button>
                )}
              </div>
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary flex items-center gap-1">
                  Model
                </div>
                <ModelSelector
                  models={availableModels}
                  value={selectedModel?.model_name || ''}
                  onChange={handleModelChange}
                  placeholder="Auto"
                />
                {/* Gemini thinking budget — same field the persona analysts
                    expose under Advanced. The PM synthesises every analyst
                    plus the track record, so its prompt is the heaviest in
                    the flow; being able to dial reasoning up to 'high' on
                    PM-only without paying that cost on every persona is
                    the usual ask. Ignored on non-Google providers. */}
                {selectedModel?.provider === 'Google' && (
                  <ThinkingBudgetField id={id} />
                )}
              </div>
            </div>
          </div>
          <InvestmentReportDialog
            isOpen={isDialogOpen}
            onOpenChange={setIsDialogOpen}
            outputNodeData={outputNodeData}
            connectedAgentIds={connectedAgentIds}
          />
        </CardContent>
      </NodeShell>
    </>
  );
}
