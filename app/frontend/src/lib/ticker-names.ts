import { useEffect, useState } from 'react';

import { getTickerNames } from '@/services/tickers-api';

// Module-level, session-long cache so every component shares lookups.
const _cache: Record<string, string> = {};
const _inflight = new Set<string>();

/** Format a ticker as ``"Coherent (COHR)"`` when we know the name, else just ``"COHR"``. */
export function formatTicker(ticker: string, names?: Record<string, string>): string {
  if (!ticker) return '';
  const name = (names ?? _cache)[ticker];
  return name ? `${name} (${ticker})` : ticker;
}

/** Hook: ensure names for ``tickers`` are loaded; returns the (growing) name map.
 *  Re-renders the caller when new names land. */
export function useTickerNames(tickers: string[]): Record<string, string> {
  const [, force] = useState(0);
  const key = (tickers || []).filter(Boolean).join(',');

  useEffect(() => {
    const need = (tickers || [])
      .filter((t) => t && !(t in _cache) && !_inflight.has(t));
    if (need.length === 0) return;
    need.forEach((t) => _inflight.add(t));
    getTickerNames(need)
      .then((map) => {
        let changed = false;
        for (const t of need) {
          const n = map[t];
          if (n && _cache[t] !== n) { _cache[t] = n; changed = true; }
        }
        if (changed) force((x) => x + 1);
      })
      .finally(() => need.forEach((t) => _inflight.delete(t)));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return _cache;
}
