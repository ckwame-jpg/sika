"use client";

import { useEffect, useState } from "react";
import useSWR, { mutate } from "swr";
import { AlertTriangle, CheckCircle2, Cpu, FlaskConical, RefreshCw } from "lucide-react";
import { fetchModelReadinessDetail, fetchModelReadinessSummary, keys } from "@/lib/api";
import type {
  ModelFamilyReadinessRead,
  ModelReadinessSummaryRead,
  ReadinessStatus,
  RuntimeHealthStatus,
} from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, fmtContractPnl, fmtEdge, fmtPercent } from "@/lib/utils";

function readinessVariant(status: ReadinessStatus): "positive" | "warning" | "negative" | "default" {
  if (status === "serving" || status === "ready_for_review") return "positive";
  if (status === "shadowing" || status === "shadow_not_started" || status === "insufficient_history") return "warning";
  return "default";
}

function runtimeVariant(status: RuntimeHealthStatus): "positive" | "warning" | "negative" | "default" {
  if (status === "healthy") return "positive";
  if (status === "degraded") return "warning";
  return "negative";
}

function FamilyCard({
  family,
  selected,
  onSelect,
}: {
  family: ModelFamilyReadinessRead;
  selected: boolean;
  onSelect: (familyKey: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(family.family_key)}
      className={cn(
        "rounded-xl border p-3 text-left transition-colors",
        selected ? "border-foreground/20 bg-surface-hover" : "border-border bg-surface",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-foreground">{family.label}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {family.settled_predictions} settled · {family.shadow_predictions} shadow · {family.coverage_predictions} coverage
          </p>
        </div>
        <Badge variant={readinessVariant(family.readiness_status)}>
          {family.readiness_status.replaceAll("_", " ")}
        </Badge>
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        <Badge variant={runtimeVariant(family.runtime.runtime_health)}>
          {family.runtime.runtime_health}
        </Badge>
        <Badge variant="default">
          {family.runtime.desired_mode}{" -> "}{family.runtime.effective_mode}
        </Badge>
        {family.runtime.fallback_active ? (
          <Badge variant="negative">fallback active</Badge>
        ) : null}
      </div>
    </button>
  );
}

function BucketTable({
  title,
  rows,
}: {
  title: string;
  rows: ModelFamilyReadinessRead["confidence_buckets"];
}) {
  return (
    <div className="rounded-xl border border-border bg-surface">
      <div className="border-b border-border px-3 py-2">
        <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">{title}</p>
      </div>
      <div className="divide-y divide-border">
        {rows.map((row) => (
          <div key={row.label} className="grid grid-cols-[1.2fr_0.8fr_0.8fr] gap-2 px-3 py-2 text-xs">
            <span className="text-foreground">{row.label}</span>
            <span className="font-mono text-muted-foreground">{row.total_count}</span>
            <span className="font-mono text-muted-foreground">{fmtPercent(row.win_rate)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function ModelReadinessPanel() {
  const [refreshing, setRefreshing] = useState(false);
  const { data: summary, isLoading, error } = useSWR<ModelReadinessSummaryRead>(
    keys.modelReadinessSummary,
    fetchModelReadinessSummary,
    {
      refreshInterval: 0,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
    },
  );
  const [selectedFamilyKey, setSelectedFamilyKey] = useState<string>("");

  useEffect(() => {
    if (!selectedFamilyKey && summary?.families?.length) {
      setSelectedFamilyKey(summary.families[0].family_key);
    }
  }, [selectedFamilyKey, summary]);

  const summaryFallback = summary?.families.find((family) => family.family_key === selectedFamilyKey) ?? summary?.families[0] ?? null;
  const { data: detail } = useSWR<ModelFamilyReadinessRead>(
    selectedFamilyKey ? keys.modelReadinessDetail(selectedFamilyKey) : null,
    () => fetchModelReadinessDetail(selectedFamilyKey),
    {
      refreshInterval: 0,
      fallbackData: summaryFallback ?? undefined,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
    },
  );

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await Promise.all([
        mutate(keys.modelReadinessSummary),
        selectedFamilyKey ? mutate(keys.modelReadinessDetail(selectedFamilyKey)) : Promise.resolve(),
      ]);
    } finally {
      setRefreshing(false);
    }
  }

  if (isLoading) {
    return (
      <div className="grid gap-3 lg:grid-cols-3">
        {Array.from({ length: 3 }).map((_, index) => (
          <Skeleton key={index} className="h-32 w-full rounded-xl" />
        ))}
      </div>
    );
  }

  if (error || !summary) {
    return (
      <Card className="border-negative/40 bg-negative/5">
        <CardContent className="flex flex-wrap items-center justify-between gap-3 px-4 py-4 text-sm text-negative">
          <div className="flex items-center gap-3">
            <AlertTriangle size={16} />
            Failed to load model readiness.
          </div>
          <Button
            variant="secondary"
            size="sm"
            className="gap-2"
            onClick={() => void handleRefresh()}
            disabled={refreshing}
          >
            <RefreshCw size={13} className={cn(refreshing && "animate-spin")} />
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const selected = detail ?? summaryFallback;

  return (
    <div className="flex flex-col gap-4">
      <Card className="overflow-hidden">
        <CardHeader className="gap-2 border-b border-border/80 bg-surface-hover px-4 py-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <FlaskConical size={16} className="text-muted-foreground" />
              <CardTitle className="text-base">Model Readiness</CardTitle>
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="gap-2"
              onClick={() => void handleRefresh()}
              disabled={refreshing}
            >
              <RefreshCw size={13} className={cn(refreshing && "animate-spin")} />
              Refresh
            </Button>
          </div>
          <p className="text-sm text-muted-foreground">
            Heuristic confidence is heuristic reliability, not calibrated probability. Only families with effective mode <span className="font-mono">ml</span> are serving calibrated probabilities.
          </p>
        </CardHeader>
        <CardContent className="px-4 py-4">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {summary.families.map((family) => (
              <FamilyCard
                key={family.family_key}
                family={family}
                selected={selected?.family_key === family.family_key}
                onSelect={setSelectedFamilyKey}
              />
            ))}
          </div>
        </CardContent>
      </Card>

      {selected ? (
        <Card>
          <CardHeader className="gap-2 border-b border-border/80 px-4 py-4">
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle className="text-base">{selected.label}</CardTitle>
              <Badge variant={readinessVariant(selected.readiness_status)}>
                {selected.readiness_status.replaceAll("_", " ")}
              </Badge>
              <Badge variant={runtimeVariant(selected.runtime.runtime_health)}>
                {selected.runtime.runtime_health}
              </Badge>
              {selected.runtime.fallback_active ? (
                <Badge variant="negative">ML requested, heuristic serving</Badge>
              ) : null}
            </div>
            <p className="text-sm text-muted-foreground">{selected.why_not_ready}</p>
          </CardHeader>
          <CardContent className="flex flex-col gap-4 px-4 py-4">
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-7">
              <div className="rounded-xl border border-border bg-surface px-3 py-3">
                <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Serving</p>
                <p className="mt-1 font-mono text-sm text-foreground">
                  {selected.runtime.desired_mode}{" -> "}{selected.runtime.effective_mode}
                </p>
              </div>
              <div className="rounded-xl border border-border bg-surface px-3 py-3">
                <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Settled Recs</p>
                <p className="mt-1 font-mono text-sm text-foreground">{selected.settled_predictions}</p>
              </div>
              <div className="rounded-xl border border-border bg-surface px-3 py-3">
                <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Coverage</p>
                <p className="mt-1 font-mono text-sm text-foreground">{selected.coverage_predictions}</p>
                <p className="text-xs text-muted-foreground">{selected.coverage_settled_predictions} settled daily samples</p>
              </div>
              <div className="rounded-xl border border-border bg-surface px-3 py-3">
                <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Shadow</p>
                <p className="mt-1 font-mono text-sm text-foreground">{selected.shadow_predictions}</p>
                <p className="text-xs text-muted-foreground">{fmtPercent(selected.shadow_coverage_ratio)} coverage</p>
              </div>
              <div className="rounded-xl border border-border bg-surface px-3 py-3">
                <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Avg Edge</p>
                <p className="mt-1 font-mono text-sm text-foreground">
                  {selected.average_edge != null ? fmtEdge(selected.average_edge) : "—"}
                </p>
              </div>
              <div className="rounded-xl border border-border bg-surface px-3 py-3">
                <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Avg Confidence</p>
                <p className="mt-1 font-mono text-sm text-foreground">{fmtPercent(selected.average_confidence)}</p>
              </div>
              <div className="rounded-xl border border-border bg-surface px-3 py-3">
                <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Avg PnL</p>
                <p className={cn(
                  "mt-1 font-mono text-sm",
                  selected.average_realized_pnl != null && selected.average_realized_pnl < 0 ? "text-negative" : "text-positive",
                )}>
                  {fmtContractPnl(selected.average_realized_pnl)}
                </p>
              </div>
            </div>

            <div className="rounded-xl border border-border bg-surface px-4 py-4">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Cpu size={15} className="text-muted-foreground" />
                <span className="font-medium text-foreground">
                  {selected.runtime.model_name ?? "heuristic"}
                </span>
                {selected.runtime.model_version ? (
                  <span className="font-mono text-xs text-muted-foreground">{selected.runtime.model_version}</span>
                ) : null}
              </div>
              <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
                <span>runtime: {selected.runtime.runtime_health}</span>
                <span>failures: {selected.runtime.consecutive_failures}</span>
                {selected.last_validation_failure ? <span>last error: {selected.last_validation_failure}</span> : null}
              </div>
            </div>

            <div className="grid gap-4 xl:grid-cols-2">
              <BucketTable title="Confidence Buckets" rows={selected.confidence_buckets} />
              <BucketTable title="Edge Buckets" rows={selected.edge_buckets} />
            </div>

            <div className="grid gap-4 xl:grid-cols-3">
              <div className="rounded-xl border border-border bg-surface px-4 py-4">
                <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">Feature Coverage</p>
                <div className="mt-3 flex flex-col gap-2 text-sm">
                  {Object.entries(selected.feature_coverage_rates).length === 0 ? (
                    <span className="text-muted-foreground">No coverage diagnostics yet.</span>
                  ) : Object.entries(selected.feature_coverage_rates).map(([key, value]) => (
                    <div key={key} className="flex items-center justify-between gap-3">
                      <span className="text-foreground">{key.replaceAll("_", " ")}</span>
                      <span className="font-mono text-muted-foreground">{fmtPercent(value)}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="rounded-xl border border-border bg-surface px-4 py-4">
                <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">Missing Context</p>
                <div className="mt-3 flex flex-col gap-2 text-sm">
                  {Object.entries(selected.missing_context_rates).length === 0 ? (
                    <span className="text-muted-foreground">No missing-context flags recorded.</span>
                  ) : Object.entries(selected.missing_context_rates).map(([key, value]) => (
                    <div key={key} className="flex items-center justify-between gap-3">
                      <span className="text-foreground">{key.replaceAll("_", " ")}</span>
                      <span className="font-mono text-muted-foreground">{fmtPercent(value)}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="rounded-xl border border-border bg-surface px-4 py-4">
                <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">Top Failure Reasons</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {Object.entries(selected.top_failure_reasons).length === 0 ? (
                    <span className="text-sm text-muted-foreground">No settled-loss diagnostics yet.</span>
                  ) : Object.entries(selected.top_failure_reasons).map(([key, value]) => (
                    <Badge key={key} variant="default">
                      {key.replaceAll("_", " ")} · {value}
                    </Badge>
                  ))}
                </div>
              </div>
            </div>

            {selected.runtime.fallback_active ? (
              <div className="rounded-xl border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning-foreground">
                <div className="flex items-center gap-2">
                  <CheckCircle2 size={14} />
                  ML was requested for this family, but runtime fallback is currently serving the heuristic path.
                </div>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
