from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

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
            shadow_inference_archive_retention_days=365,
            run_retention_days=14,
            refresh_job_retention_days=14,
            prediction_retention_days=30,
            prediction_archive_retention_days=365,
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
                source_prediction_id=old_prediction.id,
                market_id=market.id,
                ticker=market.ticker,
                confidence=0.58,
                model_name="shadow-v1",
                captured_at=now - timedelta(days=8),
            ),
            ShadowInference(
                run_id=recent_run.id,
                source_prediction_id=old_prediction.id,
                market_id=market.id,
                ticker=f"{market.ticker}-RECENT",
                confidence=0.61,
                model_name="shadow-v1",
                captured_at=now - timedelta(days=1),
            ),
            ShadowParlayInference(
                run_id=old_run.id,
                source_parlay_prediction_id=old_parlay_prediction.id,
                leg_count=2,
                combined_model_probability=0.33,
                confidence=0.54,
                model_name="shadow-v1",
                captured_at=now - timedelta(days=8),
            ),
            ShadowParlayInference(
                run_id=recent_run.id,
                source_parlay_prediction_id=old_parlay_prediction.id,
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
    recent_shadow_id = db_session.scalars(select(ShadowInference.id).where(ShadowInference.run_id == recent_run.id)).one()
    recent_shadow_parlay_id = db_session.scalars(
        select(ShadowParlayInference.id).where(ShadowParlayInference.run_id == recent_run.id)
    ).one()

    summary = maintenance.prune_runtime_artifacts(db_session)
    db_session.commit()
    db_session.expire_all()

    recent_leg = db_session.get(ParlayPredictionLeg, recent_parlay_leg_id)
    recent_recommendation = db_session.get(ParlayRecommendation, old_parlay_recommendation_id)
    recent_shadow = db_session.get(ShadowInference, recent_shadow_id)
    recent_shadow_parlay = db_session.get(ShadowParlayInference, recent_shadow_parlay_id)

    assert summary == {
        "market_snapshots_deleted": 1,
        "signal_snapshots_deleted": 1,
        "shadow_inferences_deleted": 1,
        "shadow_parlay_inferences_deleted": 1,
        "refresh_jobs_deleted": 1,
        "parlay_prediction_legs_deleted": 1,
        "parlay_predictions_deleted": 1,
        "parlay_prediction_source_links_cleared": 1,
        "shadow_prediction_source_links_cleared": 1,
        "shadow_parlay_source_links_cleared": 1,
        "predictions_deleted": 1,
        "player_search_cache_deleted": 1,
        "player_gamelog_cache_deleted": 1,
        "parlay_recommendation_run_links_cleared": 1,
        # Bug #19 round-1 P1: pre-clear run_id on retained archive rows
        # before the bulk run-delete fires. In this fixture all rows
        # tied to the old run are themselves being deleted, so the
        # update finds nothing to clear.
        "prediction_run_links_cleared": 0,
        "parlay_prediction_run_links_cleared": 0,
        "shadow_inference_run_links_cleared": 0,
        "shadow_parlay_inference_run_links_cleared": 0,
        "runs_deleted": 1,
        "current_slate_snapshots_deleted": 0,
    }
    assert db_session.get(Prediction, old_prediction_id) is None
    assert db_session.get(Prediction, recent_prediction_id) is not None
    assert db_session.get(ParlayPrediction, old_parlay_prediction_id) is None
    assert db_session.get(ParlayPrediction, recent_parlay_prediction_id) is not None
    assert recent_leg is not None
    assert recent_leg.source_prediction_id is None
    assert recent_shadow is not None
    assert recent_shadow.source_prediction_id is None
    assert recent_shadow_parlay is not None
    assert recent_shadow_parlay.source_parlay_prediction_id is None
    assert db_session.get(Run, old_run_id) is None
    assert db_session.get(Run, recent_run_id) is not None
    assert recent_recommendation is not None
    assert recent_recommendation.run_id is None


def test_prune_runtime_artifacts_keeps_settled_predictions_beyond_short_ttl(db_session, monkeypatch):
    """Bug #19: a settled prediction past the short ``prediction_retention_days``
    cutoff but within ``prediction_archive_retention_days`` must survive the
    cleanup so calibration and walk-forward eval can still read it.

    The previous single-cutoff delete reaped 23k+ settled predictions before
    the 2026-05-12 retrain, leaving 1.7k training rows. The two-tier rule
    keeps pending stragglers on the short TTL and settled rows on the long
    archive TTL."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: SimpleNamespace(
            market_snapshot_retention_days=3,
            signal_snapshot_retention_days=2,
            shadow_inference_retention_days=7,
            shadow_inference_archive_retention_days=365,
            run_retention_days=14,
            refresh_job_retention_days=14,
            prediction_retention_days=7,
            prediction_archive_retention_days=365,
        ),
    )

    event = Event(
        external_id="bug19-event",
        sport_key="NBA",
        name="Bug #19 Test",
        status="completed",
        starts_at=now - timedelta(days=10),
    )
    db_session.add(event)
    db_session.flush()

    run = Run(
        kind="refresh",
        status="completed",
        started_at=now - timedelta(days=12),
        finished_at=now - timedelta(days=12, minutes=-2),
    )
    db_session.add(run)
    db_session.flush()

    def _make_prediction(*, ticker_suffix: str, captured_days_ago: int, outcome: str) -> Prediction:
        # Predictions table has a UNIQUE (run_id, market_id) constraint —
        # give each row its own market so the test can write all of them.
        market = Market(
            ticker=f"KX-BUG19-{ticker_suffix}",
            sport_key="NBA",
            event_id=event.id,
            title=f"Bug #19 market / {ticker_suffix}",
            status="settled",
        )
        db_session.add(market)
        db_session.flush()
        return Prediction(
            run_id=run.id,
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
            rationale=f"Bug #19 / {ticker_suffix}",
            settlement_status="settled" if outcome != "pending" else "pending",
            prediction_outcome=outcome,
            captured_at=now - timedelta(days=captured_days_ago),
        )

    # Past short TTL, pending → reaped on the short TTL.
    pending_old = _make_prediction(ticker_suffix="P-OLD", captured_days_ago=12, outcome="pending")
    # Past short TTL, settled → must survive (the bug-#19 fix).
    settled_old = _make_prediction(ticker_suffix="S-OLD", captured_days_ago=60, outcome="won")
    settled_old_lost = _make_prediction(ticker_suffix="S-OLD-L", captured_days_ago=180, outcome="lost")
    # Past archive TTL, even settled rows reap (table-size ceiling).
    settled_ancient = _make_prediction(ticker_suffix="S-ANCIENT", captured_days_ago=400, outcome="won")
    # Inside short TTL → always kept.
    pending_recent = _make_prediction(ticker_suffix="P-NEW", captured_days_ago=2, outcome="pending")
    db_session.add_all([pending_old, settled_old, settled_old_lost, settled_ancient, pending_recent])
    db_session.commit()

    pending_old_id = pending_old.id
    settled_old_id = settled_old.id
    settled_old_lost_id = settled_old_lost.id
    settled_ancient_id = settled_ancient.id
    pending_recent_id = pending_recent.id

    maintenance.prune_runtime_artifacts(db_session)
    db_session.commit()
    db_session.expire_all()

    # Pending old reaped — short TTL applied as before.
    assert db_session.get(Prediction, pending_old_id) is None
    # Settled rows older than short TTL but younger than archive survive — bug-#19 fix.
    assert db_session.get(Prediction, settled_old_id) is not None
    assert db_session.get(Prediction, settled_old_lost_id) is not None
    # Settled but past the archive TTL — reaped to bound table growth.
    assert db_session.get(Prediction, settled_ancient_id) is None
    # Inside short TTL — kept regardless of outcome.
    assert db_session.get(Prediction, pending_recent_id) is not None


def test_prune_runtime_artifacts_keeps_shadow_inferences_for_settled_predictions(
    db_session, monkeypatch
):
    """Bug #19: a ``ShadowInference`` whose ``source_prediction_id`` points at
    a settled prediction is paired with a real outcome and is the input ML
    needs for calibration analysis. It must survive past the short
    ``shadow_inference_retention_days`` cutoff."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: SimpleNamespace(
            market_snapshot_retention_days=3,
            signal_snapshot_retention_days=2,
            shadow_inference_retention_days=7,
            shadow_inference_archive_retention_days=365,
            run_retention_days=14,
            refresh_job_retention_days=14,
            prediction_retention_days=7,
            prediction_archive_retention_days=365,
        ),
    )

    event = Event(
        external_id="bug19-shadow-event",
        sport_key="NBA",
        name="Bug #19 Shadow Test",
        status="completed",
        starts_at=now - timedelta(days=10),
    )
    db_session.add(event)
    db_session.flush()

    market = Market(
        ticker="KX-BUG19-SHADOW",
        sport_key="NBA",
        event_id=event.id,
        title="Bug #19 shadow market",
        status="settled",
    )
    db_session.add(market)

    run = Run(
        kind="refresh",
        status="completed",
        started_at=now - timedelta(days=10),
        finished_at=now - timedelta(days=10, minutes=-2),
    )
    db_session.add(run)
    db_session.flush()

    settled_prediction = Prediction(
        run_id=run.id,
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
        rationale="Bug #19 settled prediction",
        settlement_status="settled",
        prediction_outcome="won",
        captured_at=now - timedelta(days=45),
    )
    db_session.add(settled_prediction)
    db_session.flush()

    # Paired with a settled prediction — survives short TTL via archive rule.
    settled_shadow = ShadowInference(
        run_id=run.id,
        source_prediction_id=settled_prediction.id,
        market_id=market.id,
        ticker=f"{market.ticker}-SETTLED-SHADOW",
        confidence=0.61,
        model_name="shadow-v1",
        captured_at=now - timedelta(days=45),
    )
    # No paired prediction, old → reaped on short TTL.
    orphan_shadow = ShadowInference(
        run_id=run.id,
        source_prediction_id=None,
        market_id=market.id,
        ticker=f"{market.ticker}-ORPHAN-SHADOW",
        confidence=0.55,
        model_name="shadow-v1",
        captured_at=now - timedelta(days=45),
    )
    db_session.add_all([settled_shadow, orphan_shadow])
    db_session.commit()

    settled_shadow_id = settled_shadow.id
    orphan_shadow_id = orphan_shadow.id

    maintenance.prune_runtime_artifacts(db_session)
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(ShadowInference, settled_shadow_id) is not None
    assert db_session.get(ShadowInference, orphan_shadow_id) is None


# -----------------------------------------------------------------------------
# Bug #19 round-1 P1: clear run_id on retained archive rows before
# run-delete to avoid FK violation on Postgres.
# -----------------------------------------------------------------------------


def test_prune_runtime_artifacts_clears_run_links_on_retained_archive_rows(
    db_session, monkeypatch
):
    """A settled prediction kept past ``run_retention_days`` (because the
    archive TTL is longer) still references its terminal ``Run`` row.
    On Postgres with FK enforcement, the bulk run-delete would fail
    with a foreign-key error. The fix nulls the ``run_id`` on all
    retained Prediction / ParlayPrediction / ShadowInference /
    ShadowParlayInference rows whose ``run_id`` is in the
    about-to-be-deleted set."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: SimpleNamespace(
            market_snapshot_retention_days=3,
            signal_snapshot_retention_days=2,
            shadow_inference_retention_days=7,
            shadow_inference_archive_retention_days=365,
            run_retention_days=14,
            refresh_job_retention_days=14,
            prediction_retention_days=7,
            prediction_archive_retention_days=365,
        ),
    )

    event = Event(
        external_id="bug19-fk-event",
        sport_key="NBA",
        name="Bug #19 FK Test",
        status="completed",
        starts_at=now - timedelta(days=30),
    )
    db_session.add(event)
    db_session.flush()

    market = Market(
        ticker="KX-BUG19-FK",
        sport_key="NBA",
        event_id=event.id,
        title="Bug #19 FK market",
        status="settled",
    )
    db_session.add(market)
    db_session.flush()

    # Run is OLD (> 14-day run_retention_days) so it's in old_run_ids.
    old_run = Run(
        kind="refresh",
        status="completed",
        started_at=now - timedelta(days=40),
        finished_at=now - timedelta(days=40, minutes=-3),
    )
    db_session.add(old_run)
    db_session.flush()

    # Settled prediction within the 365-day archive TTL → retained.
    # References the about-to-be-deleted run.
    retained_prediction = Prediction(
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
        rationale="Bug #19 retained on FK fixture",
        settlement_status="settled",
        prediction_outcome="won",
        captured_at=now - timedelta(days=40),
    )
    retained_shadow = ShadowInference(
        run_id=old_run.id,
        source_prediction_id=None,  # We don't need a real pairing for this test.
        market_id=market.id,
        ticker=f"{market.ticker}-RETAINED-SHADOW",
        confidence=0.61,
        model_name="shadow-v1",
        captured_at=now - timedelta(days=2),  # recent → kept regardless
    )
    db_session.add_all([retained_prediction, retained_shadow])
    db_session.commit()

    retained_prediction_id = retained_prediction.id
    retained_shadow_id = retained_shadow.id
    old_run_id = old_run.id

    summary = maintenance.prune_runtime_artifacts(db_session)
    db_session.commit()
    db_session.expire_all()

    # Run was deleted; retained rows now point at NULL.
    assert db_session.get(Run, old_run_id) is None
    persisted_prediction = db_session.get(Prediction, retained_prediction_id)
    assert persisted_prediction is not None
    assert persisted_prediction.run_id is None
    persisted_shadow = db_session.get(ShadowInference, retained_shadow_id)
    assert persisted_shadow is not None
    assert persisted_shadow.run_id is None

    # Telemetry counters reflect the cleared links.
    assert summary["prediction_run_links_cleared"] >= 1
    assert summary["shadow_inference_run_links_cleared"] >= 1


# -----------------------------------------------------------------------------
# Bug #19 round-1 P2: ``prediction_outcome='unresolved'`` rows must also
# reap on the short TTL.
# -----------------------------------------------------------------------------


def test_prune_runtime_artifacts_reaps_unresolved_predictions_on_short_ttl(
    db_session, monkeypatch
):
    """Codex round-1 P2 on PR #46: the earlier fix only matched
    ``prediction_outcome == "pending"`` for the short TTL, leaving
    ``"unresolved"`` (closed market with no result, parlay missing
    source legs) in neither bucket. Those rows would have accumulated
    forever. The fix uses ``notin_(SETTLED_OUTCOMES)`` so anything
    that ISN'T a real settled outcome reaps on the short TTL."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        maintenance,
        "get_settings",
        lambda: SimpleNamespace(
            market_snapshot_retention_days=3,
            signal_snapshot_retention_days=2,
            shadow_inference_retention_days=7,
            shadow_inference_archive_retention_days=365,
            run_retention_days=14,
            refresh_job_retention_days=14,
            prediction_retention_days=7,
            prediction_archive_retention_days=365,
        ),
    )

    event = Event(
        external_id="bug19-unresolved-event",
        sport_key="NBA",
        name="Bug #19 Unresolved Test",
        status="completed",
        starts_at=now - timedelta(days=30),
    )
    db_session.add(event)
    db_session.flush()

    market = Market(
        ticker="KX-BUG19-UNRESOLVED",
        sport_key="NBA",
        event_id=event.id,
        title="Bug #19 unresolved market",
        status="settled",
    )
    db_session.add(market)

    run = Run(
        kind="refresh",
        status="completed",
        started_at=now - timedelta(days=12),
        finished_at=now - timedelta(days=12, minutes=-2),
    )
    db_session.add(run)
    db_session.flush()

    # Unresolved, old → must be reaped on the short TTL.
    unresolved_old = Prediction(
        run_id=run.id,
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
        rationale="Bug #19 unresolved fixture",
        settlement_status="unresolved",
        prediction_outcome="unresolved",
        captured_at=now - timedelta(days=30),
    )
    db_session.add(unresolved_old)
    db_session.commit()

    unresolved_id = unresolved_old.id

    maintenance.prune_runtime_artifacts(db_session)
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(Prediction, unresolved_id) is None, (
        "Unresolved predictions past the short TTL must reap. "
        "Falling through both buckets would leak them forever."
    )
