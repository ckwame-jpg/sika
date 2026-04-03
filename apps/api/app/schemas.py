from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

from app.datetime_utils import ensure_utc_datetime, utc_isoformat


UTCDateTime = Annotated[
    datetime,
    BeforeValidator(ensure_utc_datetime),
    PlainSerializer(utc_isoformat, return_type=str, when_used="json"),
]


class HealthResponse(BaseModel):
    status: str
    environment: str
    scheduler_enabled: bool
    refresh_status: Literal["idle", "queued", "running", "failed"]
    refresh_reason: Literal["none", "startup", "interval", "manual", "pregame"]
    last_successful_refresh_at: UTCDateTime | None = None
    data_stale: bool
    refresh_error_message: str | None = None


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
    confidence: float
    fair_yes_price: float
    fair_no_price: float
    edge: float
    reasons: list[str]
    features: dict[str, Any]


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


class RunSummaryCounts(BaseModel):
    sports_records_ingested: dict[str, int] = Field(default_factory=dict)
    total_kalshi_markets_seen: int = 0
    supported_markets_kept: int = 0
    supported_nba_props_seen: int = 0
    supported_mlb_props_seen: int = 0
    mapped_markets: int = 0
    mapped_prop_markets: int = 0
    recommendations_emitted: int = 0
    predictions_captured: int = 0
    parlay_recommendations_emitted: int = 0
    parlay_predictions_captured: int = 0
    prediction_settlement_updated: int = 0
    parlay_prediction_settlement_updated: int = 0
    prediction_outcomes: dict[str, int] = Field(default_factory=dict)
    parlay_prediction_outcomes: dict[str, int] = Field(default_factory=dict)
    unsupported_prop_category_counts: dict[str, int] = Field(default_factory=dict)
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
    run_id: int
    status: str
    records_processed: int


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
    side: str
    action: str
    suggested_price: float
    fair_yes_price: float | None = None
    fair_no_price: float | None = None
    edge: float
    confidence: float
    model_name: str
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


class EventQuery(BaseModel):
    sport: str | None = None
    day: date | None = None


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
