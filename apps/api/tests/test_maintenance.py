from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models import (
    EspnPlayerGamelogCache,
    EspnPlayerSearchCache,
    Event,
    Market,
    MarketSnapshot,
    ParlayPrediction,
    ParlayPredictionLeg,
    ParlayRecommendation,
    Prediction,
    RefreshJob,
    Run,
    ShadowInference,
    ShadowParlayInference,
    SignalSnapshot,
)
from app.services import maintenance


def test_prune_runtime_artifacts_cleans_old_runtime_rows(db_session, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: SimpleNamespace(
            market_snapshot_retention_days=7,
            signal_snapshot_retention_days=7,
            shadow_inference_retention_days=7,
            run_retention_days=14,
            refresh_job_retention_days=14,
            prediction_retention_days=30,
        ),
    )

    event = Event(
        external_id="maintenance-event",
        sport_key="NBA",
        name="Maintenance Test",
        status="scheduled",
        starts_at=now + timedelta(hours=2),
    )
    db_session.add(event)
    db_session.flush()

    market = Market(
        ticker="KX-MAINT-1",
        sport_key="NBA",
        event_id=event.id,
        title="Maintenance market",
        status="open",
    )
    db_session.add(market)

    old_run = Run(
        kind="refresh",
        status="completed",
        started_at=now - timedelta(days=20),
        finished_at=now - timedelta(days=20, minutes=-4),
    )
    recent_run = Run(
        kind="refresh",
        status="completed",
        started_at=now - timedelta(days=1),
        finished_at=now - timedelta(days=1, minutes=-4),
    )
    db_session.add_all([old_run, recent_run])
    db_session.flush()

    old_prediction = Prediction(
        run_id=old_run.id,
        event_id=event.id,
        market_id=market.id,
        ticker=market.ticker,
        sport_key="NBA",
        event_name=event.name,
        market_title=market.title,
        side="yes",
        action="buy",
        suggested_price=0.51,
        edge=0.06,
        confidence=0.62,
        model_name="heuristic-v1",
        rationale="Old prediction",
        settlement_status="pending",
        prediction_outcome="pending",
        captured_at=now - timedelta(days=31),
    )
    recent_prediction = Prediction(
        run_id=recent_run.id,
        event_id=event.id,
        market_id=market.id,
        ticker=f"{market.ticker}-RECENT",
        sport_key="NBA",
        event_name=event.name,
        market_title=market.title,
        side="no",
        action="buy",
        suggested_price=0.49,
        edge=0.05,
        confidence=0.6,
        model_name="heuristic-v1",
        rationale="Recent prediction",
        settlement_status="pending",
        prediction_outcome="pending",
        captured_at=now - timedelta(days=2),
    )
    db_session.add_all([old_prediction, recent_prediction])
    db_session.flush()

    old_parlay_prediction = ParlayPrediction(
        run_id=old_run.id,
        leg_count=2,
        sport_scope="NBA",
        combined_market_price=0.25,
        combined_model_probability=0.34,
        american_odds="+300",
        edge=0.09,
        confidence=0.57,
        rationale="Old parlay prediction",
        settlement_status="pending",
        prediction_outcome="pending",
        captured_at=now - timedelta(days=31),
    )
    recent_parlay_prediction = ParlayPrediction(
        run_id=recent_run.id,
        leg_count=2,
        sport_scope="NBA",
        combined_market_price=0.28,
        combined_model_probability=0.36,
        american_odds="+250",
        edge=0.08,
        confidence=0.59,
        rationale="Recent parlay prediction",
        settlement_status="pending",
        prediction_outcome="pending",
        captured_at=now - timedelta(days=1),
    )
    db_session.add_all([old_parlay_prediction, recent_parlay_prediction])
    db_session.flush()

    old_parlay_leg = ParlayPredictionLeg(
        parlay_prediction_id=old_parlay_prediction.id,
        leg_index=0,
        source_prediction_id=old_prediction.id,
        market_id=market.id,
        ticker=f"{market.ticker}-LEG-OLD",
        market_title=market.title,
        side="yes",
        action="buy",
        suggested_price=0.51,
        edge=0.06,
        confidence=0.62,
    )
    recent_parlay_leg = ParlayPredictionLeg(
        parlay_prediction_id=recent_parlay_prediction.id,
        leg_index=0,
        source_prediction_id=old_prediction.id,
        market_id=market.id,
        ticker=f"{market.ticker}-LEG-RECENT",
        market_title=market.title,
        side="no",
        action="buy",
        suggested_price=0.49,
        edge=0.05,
        confidence=0.6,
    )
    old_parlay_recommendation = ParlayRecommendation(
        run_id=old_run.id,
        leg_count=2,
        sport_scope="NBA",
        combined_market_price=0.3,
        combined_model_probability=0.38,
        american_odds="+230",
        edge=0.07,
        confidence=0.55,
        invalidation="Invalidate old parlay",
        rationale="Old parlay recommendation",
        captured_at=now - timedelta(days=20),
    )

    db_session.add_all(
        [
            MarketSnapshot(market_id=market.id, captured_at=now - timedelta(days=8), last_price=0.44),
            MarketSnapshot(market_id=market.id, captured_at=now - timedelta(days=1), last_price=0.45),
            SignalSnapshot(
                event_id=event.id,
                market_id=market.id,
                captured_at=now - timedelta(days=8),
                confidence=0.61,
                fair_yes_price=0.55,
                fair_no_price=0.45,
                edge=0.04,
            ),
            SignalSnapshot(
                event_id=event.id,
                market_id=market.id,
                captured_at=now - timedelta(days=1),
                confidence=0.64,
                fair_yes_price=0.57,
                fair_no_price=0.43,
                edge=0.06,
            ),
            ShadowInference(
                run_id=old_run.id,
                market_id=market.id,
                ticker=market.ticker,
                confidence=0.58,
                model_name="shadow-v1",
                captured_at=now - timedelta(days=8),
            ),
            ShadowInference(
                run_id=recent_run.id,
                market_id=market.id,
                ticker=f"{market.ticker}-RECENT",
                confidence=0.61,
                model_name="shadow-v1",
                captured_at=now - timedelta(days=1),
            ),
            ShadowParlayInference(
                run_id=old_run.id,
                leg_count=2,
                combined_model_probability=0.33,
                confidence=0.54,
                model_name="shadow-v1",
                captured_at=now - timedelta(days=8),
            ),
            ShadowParlayInference(
                run_id=recent_run.id,
                leg_count=2,
                combined_model_probability=0.35,
                confidence=0.56,
                model_name="shadow-v1",
                captured_at=now - timedelta(days=1),
            ),
            RefreshJob(
                kind="refresh",
                scope="current_slate",
                reason="interval",
                status="completed",
                run_id=old_run.id,
                queued_at=now - timedelta(days=20),
                started_at=now - timedelta(days=20),
                finished_at=now - timedelta(days=20, minutes=-2),
            ),
            RefreshJob(
                kind="refresh",
                scope="current_slate",
                reason="interval",
                status="completed",
                run_id=recent_run.id,
                queued_at=now - timedelta(days=1),
                started_at=now - timedelta(days=1),
                finished_at=now - timedelta(days=1, minutes=-2),
            ),
            RefreshJob(
                kind="refresh",
                scope="current_slate",
                reason="interval",
                status="queued",
                queued_at=now,
            ),
            old_parlay_leg,
            recent_parlay_leg,
            old_parlay_recommendation,
            EspnPlayerSearchCache(
                sport_key="NBA",
                query_normalized="old-player",
                payload={"athlete_id": "123"},
                cached_at=now - timedelta(days=2),
                expires_at=now - timedelta(hours=1),
            ),
            EspnPlayerSearchCache(
                sport_key="NBA",
                query_normalized="new-player",
                payload={"athlete_id": "456"},
                cached_at=now - timedelta(hours=1),
                expires_at=now + timedelta(days=2),
            ),
            EspnPlayerGamelogCache(
                sport_key="NBA",
                athlete_id="123",
                season=2026,
                payload={"games": []},
                cached_at=now - timedelta(days=2),
                expires_at=now - timedelta(hours=1),
            ),
            EspnPlayerGamelogCache(
                sport_key="NBA",
                athlete_id="456",
                season=2026,
                payload={"games": []},
                cached_at=now - timedelta(hours=1),
                expires_at=now + timedelta(days=2),
            ),
        ]
    )
    db_session.commit()

    old_prediction_id = old_prediction.id
    recent_prediction_id = recent_prediction.id
    old_parlay_prediction_id = old_parlay_prediction.id
    recent_parlay_prediction_id = recent_parlay_prediction.id
    recent_parlay_leg_id = recent_parlay_leg.id
    old_parlay_recommendation_id = old_parlay_recommendation.id
    old_run_id = old_run.id
    recent_run_id = recent_run.id

    summary = maintenance.prune_runtime_artifacts(db_session)
    db_session.commit()
    db_session.expire_all()

    recent_leg = db_session.get(ParlayPredictionLeg, recent_parlay_leg_id)
    recent_recommendation = db_session.get(ParlayRecommendation, old_parlay_recommendation_id)

    assert summary == {
        "market_snapshots_deleted": 1,
        "signal_snapshots_deleted": 1,
        "shadow_inferences_deleted": 1,
        "shadow_parlay_inferences_deleted": 1,
        "refresh_jobs_deleted": 1,
        "parlay_prediction_legs_deleted": 1,
        "parlay_predictions_deleted": 1,
        "parlay_prediction_source_links_cleared": 1,
        "predictions_deleted": 1,
        "player_search_cache_deleted": 1,
        "player_gamelog_cache_deleted": 1,
        "parlay_recommendation_run_links_cleared": 1,
        "runs_deleted": 1,
    }
    assert db_session.get(Prediction, old_prediction_id) is None
    assert db_session.get(Prediction, recent_prediction_id) is not None
    assert db_session.get(ParlayPrediction, old_parlay_prediction_id) is None
    assert db_session.get(ParlayPrediction, recent_parlay_prediction_id) is not None
    assert recent_leg is not None
    assert recent_leg.source_prediction_id is None
    assert db_session.get(Run, old_run_id) is None
    assert db_session.get(Run, recent_run_id) is not None
    assert recent_recommendation is not None
    assert recent_recommendation.run_id is None
