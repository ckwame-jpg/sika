"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { fetchOpsMappings, fetchRun, fetchRuns, keys } from "@/lib/api";
import type { MarketMappingListItemRead, RunDetailRead, RunRead } from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtDatetime, fmtRelative, sportLabel } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { useHealthStatus } from "@/lib/health-status";
import { sportTint } from "@/lib/sport-tints";

function statusChipClass(status: string): string {
  if (status === "completed") return "success";
  if (status === "failed") return "failed";
  return "running";
}

function statusChipLabel(status: string): string {
  if (status === "completed") return "success";
  if (status === "failed") return "failed";
  return "running";
}

function countEntries(details: Record<string, unknown>, key: string) {
  const value = details[key];
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>);
}

function runKindLabel(kind: string): string {
  if (kind === "prop_refresh") return "Maintenance Refresh";
  if (kind === "shadow_capture") return "Shadow Capture";
  if (kind === "settlement") return "Settlement";
  if (kind === "cleanup") return "Cleanup";
  return "Refresh";
}

function settlementUpdateCount(summary: RunRead["summary_counts"]) {
  return summary.prediction_settlement_updated + summary.parlay_prediction_settlement_updated;
}

function nonZeroEntries(values: Record<string, number>) {
  return Object.entries(values).filter(([, count]) => Number(count) > 0);
}

function runDuration(run: RunRead): string {
  if (!run.finished_at) return "—";
  const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
}

function fmtStartedTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function clampPct(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function MetricTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stats-tile">
      <p className="stats-tile-label">{label}</p>
      <p className="stats-tile-value font-mono text-lg">{value}</p>
    </div>
  );
}

/** Per-kind meta line for a run row (kept from the master list — the
 *  settlement variant is load-bearing for tests + operator scanning). */
function runMeta(run: RunRead): string {
  if (run.kind === "shadow_capture") {
    return `${run.records_processed} shadow captures`;
  }
  if (run.kind === "settlement") {
    const summary = run.summary_counts;
    return `${settlementUpdateCount(summary)} updates · ${summary.prediction_settlement_updated} singles · ${summary.parlay_prediction_settlement_updated} parlays`;
  }
  const parts = [
    `${run.summary_counts.supported_markets_kept} markets`,
    `${run.summary_counts.recommendations_emitted} recs`,
  ];
  if (run.kind === "prop_refresh") parts.push(`${run.summary_counts.prop_subjects_warmed} props warmed`);
  if (run.summary_counts.predictions_captured > 0) parts.push(`${run.summary_counts.predictions_captured} preds`);
  return parts.join(" · ");
}

/** Spec 5d gauge row: last completed run / mappings needing review / runs today. */
function GaugeRow({
  runs,
  mappings,
}: {
  runs: RunRead[] | undefined;
  mappings: MarketMappingListItemRead[] | undefined;
}) {
  const lastCompleted = runs?.find((run) => run.status === "completed") ?? null;
  const lastFailed = Boolean(runs?.length && runs[0].status === "failed");

  const reviewCount = mappings?.length ?? null;
  const reviewColor = reviewCount == null ? "var(--gi-micro-rail)" : reviewCount === 0 ? "var(--gi-green)" : "var(--gi-amber)";

  const today = new Date().toDateString();
  const runsToday = (runs ?? []).filter((run) => new Date(run.started_at).toDateString() === today);
  const completedToday = runsToday.filter((run) => run.status === "completed").length;

  return (
    <div className="gi-gauge-row three">
      <div className="gi-card gi-gauge-card" data-testid="runs-gauge-last">
        <div
          className="gi-gauge"
          style={
            {
              "--gg-p": lastCompleted ? 100 : 0,
              "--gg-c": lastFailed ? "var(--gi-orange)" : "var(--gi-green)",
            } as React.CSSProperties
          }
          aria-hidden
        >
          <span className="gi-gauge-value">{lastCompleted ? "ok" : "—"}</span>
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">last completed run</span>
          <span className="gi-gauge-title">{lastCompleted ? "success" : "no runs yet"}</span>
          <span className="gi-gauge-sub">
            {lastCompleted
              ? `${runKindLabel(lastCompleted.kind).toLowerCase()} · ${fmtRelative(lastCompleted.finished_at ?? lastCompleted.started_at)}`
              : "waiting on first run"}
          </span>
        </div>
      </div>
      <div className="gi-card gi-gauge-card" data-testid="runs-gauge-mappings">
        <div
          className="gi-gauge"
          style={
            {
              "--gg-p": reviewCount == null ? 0 : reviewCount === 0 ? 100 : clampPct(100 - reviewCount * 8),
              "--gg-c": reviewColor,
            } as React.CSSProperties
          }
          aria-hidden
        >
          <span className="gi-gauge-value">{reviewCount ?? "—"}</span>
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">mapping review queue</span>
          <span className="gi-gauge-title">
            {reviewCount == null ? "—" : reviewCount === 0 ? "clean" : `${reviewCount} open`}
          </span>
          <span className="gi-gauge-sub">low-confidence kalshi ↔ event links</span>
        </div>
      </div>
      <div className="gi-card gi-gauge-card" data-testid="runs-gauge-today">
        <div
          className="gi-gauge"
          style={
            {
              "--gg-p": runsToday.length > 0 ? clampPct((completedToday / runsToday.length) * 100) : 0,
              "--gg-c": "var(--color-cosmos-violet-500)",
            } as React.CSSProperties
          }
          aria-hidden
        >
          <span className="gi-gauge-value">{runsToday.length}</span>
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">runs today</span>
          <span className="gi-gauge-title">{runsToday.length} runs</span>
          <span className="gi-gauge-sub">
            {runsToday.reduce((sum, run) => sum + run.records_processed, 0)} records processed
          </span>
        </div>
      </div>
    </div>
  );
}

/** Mapping health + scheduler rail (spec 5d right column). */
function OpsRail({ mappings }: { mappings: MarketMappingListItemRead[] | undefined }) {
  const { data: health } = useHealthStatus();

  const bySport = new Map<string, number>();
  for (const item of mappings ?? []) {
    const sport = item.sport_key ?? "other";
    bySport.set(sport, (bySport.get(sport) ?? 0) + 1);
  }
  const sports = [...bySport.entries()].sort((a, b) => b[1] - a[1]);
  const maxCount = sports.length > 0 ? Math.max(...sports.map(([, count]) => count)) : 0;
  const total = mappings?.length ?? 0;

  return (
    <div className="gi-cols-rail hidden flex-col gap-4 xl:flex">
      <div className="gi-rail" data-testid="runs-mapping-rail">
        <span className="gi-micro-label rail">mapping health · kalshi ↔ events</span>
        {sports.length === 0 ? (
          <>
            <div className="gi-rail-stat">
              <span>review queue</span>
              <span className="v" style={{ color: "var(--gi-green)" }}>clean</span>
            </div>
            <div className="gi-coverage-bar clean" aria-hidden>
              <span style={{ "--cv-p": 100 } as React.CSSProperties} />
            </div>
          </>
        ) : (
          sports.map(([sport, count]) => (
            <div key={sport} className="flex flex-col gap-1.5">
              <div className="gi-rail-stat">
                <span className="flex items-center gap-2">
                  <span
                    className="gi-glow-dot"
                    style={{ "--gd": sportTint(sport, "var(--color-cosmos-violet-500)") } as React.CSSProperties}
                    aria-hidden
                  />
                  {sportLabel(sport).toLowerCase()}
                </span>
                <span className="v" style={{ color: "var(--gi-amber)" }}>{count} open</span>
              </div>
              <div className="gi-coverage-bar" aria-hidden>
                <span style={{ "--cv-p": clampPct(100 - (count / Math.max(maxCount, 1)) * 60) } as React.CSSProperties} />
              </div>
            </div>
          ))
        )}
        <Link href="/mappings" className="gi-btn-ghost">
          {total > 0 ? `review ${total} unmapped ›` : "open mappings ›"}
        </Link>
      </div>

      <div className="gi-rail" data-testid="runs-scheduler-rail">
        <span className="gi-micro-label rail">scheduler</span>
        <div className="gi-rail-stat">
          <span>auto refresh</span>
          <span className="v" style={{ color: health?.scheduler_enabled ? "var(--gi-green)" : "var(--gi-orange)" }}>
            {health?.scheduler_enabled ? "on" : "off"}
          </span>
        </div>
        <div className="gi-rail-stat">
          <span>slate refresh</span>
          <span className="v">{health?.refresh_status ?? "—"}</span>
        </div>
        <div className="gi-rail-stat">
          <span>prop refresh</span>
          <span className="v">{health?.prop_refresh_status ?? "—"}</span>
        </div>
        <div className="gi-rail-stat">
          <span>last good refresh</span>
          <span className="v">{fmtRelative(health?.last_successful_refresh_at)}</span>
        </div>
      </div>
    </div>
  );
}

export function RunsDesk() {
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const { data: health } = useHealthStatus();
  const { data: runs, isLoading } = useSWR<RunRead[]>(
    keys.runs,
    () => fetchRuns(25),
    { refreshInterval: 30_000 },
  );
  const { data: mappings } = useSWR<MarketMappingListItemRead[]>(
    "/ops/market-mapping?review",
    () => fetchOpsMappings({ limit: 100 }),
    { refreshInterval: 60_000 },
  );
  const { data: detail, isLoading: detailLoading } = useSWR<RunDetailRead>(
    selectedRunId != null ? keys.run(selectedRunId) : null,
    () => fetchRun(selectedRunId as number),
    { refreshInterval: 30_000 },
  );

  useEffect(() => {
    if (runs?.length && selectedRunId == null) {
      setSelectedRunId(runs[0].id);
    }
  }, [runs, selectedRunId]);

  const fetchErrors = detail ? countEntries(detail.details, "sports_fetch_errors") : [];
  const watchlistCounts = detail ? countEntries(detail.details, "watchlist_counts_by_sport") : [];
  const predictionOutcomes = detail
    ? nonZeroEntries(detail.summary_counts.prediction_outcomes ?? {})
    : [];
  const parlayPredictionOutcomes = detail
    ? nonZeroEntries(detail.summary_counts.parlay_prediction_outcomes ?? {})
    : [];
  const shadowScope = String(detail?.details.shadow_capture_scope ?? "");
  const shadowPredictionsCaptured = Number(detail?.details.shadow_predictions_captured ?? 0);
  const shadowParlaysCaptured = Number(detail?.details.shadow_parlay_predictions_captured ?? 0);
  const shadowSourceRunId = detail?.details.source_run_id != null ? String(detail.details.source_run_id) : null;
  const settlementProcessedSoFar = Number(detail?.details.processed_so_far ?? detail?.records_processed ?? 0);
  const settlementBatchSize = Number(detail?.details.batch_size ?? 0);

  const visibleRuns = useMemo(() => (runs ?? []).slice(0, 12), [runs]);

  return (
    <div className="gi-screen">
      <GaugeRow runs={runs} mappings={mappings} />

      <div className="gi-cols">
        <div className="gi-cols-main">
          <section className="gi-panel">
            <div className="gi-panel-head">
              <span className="gi-glow-dot" aria-hidden />
              <h2 className="gi-panel-title">recent runs</h2>
              {health?.active_refresh_job && (
                <span className="gi-status-chip running">
                  <span className="gi-glow-dot" aria-hidden />
                  refresh #{health.active_refresh_job.id} {health.active_refresh_job.status}
                </span>
              )}
              <span className="gi-count-chip">
                auto refresh: {health?.scheduler_enabled ? "on" : "off"}
              </span>
            </div>
            <div className="gi-run-colhead">
              <span>run</span>
              <span>started</span>
              <span>duration</span>
              <span>records</span>
              <span>status</span>
            </div>
            <div className="gi-run-rows">
              {isLoading
                ? Array.from({ length: 6 }).map((_, index) => (
                    <div key={index} className="gi-run-row">
                      <Skeleton className="h-9 w-full" />
                    </div>
                  ))
                : visibleRuns.map((run) => {
                    const runningRow = run.status !== "completed" && run.status !== "failed";
                    return (
                      <button
                        key={run.id}
                        type="button"
                        onClick={() => setSelectedRunId(run.id)}
                        className={cn(
                          "gi-run-row focus-visible:ring-focus",
                          runningRow && "running",
                          selectedRunId === run.id && !runningRow && "selected",
                        )}
                        data-testid="run-row"
                      >
                        <div className="min-w-0">
                          <p className="gi-run-title">
                            run_{run.id} · {runKindLabel(run.kind).toLowerCase()}
                          </p>
                          {runningRow ? (
                            <span className="gi-run-progress" aria-hidden />
                          ) : (
                            <p className={cn("gi-run-sub", run.status === "failed" && "err")}>
                              {run.status === "failed" && run.error_message
                                ? run.error_message
                                : runMeta(run)}
                            </p>
                          )}
                        </div>
                        <span className="mono">{fmtStartedTime(run.started_at)}</span>
                        <span className="mono">{runDuration(run)}</span>
                        <span className="mono">{run.records_processed}</span>
                        <span>
                          <span className={cn("gi-status-chip", statusChipClass(run.status))}>
                            {run.status !== "completed" && run.status !== "failed" && (
                              <span className="gi-glow-dot" aria-hidden />
                            )}
                            {statusChipLabel(run.status)}
                          </span>
                        </span>
                      </button>
                    );
                  })}
            </div>
            <div className="gi-panel-foot">
              {runs ? `showing ${visibleRuns.length} of ${runs.length} recent runs` : "loading runs…"}
            </div>
          </section>

          <section className="gi-panel">
            <div className="gi-panel-head">
              <span className="gi-glow-dot" style={{ "--gd": "var(--color-cosmos-violet-500)" } as React.CSSProperties} aria-hidden />
              <h2 className="gi-panel-title">
                {detail ? `${runKindLabel(detail.kind)} #${detail.id}` : "Run Detail"}
              </h2>
              {detail && (
                <span className="gi-panel-sub">
                  {fmtDatetime(detail.started_at)} · {detail.records_processed} processed
                </span>
              )}
            </div>
            <div className="p-4">
              {detailLoading || !detail ? (
                <div className="space-y-3">
                  <Skeleton className="h-16 w-full rounded-xl" />
                  <Skeleton className="h-48 w-full rounded-xl" />
                </div>
              ) : (
                <div className="grid gap-4">
                  {detail.kind === "shadow_capture" ? (
                    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                      <MetricTile label="Scope" value={shadowScope || "shadow_capture"} />
                      <MetricTile label="Shadow Singles" value={shadowPredictionsCaptured} />
                      <MetricTile label="Shadow Parlays" value={shadowParlaysCaptured} />
                      <MetricTile label="Source Run" value={shadowSourceRunId ?? "backfill"} />
                    </div>
                  ) : detail.kind === "settlement" ? (
                    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                      <MetricTile label="Processed" value={settlementProcessedSoFar} />
                      <MetricTile label="Single Updates" value={detail.summary_counts.prediction_settlement_updated} />
                      <MetricTile label="Parlay Updates" value={detail.summary_counts.parlay_prediction_settlement_updated} />
                      <MetricTile label="Batch Size" value={settlementBatchSize || "—"} />
                    </div>
                  ) : (
                    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                      <MetricTile label="Markets Seen" value={detail.summary_counts.total_kalshi_markets_seen} />
                      <MetricTile label="Supported Kept" value={detail.summary_counts.supported_markets_kept} />
                      <MetricTile label="Mapped Props" value={detail.summary_counts.mapped_prop_markets} />
                      <MetricTile
                        label={detail.kind === "prop_refresh" ? "Props Warmed" : "Recommendations"}
                        value={
                          detail.kind === "prop_refresh"
                            ? detail.summary_counts.prop_subjects_warmed
                            : detail.summary_counts.recommendations_emitted
                        }
                      />
                      <MetricTile
                        label={detail.kind === "prop_refresh" ? "Search Cache" : "Predictions Captured"}
                        value={
                          detail.kind === "prop_refresh"
                            ? `${detail.summary_counts.player_search_cache_hits}/${detail.summary_counts.player_search_cache_misses}`
                            : detail.summary_counts.predictions_captured
                        }
                      />
                      <MetricTile
                        label={detail.kind === "prop_refresh" ? "Gamelog Cache" : "Settlements Updated"}
                        value={
                          detail.kind === "prop_refresh"
                            ? `${detail.summary_counts.gamelog_cache_hits}/${detail.summary_counts.gamelog_cache_misses}`
                            : detail.summary_counts.prediction_settlement_updated
                        }
                      />
                    </div>
                  )}

                  <div className="grid gap-4 xl:grid-cols-3">
                    <div className="stats-tile">
                      <p className="stats-tile-label">Sports Ingested</p>
                      <div className="mt-2 space-y-2">
                        {Object.entries(detail.summary_counts.sports_records_ingested).map(([sport, count]) => (
                          <div key={sport} className="flex items-center justify-between rounded-lg border border-border px-3 py-2 text-sm">
                            <span className="text-foreground">{sport}</span>
                            <span className="font-mono text-muted-foreground">{count}</span>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="stats-tile">
                      <p className="stats-tile-label">Watchlist By Sport</p>
                      <div className="mt-2 space-y-2">
                        {watchlistCounts.length === 0 ? (
                          <p className="text-xs text-muted-foreground">No watchlist recommendations emitted.</p>
                        ) : (
                          watchlistCounts.map(([sport, count]) => (
                            <div key={sport} className="flex items-center justify-between rounded-lg border border-border px-3 py-2 text-sm">
                              <span className="text-foreground">{sport}</span>
                              <span className="font-mono text-muted-foreground">{String(count)}</span>
                            </div>
                          ))
                        )}
                      </div>
                    </div>

                    <div className="stats-tile">
                      <p className="stats-tile-label">Prediction Outcomes</p>
                      <div className="mt-2 space-y-2">
                        {predictionOutcomes.length === 0 ? (
                          <p className="text-xs text-muted-foreground">No settlements this run.</p>
                        ) : (
                          predictionOutcomes.map(([outcome, count]) => (
                            <div key={outcome} className="flex items-center justify-between rounded-lg border border-border px-3 py-2 text-sm">
                              <span className="text-foreground capitalize">{outcome}</span>
                              <span className="font-mono text-muted-foreground">{String(count)}</span>
                            </div>
                          ))
                        )}
                      </div>
                    </div>
                  </div>

                  {parlayPredictionOutcomes.length > 0 && (
                    <div className="stats-tile">
                      <p className="stats-tile-label">Parlay Outcomes</p>
                      <div className="mt-2 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                        {parlayPredictionOutcomes.map(([outcome, count]) => (
                          <div key={outcome} className="flex items-center justify-between rounded-lg border border-border px-3 py-2 text-sm">
                            <span className="text-foreground capitalize">{outcome}</span>
                            <span className="font-mono text-muted-foreground">{String(count)}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {fetchErrors.length > 0 && (
                    <div className="rounded-xl border border-warning/20 bg-warning/8 px-3 py-3">
                      <p className="text-[11px] uppercase tracking-[0.14em] text-warning">Sports Fetch Errors</p>
                      <div className="mt-2 space-y-2">
                        {fetchErrors.map(([sport, value]) => (
                          <div key={sport} className="rounded-lg border border-warning/20 px-3 py-2 text-sm text-warning">
                            <p className="font-medium">{sport}</p>
                            <p className="mt-1 text-xs">{JSON.stringify(value)}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {detail.error_message && (
                    <div className="rounded-xl border border-negative/20 bg-negative/8 px-3 py-3 text-sm text-negative">
                      {detail.error_message}
                    </div>
                  )}
                </div>
              )}
            </div>
          </section>
        </div>

        <OpsRail mappings={mappings} />
      </div>
    </div>
  );
}
