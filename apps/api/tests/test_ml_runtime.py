import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.models import ModelFamilyRuntimeHealth
from app.services.ml.runtime import resolve_family_runtime, run_serving_inference, run_shadow_inference


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _write_artifact(
    tmp_path,
    *,
    family_key: str,
    scope: str,
    probability: float = 0.64,
    behavior: str = "static_probability",
):
    artifact_path = tmp_path / f"{family_key}.json"
    artifact_path.write_text(
        json.dumps(
            {
                "family_key": family_key,
                "scope": scope,
                "behavior": behavior,
                "probability": probability,
                "confidence": probability,
                "metadata": {"source": "test-artifact"},
            }
        ),
        encoding="utf-8",
    )
    return artifact_path


def _write_manifest(tmp_path, *, family_key: str, artifact_path: str, mode: str = "ml"):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "test-manifest",
                "serving_mode": "ml",
                "families": [
                    {
                        "family_key": family_key,
                        "model_name": f"{family_key}-model",
                        "model_version": "test-v1",
                        "calibration_version": "test-cal",
                        "feature_set_version": "test-features",
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


def test_resolve_family_runtime_global_heuristic_overrides_family_modes(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="ml")
    monkeypatch.setenv("ML_SERVING_MODE", "heuristic")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    decision = resolve_family_runtime(db_session, "nba_singles", scope="single")

    assert decision.desired_mode == "heuristic"
    assert decision.effective_mode == "heuristic"
    assert decision.fallback_active is False


def test_ml_family_modes_override_manifest_mode(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    decision = resolve_family_runtime(db_session, "nba_singles", scope="single")

    assert decision.desired_mode == "ml"
    assert decision.effective_mode == "ml"
    assert decision.runtime_health == "healthy"


def test_run_serving_inference_falls_back_when_artifact_missing(db_session, monkeypatch, tmp_path):
    missing_path = tmp_path / "missing-artifact.json"
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(missing_path), mode="ml")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, decision = run_serving_inference(db_session, family_key="nba_singles", scope="single")

    assert result is None
    assert decision.desired_mode == "ml"
    assert decision.effective_mode == "heuristic"
    assert decision.runtime_health == "unavailable"
    assert decision.fallback_active is True


def test_run_serving_inference_degrades_and_recovers_after_cooldown(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single", behavior="raise")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="ml")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    third_decision = None
    for _ in range(3):
        _, third_decision = run_serving_inference(db_session, family_key="nba_singles", scope="single")

    assert third_decision is not None
    assert third_decision.runtime_health == "degraded"
    assert third_decision.fallback_active is True

    artifact_path.write_text(
        json.dumps(
            {
                "family_key": "nba_singles",
                "scope": "single",
                "behavior": "static_probability",
                "probability": 0.61,
                "confidence": 0.61,
                "metadata": {"source": "recovered"},
            }
        ),
        encoding="utf-8",
    )
    runtime_row = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one()
    runtime_row.degraded_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.flush()

    result, recovered_decision = run_serving_inference(db_session, family_key="nba_singles", scope="single")

    assert result is not None
    assert recovered_decision.effective_mode == "ml"
    assert recovered_decision.runtime_health == "healthy"
    assert recovered_decision.fallback_active is False


def test_run_shadow_inference_failure_does_not_activate_serving_fallback(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single", behavior="raise")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "shadow"}))

    result, decision = run_shadow_inference(db_session, family_key="nba_singles", scope="single")

    assert result is None
    assert decision.desired_mode == "shadow"
    assert decision.fallback_active is False
