// Inline SVG equity-curve chart for the Trading Account Details dialog.
//
// Deliberately no chart library — for one line + a fill area + a baseline,
// pulling in recharts/Chart.js would be overkill and add ~150KB to the
// bundle. The visual is the same shape the user sees in any broker app:
// a single equity line, a translucent fill under it, a dashed line for
// the starting equity, and an axis-free design with min/max labels at
// the corners. A hover marker shows the equity + return at the cursor.

import { PortfolioHistorySample } from '@/services/trading-api';
import { useMemo, useRef, useState } from 'react';

interface Props {
  samples: PortfolioHistorySample[];
  baseValue?: number;
  height?: number;
  // Colour cue from the parent — green when up, red when down. Defaults
  // to comparing first and last sample.
  tone?: 'pos' | 'neg' | 'auto';
}

const usd = (n: number) => n.toLocaleString(undefined, { style: 'currency', currency: 'USD' });
const pct = (n: number) => `${n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}%`;

export function EquityChart({ samples, baseValue, height = 220, tone = 'auto' }: Props) {
  // Empty / single-point dataset → nothing meaningful to chart.
  if (!samples || samples.length < 2) {
    return (
      <div
        className="flex items-center justify-center rounded-md border border-border bg-node/40 text-sm text-muted-foreground"
        style={{ height }}
      >
        Not enough history yet — Alpaca records a sample per timeframe (hourly/daily).
        Place trades and come back.
      </div>
    );
  }

  // Geometry — explicit pixel coords for the SVG.
  const width = 720; // viewBox width; the SVG scales to its container
  const padL = 8;
  const padR = 8;
  const padT = 12;
  const padB = 22;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;

  const equities = samples.map((s) => s.equity);
  const minEq = Math.min(...equities);
  const maxEq = Math.max(...equities);
  // Y-domain padded by 1% on each side so the line never touches the edges.
  const yPad = (maxEq - minEq) * 0.05 || 1;
  const yMin = Math.min(minEq, baseValue ?? minEq) - yPad;
  const yMax = Math.max(maxEq, baseValue ?? maxEq) + yPad;
  const yRange = yMax - yMin || 1;

  const xAt = (i: number) => padL + (i / (samples.length - 1)) * innerW;
  const yAt = (eq: number) => padT + (1 - (eq - yMin) / yRange) * innerH;

  const linePath = samples.map((s, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i).toFixed(2)} ${yAt(s.equity).toFixed(2)}`).join(' ');
  const fillPath = `${linePath} L ${xAt(samples.length - 1).toFixed(2)} ${padT + innerH} L ${padL} ${padT + innerH} Z`;

  // Tone: green if ending equity ≥ base/start, red otherwise.
  const last = samples[samples.length - 1].equity;
  const first = samples[0].equity;
  const ref = baseValue ?? first;
  const computedTone: 'pos' | 'neg' = last >= ref ? 'pos' : 'neg';
  const t = tone === 'auto' ? computedTone : tone;
  const stroke = t === 'pos' ? '#10b981' : '#ef4444'; // tailwind emerald-500 / red-500
  const fill = t === 'pos' ? 'rgba(16, 185, 129, 0.10)' : 'rgba(239, 68, 68, 0.10)';

  // Hover state — index of the sample nearest the cursor.
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  // Map a mouse pageX to the nearest sample index via viewBox coordinates.
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    const r = svg.getBoundingClientRect();
    const xPx = e.clientX - r.left;
    // Convert pixel x to viewBox x.
    const vx = (xPx / r.width) * width;
    const xWithin = Math.max(0, Math.min(innerW, vx - padL));
    const idx = Math.round((xWithin / innerW) * (samples.length - 1));
    setHoverIdx(Math.max(0, Math.min(samples.length - 1, idx)));
  };
  const onLeave = () => setHoverIdx(null);

  // Reference line at base/starting equity so the user can see daylight
  // between "in profit" and "in loss" at a glance.
  const refY = yAt(ref);

  const hover = hoverIdx != null ? samples[hoverIdx] : null;

  return (
    <div className="relative">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="w-full block"
        style={{ height }}
        onMouseMove={onMove}
        onMouseLeave={onLeave}
      >
        {/* Fill under the line */}
        <path d={fillPath} fill={fill} />
        {/* Baseline (dashed) */}
        <line
          x1={padL} x2={padL + innerW} y1={refY} y2={refY}
          stroke="currentColor" strokeOpacity="0.25" strokeDasharray="3 3"
        />
        {/* Equity line */}
        <path d={linePath} stroke={stroke} strokeWidth="1.5" fill="none" />
        {/* Hover marker */}
        {hover && (
          <>
            <line
              x1={xAt(hoverIdx!)} x2={xAt(hoverIdx!)} y1={padT} y2={padT + innerH}
              stroke="currentColor" strokeOpacity="0.25"
            />
            <circle cx={xAt(hoverIdx!)} cy={yAt(hover.equity)} r="3" fill={stroke} />
          </>
        )}
      </svg>

      {/* Corner labels — min/max equity + the date span */}
      <div className="pointer-events-none absolute inset-0 text-[10px] text-muted-foreground tabular-nums px-2">
        <div className="absolute top-1 right-2">{usd(maxEq)}</div>
        <div className="absolute bottom-7 right-2">{usd(minEq)}</div>
        <div className="absolute bottom-1 left-2">{new Date(samples[0].ts * 1000).toLocaleDateString()}</div>
        <div className="absolute bottom-1 right-2">
          {new Date(samples[samples.length - 1].ts * 1000).toLocaleDateString()}
        </div>
      </div>

      {/* Hover readout */}
      {hover && (
        <div className="absolute top-1 left-2 rounded border border-border bg-node/95 px-2 py-1 text-xs shadow-sm tabular-nums">
          <div className="text-primary font-medium">{usd(hover.equity)}</div>
          <div className={hover.profit_loss >= 0 ? 'text-emerald-500' : 'text-red-500'}>
            {hover.profit_loss >= 0 ? '+' : ''}{usd(hover.profit_loss)} ({pct(hover.profit_loss_pct)})
          </div>
          <div className="text-muted-foreground text-[10px]">
            {new Date(hover.ts * 1000).toLocaleString()}
          </div>
        </div>
      )}
    </div>
  );
}
