import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { BarChart3, Loader2, RotateCcw } from 'lucide-react';
import { useEffect, useState } from 'react';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

interface UsageRow {
  provider: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
}
interface UsageData {
  models: UsageRow[];
  totals: { input_tokens: number; output_tokens: number; total_tokens: number; calls: number };
}

const EMPTY: UsageData = { models: [], totals: { input_tokens: 0, output_tokens: 0, total_tokens: 0, calls: 0 } };
const fmt = (n: number) => (n ?? 0).toLocaleString();

export function TokenUsage() {
  const [data, setData] = useState<UsageData>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [resetting, setResetting] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE_URL}/usage`);
      setData(res.ok ? await res.json() : EMPTY);
    } catch {
      setData(EMPTY);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const reset = async () => {
    setResetting(true);
    try {
      await fetch(`${API_BASE_URL}/usage/reset`, { method: 'POST' });
      await load();
    } catch {
      /* fail-open */
    } finally {
      setResetting(false);
    }
  };

  const rows = data.models;
  const totals = data.totals;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-primary">Token Usage</h2>
          <p className="text-sm text-muted-foreground">
            Cumulative LLM tokens this app has used, by model. Updates after each run, the PDF
            distiller, and the researchers.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <Button variant="outline" size="sm" onClick={load} disabled={loading}>
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : 'Refresh'}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={reset}
            disabled={resetting || totals.total_tokens === 0}
            title="Clear all recorded token usage"
          >
            <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
            {resetting ? 'Resetting…' : 'Reset'}
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <BarChart3 className="h-4 w-4" /> Totals
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <Stat label="Total tokens" value={fmt(totals.total_tokens)} />
            <Stat label="Input tokens" value={fmt(totals.input_tokens)} />
            <Stat label="Output tokens" value={fmt(totals.output_tokens)} />
            <Stat label="LLM calls" value={fmt(totals.calls)} />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">By model</CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No usage recorded yet. Run a flow (or upload a PDF) to see token usage here.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-muted-foreground border-b border-border">
                  <th className="py-2 font-medium">Model</th>
                  <th className="py-2 font-medium">Provider</th>
                  <th className="py-2 font-medium text-right">Input</th>
                  <th className="py-2 font-medium text-right">Output</th>
                  <th className="py-2 font-medium text-right">Total</th>
                  <th className="py-2 font-medium text-right">Calls</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.provider}::${r.model}`} className="border-b border-border/50">
                    <td className="py-2 text-primary font-medium">{r.model}</td>
                    <td className="py-2 text-muted-foreground">{r.provider}</td>
                    <td className="py-2 text-right tabular-nums">{fmt(r.input_tokens)}</td>
                    <td className="py-2 text-right tabular-nums">{fmt(r.output_tokens)}</td>
                    <td className="py-2 text-right tabular-nums text-primary">{fmt(r.total_tokens)}</td>
                    <td className="py-2 text-right tabular-nums">{fmt(r.calls)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-2xl font-semibold text-primary tabular-nums">{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
}
