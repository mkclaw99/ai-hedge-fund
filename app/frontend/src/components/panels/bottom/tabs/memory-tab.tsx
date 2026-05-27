import { Button } from '@/components/ui/button';
import { useFlowContext } from '@/contexts/flow-context';
import { MemoryView } from '@/nodes/components/memory-view';
import { FlowMemory, getFlowMemory } from '@/services/memory-api';
import { Loader2, RotateCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

interface MemoryTabProps {
  className?: string;
}

/** Bottom-panel tab showing the current flow's accumulated research memory.
 *  Mounts (and fetches) when the user opens the tab; refreshable. */
export function MemoryTab({ className }: MemoryTabProps) {
  const { currentFlowId } = useFlowContext();
  const [memory, setMemory] = useState<FlowMemory | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setMemory(await getFlowMemory(currentFlowId ?? null));
    } catch {
      setMemory(null);
    } finally {
      setLoading(false);
    }
  }, [currentFlowId]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className={`flex flex-col ${className ?? ''}`}>
      <div className="flex items-center justify-between gap-2 mb-2 flex-shrink-0">
        <div className="text-sm text-muted-foreground">
          This flow&apos;s research memory — accumulates each run. Analysts read back their own
          prior calls; the Portfolio Manager reads everything.
        </div>
        <Button variant="outline" size="sm" onClick={load} disabled={loading} className="flex-shrink-0">
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5 mr-1.5" />}
          {loading ? '' : 'Refresh'}
        </Button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto pr-1">
        <MemoryView memory={memory} loading={loading} />
      </div>
    </div>
  );
}
