import { useEffect, useState } from 'react';

import { flowConnectionManager } from '@/hooks/use-flow-connection';
import { activityLabel, activityPending, subscribeActivity } from '@/lib/activity';

/**
 * A thin indeterminate progress bar pinned to the bottom of the window, shown
 * whenever the system is busy — any in-flight backend request (PDF analysis,
 * loading, saving, …) or a flow run in progress. A small label says what's
 * happening. Purely presentational; it observes global activity, never blocks.
 */
export function BottomProgressBar() {
  const [, force] = useState({});

  useEffect(() => {
    const onChange = () => force({});
    const unsub = subscribeActivity(onChange);
    flowConnectionManager.addListener(onChange);
    return () => {
      unsub();
      flowConnectionManager.removeListener(onChange);
    };
  }, []);

  const running = flowConnectionManager.hasActiveConnections();
  const busy = activityPending() > 0 || running;
  const label = activityLabel() || (running ? 'Running…' : null);

  return (
    <>
      {/* The bar itself: full-width track at the very bottom edge. */}
      <div
        className="pointer-events-none fixed bottom-0 left-0 right-0 z-50 h-1 overflow-hidden transition-opacity duration-200"
        style={{ opacity: busy ? 1 : 0 }}
        aria-hidden={!busy}
      >
        <div className="h-full w-full bg-primary/15">
          <div className="h-full w-2/5 animate-indeterminate bg-primary" />
        </div>
      </div>

      {/* A small status pill so it's self-explanatory what we're waiting on. */}
      {busy && label && (
        <div
          className="pointer-events-none fixed bottom-2 left-2 z-50 flex items-center gap-2 rounded-md border border-border bg-node/95 px-2.5 py-1 text-subtitle text-primary shadow-md"
          role="status"
          aria-live="polite"
        >
          <span className="h-2 w-2 flex-shrink-0 animate-pulse rounded-full bg-primary" />
          {label}
        </div>
      )}
    </>
  );
}
