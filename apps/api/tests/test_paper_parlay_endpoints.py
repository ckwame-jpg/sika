"""HTTP-level tests for the paper-parlay endpoints (PAPER_PARLAY_SCOPE.md step 3).

Covers:
- POST /paper-parlays happy path → 200 with PaperParlayRead
- POST /paper-parlays validation rejections surface as 4xx
- GET /paper-parlays lists newest-first, supports status filter
- GET /paper-parlays rejects bad status filter (400)
- GET /positions surfaces paper_parlays alongside paper_positions

Service-layer logic (validation, joint prob, snapshots) is covered
exhaustively in test_paper_parlay_service.py; these tests focus on the
wiring — endpoint URL/method, response model serialization, query
param handling.
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Event, EventParticipant, Market, Participant, Prediction


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _add_event(db: Session, *, prefix: str) -> Event:
    home = Participant(
        external_id=f"{prefix}-home",
        sport_key="NBA",
        display_name="Cleveland Cavaliers",
        short_name="CLE",
        participant_type="team",
    )
    away = Participant(
        external_id=f"{prefix}-away",
        sport_key="NBA",
        display_name="Detroit Pistons",
        short_name="DET",
        participant_type="team",
    )
    db.add_all([home, away])
    db.flush()
    event = Event(
        external_id=f"{prefix}-event",
        sport_key="NBA",
        name="Detroit Pistons at Cleveland Cavaliers",
        status="scheduled",
        starts_at=_utcnow() + timedelta(hours=2),
    )
    db.add(event)
    db.flush()
    db.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
        ]
    )
    db.flush()
    return event


def _add_market(
    db: Session,
    *,
    event: Event,
    ticker: str,
    subject_name: str,
    subject_team: str,
) -> Market:
    market = Market(
        ticker=ticker,
        sport_key=event.sport_key,
        event_id=event.id,
        title=f"{subject_name} prop",
        status="active",
        raw_data={
            "copilot_subject_name": subject_name,
            "copilot_subject_team": subject_team,
            "copilot_stat_key": "points",
            "copilot_threshold": 25.0,
            "copilot_market_kind": "player_prop",
        },
    )
    db.add(market)
    db.flush()
    return market


def _add_prediction(db: Session, *, market: Market, side: str = "yes") -> Prediction:
    pred = Prediction(
        event_id=market.event_id,
        market_id=market.id,
        ticker=market.ticker,
        market_title=market.title,
        side=side,
        action="buy",
        suggested_price=0.55,
        fair_yes_price=0.60,
        fair_no_price=0.40,
        edge=0.05,
        confidence=0.80,
        rationale="test",
        captured_at=_utcnow(),
    )
    db.add(pred)
    db.flush()
    return pred


def _make_payload(*, tickers: list[str], stake: float = 100.0) -> dict:
    return {
        "stake": stake,
        "notes": "test",
        "legs": [
            {"ticker": ticker, "side": "yes", "suggested_price": 0.50}
            for ticker in tickers
        ],
    }


def test_post_paper_parlays_creates_and_returns_serialized_parlay(
    client: TestClient, db_session: Session
) -> None:
    event = _add_event(db_session, prefix="post-happy")
    market_a = _add_market(db_session, event=event, ticker="POST-A", subject_name="A", subject_team="X")
    market_b = _add_market(db_session, event=event, ticker="POST-B", subject_name="B", subject_team="Y")
    _add_prediction(db_session, market=market_a)
    _add_prediction(db_session, market=market_b)
    db_session.commit()

    response = client.post("/paper-parlays", json=_make_payload(tickers=["POST-A", "POST-B"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["leg_count"] == 2
    assert body["stake"] == 100.0
    assert body["settlement_status"] == "pending"
    assert body["outcome"] == "pending"
    assert len(body["legs"]) == 2
    assert {leg["ticker"] for leg in body["legs"]} == {"POST-A", "POST-B"}
    # combined_market_price = 0.50 * 0.50 = 0.25
    assert body["combined_market_price"] == 0.25


def test_post_paper_parlays_propagates_validation_errors_as_4xx(
    client: TestClient, db_session: Session
) -> None:
    event = _add_event(db_session, prefix="post-bad")
    market = _add_market(db_session, event=event, ticker="POST-BAD", subject_name="A", subject_team="X")
    _add_prediction(db_session, market=market)
    db_session.commit()

    # Unknown ticker → service raises 404; endpoint surfaces it.
    bad = _make_payload(tickers=["POST-BAD", "NOT-A-REAL-TICKER"])
    response = client.post("/paper-parlays", json=bad)
    assert response.status_code == 404
    assert "NOT-A-REAL-TICKER" in response.json()["detail"]

    # Schema-level rejection: 1 leg → 422 from Pydantic validation.
    response = client.post(
        "/paper-parlays",
        json={
            "stake": 10.0,
            "legs": [{"ticker": "POST-BAD", "side": "yes", "suggested_price": 0.5}],
        },
    )
    assert response.status_code == 422


def test_get_paper_parlays_lists_newest_first(client: TestClient, db_session: Session) -> None:
    event = _add_event(db_session, prefix="list")
    market_a = _add_market(db_session, event=event, ticker="LIST-A", subject_name="A", subject_team="X")
    market_b = _add_market(db_session, event=event, ticker="LIST-B", subject_name="B", subject_team="Y")
    _add_prediction(db_session, market=market_a)
    _add_prediction(db_session, market=market_b)
    db_session.commit()

    # Create two parlays back to back; the newer one should sort first.
    first = client.post("/paper-parlays", json=_make_payload(tickers=["LIST-A", "LIST-B"]))
    second = client.post("/paper-parlays", json=_make_payload(tickers=["LIST-B", "LIST-A"], stake=50.0))
    assert first.status_code == 200
    assert second.status_code == 200

    listing = client.get("/paper-parlays")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 2
    # Most recent (the 50-dollar one) sorts to index 0 — created_at DESC.
    assert rows[0]["stake"] == 50.0
    assert rows[1]["stake"] == 100.0


def test_get_paper_parlays_filters_by_settlement_status(
    client: TestClient, db_session: Session
) -> None:
    event = _add_event(db_session, prefix="filter")
    market_a = _add_market(db_session, event=event, ticker="FILT-A", subject_name="A", subject_team="X")
    market_b = _add_market(db_session, event=event, ticker="FILT-B", subject_name="B", subject_team="Y")
    _add_prediction(db_session, market=market_a)
    _add_prediction(db_session, market=market_b)
    db_session.commit()

    client.post("/paper-parlays", json=_make_payload(tickers=["FILT-A", "FILT-B"]))

    # All created parlays start pending; the filter should return them.
    pending = client.get("/paper-parlays", params={"settlement_status": "pending"})
    assert pending.status_code == 200
    assert len(pending.json()) == 1

    # No settled parlays yet → filter returns empty list, not 404.
    settled = client.get("/paper-parlays", params={"settlement_status": "settled"})
    assert settled.status_code == 200
    assert settled.json() == []


def test_get_paper_parlays_rejects_unknown_settlement_status(
    client: TestClient, db_session: Session
) -> None:
    """Codex pattern 6 (implicit data shape): the filter parameter is
    a closed set ('pending' | 'settled'); anything else surfaces as a
    400 instead of being silently coerced or returning everything."""
    response = client.get("/paper-parlays", params={"settlement_status": "weird"})
    assert response.status_code == 400


def test_get_positions_surfaces_paper_parlays_alongside_paper_positions(
    client: TestClient, db_session: Session
) -> None:
    """Step 3 also wires paper_parlays into the /positions aggregator
    so the portfolio page gets parlays in the same response as
    paper positions + demo orders. Existing consumers that don't
    read paper_parlays keep working (default empty list)."""
    event = _add_event(db_session, prefix="agg")
    market_a = _add_market(db_session, event=event, ticker="AGG-A", subject_name="A", subject_team="X")
    market_b = _add_market(db_session, event=event, ticker="AGG-B", subject_name="B", subject_team="Y")
    _add_prediction(db_session, market=market_a)
    _add_prediction(db_session, market=market_b)
    db_session.commit()

    # Empty state: /positions returns paper_parlays as [], truncated False.
    initial = client.get("/positions")
    assert initial.status_code == 200
    assert initial.json()["paper_parlays"] == []
    assert initial.json()["paper_parlays_truncated"] is False

    # After saving a parlay, the aggregator picks it up.
    client.post("/paper-parlays", json=_make_payload(tickers=["AGG-A", "AGG-B"]))
    after = client.get("/positions")
    assert after.status_code == 200
    parlays = after.json()["paper_parlays"]
    assert len(parlays) == 1
    assert parlays[0]["leg_count"] == 2
    # Existing fields are still present and well-shaped.
    assert "paper_positions" in after.json()
    assert "demo_orders" in after.json()
