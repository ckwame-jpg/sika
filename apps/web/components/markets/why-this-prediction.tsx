"use client";

import { cn } from "@/lib/utils";

/**
 * Top-3 driver row written by the backend's heuristic factor pass.
 * The shape comes from `signal.features.advanced_factors` and is decoded
 * here at the type boundary because the contract field is typed
 * Record<string, unknown>.
 */
interface DriverRow {
  /** factor key, e.g. "efficiency_factor" */
  key: string;
  /** human-friendly label for the row */
  label: string;
  /** percentage delta vs neutral (1.0). +12 = +12% boost, -8 = -8% suppress. */
  deltaPct: number;
}

interface WhyThisPredictionProps {
  /** ``signal.features`` from a SignalSnapshotRead. We narrow at runtime. */
  features?: Record<string, unknown> | null;
}

/**
 * Renders the top-3 advanced factors driving the predicted line. Pulls the
 * data from ``features.advanced_factors`` (a dict of factor_name → multiplier
 * written by the heuristic pass in apps/api/app/services/scoring.py). Each
 * driver becomes a row showing the factor name, direction (↑/↓), and a bar
 * proportional to the delta.
 *
 * Hidden when there are no advanced factors — older predictions captured
 * before PR 3 ride along with no `advanced_factors` key, which is fine.
 */
export function WhyThisPrediction({ features }: WhyThisPredictionProps) {
  const drivers = extractDrivers(features ?? null);
  if (drivers.length === 0) return null;

  return (
    <section
      className="why-this-prediction rounded-lg border border-border/40 bg-card/40 p-4"
      data-testid="why-this-prediction"
      aria-label="Why this prediction"
    >
      <header className="text-sm font-semibold text-foreground/90 mb-2">
        Why this prediction?
      </header>
      <ul className="space-y-2">
        {drivers.map((driver) => (
          <li
            key={driver.key}
            className="flex items-center gap-3 text-sm"
            data-testid={`why-driver-${driver.key}`}
          >
            <DirectionArrow deltaPct={driver.deltaPct} />
            <span className="flex-1 text-foreground/80">{driver.label}</span>
            <DeltaBar deltaPct={driver.deltaPct} />
            <span
              className={cn(
                "tabular-nums w-12 text-right text-xs",
                driver.deltaPct > 0 ? "text-emerald-500" : "text-rose-500",
              )}
            >
              {formatDelta(driver.deltaPct)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function extractDrivers(features: Record<string, unknown> | null): DriverRow[] {
  if (!features) return [];
  const factors = features["advanced_factors"];
  if (!factors || typeof factors !== "object") return [];

  const rows: DriverRow[] = [];
  for (const [key, raw] of Object.entries(factors as Record<string, unknown>)) {
    const value = typeof raw === "number" ? raw : Number(raw);
    if (!Number.isFinite(value)) continue;
    const deltaPct = (value - 1) * 100;
    if (Math.abs(deltaPct) < 0.5) continue; // skip near-zero contributions
    rows.push({
      key,
      label: humanizeFactorName(key),
      deltaPct,
    });
  }
  rows.sort((a, b) => Math.abs(b.deltaPct) - Math.abs(a.deltaPct));
  return rows.slice(0, 3);
}

function humanizeFactorName(key: string): string {
  return key
    .replace(/_advanced$/, "")
    .replace(/_factor$/, "")
    .replace(/_/g, " ")
    .replace(/\bpct\b/g, "%")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatDelta(deltaPct: number): string {
  const sign = deltaPct > 0 ? "+" : "";
  return `${sign}${deltaPct.toFixed(1)}%`;
}

function DirectionArrow({ deltaPct }: { deltaPct: number }) {
  const isUp = deltaPct > 0;
  return (
    <span
      aria-hidden
      className={cn(
        "flex h-5 w-5 items-center justify-center rounded-full text-xs font-bold",
        isUp ? "bg-emerald-500/15 text-emerald-500" : "bg-rose-500/15 text-rose-500",
      )}
    >
      {isUp ? "↑" : "↓"}
    </span>
  );
}

function DeltaBar({ deltaPct }: { deltaPct: number }) {
  const magnitude = Math.min(Math.abs(deltaPct), 15); // 15% is the heuristic clamp ceiling
  const widthPct = (magnitude / 15) * 100;
  const isUp = deltaPct > 0;
  return (
    <div className="relative h-1.5 w-24 rounded-full bg-muted/40 overflow-hidden">
      <div
        className={cn(
          "absolute inset-y-0",
          isUp ? "left-1/2 bg-emerald-500/60" : "right-1/2 bg-rose-500/60",
        )}
        style={{ width: `${widthPct / 2}%` }}
      />
      <div className="absolute inset-y-0 left-1/2 w-px bg-border/60" aria-hidden />
    </div>
  );
}
