"use client";

import type { PredictionInterval } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Re-export the type so existing imports from this module keep
 * working. The canonical definition lives in ``lib/types.ts`` as a
 * generated-schema shim (``Wire<Schema<"PredictionIntervalRead">>``)
 * so it stays in sync with the API's ``PredictionIntervalRead``
 * Pydantic schema automatically.
 */
export type { PredictionInterval };

interface PredictionIntervalBandProps {
  interval: PredictionInterval | null | undefined;
}

/**
 * Horizontal SVG band visualizing the (p10, p50, p90) interval with a
 * tick at the market threshold. The whole [p10, p90] range is drawn
 * as a colored rectangle, with the threshold marked by a vertical
 * dashed line. Operators get a one-glance read on how confident the
 * model is the stat will clear the line.
 *
 * Color tone is driven by which side of p50 the threshold falls on:
 * threshold below p50 → over-leaning (positive); threshold above p50
 * → under-leaning (negative). This matches the existing edge / win-
 * prob coloring on the trade ticket.
 *
 * When ``coverage_status !== "ok"`` the swap is NOT happening upstream
 * — the band is informational. A small badge surfaces the band's
 * status so the operator knows the diagnostic isn't load-bearing for
 * the recommendation.
 */
export function PredictionIntervalBand({ interval }: PredictionIntervalBandProps) {
  if (interval == null) return null;

  const { p10, p50, p90, threshold, coverage_status } = interval;
  const lean: "over" | "under" = threshold < p50 ? "over" : "under";

  // SVG viewBox — same aspect ratio as MiniBars so the visual rhythm
  // matches the rest of the trade ticket.
  const W = 400;
  const H = 28;
  const padX = 12;
  const padY = 6;
  const usableW = W - padX * 2;

  // Map a stat value to an x-coordinate in the viewBox. We extend the
  // visible range slightly past [p10, p90] when the threshold lies
  // outside that band so the threshold tick is always visible.
  const xMin = Math.min(p10, threshold);
  const xMax = Math.max(p90, threshold);
  const xSpan = Math.max(xMax - xMin, 1e-6);
  const xFor = (value: number) =>
    padX + ((value - xMin) / xSpan) * usableW;

  const x10 = xFor(p10);
  const x50 = xFor(p50);
  const x90 = xFor(p90);
  const xThreshold = xFor(threshold);

  const isOk = coverage_status === "ok";
  // Tailwind color tokens come from globals.css. Both lean variants
  // use the same shape; only the fill / stroke change.
  const bandFill = lean === "over" ? "fill-positive/15" : "fill-negative/15";
  const bandStroke = lean === "over" ? "stroke-positive/40" : "stroke-negative/40";
  const p50Stroke = lean === "over" ? "stroke-positive" : "stroke-negative";

  const ariaLabel = `Prediction interval ${p10} to ${p90} with median ${p50} versus threshold ${threshold} (${lean})`;

  return (
    <div
      role="group"
      aria-label={ariaLabel}
      data-lean={lean}
      data-coverage={coverage_status}
      className="space-y-1.5"
    >
      <div className="flex items-center justify-between text-xs">
        <span className="font-medium text-muted-foreground uppercase tracking-wider">
          Interval
        </span>
        <span
          className={cn(
            "rounded border px-1.5 py-px text-[10px] font-medium uppercase tracking-wider",
            isOk
              ? "border-positive/30 bg-positive/10 text-positive"
              : "border-muted-foreground/30 bg-surface-hover text-muted-foreground",
          )}
        >
          {coverage_status}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full"
        preserveAspectRatio="none"
        aria-hidden
      >
        {/* Axis line — subtle baseline so the band has visual weight. */}
        <line
          x1={padX}
          x2={W - padX}
          y1={H / 2}
          y2={H / 2}
          className="stroke-border"
          strokeWidth={1}
        />
        {/* The (p10, p90) range as a filled rectangle. */}
        <rect
          x={x10}
          y={padY}
          width={Math.max(x90 - x10, 1)}
          height={H - padY * 2}
          rx={3}
          className={cn(bandFill, bandStroke)}
          strokeWidth={1}
        />
        {/* p50 tick — the central estimate. */}
        <line
          x1={x50}
          x2={x50}
          y1={padY - 1}
          y2={H - padY + 1}
          className={p50Stroke}
          strokeWidth={2}
        />
        {/* Threshold tick — dashed so it's visually distinct from p50. */}
        <line
          x1={xThreshold}
          x2={xThreshold}
          y1={padY - 2}
          y2={H - padY + 2}
          className="stroke-foreground"
          strokeWidth={1}
          strokeDasharray="3 2"
        />
      </svg>
      <div className="flex items-center justify-between text-[10px] font-mono text-muted-foreground">
        <span>p10 {p10}</span>
        <span className={cn("text-foreground", lean === "over" ? "text-positive" : "text-negative")}>
          p50 {p50}
        </span>
        <span>p90 {p90}</span>
      </div>
      <p className="text-[10px] text-muted-foreground">
        Threshold {threshold} · {lean === "over" ? "above" : "below"} median
      </p>
    </div>
  );
}
