from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    confidence = Column(Float, nullable=False)
    fair_yes_price = Column(Float, nullable=False)
    fair_no_price = Column(Float, nullable=False)
    edge = Column(Float, nullable=False)
    reasons = Column(JSON, default=list)
    features = Column(JSON, default=dict)

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
    invalidation = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
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
    side = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False, default="buy")
    suggested_price = Column(Float, nullable=False)
    fair_yes_price = Column(Float, nullable=True)
    fair_no_price = Column(Float, nullable=True)
    edge = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    model_name = Column(String, nullable=False, default="heuristic-v1")
    invalidation = Column(Text, nullable=True)
    rationale = Column(Text, nullable=False)
    reasons = Column(JSON, default=list)
    features = Column(JSON, default=dict)
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
    invalidation = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
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
    rationale = Column(Text, nullable=False)
    invalidation = Column(Text, nullable=True)
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
