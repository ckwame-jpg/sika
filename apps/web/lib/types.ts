/* ─── Mapped directly from apps/api/app/schemas.py ─── */

export type SportKey = "NBA" | "NFL" | "MLB" | "SOCCER" | "TENNIS" | "UFC";

export const SPORT_LABELS: Record<SportKey, string> = {
  NBA: "NBA",
  NFL: "NFL",
  MLB: "MLB",
  SOCCER: "Soccer",
  TENNIS: "Tennis",
  UFC: "UFC",
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
  confidence: number;
  fair_yes_price: number;
  fair_no_price: number;
  edge: number;
  reasons: string[];
  features: Record<string, unknown>;
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
  recommendations_emitted: number;
  predictions_captured: number;
  parlay_recommendations_emitted: number;
  parlay_predictions_captured: number;
  prediction_settlement_updated: number;
  parlay_prediction_settlement_updated: number;
  prediction_outcomes: Record<string, number>;
  parlay_prediction_outcomes: Record<string, number>;
  unsupported_prop_category_counts: Record<string, number>;
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
  side: string;
  action: string;
  suggested_price: number;
  fair_yes_price: number | null;
  fair_no_price: number | null;
  edge: number;
  confidence: number;
  model_name: string;
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
