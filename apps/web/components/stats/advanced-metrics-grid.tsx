"use client";

import { cn } from "@/lib/utils";

interface AdvancedMetricsGridProps {
  /** Metric values keyed by metric_key, e.g. { ts_pct: 0.612 } */
  metrics: Record<string, number | null>;
  /** Display labels keyed by metric_key, e.g. { ts_pct: "TS%" } */
  labels: Record<string, string>;
  /** 0-100 league percentile rank per metric_key */
  percentiles: Record<string, number>;
  /** "basic" | "advanced" tags per metric_key. Metrics without a tag are
   *  treated as "basic" and rendered in the basic group above this grid. */
  categories: Record<string, "basic" | "advanced">;
}

/**
 * Renders the "advanced" metrics group with a horizontal percentile bar per
 * metric. Color follows the percentile (red < 33, neutral 33-66, green > 66)
 * so a glance at the row tells you where the player ranks against the league.
 */
export function AdvancedMetricsGrid({
  metrics,
  labels,
  percentiles,
  categories,
}: AdvancedMetricsGridProps) {
  const advancedKeys = Object.keys(metrics).filter(
    (key) => categories[key] === "advanced" && metrics[key] != null,
  );

  if (advancedKeys.length === 0) {
    return null;
  }

  return (
    <section
      className="sa-advanced-grid"
      aria-label="Advanced metrics"
      data-testid="sa-advanced-grid"
    >
      <header className="sa-advanced-grid-h">Advanced</header>
      <div className="sa-advanced-grid-rows">
        {advancedKeys.map((key) => {
          const value = metrics[key];
          const label = labels[key] ?? key;
          const percentile = percentiles[key];
          return (
            <div key={key} className="sa-advanced-row" data-testid={`sa-advanced-${key}`}>
              <div className="sa-advanced-row-meta">
                <span className="sa-advanced-row-label">{label}</span>
                <span className="sa-advanced-row-value">{formatValue(value)}</span>
              </div>
              <PercentileBar percentile={percentile} />
            </div>
          );
        })}
      </div>
    </section>
  );
}

function formatValue(value: number | null): string {
  if (value == null) return "—";
  if (Number.isInteger(value)) return String(value);
  // Rate stats are typically 0-1, so display with 3 decimals
  if (Math.abs(value) < 1) return value.toFixed(3);
  return value.toFixed(2);
}

interface PercentileBarProps {
  percentile?: number;
}

function PercentileBar({ percentile }: PercentileBarProps) {
  if (percentile == null || !Number.isFinite(percentile)) {
    return (
      <div className="sa-advanced-row-bar is-empty" aria-label="No percentile data">
        <span className="sa-advanced-row-pct">—</span>
      </div>
    );
  }

  const clamped = Math.max(0, Math.min(100, percentile));
  const tone = clamped < 33 ? "low" : clamped > 66 ? "high" : "mid";

  return (
    <div
      className={cn("sa-advanced-row-bar", `is-${tone}`)}
      role="progressbar"
      aria-valuenow={Math.round(clamped)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`League percentile ${Math.round(clamped)}`}
    >
      <div className="sa-advanced-row-fill" style={{ width: `${clamped}%` }} />
      <span className="sa-advanced-row-pct">{Math.round(clamped)}</span>
    </div>
  );
}
