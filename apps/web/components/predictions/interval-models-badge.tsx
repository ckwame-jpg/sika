"use client";

import type { ReactElement } from "react";
import { Activity } from "lucide-react";

import type { IntervalModelStatusRead } from "@/lib/types";
import { cn } from "@/lib/utils";

interface IntervalModelsBadgeProps {
  intervals: IntervalModelStatusRead[];
}

/** Smarter #21 phase 2b — operator-facing panel section for interval-
 *  model status. Same data the ``python -m ml.cli inspect-intervals``
 *  CLI emits (PR #163); this surfaces it in the readiness panel so the
 *  browser is a destination too — not just SSH + cat metadata.json.
 *
 *  Coverage status banding (mirrors the CLI):
 *    ok       → green ("settled" tone)   — safe to promote to phase 2d.
 *    warn     → amber ("pending" tone)   — investigate, drift or thin sample.
 *    bad      → red   ("lost" tone)      — do NOT promote; fix upstream.
 *    unknown  → muted ("default" tone)   — metadata missing / unparseable.
 *
 *  Pre-CLI-run state renders an empty-state explaining how to populate
 *  it (the operator's literal next action) rather than just "no data".
 */
export function IntervalModelsBadge({ intervals }: IntervalModelsBadgeProps): ReactElement {
  if (intervals.length === 0) {
    return (
      <div className="stats-tile" data-testid="interval-models-badge">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Activity size={14} className="text-muted-foreground" />
            <p className="stats-tile-label">Prediction Intervals</p>
          </div>
          <span className="outcome-pill">none</span>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          No interval models trained yet. Run{" "}
          <code className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] font-mono">
            python -m ml.cli train-intervals --family-key nba_props --stat-key points --manifest-path manifests/current.json
          </code>{" "}
          to fit the first (p10, p50, p90) regressors.
        </p>
      </div>
    );
  }

  // Per-family counts for the header summary.
  const counts = new Map<string, number>();
  for (const entry of intervals) {
    counts.set(entry.family_key, (counts.get(entry.family_key) ?? 0) + 1);
  }
  const summary = Array.from(counts.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([family, count]) => `${family}: ${count}`)
    .join(" · ");

  return (
    <div className="stats-tile" data-testid="interval-models-badge">
      <div
        className="flex items-center justify-between gap-3"
        data-testid="interval-models-header"
      >
        <div className="flex items-center gap-2">
          <Activity size={14} className="text-muted-foreground" />
          <p className="stats-tile-label">Prediction Intervals</p>
        </div>
        <span className="text-[11px] text-muted-foreground">{summary}</span>
      </div>
      <div className="mt-3 divide-y divide-white/[0.06]" role="table" aria-label="Interval models">
        <div
          role="row"
          className="grid grid-cols-[1fr_1fr_72px_72px_88px] gap-2 pb-1.5 text-[10px] uppercase tracking-wide text-muted-foreground"
        >
          <div role="columnheader">Family</div>
          <div role="columnheader">Stat</div>
          <div role="columnheader" className="text-right">Samples</div>
          <div role="columnheader" className="text-right">Coverage</div>
          <div role="columnheader" className="text-right">Status</div>
        </div>
        {intervals.map((entry) => (
          <IntervalRow key={`${entry.family_key}-${entry.stat_key}`} entry={entry} />
        ))}
      </div>
    </div>
  );
}

interface IntervalRowProps {
  entry: IntervalModelStatusRead;
}

function IntervalRow({ entry }: IntervalRowProps): ReactElement {
  const coverageDisplay =
    entry.empirical_coverage === null || entry.empirical_coverage === undefined
      ? "?"
      : entry.empirical_coverage.toFixed(2);
  const samplesDisplay = entry.sample_size === null || entry.sample_size === undefined
    ? "?"
    : String(entry.sample_size);
  return (
    <div
      role="row"
      className="grid grid-cols-[1fr_1fr_72px_72px_88px] items-center gap-2 py-1.5 text-xs"
      data-testid={`interval-row-${entry.family_key}-${entry.stat_key}`}
    >
      <div role="cell" className="font-mono text-muted-foreground">{entry.family_key}</div>
      <div role="cell" className="font-mono">{entry.stat_key}</div>
      <div role="cell" className="text-right font-mono">{samplesDisplay}</div>
      <div role="cell" className="text-right font-mono">{coverageDisplay}</div>
      <div role="cell" className="flex justify-end">
        <span
          className={cn("outcome-pill px-1.5 py-0.5 text-[10px]", coverageToneClass(entry.coverage_status))}
          data-testid="interval-status-pill"
        >
          {entry.coverage_status}
        </span>
      </div>
    </div>
  );
}

function coverageToneClass(status: IntervalModelStatusRead["coverage_status"]): string {
  switch (status) {
    case "ok":
      return "settled";
    case "warn":
      return "pending";
    case "bad":
      return "lost";
    default:
      return "";
  }
}
