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

// ── /events + /markets endpoint family ──
//
// MarketDetailRead embeds RecommendationRead, EventRead,
// SignalSnapshotRead, and MarketSnapshotRead through the generated
// schema's component refs. Migrating MarketDetailRead alone would
// keep those inner types as the *generated* version while the
// hand-written exports stayed in place — TypeScript would treat
// ``RecommendationRead[]`` consumers as receiving an incompatible
// type. Migrate the whole transitive closure together.
export type EventParticipantRead = Wire<Schema<"EventParticipantRead">>;
export type EventRead = Wire<Schema<"EventRead">>;
export type RecommendationRead = Wire<Schema<"RecommendationRead">>;
export type MarketDetailRead = Wire<Schema<"MarketDetailRead">>;
export type MarketHistoryRead = Wire<Schema<"MarketHistoryRead">>;

// ── /trade-desk endpoint family ──
//
// The hand-written names dropped the ``Read`` suffix for the inner
// trade-desk types (TradeDeskGameLine vs. the generated
// TradeDeskGameLineRead, etc.). The shim aliases preserve the
// hand-written names so consumers don't need to rename.
export type TradeDeskGameLine = Wire<Schema<"TradeDeskGameLineRead">>;
export type TradeDeskThreshold = Wire<Schema<"TradeDeskThresholdRead">>;
export type TradeDeskPlayerProp = Wire<Schema<"TradeDeskPlayerPropRead">>;
export type TradeDeskEvent = Wire<Schema<"TradeDeskEventRead">>;
export type TradeDeskArchivedSlate = Wire<Schema<"TradeDeskArchivedSlateRead">>;
export type TradeDeskResponse = Wire<Schema<"TradeDeskResponse">>;

// Hand-written EventParticipantRead / EventRead / RecommendationRead /
// MarketSnapshotRead / SignalSnapshotRead / MarketDetailRead replaced by
// the shim re-exports near the top of this file (Bug #40 phase 4).

interface SportAvailabilityRead {
  sport_key: string;
  availability_mode: "live" | "research_only";
  events_count: number;
  recommendations_count: number;
  last_refresh_at: string | null;
}

// Hand-written TradeDeskGameLine / TradeDeskThreshold / TradeDeskStatGroup /
// TradeDeskPlayerProp / TradeDeskEvent / TradeDeskArchivedSlate /
// TradeDeskResponse replaced by the shim re-exports near the top of this
// file (Bug #40 phase 5).

// Hand-written MarketHistoryPointRead / MarketHistoryRead replaced by the
// shim re-exports near the top of this file (Bug #40 phase 4).

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
