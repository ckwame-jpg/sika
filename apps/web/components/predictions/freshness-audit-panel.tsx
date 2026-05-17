"use client";

import type { ReactElement } from "react";
import { cn } from "@/lib/utils";
import type { FreshnessAuditRowRead } from "@/lib/types";

interface FreshnessAuditPanelProps {
  rows: FreshnessAuditRowRead[];
}

type TuningSignal = "promote" | "low_sample" | "none";

const PROMOTE_DELTA_THRESHOLD = 0.05;
const MIN_BUCKET_SAMPLES = 20;
// Calibration misses in practice live in the 0.00–0.30 band. Scale
// the bars by 3× so a 0.05 miss reads as a 15% bar (visible) instead
// of a 5% sliver (invisible), while a 0.33+ miss caps the bar at
// full width (extreme — operator should already be alarmed).
const BAR_SCALE = 3;

function classify(row: FreshnessAuditRowRead): TuningSignal {
  if (row.stale_count < MIN_BUCKET_SAMPLES || row.fresh_count < MIN_BUCKET_SAMPLES) {
    return "low_sample";
  }
  if (row.calibration_delta >= PROMOTE_DELTA_THRESHOLD) {
    return "promote";
  }
  return "none";
}

function humanizeGroupKey(groupKey: string): string {
  return groupKey.replace(/_/g, " ").toLowerCase();
}

function formatPct(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function formatSignedPct(value: number): string {
  const pct = Math.round(value * 100);
  if (pct === 0) return "±0%";
  return `${pct > 0 ? "+" : ""}${pct}%`;
}

function barWidthPercent(miss: number): number {
  const scaled = Math.max(0, Math.min(100, miss * 100 * BAR_SCALE));
  // Floor at a hair so the bar registers when miss is 0 (still
  // shows the rail anchor; communicates "bucket exists, just zero").
  return scaled > 0 ? Math.max(scaled, 1.2) : 0;
}

/**
 * Smarter #22 PR B prep — operator-facing audit panel for the freshness
 * layer's calibration deltas. Mounts on /ops/readiness alongside the
 * settlement-aging and interval-models tiles.
 *
 * Each row tells a forensic story: "for this feature group, when it
 * was stale (top bar), the model's predicted probability missed the
 * actual outcome by X. When fresh (bottom bar), it missed by Y. The
 * gap is the staleness penalty." Operators read the panel to decide
 * which IGNORE-default groups to promote in
 * ``FEATURE_GROUP_POLICIES`` — see ``SMARTER_22_TUNING_PLAYBOOK.md``.
 *
 * Visual hierarchy by ``data-tuning-signal``:
 * - ``promote`` — left rail in ``negative`` tone, full opacity. The
 *   actionable rows. Staleness measurably hurt calibration here.
 * - ``low_sample`` — muted. Bars rendered but de-emphasized; the
 *   "need ≥N more samples" note explains why.
 * - ``none`` — quiet. The baseline; no policy change suggested.
 */
export function FreshnessAuditPanel({ rows }: FreshnessAuditPanelProps): ReactElement {
  if (rows.length === 0) {
    return (
      <section
        role="region"
        aria-label="Freshness calibration audit"
        className="stats-tile"
        data-testid="freshness-audit-panel"
      >
        <Header />
        <div className="mt-3 flex items-start gap-3 rounded-md border border-dashed border-border/60 bg-surface-hover/30 p-3">
          <span
            className="mt-0.5 inline-block h-2 w-2 shrink-0 rounded-full border border-muted-foreground/50"
            aria-hidden
          />
          <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
            <p>
              No settled predictions in the audit window have persisted
              freshness diagnostics yet.
            </p>
            <p className="opacity-75">
              The audit joins{" "}
              <code className="rounded bg-white/[0.06] px-1 py-px font-mono text-[10px]">
                scoring_diagnostics.freshness_stale_groups
              </code>{" "}
              with settled outcomes. Rows appear after a slate cycles
              through and Kalshi settles the markets — expect meaningful
              sample sizes after ~1–2 weeks of game cycles.
            </p>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section
      role="region"
      aria-label="Freshness calibration audit"
      className="stats-tile"
      data-testid="freshness-audit-panel"
    >
      <Header />
      <ol className="mt-3 space-y-2">
        {rows.map((row) => (
          <AuditRow key={row.group_key} row={row} />
        ))}
      </ol>
      <Legend />
    </section>
  );
}

function Header(): ReactElement {
  return (
    <div className="flex items-end justify-between gap-3">
      <div>
        <p className="stats-tile-label">Freshness · Calibration Audit</p>
        <p className="mt-1 text-[11px] text-muted-foreground/80">
          How stale features correlate with prediction accuracy
        </p>
      </div>
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground/60">
        window 30d
      </span>
    </div>
  );
}

interface AuditRowProps {
  row: FreshnessAuditRowRead;
}

function AuditRow({ row }: AuditRowProps): ReactElement {
  const signal = classify(row);
  const staleWidth = barWidthPercent(row.stale_calibration_miss);
  const freshWidth = barWidthPercent(row.fresh_calibration_miss);
  const samplesShort = row.stale_count < MIN_BUCKET_SAMPLES
    ? row.stale_count
    : row.fresh_count < MIN_BUCKET_SAMPLES
      ? row.fresh_count
      : null;

  return (
    <li
      className={cn(
        "relative rounded-md border bg-white/[0.015] pl-3 pr-3 py-2.5 transition-colors",
        // Left rail communicates tuning signal at a glance.
        signal === "promote" && "border-negative/40 bg-negative/[0.04]",
        signal === "low_sample" && "border-border/40 opacity-60",
        signal === "none" && "border-border/50",
      )}
      data-testid={`freshness-audit-row-${row.group_key}`}
      data-tuning-signal={signal}
    >
      {/* The vertical rail anchor on promote rows — full-bleed left edge so the
          eye snaps to actionable rows when scanning the list. */}
      {signal === "promote" && (
        <span
          aria-hidden
          className="pointer-events-none absolute inset-y-0 left-0 w-[3px] rounded-l-md bg-negative/70"
        />
      )}

      <div className="flex items-baseline justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <span
            className={cn(
              "font-mono text-[12.5px] capitalize",
              signal === "promote" ? "text-foreground" : "text-muted-foreground",
            )}
          >
            {humanizeGroupKey(row.group_key)}
          </span>
          <SignalChip signal={signal} />
        </div>
        <span
          className={cn(
            "font-mono text-[13px] tabular-nums tracking-tight",
            signal === "promote" && "text-negative",
            signal === "none" && "text-muted-foreground/70",
            signal === "low_sample" && "text-muted-foreground",
          )}
        >
          {formatSignedPct(row.calibration_delta)}
        </span>
      </div>

      <div className="mt-2 space-y-1">
        <BucketBar
          label="stale"
          widthPct={staleWidth}
          missPct={formatPct(row.stale_calibration_miss)}
          count={row.stale_count}
          tone={signal === "promote" ? "negative" : "muted"}
        />
        <BucketBar
          label="fresh"
          widthPct={freshWidth}
          missPct={formatPct(row.fresh_calibration_miss)}
          count={row.fresh_count}
          tone="positive"
        />
      </div>

      {signal === "low_sample" && samplesShort !== null && (
        <p className="mt-1.5 text-[10px] text-muted-foreground/70">
          {samplesShort} of {MIN_BUCKET_SAMPLES} samples in the
          smaller bucket — readings below the recommended floor; defer
          a tuning decision until more games settle.
        </p>
      )}
    </li>
  );
}

interface BucketBarProps {
  label: "stale" | "fresh";
  widthPct: number;
  missPct: string;
  count: number;
  tone: "negative" | "positive" | "muted";
}

function BucketBar({ label, widthPct, missPct, count, tone }: BucketBarProps): ReactElement {
  return (
    <div className="grid grid-cols-[44px_1fr_44px_60px] items-center gap-2 text-[10.5px]">
      <span className="font-mono uppercase tracking-[0.12em] text-muted-foreground/70">
        {label}
      </span>
      <div className="relative h-[5px] overflow-hidden rounded-full bg-white/[0.04]">
        <span
          className={cn(
            "absolute inset-y-0 left-0 rounded-full",
            tone === "negative" && "bg-negative/70",
            tone === "positive" && "bg-positive/60",
            tone === "muted" && "bg-muted-foreground/40",
          )}
          style={{ width: `${widthPct}%` }}
          aria-hidden
        />
      </div>
      <span
        className={cn(
          "text-right font-mono tabular-nums",
          tone === "negative" && "text-negative/90",
          tone === "positive" && "text-positive/90",
          tone === "muted" && "text-muted-foreground",
        )}
      >
        {missPct}
      </span>
      <span className="text-right font-mono tabular-nums text-muted-foreground/70">
        n={count}
      </span>
    </div>
  );
}

interface SignalChipProps {
  signal: TuningSignal;
}

function SignalChip({ signal }: SignalChipProps): ReactElement {
  const text =
    signal === "promote"
      ? "promote"
      : signal === "low_sample"
        ? "low sample"
        : "no signal";
  return (
    <span
      className={cn(
        "rounded-sm border px-1 py-px text-[9px] font-medium uppercase tracking-[0.1em]",
        signal === "promote" && "border-negative/40 bg-negative/10 text-negative",
        signal === "low_sample" && "border-border/60 bg-surface-hover/40 text-muted-foreground",
        signal === "none" && "border-border/40 bg-transparent text-muted-foreground/70",
      )}
    >
      {text}
    </span>
  );
}

function Legend(): ReactElement {
  return (
    <p className="mt-3 text-[10px] leading-relaxed text-muted-foreground/60">
      Calibration miss = |predicted YES probability − actual YES rate|.
      Delta = stale − fresh; positive ⇒ staleness measurably degrades
      prediction accuracy. See{" "}
      <code className="rounded bg-white/[0.06] px-1 font-mono text-[9px]">
        SMARTER_22_TUNING_PLAYBOOK.md
      </code>{" "}
      before promoting a group in the policy registry.
    </p>
  );
}
