import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import ModelFamilyRuntimeHealth, OperatorSetting, Prediction, RefreshJob, Run, ShadowInference
from app.services.ingestion import run_shadow_capture_cycle
from app.services.ml.runtime import sync_family_runtime_health


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
                "metadata": {"source": "readiness-test"},
            }
        ),
        encoding="utf-8",
    )
    return artifact_path


def _write_manifest(tmp_path, *, family_key: str, artifact_path: str, mode: str):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "readiness-test",
                "serving_mode": "ml",
                "families": [
                    {
                        "family_key": family_key,
                        "model_name": f"{family_key}-model",
                        "model_version": "v1",
                        "calibration_version": "cal-v1",
                        "feature_set_version": "features-v1",
                        "artifact_path": artifact_path,
                        "mode": mode,
                        "metadata": {"source": "manifest"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _seed_nba_single_predictions(db_session, *, total: int, settled: int, run_id: int = 1, market_id_offset: int = 0):
    captured_at = datetime.now(timezone.utc) - timedelta(days=1)
    settled_at_value = captured_at + timedelta(hours=2)
    for index in range(total):
        outcome = "pending"
        settlement_status = "pending"
        settled_at = None
        realized_pnl = None
        if index < settled:
            outcome = "won" if index % 2 == 0 else "lost"
            settlement_status = "settled"
            settled_at = settled_at_value
            realized_pnl = 0.18 if outcome == "won" else -0.42

        db_session.add(
            Prediction(
                run_id=run_id,
                event_id=None,
                market_id=market_id_offset + index + 1,
                ticker=f"NBA-READINESS-{run_id}-{index}",
                sport_key="NBA",
                event_name="Test game",
                market_title="Test market",
                market_family="winner",
                market_kind="game_winner",
                side="yes",
                action="buy",
                suggested_price=0.45,
                fair_yes_price=0.58,
                fair_no_price=0.42,
                edge=0.13,
                confidence=0.68,
                selection_score=0.17,
                model_name="heuristic-v1",
                rationale="Test rationale",
                reasons=["test"],
                features={"family_key": "nba_singles"},
                scoring_diagnostics={
                    "feature_flags": {"market_snapshot": True, "team_context": True},
                    "missing_context": [],
                    "penalties": {"thin_sample": 0.0, "missing_context": 0.0, "stale_data": 0.0},
                },
                market_status_at_capture="active",
                settlement_status=settlement_status,
                prediction_outcome=outcome,
                settled_at=settled_at,
                realized_pnl=realized_pnl,
                captured_at=captured_at,
            )
        )
    db_session.flush()


def _seed_nba_coverage_predictions(db_session, *, total: int, settled: int):
    captured_at = datetime.now(timezone.utc) - timedelta(days=1)
    settled_at_value = captured_at + timedelta(hours=2)
    for index in range(total):
        outcome = "pending"
        settlement_status = "pending"
        settled_at = None
        realized_pnl = None
        if index < settled:
            outcome = "won" if index % 2 == 0 else "lost"
            settlement_status = "settled"
            settled_at = settled_at_value
            realized_pnl = 0.18 if outcome == "won" else -0.42

        db_session.add(
            Prediction(
                run_id=1,
                event_id=None,
                market_id=10_000 + index,
                ticker=f"NBA-COVERAGE-{index}",
                sport_key="NBA",
                event_name="Coverage game",
                market_title="Coverage market",
                market_family="player_prop",
                market_kind="player_prop",
                capture_scope="coverage",
                side="yes",
                action="buy",
                suggested_price=0.45,
                fair_yes_price=0.58,
                fair_no_price=0.42,
                edge=0.13,
                confidence=0.68,
                selection_score=0.17,
                model_name="heuristic-v1",
                rationale="Coverage rationale",
                reasons=["coverage"],
                features={},
                scoring_diagnostics={},
                market_status_at_capture="active",
                settlement_status=settlement_status,
                prediction_outcome=outcome,
                settled_at=settled_at,
                realized_pnl=realized_pnl,
                captured_at=captured_at,
            )
        )
    db_session.flush()


def _seed_shadow_singles(db_session, *, count: int):
    predictions = db_session.scalars(
        select(Prediction)
        .where(Prediction.capture_scope != "coverage")
        .order_by(Prediction.captured_at.asc(), Prediction.id.asc())
        .limit(count)
    ).all()
    for prediction in predictions:
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
                model_metadata={"family_key": "nba_singles"},
                rationale="Shadow",
                reasons=["shadow"],
                features={},
                captured_at=prediction.captured_at,
            )
        )
    db_session.flush()


def test_models_readiness_endpoint_reports_insufficient_history_for_active_study_with_no_history(client):
    response = client.get("/ops/models/readiness")

    assert response.status_code == 200
    payload = response.json()
    nba = next(item for item in payload["families"] if item["family_key"] == "nba_singles")
    assert nba["study_track"] == "active"
    assert nba["readiness_status"] == "insufficient_history"


def test_models_readiness_settings_update_enables_shadow_and_queues_backfill(client, db_session):
    response = client.patch(
        "/ops/models/readiness/settings",
        json={"ml_serving_mode": "shadow"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ml_serving_mode"] == "shadow"
    assert payload["shadow_enabled"] is True
    assert payload["auto_promotion_enabled"] is False

    setting = db_session.scalar(select(OperatorSetting).where(OperatorSetting.key == "ml_serving_mode"))
    assert setting is not None
    assert setting.value["mode"] == "shadow"

    job = db_session.scalar(select(RefreshJob).where(RefreshJob.kind == "shadow_capture", RefreshJob.scope == "backfill"))
    assert job is not None
    assert job.status == "queued"


def test_models_readiness_settings_update_arms_auto_promotion(client, db_session):
    response = client.patch(
        "/ops/models/readiness/settings",
        json={"ml_serving_mode": "ml", "enqueue_shadow_backfill": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ml_serving_mode"] == "ml"
    assert payload["shadow_enabled"] is True
    assert payload["auto_promotion_enabled"] is True

    setting = db_session.scalar(select(OperatorSetting).where(OperatorSetting.key == "ml_serving_mode"))
    assert setting is not None
    assert setting.value["mode"] == "ml"

    job = db_session.scalar(select(RefreshJob).where(RefreshJob.kind == "shadow_capture"))
    assert job is None


def test_models_readiness_endpoint_marks_locked_and_heuristic_families(client):
    response = client.get("/ops/models/readiness")

    assert response.status_code == 200
    payload = response.json()
    by_key = {item["family_key"]: item for item in payload["families"]}

    for family_key in {
        "nba_singles",
        "mlb_singles",
        "nba_props",
        "mlb_props",
        "nba_parlay_2leg",
        "mlb_parlay_2leg",
        "mixed_parlay_2leg",
    }:
        assert by_key[family_key]["study_track"] == "active"

    for family_key in {
        "nba_parlay_3leg",
        "mlb_parlay_3leg",
        "mixed_parlay_3leg",
        "parlay_4_6_leg_combiner",
    }:
        assert by_key[family_key]["study_track"] == "heuristic_only"
        assert by_key[family_key]["readiness_status"] == "heuristic_only"


def test_models_readiness_endpoint_reports_insufficient_history(client, db_session):
    _seed_nba_single_predictions(db_session, total=10, settled=10)
    db_session.commit()

    response = client.get("/ops/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["readiness_status"] == "insufficient_history"
    assert payload["settled_predictions"] == 10


def test_models_readiness_endpoint_reports_shadow_not_started_for_active_study_family(client, db_session):
    _seed_nba_single_predictions(db_session, total=40, settled=40)
    db_session.commit()

    response = client.get("/ops/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["study_track"] == "active"
    assert payload["readiness_status"] == "shadow_not_started"
    assert payload["runtime"]["desired_mode"] == "heuristic"
    assert "global ML mode is locked to heuristic" in payload["why_not_ready"]


def test_models_readiness_endpoint_separates_coverage_history_from_recommendation_history(client, db_session):
    _seed_nba_coverage_predictions(db_session, total=12, settled=8)
    db_session.commit()

    response = client.get("/ops/models/readiness/nba_props")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_predictions"] == 0
    assert payload["settled_predictions"] == 0
    assert payload["coverage_predictions"] == 12
    assert payload["coverage_settled_predictions"] == 8


def test_models_readiness_endpoint_counts_coverage_settled_toward_active_study_gate(client, db_session):
    # Coverage-only families (markets the heuristic never recommends but does score) must
    # still ramp the active ML study gate, otherwise efficient single markets like NBA
    # spreads/totals would be stuck at 0 settled forever.
    _seed_nba_coverage_predictions(db_session, total=40, settled=40)
    db_session.commit()

    response = client.get("/ops/models/readiness/nba_props")

    assert response.status_code == 200
    payload = response.json()
    assert payload["coverage_settled_predictions"] == 40
    assert payload["readiness_status"] == "shadow_not_started"


def test_models_readiness_endpoint_reports_shadowing_with_low_coverage(client, db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "shadow"}))
    get_settings.cache_clear()
    _seed_nba_single_predictions(db_session, total=40, settled=40)
    _seed_shadow_singles(db_session, count=10)
    sync_family_runtime_health(db_session)
    db_session.commit()

    response = client.get("/ops/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["readiness_status"] == "shadowing"
    assert payload["runtime"]["desired_mode"] == "shadow"
    assert payload["runtime"]["effective_mode"] == "shadow"


def test_models_readiness_endpoint_reports_ready_for_review(client, db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "shadow"}))
    get_settings.cache_clear()
    _seed_nba_single_predictions(db_session, total=40, settled=40)
    _seed_shadow_singles(db_session, count=30)
    sync_family_runtime_health(db_session)
    db_session.commit()

    response = client.get("/ops/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["readiness_status"] == "ready_for_review"
    assert payload["shadow_coverage_ratio"] == 0.75
    assert "does not enable live ML serving" in payload["why_not_ready"]


def test_models_readiness_endpoint_explains_missing_shadow_manifest_entry(client, db_session, monkeypatch):
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.delenv("ML_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("ML_FAMILY_MODES_JSON", raising=False)
    get_settings.cache_clear()
    _seed_nba_single_predictions(db_session, total=40, settled=40)
    db_session.commit()

    response = client.get("/ops/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["readiness_status"] == "shadow_not_started"
    assert "no shadow artifact manifest entry is configured" in payload["why_not_ready"]


def test_shadow_capture_cycle_uses_only_the_source_refresh_run_and_advances_readiness(db_session, client, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="heuristic")
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.delenv("ML_FAMILY_MODES_JSON", raising=False)
    get_settings.cache_clear()

    history_run = Run(kind="refresh", status="completed")
    source_run = Run(kind="refresh", status="completed")
    other_run = Run(kind="refresh", status="completed")
    db_session.add_all([history_run, source_run, other_run])
    db_session.flush()
    _seed_nba_single_predictions(db_session, total=40, settled=40, run_id=history_run.id)
    _seed_nba_single_predictions(db_session, total=5, settled=0, run_id=source_run.id, market_id_offset=100)
    _seed_nba_single_predictions(db_session, total=3, settled=0, run_id=other_run.id, market_id_offset=200)
    db_session.commit()

    shadow_run = run_shadow_capture_cycle(db_session, scope="current_slate", source_run_id=source_run.id)
    db_session.commit()

    assert shadow_run.kind == "shadow_capture"
    assert shadow_run.records_processed == 5
    assert shadow_run.details["shadow_capture_scope"] == "current_slate"
    assert shadow_run.details["source_run_id"] == source_run.id
    assert shadow_run.details["shadow_predictions_captured"] == 5
    assert shadow_run.details["shadow_parlay_predictions_captured"] == 0
    assert db_session.query(ShadowInference).count() == 5
    captured_rows = db_session.query(ShadowInference).all()
    captured_tickers = {item.ticker for item in captured_rows}
    assert all(ticker.startswith(f"NBA-READINESS-{source_run.id}-") for ticker in captured_tickers)
    assert all(item.source_prediction_id is not None for item in captured_rows)

    response = client.get("/ops/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["readiness_status"] == "shadowing"
    assert payload["shadow_predictions"] == 5
    assert payload["shadow_backlog_predictions"] == 43
    assert payload["last_shadow_capture_at"] is not None
    assert payload["runtime"]["desired_mode"] == "shadow"
    assert payload["runtime"]["effective_mode"] == "shadow"


def test_models_readiness_endpoint_uses_retained_study_window_for_shadow_coverage(client, db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "shadow"}))
    get_settings.cache_clear()
    _seed_nba_single_predictions(db_session, total=10, settled=10, run_id=1, market_id_offset=0)
    _seed_shadow_singles(db_session, count=10)
    old_prediction = Prediction(
        run_id=2,
        event_id=None,
        market_id=5_000,
        ticker="NBA-OLD-READINESS",
        sport_key="NBA",
        event_name="Old game",
        market_title="Old market",
        market_family="winner",
        market_kind="game_winner",
        side="yes",
        action="buy",
        suggested_price=0.45,
        fair_yes_price=0.58,
        fair_no_price=0.42,
        edge=0.13,
        confidence=0.68,
        selection_score=0.17,
        model_name="heuristic-v1",
        rationale="Old prediction",
        reasons=["old"],
        features={"family_key": "nba_singles"},
        scoring_diagnostics={},
        market_status_at_capture="closed",
        settlement_status="settled",
        prediction_outcome="won",
        settled_at=datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc),
        realized_pnl=0.2,
        captured_at=datetime.now(timezone.utc) - timedelta(days=45),
    )
    db_session.add(old_prediction)
    db_session.flush()
    db_session.add(
        ShadowInference(
            run_id=old_prediction.run_id,
            source_prediction_id=old_prediction.id,
            event_id=old_prediction.event_id,
            market_id=old_prediction.market_id,
            ticker=old_prediction.ticker,
            sport_key=old_prediction.sport_key,
            event_name=old_prediction.event_name,
            market_title=old_prediction.market_title,
            market_family=old_prediction.market_family,
            market_kind=old_prediction.market_kind,
            inference_scope="single",
            recommended_side=old_prediction.side,
            suggested_price=old_prediction.suggested_price,
            fair_yes_price=0.6,
            fair_no_price=0.4,
            edge=0.15,
            confidence=0.6,
            model_name="nba_singles-model",
            model_version="v1",
            calibration_version="cal-v1",
            feature_set_version="features-v1",
            model_metadata={"family_key": "nba_singles"},
            rationale="Old shadow",
            reasons=["shadow"],
            features={},
            captured_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
    )
    sync_family_runtime_health(db_session)
    db_session.commit()

    response = client.get("/ops/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_predictions"] == 10
    assert payload["shadow_predictions"] == 10
    assert payload["shadow_coverage_ratio"] == 1.0


def test_models_readiness_endpoint_reports_serving_with_fallback_active(client, db_session, monkeypatch, tmp_path):
    missing_path = tmp_path / "missing.json"
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(missing_path), mode="ml")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))
    get_settings.cache_clear()
    _seed_nba_single_predictions(db_session, total=40, settled=40)
    sync_family_runtime_health(db_session)
    db_session.commit()

    response = client.get("/ops/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["readiness_status"] == "serving"
    assert payload["runtime"]["fallback_active"] is True
    assert payload["runtime"]["effective_mode"] == "heuristic"


def test_models_readiness_endpoint_does_not_mutate_runtime_health_on_get(client, db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "shadow"}))
    get_settings.cache_clear()
    sync_family_runtime_health(db_session)
    db_session.commit()

    before = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one()
    snapshot = {
      "last_check_at": before.last_check_at,
      "desired_mode": before.desired_mode,
      "effective_mode": before.effective_mode,
      "runtime_health": before.runtime_health,
    }

    response = client.get("/ops/models/readiness/nba_singles")

    assert response.status_code == 200
    after = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one()
    assert after.last_check_at == snapshot["last_check_at"]
    assert after.desired_mode == snapshot["desired_mode"]
    assert after.effective_mode == snapshot["effective_mode"]
    assert after.runtime_health == snapshot["runtime_health"]
