import { type NodeProps } from '@xyflow/react';
import { Loader2, RotateCw, Wallet } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { CardContent } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useNodeState } from '@/hooks/use-node-state';
import { cn } from '@/lib/utils';
import { getPaperAccount, PaperAccount } from '@/services/trading-api';
import { type TradingAccountNode as TradingAccountNodeType } from '../types';
import { NodeShell } from './node-shell';

const fmt = (n?: number) =>
  typeof n === 'number' ? n.toLocaleString(undefined, { style: 'currency', currency: 'USD' }) : '—';

export function TradingAccountNode({ data, selected, id, isConnectable }: NodeProps<TradingAccountNodeType>) {
  // User-set target / baseline budget. Alpaca paper has its own actual balance,
  // shown alongside this; we don't try to push this into Alpaca (paper accounts
  // can only be reset from the Alpaca dashboard).
  const [startingBudget, setStartingBudget] = useNodeState(id, 'startingBudget', 100000);
  // Opt-in: submit the PM's decisions as Alpaca PAPER orders. Default OFF.
  const [autoTrade, setAutoTrade] = useNodeState<boolean>(id, 'autoTrade', false);

  const [account, setAccount] = useState<PaperAccount | null>(null);
  const [loading, setLoading] = useState(true);
  const [toggleError, setToggleError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setAccount(await getPaperAccount());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const connected = !!account?.connected;
  const canEnable = connected; // can only turn Auto-trade ON when paper creds work

  // Defensive: if Auto-trade is ON and we later discover the paper account is
  // disconnected (key revoked, network blip, …), flip it OFF and surface why.
  useEffect(() => {
    if (autoTrade && account && account.connected === false) {
      setAutoTrade(false);
      setToggleError(account.reason || 'Alpaca Paper not connected — Auto-trade turned off.');
    }
  }, [autoTrade, account, setAutoTrade]);

  const handleToggleClick = useCallback(async () => {
    if (autoTrade) {
      setAutoTrade(false);
      setToggleError(null);
      return;
    }
    // Re-verify connection BEFORE turning ON — never trust a stale status.
    setLoading(true);
    let fresh: PaperAccount;
    try {
      fresh = await getPaperAccount();
      setAccount(fresh);
    } finally {
      setLoading(false);
    }
    if (!fresh.connected) {
      setToggleError(
        fresh.reason ||
          'Configure ALPACA_PAPER_API_KEY_ID and ALPACA_PAPER_SECRET_KEY in Settings → API Keys.',
      );
      return;
    }
    setToggleError(null);
    setAutoTrade(true);
  }, [autoTrade, setAutoTrade]);

  return (
    <TooltipProvider>
      <NodeShell
        id={id}
        selected={selected}
        isConnectable={isConnectable}
        icon={<Wallet className="h-5 w-5" />}
        iconColor="text-emerald-500"
        // System node — show the canonical name/description so old saves update.
        name="Trading Account"
        description="Your trading account (paper-trading only for now). Connect it downstream of the Portfolio Manager to have its decisions placed as orders; otherwise it's just a status display."
        hasLeftHandle={true}
        hasRightHandle={false}
        width="w-80"
      >
        <CardContent className="p-0">
          <div className="border-t border-border p-3 space-y-4">
            {/* PAPER badge — explicit so it's never confused with a live account */}
            <div className="flex items-center justify-between">
              <span className="inline-flex items-center gap-1.5 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-2 py-0.5 text-subtitle font-semibold text-yellow-500">
                PAPER
              </span>
              <Tooltip delayDuration={200}>
                <TooltipTrigger asChild>
                  <span tabIndex={0} className="inline-flex">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={refresh}
                      disabled={loading}
                      className="nodrag h-7 gap-1.5 text-xs"
                    >
                      {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5" />}
                      {loading ? 'Loading…' : 'Refresh'}
                    </Button>
                  </span>
                </TooltipTrigger>
                <TooltipContent side="bottom" className="max-w-xs">
                  Re-fetch the latest account state from Alpaca Paper.
                </TooltipContent>
              </Tooltip>
            </div>

            {/* Starting budget — user-configurable target */}
            <div className="flex flex-col gap-2">
              <div className="text-subtitle text-primary flex items-center gap-1">
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild><span>Starting budget</span></TooltipTrigger>
                  <TooltipContent side="right" className="max-w-xs">
                    Your intended starting capital for this account. Used as the system's baseline
                    for sizing decisions. Alpaca's actual paper balance is shown below (resettable
                    only from the Alpaca dashboard).
                  </TooltipContent>
                </Tooltip>
              </div>
              <input
                type="number"
                min={0}
                step={1000}
                value={Number(startingBudget ?? 0)}
                onChange={(e) => setStartingBudget(parseFloat(e.target.value) || 0)}
                className="nodrag h-9 w-full rounded-md border border-border bg-node px-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              />
            </div>

            {/* Auto-trade toggle (opt-in, gated on paper credentials being valid) */}
            <div className="flex flex-col gap-2">
              <div className="text-subtitle text-primary flex items-center gap-1">
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild><span>Auto-trade</span></TooltipTrigger>
                  <TooltipContent side="right" className="max-w-xs">
                    When ON, the Portfolio Manager's per-ticker decisions are submitted as MARKET
                    DAY orders on your Alpaca PAPER account after each run. Paper-only — no path
                    to a live account. Can only be turned ON when Alpaca Paper credentials are
                    configured and verified; flips back to OFF on a connection error.
                  </TooltipContent>
                </Tooltip>
              </div>
              <div className="flex items-center gap-3">
                <Tooltip delayDuration={200}>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={!!autoTrade}
                      onClick={handleToggleClick}
                      disabled={loading || (!autoTrade && !canEnable)}
                      className={cn(
                        'nodrag relative inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 focus:ring-offset-background',
                        autoTrade ? 'bg-emerald-500' : 'bg-muted',
                        loading || (!autoTrade && !canEnable)
                          ? 'opacity-50 cursor-not-allowed'
                          : 'cursor-pointer',
                      )}
                    >
                      <span
                        className={cn(
                          'inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform',
                          autoTrade ? 'translate-x-[22px]' : 'translate-x-0.5',
                        )}
                      />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom" className="max-w-xs">
                    {autoTrade
                      ? 'Click to turn Auto-trade OFF'
                      : canEnable
                        ? 'Click to turn Auto-trade ON (re-verifies credentials first)'
                        : 'Configure Alpaca Paper credentials in Settings → API Keys to enable.'}
                  </TooltipContent>
                </Tooltip>
                <span
                  className={cn(
                    'text-sm',
                    autoTrade ? 'text-emerald-500 font-medium' : 'text-muted-foreground',
                  )}
                >
                  {autoTrade
                    ? 'ON — PM decisions will be placed on Alpaca PAPER'
                    : canEnable
                      ? 'OFF'
                      : 'OFF — needs paper credentials'}
                </span>
              </div>
              {toggleError && (
                <span className="text-xs text-red-500 break-words">{toggleError}</span>
              )}
            </div>

            {/* Live Alpaca paper state */}
            <div className="flex flex-col gap-2">
              <div className="text-subtitle text-primary">Alpaca Paper account</div>
              {!connected ? (
                <div className="rounded-md border border-border bg-node/60 p-2 text-xs text-muted-foreground">
                  {loading
                    ? 'Loading…'
                    : account?.reason || 'Not connected. Set ALPACA_PAPER_API_KEY_ID and ALPACA_PAPER_SECRET_KEY in Settings → API Keys.'}
                </div>
              ) : (
                <div className="rounded-md border border-border bg-node/60 p-3 text-sm space-y-1.5">
                  <Row label="Status" value={(account?.status || 'unknown').toLowerCase()} />
                  <Row label="Cash" value={fmt(account?.cash)} />
                  <Row label="Equity" value={fmt(account?.equity)} />
                  <Row label="Buying power" value={fmt(account?.buying_power)} />
                  {account?.account_number && (
                    <Row label="Account #" value={account.account_number} muted />
                  )}
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </NodeShell>
    </TooltipProvider>
  );
}

function Row({ label, value, muted = false }: { label: string; value: string; muted?: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={`tabular-nums ${muted ? 'text-xs text-muted-foreground' : 'text-primary font-medium'}`}>{value}</span>
    </div>
  );
}
