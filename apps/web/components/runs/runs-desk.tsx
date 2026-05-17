"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { fetchRun, fetchRuns, keys } from "@/lib/api";
import type { RunDetailRead, RunRead } from "@/lib/types";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtDatetime } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { useHealthStatus } from "@/lib/health-status";

function statusPillClass(status: string): string {
  if (status === "completed") return "settled";
  if (status === "failed") return "lost";
  return "pending";
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

function MetricTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stats-tile">
      <p className="stats-tile-label">{label}</p>
      <p className="stats-tile-value font-mono text-lg">{value}</p>
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

  return (
    <div className="grid h-full min-h-0 gap-4 overflow-auto xl:grid-cols-[360px_minmax(0,1fr)]">
      <section className="cosmos-panel relative z-10 min-h-0 overflow-hidden">
        <div className="cosmos-panel-head">
          <div className="cosmos-panel-head-text">
            <h2 className="cosmos-panel-title">Recent Runs</h2>
          </div>
        </div>
        <div className="cosmos-panel-body min-h-0 pb-0">
          {health?.active_refresh_job && (
            <div className="mb-3 rounded-xl border border-warning/20 bg-warning/8 px-3 py-3 text-sm">
              <div className="flex items-center gap-2">
                <span className="outcome-pill pending">{health.active_refresh_job.status}</span>
                <span className="text-foreground">
                  {health.active_refresh_job.scope === "current_slate" ? "Current-slate refresh" : "Refresh job"} #{health.active_refresh_job.id}
                </span>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                Queued {fmtDatetime(health.active_refresh_job.queued_at)}
              </p>
            </div>
          )}
          <ScrollArea className="h-[560px] pr-3">
            <div className="space-y-2">
              {isLoading
                ? Array.from({ length: 6 }).map((_, index) => (
                    <Skeleton key={index} className="h-20 w-full rounded-xl" />
                  ))
                : runs?.map((run) => (
                    <button
                      key={run.id}
                      onClick={() => setSelectedRunId(run.id)}
                      className={cn(
                        "w-full rounded-xl border px-3 py-3 text-left transition-colors duration-[120ms]",
                        "focus-visible:ring-focus",
                        selectedRunId === run.id
                          ? "border-accent/30 bg-accent/8"
                          : "border-border bg-surface-hover hover:border-border-bright",
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-sm font-medium text-foreground">
                          {runKindLabel(run.kind)} #{run.id}
                        </p>
                        <span className={cn("outcome-pill", statusPillClass(run.status))}>{run.status}</span>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {fmtDatetime(run.started_at)}
                      </p>
                      <div className="mt-2 flex items-center gap-4 text-xs text-muted-foreground">
                        <span>{run.records_processed} records</span>
                        {run.kind === "shadow_capture" ? (
                          <span>{String((run as RunRead).records_processed)} shadow captures</span>
                        ) : run.kind === "settlement" ? (
                          <>
                            <span>{settlementUpdateCount(run.summary_counts)} updates</span>
                            <span>{run.summary_counts.prediction_settlement_updated} singles</span>
                            <span>{run.summary_counts.parlay_prediction_settlement_updated} parlays</span>
                          </>
                        ) : (
                          <>
                            <span>{run.summary_counts.supported_markets_kept} markets</span>
                            <span>{run.summary_counts.recommendations_emitted} recs</span>
                          </>
                        )}
                        {run.kind === "prop_refresh" && (
                          <span>{run.summary_counts.prop_subjects_warmed} props warmed</span>
                        )}
                        {run.summary_counts.predictions_captured > 0 && (
                          <span>{run.summary_counts.predictions_captured} preds</span>
                        )}
                      </div>
                    </button>
                  ))}
            </div>
          </ScrollArea>
        </div>
      </section>

      <section className="cosmos-panel min-h-0 overflow-hidden">
        <div className="cosmos-panel-head">
          <div className="cosmos-panel-head-text">
            <h2 className="cosmos-panel-title">
              {detail ? `${runKindLabel(detail.kind)} #${detail.id}` : "Run Detail"}
            </h2>
            {detail && (
              <p className="cosmos-panel-desc">
                {fmtDatetime(detail.started_at)} · {detail.records_processed} processed
              </p>
            )}
          </div>
        </div>
        <div className="cosmos-panel-body min-h-0">
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
  );
}
