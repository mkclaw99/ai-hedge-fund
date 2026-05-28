// Resolve stock tickers to company names so the UI can render "Coherent (COHR)".
const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

/** Fetch company names for the given tickers. Returns `{ticker: name}`; tickers
 *  the backend couldn't resolve are simply omitted (caller falls back to the ticker). */
export async function getTickerNames(tickers: string[]): Promise<Record<string, string>> {
  const clean = (tickers ?? []).map((t) => String(t).trim()).filter(Boolean);
  if (clean.length === 0) return {};
  try {
    const q = clean.map(encodeURIComponent).join(',');
    const res = await fetch(`${API_BASE_URL}/tickers/names?tickers=${q}`);
    if (!res.ok) return {};
    const data = await res.json();
    return (data?.names ?? {}) as Record<string, string>;
  } catch {
    return {};
  }
}
