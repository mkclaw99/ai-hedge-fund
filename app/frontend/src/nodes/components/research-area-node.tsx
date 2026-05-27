import { useReactFlow, type NodeProps } from '@xyflow/react';
import { ChevronDown, FlaskConical, Play, Square } from 'lucide-react';
import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { CardContent } from '@/components/ui/card';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandList,
} from '@/components/ui/command';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useFlowContext } from '@/contexts/flow-context';
import { useLayoutContext } from '@/contexts/layout-context';
import { useNodeContext } from '@/contexts/node-context';
import { useFlowConnection } from '@/hooks/use-flow-connection';
import { useNodeState } from '@/hooks/use-node-state';
import { getThemes, ResearchTheme } from '@/services/research-api';
import { type ResearchAreaNode } from '../types';
import { NodeShell } from './node-shell';

export function ResearchAreaNode({
  data,
  selected,
  id,
  isConnectable,
}: NodeProps<ResearchAreaNode>) {
  const [theme, setTheme] = useNodeState(id, 'researchTheme', '');
  const [materials, setMaterials] = useNodeState(id, 'researchMaterials', '');
  const [maxCompanies, setMaxCompanies] = useNodeState(id, 'researchMaxCompanies', '10');
  const [themes, setThemes] = useState<ResearchTheme[]>([]);
  const [open, setOpen] = useState(false);

  const { currentFlowId } = useFlowContext();
  const { getAllAgentModels } = useNodeContext();
  const { getNodes, getEdges } = useReactFlow();
  const { setBottomPanelTab, expandBottomPanel } = useLayoutContext();

  const flowId = currentFlowId?.toString() || null;
  const { isConnecting, isConnected, isProcessing, canRun, runFlow, stopFlow } =
    useFlowConnection(flowId);

  useEffect(() => {
    getThemes().then(setThemes);
  }, []);

  const showAsProcessing = isConnecting || isConnected || isProcessing;
  const canRunResearch = canRun && theme.trim() !== '';
  const selectedTheme = themes.find((t) => t.slug === theme);

  const handlePlay = () => {
    expandBottomPanel();
    setBottomPanelTab('output');

    const allNodes = getNodes();
    const allEdges = getEdges();

    // DFS for nodes reachable downstream of this Research Area node.
    const reachable = new Set<string>();
    const visited = new Set<string>();
    const dfs = (nodeId: string) => {
      if (visited.has(nodeId)) return;
      visited.add(nodeId);
      if (nodeId !== id) reachable.add(nodeId);
      for (const e of allEdges.filter((edge) => edge.source === nodeId)) dfs(e.target);
    };
    dfs(id);

    // Resource/input nodes don't execute in the backend graph.
    const agentNodes = allNodes.filter(
      (node) => reachable.has(node.id) && node.type !== 'memory-node' && node.type !== 'research-area-node',
    );
    const reachableIds = new Set([id, ...reachable]);
    const validEdges = allEdges.filter(
      (edge) => reachableIds.has(edge.source) && reachableIds.has(edge.target),
    );

    const agentModels = [];
    const allAgentModels = getAllAgentModels(flowId);
    for (const node of agentNodes) {
      const model = allAgentModels[node.id];
      if (model) {
        agentModels.push({ agent_id: node.id, model_name: model.model_name, model_provider: model.provider as any });
      }
    }

    runFlow({
      tickers: [], // resolved by the backend from the theme
      research_theme: theme,
      research_materials: materials || undefined,
      research_max_companies: parseInt(maxCompanies, 10) || 10,
      graph_nodes: agentNodes.map((node) => ({ id: node.id, type: node.type, data: node.data, position: node.position })),
      graph_edges: validEdges,
      agent_models: agentModels,
      model_name: undefined,
      model_provider: undefined,
    });
  };

  return (
    <TooltipProvider>
      <NodeShell
        id={id}
        selected={selected}
        isConnectable={isConnectable}
        icon={<FlaskConical className="h-5 w-5" />}
        iconColor="text-amber-500"
        name={data.name || 'Research Area'}
        description={data.description}
        hasLeftHandle={false}
        status={showAsProcessing ? 'IN_PROGRESS' : 'IDLE'}
        width="w-80"
      >
        <CardContent className="p-0">
          <div className="border-t border-border p-3">
            <div className="flex flex-col gap-4">
              {/* Theme */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary flex items-center gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild><span>Theme</span></TooltipTrigger>
                    <TooltipContent side="right">
                      An analyst research theme. Its companies are discovered and validated into a
                      tradable universe when you run.
                    </TooltipContent>
                  </Tooltip>
                </div>
                <Popover open={open} onOpenChange={setOpen}>
                  <PopoverTrigger asChild>
                    <Button
                      variant="outline"
                      role="combobox"
                      aria-expanded={open}
                      className="justify-between h-10 px-3 py-2 bg-node border border-border hover:bg-accent"
                    >
                      <span className="text-subtitle truncate">
                        {selectedTheme ? `${selectedTheme.name} (${selectedTheme.company_count ?? 0})` : 'Select a theme…'}
                      </span>
                      <ChevronDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent className="w-[var(--radix-popover-trigger-width)] p-0 bg-node border border-border shadow-lg">
                    <Command className="bg-node">
                      <CommandList className="bg-node">
                        <CommandEmpty>No themes (is analyst reachable?)</CommandEmpty>
                        <CommandGroup>
                          {themes.map((t) => (
                            <CommandItem
                              key={t.slug}
                              value={t.slug}
                              className="cursor-pointer bg-node hover:bg-accent"
                              onSelect={(v) => { setTheme(v); setOpen(false); }}
                            >
                              {t.name} <span className="ml-1 text-muted-foreground">({t.company_count ?? 0})</span>
                            </CommandItem>
                          ))}
                        </CommandGroup>
                      </CommandList>
                    </Command>
                  </PopoverContent>
                </Popover>
              </div>

              {/* Materials */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary flex items-center gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild><span>Materials</span></TooltipTrigger>
                    <TooltipContent side="right">
                      Optional notes/thesis. Injected into every analyst as background grounding.
                    </TooltipContent>
                  </Tooltip>
                </div>
                <textarea
                  className="nodrag flex min-h-[72px] w-full rounded-md border border-border bg-node px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  placeholder="e.g. Focus on fabless designers with defense backlog; avoid commodity memory…"
                  value={materials}
                  onChange={(e) => setMaterials(e.target.value)}
                />
              </div>

              {/* Max companies + Run */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary">Run</div>
                <div className="flex gap-2 items-center">
                  <input
                    type="number"
                    min={1}
                    max={25}
                    title="Max companies to analyze"
                    className="nodrag h-10 w-16 rounded-md border border-border bg-node px-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    value={maxCompanies}
                    onChange={(e) => setMaxCompanies(e.target.value.replace(/[^0-9]/g, ''))}
                  />
                  <Button
                    size="icon"
                    variant="secondary"
                    className="flex-shrink-0 transition-all duration-200 hover:bg-primary hover:text-primary-foreground active:scale-95"
                    title={showAsProcessing ? 'Stop' : 'Discover companies and run'}
                    onClick={showAsProcessing ? stopFlow : handlePlay}
                    disabled={!canRunResearch && !showAsProcessing}
                  >
                    {showAsProcessing ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                  </Button>
                </div>
              </div>
            </div>
          </div>
        </CardContent>
      </NodeShell>
    </TooltipProvider>
  );
}
