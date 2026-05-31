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

// Color a memo body section's `##` heading by what the section is for — readers can
// tell at a glance which paragraph is the bull case vs the bear case vs the risks.
function h2AccentClass(text: string): string {
  const t = text.toLowerCase();
  if (t.includes('bull')) return 'border-l-4 border-emerald-500 pl-3';
  if (t.includes('bear')) return 'border-l-4 border-red-500 pl-3';
  if (t.includes('risk') || t.includes('change my mind') || t.includes('what would')) return 'border-l-4 border-amber-500 pl-3';
  if (t.includes('thesis') || t.includes('summary')) return 'border-l-4 border-blue-500 pl-3';
  if (t.includes('verdict') || t.includes('recommendation') || t.includes('conclusion')) return 'border-l-4 border-primary pl-3';
  if (t.includes('evidence') || t.includes('findings') || t.includes('data')) return 'border-l-4 border-purple-500 pl-3';
  return 'border-b border-border pb-1';
}

// Extract the visible text from a markdown heading's React children (used to pick a color).
function childrenText(children: any): string {
  if (typeof children === 'string') return children;
  if (Array.isArray(children)) return children.map(childrenText).join('');
  if (children && typeof children === 'object' && 'props' in children) return childrenText((children as any).props?.children);
  return '';
}

// Styled element map so analyst reports (Markdown) render cleanly in the dialog.
const MD_COMPONENTS = {
  h1: (p: any) => <h1 className="text-lg font-semibold text-primary mt-4 mb-2 first:mt-0" {...p} />,
  h2: ({ children, ...rest }: any) => (
    <h2 className={`text-base font-semibold text-primary mt-5 mb-2 first:mt-0 ${h2AccentClass(childrenText(children))}`} {...rest}>
      {children}
    </h2>
  ),
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

// Map an analyst signal to a colored chip — bullish/bearish/neutral get the obvious
// finance colors, anything unknown stays muted.
function signalChipClass(sig?: string): string {
  const s = (sig || '').toLowerCase();
  if (s === 'bullish') return 'bg-emerald-500/15 text-emerald-500 border-emerald-500/40';
  if (s === 'bearish') return 'bg-red-500/15 text-red-500 border-red-500/40';
  if (s === 'neutral') return 'bg-amber-500/15 text-amber-500 border-amber-500/40';
  return 'bg-muted text-muted-foreground border-border';
}

// HTML-safe anchor id so the universe strip can jump to a section even for tickers
// that contain dots ("MOG.A") or other URL-unfriendly chars.
function tickerAnchor(t: string): string {
  return `memo-${t.replace(/\W/g, '_')}`;
}

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

  // Union: tickers visible in the dialog = runtime ∪ memory fallback. The committee
  // memo renders every company in order — no dropdown, the whole report scrolls.
  const tickersWithDecisions = Array.from(
    new Set([...runtimeTickers, ...Object.keys(memFallback)]),
  );
  // Resolve "Coherent Corp (COHR)"-style labels (cached); falls back to the bare ticker.
  const tickerNames = useTickerNames(tickersWithDecisions);

  // Per-ticker text + source (runtime takes precedence over memory).
  type TickerReport = {
    ticker: string;
    text: string;
    fromMemory: boolean;
    memDate?: string;
    memSignal?: string;
    memConfidence?: number;
  };
  // Strip the ForecasterNode's inline-chart fence — it travels through the
  // analysis Markdown so we can chart it on the node, but the human-readable
  // report shouldn't end with a raw JSON blob.
  const stripForecastFence = (md: string) => md.replace(/\n?```forecast-data\s*\n[\s\S]*?\n```\s*$/m, '').trimEnd();
  const reports: TickerReport[] = tickersWithDecisions
    .map((t): TickerReport | null => {
      const runtime = allAnalysis[t];
      if (runtime) return { ticker: t, text: stripForecastFence(runtime), fromMemory: false };
      const mem = memFallback[t];
      if (mem?.reasoning) {
        return {
          ticker: t,
          text: mem.reasoning,
          fromMemory: true,
          memDate: mem.date,
          memSignal: mem.signal,
          memConfidence: mem.confidence,
        };
      }
      return null;
    })
    .filter((r): r is TickerReport => r !== null);

  const copyToClipboard = () => {
    if (reports.length === 0) return;
    const combined = reports
      .map((r) => `# ${formatTicker(r.ticker, tickerNames)}\n\n${r.text}`)
      .join('\n\n---\n\n');
    navigator.clipboard.writeText(combined)
      .then(() => {
        setCopySuccess(true);
        setTimeout(() => setCopySuccess(false), 2000);
      })
      .catch((err) => console.error('Failed to copy text: ', err));
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
          
          {/* Committee memo: every company in one scrolling report. */}
          <div className="flex flex-col min-h-0">
            <div className="flex justify-between items-center mb-3">
              <h3 className="font-medium text-primary">
                Memo to the Investment Committee
                {reports.length > 0 && (
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {reports.length} {reports.length === 1 ? 'company' : 'companies'}
                  </span>
                )}
              </h3>
              {reports.length > 0 && (
                <button
                  onClick={copyToClipboard}
                  className="flex items-center gap-1.5 text-xs p-1.5 rounded hover:bg-accent transition-colors text-muted-foreground"
                  title="Copy the whole memo to the clipboard"
                >
                  <Copy className="h-3.5 w-3.5" />
                  <span className="font-medium">{copySuccess ? 'Copied!' : 'Copy all'}</span>
                </button>
              )}
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto border border-border rounded-lg">
              {reports.length > 0 ? (
                <div className="text-[15px] leading-7">
                  {/* Sticky universe strip: signal-colored pills, click to jump to a
                      company. Doubles as an at-a-glance summary of the verdict per
                      company without scrolling. */}
                  {reports.length > 1 && (
                    <div className="sticky top-0 z-10 bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80 border-b border-border px-4 py-2 flex flex-wrap gap-1.5">
                      {reports.map((r) => {
                        const label = formatTicker(r.ticker, tickerNames);
                        return (
                          <a
                            key={r.ticker}
                            href={`#${tickerAnchor(r.ticker)}`}
                            onClick={(e) => {
                              e.preventDefault();
                              document
                                .getElementById(tickerAnchor(r.ticker))
                                ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                            }}
                            className={`text-xs px-2 py-1 rounded border font-medium hover:opacity-80 cursor-pointer ${signalChipClass(r.memSignal)}`}
                            title={`${label}${r.memSignal ? ` — ${r.memSignal} ${r.memConfidence}%` : ''}`}
                          >
                            <span>{r.ticker}</span>
                            {r.memSignal && (
                              <span className="ml-1 uppercase tracking-wide">
                                {r.memSignal.charAt(0)} {r.memConfidence}%
                              </span>
                            )}
                          </a>
                        );
                      })}
                    </div>
                  )}
                  <div className="p-5 space-y-10">
                    {reports.map((r, i) => (
                      <section key={r.ticker} id={tickerAnchor(r.ticker)} className="scroll-mt-16">
                        {i > 0 && <hr className="mb-8 border-border" />}
                        <div className="mb-3 flex items-baseline justify-between gap-3 flex-wrap">
                          <h2 className="text-xl font-semibold text-primary">
                            {formatTicker(r.ticker, tickerNames)}
                          </h2>
                          <div className="flex items-center gap-2">
                            {r.memSignal && (
                              <span
                                className={`text-xs uppercase font-semibold tracking-wide px-2.5 py-1 rounded-full border ${signalChipClass(r.memSignal)}`}
                              >
                                {r.memSignal} {r.memConfidence}%
                              </span>
                            )}
                            {r.fromMemory && (
                              <span className="text-xs italic text-muted-foreground">
                                from memory · {r.memDate}
                              </span>
                            )}
                          </div>
                        </div>
                        <div className="text-foreground break-words">
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                            {r.text}
                          </ReactMarkdown>
                        </div>
                      </section>
                    ))}
                  </div>
                </div>
              ) : nodeStatus === 'IN_PROGRESS' ? (
                <div className="flex items-center justify-center h-full text-muted-foreground p-6">
                  <Loader2 className="h-5 w-5 animate-spin mr-2" />
                  Analysis in progress...
                </div>
              ) : nodeStatus === 'COMPLETE' ? (
                <div className="flex items-center justify-center h-full text-muted-foreground p-6">
                  Analysis completed with no results
                </div>
              ) : nodeStatus === 'ERROR' ? (
                <div className="flex items-center justify-center h-full text-muted-foreground p-6">
                  Analysis failed
                </div>
              ) : (
                <div className="flex items-center justify-center h-full text-muted-foreground p-6">
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