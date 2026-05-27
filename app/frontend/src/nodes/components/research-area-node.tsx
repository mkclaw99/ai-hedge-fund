import { useReactFlow, type NodeProps } from '@xyflow/react';
import { Check, ChevronDown, FileText, FlaskConical, Loader2, Play, Square, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

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
import { getNodeInternalState, useNodeState } from '@/hooks/use-node-state';
import { getMaterials, getThemes, MaterialsStatus, ResearchTheme, uploadMaterials } from '@/services/research-api';
import { type ResearchAreaNode } from '../types';
import { NodeShell } from './node-shell';

const SCHEDULES = [
  { value: 'off', label: 'Off (manual only)' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
];

export function ResearchAreaNode({
  data,
  selected,
  id,
  isConnectable,
}: NodeProps<ResearchAreaNode>) {
  const [theme, setTheme] = useNodeState(id, 'researchTheme', '');
  const [mandate, setMandate] = useNodeState(id, 'researchMandate', '');
  const [materials, setMaterials] = useNodeState(id, 'researchMaterials', '');
  const [schedule, setSchedule] = useNodeState(id, 'researchSchedule', 'off');
  const [runError, setRunError] = useState<string | null>(null);
  const [themes, setThemes] = useState<ResearchTheme[]>([]);
  const [open, setOpen] = useState(false);
  const [schedOpen, setSchedOpen] = useState(false);
  const [pdf, setPdf] = useState<MaterialsStatus>({ has_brief: false });
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

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

  useEffect(() => {
    if (currentFlowId != null) getMaterials(currentFlowId).then(setPdf);
  }, [currentFlowId]);

  const showAsProcessing = isConnecting || isConnected || isProcessing;
  const canRunResearch = canRun && theme.trim() !== '';
  const selectedTheme = themes.find((t) => t.slug === theme);

  const handlePdfUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.currentTarget.files?.[0];
    e.currentTarget.value = ''; // allow re-selecting the same file
    if (!file || currentFlowId == null) return;
    setUploading(true);
    setUploadError(null);
    try {
      const res = await uploadMaterials(currentFlowId, file);
      setPdf({ has_brief: true, filename: res.filename, brief: res.brief });
    } catch (err: any) {
      setUploadError(err?.message || 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  const handlePlay = () => {
    expandBottomPanel();
    setBottomPanelTab('output');

    const allNodes = getNodes();
    const allEdges = getEdges();
    const reachable = new Set<string>();
    const visited = new Set<string>();
    const dfs = (nodeId: string) => {
      if (visited.has(nodeId)) return;
      visited.add(nodeId);
      if (nodeId !== id) reachable.add(nodeId);
      for (const e of allEdges.filter((edge) => edge.source === nodeId)) dfs(e.target);
    };
    dfs(id);

    // The Fundamental Companies node extracts the universe — it's required.
    const fcNode = allNodes.find((n) => reachable.has(n.id) && n.type === 'research-companies-node');
    if (!fcNode) {
      setRunError('Connect a Fundamental Companies node: Fundamental Research → Fundamental Companies → Analysts.');
      return;
    }
    setRunError(null);
    const fcState = getNodeInternalState(fcNode.id) || {};
    const companyMandate = (fcState.mandate as string) || '';
    const companyMax = parseInt(fcState.maxCompanies as string, 10) || 10;

    // Resource/input nodes don't execute in the backend graph.
    const agentNodes = allNodes.filter(
      (node) => reachable.has(node.id)
        && node.type !== 'memory-node'
        && node.type !== 'research-area-node'
        && node.type !== 'research-companies-node',
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
      research_mandate: mandate || undefined,
      research_company_mandate: companyMandate || undefined,
      research_materials: materials || undefined,
      research_max_companies: companyMax,
      research_schedule: schedule,
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
        name={data.name || 'Fundamental Research'}
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
                    <TooltipContent side="right" className="max-w-xs">
                      An analyst research theme. Its companies are discovered and validated into a
                      tradable universe when you run.
                    </TooltipContent>
                  </Tooltip>
                </div>
                <Popover open={open} onOpenChange={setOpen}>
                  <PopoverTrigger asChild>
                    <Button variant="outline" role="combobox" aria-expanded={open}
                      className="justify-between h-10 px-3 py-2 bg-node border border-border hover:bg-accent">
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
                            <CommandItem key={t.slug} value={t.slug} className="cursor-pointer bg-node hover:bg-accent"
                              onSelect={(v) => { setTheme(v); setOpen(false); }}>
                              {t.name} <span className="ml-1 text-muted-foreground">({t.company_count ?? 0})</span>
                            </CommandItem>
                          ))}
                        </CommandGroup>
                      </CommandList>
                    </Command>
                  </PopoverContent>
                </Popover>
              </div>

              {/* Researcher mandate — the lens driving the research */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary flex items-center gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild><span>Researcher mandate</span></TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      The lens of the researcher driving this. It shapes which companies get
                      extracted and how the research note is framed. e.g. "favor US-listed primes
                      with real revenue; avoid pre-revenue SPACs and foreign listings."
                    </TooltipContent>
                  </Tooltip>
                </div>
                <textarea
                  className="nodrag flex min-h-[52px] w-full rounded-md border border-border bg-node px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  placeholder="How should the researcher approach this? (focus, style, what to avoid)"
                  value={mandate}
                  onChange={(e) => setMandate(e.target.value)}
                />
              </div>

              {/* Materials: notes + PDF information base */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary flex items-center gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild><span>Materials</span></TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      Notes and/or a PDF "information base". A PDF is distilled into a short brief and
                      injected into every analyst as grounding (the full text is kept on file).
                    </TooltipContent>
                  </Tooltip>
                </div>
                <textarea
                  className="nodrag flex min-h-[72px] w-full rounded-md border border-border bg-node px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  placeholder="e.g. Focus on fabless designers with defense backlog; avoid commodity memory…"
                  value={materials}
                  onChange={(e) => setMaterials(e.target.value)}
                />
                <input ref={fileRef} type="file" accept="application/pdf,.pdf" className="hidden" onChange={handlePdfUpload} />
                <div className="flex items-center gap-2">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <span tabIndex={0} className="inline-flex">
                        <Button
                          variant="outline" size="sm"
                          className="nodrag h-8 gap-1.5 text-xs"
                          onClick={() => fileRef.current?.click()}
                          disabled={uploading || currentFlowId == null}
                        >
                          {uploading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
                          {uploading ? 'Distilling…' : 'Upload PDF'}
                        </Button>
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="bottom" className="max-w-xs">
                      {currentFlowId == null
                        ? 'Save the flow first, then upload a PDF information base.'
                        : 'Upload a PDF; it’s distilled into a brief and used as grounding.'}
                    </TooltipContent>
                  </Tooltip>
                  {pdf.has_brief && pdf.filename && (
                    <span className="text-xs text-muted-foreground flex items-center gap-1 truncate" title={pdf.filename}>
                      <FileText className="h-3.5 w-3.5 text-green-500 shrink-0" /> {pdf.filename}
                    </span>
                  )}
                </div>
                {uploadError && <span className="text-xs text-red-500">{uploadError}</span>}
              </div>

              {/* Auto-refresh schedule */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary flex items-center gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild><span>Auto-refresh</span></TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      How often the backend re-pulls this theme from analyst and re-runs the analysis
                      automatically (even with the app closed). Runs the last configuration you ran here.
                    </TooltipContent>
                  </Tooltip>
                </div>
                <Popover open={schedOpen} onOpenChange={setSchedOpen}>
                  <PopoverTrigger asChild>
                    <Button variant="outline" role="combobox" aria-expanded={schedOpen}
                      className="justify-between h-10 px-3 py-2 bg-node border border-border hover:bg-accent">
                      <span className="text-subtitle">{SCHEDULES.find((s) => s.value === schedule)?.label ?? 'Off (manual only)'}</span>
                      <ChevronDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent className="w-[var(--radix-popover-trigger-width)] p-0 bg-node border border-border shadow-lg">
                    <Command className="bg-node">
                      <CommandList className="bg-node">
                        <CommandGroup>
                          {SCHEDULES.map((s) => (
                            <CommandItem key={s.value} value={s.value} className="cursor-pointer bg-node hover:bg-accent"
                              onSelect={(v) => { setSchedule(v); setSchedOpen(false); }}>
                              {schedule === s.value && <Check className="mr-2 h-3.5 w-3.5" />}
                              {s.label}
                            </CommandItem>
                          ))}
                        </CommandGroup>
                      </CommandList>
                    </Command>
                  </PopoverContent>
                </Popover>
              </div>

              {/* Run — requires a downstream Fundamental Companies node */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary">Run</div>
                <div className="flex gap-2 items-center">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild>
                      <span tabIndex={0} className="inline-flex">
                        <Button
                          size="icon"
                          variant="secondary"
                          className="flex-shrink-0 transition-all duration-200 hover:bg-primary hover:text-primary-foreground active:scale-95"
                          onClick={showAsProcessing ? stopFlow : handlePlay}
                          disabled={!canRunResearch && !showAsProcessing}
                        >
                          {showAsProcessing ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                        </Button>
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="bottom" className="max-w-xs">
                      {showAsProcessing
                        ? 'Stop the run'
                        : canRunResearch
                          ? 'Researches the topic, then the connected Fundamental Companies node extracts the universe and the Analysts run.'
                          : 'Select a theme first'}
                    </TooltipContent>
                  </Tooltip>
                </div>
                {runError && <span className="text-xs text-red-500">{runError}</span>}
              </div>
            </div>
          </div>
        </CardContent>
      </NodeShell>
    </TooltipProvider>
  );
}
