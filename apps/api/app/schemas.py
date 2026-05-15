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


class UpstreamSourceHealthRead(BaseModel):
    """Smarter #23 — per-upstream-source freshness for the /health surface."""

    source: str
    last_success_at: UTCDateTime | None = None
    last_failure_at: UTCDateTime | None = None
    last_error: str | None = None
    is_stale: bool


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
    active_settlement_job: RefreshJobRead | None = None
    latest_settlement_job: RefreshJobRead | None = None
    # Smarter #23 — per-upstream-source freshness. Sources that have
    # never been recorded show ``last_success_at = None`` / ``is_stale =
    # True`` so operators see the explicit "never reported in" signal.
    upstream_sources: list[UpstreamSourceHealthRead] = []


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
    # Smarter #24 — minutes until ``market.close_time``. ``None`` when the
    # market has no scheduled close. Clamped at 0 when close_time is in
    # the past (a closed market shouldn't appear on the watchlist, but we
    # don't want to surface negative values if one slips through).
    # Operators sort/highlight by this to triage T-minus-15min picks
    # ahead of T-minus-4h ones with the same edge.
    time_to_close_minutes: int | None = None
    # Smarter #31 — operator-facing LLM narration grounded in the
    # feature dict. ``None`` when narrator is disabled, no cache exists,
    # or the verifier rejected the output. Always renders alongside (not
    # instead of) the mechanical ``rationale`` so operators can compare.
    narrator_text: str | None = None


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


class MarketMappingCandidateRead(BaseModel):
    """A single candidate the auto-mapper scored when matching a
    Kalshi market ticker to a Sika ``Event``. Bug #17: persisted on
    ``Market`` so ops can review ambiguous matches."""

    event_id: int
    event_name: str | None = None
    sport_key: str | None = None
    score: float
    time_delta_seconds: float | None = None


class MarketMappingStateRead(BaseModel):
    """Read-only view of a market's current mapping state, including
    confidence + top-K candidates the auto-mapper considered and any
    manual override stamp."""

    ticker: str
    event_id: int | None = None
    sport_key: str | None = None
    mapping_confidence: float | None = None
    mapping_candidates: list[MarketMappingCandidateRead] = []
    mapping_overridden_at: UTCDateTime | None = None
    mapping_overridden_reason: str | None = None


class MarketMappingOverrideCreate(BaseModel):
    """Body for ``POST /ops/market-mapping/{ticker}``. ``event_id =
    None`` clears the mapping; otherwise links to that event."""

    event_id: int | None
    reason: str | None = Field(default=None, max_length=500)


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
    # Signed numeric line from the picked side's perspective. Negative when
    # the pick is on the favored / under side, positive when on the dog /
    # over side. Null for moneyline / first_five_winner where there is no
    # number to draw against. Consumed by the pick-history strip on the
    # frontend to render a threshold reference line.
    numeric_line: float | None = None
    # Bug #37: most recent ``last_price`` values for this market in
    # chronological order (oldest → newest), capped server-side. Empty
    # when no captured snapshots exist; the frontend sparkline then
    # falls back to a deterministic synthetic walk so the slot doesn't
    # collapse on cold-start markets.
    price_history: list[float] = Field(default_factory=list)
    # Codex round-1 P2 on PR #24: the effective over/under direction the
    # pick represents — folds ``copilot_direction`` + ``selected_side``
    # so the frontend doesn't have to re-derive it. ``"over"`` /
    # ``"under"`` for total markets, ``None`` for everything else.
    total_direction: Literal["over", "under"] | None = None
    # Smarter #24 — minutes until ``market.close_time``. ``None`` when no
    # close time; clamped at 0 if close_time is in the past. Operators
    # triage T-15min picks ahead of T-4h ones with the same edge.
    time_to_close_minutes: int | None = None


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
    # Smarter #24 — see ``TradeDeskGameLineRead.time_to_close_minutes``.
    time_to_close_minutes: int | None = None


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
    candidate_market_count: int = 0
    scored_market_count: int = 0
    coverage_prediction_count: int = 0
    game_lines: list[TradeDeskGameLineRead] = Field(default_factory=list)
    player_props: list[TradeDeskPlayerPropRead] = Field(default_factory=list)


class TradeDeskArchivedSlateRead(BaseModel):
    events: list[TradeDeskEventRead] = Field(default_factory=list)
    generated_at: UTCDateTime | None = None
    freshness_status: Literal["stale"] = "stale"
    event_count: int = 0
    candidate_market_count: int = 0
    scored_market_count: int = 0
    recommendation_count: int = 0
    coverage_prediction_count: int = 0
    blocking_reason: str | None = None
    generated_from_run_id: int | None = None


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
    previous_slate: TradeDeskArchivedSlateRead | None = None


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
    # Bug #20 walk-forward floor: shadow coverage cleared but settled
    # history (≥200 rows across ≥8 weeks) hasn't accumulated yet, so
    # advancing to ``ready_for_review`` would mislead operators —
    # arming auto-promotion in this state yields nothing because the
    # gate keeps returning ``insufficient_history``.
    "history_accumulating",
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


class CalibrationBucketRead(BaseModel):
    """Reliability-curve point. ``avg_predicted`` is the model's mean P(YES)
    for rows in this bucket; ``actual_yes_rate`` is the observed YES rate;
    ``miscalibration = avg_predicted - actual_yes_rate`` (positive = the model
    was over-confident in YES). ``None`` fields signal an empty bucket."""

    label: str
    settled_count: int
    avg_predicted: float | None = None
    actual_yes_rate: float | None = None
    miscalibration: float | None = None


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
    # Smarter #3 — signed mean closing-line value over settled predictions
    # in this family's sample. Positive = the model is sharp (line moved
    # toward the picks between capture and close). Null when no settled
    # rows carry a CLV yet, or for parlay families (no per-row close
    # price for multi-leg combinations).
    average_clv: float | None = None
    last_settled_at: UTCDateTime | None = None
    confidence_buckets: list[ReadinessBucketRead] = Field(default_factory=list)
    edge_buckets: list[ReadinessBucketRead] = Field(default_factory=list)
    # Smarter #1: per-family reliability-curve buckets. Empty list pre-shadow
    # (no settled rows in this family yet). Each bucket carries the bucket's
    # mean predicted P(YES) and the observed YES rate so the UI can render a
    # reliability curve without recomputing.
    calibration_buckets: list[CalibrationBucketRead] = Field(default_factory=list)
    feature_coverage_rates: dict[str, float] = Field(default_factory=dict)
    missing_context_rates: dict[str, float] = Field(default_factory=dict)
    top_failure_reasons: dict[str, int] = Field(default_factory=dict)
    last_validation_failure: str | None = None
    last_fallback_event_at: UTCDateTime | None = None


class SettlementAgingRead(BaseModel):
    """Smarter #26 — counts of predictions stuck in ``pending`` past
    their market close, bucketed by how long ago the close was. Surfaces
    on the readiness panel as an ops badge."""

    bucket_0_to_1h: int = 0
    bucket_1_to_6h: int = 0
    bucket_6_to_24h: int = 0
    bucket_beyond_24h: int = 0
    total_pending_past_close: int = 0


class ModelReadinessSummaryRead(BaseModel):
    generated_at: UTCDateTime
    ml_serving_mode: Literal["heuristic", "shadow", "ml"] = "heuristic"
    shadow_enabled: bool = False
    auto_promotion_enabled: bool = False
    min_settled_for_review: int = 40
    # Bug #20 walk-forward floor — settled rows needed before the
    # promotion gate can evaluate. Distinct from
    # ``min_settled_for_review`` (40), which gates shadow-mode entry.
    # The readiness ladder holds at ``history_accumulating`` between the
    # two thresholds.
    min_settled_for_promotion_review: int = 200
    min_shadow_coverage: float = 0.75
    min_promotion_shadow_samples: int = 150
    promotion_stability_days_required: int = 3
    # Operator-pinned default for the trade-ticket pick-history strip.
    # Per-pick toggles override at runtime; this is the initial value.
    pick_history_default_n: int = 5
    families: list[ModelFamilyReadinessRead] = Field(default_factory=list)
    # Smarter #26 — predictions stuck in ``pending`` past their market
    # close, bucketed by hours-since-close (0-1h / 1-6h / 6-24h / 24h+).
    # Defaults to all-zeros so existing callers that don't surface the
    # field render cleanly.
    settlement_aging: SettlementAgingRead = Field(default_factory=SettlementAgingRead)
    # Smarter #31 — LLM narrator toggle. False by default so operators
    # don't burn tokens until they've eyeballed quality on a few picks.
    narrator_enabled: bool = False
    # Smarter #18 — sportsbook disagreement suppression knobs. The
    # toggle is exposed at a separate operator surface (REPL today);
    # the threshold + min_book_count expose here so operators can
    # tune the rule's sensitivity from the readiness panel.
    # Defaults: 0.15 (15-pp gap) AND ≥3 books before the rule fires.
    sportsbook_disagreement_threshold: float = 0.15
    sportsbook_disagreement_min_book_count: int = 3


class ModelReadinessSettingsUpdate(BaseModel):
    # Codex round-4 P2 on PR #24: ``ml_serving_mode`` is optional so
    # callers can do partial updates (e.g. change ONLY
    # ``pick_history_default_n`` from the settings page without
    # writing back a possibly-stale serving mode from SWR cache).
    # The route skips ``set_ml_serving_mode`` when this is None.
    ml_serving_mode: Literal["heuristic", "shadow", "ml"] | None = None
    enqueue_shadow_backfill: bool = True
    # Codex round-6 P2 on PR #24: pinned to the exact UI options
    # (the trade-ticket strip's ``HISTORY_OPTIONS``). Accepting an
    # in-range-but-non-canonical value (6, 15, …) would have the
    # readiness summary echo it back while the strip silently
    # coerced it to 5.
    pick_history_default_n: Literal[5, 10, 20] | None = None
    # Smarter #31 — operator toggle for the LLM narrator. Optional
    # (partial-PATCH idiom) so changing only this knob doesn't
    # require resending the other settings.
    narrator_enabled: bool | None = None
    # Smarter #18 — sportsbook disagreement suppression knobs. The
    # writers in operator_settings.py are permissive (accept any
    # numeric, clamp at read time) so operators see typo-induced
    # clamping on the next read; we still validate at the API
    # boundary to catch obvious typos (1.5 / -0.1 / 0 books) before
    # the writer is called.
    sportsbook_disagreement_threshold: float | None = Field(default=None, gt=0.0, lt=1.0)
    sportsbook_disagreement_min_book_count: int | None = Field(default=None, ge=1)


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


def _normalize_lowercase(value: Any) -> Any:
    """Pre-validator that lowercases string inputs. Keeps the
    enum-literal validation strict while preserving the previous
    ``.lower()``-equivalent leniency at the API boundary (e.g.
    ``"YES"`` from a hand-rolled curl call still maps to ``"yes"``)."""
    if isinstance(value, str):
        return value.lower()
    return value


LowercaseSide = Annotated[Literal["yes", "no"], BeforeValidator(_normalize_lowercase)]
LowercaseAction = Annotated[Literal["buy", "sell"], BeforeValidator(_normalize_lowercase)]
LowercaseTimeInForce = Annotated[
    Literal["good_till_canceled", "immediate_or_cancel", "fill_or_kill"],
    BeforeValidator(_normalize_lowercase),
]


class PaperPositionCreate(BaseModel):
    ticker: str
    # Bug #15: previously ``str`` — schema accepted any value and the
    # service layer lowercased it without validating. Kalshi only has
    # YES/NO contracts; lock the vocabulary here. The before-validator
    # preserves case-insensitive input (``"YES"`` / ``"Yes"`` work as
    # they did before, but ``"hold"`` is rejected as a typo).
    side: LowercaseSide
    quantity: int = Field(ge=1)
    entry_price: float = Field(gt=0, le=1)
    notes: str | None = None


class PaperPositionExit(BaseModel):
    # Bug #15: ``exit_price`` is the SAME-side closing price as the
    # position's entry — e.g. a YES position at 0.40 closes at the
    # current YES price, NOT the NO price. PnL is computed as
    # ``(exit_price - entry_price) * quantity`` and silently inverts
    # if the caller passes the opposite-side price.
    #
    # The server cannot verify the caller's claim (the value 0.70 is
    # equally plausible as YES-close or NO-close); we document the
    # contract here and the UI labels the field with the position's
    # side. The punch-list noted two viable fixes — "Document &
    # enforce" (this PR) or "accept both prices and derive PnL from
    # the side" (a UI/schema migration, deferred). Codex round-2
    # flagged that documentation alone leaves the inversion possible
    # for future integrations; that's true and acknowledged.
    exit_price: float = Field(
        gt=0,
        le=1,
        description=(
            "Same-side closing price (YES position → YES exit; NO position → "
            "NO exit). PnL = (exit_price - entry_price) * quantity and will "
            "be wrong if the opposite-side price is supplied."
        ),
    )


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
    # Bug #15: lock the trade-action vocabulary at the boundary. Was
    # ``str``; service layer lowercased without validating, so any
    # typo (or unexpected enum from a future client) silently shipped
    # to Kalshi as the bad value and produced a confusing error
    # downstream. The before-validators preserve case-insensitive
    # input from existing hand-rolled callers.
    side: LowercaseSide
    action: LowercaseAction = "buy"
    quantity: int = Field(ge=1)
    limit_price: float = Field(gt=0, lt=1)
    approved: bool = False
    time_in_force: LowercaseTimeInForce = "good_till_canceled"


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


class KalshiAccountBalanceRead(BaseModel):
    cash_balance_dollars: float | None = None
    portfolio_value_dollars: float | None = None
    updated_ts: int | None = None


class KalshiAccountMarketPositionRead(BaseModel):
    ticker: str
    bet_label: str | None = None
    bet_subtitle: str | None = None
    market_title: str | None = None
    market_subtitle: str | None = None
    sport_key: str | None = None
    position: float
    total_traded_dollars: float | None = None
    market_exposure_dollars: float | None = None
    realized_pnl_dollars: float | None = None
    fees_paid_dollars: float | None = None
    resting_orders_count: int = 0
    last_updated_ts: UTCDateTime | None = None


class KalshiAccountFillRead(BaseModel):
    fill_id: str | None = None
    trade_id: str | None = None
    order_id: str | None = None
    ticker: str
    bet_label: str | None = None
    bet_subtitle: str | None = None
    market_title: str | None = None
    market_subtitle: str | None = None
    sport_key: str | None = None
    side: str | None = None
    action: str | None = None
    count: float
    yes_price_dollars: float | None = None
    no_price_dollars: float | None = None
    fee_dollars: float | None = None
    created_time: UTCDateTime | None = None


class KalshiAccountRead(BaseModel):
    configured: bool
    status: Literal["connected", "not_configured", "error"]
    error_message: str | None = None
    balance: KalshiAccountBalanceRead | None = None
    market_positions: list[KalshiAccountMarketPositionRead] = Field(default_factory=list)
    recent_fills: list[KalshiAccountFillRead] = Field(default_factory=list)


class PositionsRead(BaseModel):
    paper_positions: list[PaperPositionRead]
    demo_orders: list[DemoOrderRead]
    kalshi_account: KalshiAccountRead


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
    # Codex round-2 P2 on PR #24: same-name player disambiguation.
    # Forwarded to ``EspnPublicClient.search_player`` so duplicate
    # names (two "John Smith"s on different teams) resolve to the
    # right athlete instead of the first ESPN result. The pick-
    # history strip sets this from ``selection.subjectTeam``.
    team_hint: str | None = None


class StatsSummaryRead(BaseModel):
    games: int
    wins: int | None = None
    losses: int | None = None
    draws: int | None = None
    metrics: dict[str, float | None]
    stat_line: str | None = None
    # PR 3c: 0-100 league percentile rank per metric_key, populated only
    # for advanced metrics that have a cached league distribution.
    percentiles: dict[str, float] = Field(default_factory=dict)
    # PR 3c: tags each metric key as "basic" or "advanced" so the UI can
    # group them. Default empty so older callers (no augmentation) don't
    # get noise.
    metric_categories: dict[str, str] = Field(default_factory=dict)


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


class TeamHistoryRequest(BaseModel):
    team_name: str = Field(min_length=2)
    sport_key: str = "NBA"
    n: int = Field(default=5, ge=1, le=20)
    # Optional filters narrow the schedule before clipping to N. Both apply
    # independently — pass either or both. Unmatched results just shrink
    # the returned list.
    opponent: str | None = None
    location: Literal["home", "away"] | None = None


class TeamGameResultRead(BaseModel):
    game_date: UTCDateTime
    opponent: str
    opponent_abbreviation: str | None = None
    location: str  # "home" | "away"
    team_score: int
    opp_score: int
    result: str    # "W" | "L"


class TeamHistoryRead(BaseModel):
    entity_id: str
    team_name: str
    sport_key: str
    results: list[TeamGameResultRead]
