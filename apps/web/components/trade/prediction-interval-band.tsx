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
 * SVG viewBox dimensions. Matched to the MiniBars aspect ratio used
 * elsewhere in the trade ticket so the diagnostic strip visually
 * rhymes with the rest of the ticket's data graphics.
 */
const VIEWBOX_W = 400;
const VIEWBOX_H = 24;
const PAD_X = 12;
const PAD_Y = 5;
const USABLE_W = VIEWBOX_W - PAD_X * 2;

/**
 * Smarter #21 phase 2d (redesign 2026-05-17) — horizontal SVG band
 * visualizing the (p10, p50, p90) prediction interval against the
 * market threshold. The full [p10, p90] range is rendered as a tonal
 * band; p50 is a heavy centered tick (the model's central estimate);
 * threshold is a dashed tick (the line to clear).
 *
 * Color tone follows the lean: threshold < p50 ⇒ over (positive),
 * threshold ≥ p50 ⇒ under (negative). Matches the existing edge /
 * win-prob coloring on the trade ticket.
 *
 * The coverage chip in the eyebrow tells the operator whether this
 * band's swap is load-bearing for the recommendation (``ok``) or
 * informational only (``bad`` / ``insufficient``).
 */
export function PredictionIntervalBand({
  interval,
}: PredictionIntervalBandProps) {
  if (interval == null) return null;

  const { p10, p50, p90, threshold, coverage_status } = interval;
  const lean: "over" | "under" = threshold < p50 ? "over" : "under";
  const isOver = lean === "over";
  const isOk = coverage_status === "ok";

  // Extend the x-axis past [p10, p90] when threshold falls outside the
  // band so the threshold tick is always visible inside the viewBox.
  const xMin = Math.min(p10, threshold);
  const xMax = Math.max(p90, threshold);
  const xSpan = Math.max(xMax - xMin, 1e-6);
  const xFor = (value: number) =>
    PAD_X + ((value - xMin) / xSpan) * USABLE_W;

  const x10 = xFor(p10);
  const x50 = xFor(p50);
  const x90 = xFor(p90);
  const xThreshold = xFor(threshold);

  const bandFillClass = isOver ? "fill-positive/15" : "fill-negative/15";
  const bandStrokeClass = isOver ? "stroke-positive/50" : "stroke-negative/50";
  const p50StrokeClass = isOver ? "stroke-positive" : "stroke-negative";
  const p50ValueClass = isOver ? "text-positive" : "text-negative";

  const ariaLabel =
    `Prediction interval ${p10} to ${p90} with median ${p50} versus threshold ${threshold} (${lean})`;

  return (
    <div
      role="group"
      aria-label={ariaLabel}
      data-lean={lean}
      data-coverage={coverage_status}
      className="space-y-2"
    >
      <header className="flex items-baseline justify-between gap-2">
        <span className="ticket-stat-label">Interval</span>
        <CoverageChip status={coverage_status} isOk={isOk} />
      </header>

      <svg
        viewBox={`0 0 ${VIEWBOX_W} ${VIEWBOX_H}`}
        className="block w-full"
        preserveAspectRatio="none"
        aria-hidden
      >
        {/* Baseline rail — subtle anchor so the band reads as graph,
            not a floating chip. */}
        <line
          x1={PAD_X}
          x2={VIEWBOX_W - PAD_X}
          y1={VIEWBOX_H / 2}
          y2={VIEWBOX_H / 2}
          className="stroke-border"
          strokeWidth={1}
        />
        {/* (p10, p90) range as a tonal rectangle. */}
        <rect
          x={x10}
          y={PAD_Y}
          width={Math.max(x90 - x10, 1)}
          height={VIEWBOX_H - PAD_Y * 2}
          rx={4}
          className={cn(bandFillClass, bandStrokeClass)}
          strokeWidth={1}
        />
        {/* p50 tick — central estimate, in the lean tone. */}
        <line
          x1={x50}
          x2={x50}
          y1={PAD_Y - 2}
          y2={VIEWBOX_H - PAD_Y + 2}
          className={p50StrokeClass}
          strokeWidth={2}
          strokeLinecap="round"
        />
        {/* Threshold tick — dashed, in foreground tone. The line to
            clear. */}
        <line
          x1={xThreshold}
          x2={xThreshold}
          y1={PAD_Y - 3}
          y2={VIEWBOX_H - PAD_Y + 3}
          className="stroke-foreground/70"
          strokeWidth={1}
          strokeDasharray="3 2"
        />
      </svg>

      <div className="grid grid-cols-3 items-baseline">
        <Landmark label="p10" value={p10} align="left" />
        <Landmark
          label="p50"
          value={p50}
          align="center"
          toneClass={p50ValueClass}
        />
        <Landmark label="p90" value={p90} align="right" />
      </div>

      <p className="text-[10px] leading-relaxed text-muted-foreground/70">
        Threshold{" "}
        <span className="font-mono tabular-nums tracking-tight text-foreground/80">
          {threshold}
        </span>{" "}
        · {isOver ? "above" : "below"} median
      </p>
    </div>
  );
}

interface LandmarkProps {
  label: string;
  value: number;
  align: "left" | "center" | "right";
  /** When set, the value renders with strong emphasis in this tone
   *  class. When omitted, the value renders in the default muted
   *  tone. The presence of toneClass is what indicates "this is the
   *  focal landmark" — collapsing the emphasis signal into a single
   *  prop so an emphasised value can't accidentally render untoned. */
  toneClass?: string;
}

function Landmark({ label, value, align, toneClass }: LandmarkProps) {
  return (
    <div
      className={cn(
        "flex flex-col gap-px",
        align === "center" && "items-center",
        align === "right" && "items-end",
      )}
    >
      <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground/70">
        {label}
      </span>
      <span
        className={cn(
          "font-mono tabular-nums tracking-tight",
          toneClass
            ? cn("text-[12.5px] font-medium", toneClass)
            : "text-[11px] text-muted-foreground",
        )}
      >
        {value}
      </span>
    </div>
  );
}

interface CoverageChipProps {
  status: string;
  isOk: boolean;
}

function CoverageChip({ status, isOk }: CoverageChipProps) {
  return (
    <span
      className={cn(
        "rounded-sm border px-1 py-px text-[9px] font-medium uppercase tracking-[0.12em]",
        isOk
          ? "border-positive/40 bg-positive/10 text-positive"
          : "border-border/60 bg-surface-hover/40 text-muted-foreground",
      )}
    >
      {status}
    </span>
  );
}
