"use client";

/**
 * Small SVG bar chart used by:
 *   - the /stats workspace (long-form game-log trend)
 *   - the /trade pick-history strip (last-5 with threshold annotation)
 *
 * Two annotation hooks:
 *   - ``threshold``: when provided, a dashed reference line is drawn at that
 *     value instead of the mean. Use this for prop picks where the pick's
 *     line (e.g. 25+ points) is more meaningful than the average.
 *   - ``bandTone``: optional per-bar color callback. Default is "mid" tone
 *     for all bars (matches the historical /stats look). Pass
 *     ``(v) => v >= threshold ? "high" : "low"`` to color-code pass/fail.
 */

export type MiniBarsTone = "high" | "low" | "mid";

interface MiniBarsProps {
  points: number[];
  /** When set, dashed reference line at this y-value instead of the mean. */
  threshold?: number;
  /** Per-bar color callback. Defaults to constant "mid". */
  bandTone?: (value: number, index: number) => MiniBarsTone;
  ariaLabel?: string;
}

const TONE_FILL: Record<MiniBarsTone, string> = {
  high: "rgba(120,210,200,0.78)",
  low: "rgba(170,140,235,0.62)",
  mid: "rgba(170,140,235,0.62)",
};

export function MiniBars({
  points,
  threshold,
  bandTone,
  ariaLabel = "Trend chart",
}: MiniBarsProps) {
  if (points.length === 0) return null;

  const min = Math.min(...points);
  const max = Math.max(...points);
  const mean = points.reduce((sum, value) => sum + value, 0) / points.length;
  const referenceLine = threshold ?? mean;
  const lowerBound = Math.min(min, referenceLine);
  const upperBound = Math.max(max, referenceLine);
  const range = Math.max(1, upperBound - lowerBound);

  const W = 400;
  const H = 90;
  const PAD_X = 20;
  const BAR_Y_TOP = 14;
  const BAR_AREA = 70;
  const FILL = 0.85;
  const FLOOR = 0.12;
  const yFor = (value: number) => {
    const fraction = ((value - lowerBound) / range) * FILL + FLOOR;
    return BAR_Y_TOP + BAR_AREA - fraction * BAR_AREA;
  };

  const toneFor = bandTone ?? ((value: number) => (value >= mean ? "high" : "mid"));

  return (
    <div
      className="sa-chart-svg-wrap"
      style={{ width: "100%", maxWidth: 720, aspectRatio: `${W} / ${H}`, margin: "0 auto" }}
    >
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        width="100%"
        height="100%"
        role="img"
        aria-label={ariaLabel}
        style={{ display: "block" }}
      >
        <line
          x1={0}
          x2={W}
          y1={yFor(referenceLine)}
          y2={yFor(referenceLine)}
          stroke="rgba(150,140,255,0.45)"
          strokeWidth={1}
          strokeDasharray="4 4"
          data-testid="mini-bars-reference"
        />
        {points.map((value, index) => {
          const x = PAD_X + (index / Math.max(1, points.length - 1)) * (W - 2 * PAD_X);
          const y = yFor(value);
          const h = BAR_Y_TOP + BAR_AREA - y;
          const tone = toneFor(value, index);
          return (
            <g key={index}>
              <rect
                x={x - 10}
                y={y}
                width={20}
                height={h}
                rx={2}
                fill={TONE_FILL[tone]}
                data-testid={`mini-bars-bar-${index}`}
                data-tone={tone}
              />
              <text
                x={x}
                y={BAR_Y_TOP - 4}
                textAnchor="middle"
                fill="rgba(210,220,240,0.85)"
                fontSize="10"
                fontFamily="var(--font-geist-sans), system-ui, sans-serif"
              >
                {Number.isInteger(value) ? value : value.toFixed(1)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
