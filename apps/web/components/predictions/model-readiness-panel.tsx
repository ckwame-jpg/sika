"use client";

import { useEffect, useState } from "react";
import useSWR, { mutate } from "swr";
import { AlertTriangle, CheckCircle2, Cpu, FlaskConical, Power, RefreshCw, Rocket, ShieldCheck } from "lucide-react";
import { fetchHealth, fetchModelReadinessDetail, fetchModelReadinessSummary, keys, updateModelReadinessSettings } from "@/lib/api";
import type {
  HealthResponse,
  ModelFamilyReadinessRead,
  ModelReadinessSummaryRead,
  RefreshJobRead,
  ReadinessStatus,
  RuntimeHealthStatus,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, fmtContractPnl, fmtDatetime, fmtEdge, fmtPercent } from "@/lib/utils";

type RuntimeMode = ModelReadinessSummaryRead["ml_serving_mode"];

const STUDY_LADDER = [
  "insufficient history",
  "shadow not started",
  "shadowing",
  "history accumulating",
  "ready for review",
  "serving",
] as const;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function numberFromRecord(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function promotionMetric(family: ModelFamilyReadinessRead, key: string): number | null {
  const metrics = asRecord(asRecord(family.runtime.promotion_metrics).metrics);
  const value = metrics[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function progressRatio(value: number, target: number): number {
  if (target <= 0) return 1;
  return Math.min(Math.max(value / target, 0), 1);
}

function rolloutAction(summary: ModelReadinessSummaryRead, family: ModelFamilyReadinessRead): string {
  if (family.study_track !== "active") return "Heuristic lane";
  if (family.runtime.effective_mode === "ml") return "Serving ML";
  if (!summary.shadow_enabled) return "Enable shadow mode";
  if (family.shadow_predictions === 0) return "Waiting for shadow capture";
  if (family.readiness_status === "shadowing") return "Collecting shadow coverage";
  if (family.readiness_status === "history_accumulating") return "Accumulating settled history";
  if (family.readiness_status === "ready_for_review" && !summary.auto_promotion_enabled) return "Ready to arm auto-promotion";
  if (summary.auto_promotion_enabled) return "Auto-promotion armed";
  return family.readiness_status.replaceAll("_", " ");
}

function readinessPillClass(status: ReadinessStatus): string {
  if (status === "serving" || status === "ready_for_review") return "settled";
  if (
    status === "shadowing" ||
    status === "shadow_not_started" ||
    status === "insufficient_history" ||
    status === "history_accumulating"
  ) return "pending";
  return "";
}

function runtimePillClass(status: RuntimeHealthStatus): string {
  if (status === "healthy") return "settled";
  if (status === "degraded") return "pending";
  return "lost";
}

function studyTrackPillClass(studyTrack: ModelFamilyReadinessRead["study_track"]): string {
  return studyTrack === "active" ? "pending" : "";
}

function studyTrackLabel(studyTrack: ModelFamilyReadinessRead["study_track"]): string {
  return studyTrack === "active" ? "active study" : "heuristic lane";
}

function shadowBacklogCount(family: ModelFamilyReadinessRead): number {
  return family.shadow_backlog_predictions + family.shadow_backlog_parlays;
}

function shadowBacklogLabel(family: ModelFamilyReadinessRead): string {
  const backlog = shadowBacklogCount(family);
  if (backlog <= 0) return "backlog clear";
  const unit = family.scope === "parlay" ? "parlays" : "predictions";
  return `${backlog} ${unit} pending`;
}

function defaultFamilyKey(families: ModelFamilyReadinessRead[]): string {
  const activeWithVolume = families.find(
    (family) =>
      family.study_track === "active" &&
      (family.total_predictions > 0 ||
        family.settled_predictions > 0 ||
        family.pending_predictions > 0 ||
        family.shadow_predictions > 0 ||
        shadowBacklogCount(family) > 0),
  );
  return activeWithVolume?.family_key ?? families.find((family) => family.study_track === "active")?.family_key ?? families[0]?.family_key ?? "";
}

function settlementJobMetric(job: RefreshJobRead | null | undefined, summaryKey: string, metricKey: string): number {
  const details = asRecord(job?.details);
  return numberFromRecord(asRecord(details[summaryKey])[metricKey]);
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
        <span className={cn("outcome-pill", readinessPillClass(family.readiness_status))}>
          {family.readiness_status.replaceAll("_", " ")}
        </span>
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        <span className={cn("outcome-pill", studyTrackPillClass(family.study_track))}>
          {studyTrackLabel(family.study_track)}
        </span>
        <span className={cn("outcome-pill", runtimePillClass(family.runtime.runtime_health))}>
          {family.runtime.runtime_health}
        </span>
        <span className="outcome-pill">
          {family.runtime.desired_mode}{" -> "}{family.runtime.effective_mode}
        </span>
        {family.runtime.fallback_active ? (
          <span className="outcome-pill lost">fallback active</span>
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
    <div className="overflow-hidden rounded-[10px] border border-white/[0.06] bg-white/[0.03]">
      <div className="border-b border-white/[0.06] px-3 py-2">
        <p className="stats-tile-label">{title}</p>
      </div>
      <div className="divide-y divide-white/[0.06]">
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

function ProgressStep({
  label,
  value,
  target,
  detail,
  complete,
}: {
  label: string;
  value: number;
  target: number;
  detail: string;
  complete?: boolean;
}) {
  const ratio = complete ? 1 : progressRatio(value, target);
  return (
    <div className="stats-tile">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="stats-tile-label">{label}</p>
          <p className="mt-1 text-xs text-muted-foreground">{detail}</p>
        </div>
        <span className={cn("outcome-pill", ratio >= 1 ? "settled" : "pending")}>
          {ratio >= 1 ? "done" : `${Math.round(ratio * 100)}%`}
        </span>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-white/[0.08]">
        <div className="h-full rounded-full bg-positive" style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}

export function ModelReadinessPanel() {
  const [refreshing, setRefreshing] = useState(false);
  const [savingMode, setSavingMode] = useState<RuntimeMode | null>(null);
  const [modeError, setModeError] = useState<string | null>(null);
  const { data: summary, isLoading, error } = useSWR<ModelReadinessSummaryRead>(
    keys.modelReadinessSummary,
    fetchModelReadinessSummary,
    {
      refreshInterval: 0,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
    },
  );
  const { data: health } = useSWR<HealthResponse>(keys.health, fetchHealth, {
    refreshInterval: 30_000,
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
  });
  const [selectedFamilyKey, setSelectedFamilyKey] = useState<string>("");

  useEffect(() => {
    if (!summary?.families?.length) {
      return;
    }
    const selectedStillExists = summary.families.some((family) => family.family_key === selectedFamilyKey);
    if (!selectedFamilyKey || !selectedStillExists) {
      setSelectedFamilyKey(defaultFamilyKey(summary.families));
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
        mutate(keys.health),
      ]);
    } finally {
      setRefreshing(false);
    }
  }

  async function handleModeChange(mode: RuntimeMode) {
    setSavingMode(mode);
    setModeError(null);
    try {
      const nextSummary = await updateModelReadinessSettings({
        ml_serving_mode: mode,
        enqueue_shadow_backfill: mode !== "heuristic",
      });
      await mutate(keys.modelReadinessSummary, nextSummary, { revalidate: false });
      if (selectedFamilyKey) {
        await mutate(keys.modelReadinessDetail(selectedFamilyKey));
      }
    } catch (updateError) {
      setModeError(updateError instanceof Error ? updateError.message : "Failed to update model mode.");
    } finally {
      setSavingMode(null);
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
      <section className="cosmos-panel">
        <div className="cosmos-panel-body flex flex-wrap items-center justify-between gap-3 text-sm text-negative">
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
        </div>
      </section>
    );
  }

  const selected = detail ?? summaryFallback;
  const selectedPromotionSamples = selected ? promotionMetric(selected, "sample_count") ?? selected.shadow_predictions : 0;
  const selectedShadowBrier = selected ? promotionMetric(selected, "shadow_brier") : null;
  const selectedHeuristicBrier = selected ? promotionMetric(selected, "heuristic_brier") : null;
  const selectedShadowTopDecileRoi = selected ? promotionMetric(selected, "shadow_top_decile_roi") : null;
  const selectedHasNoRecommendationRows = selected
    ? selected.total_predictions === 0 && selected.settled_predictions === 0 && selected.pending_predictions === 0
    : false;
  const settlementFamilies = summary.families.filter(
    (family) => family.study_track === "active" && family.pending_predictions > 0 && family.settled_predictions === 0,
  );
  const settlementPendingCount = settlementFamilies.reduce((total, family) => total + family.pending_predictions, 0);
  const settlementJob = health?.active_settlement_job ?? health?.latest_settlement_job ?? null;
  const settlementProcessed = settlementJob
    ? numberFromRecord(asRecord(settlementJob.details).processed_so_far)
    : 0;
  const settlementUpdated =
    settlementJobMetric(settlementJob, "single_settlement_summary", "updated") +
    settlementJobMetric(settlementJob, "parlay_settlement_summary", "updated");
  const settlementJobStatus = health?.active_settlement_job?.status ?? settlementJob?.status ?? "idle";

  return (
    <div className="flex flex-col gap-4">
      <section className="cosmos-panel overflow-hidden">
        <div className="cosmos-panel-head">
          <div className="cosmos-panel-head-text">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <FlaskConical size={16} className="text-muted-foreground" />
                <h2 className="cosmos-panel-title">Model Readiness</h2>
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
            <div className="mt-2 flex flex-col gap-2 text-sm text-muted-foreground">
              <p>Study ladder: {STUDY_LADDER.join(" -> ")}.</p>
              <p>
                Runtime stays separate: <span className="font-mono">desired -&gt; effective</span> shows what is configured versus what is actually serving live. Only families with effective mode <span className="font-mono">ml</span> are serving calibrated probabilities.
              </p>
            </div>
          </div>
        </div>
        <div className="cosmos-panel-body">
          {modeError ? (
            <div className="mb-4 flex items-center gap-2 rounded-lg border border-negative/30 bg-negative/10 px-3 py-2 text-sm text-negative">
              <AlertTriangle size={14} />
              {modeError}
            </div>
          ) : null}
          {settlementFamilies.length > 0 ? (
            <div className="stats-tile mb-4" data-testid="model-settlement-status">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="stats-tile-label">Settlement Worker</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {settlementPendingCount} pending predictions across {settlementFamilies.length} active families.
                  </p>
                </div>
                <span className={cn("outcome-pill", settlementJobStatus === "failed" ? "lost" : settlementJobStatus === "completed" ? "settled" : "pending")}>
                  {settlementJobStatus}
                </span>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                {settlementJob
                  ? `latest job processed ${settlementProcessed} rows and updated ${settlementUpdated}.`
                  : "waiting for the next automatic settlement job."}
              </p>
            </div>
          ) : null}
          <div className="mb-4 grid gap-3 md:grid-cols-3">
            <div className="stats-tile">
              <p className="stats-tile-label">Global Mode</p>
              <p className="stats-tile-value font-mono">{summary.ml_serving_mode}</p>
              {summary.ml_serving_mode !== "heuristic" ? (
                <Button
                  variant="ghost"
                  size="sm"
                  className="mt-3 gap-2"
                  onClick={() => void handleModeChange("heuristic")}
                  disabled={savingMode !== null}
                >
                  <Power size={13} />
                  Pause ML
                </Button>
              ) : null}
            </div>
            <div className="stats-tile">
              <p className="stats-tile-label">Shadow Capture</p>
              <p className="stats-tile-value">{summary.shadow_enabled ? "enabled" : "off"}</p>
              <Button
                variant={summary.shadow_enabled ? "secondary" : "primary"}
                size="sm"
                className="mt-3 gap-2"
                onClick={() => void handleModeChange("shadow")}
                disabled={summary.ml_serving_mode === "shadow" || savingMode !== null}
              >
                <ShieldCheck size={13} />
                {summary.shadow_enabled ? "Shadow On" : "Enable Shadow"}
              </Button>
            </div>
            <div className="stats-tile">
              <p className="stats-tile-label">Auto Promotion</p>
              <p className="stats-tile-value">{summary.auto_promotion_enabled ? "armed" : "manual"}</p>
              <Button
                variant={summary.auto_promotion_enabled ? "secondary" : "primary"}
                size="sm"
                className="mt-3 gap-2"
                onClick={() => void handleModeChange(summary.auto_promotion_enabled ? "shadow" : "ml")}
                disabled={savingMode !== null}
              >
                <Rocket size={13} />
                {summary.auto_promotion_enabled ? "Manual Only" : "Arm Auto"}
              </Button>
            </div>
          </div>
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
        </div>
      </section>

      {selected ? (
        <section className="cosmos-panel">
          <div className="cosmos-panel-head">
            <div className="cosmos-panel-head-text">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="cosmos-panel-title">{selected.label}</h2>
                <span className={cn("outcome-pill", studyTrackPillClass(selected.study_track))}>
                  {studyTrackLabel(selected.study_track)}
                </span>
                <span className={cn("outcome-pill", readinessPillClass(selected.readiness_status))}>
                  {selected.readiness_status.replaceAll("_", " ")}
                </span>
                <span className={cn("outcome-pill", runtimePillClass(selected.runtime.runtime_health))}>
                  {selected.runtime.runtime_health}
                </span>
                {selected.runtime.fallback_active ? (
                  <span className="outcome-pill lost">ML requested, heuristic serving</span>
                ) : null}
              </div>
              <p className="mt-2 text-sm text-muted-foreground">{selected.why_not_ready}</p>
              <p className="mt-2 text-sm font-medium text-foreground">{rolloutAction(summary, selected)}</p>
            </div>
          </div>
          <div className="cosmos-panel-body flex flex-col gap-4">
            {selectedHasNoRecommendationRows ? (
              <div className="stats-tile" data-testid="model-family-empty-volume">
                <p className="stats-tile-label">Prediction Volume</p>
                <p className="mt-1 text-sm text-muted-foreground">
                  No recommendation predictions have been captured for this family yet. Settlement progress appears on active families with captured rows.
                </p>
              </div>
            ) : null}
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
              <ProgressStep
                label="History"
                value={selected.settled_predictions}
                target={summary.min_settled_for_review}
                detail={`${selected.settled_predictions}/${summary.min_settled_for_review} settled`}
              />
              <ProgressStep
                label="Shadow Coverage"
                value={selected.shadow_coverage_ratio}
                target={summary.min_shadow_coverage}
                detail={`${fmtPercent(selected.shadow_coverage_ratio)} / ${fmtPercent(summary.min_shadow_coverage)}`}
              />
              <ProgressStep
                label="Promotion Samples"
                value={selectedPromotionSamples}
                target={summary.min_promotion_shadow_samples}
                detail={`${selectedPromotionSamples}/${summary.min_promotion_shadow_samples} paired samples`}
              />
              <ProgressStep
                label="Daily Stability"
                value={selected.runtime.promotion_stability_days}
                target={summary.promotion_stability_days_required}
                detail={`${selected.runtime.promotion_stability_days}/${summary.promotion_stability_days_required} passing days`}
              />
              <ProgressStep
                label="Serving"
                value={selected.runtime.effective_mode === "ml" ? 1 : 0}
                target={1}
                detail={selected.runtime.effective_mode === "ml" ? "ML live" : "not live"}
                complete={selected.runtime.effective_mode === "ml"}
              />
            </div>

            {(selectedHeuristicBrier != null || selectedShadowBrier != null || selectedShadowTopDecileRoi != null) ? (
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="stats-tile">
                  <p className="stats-tile-label" title="Worst per-week walk-forward fold; bug #20 promotion gate metric.">Heuristic Brier (worst fold)</p>
                  <p className="stats-tile-value font-mono">{selectedHeuristicBrier?.toFixed(4) ?? "—"}</p>
                </div>
                <div className="stats-tile">
                  <p className="stats-tile-label" title="Worst per-week walk-forward fold; bug #20 promotion gate metric.">Shadow Brier (worst fold)</p>
                  <p className="stats-tile-value font-mono">{selectedShadowBrier?.toFixed(4) ?? "—"}</p>
                </div>
                <div className="stats-tile">
                  <p className="stats-tile-label">Shadow Top-Decile ROI</p>
                  <p className="stats-tile-value font-mono">{selectedShadowTopDecileRoi != null ? fmtContractPnl(selectedShadowTopDecileRoi) : "—"}</p>
                </div>
              </div>
            ) : null}

            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-7">
              <div className="stats-tile">
                <p className="stats-tile-label">Runtime</p>
                <p className="stats-tile-value font-mono">
                  {selected.runtime.desired_mode}{" -> "}{selected.runtime.effective_mode}
                </p>
                <p className="text-xs text-muted-foreground">Configured versus actually serving</p>
              </div>
              <div className="stats-tile">
                <p className="stats-tile-label">Settled Recs</p>
                <p className="stats-tile-value font-mono">{selected.settled_predictions}</p>
              </div>
              <div className="stats-tile">
                <p className="stats-tile-label">Coverage</p>
                <p className="stats-tile-value font-mono">{selected.coverage_predictions}</p>
                <p className="text-xs text-muted-foreground">{selected.coverage_settled_predictions} settled daily samples</p>
              </div>
              <div className="stats-tile">
                <p className="stats-tile-label">Shadow</p>
                <p className="stats-tile-value font-mono">{selected.shadow_predictions}</p>
                <p className="text-xs text-muted-foreground">
                  {fmtPercent(selected.shadow_coverage_ratio)} coverage · {shadowBacklogLabel(selected)}
                </p>
                <p className="text-xs text-muted-foreground">
                  {selected.last_shadow_capture_at ? `Last capture ${fmtDatetime(selected.last_shadow_capture_at)}` : "No shadow capture recorded yet"}
                </p>
              </div>
              <div className="stats-tile">
                <p className="stats-tile-label">Avg Edge</p>
                <p className="stats-tile-value font-mono">
                  {selected.average_edge != null ? fmtEdge(selected.average_edge) : "—"}
                </p>
              </div>
              <div className="stats-tile">
                <p className="stats-tile-label">Avg Confidence</p>
                <p className="stats-tile-value font-mono">{fmtPercent(selected.average_confidence)}</p>
              </div>
              <div className="stats-tile">
                <p className="stats-tile-label">Avg PnL</p>
                <p className={cn(
                  "stats-tile-value font-mono",
                  selected.average_realized_pnl != null && selected.average_realized_pnl < 0 ? "text-negative" : "text-positive",
                )}>
                  {fmtContractPnl(selected.average_realized_pnl)}
                </p>
              </div>
            </div>

            <div className="stats-tile">
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
              <div className="stats-tile">
                <p className="stats-tile-label">Feature Coverage</p>
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
              <div className="stats-tile">
                <p className="stats-tile-label">Missing Context</p>
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
              <div className="stats-tile">
                <p className="stats-tile-label">Top Failure Reasons</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {Object.entries(selected.top_failure_reasons).length === 0 ? (
                    <span className="text-sm text-muted-foreground">No settled-loss diagnostics yet.</span>
                  ) : Object.entries(selected.top_failure_reasons).map(([key, value]) => (
                    <span key={key} className="outcome-pill">
                      {key.replaceAll("_", " ")} · {value}
                    </span>
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
          </div>
        </section>
      ) : null}
    </div>
  );
}
