import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import ParlayPrediction, ParlayPredictionLeg, Prediction, ShadowInference, ShadowParlayInference, Run
from app.services.ingestion import run_shadow_capture_cycle
from app.services.ml.runtime import resolve_family_runtime
from app.services.ml.shadow_modes import DIAGNOSTIC_BACKFILL_CAPTURE_MODE
from app.services.ml.study_progress import retained_study_cutoff


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _write_artifact(tmp_path, *, family_key: str, scope: str, probability: float = 0.62):
    artifact_path = tmp_path / f"{family_key}.json"
    artifact_path.write_text(
        json.dumps(
            {
                "family_key": family_key,
                "scope": scope,
                "behavior": "static_probability",
                "probability": probability,
                "confidence": probability,
                "metadata": {"source": "shadow-test"},
            }
        ),
        encoding="utf-8",
    )
    return artifact_path


def _write_manifest(tmp_path, *, families: list[dict[str, str]]):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "shadow-test",
                "serving_mode": "shadow",
                "families": families,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _configure_shadow(monkeypatch, tmp_path, *, family_scopes: dict[str, str], family_modes: dict[str, str] | None = None) -> None:
    families = []
    for family_key, scope in family_scopes.items():
        artifact_path = _write_artifact(tmp_path, family_key=family_key, scope=scope)
        families.append(
            {
                "family_key": family_key,
                "model_name": f"{family_key}-model",
                "model_version": "v1",
                "calibration_version": "cal-v1",
                "feature_set_version": "features-v1",
                "artifact_path": str(artifact_path),
                "mode": "heuristic",
                "metadata": {"source": "manifest"},
            }
        )
    manifest_path = _write_manifest(tmp_path, families=families)
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps(family_modes or {family_key: "shadow" for family_key in family_scopes}))
    get_settings.cache_clear()


def _current_lineage_metadata(db_session, family_key: str, *, scope: str) -> dict[str, object]:
    decision = resolve_family_runtime(db_session, family_key, scope=scope)
    return dict(decision.lineage.model_metadata or {})


def _seed_prediction(
    db_session,
    *,
    ticker: str,
    market_id: int,
    run_id: int,
    captured_at: datetime,
    sport_key: str = "NBA",
    market_family: str = "winner",
    capture_scope: str = "recommendation",
) -> Prediction:
    prediction = Prediction(
        run_id=run_id,
        event_id=None,
        market_id=market_id,
        ticker=ticker,
        sport_key=sport_key,
        event_name="Shadow test",
        market_title="Shadow market",
        market_family=market_family,
        market_kind="game_winner" if market_family != "player_prop" else "player_prop",
        capture_scope=capture_scope,
        side="yes",
        action="buy",
        suggested_price=0.45,
        fair_yes_price=0.58,
        fair_no_price=0.42,
        edge=0.13,
        confidence=0.68,
        selection_score=0.17,
        model_name="heuristic-v1",
        rationale="Shadow test prediction",
        reasons=["shadow-test"],
        features={},
        scoring_diagnostics={},
        market_status_at_capture="active",
        settlement_status="pending",
        prediction_outcome="pending",
        captured_at=captured_at,
    )
    db_session.add(prediction)
    db_session.flush()
    return prediction


def _seed_parlay_prediction(
    db_session,
    *,
    run_id: int,
    captured_at: datetime,
    leg_predictions: list[Prediction],
    sport_scope: str = "NBA",
) -> ParlayPrediction:
    parlay = ParlayPrediction(
        run_id=run_id,
        leg_count=len(leg_predictions),
        sport_scope=sport_scope,
        participating_sports=[prediction.sport_key or sport_scope for prediction in leg_predictions],
        combined_market_price=0.24,
        combined_model_probability=0.36,
        american_odds="+320",
        edge=0.12,
        confidence=0.59,
        model_name="heuristic-v1",
        rationale="Shadow test parlay",
        settlement_status="pending",
        prediction_outcome="pending",
        captured_at=captured_at,
    )
    db_session.add(parlay)
    db_session.flush()
    for index, prediction in enumerate(leg_predictions):
        db_session.add(
            ParlayPredictionLeg(
                parlay_prediction_id=parlay.id,
                leg_index=index,
                source_prediction_id=prediction.id,
                event_id=prediction.event_id,
                market_id=prediction.market_id,
                ticker=prediction.ticker,
                sport_key=prediction.sport_key,
                event_name=prediction.event_name,
                market_title=prediction.market_title,
                market_family=prediction.market_family,
                market_kind=prediction.market_kind,
                stat_key=prediction.stat_key,
                threshold=prediction.threshold,
                subject_name=prediction.subject_name,
                subject_team=prediction.subject_team,
                side=prediction.side,
                action=prediction.action,
                suggested_price=prediction.suggested_price,
                fair_yes_price=prediction.fair_yes_price,
                fair_no_price=prediction.fair_no_price,
                edge=prediction.edge,
                confidence=prediction.confidence,
            )
        )
    db_session.flush()
    db_session.refresh(parlay)
    return parlay


def test_current_slate_shadow_capture_is_idempotent_and_source_linked(db_session, monkeypatch, tmp_path):
    _configure_shadow(monkeypatch, tmp_path, family_scopes={"nba_singles": "single"})

    source_run = Run(kind="refresh", status="completed")
    db_session.add(source_run)
    db_session.flush()
    base_captured_at = datetime.now(timezone.utc) - timedelta(days=1)
    for index in range(3):
        _seed_prediction(
            db_session,
            ticker=f"NBA-CURRENT-{index}",
            market_id=index + 1,
            run_id=source_run.id,
            captured_at=base_captured_at + timedelta(minutes=index),
        )
    db_session.commit()

    first = run_shadow_capture_cycle(db_session, scope="current_slate", source_run_id=source_run.id)
    db_session.commit()
    second = run_shadow_capture_cycle(db_session, scope="current_slate", source_run_id=source_run.id)
    db_session.commit()

    rows = db_session.scalars(select(ShadowInference).order_by(ShadowInference.id.asc())).all()
    assert first.records_processed == 3
    assert second.records_processed == 0
    assert len(rows) == 3
    assert all(row.source_prediction_id is not None for row in rows)
    assert {row.ticker for row in rows} == {"NBA-CURRENT-0", "NBA-CURRENT-1", "NBA-CURRENT-2"}


def test_shadow_backfill_captures_uncaptured_historical_predictions_and_parlays(db_session, monkeypatch, tmp_path):
    _configure_shadow(
        monkeypatch,
        tmp_path,
        family_scopes={"nba_singles": "single", "nba_parlay_2leg": "parlay"},
    )

    now = retained_study_cutoff() + timedelta(days=5)
    run = Run(kind="refresh", status="completed")
    db_session.add(run)
    db_session.flush()

    covered_single = _seed_prediction(db_session, ticker="NBA-COVERED", market_id=1, run_id=run.id, captured_at=now - timedelta(days=3))
    _seed_prediction(db_session, ticker="NBA-UNCAPTURED", market_id=2, run_id=run.id, captured_at=now - timedelta(days=2))
    _seed_prediction(
        db_session,
        ticker="NBA-COVERAGE",
        market_id=3,
        run_id=run.id,
        captured_at=now - timedelta(days=1),
        capture_scope="coverage",
    )
    # Heuristic-only families (NFL singles) are still excluded by the active_study_only gate.
    _seed_prediction(
        db_session,
        ticker="NFL-HEURISTIC",
        market_id=4,
        run_id=run.id,
        captured_at=now - timedelta(days=1),
        sport_key="NFL",
    )
    leg_a = _seed_prediction(
        db_session,
        ticker="NBA-PARLAY-A",
        market_id=5,
        run_id=run.id,
        captured_at=now - timedelta(days=4),
        capture_scope="coverage",
    )
    leg_b = _seed_prediction(
        db_session,
        ticker="NBA-PARLAY-B",
        market_id=6,
        run_id=run.id,
        captured_at=now - timedelta(days=4, minutes=-1),
        capture_scope="coverage",
    )
    parlay = _seed_parlay_prediction(
        db_session,
        run_id=run.id,
        captured_at=now - timedelta(days=4),
        leg_predictions=[leg_a, leg_b],
    )
    nba_single_metadata = _current_lineage_metadata(db_session, "nba_singles", scope="single")
    db_session.add(
        ShadowInference(
            run_id=covered_single.run_id,
            source_prediction_id=covered_single.id,
            event_id=covered_single.event_id,
            market_id=covered_single.market_id,
            ticker=covered_single.ticker,
            sport_key=covered_single.sport_key,
            event_name=covered_single.event_name,
            market_title=covered_single.market_title,
            market_family=covered_single.market_family,
            market_kind=covered_single.market_kind,
            inference_scope="single",
            recommended_side=covered_single.side,
            suggested_price=covered_single.suggested_price,
            fair_yes_price=0.6,
            fair_no_price=0.4,
            edge=0.15,
            confidence=0.6,
            model_name="nba_singles-model",
            model_version="v1",
            calibration_version="cal-v1",
            feature_set_version="features-v1",
            model_metadata=nba_single_metadata,
            rationale="Existing shadow",
            reasons=["shadow"],
            features={},
            captured_at=covered_single.captured_at,
        )
    )
    db_session.commit()

    shadow_run = run_shadow_capture_cycle(db_session, scope="backfill")
    db_session.commit()

    single_rows = db_session.scalars(select(ShadowInference).order_by(ShadowInference.id.asc())).all()
    parlay_rows = db_session.scalars(select(ShadowParlayInference).order_by(ShadowParlayInference.id.asc())).all()

    # Coverage-scoped predictions (NBA-COVERAGE, NBA-PARLAY-A, NBA-PARLAY-B) are now first-class
    # for shadow capture so the active ML study can learn from every scored watchlist market.
    assert shadow_run.records_processed == 5
    assert shadow_run.details["shadow_capture_scope"] == "backfill"
    assert {row.ticker for row in single_rows} == {
        "NBA-COVERED",
        "NBA-UNCAPTURED",
        "NBA-COVERAGE",
        "NBA-PARLAY-A",
        "NBA-PARLAY-B",
    }
    assert all(row.source_prediction_id is not None for row in single_rows)
    assert len(parlay_rows) == 1
    assert parlay_rows[0].source_parlay_prediction_id == parlay.id


def test_diagnostic_backfill_is_excluded_and_does_not_block_normal_shadow_capture(db_session, monkeypatch, tmp_path):
    _configure_shadow(monkeypatch, tmp_path, family_scopes={"nba_singles": "single"})

    run = Run(kind="refresh", status="completed")
    db_session.add(run)
    db_session.flush()
    prediction = _seed_prediction(
        db_session,
        ticker="NBA-DIAGNOSTIC",
        market_id=21,
        run_id=run.id,
        captured_at=retained_study_cutoff() + timedelta(days=1),
    )
    db_session.commit()

    diagnostic_run = run_shadow_capture_cycle(db_session, scope=DIAGNOSTIC_BACKFILL_CAPTURE_MODE)
    db_session.commit()
    db_session.add(
        ShadowInference(
            run_id=prediction.run_id,
            source_prediction_id=prediction.id,
            event_id=prediction.event_id,
            market_id=prediction.market_id,
            ticker=prediction.ticker,
            sport_key=prediction.sport_key,
            event_name=prediction.event_name,
            market_title=prediction.market_title,
            market_family=prediction.market_family,
            market_kind=prediction.market_kind,
            inference_scope="single",
            recommended_side=prediction.side,
            suggested_price=prediction.suggested_price,
            fair_yes_price=0.6,
            fair_no_price=0.4,
            edge=0.15,
            confidence=0.6,
            model_name="nba_singles-model",
            model_version="v1",
            calibration_version="cal-v1",
            feature_set_version="features-v1",
            model_metadata={"family_key": "nba_singles", "artifact_signature": "old"},
            rationale="Old shadow",
            reasons=["shadow"],
            features={},
            captured_at=prediction.captured_at,
        )
    )
    db_session.commit()
    normal_run = run_shadow_capture_cycle(db_session, scope="backfill")
    db_session.commit()
    duplicate_diagnostic_run = run_shadow_capture_cycle(db_session, scope=DIAGNOSTIC_BACKFILL_CAPTURE_MODE)
    db_session.commit()

    rows = db_session.scalars(select(ShadowInference).order_by(ShadowInference.id.asc())).all()
    diagnostic_rows = [
        row for row in rows
        if row.model_metadata.get("capture_mode") == DIAGNOSTIC_BACKFILL_CAPTURE_MODE
    ]
    normal_rows = [
        row for row in rows
        if row.model_metadata.get("capture_mode") != DIAGNOSTIC_BACKFILL_CAPTURE_MODE
    ]

    assert diagnostic_run.records_processed == 1
    assert normal_run.records_processed == 1
    assert duplicate_diagnostic_run.records_processed == 0
    assert len(diagnostic_rows) == 1
    assert diagnostic_rows[0].model_metadata["promotion_excluded"] is True
    assert len(normal_rows) == 2
    current_normal_rows = [
        row
        for row in normal_rows
        if row.model_metadata.get("artifact_signature")
        and row.model_metadata.get("artifact_signature") != "old"
    ]
    assert len(current_normal_rows) == 1
    assert current_normal_rows[0].model_metadata.get("promotion_excluded") is not True


def test_shadow_backfill_skips_legacy_unlinked_duplicates(db_session, monkeypatch, tmp_path):
    _configure_shadow(
        monkeypatch,
        tmp_path,
        family_scopes={"nba_singles": "single", "nba_parlay_2leg": "parlay"},
    )

    now = datetime.now(timezone.utc)
    run = Run(kind="refresh", status="completed")
    db_session.add(run)
    db_session.flush()

    single = _seed_prediction(db_session, ticker="NBA-LEGACY", market_id=11, run_id=run.id, captured_at=now - timedelta(days=2))
    leg_a = _seed_prediction(
        db_session,
        ticker="NBA-LEGACY-A",
        market_id=12,
        run_id=run.id,
        captured_at=now - timedelta(days=2),
        capture_scope="coverage",
    )
    leg_b = _seed_prediction(
        db_session,
        ticker="NBA-LEGACY-B",
        market_id=13,
        run_id=run.id,
        captured_at=now - timedelta(days=2),
        capture_scope="coverage",
    )
    parlay = _seed_parlay_prediction(
        db_session,
        run_id=run.id,
        captured_at=now - timedelta(days=2),
        leg_predictions=[leg_a, leg_b],
    )
    nba_single_metadata = _current_lineage_metadata(db_session, "nba_singles", scope="single")
    nba_parlay_metadata = _current_lineage_metadata(db_session, "nba_parlay_2leg", scope="parlay")
    for source in (single, leg_a, leg_b):
        db_session.add(
            ShadowInference(
                run_id=source.run_id,
                event_id=source.event_id,
                market_id=source.market_id,
                ticker=source.ticker,
                sport_key=source.sport_key,
                event_name=source.event_name,
                market_title=source.market_title,
                market_family=source.market_family,
                market_kind=source.market_kind,
                inference_scope="single",
                recommended_side=source.side,
                suggested_price=source.suggested_price,
                fair_yes_price=0.6,
                fair_no_price=0.4,
                edge=0.15,
                confidence=0.6,
                model_name="nba_singles-model",
                model_version="v1",
                calibration_version="cal-v1",
                feature_set_version="features-v1",
                model_metadata=nba_single_metadata,
                rationale="Legacy shadow",
                reasons=["shadow"],
                features={},
                captured_at=source.captured_at,
            )
        )
    db_session.add(
        ShadowParlayInference(
            run_id=parlay.run_id,
            leg_count=parlay.leg_count,
            sport_scope=parlay.sport_scope,
            participating_sports=list(parlay.participating_sports or []),
            leg_tickers=[leg.ticker for leg in parlay.legs],
            combined_market_price=parlay.combined_market_price,
            combined_model_probability=0.62,
            edge=0.14,
            confidence=0.62,
            model_name="nba_parlay_2leg-model",
            model_version="v1",
            calibration_version="cal-v1",
            feature_set_version="features-v1",
            model_metadata=nba_parlay_metadata,
            rationale="Legacy parlay shadow",
            features={},
            captured_at=parlay.captured_at,
        )
    )
    db_session.commit()

    shadow_run = run_shadow_capture_cycle(db_session, scope="backfill")
    db_session.commit()

    assert shadow_run.records_processed == 0
    assert db_session.query(ShadowInference).count() == 3
    assert db_session.query(ShadowParlayInference).count() == 1


def test_shadow_backfill_respects_oldest_first_prediction_cap(db_session, monkeypatch, tmp_path):
    _configure_shadow(monkeypatch, tmp_path, family_scopes={"nba_singles": "single"})

    run = Run(kind="refresh", status="completed")
    db_session.add(run)
    db_session.flush()
    base_time = datetime.now(timezone.utc) - timedelta(hours=1)
    for index in range(260):
        _seed_prediction(
            db_session,
            ticker=f"NBA-BACKFILL-{index:03d}",
            market_id=1000 + index,
            run_id=run.id,
            captured_at=base_time - timedelta(minutes=260 - index),
        )
    db_session.commit()

    shadow_run = run_shadow_capture_cycle(db_session, scope="backfill")
    db_session.commit()

    captured = db_session.scalars(select(ShadowInference).order_by(ShadowInference.id.asc())).all()
    assert shadow_run.records_processed == 250
    assert len(captured) == 250
    assert captured[0].ticker == "NBA-BACKFILL-000"
    assert captured[-1].ticker == "NBA-BACKFILL-249"
    assert "NBA-BACKFILL-250" not in {row.ticker for row in captured}
