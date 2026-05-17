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
  /** Total confidence penalty applied across all stale groups.
   *  Surfaced in the footnote so the operator sees the aggregate
   *  effect without summing individual deltas. ``null`` when no
   *  penalty was applied (all stale groups were SUPPRESS severity, or
   *  no groups are stale). */
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
  if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)}m`;
  if (ageSeconds < 86400) return `${Math.floor(ageSeconds / 3600)}h`;
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

const SEVERITY_CHIP_CLASS: Record<Severity, string> = {
  suppress: "border-negative/40 bg-negative/10 text-negative",
  penalize: "border-warning/40 bg-warning/10 text-warning",
  ignore: "border-border/60 bg-surface-hover/40 text-muted-foreground",
};

const SEVERITY_RAIL_CLASS: Record<Severity, string> = {
  suppress: "bg-negative/70",
  penalize: "bg-warning/70",
  ignore: "bg-muted-foreground/30",
};

/**
 * Smarter #22 PR A (redesign 2026-05-17) — operator-facing badge that
 * surfaces which feature groups went stale for this recommendation
 * and what the Architecture #5 freshness policy charged the
 * confidence.
 *
 * Reads from ``TradeDeskThresholdRead.freshness_stale_groups`` +
 * ``TradeDeskThresholdRead.freshness_confidence_delta`` (and the
 * matching fields on ``TradeDeskGameLineRead``). The diagnostics are
 * populated by the scoring kernel — see
 * ``apps/api/app/services/scoring/__init__.py`` around the
 * ``check_freshness`` call and the ``freshness_stale_groups`` write.
 *
 * Renders only when at least one group is stale. The eyebrow chip
 * carries the max severity; per-row left rails escalate per-group
 * severity at a glance. Aggregate confidence delta appears in the
 * footnote when nonzero.
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
      className="space-y-2"
    >
      <header className="flex items-baseline justify-between gap-2">
        <span className="ticket-stat-label">Stale data</span>
        <span
          className={cn(
            "rounded-sm border px-1 py-px text-3xs font-medium uppercase tracking-[0.12em]",
            SEVERITY_CHIP_CLASS[severity],
          )}
        >
          {severity}
        </span>
      </header>

      <ol className="space-y-1">
        {staleGroups.map((group) => (
          <StaleGroupRow key={group.group_key} group={group} />
        ))}
      </ol>

      {totalDelta !== 0 && (
        <p className="text-2xs leading-relaxed text-muted-foreground/70">
          Aggregate confidence{" "}
          <span
            className={cn(
              "font-mono tabular-nums tracking-tight",
              totalDelta < 0 ? "text-negative" : "text-positive",
            )}
          >
            {formatPct(totalDelta)}
          </span>{" "}
          for stale data
        </p>
      )}
    </div>
  );
}

interface StaleGroupRowProps {
  group: FreshnessStaleGroup;
}

function StaleGroupRow({ group }: StaleGroupRowProps) {
  const hasBackground = group.severity !== "ignore";
  const hasDelta = group.confidence_delta !== 0;
  return (
    <li
      data-severity={group.severity}
      className={cn(
        "relative rounded-sm py-1 pl-2.5 pr-1",
        hasBackground && "bg-surface-softer",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-y-1 left-0 w-[2px] rounded-full",
          SEVERITY_RAIL_CLASS[group.severity],
        )}
      />
      <div className="grid grid-cols-[1fr_auto_auto] items-baseline gap-x-2 text-2xs">
        <span className="truncate font-mono capitalize text-foreground">
          {humanizeGroupKey(group.group_key)}
        </span>
        <span className="font-mono tabular-nums text-muted-foreground/80">
          {formatAge(group.age_seconds)}
        </span>
        <span
          className={cn(
            "font-mono tabular-nums tracking-tight",
            !hasDelta && "text-muted-foreground/40",
            hasDelta && group.confidence_delta < 0 && "text-negative",
            hasDelta && group.confidence_delta > 0 && "text-positive",
          )}
        >
          {hasDelta ? formatPct(group.confidence_delta) : "—"}
        </span>
      </div>
      {group.source && (
        <p className="mt-0.5 pl-px font-mono text-3xs tracking-tight text-muted-foreground/55">
          {group.source}
        </p>
      )}
    </li>
  );
}
