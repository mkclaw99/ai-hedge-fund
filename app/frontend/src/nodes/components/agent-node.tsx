import { type NodeProps } from '@xyflow/react';
import { Bot } from 'lucide-react';
import { useEffect, useState } from 'react';

import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion';
import { CardContent } from '@/components/ui/card';
import { ModelSelector } from '@/components/ui/llm-selector';
import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { getDefaultModel, getModels, LanguageModel } from '@/data/models';
import { useNodeState } from '@/hooks/use-node-state';
import { cn } from '@/lib/utils';
import { type AgentNode } from '../types';
import { getStatusColor } from '../utils';
import { AgentOutputDialog } from './agent-output-dialog';
import { NodeShell } from './node-shell';

export function AgentNode({
  data,
  selected,
  id,
  isConnectable,
}: NodeProps<AgentNode>) {
  const { currentFlowId } = useFlowContext();
  const { getAgentNodeDataForFlow, setAgentModel, getAgentModel } = useNodeContext();
  
  // Get agent node data for the current flow
  const agentNodeData = getAgentNodeDataForFlow(currentFlowId?.toString() || null);
  const nodeData = agentNodeData[id] || { 
    status: 'IDLE', 
    ticker: null, 
    message: '', 
    messages: [],
    lastUpdated: 0
  };
  const status = nodeData.status;
  const isInProgress = status === 'IN_PROGRESS';
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  
  // Use persistent state hooks
  const [availableModels, setAvailableModels] = useNodeState<LanguageModel[]>(id, 'availableModels', []);
  const [selectedModel, setSelectedModel] = useNodeState<LanguageModel | null>(id, 'selectedModel', null);

  // Load models on mount, defaulting to the system default (Gemini) so a
  // freshly-dropped analyst uses a working model instead of "Auto", which the
  // backend resolves to OpenAI and errors on without an OpenAI key.
  useEffect(() => {
    const loadModels = async () => {
      try {
        const [models, defaultModel] = await Promise.all([getModels(), getDefaultModel()]);
        setAvailableModels(models);
        if (!selectedModel && defaultModel) {
          setSelectedModel(defaultModel);
        }
      } catch (error) {
        console.error('Failed to load models:', error);
        // Keep empty array as fallback
      }
    };

    loadModels();
  }, [setAvailableModels]);

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

  const handleUseGlobalModel = () => {
    setSelectedModel(null);
  };

  return (
    <NodeShell
      id={id}
      selected={selected}
      isConnectable={isConnectable}
      icon={<Bot className="h-5 w-5" />}
      iconColor={getStatusColor(status)}
      name={data.name || "Agent"}
      description={data.description}
      status={status}
    >
      <CardContent className="p-0">
        <div className="border-t border-border p-3">
          <div className="flex flex-col gap-2">
            <div className="text-subtitle text-primary flex items-center gap-1">
              Status
            </div>

            <div className={cn(
              "text-foreground text-xs rounded p-2 border border-status",
              isInProgress ? "gradient-animation" : getStatusColor(status)
            )}>
              <span className="capitalize">{status.toLowerCase().replace(/_/g, ' ')}</span>
            </div>
            
            {nodeData.message && (
              <div className="text-foreground text-subtitle">
                {nodeData.message !== "Done" && nodeData.message}
                {nodeData.ticker && <span className="ml-1">({nodeData.ticker})</span>}
              </div>
            )}
            <Accordion type="single" collapsible>
              <AccordionItem value="advanced" className="border-none">
                <AccordionTrigger className="!text-subtitle text-primary">
                  Advanced
                </AccordionTrigger>
                <AccordionContent className="pt-2">
                  <div className="flex flex-col gap-2">
                    <div className="text-subtitle text-primary flex items-center gap-1">
                      Model
                    </div>
                    <ModelSelector
                      models={availableModels}
                      value={selectedModel?.model_name || ""}
                      onChange={handleModelChange}
                      placeholder="Auto"
                    />
                    {selectedModel && (
                      <button
                        onClick={handleUseGlobalModel}
                        className="text-subtitle text-primary hover:text-foreground transition-colors text-left"
                      >
                        Reset to Auto
                      </button>
                    )}
                    {/* Gemini thinking budget — exposed only when a
                        Google model is selected, since the parameter is
                        ignored on other providers. Values map to the
                        Gemini thinkingConfig.thinkingBudget API:
                          off  = 0     (skip thinking; Flash supports it)
                          low  = 1024
                          med  = 8192
                          high = 24576 (Pro's max)
                          dyn  = -1    (model decides) */}
                    {selectedModel?.provider === 'Google' && (
                      <ThinkingBudgetField id={id} />
                    )}
                  </div>
                </AccordionContent>
              </AccordionItem>
            </Accordion>
          </div>
        </div>
        <AgentOutputDialog
          isOpen={isDialogOpen}
          onOpenChange={setIsDialogOpen}
          name={data.name || "Agent"}
          nodeId={id}
          flowId={currentFlowId?.toString() || null}
        />
      </CardContent>
    </NodeShell>
  );
}

// Gemini thinking-budget picker. Lives in the agent-node so each
// agent can independently dial up reasoning cost (PM might want
// 'high', a quick technical analyst 'low' or 'off'). Persisted via
// useNodeState so it survives reload, and the Play-trigger nodes
// pick it up via getNodeInternalState() when assembling the request.
const THINKING_OPTIONS: { value: string; label: string; hint: string }[] = [
  { value: 'dynamic', label: 'Dynamic (default)', hint: 'Let the model decide how much to think per prompt.' },
  { value: 'off',     label: 'Off',               hint: 'Disable thinking entirely. Fastest. Flash models only; Pro ignores this.' },
  { value: 'low',     label: 'Low (~1k tokens)',  hint: 'Light reasoning step before the answer.' },
  { value: 'medium',  label: 'Medium (~8k tokens)', hint: 'Standard chain-of-thought.' },
  { value: 'high',    label: 'High (~24k tokens)', hint: 'Maximum reasoning budget (Pro caps at 24k).' },
];

function ThinkingBudgetField({ id }: { id: string }) {
  const [value, setValue] = useNodeState<string>(id, 'thinkingBudget', 'dynamic');
  const active = THINKING_OPTIONS.find((o) => o.value === value) ?? THINKING_OPTIONS[0];
  return (
    <div className="flex flex-col gap-1 mt-1">
      <div className="text-subtitle text-primary flex items-center gap-1" title={active.hint}>
        Thinking
      </div>
      <select
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="w-full rounded border border-border bg-node/60 px-2 py-1 text-xs text-foreground focus:outline-none focus:border-primary/50"
      >
        {THINKING_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
      <div className="text-[10px] text-muted-foreground leading-snug">{active.hint}</div>
    </div>
  );
}
