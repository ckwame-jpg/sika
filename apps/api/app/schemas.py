from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

from app.datetime_utils import ensure_utc_datetime, utc_isoformat


UTCDateTime = Annotated[
    datetime,
    BeforeValidator(ensure_utc_datetime),
    PlainSerializer(utc_isoformat, return_type=str, when_used="json"),
]


ProductSlateStatus = Literal["fresh", "stale", "degraded", "empty"]
ProductFreshnessStatus = Literal["fresh", "stale", "degraded", "empty", "missing"]


class RefreshJobRead(BaseModel):
    id: int
    kind: str
    scope: str
    reason: str
    status: Literal["queued", "running", "completed", "failed"]
    run_id: int | None = None
    error_message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    queued_at: UTCDateTime
    started_at: UTCDateTime | None = None
    finished_at: UTCDateTime | None = None


class HealthResponse(BaseModel):
    status: str
    environment: str
    scheduler_enabled: bool
    refresh_status: Literal["idle", "queued", "running", "failed"]
    refresh_reason: Literal["none", "startup", "interval", "manual", "pregame"]
    last_successful_refresh_at: UTCDateTime | None = None
    data_stale: bool
    refresh_error_message: str | None = None
    prop_refresh_status: Literal["idle", "queued", "running", "failed"]
    prop_refresh_reason: Literal["none", "startup", "interval", "manual"]
    last_prop_refresh_at: UTCDateTime | None = None
    prop_data_stale: bool
    prop_refresh_error_message: str | None = None
    active_refresh_job: RefreshJobRead | None = None
    latest_refresh_job: RefreshJobRead | None = None
    active_prop_refresh_job: RefreshJobRead | None = None
    latest_prop_refresh_job: RefreshJobRead | None = None


class SportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str


class EventParticipantRead(BaseModel):
    participant_id: int
    display_name: str
    role: str
    is_home: bool
    score: float | None = None
    result: str | None = None


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    external_id: str
    sport_key: str
    name: str
    status: str
    starts_at: UTCDateTime
    completed_at: UTCDateTime | None = None
    participants: list[EventParticipantRead]
    raw_data: dict[str, Any] = Field(default_factory=dict)


class RecommendationRead(BaseModel):
    id: int
    ticker: str
    sport_key: str | None
    market_title: str
    event_name: str
    starts_at: UTCDateTime | None = None
    market_family: str | None = None
    market_kind: str | None = None
    stat_key: str | None = None
    threshold: float | None = None
    direction: str | None = None
    subject_name: str | None = None
    subject_team: str | None = None
    side: str
    action: str
    suggested_price: float
    edge: float
    confidence: float
    selected_side_probability: float | None = None
    source_type: str | None = None
    source_market_ticker: str | None = None
    source_market_title: str | None = None
    display_market_title: str | None = None
    source_badge_label: str | None = None
    context_coverage_score: float | None = None
    quality_tier: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    calibration_version: str | None = None
    feature_set_version: str | None = None
    invalidation: str
    rationale: str
    captured_at: UTCDateTime


class ParlayRecommendationLegRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    leg_index: int
    ticker: str
    sport_key: str | None = None
    event_name: str | None = None
    market_title: str
    market_family: str | None = None
    market_kind: str | None = None
    stat_key: str | None = None
    threshold: float | None = None
    subject_name: str | None = None
    subject_team: str | None = None
    side: str
    action: str
    suggested_price: float
    fair_yes_price: float | None = None
    fair_no_price: float | None = None
    edge: float
    confidence: float


class ParlayRecommendationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int | None = None
    leg_count: int
    sport_scope: str
    participating_sports: list[str] = Field(default_factory=list)
    status: str
    combined_market_price: float
    combined_model_probability: float
    american_odds: str
    edge: float
    confidence: float
    model_name: str | None = None
    model_version: str | None = None
    calibration_version: str | None = None
    feature_set_version: str | None = None
    invalidation: str
    rationale: str
    captured_at: UTCDateTime
    legs: list[ParlayRecommendationLegRead] = Field(default_factory=list)


class MarketSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    captured_at: UTCDateTime
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    last_price: float | None = None
    volume: float | None = None
    open_interest: float | None = None


class SignalSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    captured_at: UTCDateTime
    model_name: str
    model_version: str | None = None
    calibration_version: str | None = None
    feature_set_version: str | None = None
    confidence: float
    fair_yes_price: float
    fair_no_price: float
    edge: float
    reasons: list[str]
    features: dict[str, Any]
    scoring_diagnostics: dict[str, Any] = Field(default_factory=dict)


class MarketDetailRead(BaseModel):
    ticker: str
    title: str
    subtitle: str | None = None
    sport_key: str | None = None
    market_family: str | None = None
    market_kind: str | None = None
    stat_key: str | None = None
    threshold: float | None = None
    direction: str | None = None
    subject_name: str | None = None
    subject_team: str | None = None
    status: str
    close_time: UTCDateTime | None = None
    event: EventRead | None = None
    latest_snapshot: MarketSnapshotRead | None = None
    latest_signal: SignalSnapshotRead | None = None
    recommendations: list[RecommendationRead]


class MarketListRead(BaseModel):
    ticker: str
    title: str
    subtitle: str | None = None
    sport_key: str | None = None
    market_family: str | None = None
    market_kind: str | None = None
    stat_key: str | None = None
    threshold: float | None = None
    direction: str | None = None
    subject_name: str | None = None
    subject_team: str | None = None
    status: str
    close_time: UTCDateTime | None = None
    event_name: str | None = None
    latest_snapshot: MarketSnapshotRead | None = None
    latest_recommendation: RecommendationRead | None = None


class WatchlistCoverageRowRead(BaseModel):
    ticker: str
    event_id: int | None = None
    event_name: str | None = None
    event_status: str | None = None
    starts_at: UTCDateTime | None = None
    sport_key: str | None = None
    market_title: str
    market_family: str | None = None
    market_kind: str | None = None
    stat_key: str | None = None
    threshold: float | None = None
    direction: str | None = None
    subject_name: str | None = None
    subject_team: str | None = None
    coverage_status: Literal["recommendation", "prediction", "market"]
    prop_context_stale: bool = False
    latest_snapshot: MarketSnapshotRead | None = None
    latest_recommendation: RecommendationRead | None = None
    latest_prediction: PredictionRead | None = None


AvailabilityMode = Literal["live", "research_only"]


class SportAvailabilityRead(BaseModel):
    sport_key: str
    availability_mode: AvailabilityMode
    events_count: int = 0
    recommendations_count: int = 0
    last_refresh_at: UTCDateTime | None = None


class TradeDeskGameLineRead(BaseModel):
    ticker: str
    market_title: str
    display_label: str
    sport_key: str | None = None
    market_kind: str
    selected_side: str
    projected_side_label: str | None = None
    selected_side_probability: float | None = None
    entry_price: float | None = None
    edge: float
    confidence: float
    kalshi_url: str | None = None


class TradeDeskThresholdRead(BaseModel):
    ticker: str
    threshold: float
    probability_yes: float
    selected_side: str
    selected_side_probability: float | None = None
    entry_price: float | None = None
    edge: float
    confidence: float
    is_best: bool = False
    kalshi_url: str | None = None


class TradeDeskStatGroupRead(BaseModel):
    stat_key: str
    thresholds: list[TradeDeskThresholdRead] = Field(default_factory=list)


class TradeDeskPlayerPropRead(BaseModel):
    subject_name: str
    subject_team: str | None = None
    stat_groups: list[TradeDeskStatGroupRead] = Field(default_factory=list)
    best_edge: float
    best_win_prob: float | None = None


class TradeDeskEventRead(BaseModel):
    event_id: int
    event_name: str
    event_status: str
    starts_at: UTCDateTime | None = None
    sport_key: str
    game_lines: list[TradeDeskGameLineRead] = Field(default_factory=list)
    player_props: list[TradeDeskPlayerPropRead] = Field(default_factory=list)


class TradeDeskResponse(BaseModel):
    events: list[TradeDeskEventRead] = Field(default_factory=list)
    research_sports: list[SportAvailabilityRead] = Field(default_factory=list)
    generated_at: UTCDateTime | None = None
    freshness_status: ProductSlateStatus = "fresh"
    event_count: int = 0
    candidate_market_count: int = 0
    scored_market_count: int = 0
    recommendation_count: int = 0
    coverage_prediction_count: int = 0
    blocking_reason: str | None = None
    generated_from_run_id: int | None = None


class ProductScopeFreshnessRead(BaseModel):
    """Per-scope freshness status for the product read path.

    ``scope`` is either ``"all"`` (full cross-sport slate) or a sport key
    such as ``"NBA"``/``"MLB"``. ``generated_at`` is the ``persisted_at`` of
    the latest snapshot row for that scope. ``status`` follows the same
    vocabulary as ``TradeDeskResponse.freshness_status`` with an extra
    ``"missing"`` state for scopes that have never been snapshotted.
    """

    scope: str
    generated_at: UTCDateTime | None = None
    status: ProductFreshnessStatus
    event_count: int = 0
    candidate_market_count: int = 0
    scored_market_count: int = 0
    recommendation_count: int = 0
    coverage_prediction_count: int = 0
    blocking_reason: str | None = None
    generated_from_run_id: int | None = None


class ProductFreshnessResponse(BaseModel):
    """Product-facing freshness gauge.

    Populated by reading the latest row of ``current_slate_snapshots`` per
    scope. Because the snapshot store is versioned and append-only, this
    endpoint is side-effect-free: the product read path never blocks on,
    and never fails because of, the write path. ``overall_status`` is the
    worst status across all scopes (missing > stale > fresh).
    """

    scopes: list[ProductScopeFreshnessRead] = Field(default_factory=list)
    overall_status: ProductFreshnessStatus


class ProductSportsResponse(BaseModel):
    """Runtime sport scope for product pickers.

    Slice 4: replaces the hardcoded ``SportKey`` TS union in
    ``apps/web/lib/types.ts`` with a runtime list sourced from
    ``config.py:enabled_sports``. The frontend consumes this so changing
    enabled sports on the backend does not require a frontend redeploy.
    """

    sports: list[str] = Field(default_factory=list)


class RunSummaryCounts(BaseModel):
    sports_records_ingested: dict[str, int] = Field(default_factory=dict)
    total_kalshi_markets_seen: int = 0
    supported_markets_kept: int = 0
    supported_nba_props_seen: int = 0
    supported_mlb_props_seen: int = 0
    mapped_markets: int = 0
    mapped_prop_markets: int = 0
    current_slate_event_count: int = 0
    current_slate_candidate_market_count: int = 0
    current_slate_loaded_candidate_market_count: int = 0
    current_slate_filtered_candidate_market_count: int = 0
    current_slate_candidate_filter_reason_counts: dict[str, int] = Field(default_factory=dict)
    current_slate_scored_market_count: int = 0
    current_slate_coverage_prediction_count: int = 0
    current_slate_blocking_reason: str | None = None
    scorer_outcome_counts: dict[str, int] = Field(default_factory=dict)
    recommendations_emitted: int = 0
    predictions_captured: int = 0
    parlay_recommendations_emitted: int = 0
    parlay_predictions_captured: int = 0
    prediction_settlement_updated: int = 0
    parlay_prediction_settlement_updated: int = 0
    prediction_outcomes: dict[str, int] = Field(default_factory=dict)
    parlay_prediction_outcomes: dict[str, int] = Field(default_factory=dict)
    unsupported_prop_category_counts: dict[str, int] = Field(default_factory=dict)
    heuristic_longshots_suppressed: int = 0
    inverse_winner_duplicates_collapsed: int = 0
    combo_prop_candidates_emitted: int = 0
    combo_prop_candidates_suppressed: int = 0
    critical_context_suppressed: int = 0
    quality_tier_counts: dict[str, int] = Field(default_factory=dict)
    prop_subjects_warmed: int = 0
    player_search_cache_hits: int = 0
    player_search_cache_misses: int = 0
    gamelog_cache_hits: int = 0
    gamelog_cache_misses: int = 0
    stale_gamelog_fallbacks: int = 0
    combo_prop_legs_discovered: int = 0
    combo_prop_legs_refreshed: int = 0
    watchlist_counts_by_sport: dict[str, int] = Field(default_factory=dict)
    watchlist_counts_by_prop_category: dict[str, int] = Field(default_factory=dict)
    parlay_watchlist_counts_by_scope: dict[str, int] = Field(default_factory=dict)
    parlay_watchlist_counts_by_leg_count: dict[str, int] = Field(default_factory=dict)


class RunRead(BaseModel):
    id: int
    kind: str
    status: str
    started_at: UTCDateTime
    finished_at: UTCDateTime | None = None
    records_processed: int
    error_message: str | None = None
    summary_counts: RunSummaryCounts


class RunDetailRead(RunRead):
    details: dict[str, Any] = Field(default_factory=dict)


class WatchlistDiagnosticsRead(BaseModel):
    status: str
    environment: str
    scheduler_enabled: bool
    refresh_status: Literal["idle", "queued", "running", "failed"]
    refresh_reason: Literal["none", "startup", "interval", "manual", "pregame"]
    last_successful_refresh_at: UTCDateTime | None = None
    data_stale: bool
    refresh_error_message: str | None = None
    prop_refresh_status: Literal["idle", "queued", "running", "failed"]
    prop_refresh_reason: Literal["none", "startup", "interval", "manual"]
    last_prop_refresh_at: UTCDateTime | None = None
    prop_data_stale: bool
    prop_refresh_error_message: str | None = None
    latest_refresh_run: RunRead | None = None
    latest_refresh_succeeded: bool | None = None
    latest_supported_markets_kept: int = 0
    latest_recommendations_emitted: int = 0
    latest_current_slate_event_count: int = 0
    latest_current_slate_candidate_market_count: int = 0
    latest_current_slate_loaded_candidate_market_count: int = 0
    latest_current_slate_filtered_candidate_market_count: int = 0
    latest_current_slate_candidate_filter_reason_counts: dict[str, int] = Field(default_factory=dict)
    latest_current_slate_scored_market_count: int = 0
    latest_current_slate_coverage_prediction_count: int = 0
    latest_current_slate_blocking_reason: str | None = None
    latest_scorer_outcome_counts: dict[str, int] = Field(default_factory=dict)
    latest_watchlist_counts_by_sport: dict[str, int] = Field(default_factory=dict)
    current_recommendation_count: int = 0
    watchlist_min_edge: float
    watchlist_min_confidence: float
    active_refresh_job: RefreshJobRead | None = None
    latest_refresh_job: RefreshJobRead | None = None
    active_prop_refresh_job: RefreshJobRead | None = None
    latest_prop_refresh_job: RefreshJobRead | None = None


ReadinessStatus = Literal[
    "heuristic_only",
    "insufficient_history",
    "shadow_not_started",
    "shadowing",
    "ready_for_review",
    "serving",
]

RuntimeHealthStatus = Literal["healthy", "degraded", "unavailable"]
StudyTrack = Literal["active", "heuristic_only"]


class ReadinessBucketRead(BaseModel):
    label: str
    total_count: int
    won_count: int
    lost_count: int
    push_count: int
    cancelled_count: int
    win_rate: float | None = None
    average_realized_pnl: float | None = None


class ModelFamilyRuntimeHealthRead(BaseModel):
    family_key: str
    desired_mode: Literal["heuristic", "shadow", "ml"]
    effective_mode: Literal["heuristic", "shadow", "ml"]
    runtime_health: RuntimeHealthStatus
    fallback_active: bool
    consecutive_failures: int
    last_check_at: UTCDateTime | None = None
    last_success_at: UTCDateTime | None = None
    last_error: str | None = None
    last_error_at: UTCDateTime | None = None
    artifact_path: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    calibration_version: str | None = None
    feature_set_version: str | None = None
    model_metadata: dict[str, Any] = Field(default_factory=dict)
    promotion_mode: Literal["shadow", "ml"] | None = None
    promotion_stability_days: int = 0
    promotion_baseline_brier: float | None = None
    promotion_metrics: dict[str, Any] = Field(default_factory=dict)
    promotion_updated_at: UTCDateTime | None = None


class ModelFamilyReadinessRead(BaseModel):
    family_key: str
    label: str
    scope: str
    sport_scope: str
    leg_count: int | None = None
    study_track: StudyTrack
    readiness_status: ReadinessStatus
    why_not_ready: str
    runtime: ModelFamilyRuntimeHealthRead
    total_predictions: int
    settled_predictions: int
    pending_predictions: int
    coverage_predictions: int = 0
    coverage_settled_predictions: int = 0
    coverage_pending_predictions: int = 0
    shadow_predictions: int
    shadow_coverage_ratio: float
    shadow_backlog_predictions: int = 0
    shadow_backlog_parlays: int = 0
    last_shadow_capture_at: UTCDateTime | None = None
    won_predictions: int
    lost_predictions: int
    push_predictions: int
    cancelled_predictions: int
    average_edge: float | None = None
    average_confidence: float | None = None
    average_realized_pnl: float | None = None
    last_settled_at: UTCDateTime | None = None
    confidence_buckets: list[ReadinessBucketRead] = Field(default_factory=list)
    edge_buckets: list[ReadinessBucketRead] = Field(default_factory=list)
    feature_coverage_rates: dict[str, float] = Field(default_factory=dict)
    missing_context_rates: dict[str, float] = Field(default_factory=dict)
    top_failure_reasons: dict[str, int] = Field(default_factory=dict)
    last_validation_failure: str | None = None
    last_fallback_event_at: UTCDateTime | None = None


class ModelReadinessSummaryRead(BaseModel):
    generated_at: UTCDateTime
    ml_serving_mode: Literal["heuristic", "shadow", "ml"] = "heuristic"
    shadow_enabled: bool = False
    auto_promotion_enabled: bool = False
    min_settled_for_review: int = 40
    min_shadow_coverage: float = 0.75
    min_promotion_shadow_samples: int = 150
    promotion_stability_days_required: int = 3
    families: list[ModelFamilyReadinessRead] = Field(default_factory=list)


class ModelReadinessSettingsUpdate(BaseModel):
    ml_serving_mode: Literal["heuristic", "shadow", "ml"]
    enqueue_shadow_backfill: bool = True


class MarketHistoryPointRead(BaseModel):
    timestamp: UTCDateTime
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    last_price: float | None = None
    mean_price: float | None = None
    volume: float | None = None
    source: str


class MarketHistoryRead(BaseModel):
    ticker: str
    range: str
    points: list[MarketHistoryPointRead]


class PaperPositionCreate(BaseModel):
    ticker: str
    side: str
    quantity: int = Field(ge=1)
    entry_price: float = Field(gt=0, le=1)
    notes: str | None = None


class PaperPositionExit(BaseModel):
    exit_price: float = Field(gt=0, le=1)


class PaperPositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    side: str
    quantity: int
    entry_price: float
    exit_price: float | None = None
    status: str
    pnl: float | None = None
    notes: str | None = None
    opened_at: UTCDateTime
    closed_at: UTCDateTime | None = None


class DemoOrderCreate(BaseModel):
    ticker: str
    side: str
    action: str = "buy"
    quantity: int = Field(ge=1)
    limit_price: float = Field(gt=0, lt=1)
    approved: bool = False
    time_in_force: str = "good_till_canceled"


class DemoOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    client_order_id: str
    kalshi_order_id: str | None = None
    side: str
    action: str
    quantity: int
    limit_price: float
    status: str
    approved_by_user: bool
    submitted_at: UTCDateTime | None = None
    last_synced_at: UTCDateTime | None = None


class PositionsRead(BaseModel):
    paper_positions: list[PaperPositionRead]
    demo_orders: list[DemoOrderRead]


class JobRefreshResponse(BaseModel):
    job_id: int
    kind: str
    scope: str
    status: Literal["queued", "running", "completed", "failed"]


class PredictionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int | None = None
    event_id: int | None = None
    market_id: int
    ticker: str
    sport_key: str | None = None
    event_name: str | None = None
    market_title: str
    market_family: str | None = None
    market_kind: str | None = None
    stat_key: str | None = None
    threshold: float | None = None
    subject_name: str | None = None
    subject_team: str | None = None
    capture_scope: str = "recommendation"
    side: str
    action: str
    suggested_price: float
    fair_yes_price: float | None = None
    fair_no_price: float | None = None
    edge: float
    confidence: float
    selected_side_probability: float | None = None
    source_type: str | None = None
    source_market_ticker: str | None = None
    source_market_title: str | None = None
    display_market_title: str | None = None
    source_badge_label: str | None = None
    context_coverage_score: float | None = None
    quality_tier: str | None = None
    model_name: str
    model_version: str | None = None
    calibration_version: str | None = None
    feature_set_version: str | None = None
    invalidation: str | None = None
    rationale: str
    reasons: list[str] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    market_status_at_capture: str | None = None
    settlement_status: str
    prediction_outcome: str
    market_result: str | None = None
    winning_side: str | None = None
    settlement_value: float | None = None
    settled_at: UTCDateTime | None = None
    realized_pnl: float | None = None
    settlement_source: str | None = None
    settlement_notes: str | None = None
    captured_at: UTCDateTime


class PredictionSummaryRead(BaseModel):
    total_predictions: int
    settled_predictions: int
    pending_predictions: int
    unresolved_predictions: int
    won_predictions: int
    lost_predictions: int
    push_predictions: int
    cancelled_predictions: int
    win_rate: float | None = None
    loss_rate: float | None = None
    average_edge: float | None = None
    average_confidence: float | None = None
    average_realized_pnl: float | None = None
    by_sport: dict[str, int] = Field(default_factory=dict)
    by_market_family: dict[str, int] = Field(default_factory=dict)
    by_outcome: dict[str, int] = Field(default_factory=dict)


WatchlistCoverageRowRead.model_rebuild()


class ParlayPredictionLegRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    leg_index: int
    ticker: str
    sport_key: str | None = None
    event_name: str | None = None
    market_title: str
    market_family: str | None = None
    market_kind: str | None = None
    stat_key: str | None = None
    threshold: float | None = None
    subject_name: str | None = None
    subject_team: str | None = None
    side: str
    action: str
    suggested_price: float
    fair_yes_price: float | None = None
    fair_no_price: float | None = None
    edge: float
    confidence: float


class ParlayPredictionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int | None = None
    leg_count: int
    sport_scope: str
    participating_sports: list[str] = Field(default_factory=list)
    combined_market_price: float
    combined_model_probability: float
    american_odds: str
    edge: float
    confidence: float
    model_name: str | None = None
    model_version: str | None = None
    calibration_version: str | None = None
    feature_set_version: str | None = None
    rationale: str
    invalidation: str | None = None
    settlement_status: str
    prediction_outcome: str
    settlement_value: float | None = None
    settled_at: UTCDateTime | None = None
    realized_pnl: float | None = None
    settlement_notes: str | None = None
    captured_at: UTCDateTime
    legs: list[ParlayPredictionLegRead] = Field(default_factory=list)


class ParlayPredictionSummaryRead(BaseModel):
    total_predictions: int
    settled_predictions: int
    pending_predictions: int
    unresolved_predictions: int
    won_predictions: int
    lost_predictions: int
    push_predictions: int
    cancelled_predictions: int
    win_rate: float | None = None
    loss_rate: float | None = None
    average_edge: float | None = None
    average_confidence: float | None = None
    average_realized_pnl: float | None = None
    by_sport_scope: dict[str, int] = Field(default_factory=dict)
    by_leg_count: dict[str, int] = Field(default_factory=dict)
    by_outcome: dict[str, int] = Field(default_factory=dict)


class PredictionSettlementResponse(BaseModel):
    processed: int
    updated: int
    won: int
    lost: int
    push: int
    cancelled: int
    pending: int
    unresolved: int
    errors: int = 0


class StatsQueryRequest(BaseModel):
    question: str = Field(min_length=3)
    sport_key: str = "NBA"
    season: int | None = None


class StatsSummaryRead(BaseModel):
    games: int
    wins: int | None = None
    losses: int | None = None
    draws: int | None = None
    metrics: dict[str, float | None]
    stat_line: str | None = None


class StatsGameLogRead(BaseModel):
    game_id: str
    game_date: UTCDateTime
    competition: str | None = None
    team_name: str | None = None
    location: str
    opponent: str
    opponent_abbreviation: str | None = None
    result: str | None = None
    team_score: float
    opponent_score: float
    metrics: dict[str, float | None]
    stat_line: str | None = None


class StatsQueryRead(BaseModel):
    question: str
    sport_key: str
    entity_name: str
    entity_id: str
    team_name: str | None = None
    query_type: str
    season: int
    games_requested: int | None = None
    games_analyzed: int
    split: str | None = None
    opponent: str | None = None
    metric_labels: dict[str, str] = Field(default_factory=dict)
    summary: StatsSummaryRead
    game_logs: list[StatsGameLogRead]
    explanation: str
    coverage_note: str | None = None
    source: str
