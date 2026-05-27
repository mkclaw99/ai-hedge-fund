import { type NodeProps } from '@xyflow/react';
import { ListChecks } from 'lucide-react';

import { CardContent } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useNodeState } from '@/hooks/use-node-state';
import { type ResearchCompaniesNode } from '../types';
import { NodeShell } from './node-shell';

export function ResearchCompaniesNode({
  data,
  selected,
  id,
  isConnectable,
}: NodeProps<ResearchCompaniesNode>) {
  const [mandate, setMandate] = useNodeState(id, 'mandate', '');
  const [maxCompanies, setMaxCompanies] = useNodeState(id, 'maxCompanies', '10');

  return (
    <TooltipProvider>
      <NodeShell
        id={id}
        selected={selected}
        isConnectable={isConnectable}
        icon={<ListChecks className="h-5 w-5" />}
        iconColor="text-amber-500"
        name={data.name || 'Fundamental Companies'}
        description={data.description}
        width="w-80"
      >
        <CardContent className="p-0">
          <div className="border-t border-border p-3">
            <div className="flex flex-col gap-4">
              {/* Extraction mandate */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary flex items-center gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild><span>Extraction mandate</span></TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      How this researcher should pick companies from the upstream Fundamental
                      Research note. e.g. "US-listed primes + chip suppliers with real revenue;
                      avoid pre-revenue SPACs and foreign listings."
                    </TooltipContent>
                  </Tooltip>
                </div>
                <textarea
                  className="nodrag flex min-h-[64px] w-full rounded-md border border-border bg-node px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  placeholder="How should the researcher select companies? (focus, what to avoid)"
                  value={mandate}
                  onChange={(e) => setMandate(e.target.value)}
                />
              </div>

              {/* Max companies */}
              <div className="flex flex-col gap-2">
                <div className="text-subtitle text-primary flex items-center gap-1">
                  <Tooltip delayDuration={200}>
                    <TooltipTrigger asChild><span>Max companies to extract</span></TooltipTrigger>
                    <TooltipContent side="right" className="max-w-xs">
                      How many companies to hand to the Analysts (validated against Financial
                      Datasets). Higher = broader coverage but slower and more data/LLM usage.
                    </TooltipContent>
                  </Tooltip>
                </div>
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild>
                    <input
                      type="number" min={1} max={25}
                      aria-label="Max companies to extract"
                      className="nodrag h-10 w-16 rounded-md border border-border bg-node px-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                      value={maxCompanies}
                      onChange={(e) => setMaxCompanies(e.target.value.replace(/[^0-9]/g, ''))}
                    />
                  </TooltipTrigger>
                  <TooltipContent side="bottom">Number of companies (1–25)</TooltipContent>
                </Tooltip>
              </div>

              <div className="text-xs text-muted-foreground">
                Connect: Fundamental Research → Fundamental Companies → Analysts.
              </div>
            </div>
          </div>
        </CardContent>
      </NodeShell>
    </TooltipProvider>
  );
}
