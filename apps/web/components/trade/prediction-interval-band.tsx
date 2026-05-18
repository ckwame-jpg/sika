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
  /** Stat key from the selection (e.g. ``"points"``, ``"rebounds"``,
   *  ``"passing_yards"``) used to compose the plain-English headline.
   *  Optional: when omitted, the headline falls back to a unitless
   *  phrasing ("model expects ~16"). */
  statKey?: string | null;
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
 * Smarter #21 phase 2d (redesign 2026-05-17b) — horizontal SVG band
 * visualizing the (p10, p50, p90) prediction interval against the
 * market threshold.
 *
 * Redesign goals (operator feedback 2026-05-17):
 * 1. **Plain-English headline** above the bar so operators don't
 *    have to translate percentile labels into prose every time. The
 *    headline composes the median estimate, the floor/ceiling, and
 *    a clearance verdict relative to the threshold ("easy clear",
 *    "coin-flip near the line", "leans under").
 * 2. **Renamed landmarks**: ``floor`` / ``typical`` / ``ceiling``
 *    instead of ``p10`` / ``p50`` / ``p90``. The technical labels
 *    are surfaced as tooltips for the small number of operators
 *    who prefer the statistical phrasing.
 * 3. **Threshold split coloring**: the slice of the band that beats
 *    the threshold renders in the lean tone (green for over, red
 *    for under); the opposite slice renders muted. This collapses
 *    "where's the threshold vs the distribution" into a single
 *    glance instead of requiring two saccades.
 *
 * Coverage chip behavior is unchanged (``ok`` = swap active,
 * everything else = informational).
 */
export function PredictionIntervalBand({
  interval,
  statKey,
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

  // Clamp the threshold into [x10, x90] when computing the
  // "clearing" sub-rect so the green/red fill stays inside the
  // [p10, p90] band even if the threshold sits past the floor or
  // ceiling. The dashed threshold tick itself still draws at its
  // true position via ``xThreshold``.
  const xThresholdClamped = Math.min(Math.max(xThreshold, x10), x90);

  // Same lean tone for both slices, but the "clearing" slice fills at
  // ~3.5× the opacity of the "doesn't clear" slice. Operator's eye is
  // drawn to the green block in an over lean; the faded section reads
  // as "still in the distribution, but losing territory." Using a
  // single hue (vs introducing a neutral like ``fill-muted``) keeps
  // the band visually quiet — the threshold tick + headline carry the
  // load of "where exactly is the line."
  const leanFillClass = isOver ? "fill-positive/35" : "fill-negative/35";
  const leanStrokeClass = isOver ? "stroke-positive/60" : "stroke-negative/60";
  const fadedFillClass = isOver ? "fill-positive/10" : "fill-negative/10";
  const fadedStrokeClass = isOver ? "stroke-positive/30" : "stroke-negative/30";
  const p50StrokeClass = isOver ? "stroke-positive" : "stroke-negative";
  const p50ValueClass = isOver ? "text-positive" : "text-negative";

  const headline = composeHeadline({ p10, p50, p90, threshold, lean, statKey });

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

      {/* Plain-English headline — the operator's "what is this telling
          me?" answered in one line. */}
      <p
        className="text-[12.5px] leading-snug text-foreground/90"
        data-testid="prediction-interval-headline"
      >
        {headline}
      </p>

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
        {/* Threshold-split band: the "clearing" half (over=above,
            under=below the threshold) fills in the lean tone; the
            opposite half stays muted. Operators see "how much of the
            distribution actually beats the line" without doing the
            math. */}
        {isOver ? (
          <>
            <rect
              x={x10}
              y={PAD_Y}
              width={Math.max(xThresholdClamped - x10, 0.5)}
              height={VIEWBOX_H - PAD_Y * 2}
              className={cn(fadedFillClass, fadedStrokeClass)}
              strokeWidth={1}
            />
            <rect
              x={xThresholdClamped}
              y={PAD_Y}
              width={Math.max(x90 - xThresholdClamped, 0.5)}
              height={VIEWBOX_H - PAD_Y * 2}
              className={cn(leanFillClass, leanStrokeClass)}
              strokeWidth={1}
            />
          </>
        ) : (
          <>
            <rect
              x={x10}
              y={PAD_Y}
              width={Math.max(xThresholdClamped - x10, 0.5)}
              height={VIEWBOX_H - PAD_Y * 2}
              className={cn(leanFillClass, leanStrokeClass)}
              strokeWidth={1}
            />
            <rect
              x={xThresholdClamped}
              y={PAD_Y}
              width={Math.max(x90 - xThresholdClamped, 0.5)}
              height={VIEWBOX_H - PAD_Y * 2}
              className={cn(fadedFillClass, fadedStrokeClass)}
              strokeWidth={1}
            />
          </>
        )}
        {/* Typical (p50) tick — central estimate, in the lean tone. */}
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
        <Landmark label="floor" technical="p10" value={p10} align="left" />
        <Landmark
          label="typical"
          technical="p50"
          value={p50}
          align="center"
          toneClass={p50ValueClass}
        />
        <Landmark label="ceiling" technical="p90" value={p90} align="right" />
      </div>

      <p className="text-2xs leading-relaxed text-muted-foreground/70">
        Threshold{" "}
        <span className="font-mono tabular-nums tracking-tight text-foreground/80">
          {threshold}
        </span>{" "}
        · {isOver ? "above" : "below"} median
      </p>
    </div>
  );
}

/**
 * Compose a one-sentence plain-English summary of the interval
 * relative to the threshold. The headline is the operator's quick
 * read; the bar + landmarks underneath are for drill-down.
 *
 * Three buckets per lean direction:
 *   - Over lean, threshold ≤ p10: "easy clear" — even the floor beats it
 *   - Over lean, p10 < threshold ≤ p50: "leans over but bad night could miss"
 *   - Under lean, threshold > p90: "easy under" — even the ceiling misses
 *   - Under lean, p50 < threshold ≤ p90: "leans under but good night could overshoot"
 *
 * Borderline cases (threshold exactly at p10/p50/p90) fall into the
 * tighter "coin-flip" framing rather than the easy/clear one — the
 * idea is the headline should never overstate the model's confidence.
 *
 * Exported for unit tests; not used outside this file.
 */
export function composeHeadline({
  p10,
  p50,
  p90,
  threshold,
  lean,
  statKey,
}: {
  p10: number;
  p50: number;
  p90: number;
  threshold: number;
  lean: "over" | "under";
  statKey?: string | null;
}): string {
  const unit = formatUnit(statKey);
  const median = formatNumber(p50);
  const floor = formatNumber(p10);
  const ceiling = formatNumber(p90);
  const line = formatNumber(threshold);

  const expectsClause = unit
    ? `model expects ~${median} ${unit}`
    : `model expects ~${median}`;
  const rangeClause = `floor ${floor}, ceiling ${ceiling}`;

  // Strict inequalities at the floor / ceiling boundaries so a
  // threshold sitting EXACTLY at p10 (or p90) doesn't get the
  // overconfident "easy clear" framing — at the boundary it's a
  // ~90% clear (10% of the distribution sits at or below the
  // floor by definition), which still warrants a "could miss"
  // hedge. Anything strictly inside (p10, p90) — including the
  // boundary — falls into the cushion-warning bucket.
  let verdict: string;
  if (lean === "over") {
    if (threshold < p10) {
      verdict = `clears ${line} even on a bad night.`;
    } else if (threshold < p50) {
      const cushion = formatNumber(p50 - threshold);
      verdict = `leans over ${line} by ~${cushion} — but a floor night could miss.`;
    } else {
      // Edge case: threshold == p50 exactly. Treat as coin-flip.
      verdict = `right at ${line} — coin flip.`;
    }
  } else {
    if (threshold > p90) {
      verdict = `stays under ${line} even on a great night.`;
    } else if (threshold > p50) {
      const cushion = formatNumber(threshold - p50);
      verdict = `leans under ${line} by ~${cushion} — but a ceiling night could overshoot.`;
    } else {
      verdict = `right at ${line} — coin flip.`;
    }
  }

  return `${expectsClause}. ${rangeClause}. ${verdict}`;
}

/** Trim a stat key into something readable inside a sentence.
 *  ``"points"`` → ``"pts"``, ``"passing_yards"`` → ``"pass yds"``,
 *  ``null`` / unknown → ``""`` (unitless fallback). */
function formatUnit(statKey: string | null | undefined): string {
  if (!statKey) return "";
  const map: Record<string, string> = {
    points: "pts",
    rebounds: "rebs",
    assists: "asts",
    steals: "stls",
    blocks: "blks",
    made_threes: "3PM",
    turnovers: "TOs",
    minutes: "min",
    hits: "hits",
    runs: "runs",
    home_runs: "HR",
    rbis: "RBI",
    walks: "BB",
    strikeouts: "Ks",
    total_bases: "TB",
    passing_yards: "pass yds",
    passing_touchdowns: "pass TD",
    rushing_yards: "rush yds",
    rushing_touchdowns: "rush TD",
  };
  return map[statKey] ?? statKey.replace(/_/g, " ");
}

/** Render a number with a single decimal when needed, integer
 *  otherwise. Keeps the headline tight ("15" rather than "15.0"). */
function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

interface LandmarkProps {
  label: string;
  /** Statistical alias (``p10`` / ``p50`` / ``p90``) shown as a
   *  ``title`` tooltip — keeps the operator-friendly label visible
   *  while preserving the technical phrasing for hover discovery. */
  technical: string;
  value: number;
  align: "left" | "center" | "right";
  /** When set, the value renders with strong emphasis in this tone
   *  class. When omitted, the value renders in the default muted
   *  tone. The presence of toneClass is what indicates "this is the
   *  focal landmark" — collapsing the emphasis signal into a single
   *  prop so an emphasised value can't accidentally render untoned. */
  toneClass?: string;
}

function Landmark({ label, technical, value, align, toneClass }: LandmarkProps) {
  return (
    <div
      className={cn(
        "flex flex-col gap-px",
        align === "center" && "items-center",
        align === "right" && "items-end",
      )}
      title={`${technical} · ${label}`}
    >
      <span className="font-mono text-3xs uppercase tracking-[0.14em] text-muted-foreground/70">
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
        "rounded-sm border px-1 py-px text-3xs font-medium uppercase tracking-[0.12em]",
        isOk
          ? "border-positive/40 bg-positive/10 text-positive"
          : "border-border/60 bg-surface-hover/40 text-muted-foreground",
      )}
    >
      {status}
    </span>
  );
}
