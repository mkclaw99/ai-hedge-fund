// Global "is the system busy" tracker for the bottom progress bar.
//
// Two sources feed the bar:
//   1. Backend HTTP — we patch window.fetch once and count in-flight requests to
//      the API base URL (covers PDF distillation, theme/flow/agent loads, …).
//   2. Non-HTTP work — callers use beginActivity/endActivity directly (e.g. a long
//      client-side step). Flow runs stream over a socket and are tracked separately
//      via flowConnectionManager, so they're not handled here.
//
// React components subscribe via useSyncExternalStore-style subscribe()/getVersion().

type Listener = () => void;

let pending = 0;
let version = 0;
let nextId = 1;
const labels: { id: number; label: string }[] = [];
const listeners = new Set<Listener>();

function emit() {
  version += 1;
  listeners.forEach((l) => l());
}

export function beginActivity(label = 'Working…'): number {
  const id = nextId++;
  pending += 1;
  labels.push({ id, label });
  emit();
  return id;
}

export function endActivity(id: number): void {
  pending = Math.max(0, pending - 1);
  const i = labels.findIndex((l) => l.id === id);
  if (i !== -1) labels.splice(i, 1);
  emit();
}

export function subscribeActivity(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** A monotonically increasing version — re-renders subscribers on any change. */
export function getActivityVersion(): number {
  return version;
}

export function activityPending(): number {
  return pending;
}

/** The most recently started activity's label (what to show on the bar). */
export function activityLabel(): string | null {
  return labels.length ? labels[labels.length - 1].label : null;
}

function labelForRequest(url: string, method: string): string {
  const m = method.toUpperCase();
  if (url.includes('/research/materials') && m === 'POST') return 'Analyzing PDF…';
  if (url.includes('/research/themes')) return 'Loading research themes…';
  if (url.includes('/research/')) return 'Researching…';
  if (url.includes('/hedge-fund/run')) return 'Running…';
  if (url.includes('/language-models') || url.includes('/hedge-fund/agents')) return 'Loading…';
  if (url.includes('/flows')) return m === 'GET' ? 'Loading flow…' : 'Saving…';
  if (url.includes('/memory/track-record')) return 'Loading track record…';
  if (url.includes('/memory')) return 'Loading flow memory…';
  return m === 'GET' ? 'Loading…' : 'Working…';
}

let patched = false;

/** Patch window.fetch once so every request to `apiBase` shows on the progress bar. */
export function installFetchTracking(apiBase: string): void {
  if (patched || typeof window === 'undefined' || typeof window.fetch !== 'function') return;
  patched = true;
  const original = window.fetch.bind(window);

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    let url = '';
    try {
      url = typeof input === 'string' ? input : input instanceof URL ? input.href : (input as Request).url;
    } catch {
      url = '';
    }
    const track = !!url && url.startsWith(apiBase);
    let id = -1;
    if (track) {
      const method = init?.method || (input instanceof Request ? input.method : 'GET');
      id = beginActivity(labelForRequest(url, method || 'GET'));
    }
    try {
      return await original(input as any, init);
    } finally {
      if (track) endActivity(id);
    }
  };
}

// Install immediately for the app's API base (same default the services use).
installFetchTracking(import.meta.env.VITE_API_URL || 'http://localhost:8001');
