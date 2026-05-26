import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { cn } from '@/lib/utils';

interface DebugConsoleTabProps {
  className?: string;
}

function statusColor(status: string): string {
  switch (status) {
    case 'COMPLETE':
      return 'text-green-500';
    case 'ERROR':
      return 'text-red-500';
    case 'IN_PROGRESS':
      return 'text-yellow-500';
    default:
      return 'text-muted-foreground';
  }
}

/**
 * Verbose diagnostics for the current flow: a one-line summary, per-agent
 * status, and the raw node/output state pretty-printed as JSON. Reads
 * node-context, so it updates live during a run.
 */
export function DebugConsoleTab({ className }: DebugConsoleTabProps) {
  const { currentFlowId } = useFlowContext();
  const { getAgentNodeDataForFlow, getOutputNodeDataForFlow } = useNodeContext();
  const flowId = currentFlowId?.toString() || null;
  const agentData = getAgentNodeDataForFlow(flowId);
  const outputData = getOutputNodeDataForFlow(flowId);

  const entries = Object.entries(agentData);
  const statusCounts = entries.reduce<Record<string, number>>((acc, [, d]) => {
    acc[d.status] = (acc[d.status] || 0) + 1;
    return acc;
  }, {});

  const summary = [
    `flow=${flowId ?? 'unsaved'}`,
    `agents=${entries.length}`,
    `output=${outputData ? 'yes' : 'none'}`,
    ...Object.entries(statusCounts).map(([s, c]) => `${s}=${c}`),
  ].join('  ·  ');

  const isEmpty = entries.length === 0 && !outputData;

  return (
    <div className={className}>
      <div className="h-full space-y-3 overflow-auto rounded-md p-3 font-mono text-xs">
        <div className="text-muted-foreground">{summary}</div>

        {isEmpty ? (
          <div className="text-muted-foreground">
            Debug console is ready. Run a flow to see raw agent state and output here.
          </div>
        ) : (
          <>
            {entries.map(([nodeId, data]) => (
              <div key={nodeId} className="space-y-1">
                <div>
                  <span className="text-cyan-500">{nodeId}</span>{' '}
                  <span className={statusColor(data.status)}>{data.status}</span>{' '}
                  <span className="text-muted-foreground">({data.messages.length} msgs)</span>
                </div>
                <pre className={cn('whitespace-pre-wrap break-all text-primary/80')}>
                  {JSON.stringify(data, null, 2)}
                </pre>
              </div>
            ))}

            {outputData && (
              <div className="space-y-1">
                <div className="text-green-500">outputData</div>
                <pre className="whitespace-pre-wrap break-all text-primary/80">
                  {JSON.stringify(outputData, null, 2)}
                </pre>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
