"use client";

import type { FreshnessStaleGroup } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Re-export so existing imports keep working. The canonical type lives
 * in ``lib/types.ts`` as a generated-schema shim
 * (``Wire<Schema<"FreshnessStaleGroupRead">>``) so it stays in sync
 * with the API's Pydantic schema automatically.
 */
export type { FreshnessStaleGroup };

interface FreshnessBadgeProps {
  staleGroups: FreshnessStaleGroup[] | null | undefined;
  /** Total confidence penalty applied across all stale groups. Surfaced
   *  in the summary line as ``-X%`` so the operator sees the aggregate
   *  effect without summing individual deltas. ``null`` when no penalty
   *  was applied (all stale groups were SUPPRESS severity, or no groups
   *  are stale). */
  confidenceDelta: number | null | undefined;
}

type Severity = FreshnessStaleGroup["severity"];

const SEVERITY_RANK: Record<Severity, number> = {
  ignore: 0,
  penalize: 1,
  suppress: 2,
};

/** Highest-severity tone for the badge's outer container. SUPPRESS
 *  takes precedence over PENALIZE even when only one of many groups
 *  is at SUPPRESS — operators should notice the worst signal first. */
function maxSeverity(staleGroups: FreshnessStaleGroup[]): Severity {
  return staleGroups.reduce<Severity>(
    (acc, group) =>
      SEVERITY_RANK[group.severity] > SEVERITY_RANK[acc] ? group.severity : acc,
    "ignore",
  );
}

/** Humanize age_seconds for operator reading. Sub-hour → minutes;
 *  sub-day → hours; day+ → days. Truncates rather than rounds so a
 *  6:59:00-stale group reads as "6h", not "7h". Null when the kernel
 *  appended a stale entry without a fresh_at timestamp (defensive —
 *  current code paths don't produce this, but the schema allows it). */
function formatAge(ageSeconds: number | null): string {
  if (ageSeconds == null) return "—";
  if (ageSeconds < 3600) {
    return `${Math.floor(ageSeconds / 60)}m`;
  }
  if (ageSeconds < 86400) {
    return `${Math.floor(ageSeconds / 3600)}h`;
  }
  return `${Math.floor(ageSeconds / 86400)}d`;
}

/** Humanize group_key: ``mlb_weather`` → ``mlb weather``. Lowercased so
 *  the badge reads as a soft label, not a constant. */
function humanizeGroupKey(groupKey: string): string {
  return groupKey.replace(/_/g, " ").toLowerCase();
}

/** Format a signed percentage from a confidence delta in [-1, 1].
 *  ``-0.05`` → ``-5%``. Drops the sign for zero. */
function formatPct(delta: number): string {
  const pct = Math.round(delta * 100);
  if (pct === 0) return "0%";
  return `${pct > 0 ? "+" : ""}${pct}%`;
}

/**
 * Smarter #22 PR A — operator-facing badge that surfaces which
 * feature groups went stale for this recommendation and what the
 * Architecture #5 freshness policy charged the confidence.
 *
 * Reads from ``TradeDeskThresholdRead.freshness_stale_groups`` +
 * ``TradeDeskThresholdRead.freshness_confidence_delta`` (and the
 * matching fields on ``TradeDeskGameLineRead``). The diagnostics
 * are populated by the scoring kernel — see
 * ``apps/api/app/services/scoring/__init__.py`` around the
 * ``check_freshness`` call and the ``freshness_stale_groups`` write.
 *
 * The badge renders **only when at least one group is stale**. When
 * nothing is stale the trade ticket stays clean — operators don't
 * need an "all fresh" pill cluttering every pick.
 *
 * Coloring escalates with the maximum severity: SUPPRESS (red) for
 * groups that would suppress the recommendation, PENALIZE (amber) for
 * groups that only reduced confidence, IGNORE (muted) for
 * informational stale groups. The operator notices the worst signal
 * first.
 */
export function FreshnessBadge({
  staleGroups,
  confidenceDelta,
}: FreshnessBadgeProps) {
  if (!staleGroups || staleGroups.length === 0) return null;

  const severity = maxSeverity(staleGroups);
  const totalDelta = confidenceDelta ?? 0;
  // Aria-label always names the max severity so a screen-reader user
  // knows whether the badge represents a structural drop (suppress)
  // vs a numeric penalty (penalize) vs informational (ignore) —
  // before they hear the count or the delta.
  const ariaLabel =
    `Stale feature groups (${severity}): ${staleGroups.length} stale` +
    (totalDelta !== 0 ? `, ${formatPct(totalDelta)} confidence` : "");

  return (
    <div
      role="group"
      aria-label={ariaLabel}
      data-max-severity={severity}
      className="space-y-1.5"
    >
      <div className="flex items-center justify-between text-xs">
        <span className="font-medium text-muted-foreground uppercase tracking-wider">
          Stale data
        </span>
        <span
          className={cn(
            "rounded border px-1.5 py-px text-[10px] font-medium uppercase tracking-wider",
            severity === "suppress"
              ? "border-negative/30 bg-negative/10 text-negative"
              : severity === "penalize"
                ? "border-warning/30 bg-warning/10 text-warning"
                : "border-muted-foreground/30 bg-surface-hover text-muted-foreground",
          )}
        >
          {severity}
        </span>
      </div>
      <ul className="space-y-1">
        {staleGroups.map((group) => (
          <li
            key={group.group_key}
            className="flex items-center justify-between gap-2 text-[10px] font-mono text-muted-foreground"
          >
            <span className="capitalize text-foreground">
              {humanizeGroupKey(group.group_key)}
            </span>
            <span className="text-[9px] tabular-nums">{formatAge(group.age_seconds)}</span>
            {group.confidence_delta !== 0 && (
              <span
                className={cn(
                  "tabular-nums",
                  group.confidence_delta < 0 ? "text-negative" : "text-positive",
                )}
              >
                {formatPct(group.confidence_delta)}
              </span>
            )}
            {group.source && (
              <span className="truncate text-[9px] opacity-60">{group.source}</span>
            )}
          </li>
        ))}
      </ul>
      {totalDelta !== 0 && (
        <p className="text-[10px] text-muted-foreground">
          Confidence adjusted{" "}
          <span className={totalDelta < 0 ? "text-negative" : "text-positive"}>
            {formatPct(totalDelta)}
          </span>{" "}
          for stale data.
        </p>
      )}
    </div>
  );
}
