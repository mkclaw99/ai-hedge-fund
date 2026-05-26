import { useFlowContext } from '@/contexts/flow-context';
import { useNodeContext } from '@/contexts/node-context';
import { useEffect, useRef } from 'react';
import { getDisplayName } from './output-tab-utils';

interface TerminalTabProps {
  className?: string;
}

interface LogLine {
  key: string;
  timestamp: string;
  agent: string;
  ticker: string | null;
  message: string;
}

function formatTime(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleTimeString();
}

/**
 * Live console stream of a run: every agent's message history flattened into a
 * single chronological log. Reads node-context (reactive — re-renders as the
 * run streams in) so no polling is needed.
 */
export function TerminalTab({ className }: TerminalTabProps) {
  const { currentFlowId } = useFlowContext();
  const { getAgentNodeDataForFlow } = useNodeContext();
  const agentData = getAgentNodeDataForFlow(currentFlowId?.toString() || null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  // Flatten all agents' message histories into one chronological stream.
  const lines: LogLine[] = [];
  Object.entries(agentData).forEach(([agentId, data]) => {
    const agent = getDisplayName(agentId);
    data.messages.forEach((m, i) => {
      lines.push({
        key: `${agentId}-${i}-${m.timestamp}`,
        timestamp: m.timestamp,
        agent,
        ticker: m.ticker,
        message: m.message,
      });
    });
  });
  lines.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());

  // Stick to the bottom as new lines arrive, unless the user scrolled up.
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [lines.length]);

  return (
    <div className={className}>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="h-full overflow-auto rounded-md p-3 font-mono text-sm"
      >
        {lines.length === 0 ? (
          <div className="text-muted-foreground">
            <span className="text-blue-500">$ </span>
            Waiting for a run — output will stream here as agents report progress.
          </div>
        ) : (
          <div className="space-y-0.5 whitespace-pre-wrap">
            {lines.map((line) => (
              <div key={line.key}>
                <span className="text-muted-foreground">{formatTime(line.timestamp)} </span>
                <span className="text-cyan-500">{line.agent}</span>
                {line.ticker && <span className="text-yellow-500"> [{line.ticker}]</span>}
                <span className="text-primary"> {line.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
