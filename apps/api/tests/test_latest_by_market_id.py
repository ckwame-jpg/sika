"""Latest-per-market helpers must order by ``captured_at``, not ``max(id)``.

Bug #8 from SIKA_PUNCH_LIST.md
==============================

``latest_snapshot_by_market_id`` / ``latest_recommendation_by_market_id``
/ ``latest_prediction_by_market_id`` historically used
``func.max(Model.id)`` to pick the latest row per market. That's only
correct while inserts are strictly monotonic by capture time. Real
production cases violate that invariant:

- The retry queue inserts old rows after newer ones.
- Backfill jobs insert historical rows into a live table.
- Concurrent workers can interleave insert order.

When the latest by ``captured_at`` has a lower ``id`` than a stale
row, the helpers serve the stale row to scoring. These tests insert
out-of-order rows and assert that ``captured_at`` wins.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Event, Market, MarketSnapshot, Prediction, Recommendation
from app.services.watchlist_coverage import (
    latest_prediction_by_market_id,
    latest_recommendation_by_market_id,
    latest_snapshot_by_market_id,
    recent_snapshots_by_market_id,
)


def _make_event(db_session, *, key: str) -> Event:
    event = Event(
        external_id=f"{key}-event",
        sport_key="NBA",
        name=f"{key} event",
        status="scheduled",
        starts_at=datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    return event


def _make_market(db_session, event: Event, ticker: str) -> Market:
    market = Market(
        ticker=ticker,
        sport_key="NBA",
        event_id=event.id,
        title=f"{ticker} title",
        status="active",
        raw_data={"copilot_market_family": "winner"},
    )
    db_session.add(market)
    db_session.flush()
    return market


def _make_snapshot(db_session, market: Market, *, captured_at: datetime, yes_ask: float) -> MarketSnapshot:
    snapshot = MarketSnapshot(
        market=market,
        yes_ask=yes_ask,
        no_ask=round(1 - yes_ask, 4),
        last_price=yes_ask,
        captured_at=captured_at,
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


def _make_recommendation(db_session, market: Market, *, captured_at: datetime, edge: float) -> Recommendation:
    rec = Recommendation(
        event_id=market.event_id,
        market_id=market.id,
        side="yes",
        action="buy",
        status="active",
        suggested_price=0.5,
        edge=edge,
        confidence=0.6,
        selection_score=edge + 0.6,
        invalidation="test",
        rationale="test",
        captured_at=captured_at,
        scoring_diagnostics={},
    )
    db_session.add(rec)
    db_session.flush()
    return rec


def _make_prediction(db_session, market: Market, *, captured_at: datetime, edge: float) -> Prediction:
    prediction = Prediction(
        event_id=market.event_id,
        market_id=market.id,
        ticker=market.ticker,
        sport_key="NBA",
        event_name=market.event.name if market.event else "test",
        market_title=market.title,
        market_family="winner",
        market_kind="game_winner",
        side="yes",
        action="buy",
        suggested_price=0.5,
        fair_yes_price=0.6,
        fair_no_price=0.4,
        edge=edge,
        confidence=0.6,
        model_name="heuristic-v1",
        rationale="test",
        market_status_at_capture="active",
        selection_score=edge + 0.6,
        scoring_diagnostics={},
        captured_at=captured_at,
    )
    db_session.add(prediction)
    db_session.flush()
    return prediction


def test_latest_snapshot_picks_by_captured_at_not_max_id(db_session):
    """Insert two snapshots: row with the higher id has the EARLIER
    captured_at. The helper must return the row with the later
    captured_at — even though it has the lower id."""
    event = _make_event(db_session, key="snap-out-of-order")
    market = _make_market(db_session, event, ticker="SNAP-OOO")

    later_capture = datetime(2026, 4, 5, 19, 0, tzinfo=timezone.utc)
    earlier_capture = datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc)

    # Insert the LATER snapshot first so it gets the LOWER id.
    later_snapshot = _make_snapshot(db_session, market, captured_at=later_capture, yes_ask=0.55)
    earlier_snapshot = _make_snapshot(db_session, market, captured_at=earlier_capture, yes_ask=0.40)

    assert later_snapshot.id < earlier_snapshot.id  # sanity check on the setup
    db_session.commit()

    result = latest_snapshot_by_market_id(db_session, [market.id])
    assert result[market.id].id == later_snapshot.id
    # SQLite drops tz-info on round-trip; compare on the naive form.
    assert result[market.id].captured_at.replace(tzinfo=None) == later_capture.replace(tzinfo=None)


def test_latest_recommendation_picks_by_captured_at_not_max_id(db_session):
    event = _make_event(db_session, key="rec-out-of-order")
    market = _make_market(db_session, event, ticker="REC-OOO")
    later_capture = datetime(2026, 4, 5, 19, 0, tzinfo=timezone.utc)
    earlier_capture = datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc)
    later_rec = _make_recommendation(db_session, market, captured_at=later_capture, edge=0.10)
    earlier_rec = _make_recommendation(db_session, market, captured_at=earlier_capture, edge=0.05)
    assert later_rec.id < earlier_rec.id
    db_session.commit()

    result = latest_recommendation_by_market_id(db_session, [market.id])
    assert result[market.id].id == later_rec.id
    assert result[market.id].edge == pytest.approx(0.10)


def test_latest_prediction_picks_by_captured_at_not_max_id(db_session):
    event = _make_event(db_session, key="pred-out-of-order")
    market = _make_market(db_session, event, ticker="PRED-OOO")
    later_capture = datetime(2026, 4, 5, 19, 0, tzinfo=timezone.utc)
    earlier_capture = datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc)
    later_pred = _make_prediction(db_session, market, captured_at=later_capture, edge=0.10)
    earlier_pred = _make_prediction(db_session, market, captured_at=earlier_capture, edge=0.05)
    assert later_pred.id < earlier_pred.id
    db_session.commit()

    result = latest_prediction_by_market_id(db_session, [market.id])
    assert result[market.id].id == later_pred.id
    assert result[market.id].edge == pytest.approx(0.10)


def test_latest_helpers_tiebreak_on_id_when_captured_at_is_identical(db_session):
    """When two rows share the same ``captured_at`` (rare but possible),
    fall back to the higher id as the deterministic tiebreaker."""
    event = _make_event(db_session, key="tie")
    market = _make_market(db_session, event, ticker="TIE-MKT")
    shared_capture = datetime(2026, 4, 5, 19, 0, tzinfo=timezone.utc)
    first = _make_snapshot(db_session, market, captured_at=shared_capture, yes_ask=0.40)
    second = _make_snapshot(db_session, market, captured_at=shared_capture, yes_ask=0.55)
    assert first.id < second.id
    db_session.commit()

    result = latest_snapshot_by_market_id(db_session, [market.id])
    assert result[market.id].id == second.id


def test_latest_helpers_handle_multiple_markets_independently(db_session):
    """Regression guard: out-of-order inserts on one market must not
    affect the latest-per-market answer for a different market."""
    event = _make_event(db_session, key="multi")
    market_a = _make_market(db_session, event, ticker="MULTI-A")
    market_b = _make_market(db_session, event, ticker="MULTI-B")
    # Market A: later snapshot has lower id (out of order)
    a_later = _make_snapshot(db_session, market_a, captured_at=datetime(2026, 4, 5, 19, 0, tzinfo=timezone.utc), yes_ask=0.60)
    a_earlier = _make_snapshot(db_session, market_a, captured_at=datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc), yes_ask=0.40)
    # Market B: normal order
    b_earlier = _make_snapshot(db_session, market_b, captured_at=datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc), yes_ask=0.45)
    b_later = _make_snapshot(db_session, market_b, captured_at=datetime(2026, 4, 5, 19, 0, tzinfo=timezone.utc), yes_ask=0.65)
    db_session.commit()

    result = latest_snapshot_by_market_id(db_session, [market_a.id, market_b.id])
    assert result[market_a.id].id == a_later.id
    assert result[market_b.id].id == b_later.id


# ---------------------------------------------------------------------------
# Bug #37 — recent_snapshots_by_market_id (windowed last-N per market)
# ---------------------------------------------------------------------------


def test_recent_snapshots_returns_chronological_order(db_session):
    """The window-function helper must return rows oldest → newest so a
    sparkline can plot them left-to-right without reversing on the
    client."""
    event = _make_event(db_session, key="recent-chrono")
    market = _make_market(db_session, event, ticker="RECENT-CHRONO")
    times = [datetime(2026, 4, 5, 18 + offset, 0, tzinfo=timezone.utc) for offset in range(5)]
    # Insert in reverse-chronological order so the natural id order
    # disagrees with captured_at — proves the window function is doing
    # the work, not the row-id ordering.
    for ts in reversed(times):
        _make_snapshot(db_session, market, captured_at=ts, yes_ask=0.40 + (ts.hour - 18) * 0.05)
    db_session.commit()

    result = recent_snapshots_by_market_id(db_session, [market.id], limit_per_market=10)
    captured = [row.captured_at.replace(tzinfo=None) for row in result[market.id]]
    assert captured == [ts.replace(tzinfo=None) for ts in times]


def test_recent_snapshots_respects_per_market_limit(db_session):
    """Last-N cap must be applied per market, not across the whole
    result set — otherwise a busy market would starve a quiet one."""
    event = _make_event(db_session, key="recent-cap")
    busy = _make_market(db_session, event, ticker="RECENT-BUSY")
    quiet = _make_market(db_session, event, ticker="RECENT-QUIET")
    base = datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
    for offset in range(8):
        _make_snapshot(db_session, busy, captured_at=base + timedelta(minutes=offset * 5), yes_ask=0.50)
    for offset in range(2):
        _make_snapshot(db_session, quiet, captured_at=base + timedelta(minutes=offset * 5), yes_ask=0.30)
    db_session.commit()

    result = recent_snapshots_by_market_id(db_session, [busy.id, quiet.id], limit_per_market=3)
    assert len(result[busy.id]) == 3
    assert len(result[quiet.id]) == 2  # fewer rows than the cap → all retained


def test_recent_snapshots_empty_inputs_short_circuit(db_session):
    assert recent_snapshots_by_market_id(db_session, [], limit_per_market=5) == {}


def test_recent_snapshots_zero_limit_returns_empty(db_session):
    """A non-positive cap is a degenerate input; return {} instead of
    issuing a query that would surface every snapshot."""
    event = _make_event(db_session, key="recent-zero")
    market = _make_market(db_session, event, ticker="RECENT-ZERO")
    _make_snapshot(db_session, market, captured_at=datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc), yes_ask=0.5)
    db_session.commit()
    assert recent_snapshots_by_market_id(db_session, [market.id], limit_per_market=0) == {}
    assert recent_snapshots_by_market_id(db_session, [market.id], limit_per_market=-3) == {}
