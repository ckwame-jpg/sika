"""Tests for Smarter #26 — settled-outcome SLA aging buckets.

Covers:
- ``compute_settlement_aging`` correctly buckets pending predictions
  by hours-past-market-close.
- Markets without ``close_time`` are excluded (we don't know when they
  SHOULD have settled).
- Markets whose ``close_time`` is in the future are excluded (they
  haven't reached their settlement window yet).
- Predictions whose ``settlement_status`` is no longer ``pending`` are
  excluded (cancelled / settled / resolved rows don't count).
- The readiness summary surfaces the bucket counts.
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.models import Event, Market, Prediction
from app.services.ml.readiness import build_model_readiness_summary
from app.services.predictions import (
    SettlementAging,
    compute_settlement_aging,
)


# -- fixtures ------------------------------------------------------------


_NOW = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)


_event_counter = {"n": 0}


def _seed_event(db_session) -> Event:
    _event_counter["n"] += 1
    event = Event(
        sport_key="NBA",
        external_id=f"settlement-aging-evt-{_event_counter['n']}",
        name="Test Event",
        starts_at=_NOW - timedelta(hours=3),
        status="completed",
    )
    db_session.add(event)
    db_session.flush()
    return event


def _seed_pending_prediction(
    db_session,
    *,
    close_offset_hours: float | None,
    settlement_status: str = "pending",
    captured_offset_hours: float = 24.0,
) -> Prediction:
    """Create a Market + Prediction pair where ``close_time = _NOW -
    close_offset_hours``. Pass ``close_offset_hours=None`` for a market
    with no close_time."""
    event = _seed_event(db_session)
    _event_counter["n"] += 1
    ticker = f"settlement-aging-mkt-{_event_counter['n']}"
    close_time = None
    if close_offset_hours is not None:
        close_time = _NOW - timedelta(hours=close_offset_hours)
    market = Market(
        ticker=ticker,
        sport_key="NBA",
        event_id=event.id,
        title=f"{ticker} title",
        status="closed" if close_offset_hours and close_offset_hours > 0 else "active",
        close_time=close_time,
        raw_data={},
    )
    db_session.add(market)
    db_session.flush()
    prediction = Prediction(
        event_id=event.id,
        market_id=market.id,
        ticker=ticker,
        sport_key="NBA",
        market_title=market.title,
        side="yes",
        action="buy",
        suggested_price=0.5,
        edge=0.05,
        confidence=0.6,
        rationale="test",
        settlement_status=settlement_status,
        prediction_outcome="pending" if settlement_status == "pending" else "won",
        captured_at=_NOW - timedelta(hours=captured_offset_hours),
    )
    db_session.add(prediction)
    db_session.flush()
    return prediction


# -- compute_settlement_aging branches -----------------------------------


def test_aging_returns_zero_buckets_when_no_pending_predictions(db_session) -> None:
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging == SettlementAging(
        bucket_0_to_1h=0,
        bucket_1_to_6h=0,
        bucket_6_to_24h=0,
        bucket_beyond_24h=0,
        total_pending_past_close=0,
    )


def test_aging_buckets_a_recent_close(db_session) -> None:
    _seed_pending_prediction(db_session, close_offset_hours=0.5)
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.bucket_0_to_1h == 1
    assert aging.total_pending_past_close == 1


def test_aging_buckets_a_three_hour_close(db_session) -> None:
    _seed_pending_prediction(db_session, close_offset_hours=3.0)
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.bucket_1_to_6h == 1


def test_aging_buckets_an_eight_hour_close(db_session) -> None:
    _seed_pending_prediction(db_session, close_offset_hours=8.0)
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.bucket_6_to_24h == 1


def test_aging_buckets_a_two_day_close(db_session) -> None:
    _seed_pending_prediction(db_session, close_offset_hours=48.0)
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.bucket_beyond_24h == 1


def test_aging_boundaries_are_exclusive_on_the_upper_edge(db_session) -> None:
    # Exactly 1.0h past close → still in 0-1h bucket? The implementation
    # uses ``<`` for the upper bound so 1.0h falls into 1-6h.
    _seed_pending_prediction(db_session, close_offset_hours=1.0)
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.bucket_0_to_1h == 0
    assert aging.bucket_1_to_6h == 1


def test_aging_buckets_are_non_overlapping(db_session) -> None:
    # One row in each bucket — confirm each bucket counts exactly one.
    _seed_pending_prediction(db_session, close_offset_hours=0.2)
    _seed_pending_prediction(db_session, close_offset_hours=3.5)
    _seed_pending_prediction(db_session, close_offset_hours=12.0)
    _seed_pending_prediction(db_session, close_offset_hours=72.0)
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.bucket_0_to_1h == 1
    assert aging.bucket_1_to_6h == 1
    assert aging.bucket_6_to_24h == 1
    assert aging.bucket_beyond_24h == 1
    assert aging.total_pending_past_close == 4


def test_aging_excludes_markets_without_close_time(db_session) -> None:
    # Without a close_time we don't know when the prediction SHOULD
    # have settled — skip rather than guess.
    _seed_pending_prediction(db_session, close_offset_hours=None)
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.total_pending_past_close == 0


def test_aging_excludes_markets_with_future_close_time(db_session) -> None:
    # close_time in the future → market hasn't closed yet → don't count.
    _seed_pending_prediction(db_session, close_offset_hours=-2.0)  # close in 2h
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.total_pending_past_close == 0


def test_aging_excludes_already_settled_predictions(db_session) -> None:
    _seed_pending_prediction(
        db_session,
        close_offset_hours=8.0,
        settlement_status="settled",
    )
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.total_pending_past_close == 0


def test_aging_excludes_cancelled_predictions(db_session) -> None:
    _seed_pending_prediction(
        db_session,
        close_offset_hours=8.0,
        settlement_status="cancelled",
    )
    aging = compute_settlement_aging(db_session, now=_NOW)
    assert aging.total_pending_past_close == 0


def test_aging_handles_naive_close_times_from_sqlite(db_session) -> None:
    # SQLite drops tz info on read — close_time comes back naive. The
    # helper coerces to UTC; in Postgres both sides stay aware.
    aging = compute_settlement_aging(db_session, now=_NOW.replace(tzinfo=None))
    # Smoke test: empty result is fine, just verify no TypeError.
    assert aging.total_pending_past_close == 0


# -- readiness summary surface -------------------------------------------


def test_readiness_summary_includes_settlement_aging_zero_when_empty(db_session) -> None:
    summary = build_model_readiness_summary(db_session)
    assert "settlement_aging" in summary
    aging = summary["settlement_aging"]
    assert aging["bucket_0_to_1h"] == 0
    assert aging["bucket_1_to_6h"] == 0
    assert aging["bucket_6_to_24h"] == 0
    assert aging["bucket_beyond_24h"] == 0
    assert aging["total_pending_past_close"] == 0


def test_readiness_summary_surfaces_stuck_predictions(db_session) -> None:
    _seed_pending_prediction(db_session, close_offset_hours=8.0)
    _seed_pending_prediction(db_session, close_offset_hours=48.0)
    summary = build_model_readiness_summary(db_session)
    aging = summary["settlement_aging"]
    assert aging["bucket_6_to_24h"] == 1
    assert aging["bucket_beyond_24h"] == 1
    assert aging["total_pending_past_close"] == 2


def test_readiness_endpoint_surfaces_settlement_aging(
    client: TestClient, db_session
) -> None:
    _seed_pending_prediction(db_session, close_offset_hours=8.0)
    db_session.commit()
    response = client.get("/ops/models/readiness")
    assert response.status_code == 200
    payload = response.json()
    assert "settlement_aging" in payload
    assert payload["settlement_aging"]["bucket_6_to_24h"] == 1
    assert payload["settlement_aging"]["total_pending_past_close"] == 1
