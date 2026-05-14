import type {
  CalibrationBucketRead,
  ModelFamilyReadinessRead,
  ModelFamilyRuntimeHealthRead,
  ModelReadinessSummaryRead,
  ReadinessBucketRead,
} from "@/lib/types";

const emptyBucket = (label: string): ReadinessBucketRead => ({
  label,
  total_count: 0,
  won_count: 0,
  lost_count: 0,
  push_count: 0,
  cancelled_count: 0,
  win_rate: null,
  average_realized_pnl: null,
});

const emptyCalibrationBucket = (label: string): CalibrationBucketRead => ({
  label,
  settled_count: 0,
  avg_predicted: null,
  actual_yes_rate: null,
  miscalibration: null,
});

function runtime(overrides: Partial<ModelFamilyRuntimeHealthRead>): ModelFamilyRuntimeHealthRead {
  return {
    family_key: overrides.family_key ?? "nba_singles",
    desired_mode: overrides.desired_mode ?? "heuristic",
    effective_mode: overrides.effective_mode ?? "heuristic",
    runtime_health: overrides.runtime_health ?? "healthy",
    fallback_active: overrides.fallback_active ?? false,
    consecutive_failures: overrides.consecutive_failures ?? 0,
    last_check_at: overrides.last_check_at ?? null,
    last_success_at: overrides.last_success_at ?? null,
    last_error: overrides.last_error ?? null,
    last_error_at: overrides.last_error_at ?? null,
    artifact_path: overrides.artifact_path ?? null,
    model_name: overrides.model_name ?? null,
    model_version: overrides.model_version ?? null,
    calibration_version: overrides.calibration_version ?? null,
    feature_set_version: overrides.feature_set_version ?? null,
    model_metadata: overrides.model_metadata ?? {},
    promotion_mode: overrides.promotion_mode ?? null,
    promotion_stability_days: overrides.promotion_stability_days ?? 0,
    promotion_baseline_brier: overrides.promotion_baseline_brier ?? null,
    promotion_metrics: overrides.promotion_metrics ?? {},
    promotion_updated_at: overrides.promotion_updated_at ?? null,
  };
}

function family(overrides: Partial<ModelFamilyReadinessRead>): ModelFamilyReadinessRead {
  return {
    family_key: overrides.family_key ?? "nba_singles",
    label: overrides.label ?? "NBA singles",
    scope: overrides.scope ?? "single",
    sport_scope: overrides.sport_scope ?? "NBA",
    leg_count: overrides.leg_count ?? null,
    study_track: overrides.study_track ?? "active",
    readiness_status: overrides.readiness_status ?? "shadow_not_started",
    why_not_ready: overrides.why_not_ready ?? "Shadow has not started yet.",
    runtime: overrides.runtime ?? runtime({ family_key: overrides.family_key ?? "nba_singles" }),
    total_predictions: overrides.total_predictions ?? 50,
    settled_predictions: overrides.settled_predictions ?? 45,
    pending_predictions: overrides.pending_predictions ?? 5,
    coverage_predictions: overrides.coverage_predictions ?? 12,
    coverage_settled_predictions: overrides.coverage_settled_predictions ?? 8,
    coverage_pending_predictions: overrides.coverage_pending_predictions ?? 4,
    shadow_predictions: overrides.shadow_predictions ?? 0,
    shadow_coverage_ratio: overrides.shadow_coverage_ratio ?? 0,
    shadow_backlog_predictions: overrides.shadow_backlog_predictions ?? 12,
    shadow_backlog_parlays: overrides.shadow_backlog_parlays ?? 0,
    last_shadow_capture_at: overrides.last_shadow_capture_at ?? null,
    won_predictions: overrides.won_predictions ?? 24,
    lost_predictions: overrides.lost_predictions ?? 20,
    push_predictions: overrides.push_predictions ?? 1,
    cancelled_predictions: overrides.cancelled_predictions ?? 0,
    average_edge: overrides.average_edge ?? 0.11,
    average_confidence: overrides.average_confidence ?? 0.72,
    average_realized_pnl: overrides.average_realized_pnl ?? 0.06,
    average_clv: overrides.average_clv ?? 0.02,
    last_settled_at: overrides.last_settled_at ?? "2026-04-07T18:00:00Z",
    confidence_buckets: overrides.confidence_buckets ?? [emptyBucket("0-20%"), emptyBucket("20-40%")],
    edge_buckets: overrides.edge_buckets ?? [emptyBucket("<0"), emptyBucket("0-5%")],
    calibration_buckets:
      overrides.calibration_buckets ?? [
        emptyCalibrationBucket("0-10%"),
        emptyCalibrationBucket("10-20%"),
      ],
    feature_coverage_rates: overrides.feature_coverage_rates ?? {},
    missing_context_rates: overrides.missing_context_rates ?? {},
    top_failure_reasons: overrides.top_failure_reasons ?? {},
    last_validation_failure: overrides.last_validation_failure ?? null,
    last_fallback_event_at: overrides.last_fallback_event_at ?? null,
  };
}

export const activeStudyFamilyFixture = family({
  family_key: "nba_props",
  label: "NBA props",
  study_track: "active",
  readiness_status: "shadow_not_started",
  why_not_ready: "This family has enough settled history and is shadow-eligible, but no shadow samples have been recorded yet.",
  runtime: runtime({
    family_key: "nba_props",
    desired_mode: "heuristic",
    effective_mode: "heuristic",
    runtime_health: "healthy",
  }),
});

export const heuristicLaneFamilyFixture = family({
  family_key: "nba_parlay_3leg",
  label: "NBA 3-leg parlays",
  scope: "parlay",
  sport_scope: "NBA",
  leg_count: 3,
  study_track: "heuristic_only",
  readiness_status: "heuristic_only",
  why_not_ready: "This family is not in the active ML study track and stays on the heuristic path.",
  runtime: runtime({
    family_key: "nba_parlay_3leg",
    desired_mode: "heuristic",
    effective_mode: "heuristic",
    runtime_health: "healthy",
  }),
  shadow_backlog_parlays: 0,
  shadow_backlog_predictions: 0,
});

export const modelReadinessSummaryFixture: ModelReadinessSummaryRead = {
  generated_at: "2026-04-07T18:00:00Z",
  ml_serving_mode: "shadow",
  shadow_enabled: true,
  auto_promotion_enabled: false,
  min_settled_for_review: 40,
  min_settled_for_promotion_review: 200,
  min_shadow_coverage: 0.75,
  min_promotion_shadow_samples: 150,
  promotion_stability_days_required: 3,
  pick_history_default_n: 5,
  families: [activeStudyFamilyFixture, heuristicLaneFamilyFixture],
  narrator_enabled: false,
};
