import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { useNodeContext } from '@/contexts/node-context';
import { formatTicker, useTickerNames } from '@/lib/ticker-names';
import { FlowMemory, MemoryAnalystRow, getFlowMemory } from '@/services/memory-api';
import { formatTimeFromTimestamp } from '@/utils/date-utils';

/** Mirror of src/memory/ingest.normalize_analyst_name — turns a node id like
 *  "warren_buffett_a1b2c3" into the display name ("Warren Buffett") under which
 *  the memory store keys its insights. */
function normalizeAnalystName(agentId: string): string {
  let n = (agentId || '').replace(/_agent$/, '');
  n = n.replace(/_[a-z0-9]{6}$/, ''); // strip the unique 6-char node-id suffix
  n = n.replace(/_/g, ' ').trim();
  if (!n) return agentId;
  return n
    .split(/\s+/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(' ');
}
import { AlignJustify, Copy, Loader2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Styled element map so analyst reports (Markdown) render cleanly in the dialog.
const MD_COMPONENTS = {
  h1: (p: any) => <h1 className="text-lg font-semibold text-primary mt-4 mb-2 first:mt-0" {...p} />,
  h2: (p: any) => <h2 className="text-base font-semibold text-primary mt-4 mb-2 first:mt-0 border-b border-border pb-1" {...p} />,
  h3: (p: any) => <h3 className="text-sm font-semibold text-primary mt-3 mb-1" {...p} />,
  p: (p: any) => <p className="mb-3 leading-7" {...p} />,
  ul: (p: any) => <ul className="list-disc pl-5 mb-3 space-y-1" {...p} />,
  ol: (p: any) => <ol className="list-decimal pl-5 mb-3 space-y-1" {...p} />,
  li: (p: any) => <li className="leading-6" {...p} />,
  strong: (p: any) => <strong className="font-semibold text-primary" {...p} />,
  em: (p: any) => <em className="italic" {...p} />,
  code: (p: any) => <code className="rounded bg-muted px-1 py-0.5 text-[0.85em]" {...p} />,
  pre: (p: any) => <pre className="mb-3 overflow-x-auto rounded-md bg-muted/50 p-3 text-[0.85em]" {...p} />,
  table: (p: any) => <table className="w-full text-sm border-collapse mb-3" {...p} />,
  th: (p: any) => <th className="border border-border px-2 py-1 text-left bg-muted/40" {...p} />,
  td: (p: any) => <td className="border border-border px-2 py-1" {...p} />,
  a: (p: any) => <a className="text-blue-500 underline" target="_blank" rel="noreferrer" {...p} />,
  blockquote: (p: any) => <blockquote className="border-l-2 border-border pl-3 italic text-muted-foreground mb-3" {...p} />,
  hr: () => <hr className="my-4 border-border" />,
};

interface AgentOutputDialogProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  name: string;
  nodeId: string;
  flowId: string | null;
}

export function AgentOutputDialog({ 
  isOpen, 
  onOpenChange, 
  name, 
  nodeId,
  flowId
}: AgentOutputDialogProps) {
  const { getAgentNodeDataForFlow } = useNodeContext();
  
  // Use the passed flowId instead of getting it from flow context
  const agentNodeData = getAgentNodeDataForFlow(flowId);
  const nodeData = agentNodeData[nodeId] || { 
    status: 'IDLE', 
    ticker: null, 
    message: '', 
    messages: [],
    lastUpdated: 0
  };

  const messages = nodeData.messages || [];
  const nodeStatus = nodeData.status;
  
  const [copySuccess, setCopySuccess] = useState(false);
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const initialFocusRef = useRef<HTMLDivElement>(null);

  // Collect all analysis from all messages into a single analysis dictionary
  const allAnalysis = messages
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()) // Sort by timestamp
    .reduce<Record<string, string>>((acc, msg) => {
      // Add analysis from this message to our accumulated analysis
      if (msg.analysis && Object.keys(msg.analysis).length > 0) {
        // Filter out null values before adding to our accumulated decisions
        const validDecisions = Object.entries(msg.analysis)
          .filter(([_, value]) => value !== null && value !== undefined)
          .reduce((obj, [key, value]) => {
            obj[key] = value;
            return obj;
          }, {} as Record<string, string>);
        
        if (Object.keys(validDecisions).length > 0) {
          // Combine with accumulated decisions, newer messages overwrite older ones for the same ticker
          return { ...acc, ...validDecisions };
        }
      }
      return acc;
    }, {});

  // Runtime data (this session) — keyed by ticker → reasoning text.
  const runtimeTickers = Object.keys(allAnalysis);

  // Persistent fallback from the flow's memory: when the dialog opens we look up
  // this analyst's most recent insight per ticker so a reload doesn't show "empty".
  const myAnalystName = normalizeAnalystName(nodeId);
  const [memFallback, setMemFallback] = useState<Record<string, MemoryAnalystRow>>({});

  useEffect(() => {
    if (!isOpen || flowId == null) return;
    let cancelled = false;
    getFlowMemory(Number(flowId))
      .then((mem: FlowMemory) => {
        if (cancelled) return;
        const map: Record<string, MemoryAnalystRow> = {};
        for (const t of mem.tickers || []) {
          const rows = (t.analysts || []).filter((r) => r.analyst === myAnalystName);
          if (rows.length === 0) continue;
          // Pick the most recent insight (string-sortable ISO dates).
          const latest = [...rows].sort((a, b) => (b.date || '').localeCompare(a.date || ''))[0];
          if (latest?.reasoning) map[t.ticker] = latest;
        }
        setMemFallback(map);
      })
      .catch(() => setMemFallback({}));
    return () => { cancelled = true; };
  }, [isOpen, flowId, myAnalystName]);

  // Union: tickers visible in the dialog = runtime ∪ memory fallback.
  const tickersWithDecisions = Array.from(
    new Set([...runtimeTickers, ...Object.keys(memFallback)]),
  );
  // Resolve "Coherent (COHR)"-style labels (cached); falls back to the bare ticker.
  const tickerNames = useTickerNames(tickersWithDecisions);

  // Reset selected ticker when node changes
  useEffect(() => {
    setSelectedTicker(null);
  }, [nodeId]);

  // If no ticker is selected but we have decisions, select the first one
  useEffect(() => {
    if (tickersWithDecisions.length > 0 && (!selectedTicker || !tickersWithDecisions.includes(selectedTicker))) {
      setSelectedTicker(tickersWithDecisions[0]);
    }
  }, [tickersWithDecisions, selectedTicker]);

  // Get the selected decision text — prefer runtime (this session); else memory.
  const selectedDecision = selectedTicker
    ? allAnalysis[selectedTicker] ?? memFallback[selectedTicker]?.reasoning ?? null
    : null;
  // True when the analysis is coming from memory, not the current session's run.
  const selectedFromMemory =
    !!selectedTicker && !(selectedTicker in allAnalysis) && !!memFallback[selectedTicker];

  const copyToClipboard = () => {
    if (selectedDecision) {
      navigator.clipboard.writeText(selectedDecision)
        .then(() => {
          setCopySuccess(true);
          setTimeout(() => setCopySuccess(false), 2000);
        })
        .catch(err => {
          console.error('Failed to copy text: ', err);
        });
    }
  };

  return (
    <Dialog 
      open={isOpen} 
      onOpenChange={onOpenChange}
      defaultOpen={false}
      modal={true}
    >
      <DialogTrigger asChild>
        <div className="border-t border-border p-3 flex justify-end items-center cursor-pointer hover:bg-accent/50" onClick={() => onOpenChange(true)}>
          <div className="flex items-center gap-1">
            <div className="text-subtitle text-muted-foreground">Output</div>
            <AlignJustify className="h-3.5 w-3.5 text-muted-foreground" />
          </div>
        </div>
      </DialogTrigger>
      <DialogContent
        className="flex flex-col w-[95vw] max-w-[1500px] h-[90vh]"
        autoFocus={false}
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle className="text-xl">{name}</DialogTitle>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-6 pt-4 flex-1 min-h-0" ref={initialFocusRef} tabIndex={-1}>
          {/* Activity Log Section */}
          <div className="flex flex-col min-h-0">
            <h3 className="font-medium mb-3 text-primary">Log</h3>
            <div className="flex-1 min-h-0 overflow-y-auto border border-border rounded-lg p-3">
              {messages.length > 0 ? (
                <div className="p-3 space-y-3">
                  {messages
                    .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()) // Sort newest first for log
                    .map((msg, idx) => (
                    <div key={idx} className="border-l-2 border-primary pl-3 text-sm">
                      <div className="text-foreground break-words">
                        {msg.ticker && <span className="font-medium text-primary">[{formatTicker(msg.ticker, tickerNames)}] </span>}
                        {msg.message}
                      </div>
                      <div className="text-xs text-muted-foreground mt-0.5">
                        {formatTimeFromTimestamp(msg.timestamp)}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="flex items-center justify-center h-full text-muted-foreground">
                  No activity available
                </div>
              )}
            </div>
          </div>
          
          {/* Analysis Section */}
          <div className="flex flex-col min-h-0">
            <div className="flex justify-between items-center mb-3">
              <h3 className="font-medium text-primary">Analysis</h3>
              <div className="flex items-center gap-2">
                {/* Ticker selector */}
                {tickersWithDecisions.length > 0 && (
                  <div className="flex items-center gap-1">
                    <span className="text-xs text-muted-foreground font-medium">Ticker:</span>
                    <select 
                      className="text-xs p-1 rounded bg-background border border-border cursor-pointer"
                      value={selectedTicker || ''}
                      onChange={(e) => setSelectedTicker(e.target.value)}
                      autoFocus={false}
                    >
                      {tickersWithDecisions.map((ticker) => (
                        <option key={ticker} value={ticker}>
                          {formatTicker(ticker, tickerNames)}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
              </div>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto border border-border rounded-lg p-3">
              {tickersWithDecisions.length > 0 ? (
                <div className="p-3 rounded-lg text-[15px] leading-7">
                  {selectedTicker && (
                    <div className="mb-3 flex justify-between items-center">
                      <div className="text-muted-foreground font-medium flex items-center gap-2">
                        <span>Summary for {formatTicker(selectedTicker, tickerNames)}</span>
                        {selectedFromMemory && (
                          <span className="text-xs italic font-normal">
                            from memory · {memFallback[selectedTicker]?.date}
                            {memFallback[selectedTicker]?.signal && (
                              <> · {memFallback[selectedTicker].signal} {memFallback[selectedTicker].confidence}%</>
                            )}
                          </span>
                        )}
                      </div>
                      {selectedDecision && (
                        <button 
                          onClick={copyToClipboard}
                          className="flex items-center gap-1.5 text-xs p-1.5 rounded hover:bg-accent transition-colors text-muted-foreground"
                          title="Copy to clipboard"
                        >
                          <Copy className="h-3.5 w-3.5 " />
                          <span className="font-medium">{copySuccess ? 'Copied!' : 'Copy'}</span>
                        </button>
                      )}
                    </div>
                  )}
                  {selectedDecision ? (
                    <div className="text-foreground break-words">
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                        {selectedDecision}
                      </ReactMarkdown>
                    </div>
                  ) : nodeStatus === 'IN_PROGRESS' ? (
                    <div className="flex items-center justify-center h-full text-muted-foreground">
                      <Loader2 className="h-5 w-5 animate-spin mr-2" />
                      Analysis in progress...
                    </div>
                  ) : (
                    <div className="flex items-center justify-center h-full text-muted-foreground">
                      No analysis available for {formatTicker(selectedTicker || '', tickerNames)}
                    </div>
                  )}
                </div>
              ) : nodeStatus === 'IN_PROGRESS' ? (
                <div className="flex items-center justify-center h-full text-muted-foreground">
                  <Loader2 className="h-5 w-5 animate-spin mr-2" />
                  Analysis in progress...
                </div>
              ) : nodeStatus === 'COMPLETE' ? (
                <div className="flex items-center justify-center h-full text-muted-foreground">
                  Analysis completed with no results
                </div>
              ) : nodeStatus === 'ERROR' ? (
                <div className="flex items-center justify-center h-full text-muted-foreground">
                  Analysis failed
                </div>
              ) : (
                <div className="flex items-center justify-center h-full text-muted-foreground">
                  No analysis available
                </div>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
} 