import { cn } from '@/lib/utils';
import { useEdges, useNodes } from '@xyflow/react';
import { AlertCircle, CheckCircle, XCircle } from 'lucide-react';

interface ProblemsTabProps {
  className?: string;
}

type Severity = 'error' | 'warning';

interface Problem {
  id: string;
  severity: Severity;
  message: string;
}

const INPUT_TYPES = ['portfolio-start-node', 'stock-analyzer-node'];
const ANALYST_TYPE = 'agent-node';
const MANAGER_TYPE = 'portfolio-manager-node';

function nodeName(node: any): string {
  return node?.data?.name || node?.type || node?.id || 'Node';
}

/**
 * Validate a flow's graph and return any problems that would stop it from
 * running as intended. Pure function of (nodes, edges) so it's easy to reason
 * about: data → input nodes → analysts → portfolio manager.
 */
function validateFlow(nodes: any[], edges: any[]): Problem[] {
  const problems: Problem[] = [];

  if (nodes.length === 0) {
    problems.push({
      id: 'empty',
      severity: 'warning',
      message:
        'This flow is empty. Add an input node, one or more analysts, and a Portfolio Manager to run it.',
    });
    return problems;
  }

  const inputs = nodes.filter((n) => INPUT_TYPES.includes(n.type));
  const analysts = nodes.filter((n) => n.type === ANALYST_TYPE);
  const managers = nodes.filter((n) => n.type === MANAGER_TYPE);

  if (inputs.length === 0) {
    problems.push({
      id: 'no-input',
      severity: 'error',
      message: 'No input node. Add a Portfolio Input or Stock Input to feed data into your analysts.',
    });
  }
  if (analysts.length === 0) {
    problems.push({
      id: 'no-analyst',
      severity: 'error',
      message: 'No analyst nodes. Add at least one analyst to generate trading signals.',
    });
  }
  if (managers.length === 0) {
    problems.push({
      id: 'no-manager',
      severity: 'error',
      message: 'No Portfolio Manager. Add one to turn analyst signals into trading decisions.',
    });
  }

  // Connectivity checks (only meaningful once there's more than one node)
  const hasOutgoing = new Set(edges.map((e) => e.source));
  const hasIncoming = new Set(edges.map((e) => e.target));

  if (nodes.length > 1) {
    for (const n of nodes) {
      const isOrphan = !hasOutgoing.has(n.id) && !hasIncoming.has(n.id);
      if (isOrphan) {
        problems.push({
          id: `orphan-${n.id}`,
          severity: 'warning',
          message: `"${nodeName(n)}" isn't connected to anything.`,
        });
        continue; // don't pile on more specific warnings for a fully-disconnected node
      }
      if (INPUT_TYPES.includes(n.type) && !hasOutgoing.has(n.id)) {
        problems.push({
          id: `input-no-out-${n.id}`,
          severity: 'warning',
          message: `"${nodeName(n)}" isn't connected to an analyst.`,
        });
      }
      if (n.type === MANAGER_TYPE && !hasIncoming.has(n.id)) {
        problems.push({
          id: `manager-no-in-${n.id}`,
          severity: 'warning',
          message: `"${nodeName(n)}" has no analysts connected to it.`,
        });
      }
    }
  }

  return problems;
}

export function ProblemsTab({ className }: ProblemsTabProps) {
  // useNodes/useEdges are reactive — the list re-validates as the graph changes.
  const nodes = useNodes();
  const edges = useEdges();
  const problems = validateFlow(nodes, edges);

  const errorCount = problems.filter((p) => p.severity === 'error').length;
  const warningCount = problems.length - errorCount;

  return (
    <div className={className}>
      <div className="h-full bg-background/50 rounded-md p-3 text-sm overflow-auto">
        {problems.length === 0 ? (
          <div className="flex items-center gap-2 text-muted-foreground">
            <CheckCircle className="h-4 w-4 flex-shrink-0 text-green-500" />
            No problems detected
          </div>
        ) : (
          <div className="space-y-2">
            <div className="mb-2 text-xs text-muted-foreground">
              {errorCount} {errorCount === 1 ? 'error' : 'errors'}, {warningCount}{' '}
              {warningCount === 1 ? 'warning' : 'warnings'}
            </div>
            {problems.map((problem) => {
              const Icon = problem.severity === 'error' ? XCircle : AlertCircle;
              const color = problem.severity === 'error' ? 'text-red-500' : 'text-yellow-500';
              return (
                <div key={problem.id} className="flex items-start gap-2">
                  <Icon className={cn('mt-0.5 h-4 w-4 flex-shrink-0', color)} />
                  <span className="text-primary">{problem.message}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
