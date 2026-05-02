"use client";

import { cn } from "@/lib/utils";

/**
 * Top-3 driver row written by the backend's heuristic factor pass.
 * Read first from ``features._drivers`` (new shape, includes a curated
 * label and one-line ``detail`` string); falls back to deriving from
 * ``features.advanced_factors`` for predictions captured before PR 3b.
 */
interface DriverRow {
  /** factor key, e.g. "efficiency_factor" */
  key: string;
  /** human-friendly label for the row */
  label: string;
  /** percentage delta vs neutral (1.0). +12 = +12% boost, -8 = -8% suppress. */
  deltaPct: number;
  /** optional one-line explanation pulled from underlying features */
  detail?: string | null;
}

interface WhyThisPredictionProps {
  /** ``signal.features`` from a SignalSnapshotRead. We narrow at runtime. */
  features?: Record<string, unknown> | null;
}

/**
 * Renders the top-3 advanced factors driving the predicted line. Prefers
 * ``features._drivers`` (rich, server-rendered with detail strings); falls
 * back to deriving rows from ``features.advanced_factors`` for older
 * predictions. Each row shows direction (↑/↓), label, optional detail,
 * a bar proportional to the delta, and the formatted percentage.
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
            className="flex flex-col gap-0.5 text-sm"
            data-testid={`why-driver-${driver.key}`}
          >
            <div className="flex items-center gap-3">
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
            </div>
            {driver.detail ? (
              <span
                className="ml-8 text-xs text-foreground/60"
                data-testid={`why-driver-${driver.key}-detail`}
              >
                {driver.detail}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

function extractDrivers(features: Record<string, unknown> | null): DriverRow[] {
  if (!features) return [];

  // Prefer the server-built ``_drivers`` payload — it carries curated labels
  // and detail strings ("Recent USG% 32% vs season 28%") that the client
  // can't reliably re-derive.
  const serverDrivers = features["_drivers"];
  if (Array.isArray(serverDrivers) && serverDrivers.length > 0) {
    const rows: DriverRow[] = [];
    for (const entry of serverDrivers) {
      if (!entry || typeof entry !== "object") continue;
      const e = entry as Record<string, unknown>;
      const key = typeof e.key === "string" ? e.key : null;
      const label = typeof e.label === "string" ? e.label : null;
      const deltaPctRaw = e.delta_pct;
      const deltaPct = typeof deltaPctRaw === "number" ? deltaPctRaw : Number(deltaPctRaw);
      if (!key || !label || !Number.isFinite(deltaPct)) continue;
      const detail = typeof e.detail === "string" ? e.detail : null;
      rows.push({ key, label, deltaPct, detail });
    }
    if (rows.length > 0) return rows.slice(0, 3);
  }

  // Fallback: derive from raw factor multipliers (older predictions).
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
