from __future__ import annotations

import json
import os
import time

import joblib
import numpy as np
import pytest

from app.config import get_settings
from app.services.ml.artifact_loader import clear_cache
from app.services.ml.runtime import run_serving_inference


class ConstantPredictor:
    def __init__(self, probability: float):
        self.probability = probability

    def predict_proba(self, vector):
        rows = len(vector)
        return np.asarray([[1.0 - self.probability, self.probability] for _ in range(rows)])


class FailingPredictor:
    def predict_proba(self, vector):
        raise RuntimeError("synthetic predict failure")


@pytest.fixture(autouse=True)
def clear_runtime_state():
    get_settings.cache_clear()
    clear_cache()
    yield
    get_settings.cache_clear()
    clear_cache()


def _write_manifest(tmp_path, *, artifact_path: str, feature_set_version: str = "test-feature-v1"):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "test-manifest",
                "serving_mode": "ml",
                "families": [
                    {
                        "family_key": "global_v1",
                        "serves_family_key": "nba_singles",
                        "model_name": "global-test-model",
                        "model_version": "test-v1",
                        "calibration_version": "test-cal",
                        "feature_set_version": feature_set_version,
                        "artifact_path": artifact_path,
                        "mode": "ml",
                        "metadata": {
                            "behavior": "sklearn_predict_proba",
                            "target_type": "yes_won",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _write_artifact_dir(tmp_path, *, probability: float = 0.71, feature_version: str = "test-feature-v1", predictor=None):
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    joblib.dump(predictor if predictor is not None else ConstantPredictor(probability), artifact_dir / "model.joblib")
    (artifact_dir / "feature_spec.json").write_text(
        json.dumps(
            {
                "version": feature_version,
                "ordered_keys": ["recent_average", "threshold"],
                "default_values": {"recent_average": 0.0, "threshold": 0.0},
                "family_one_hot_keys": ["nba_singles"],
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "training_metadata.json").write_text(
        json.dumps({"trained_at": "2026-04-24T00:00:00Z", "metrics": {"brier": 0.2}}),
        encoding="utf-8",
    )
    return artifact_dir


def test_sklearn_behavior_routes_to_directory_artifact(db_session, monkeypatch, tmp_path):
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.73)
    manifest_path = _write_manifest(tmp_path, artifact_path=str(artifact_dir))
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, decision = run_serving_inference(
        db_session,
        family_key="nba_singles",
        scope="single",
        features={"family_key": "nba_singles", "recent_average": 21.4, "threshold": 20.5},
    )

    assert result is not None
    assert result.probability == pytest.approx(0.73)
    assert result.metadata["feature_spec_version"] == "test-feature-v1"
    assert decision.runtime_health == "healthy"
    assert decision.effective_mode == "ml"


def test_missing_artifact_directory_falls_back_to_heuristic(db_session, monkeypatch, tmp_path):
    manifest_path = _write_manifest(tmp_path, artifact_path=str(tmp_path / "missing-dir"))
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, decision = run_serving_inference(db_session, family_key="nba_singles", scope="single")

    assert result is None
    assert decision.effective_mode == "heuristic"
    assert decision.runtime_health == "unavailable"


def test_feature_spec_version_mismatch_falls_back_to_heuristic(db_session, monkeypatch, tmp_path):
    artifact_dir = _write_artifact_dir(tmp_path, feature_version="artifact-feature-v2")
    manifest_path = _write_manifest(tmp_path, artifact_path=str(artifact_dir), feature_set_version="manifest-feature-v1")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, decision = run_serving_inference(db_session, family_key="nba_singles", scope="single")

    assert result is None
    assert decision.effective_mode == "heuristic"
    assert decision.runtime_health == "unavailable"
    assert "Feature spec version mismatch" in (decision.last_error or "")


def test_malformed_pipeline_marks_runtime_degraded(db_session, monkeypatch, tmp_path):
    artifact_dir = _write_artifact_dir(tmp_path, predictor=FailingPredictor())
    manifest_path = _write_manifest(tmp_path, artifact_path=str(artifact_dir))
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    decision = None
    for _ in range(3):
        result, decision = run_serving_inference(db_session, family_key="nba_singles", scope="single", features={})
        assert result is None

    assert decision is not None
    assert decision.runtime_health == "degraded"
    assert "synthetic predict failure" in (decision.last_error or "")


def test_artifact_cache_respects_mtime(db_session, monkeypatch, tmp_path):
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.22)
    manifest_path = _write_manifest(tmp_path, artifact_path=str(artifact_dir))
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    first, _decision = run_serving_inference(db_session, family_key="nba_singles", scope="single", features={})
    assert first is not None
    assert first.probability == pytest.approx(0.22)

    model_path = artifact_dir / "model.joblib"
    joblib.dump(ConstantPredictor(0.81), model_path)
    future = time.time() + 5
    os.utime(model_path, (future, future))

    second, _decision = run_serving_inference(db_session, family_key="nba_singles", scope="single", features={})
    assert second is not None
    assert second.probability == pytest.approx(0.81)
