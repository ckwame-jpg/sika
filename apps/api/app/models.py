from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Sport(Base):
    __tablename__ = "sports"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)


class League(Base):
    __tablename__ = "leagues"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(String, unique=True, nullable=True, index=True)
    sport_key = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False, index=True)
    raw_data = Column(JSON, default=dict)


class Participant(Base):
    __tablename__ = "participants"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(String, unique=True, nullable=True, index=True)
    sport_key = Column(String, nullable=False, index=True)
    display_name = Column(String, nullable=False, index=True)
    short_name = Column(String, nullable=True)
    participant_type = Column(String, nullable=False, default="competitor")
    raw_data = Column(JSON, default=dict)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(String, unique=True, nullable=False, index=True)
    sport_key = Column(String, nullable=False, index=True)
    league_id = Column(Integer, ForeignKey("leagues.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    status = Column(String, nullable=False, default="scheduled")
    starts_at = Column(DateTime(timezone=True), nullable=False, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    raw_data = Column(JSON, default=dict)

    league = relationship("League")
    participants = relationship("EventParticipant", back_populates="event", cascade="all, delete-orphan")


class EventParticipant(Base):
    __tablename__ = "event_participants"
    __table_args__ = (UniqueConstraint("event_id", "participant_id", name="uq_event_participant"),)

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    participant_id = Column(Integer, ForeignKey("participants.id"), nullable=False, index=True)
    role = Column(String, nullable=False)
    is_home = Column(Boolean, nullable=False, default=False)
    is_favorite = Column(Boolean, nullable=True)
    score = Column(Float, nullable=True)
    result = Column(String, nullable=True)
    raw_data = Column(JSON, default=dict)

    event = relationship("Event", back_populates="participants")
    participant = relationship("Participant")


class Market(Base):
    __tablename__ = "markets"

    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, unique=True, nullable=False, index=True)
    series_ticker = Column(String, nullable=True, index=True)
    event_ticker = Column(String, nullable=True, index=True)
    sport_key = Column(String, nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    title = Column(String, nullable=False)
    subtitle = Column(String, nullable=True)
    status = Column(String, nullable=False, default="open")
    close_time = Column(DateTime(timezone=True), nullable=True, index=True)
    raw_data = Column(JSON, default=dict)
    # Bug #17: persist the fuzzy-match score + top candidates the
    # auto-mapper considered, so ops can review ambiguous cases
    # instead of trusting a silent best-match. ``mapping_overridden_at``
    # marks a manual override (via /ops/market-mapping/{ticker}); the
    # auto-mapper skips overridden rows so subsequent runs don't
    # clobber them.
    mapping_confidence = Column(Float, nullable=True)
    mapping_candidates = Column(JSON, nullable=True)
    mapping_overridden_at = Column(DateTime(timezone=True), nullable=True)
    mapping_overridden_reason = Column(Text, nullable=True)

    event = relationship("Event")
    snapshots = relationship("MarketSnapshot", back_populates="market", cascade="all, delete-orphan")


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    yes_bid = Column(Float, nullable=True)
    yes_ask = Column(Float, nullable=True)
    no_bid = Column(Float, nullable=True)
    no_ask = Column(Float, nullable=True)
    last_price = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    open_interest = Column(Float, nullable=True)
    raw_data = Column(JSON, default=dict)

    market = relationship("Market", back_populates="snapshots")


class SignalSnapshot(Base):
    __tablename__ = "signal_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=True, index=True)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    model_name = Column(String, nullable=False, default="heuristic-v1")
    model_version = Column(String, nullable=True)
    calibration_version = Column(String, nullable=True)
    feature_set_version = Column(String, nullable=True)
    model_metadata = Column(JSON, default=dict)
    confidence = Column(Float, nullable=False)
    fair_yes_price = Column(Float, nullable=False)
    fair_no_price = Column(Float, nullable=False)
    edge = Column(Float, nullable=False)
    selection_score = Column(Float, nullable=True)
    reasons = Column(JSON, default=list)
    features = Column(JSON, default=dict)
    scoring_diagnostics = Column(JSON, default=dict)

    event = relationship("Event")
    market = relationship("Market")


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    side = Column(String, nullable=False)
    action = Column(String, nullable=False, default="buy")
    status = Column(String, nullable=False, default="active")
    suggested_price = Column(Float, nullable=False)
    edge = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    selection_score = Column(Float, nullable=True, index=True)
    model_name = Column(String, nullable=True, index=True)
    model_version = Column(String, nullable=True)
    calibration_version = Column(String, nullable=True)
    feature_set_version = Column(String, nullable=True)
    model_metadata = Column(JSON, default=dict)
    invalidation = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
    scoring_diagnostics = Column(JSON, default=dict)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    event = relationship("Event")
    market = relationship("Market")


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (UniqueConstraint("run_id", "market_id", name="uq_prediction_run_market"),)

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    ticker = Column(String, nullable=False, index=True)
    sport_key = Column(String, nullable=True, index=True)
    event_name = Column(String, nullable=True)
    market_title = Column(String, nullable=False)
    market_family = Column(String, nullable=True, index=True)
    market_kind = Column(String, nullable=True, index=True)
    stat_key = Column(String, nullable=True, index=True)
    threshold = Column(Float, nullable=True)
    subject_name = Column(String, nullable=True, index=True)
    subject_team = Column(String, nullable=True, index=True)
    capture_scope = Column(String, nullable=False, default="recommendation", index=True)
    side = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False, default="buy")
    suggested_price = Column(Float, nullable=False)
    fair_yes_price = Column(Float, nullable=True)
    fair_no_price = Column(Float, nullable=True)
    edge = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    selection_score = Column(Float, nullable=True, index=True)
    model_name = Column(String, nullable=False, default="heuristic-v1")
    model_version = Column(String, nullable=True)
    calibration_version = Column(String, nullable=True)
    feature_set_version = Column(String, nullable=True)
    model_metadata = Column(JSON, default=dict)
    invalidation = Column(Text, nullable=True)
    rationale = Column(Text, nullable=False)
    reasons = Column(JSON, default=list)
    features = Column(JSON, default=dict)
    scoring_diagnostics = Column(JSON, default=dict)
    market_status_at_capture = Column(String, nullable=True)
    settlement_status = Column(String, nullable=False, default="pending", index=True)
    prediction_outcome = Column(String, nullable=False, default="pending", index=True)
    market_result = Column(String, nullable=True)
    winning_side = Column(String, nullable=True)
    settlement_value = Column(Float, nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True, index=True)
    realized_pnl = Column(Float, nullable=True)
    settlement_source = Column(String, nullable=True)
    settlement_notes = Column(Text, nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    # Smarter #3 (closing-line value): captured at settlement.
    # ``closing_yes_price`` is the YES mid-price (or last traded price) from
    # the latest ``MarketSnapshot`` before ``market.close_time``.
    # ``closing_line_value`` is the signed move in the pick's favor:
    # YES → ``closing_yes_price - suggested_price``;
    # NO → ``(1 - closing_yes_price) - suggested_price``.
    # Positive CLV is the standard sharpness signal — the line moved toward
    # sika's recommendation between capture and close.
    closing_yes_price = Column(Float, nullable=True)
    closing_line_value = Column(Float, nullable=True)

    run = relationship("Run")
    event = relationship("Event")
    market = relationship("Market")


class ParlayRecommendation(Base):
    __tablename__ = "parlay_recommendations"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    leg_count = Column(Integer, nullable=False, index=True)
    sport_scope = Column(String, nullable=False, default="MIXED", index=True)
    participating_sports = Column(JSON, default=list)
    status = Column(String, nullable=False, default="active")
    combined_market_price = Column(Float, nullable=False)
    combined_model_probability = Column(Float, nullable=False)
    american_odds = Column(String, nullable=False)
    edge = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    selection_score = Column(Float, nullable=True, index=True)
    model_name = Column(String, nullable=True, index=True)
    model_version = Column(String, nullable=True)
    calibration_version = Column(String, nullable=True)
    feature_set_version = Column(String, nullable=True)
    model_metadata = Column(JSON, default=dict)
    invalidation = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
    scoring_diagnostics = Column(JSON, default=dict)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    run = relationship("Run")
    legs = relationship(
        "ParlayRecommendationLeg",
        back_populates="parlay",
        cascade="all, delete-orphan",
        order_by="ParlayRecommendationLeg.leg_index",
    )


class ParlayRecommendationLeg(Base):
    __tablename__ = "parlay_recommendation_legs"

    id = Column(Integer, primary_key=True, index=True)
    parlay_recommendation_id = Column(Integer, ForeignKey("parlay_recommendations.id"), nullable=False, index=True)
    leg_index = Column(Integer, nullable=False)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    ticker = Column(String, nullable=False, index=True)
    sport_key = Column(String, nullable=True, index=True)
    event_name = Column(String, nullable=True)
    market_title = Column(String, nullable=False)
    market_family = Column(String, nullable=True)
    market_kind = Column(String, nullable=True)
    stat_key = Column(String, nullable=True)
    threshold = Column(Float, nullable=True)
    subject_name = Column(String, nullable=True)
    subject_team = Column(String, nullable=True)
    side = Column(String, nullable=False)
    action = Column(String, nullable=False, default="buy")
    suggested_price = Column(Float, nullable=False)
    fair_yes_price = Column(Float, nullable=True)
    fair_no_price = Column(Float, nullable=True)
    edge = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)

    parlay = relationship("ParlayRecommendation", back_populates="legs")
    event = relationship("Event")
    market = relationship("Market")


class ParlayPrediction(Base):
    __tablename__ = "parlay_predictions"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    leg_count = Column(Integer, nullable=False, index=True)
    sport_scope = Column(String, nullable=False, default="MIXED", index=True)
    participating_sports = Column(JSON, default=list)
    combined_market_price = Column(Float, nullable=False)
    combined_model_probability = Column(Float, nullable=False)
    american_odds = Column(String, nullable=False)
    edge = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    selection_score = Column(Float, nullable=True, index=True)
    model_name = Column(String, nullable=True, index=True)
    model_version = Column(String, nullable=True)
    calibration_version = Column(String, nullable=True)
    feature_set_version = Column(String, nullable=True)
    model_metadata = Column(JSON, default=dict)
    rationale = Column(Text, nullable=False)
    invalidation = Column(Text, nullable=True)
    scoring_diagnostics = Column(JSON, default=dict)
    settlement_status = Column(String, nullable=False, default="pending", index=True)
    prediction_outcome = Column(String, nullable=False, default="pending", index=True)
    settlement_value = Column(Float, nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True, index=True)
    realized_pnl = Column(Float, nullable=True)
    settlement_notes = Column(Text, nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    run = relationship("Run")
    legs = relationship(
        "ParlayPredictionLeg",
        back_populates="parlay",
        cascade="all, delete-orphan",
        order_by="ParlayPredictionLeg.leg_index",
    )


class ParlayPredictionLeg(Base):
    __tablename__ = "parlay_prediction_legs"

    id = Column(Integer, primary_key=True, index=True)
    parlay_prediction_id = Column(Integer, ForeignKey("parlay_predictions.id"), nullable=False, index=True)
    leg_index = Column(Integer, nullable=False)
    source_prediction_id = Column(Integer, ForeignKey("predictions.id"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    ticker = Column(String, nullable=False, index=True)
    sport_key = Column(String, nullable=True, index=True)
    event_name = Column(String, nullable=True)
    market_title = Column(String, nullable=False)
    market_family = Column(String, nullable=True)
    market_kind = Column(String, nullable=True)
    stat_key = Column(String, nullable=True)
    threshold = Column(Float, nullable=True)
    subject_name = Column(String, nullable=True)
    subject_team = Column(String, nullable=True)
    side = Column(String, nullable=False)
    action = Column(String, nullable=False, default="buy")
    suggested_price = Column(Float, nullable=False)
    fair_yes_price = Column(Float, nullable=True)
    fair_no_price = Column(Float, nullable=True)
    edge = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)

    parlay = relationship("ParlayPrediction", back_populates="legs")
    source_prediction = relationship("Prediction")
    event = relationship("Event")
    market = relationship("Market")


class ShadowInference(Base):
    __tablename__ = "shadow_inferences"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    source_prediction_id = Column(Integer, ForeignKey("predictions.id"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=True, index=True)
    ticker = Column(String, nullable=False, index=True)
    sport_key = Column(String, nullable=True, index=True)
    event_name = Column(String, nullable=True)
    market_title = Column(String, nullable=True)
    market_family = Column(String, nullable=True, index=True)
    market_kind = Column(String, nullable=True, index=True)
    stat_key = Column(String, nullable=True, index=True)
    threshold = Column(Float, nullable=True)
    subject_name = Column(String, nullable=True, index=True)
    subject_team = Column(String, nullable=True, index=True)
    inference_scope = Column(String, nullable=False, default="single", index=True)
    recommended_side = Column(String, nullable=True)
    suggested_price = Column(Float, nullable=True)
    fair_yes_price = Column(Float, nullable=True)
    fair_no_price = Column(Float, nullable=True)
    edge = Column(Float, nullable=True)
    confidence = Column(Float, nullable=False)
    model_name = Column(String, nullable=False, index=True)
    model_version = Column(String, nullable=True)
    calibration_version = Column(String, nullable=True)
    feature_set_version = Column(String, nullable=True)
    model_metadata = Column(JSON, default=dict)
    rationale = Column(Text, nullable=True)
    reasons = Column(JSON, default=list)
    features = Column(JSON, default=dict)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    run = relationship("Run")
    source_prediction = relationship("Prediction")
    event = relationship("Event")
    market = relationship("Market")


class ShadowParlayInference(Base):
    __tablename__ = "shadow_parlay_inferences"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    source_parlay_prediction_id = Column(Integer, ForeignKey("parlay_predictions.id"), nullable=True, index=True)
    leg_count = Column(Integer, nullable=False, index=True)
    sport_scope = Column(String, nullable=False, default="MIXED", index=True)
    participating_sports = Column(JSON, default=list)
    leg_tickers = Column(JSON, default=list)
    combined_market_price = Column(Float, nullable=True)
    combined_model_probability = Column(Float, nullable=False)
    edge = Column(Float, nullable=True)
    confidence = Column(Float, nullable=False)
    model_name = Column(String, nullable=False, index=True)
    model_version = Column(String, nullable=True)
    calibration_version = Column(String, nullable=True)
    feature_set_version = Column(String, nullable=True)
    model_metadata = Column(JSON, default=dict)
    rationale = Column(Text, nullable=True)
    features = Column(JSON, default=dict)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    run = relationship("Run")
    source_parlay_prediction = relationship("ParlayPrediction")


class EspnPlayerSearchCache(Base):
    __tablename__ = "espn_player_search_cache"
    __table_args__ = (UniqueConstraint("sport_key", "query_normalized", name="uq_espn_player_search_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    sport_key = Column(String, nullable=False, index=True)
    query_normalized = Column(String, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class EspnPlayerGamelogCache(Base):
    __tablename__ = "espn_player_gamelog_cache"
    __table_args__ = (UniqueConstraint("sport_key", "athlete_id", "season", name="uq_espn_player_gamelog_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    sport_key = Column(String, nullable=False, index=True)
    athlete_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaAdvancedGamelogCache(Base):
    __tablename__ = "nba_advanced_gamelog_cache"
    __table_args__ = (UniqueConstraint("athlete_id", "season", name="uq_nba_advanced_gamelog_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaTeamAdvancedCache(Base):
    __tablename__ = "nba_team_advanced_cache"
    __table_args__ = (UniqueConstraint("team_id", "season", name="uq_nba_team_advanced_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaLeaguePercentilesCache(Base):
    __tablename__ = "nba_league_percentiles_cache"
    __table_args__ = (UniqueConstraint("season", "metric_key", name="uq_nba_league_percentiles_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    season = Column(Integer, nullable=False, index=True)
    metric_key = Column(String, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaTeamGamelogCache(Base):
    __tablename__ = "nba_team_gamelog_cache"
    __table_args__ = (UniqueConstraint("team_id", "season", name="uq_nba_team_gamelog_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaLineupAdvancedCache(Base):
    __tablename__ = "nba_lineup_advanced_cache"
    __table_args__ = (UniqueConstraint("season", "group_quantity", name="uq_nba_lineup_advanced_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    season = Column(Integer, nullable=False, index=True)
    group_quantity = Column(Integer, nullable=False, index=True, default=5)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaBoxscoreAdvancedCache(Base):
    __tablename__ = "nba_boxscore_advanced_cache"
    __table_args__ = (UniqueConstraint("game_id", name="uq_nba_boxscore_advanced_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    game_id = Column(String, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaPlayerRosterCache(Base):
    __tablename__ = "nba_player_roster_cache"
    __table_args__ = (UniqueConstraint("season", name="uq_nba_player_roster_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbBatterAdvancedCache(Base):
    __tablename__ = "mlb_batter_advanced_cache"
    __table_args__ = (UniqueConstraint("athlete_id", "season", name="uq_mlb_batter_advanced_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbPitcherAdvancedCache(Base):
    __tablename__ = "mlb_pitcher_advanced_cache"
    __table_args__ = (UniqueConstraint("athlete_id", "season", name="uq_mlb_pitcher_advanced_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbStatcastBatterCache(Base):
    __tablename__ = "mlb_statcast_batter_cache"
    __table_args__ = (UniqueConstraint("athlete_id", "season", name="uq_mlb_statcast_batter_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbStatcastPitcherCache(Base):
    __tablename__ = "mlb_statcast_pitcher_cache"
    __table_args__ = (UniqueConstraint("athlete_id", "season", name="uq_mlb_statcast_pitcher_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbPlayerSplitsCache(Base):
    __tablename__ = "mlb_player_splits_cache"
    __table_args__ = (
        UniqueConstraint("athlete_id", "season", "split_kind", name="uq_mlb_player_splits_cache"),
    )

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    split_kind = Column(String, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbTeamGamelogCache(Base):
    __tablename__ = "mlb_team_gamelog_cache"
    __table_args__ = (UniqueConstraint("team_id", "season", name="uq_mlb_team_gamelog_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(String, nullable=False, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbBullpenStateCache(Base):
    __tablename__ = "mlb_bullpen_state_cache"
    __table_args__ = (UniqueConstraint("team_id", "game_date", name="uq_mlb_bullpen_state_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(String, nullable=False, index=True)
    game_date = Column(String, nullable=False, index=True)  # YYYY-MM-DD
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbLineupCache(Base):
    __tablename__ = "mlb_lineup_cache"
    __table_args__ = (UniqueConstraint("event_id", name="uq_mlb_lineup_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbWeatherCache(Base):
    __tablename__ = "mlb_weather_cache"
    __table_args__ = (UniqueConstraint("event_id", name="uq_mlb_weather_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbPlayerRosterCache(Base):
    __tablename__ = "mlb_player_roster_cache"
    __table_args__ = (UniqueConstraint("season", name="uq_mlb_player_roster_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbLeaguePercentilesCache(Base):
    __tablename__ = "mlb_league_percentiles_cache"
    __table_args__ = (
        UniqueConstraint("season", "metric_key", name="uq_mlb_league_percentiles_cache"),
    )

    id = Column(Integer, primary_key=True, index=True)
    season = Column(Integer, nullable=False, index=True)
    metric_key = Column(String, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class MlbInjuryReportCache(Base):
    __tablename__ = "mlb_injury_report_cache"
    __table_args__ = (
        UniqueConstraint("team_id", "fetched_date", name="uq_mlb_injury_report_cache"),
    )

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(String, nullable=False, index=True)
    fetched_date = Column(String, nullable=False, index=True)  # YYYY-MM-DD
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaHustlePlayerCache(Base):
    __tablename__ = "nba_hustle_player_cache"
    __table_args__ = (UniqueConstraint("season", name="uq_nba_hustle_player_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaTrackingCache(Base):
    __tablename__ = "nba_tracking_cache"
    __table_args__ = (
        UniqueConstraint("season", "pt_measure_type", name="uq_nba_tracking_cache"),
    )

    id = Column(Integer, primary_key=True, index=True)
    season = Column(Integer, nullable=False, index=True)
    pt_measure_type = Column(String, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaClutchPlayerCache(Base):
    __tablename__ = "nba_clutch_player_cache"
    __table_args__ = (UniqueConstraint("season", name="uq_nba_clutch_player_cache"),)

    id = Column(Integer, primary_key=True, index=True)
    season = Column(Integer, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaPlayerDefenseCache(Base):
    __tablename__ = "nba_player_defense_cache"
    __table_args__ = (
        UniqueConstraint("season", "defense_category", name="uq_nba_player_defense_cache"),
    )

    id = Column(Integer, primary_key=True, index=True)
    season = Column(Integer, nullable=False, index=True)
    defense_category = Column(String, nullable=False, index=True)
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaInjuryReportCache(Base):
    __tablename__ = "nba_injury_report_cache"
    __table_args__ = (
        UniqueConstraint("fetched_date", name="uq_nba_injury_report_cache"),
    )

    id = Column(Integer, primary_key=True, index=True)
    fetched_date = Column(String, nullable=False, index=True)  # YYYY-MM-DD
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class NbaRefereeAssignmentCache(Base):
    """Smarter #13 phase 2a — cached NBA referee assignments per day.

    Keyed by ``fetched_date`` (UTC YYYY-MM-DD), mirroring
    ``NbaInjuryReportCache``. NBA posts referee assignments the
    afternoon-of (typically around 5pm ET) for that night's games;
    a 4-hour TTL refreshes ~6x per day so the publication window
    is caught without hammering official.nba.com.

    ``payload`` stores the serialized ``NbaRefereeAssignmentDay``
    (page_date + per-game crew slots). The consumer side (Phase
    2b/c/d) joins this with per-referee tendency stats to produce
    scoring features.
    """
    __tablename__ = "nba_referee_assignment_cache"
    __table_args__ = (
        UniqueConstraint("fetched_date", name="uq_nba_referee_assignment_cache"),
    )

    id = Column(Integer, primary_key=True, index=True)
    fetched_date = Column(String, nullable=False, index=True)  # YYYY-MM-DD
    payload = Column(JSON, default=dict)
    cached_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class ModelFamilyRuntimeHealth(Base):
    __tablename__ = "model_family_runtime_health"

    id = Column(Integer, primary_key=True, index=True)
    family_key = Column(String, nullable=False, unique=True, index=True)
    desired_mode = Column(String, nullable=False, default="heuristic")
    effective_mode = Column(String, nullable=False, default="heuristic")
    runtime_health = Column(String, nullable=False, default="unavailable")
    fallback_active = Column(Boolean, nullable=False, default=False)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    last_check_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    degraded_until = Column(DateTime(timezone=True), nullable=True)
    artifact_path = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    model_version = Column(String, nullable=True)
    calibration_version = Column(String, nullable=True)
    feature_set_version = Column(String, nullable=True)
    model_metadata = Column(JSON, default=dict)
    promotion_mode = Column(String, nullable=True)
    promotion_stability_days = Column(Integer, nullable=True)
    promotion_baseline_brier = Column(Float, nullable=True)
    promotion_metrics = Column(JSON, default=dict)
    promotion_updated_at = Column(DateTime(timezone=True), nullable=True)


class OperatorSetting(Base):
    __tablename__ = "operator_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, nullable=False, unique=True, index=True)
    value = Column(JSON, default=dict)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class NarratorOutputCache(Base):
    """Smarter #31 — cached LLM narration per recommendation.

    Keyed by ``recommendation_id`` so a single recommendation gets at
    most one narration. ``verifier_passed`` indicates whether the
    grounding-verifier accepted the output; consumers should only
    surface narrations where this is True (the operator-facing rationale
    falls back to the mechanical version otherwise).
    """
    __tablename__ = "narrator_output_cache"
    __table_args__ = (
        UniqueConstraint("recommendation_id", name="uq_narrator_output_recommendation"),
    )

    id = Column(Integer, primary_key=True, index=True)
    recommendation_id = Column(
        Integer, ForeignKey("recommendations.id"), nullable=False, index=True
    )
    text = Column(Text, nullable=False)
    verifier_passed = Column(Boolean, nullable=False, default=True)
    rejected_claims = Column(JSON, default=list)
    model_name = Column(String, nullable=False)
    generated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    ticker = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    status = Column(String, nullable=False, default="open")
    opened_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    pnl = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)

    market = relationship("Market")


class DemoOrder(Base):
    __tablename__ = "demo_orders"

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=True, index=True)
    ticker = Column(String, nullable=False, index=True)
    client_order_id = Column(String, unique=True, nullable=False, index=True)
    kalshi_order_id = Column(String, nullable=True, index=True)
    side = Column(String, nullable=False)
    action = Column(String, nullable=False, default="buy")
    quantity = Column(Integer, nullable=False)
    limit_price = Column(Float, nullable=False)
    status = Column(String, nullable=False, default="pending_submission")
    approved_by_user = Column(Boolean, nullable=False, default=False)
    request_body = Column(JSON, default=dict)
    response_body = Column(JSON, default=dict)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)

    market = relationship("Market")
    fills = relationship("DemoFill", back_populates="order", cascade="all, delete-orphan")


class OutboxEntry(Base):
    """Transactional outbox for side-effecting work (bug #31).

    Records an intent to perform an external action (today: Kalshi demo
    order submit + cancel) atomically with the local DB write that
    motivated it. A background worker drains pending entries with
    retries + dead-lettering, so the local DB and Kalshi can never
    diverge silently — either both reflect the intent or only the local
    side does (and the outbox row exposes the divergence to be
    reconciled).

    The ``intent_kind`` discriminates which handler runs; ``payload`` is
    a JSON blob of handler-specific args. ``target_kind`` + ``target_id``
    let a handler look up the row whose lifecycle this entry tracks
    (e.g., the ``DemoOrder`` to mark as submitted on success).
    """

    __tablename__ = "outbox_entries"

    id = Column(Integer, primary_key=True, index=True)
    intent_kind = Column(String, nullable=False, index=True)
    target_kind = Column(String, nullable=True, index=True)
    target_id = Column(Integer, nullable=True, index=True)
    payload = Column(JSON, default=dict, nullable=False)
    status = Column(String, nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class DemoFill(Base):
    __tablename__ = "demo_fills"

    id = Column(Integer, primary_key=True, index=True)
    demo_order_id = Column(Integer, ForeignKey("demo_orders.id"), nullable=False, index=True)
    kalshi_fill_id = Column(String, unique=True, nullable=True, index=True)
    count = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    side = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    raw_data = Column(JSON, default=dict)

    order = relationship("DemoOrder", back_populates="fills")


class Run(Base):
    __tablename__ = "runs"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String, nullable=False)
    status = Column(String, nullable=False, default="running")
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    details = Column(JSON, default=dict)
    records_processed = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)


class RefreshJob(Base):
    __tablename__ = "refresh_jobs"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String, nullable=False, index=True)
    scope = Column(String, nullable=False, index=True)
    reason = Column(String, nullable=False, default="manual", index=True)
    status = Column(String, nullable=False, default="queued", index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    error_message = Column(Text, nullable=True)
    details = Column(JSON, default=dict)
    queued_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True, index=True)

    run = relationship("Run")


class CurrentSlateSnapshot(Base):
    __tablename__ = "current_slate_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    scope = Column(String, nullable=False, index=True)
    source_run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    generated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    payload = Column(JSON, default=dict)

    source_run = relationship("Run")

    __table_args__ = (
        Index(
            "ix_current_slate_snapshots_scope_generated_at",
            "scope",
            "generated_at",
        ),
    )
