import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { FlowMemory, MemoryAnalystRow } from '@/services/memory-api';

interface MemoryDialogProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  memory: FlowMemory | null;
  loading: boolean;
}

const signalColor = (signal: string) => {
  switch (signal) {
    case 'bullish':
      return 'text-green-500';
    case 'bearish':
      return 'text-red-500';
    default:
      return 'text-yellow-500';
  }
};

function SignalTable({ rows, label }: { rows: MemoryAnalystRow[]; label: string }) {
  if (!rows.length) return null;
  return (
    <div className="mt-3">
      <div className="text-subtitle text-muted-foreground mb-1">{label}</div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Analyst</TableHead>
            <TableHead>Signal</TableHead>
            <TableHead className="text-right">Conf</TableHead>
            <TableHead>Date</TableHead>
            <TableHead>Reasoning</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r, i) => (
            <TableRow key={`${r.analyst}-${i}`}>
              <TableCell className="font-medium">{r.analyst}</TableCell>
              <TableCell className={signalColor(r.signal)}>{r.signal}</TableCell>
              <TableCell className="text-right">{r.confidence}%</TableCell>
              <TableCell className="text-muted-foreground">{r.date}</TableCell>
              <TableCell className="text-muted-foreground max-w-md truncate" title={r.reasoning}>
                {r.reasoning}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function MemoryDialog({ isOpen, onOpenChange, memory, loading }: MemoryDialogProps) {
  const tickers = memory?.tickers ?? [];

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Flow Research Memory</DialogTitle>
          <DialogDescription>
            What this flow has learned across runs. Each analyst reads back only its own
            prior calls; the Portfolio Manager reads everything here.
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="py-8 text-center text-muted-foreground">Loading…</div>
        ) : tickers.length === 0 ? (
          <div className="py-8 text-center text-muted-foreground">
            No memory yet — run this flow and insights will accumulate here.
          </div>
        ) : (
          tickers.map((t) => (
            <div key={t.ticker} className="border border-border rounded-lg p-4 mb-3">
              <div className="flex items-center gap-3">
                <span className="text-title font-semibold text-primary">{t.ticker}</span>
                <Badge variant="outline" className={signalColor(t.consensus)}>
                  consensus {t.consensus}
                </Badge>
                <span className="text-subtitle text-muted-foreground">
                  {t.bullish.length} bull / {t.bearish.length} bear / {t.neutral.length} neutral
                  {' · '}
                  {t.n_runs} run(s) · {t.n_insights} insight(s)
                </span>
              </div>
              <SignalTable rows={t.analysts} label="Analyst signals" />
              <SignalTable rows={t.pm_decisions} label="Portfolio Manager decisions" />
            </div>
          ))
        )}
      </DialogContent>
    </Dialog>
  );
}
