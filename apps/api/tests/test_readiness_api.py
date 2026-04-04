import json
from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.models import ModelFamilyRuntimeHealth, Prediction, ShadowInference
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


def _seed_nba_single_predictions(db_session, *, total: int, settled: int):
    for index in range(total):
        outcome = "pending"
        settlement_status = "pending"
        settled_at = None
        realized_pnl = None
        if index < settled:
            outcome = "won" if index % 2 == 0 else "lost"
            settlement_status = "settled"
            settled_at = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
            realized_pnl = 0.18 if outcome == "won" else -0.42

        db_session.add(
            Prediction(
                run_id=1,
                event_id=None,
                market_id=index + 1,
                ticker=f"NBA-READINESS-{index}",
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
                captured_at=datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc),
            )
        )
    db_session.flush()


def _seed_shadow_singles(db_session, *, count: int):
    for index in range(count):
        db_session.add(
            ShadowInference(
                run_id=1,
                event_id=None,
                market_id=index + 1,
                ticker=f"NBA-SHADOW-{index}",
                sport_key="NBA",
                event_name="Test game",
                market_title="Test market",
                market_family="winner",
                market_kind="game_winner",
                inference_scope="single",
                recommended_side="yes",
                suggested_price=0.45,
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
                captured_at=datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc),
            )
        )
    db_session.flush()


def test_models_readiness_endpoint_reports_heuristic_only_with_no_history(client):
    response = client.get("/models/readiness")

    assert response.status_code == 200
    payload = response.json()
    nba = next(item for item in payload["families"] if item["family_key"] == "nba_singles")
    assert nba["readiness_status"] == "heuristic_only"


def test_models_readiness_endpoint_reports_insufficient_history(client, db_session):
    _seed_nba_single_predictions(db_session, total=10, settled=10)
    db_session.commit()

    response = client.get("/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["readiness_status"] == "insufficient_history"
    assert payload["settled_predictions"] == 10


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

    response = client.get("/models/readiness/nba_singles")

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

    response = client.get("/models/readiness/nba_singles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["readiness_status"] == "ready_for_review"
    assert payload["shadow_coverage_ratio"] == 0.75


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

    response = client.get("/models/readiness/nba_singles")

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

    response = client.get("/models/readiness/nba_singles")

    assert response.status_code == 200
    after = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one()
    assert after.last_check_at == snapshot["last_check_at"]
    assert after.desired_mode == snapshot["desired_mode"]
    assert after.effective_mode == snapshot["effective_mode"]
    assert after.runtime_health == snapshot["runtime_health"]
