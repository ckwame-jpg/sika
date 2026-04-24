/* ─── Mapped directly from apps/api/app/schemas.py ─── */

export type SportKey = "NBA" | "NFL" | "MLB" | "SOCCER" | "TENNIS";

export const SPORT_LABELS: Record<SportKey, string> = {
  NBA: "NBA",
  NFL: "NFL",
  MLB: "MLB",
  SOCCER: "Soccer",
  TENNIS: "Tennis",
};

export interface HealthResponse {
  status: string;
  environment: string;
  scheduler_enabled: boolean;
  refresh_status: "idle" | "queued" | "running" | "failed";
  refresh_reason: "none" | "startup" | "interval" | "manual" | "pregame";
  last_successful_refresh_at: string | null;
  data_stale: boolean;
  refresh_error_message: string | null;
  prop_refresh_status: "idle" | "queued" | "running" | "failed";
  prop_refresh_reason: "none" | "startup" | "interval" | "manual";
  last_prop_refresh_at: string | null;
  prop_data_stale: boolean;
  prop_refresh_error_message: string | null;
  active_refresh_job: RefreshJobRead | null;
  latest_refresh_job: RefreshJobRead | null;
  active_prop_refresh_job: RefreshJobRead | null;
  latest_prop_refresh_job: RefreshJobRead | null;
}

export interface RefreshJobRead {
  id: number;
  kind: string;
  scope: string;
  reason: string;
  status: "queued" | "running" | "completed" | "failed";
  run_id: number | null;
  error_message: string | null;
  details: Record<string, unknown>;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface SportRead {
  key: string;
  name: string;
}

export interface EventParticipantRead {
  participant_id: number;
  display_name: string;
  role: string;
  is_home: boolean;
  score: number | null;
  result: string | null;
}

export interface EventRead {
  id: number;
  external_id: string;
  sport_key: string;
  name: string;
  status: string;
  starts_at: string;
  completed_at: string | null;
  participants: EventParticipantRead[];
  raw_data: Record<string, unknown>;
}

export interface RecommendationRead {
  id: number;
  ticker: string;
  sport_key: string | null;
  market_title: string;
  event_name: string;
  starts_at: string | null;
  market_family: string | null;
  market_kind: string | null;
  stat_key: string | null;
  threshold: number | null;
  direction: string | null;
  subject_name: string | null;
  subject_team: string | null;
  side: string;
  action: string;
  suggested_price: number;
  edge: number;
  confidence: number;
  selected_side_probability: number | null;
  source_type: string | null;
  source_market_ticker: string | null;
  source_market_title: string | null;
  display_market_title: string | null;
  source_badge_label: string | null;
  context_coverage_score: number | null;
  quality_tier: string | null;
  model_name: string | null;
  model_version: string | null;
  calibration_version: string | null;
  feature_set_version: string | null;
  invalidation: string;
  rationale: string;
  captured_at: string;
}

export interface ParlayRecommendationLegRead {
  leg_index: number;
  ticker: string;
  sport_key: string | null;
  event_name: string | null;
  market_title: string;
  market_family: string | null;
  market_kind: string | null;
  stat_key: string | null;
  threshold: number | null;
  subject_name: string | null;
  subject_team: string | null;
  side: string;
  action: string;
  suggested_price: number;
  fair_yes_price: number | null;
  fair_no_price: number | null;
  edge: number;
  confidence: number;
}

export interface ParlayRecommendationRead {
  id: number;
  run_id: number | null;
  leg_count: number;
  sport_scope: string;
  participating_sports: string[];
  status: string;
  combined_market_price: number;
  combined_model_probability: number;
  american_odds: string;
  edge: number;
  confidence: number;
  model_name: string | null;
  model_version: string | null;
  calibration_version: string | null;
  feature_set_version: string | null;
  invalidation: string;
  rationale: string;
  captured_at: string;
  legs: ParlayRecommendationLegRead[];
}

export interface MarketSnapshotRead {
  captured_at: string;
  yes_bid: number | null;
  yes_ask: number | null;
  no_bid: number | null;
  no_ask: number | null;
  last_price: number | null;
  volume: number | null;
  open_interest: number | null;
}

export interface SignalSnapshotRead {
  captured_at: string;
  model_name: string;
  model_version: string | null;
  calibration_version: string | null;
  feature_set_version: string | null;
  confidence: number;
  fair_yes_price: number;
  fair_no_price: number;
  edge: number;
  reasons: string[];
  features: Record<string, unknown>;
  scoring_diagnostics: Record<string, unknown>;
}

export interface MarketDetailRead {
  ticker: string;
  title: string;
  subtitle: string | null;
  sport_key: string | null;
  market_family: string | null;
  market_kind: string | null;
  stat_key: string | null;
  threshold: number | null;
  direction: string | null;
  subject_name: string | null;
  subject_team: string | null;
  status: string;
  close_time: string | null;
  event: EventRead | null;
  latest_snapshot: MarketSnapshotRead | null;
  latest_signal: SignalSnapshotRead | null;
  recommendations: RecommendationRead[];
}

export interface MarketListRead {
  ticker: string;
  title: string;
  subtitle: string | null;
  sport_key: string | null;
  market_family: string | null;
  market_kind: string | null;
  stat_key: string | null;
  threshold: number | null;
  direction: string | null;
  subject_name: string | null;
  subject_team: string | null;
  status: string;
  close_time: string | null;
  event_name: string | null;
  latest_snapshot: MarketSnapshotRead | null;
  latest_recommendation: RecommendationRead | null;
}

export interface WatchlistCoverageRowRead {
  ticker: string;
  event_id: number | null;
  event_name: string | null;
  event_status: string | null;
  starts_at: string | null;
  sport_key: string | null;
  market_title: string;
  market_family: string | null;
  market_kind: string | null;
  stat_key: string | null;
  threshold: number | null;
  direction: string | null;
  subject_name: string | null;
  subject_team: string | null;
  coverage_status: "recommendation" | "prediction" | "market";
  prop_context_stale: boolean;
  latest_snapshot: MarketSnapshotRead | null;
  latest_recommendation: RecommendationRead | null;
  latest_prediction: PredictionRead | null;
}

export interface SportAvailabilityRead {
  sport_key: string;
  availability_mode: "live" | "research_only";
  events_count: number;
  recommendations_count: number;
  last_refresh_at: string | null;
}

export interface TradeDeskGameLine {
  ticker: string;
  market_title: string;
  display_label: string;
  sport_key: string | null;
  market_kind: string;
  selected_side: string;
  projected_side_label: string | null;
  selected_side_probability: number | null;
  entry_price: number | null;
  edge: number;
  confidence: number;
  kalshi_url: string | null;
}

export interface TradeDeskThreshold {
  ticker: string;
  threshold: number;
  probability_yes: number;
  selected_side: string;
  selected_side_probability: number | null;
  entry_price: number | null;
  edge: number;
  confidence: number;
  is_best: boolean;
  kalshi_url: string | null;
}

export interface TradeDeskStatGroup {
  stat_key: string;
  thresholds: TradeDeskThreshold[];
}

export interface TradeDeskPlayerProp {
  subject_name: string;
  subject_team: string | null;
  stat_groups: TradeDeskStatGroup[];
  best_edge: number;
  best_win_prob: number | null;
}

export interface TradeDeskEvent {
  event_id: number;
  event_name: string;
  event_status: string;
  starts_at: string | null;
  sport_key: string;
  game_lines: TradeDeskGameLine[];
  player_props: TradeDeskPlayerProp[];
}

export interface TradeDeskResponse {
  events: TradeDeskEvent[];
  research_sports: SportAvailabilityRead[];
  generated_at: string | null;
  freshness_status: "fresh" | "stale" | "degraded" | "empty";
  event_count: number;
  candidate_market_count: number;
  scored_market_count: number;
  recommendation_count: number;
  coverage_prediction_count: number;
  blocking_reason: string | null;
  generated_from_run_id: number | null;
}

export interface MarketHistoryPointRead {
  timestamp: string;
  yes_bid: number | null;
  yes_ask: number | null;
  no_bid: number | null;
  no_ask: number | null;
  last_price: number | null;
  mean_price: number | null;
  volume: number | null;
  source: string;
}

export interface MarketHistoryRead {
  ticker: string;
  range: string;
  points: MarketHistoryPointRead[];
}

export interface RunSummaryCounts {
  sports_records_ingested: Record<string, number>;
  total_kalshi_markets_seen: number;
  supported_markets_kept: number;
  supported_nba_props_seen: number;
  supported_mlb_props_seen: number;
  mapped_markets: number;
  mapped_prop_markets: number;
  current_slate_event_count: number;
  current_slate_candidate_market_count: number;
  current_slate_loaded_candidate_market_count: number;
  current_slate_filtered_candidate_market_count: number;
  current_slate_candidate_filter_reason_counts: Record<string, number>;
  current_slate_scored_market_count: number;
  current_slate_coverage_prediction_count: number;
  current_slate_blocking_reason: string | null;
  scorer_outcome_counts: Record<string, number>;
  recommendations_emitted: number;
  predictions_captured: number;
  parlay_recommendations_emitted: number;
  parlay_predictions_captured: number;
  prediction_settlement_updated: number;
  parlay_prediction_settlement_updated: number;
  prediction_outcomes: Record<string, number>;
  parlay_prediction_outcomes: Record<string, number>;
  unsupported_prop_category_counts: Record<string, number>;
  heuristic_longshots_suppressed: number;
  inverse_winner_duplicates_collapsed: number;
  combo_prop_candidates_emitted: number;
  combo_prop_candidates_suppressed: number;
  critical_context_suppressed: number;
  quality_tier_counts: Record<string, number>;
  prop_subjects_warmed: number;
  player_search_cache_hits: number;
  player_search_cache_misses: number;
  gamelog_cache_hits: number;
  gamelog_cache_misses: number;
  stale_gamelog_fallbacks: number;
  combo_prop_legs_discovered: number;
  combo_prop_legs_refreshed: number;
  watchlist_counts_by_sport: Record<string, number>;
  watchlist_counts_by_prop_category: Record<string, number>;
  parlay_watchlist_counts_by_scope: Record<string, number>;
  parlay_watchlist_counts_by_leg_count: Record<string, number>;
}

export interface RunRead {
  id: number;
  kind: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  records_processed: number;
  error_message: string | null;
  summary_counts: RunSummaryCounts;
}

export interface RunDetailRead extends RunRead {
  details: Record<string, unknown>;
}

export interface WatchlistDiagnosticsRead {
  status: string;
  environment: string;
  scheduler_enabled: boolean;
  refresh_status: "idle" | "queued" | "running" | "failed";
  refresh_reason: "none" | "startup" | "interval" | "manual" | "pregame";
  last_successful_refresh_at: string | null;
  data_stale: boolean;
  refresh_error_message: string | null;
  prop_refresh_status: "idle" | "queued" | "running" | "failed";
  prop_refresh_reason: "none" | "startup" | "interval" | "manual";
  last_prop_refresh_at: string | null;
  prop_data_stale: boolean;
  prop_refresh_error_message: string | null;
  latest_refresh_run: RunRead | null;
  latest_refresh_succeeded: boolean | null;
  latest_supported_markets_kept: number;
  latest_recommendations_emitted: number;
  latest_current_slate_event_count: number;
  latest_current_slate_candidate_market_count: number;
  latest_current_slate_loaded_candidate_market_count: number;
  latest_current_slate_filtered_candidate_market_count: number;
  latest_current_slate_candidate_filter_reason_counts: Record<string, number>;
  latest_current_slate_scored_market_count: number;
  latest_current_slate_coverage_prediction_count: number;
  latest_current_slate_blocking_reason: string | null;
  latest_scorer_outcome_counts: Record<string, number>;
  latest_watchlist_counts_by_sport: Record<string, number>;
  current_recommendation_count: number;
  watchlist_min_edge: number;
  watchlist_min_confidence: number;
  active_refresh_job: RefreshJobRead | null;
  latest_refresh_job: RefreshJobRead | null;
  active_prop_refresh_job: RefreshJobRead | null;
  latest_prop_refresh_job: RefreshJobRead | null;
}

export interface JobRefreshResponse {
  job_id: number;
  kind: string;
  scope: string;
  status: "queued" | "running" | "completed" | "failed";
}

export type ReadinessStatus =
  | "heuristic_only"
  | "insufficient_history"
  | "shadow_not_started"
  | "shadowing"
  | "ready_for_review"
  | "serving";

export type RuntimeHealthStatus = "healthy" | "degraded" | "unavailable";
export type StudyTrack = "active" | "heuristic_only";

export interface ReadinessBucketRead {
  label: string;
  total_count: number;
  won_count: number;
  lost_count: number;
  push_count: number;
  cancelled_count: number;
  win_rate: number | null;
  average_realized_pnl: number | null;
}

export interface ModelFamilyRuntimeHealthRead {
  family_key: string;
  desired_mode: "heuristic" | "shadow" | "ml";
  effective_mode: "heuristic" | "shadow" | "ml";
  runtime_health: RuntimeHealthStatus;
  fallback_active: boolean;
  consecutive_failures: number;
  last_check_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  artifact_path: string | null;
  model_name: string | null;
  model_version: string | null;
  calibration_version: string | null;
  feature_set_version: string | null;
  model_metadata: Record<string, unknown>;
  promotion_mode: "shadow" | "ml" | null;
  promotion_stability_days: number;
  promotion_baseline_brier: number | null;
  promotion_metrics: Record<string, unknown>;
  promotion_updated_at: string | null;
}

export interface ModelFamilyReadinessRead {
  family_key: string;
  label: string;
  scope: string;
  sport_scope: string;
  leg_count: number | null;
  study_track: StudyTrack;
  readiness_status: ReadinessStatus;
  why_not_ready: string;
  runtime: ModelFamilyRuntimeHealthRead;
  total_predictions: number;
  settled_predictions: number;
  pending_predictions: number;
  coverage_predictions: number;
  coverage_settled_predictions: number;
  coverage_pending_predictions: number;
  shadow_predictions: number;
  shadow_coverage_ratio: number;
  shadow_backlog_predictions: number;
  shadow_backlog_parlays: number;
  last_shadow_capture_at: string | null;
  won_predictions: number;
  lost_predictions: number;
  push_predictions: number;
  cancelled_predictions: number;
  average_edge: number | null;
  average_confidence: number | null;
  average_realized_pnl: number | null;
  last_settled_at: string | null;
  confidence_buckets: ReadinessBucketRead[];
  edge_buckets: ReadinessBucketRead[];
  feature_coverage_rates: Record<string, number>;
  missing_context_rates: Record<string, number>;
  top_failure_reasons: Record<string, number>;
  last_validation_failure: string | null;
  last_fallback_event_at: string | null;
}

export interface ModelReadinessSummaryRead {
  generated_at: string;
  ml_serving_mode: "heuristic" | "shadow" | "ml";
  shadow_enabled: boolean;
  auto_promotion_enabled: boolean;
  min_settled_for_review: number;
  min_shadow_coverage: number;
  min_promotion_shadow_samples: number;
  promotion_stability_days_required: number;
  families: ModelFamilyReadinessRead[];
}

export interface ModelReadinessSettingsUpdate {
  ml_serving_mode: "heuristic" | "shadow" | "ml";
  enqueue_shadow_backfill?: boolean;
}

export interface PaperPositionRead {
  id: number;
  ticker: string;
  side: string;
  quantity: number;
  entry_price: number;
  exit_price: number | null;
  status: string;
  pnl: number | null;
  notes: string | null;
  opened_at: string;
  closed_at: string | null;
}

export interface DemoOrderRead {
  id: number;
  ticker: string;
  client_order_id: string;
  kalshi_order_id: string | null;
  side: string;
  action: string;
  quantity: number;
  limit_price: number;
  status: string;
  approved_by_user: boolean;
  submitted_at: string | null;
  last_synced_at: string | null;
}

export interface PositionsRead {
  paper_positions: PaperPositionRead[];
  demo_orders: DemoOrderRead[];
}

export interface PaperPositionCreate {
  ticker: string;
  side: string;
  quantity: number;
  entry_price: number;
  notes?: string;
}

export interface PaperPositionExit {
  exit_price: number;
}

export interface DemoOrderCreate {
  ticker: string;
  side: string;
  action?: string;
  quantity: number;
  limit_price: number;
  approved?: boolean;
  time_in_force?: string;
}

export interface PredictionRead {
  id: number;
  run_id: number | null;
  event_id: number | null;
  market_id: number;
  ticker: string;
  sport_key: string | null;
  event_name: string | null;
  market_title: string;
  market_family: string | null;
  market_kind: string | null;
  stat_key: string | null;
  threshold: number | null;
  subject_name: string | null;
  subject_team: string | null;
  capture_scope: string;
  side: string;
  action: string;
  suggested_price: number;
  fair_yes_price: number | null;
  fair_no_price: number | null;
  edge: number;
  confidence: number;
  selected_side_probability: number | null;
  source_type: string | null;
  source_market_ticker: string | null;
  source_market_title: string | null;
  display_market_title: string | null;
  source_badge_label: string | null;
  context_coverage_score: number | null;
  quality_tier: string | null;
  model_name: string;
  model_version: string | null;
  calibration_version: string | null;
  feature_set_version: string | null;
  invalidation: string | null;
  rationale: string;
  reasons: string[];
  features: Record<string, unknown>;
  market_status_at_capture: string | null;
  settlement_status: string;
  prediction_outcome: string;
  market_result: string | null;
  winning_side: string | null;
  settlement_value: number | null;
  settled_at: string | null;
  realized_pnl: number | null;
  settlement_source: string | null;
  settlement_notes: string | null;
  captured_at: string;
}

export interface PredictionSummaryRead {
  total_predictions: number;
  settled_predictions: number;
  pending_predictions: number;
  unresolved_predictions: number;
  won_predictions: number;
  lost_predictions: number;
  push_predictions: number;
  cancelled_predictions: number;
  win_rate: number | null;
  loss_rate: number | null;
  average_edge: number | null;
  average_confidence: number | null;
  average_realized_pnl: number | null;
  by_sport: Record<string, number>;
  by_market_family: Record<string, number>;
  by_outcome: Record<string, number>;
}

export interface ParlayPredictionLegRead {
  leg_index: number;
  ticker: string;
  sport_key: string | null;
  event_name: string | null;
  market_title: string;
  market_family: string | null;
  market_kind: string | null;
  stat_key: string | null;
  threshold: number | null;
  subject_name: string | null;
  subject_team: string | null;
  side: string;
  action: string;
  suggested_price: number;
  fair_yes_price: number | null;
  fair_no_price: number | null;
  edge: number;
  confidence: number;
}

export interface ParlayPredictionRead {
  id: number;
  run_id: number | null;
  leg_count: number;
  sport_scope: string;
  participating_sports: string[];
  combined_market_price: number;
  combined_model_probability: number;
  american_odds: string;
  edge: number;
  confidence: number;
  model_name: string | null;
  model_version: string | null;
  calibration_version: string | null;
  feature_set_version: string | null;
  rationale: string;
  invalidation: string | null;
  settlement_status: string;
  prediction_outcome: string;
  settlement_value: number | null;
  settled_at: string | null;
  realized_pnl: number | null;
  settlement_notes: string | null;
  captured_at: string;
  legs: ParlayPredictionLegRead[];
}

export interface ParlayPredictionSummaryRead {
  total_predictions: number;
  settled_predictions: number;
  pending_predictions: number;
  unresolved_predictions: number;
  won_predictions: number;
  lost_predictions: number;
  push_predictions: number;
  cancelled_predictions: number;
  win_rate: number | null;
  loss_rate: number | null;
  average_edge: number | null;
  average_confidence: number | null;
  average_realized_pnl: number | null;
  by_sport_scope: Record<string, number>;
  by_leg_count: Record<string, number>;
  by_outcome: Record<string, number>;
}

export interface StatsSummaryRead {
  games: number;
  wins: number | null;
  losses: number | null;
  draws: number | null;
  metrics: Record<string, number | null>;
  stat_line: string | null;
}

export interface StatsGameLogRead {
  game_id: string;
  game_date: string;
  competition: string | null;
  team_name: string | null;
  location: string;
  opponent: string;
  opponent_abbreviation: string | null;
  result: string | null;
  team_score: number;
  opponent_score: number;
  metrics: Record<string, number | null>;
  stat_line: string | null;
}

export interface StatsQueryRead {
  question: string;
  sport_key: string;
  entity_name: string;
  entity_id: string;
  team_name: string | null;
  query_type: string;
  season: number;
  games_requested: number | null;
  games_analyzed: number;
  split: string | null;
  opponent: string | null;
  metric_labels: Record<string, string>;
  summary: StatsSummaryRead;
  game_logs: StatsGameLogRead[];
  explanation: string;
  coverage_note?: string | null;
  source: string;
}

export interface PredictionSettlementResponse {
  processed: number;
  updated: number;
  won: number;
  lost: number;
  push: number;
  cancelled: number;
  pending: number;
  unresolved: number;
  errors: number;
}
