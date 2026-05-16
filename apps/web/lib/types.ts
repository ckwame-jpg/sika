/* ─── Mapped directly from apps/api/app/schemas.py ─── */

import type { Schema } from "@kalshi-sports-copilot/contracts";

export type SportKey = "NBA" | "NFL" | "MLB" | "SOCCER" | "TENNIS";

export const SPORT_LABELS: Record<SportKey, string> = {
  NBA: "NBA",
  NFL: "NFL",
  MLB: "MLB",
  SOCCER: "Soccer",
  TENNIS: "Tennis",
};

/**
 * Bug #40 / Architecture #6 — web contracts migration.
 *
 * The hand-written interfaces in this file mirror the Pydantic
 * schemas in `apps/api/app/schemas.py`. The mirror has drifted —
 * Smarter #23 added `upstream_sources` to `HealthResponse` server-side
 * but the hand-written version never picked it up, so consumers had
 * no type-level signal that the field exists.
 *
 * The migration replaces each hand-written interface with a shim
 * re-export of the OpenAPI-generated `Schema<"…">` type. The
 * `Wire<T>` utility strips the optional modifier from every field
 * — recursively — because Pydantic always serializes
 * `field | None = None` as `{"field": null}` (the key is present on
 * the wire), even though openapi-typescript marks the field `?:`
 * per the OpenAPI nullable spec. The runtime contract is "always
 * present, maybe null"; this encodes that at the type level so
 * consumers don't have to handle a spurious `undefined`.
 *
 * The recursion is required because a top-level `Required<T>`
 * doesn't propagate into nested objects (e.g.
 * `HealthResponse.active_refresh_job` is `RefreshJobRead | null`,
 * and the nested `RefreshJobRead` keeps its optional fields unless
 * we recurse). Without the deep variant, a consumer passing
 * `health.active_settlement_job` into a function typed against
 * the shim's `RefreshJobRead` fails to type-check.
 *
 * Migration order: read-only endpoint families first. Each family's
 * types land in one PR so all consumers update together (no
 * half-migrated state).
 */
type Wire<T> = T extends (infer U)[]
  ? Wire<U>[]
  : T extends object
    ? { [K in keyof T]-?: Wire<T[K]> }
    : T;

// ── /health endpoint family (first migration) ──
export type HealthResponse = Wire<Schema<"HealthResponse">>;
export type RefreshJobRead = Wire<Schema<"RefreshJobRead">>;
export type UpstreamSourceHealthRead = Wire<Schema<"UpstreamSourceHealthRead">>;

// ── /predictions + /parlays/predictions endpoint family ──
export type PredictionRead = Wire<Schema<"PredictionRead">>;
export type PredictionSummaryRead = Wire<Schema<"PredictionSummaryRead">>;
export type ParlayPredictionLegRead = Wire<Schema<"ParlayPredictionLegRead">>;
export type ParlayPredictionRead = Wire<Schema<"ParlayPredictionRead">>;
export type ParlayPredictionSummaryRead = Wire<Schema<"ParlayPredictionSummaryRead">>;
export type PredictionSettlementResponse = Wire<Schema<"PredictionSettlementResponse">>;

// ── /ops/models/readiness endpoint family ──
//
// ``ReadinessStatus`` / ``RuntimeHealthStatus`` / ``StudyTrack`` are
// inline literal unions in the generated schema (not standalone
// component types). Pull them out via indexed-access so the source
// of truth stays the OpenAPI generation — adding a new readiness
// state on the API side flows through automatically.
export type ReadinessStatus = Wire<Schema<"ModelFamilyReadinessRead">>["readiness_status"];
export type RuntimeHealthStatus = Wire<Schema<"ModelFamilyRuntimeHealthRead">>["runtime_health"];
export type ReadinessBucketRead = Wire<Schema<"ReadinessBucketRead">>;
export type CalibrationBucketRead = Wire<Schema<"CalibrationBucketRead">>;
export type ModelFamilyRuntimeHealthRead = Wire<Schema<"ModelFamilyRuntimeHealthRead">>;
export type ModelFamilyReadinessRead = Wire<Schema<"ModelFamilyReadinessRead">>;
export type SettlementAgingRead = Wire<Schema<"SettlementAgingRead">>;
export type ModelReadinessSummaryRead = Wire<Schema<"ModelReadinessSummaryRead">>;
// Update DTOs use ``Partial<Schema<…>>`` instead of ``Wire<Schema<…>>``
// because the partial-PATCH idiom (caller sends only the fields they
// want to change) requires every field to be optional. The generated
// schema already marks fields with ``T | None = None`` Pydantic
// defaults as ``?:``; ``Partial<>`` extends that to the few fields
// that have non-None defaults (e.g. ``enqueue_shadow_backfill: bool
// = True``) so call sites like ``{ pick_history_default_n: 5 }``
// continue to type-check.
export type ModelReadinessSettingsUpdate = Partial<Schema<"ModelReadinessSettingsUpdate">>;

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
  // Smarter #24 — minutes until the market closes. ``null`` when no
  // close_time is set on the market; clamped at 0 for past close.
  time_to_close_minutes: number | null;
  // Smarter #31 — verifier-checked LLM explanation. ``null`` when the
  // narrator toggle is off, no cache exists, or the verifier rejected
  // the output. The card renders this in addition to (not instead of)
  // the mechanical rationale.
  narrator_text: string | null;
}

interface MarketSnapshotRead {
  captured_at: string;
  yes_bid: number | null;
  yes_ask: number | null;
  no_bid: number | null;
  no_ask: number | null;
  last_price: number | null;
  volume: number | null;
  open_interest: number | null;
}

interface SignalSnapshotRead {
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

interface SportAvailabilityRead {
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
  /** Signed numeric line from the picked side's perspective. Negative for
   *  favored/under, positive for dog/over. Null when there's no number to
   *  chart (moneyline, first_five_winner). */
  numeric_line: number | null;
  /** Effective over/under direction for total markets (folds in
   *  ``copilot_direction`` so Under-market YES picks resolve to
   *  ``under``). Null for non-total markets. */
  total_direction: "over" | "under" | null;
  /** Bug #37: most recent ``last_price`` values for this market in
   *  chronological order (oldest → newest), capped server-side. Empty
   *  when no captured snapshots exist; the row sparkline falls back to
   *  a deterministic synthetic walk. */
  price_history: number[];
  /** Smarter #24 — minutes until close. ``null`` for markets without a
   *  scheduled close; clamped at 0 for past close. */
  time_to_close_minutes: number | null;
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
  /** Smarter #24 — minutes until close. ``null`` for markets without a
   *  scheduled close; clamped at 0 for past close. */
  time_to_close_minutes: number | null;
}

interface TradeDeskStatGroup {
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
  candidate_market_count: number;
  scored_market_count: number;
  coverage_prediction_count: number;
  game_lines: TradeDeskGameLine[];
  player_props: TradeDeskPlayerProp[];
}

export interface TradeDeskArchivedSlate {
  events: TradeDeskEvent[];
  generated_at: string | null;
  freshness_status: "stale";
  event_count: number;
  candidate_market_count: number;
  scored_market_count: number;
  recommendation_count: number;
  coverage_prediction_count: number;
  blocking_reason: string | null;
  generated_from_run_id: number | null;
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
  previous_slate: TradeDeskArchivedSlate | null;
}

interface MarketHistoryPointRead {
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

export interface JobRefreshResponse {
  job_id: number;
  kind: string;
  scope: string;
  status: "queued" | "running" | "completed" | "failed";
}

// Hand-written ReadinessStatus / RuntimeHealthStatus / StudyTrack /
// ReadinessBucketRead / CalibrationBucketRead / ModelFamilyRuntimeHealthRead /
// ModelFamilyReadinessRead / SettlementAgingRead / ModelReadinessSummaryRead /
// ModelReadinessSettingsUpdate replaced by shim re-exports near the top
// of this file (Bug #40 phase 3).

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

interface KalshiAccountBalanceRead {
  cash_balance_dollars: number | null;
  portfolio_value_dollars: number | null;
  updated_ts: number | null;
}

export interface KalshiAccountMarketPositionRead {
  ticker: string;
  bet_label: string | null;
  bet_subtitle: string | null;
  market_title: string | null;
  market_subtitle: string | null;
  sport_key: string | null;
  position: number;
  total_traded_dollars: number | null;
  market_exposure_dollars: number | null;
  realized_pnl_dollars: number | null;
  fees_paid_dollars: number | null;
  resting_orders_count: number;
  last_updated_ts: string | null;
}

export interface KalshiAccountFillRead {
  fill_id: string | null;
  trade_id: string | null;
  order_id: string | null;
  ticker: string;
  bet_label: string | null;
  bet_subtitle: string | null;
  market_title: string | null;
  market_subtitle: string | null;
  sport_key: string | null;
  side: string | null;
  action: string | null;
  count: number;
  yes_price_dollars: number | null;
  no_price_dollars: number | null;
  fee_dollars: number | null;
  created_time: string | null;
}

interface KalshiAccountRead {
  configured: boolean;
  status: "connected" | "not_configured" | "error";
  error_message: string | null;
  balance: KalshiAccountBalanceRead | null;
  market_positions: KalshiAccountMarketPositionRead[];
  recent_fills: KalshiAccountFillRead[];
}

export interface PositionsRead {
  paper_positions: PaperPositionRead[];
  demo_orders: DemoOrderRead[];
  kalshi_account: KalshiAccountRead;
  /** Bug #28: ``true`` when the server hit ``paper_limit`` and at
   *  least one row past the cap exists. UI surfaces a "showing N of
   *  more" hint so operators know to raise the cap. Optional for
   *  backwards compatibility with older API builds. */
  paper_truncated?: boolean;
  /** Bug #28: ``true`` when the server hit ``demo_limit`` and at
   *  least one row past the cap exists. */
  demo_truncated?: boolean;
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

// Hand-written PredictionRead / PredictionSummaryRead / ParlayPredictionRead /
// ParlayPredictionSummaryRead / ParlayPredictionLegRead replaced by the shim
// re-exports near the top of this file (Bug #40 phase 2).

interface StatsSummaryRead {
  games: number;
  wins: number | null;
  losses: number | null;
  draws: number | null;
  metrics: Record<string, number | null>;
  stat_line: string | null;
  /** 0-100 league percentile rank per metric key. Optional — populated only
   *  for advanced metrics that have a league-wide distribution cached. */
  percentiles?: Record<string, number>;
  /** Tags each metric key as "basic" or "advanced" so the UI can group them
   *  into separate sections. Optional — UI falls back to single-section
   *  rendering when absent. */
  metric_categories?: Record<string, "basic" | "advanced">;
}

interface StatsGameLogRead {
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

export interface TeamGameResultRead {
  game_date: string;
  opponent: string;
  opponent_abbreviation: string | null;
  location: "home" | "away";
  team_score: number;
  opp_score: number;
  result: "W" | "L";
}

export interface TeamHistoryRead {
  entity_id: string;
  team_name: string;
  sport_key: string;
  results: TeamGameResultRead[];
}

// PredictionSettlementResponse replaced by the shim re-export near the
// top of this file (Bug #40 phase 2).

/* ─── Smarter #25 — market-mapping review queue ─── */

export interface MarketMappingCandidateRead {
  event_id: number;
  event_name: string | null;
  sport_key: string | null;
  score: number;
  time_delta_seconds: number | null;
}

export interface MarketMappingStateRead {
  ticker: string;
  event_id: number | null;
  sport_key: string | null;
  mapping_confidence: number | null;
  mapping_candidates: MarketMappingCandidateRead[];
  mapping_overridden_at: string | null;
  mapping_overridden_reason: string | null;
}

export interface MarketMappingListItemRead {
  ticker: string;
  title: string;
  sport_key: string | null;
  event_id: number | null;
  event_name: string | null;
  mapping_confidence: number | null;
  candidate_count: number;
  top_candidate_event_id: number | null;
  top_candidate_event_name: string | null;
  top_candidate_score: number | null;
  mapping_overridden_at: string | null;
  mapping_overridden_reason: string | null;
}

export interface MarketMappingOverrideCreate {
  event_id: number | null;
  reason: string | null;
}
