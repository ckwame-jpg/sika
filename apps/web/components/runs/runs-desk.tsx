"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { fetchRun, fetchRuns, keys } from "@/lib/api";
import type { RunDetailRead, RunRead } from "@/lib/types";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtDatetime } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { useHealthStatus } from "@/lib/health-status";

function statusVariant(status: string) {
  if (status === "completed") return "positive";
  if (status === "failed") return "negative";
  return "warning";
}

function countEntries(details: Record<string, unknown>, key: string) {
  const value = details[key];
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>);
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
    ? Object.entries(detail.summary_counts.prediction_outcomes ?? {})
    : [];

  return (
    <div className="grid h-full min-h-0 gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
      <Card className="min-h-0">
        <CardHeader className="flex-col items-start gap-1 border-none">
          <CardTitle>Recent Runs</CardTitle>
          <CardDescription>Refresh history, diagnostics, and emitted recommendations</CardDescription>
        </CardHeader>
        <CardContent className="min-h-0 pb-0">
          {health?.active_refresh_job && (
            <div className="mb-3 rounded-xl border border-warning/20 bg-warning/8 px-3 py-3 text-sm">
              <div className="flex items-center gap-2">
                <Badge variant="warning">{health.active_refresh_job.status}</Badge>
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
                        selectedRunId === run.id
                          ? "border-accent/30 bg-accent/8"
                          : "border-border bg-surface-hover hover:border-border-bright",
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-sm font-medium text-foreground">
                          {run.kind === "prop_refresh" ? "Maintenance Refresh" : "Refresh"} #{run.id}
                        </p>
                        <Badge variant={statusVariant(run.status)}>{run.status}</Badge>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {fmtDatetime(run.started_at)}
                      </p>
                      <div className="mt-2 flex items-center gap-4 text-xs text-muted-foreground">
                        <span>{run.records_processed} records</span>
                        <span>{run.summary_counts.supported_markets_kept} markets</span>
                        <span>{run.summary_counts.recommendations_emitted} recs</span>
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
        </CardContent>
      </Card>

      <Card className="min-h-0">
        <CardHeader className="flex-col items-start gap-1 border-none">
          <CardTitle>{detail ? `Run #${detail.id}` : "Run Detail"}</CardTitle>
          <CardDescription>
            {detail ? `${fmtDatetime(detail.started_at)} · ${detail.records_processed} processed` : "Select a run to inspect details"}
          </CardDescription>
        </CardHeader>
        <CardContent className="min-h-0">
          {detailLoading || !detail ? (
            <div className="space-y-3">
              <Skeleton className="h-16 w-full rounded-xl" />
              <Skeleton className="h-48 w-full rounded-xl" />
            </div>
          ) : (
            <div className="grid gap-4">
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                <Card className="bg-surface-hover shadow-none">
                  <CardContent className="px-3 py-3">
                    <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Markets Seen</p>
                    <p className="mt-1 font-mono text-lg text-foreground">{detail.summary_counts.total_kalshi_markets_seen}</p>
                  </CardContent>
                </Card>
                <Card className="bg-surface-hover shadow-none">
                  <CardContent className="px-3 py-3">
                    <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Supported Kept</p>
                    <p className="mt-1 font-mono text-lg text-foreground">{detail.summary_counts.supported_markets_kept}</p>
                  </CardContent>
                </Card>
                <Card className="bg-surface-hover shadow-none">
                  <CardContent className="px-3 py-3">
                    <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Mapped Props</p>
                    <p className="mt-1 font-mono text-lg text-foreground">{detail.summary_counts.mapped_prop_markets}</p>
                  </CardContent>
                </Card>
                <Card className="bg-surface-hover shadow-none">
                  <CardContent className="px-3 py-3">
                    <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                      {detail.kind === "prop_refresh" ? "Props Warmed" : "Recommendations"}
                    </p>
                    <p className="mt-1 font-mono text-lg text-foreground">
                      {detail.kind === "prop_refresh"
                        ? detail.summary_counts.prop_subjects_warmed
                        : detail.summary_counts.recommendations_emitted}
                    </p>
                  </CardContent>
                </Card>
                <Card className="bg-surface-hover shadow-none">
                  <CardContent className="px-3 py-3">
                    <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                      {detail.kind === "prop_refresh" ? "Search Cache" : "Predictions Captured"}
                    </p>
                    <p className="mt-1 font-mono text-lg text-foreground">
                      {detail.kind === "prop_refresh"
                        ? `${detail.summary_counts.player_search_cache_hits}/${detail.summary_counts.player_search_cache_misses}`
                        : detail.summary_counts.predictions_captured}
                    </p>
                  </CardContent>
                </Card>
                <Card className="bg-surface-hover shadow-none">
                  <CardContent className="px-3 py-3">
                    <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                      {detail.kind === "prop_refresh" ? "Gamelog Cache" : "Settlements Updated"}
                    </p>
                    <p className="mt-1 font-mono text-lg text-foreground">
                      {detail.kind === "prop_refresh"
                        ? `${detail.summary_counts.gamelog_cache_hits}/${detail.summary_counts.gamelog_cache_misses}`
                        : detail.summary_counts.prediction_settlement_updated}
                    </p>
                  </CardContent>
                </Card>
              </div>

              <div className="grid gap-4 xl:grid-cols-3">
                <Card className="bg-surface-hover shadow-none">
                  <CardHeader className="flex-col items-start gap-1 border-none px-3 py-3">
                    <CardTitle className="text-xs uppercase tracking-[0.14em] text-muted-foreground">
                      Sports Ingested
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-2 px-3 pt-0">
                    {Object.entries(detail.summary_counts.sports_records_ingested).map(([sport, count]) => (
                      <div key={sport} className="flex items-center justify-between rounded-lg border border-border px-3 py-2 text-sm">
                        <span className="text-foreground">{sport}</span>
                        <span className="font-mono text-muted-foreground">{count}</span>
                      </div>
                    ))}
                  </CardContent>
                </Card>

                <Card className="bg-surface-hover shadow-none">
                  <CardHeader className="flex-col items-start gap-1 border-none px-3 py-3">
                    <CardTitle className="text-xs uppercase tracking-[0.14em] text-muted-foreground">
                      Watchlist By Sport
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-2 px-3 pt-0">
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
                  </CardContent>
                </Card>

                <Card className="bg-surface-hover shadow-none">
                  <CardHeader className="flex-col items-start gap-1 border-none px-3 py-3">
                    <CardTitle className="text-xs uppercase tracking-[0.14em] text-muted-foreground">
                      Prediction Outcomes
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-2 px-3 pt-0">
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
                  </CardContent>
                </Card>
              </div>

              {fetchErrors.length > 0 && (
                <Card className="border-warning/20 bg-warning/8 shadow-none">
                  <CardHeader className="flex-col items-start gap-1 border-none px-3 py-3">
                    <CardTitle className="text-xs uppercase tracking-[0.14em] text-warning">
                      Sports Fetch Errors
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-2 px-3 pt-0">
                    {fetchErrors.map(([sport, value]) => (
                      <div key={sport} className="rounded-lg border border-warning/20 px-3 py-2 text-sm text-warning">
                        <p className="font-medium">{sport}</p>
                        <p className="mt-1 text-xs">{JSON.stringify(value)}</p>
                      </div>
                    ))}
                  </CardContent>
                </Card>
              )}

              {detail.error_message && (
                <Card className="border-negative/20 bg-negative/8 shadow-none">
                  <CardContent className="px-3 py-3 text-sm text-negative">
                    {detail.error_message}
                  </CardContent>
                </Card>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
