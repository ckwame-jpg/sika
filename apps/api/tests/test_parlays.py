from datetime import datetime, timezone

from sqlalchemy import select

from app.models import Event, Market, ParlayPrediction, Prediction, Recommendation, SignalSnapshot
from app.services.parlays import ParlayCandidateInput, american_odds_from_probability, capture_parlay_artifacts, settle_parlay_predictions


def _make_candidate(
    db_session,
    *,
    sport_key: str,
    ticker: str,
    side: str,
    suggested_price: float,
    fair_yes_price: float,
    fair_no_price: float,
    edge: float,
    confidence: float,
) -> ParlayCandidateInput:
    event = Event(
        external_id=f"{ticker}-event",
        sport_key=sport_key,
        name=f"{ticker} event",
        status="scheduled",
        starts_at=datetime(2026, 4, 3, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()

    market = Market(
        ticker=ticker,
        sport_key=sport_key,
        event_id=event.id,
        title=f"{ticker} market",
        status="active",
        raw_data={
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
        },
    )
    db_session.add(market)
    db_session.flush()

    recommendation = Recommendation(
        event_id=event.id,
        market_id=market.id,
        side=side,
        action="buy",
        status="active",
        suggested_price=suggested_price,
        edge=edge,
        confidence=confidence,
        invalidation="Test invalidation",
        rationale="Test rationale",
        captured_at=datetime(2026, 4, 2, 23, 0, tzinfo=timezone.utc),
    )
    signal = SignalSnapshot(
        event_id=event.id,
        market_id=market.id,
        captured_at=datetime(2026, 4, 2, 23, 0, tzinfo=timezone.utc),
        model_name="heuristic-v1",
        confidence=confidence,
        fair_yes_price=fair_yes_price,
        fair_no_price=fair_no_price,
        edge=edge,
        reasons=["test"],
        features={},
    )
    prediction = Prediction(
        event_id=event.id,
        market_id=market.id,
        ticker=ticker,
        sport_key=sport_key,
        event_name=event.name,
        market_title=market.title,
        market_family="winner",
        market_kind="game_winner",
        side=side,
        action="buy",
        suggested_price=suggested_price,
        fair_yes_price=fair_yes_price,
        fair_no_price=fair_no_price,
        edge=edge,
        confidence=confidence,
        model_name="heuristic-v1",
        rationale="Test rationale",
        market_status_at_capture="active",
        captured_at=datetime(2026, 4, 2, 23, 0, tzinfo=timezone.utc),
    )
    db_session.add_all([recommendation, signal, prediction])
    db_session.flush()
    return ParlayCandidateInput(
        event=event,
        market=market,
        recommendation=recommendation,
        signal=signal,
        prediction=prediction,
        metadata=market.raw_data or {},
    )


def test_capture_parlay_artifacts_generates_mixed_and_same_sport_parlays(db_session):
    candidates = [
        _make_candidate(
            db_session,
            sport_key="NBA",
            ticker="NBA-LEG-1",
            side="yes",
            suggested_price=0.42,
            fair_yes_price=0.58,
            fair_no_price=0.42,
            edge=0.16,
            confidence=0.74,
        ),
        _make_candidate(
            db_session,
            sport_key="MLB",
            ticker="MLB-LEG-1",
            side="yes",
            suggested_price=0.35,
            fair_yes_price=0.49,
            fair_no_price=0.51,
            edge=0.14,
            confidence=0.69,
        ),
        _make_candidate(
            db_session,
            sport_key="NBA",
            ticker="NBA-LEG-2",
            side="no",
            suggested_price=0.40,
            fair_yes_price=0.48,
            fair_no_price=0.52,
            edge=0.12,
            confidence=0.71,
        ),
    ]

    recommendation_count, prediction_count = capture_parlay_artifacts(db_session, run_id=7, candidates=candidates)
    db_session.commit()

    assert recommendation_count == 4
    assert prediction_count == 4

    parlays = db_session.scalars(select(ParlayPrediction).order_by(ParlayPrediction.leg_count, ParlayPrediction.id)).all()
    assert len(parlays) == 4
    assert {parlay.sport_scope for parlay in parlays} >= {"NBA", "MIXED"}

    mixed_two_leg = next(
        parlay
        for parlay in parlays
        if parlay.sport_scope == "MIXED" and parlay.leg_count == 2 and {leg.ticker for leg in parlay.legs} == {"NBA-LEG-1", "MLB-LEG-1"}
    )
    assert mixed_two_leg.combined_market_price == round(0.42 * 0.35, 4)
    assert mixed_two_leg.combined_model_probability == round(0.58 * 0.49, 4)
    assert mixed_two_leg.american_odds == american_odds_from_probability(mixed_two_leg.combined_market_price)
    assert [leg.leg_index for leg in mixed_two_leg.legs] == [1, 2]


def test_settle_parlay_predictions_follows_leg_outcomes(db_session):
    won_a = _make_candidate(db_session, sport_key="NBA", ticker="SETTLE-WON-A", side="yes", suggested_price=0.41, fair_yes_price=0.56, fair_no_price=0.44, edge=0.15, confidence=0.7)
    won_b = _make_candidate(db_session, sport_key="MLB", ticker="SETTLE-WON-B", side="yes", suggested_price=0.39, fair_yes_price=0.55, fair_no_price=0.45, edge=0.16, confidence=0.68)
    lost_leg = _make_candidate(db_session, sport_key="NBA", ticker="SETTLE-LOST", side="yes", suggested_price=0.38, fair_yes_price=0.54, fair_no_price=0.46, edge=0.16, confidence=0.66)
    cancelled_leg = _make_candidate(db_session, sport_key="MLB", ticker="SETTLE-CANCEL", side="no", suggested_price=0.37, fair_yes_price=0.43, fair_no_price=0.57, edge=0.2, confidence=0.67)

    won_a.prediction.prediction_outcome = "won"
    won_a.prediction.settlement_status = "settled"
    won_b.prediction.prediction_outcome = "won"
    won_b.prediction.settlement_status = "settled"
    lost_leg.prediction.prediction_outcome = "lost"
    lost_leg.prediction.settlement_status = "settled"
    cancelled_leg.prediction.prediction_outcome = "cancelled"
    cancelled_leg.prediction.settlement_status = "cancelled"

    win_parlay_count, _ = capture_parlay_artifacts(db_session, run_id=11, candidates=[won_a, won_b])
    lost_parlay_count, _ = capture_parlay_artifacts(db_session, run_id=12, candidates=[won_a, lost_leg])
    cancelled_parlay_count, _ = capture_parlay_artifacts(db_session, run_id=13, candidates=[won_a, cancelled_leg])
    assert win_parlay_count == lost_parlay_count == cancelled_parlay_count == 1
    db_session.commit()

    summary = settle_parlay_predictions(db_session)
    db_session.commit()

    assert summary["won"] == 1
    assert summary["lost"] == 1
    assert summary["cancelled"] == 1

    parlay_by_run = {parlay.run_id: parlay for parlay in db_session.scalars(select(ParlayPrediction)).all()}
    assert parlay_by_run[11].prediction_outcome == "won"
    assert parlay_by_run[12].prediction_outcome == "lost"
    assert parlay_by_run[13].prediction_outcome == "cancelled"


def test_parlay_routes_filter_scope_and_leg_count(client, db_session):
    candidates = [
        _make_candidate(db_session, sport_key="NBA", ticker="ROUTE-NBA-1", side="yes", suggested_price=0.42, fair_yes_price=0.58, fair_no_price=0.42, edge=0.16, confidence=0.74),
        _make_candidate(db_session, sport_key="NBA", ticker="ROUTE-NBA-2", side="yes", suggested_price=0.36, fair_yes_price=0.5, fair_no_price=0.5, edge=0.14, confidence=0.71),
        _make_candidate(db_session, sport_key="MLB", ticker="ROUTE-MLB-1", side="no", suggested_price=0.4, fair_yes_price=0.46, fair_no_price=0.54, edge=0.14, confidence=0.69),
    ]
    capture_parlay_artifacts(db_session, run_id=21, candidates=candidates)
    db_session.commit()

    nba_watchlist = client.get("/parlays/watchlist", params={"sport_scope": "NBA"})
    assert nba_watchlist.status_code == 200
    assert all(item["sport_scope"] == "NBA" for item in nba_watchlist.json())

    mlb_watchlist = client.get("/parlays/watchlist", params={"sport_scope": "MLB"})
    assert mlb_watchlist.status_code == 200
    assert all(item["sport_scope"] == "MLB" for item in mlb_watchlist.json())

    all_two_leg = client.get("/parlays/predictions", params={"sport_scope": "all", "leg_count": 2})
    assert all_two_leg.status_code == 200
    assert all(item["leg_count"] == 2 for item in all_two_leg.json())
    assert any(item["sport_scope"] == "MIXED" for item in all_two_leg.json())

    summary = client.get("/parlays/predictions/summary", params={"sport_scope": "all", "leg_count": 2})
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["total_predictions"] >= 1
    assert payload["by_leg_count"]["2"] == payload["total_predictions"]
