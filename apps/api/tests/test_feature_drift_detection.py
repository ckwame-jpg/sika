"""Tests for Smarter #27 — train/serve feature-dictionary drift detection.

Covers:
- ``_detect_feature_drift`` returns the expected diff sets for each
  drift scenario, with known-legitimate misses filtered out.
- ``_run_artifact_inference`` logs a WARNING when drift is detected and
  no log entry when serving and training schemas agree.
- The logging is log-only — inference output is identical regardless of
  whether drift was detected.
"""

import json
import logging
from typing import Any

import joblib
import numpy as np
import pytest

from app.services.ml.artifact_loader import clear_cache
from app.services.ml.runtime import (
    _TRAINING_ONLY_FEATURE_KEYS,
    _VECTORIZE_NON_ORDERED_KEYS,
    _detect_feature_drift,
    _run_artifact_inference,
)


class _ConstantPredictor:
    """Minimal predictor so we exercise the full sklearn behavior branch."""

    def predict_proba(self, vector):
        rows = len(vector)
        return np.asarray([[0.4, 0.6] for _ in range(rows)])


@pytest.fixture(autouse=True)
def _clear_artifact_cache():
    clear_cache()
    yield
    clear_cache()


def _write_artifact(
    tmp_path,
    *,
    ordered_keys: list[str],
    default_values: dict[str, float] | None = None,
    family_one_hot_keys: list[str] | None = None,
):
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    joblib.dump(_ConstantPredictor(), artifact_dir / "model.joblib")
    defaults = default_values or {key: 0.0 for key in ordered_keys}
    one_hot = family_one_hot_keys or []
    (artifact_dir / "feature_spec.json").write_text(
        json.dumps(
            {
                "version": "drift-test-v1",
                "ordered_keys": ordered_keys,
                "default_values": defaults,
                "family_one_hot_keys": one_hot,
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "training_metadata.json").write_text(
        json.dumps({"trained_at": "2026-05-14T00:00:00Z"}),
        encoding="utf-8",
    )
    return artifact_dir


# -- _detect_feature_drift helper ----------------------------------------


def test_detect_drift_returns_empty_sets_when_schemas_agree() -> None:
    features = {"recent_average": 21.4, "threshold": 20.5}
    extra, missing = _detect_feature_drift(features, ["recent_average", "threshold"])
    assert extra == set()
    assert missing == set()


def test_detect_drift_flags_extra_serving_key() -> None:
    # Serving emits a new feature the model wasn't trained on — silently
    # dropped by vectorize. Smarter #12's ``nba_offense_interaction_term``
    # would land in this state until the next retrain.
    features = {
        "recent_average": 21.4,
        "threshold": 20.5,
        "nba_offense_interaction_term": 1.18,
    }
    extra, missing = _detect_feature_drift(features, ["recent_average", "threshold"])
    assert extra == {"nba_offense_interaction_term"}
    assert missing == set()


def test_detect_drift_flags_missing_serving_key() -> None:
    # The trained model expects ``recent_workload_minutes_per_game`` but
    # scoring forgot to emit it. The vector silently falls back to the
    # default, biasing inference.
    features = {"recent_average": 21.4}
    extra, missing = _detect_feature_drift(
        features,
        ["recent_average", "recent_workload_minutes_per_game"],
    )
    assert extra == set()
    assert missing == {"recent_workload_minutes_per_game"}


def test_detect_drift_filters_known_training_only_missing_keys() -> None:
    # ``sport_is_nba`` is in every trained spec but is added by
    # ``dataset.py`` normalization, never by the scoring path. It must
    # not trip the drift detector.
    features = {"recent_average": 21.4}
    extra, missing = _detect_feature_drift(
        features,
        [
            "recent_average",
            "sport_is_nba",
            "sport_is_mlb",
            "sport_is_wnba",
            "sport_is_nfl",
        ],
    )
    assert extra == set()
    assert missing == set()


def test_detect_drift_filters_family_key_from_extra_when_only_in_serving() -> None:
    # ``family_key`` is part of the serving features dict but ``vectorize``
    # reads it from a separate slot, so it intentionally isn't in
    # ``ordered_keys``. Don't flag it as extra.
    features = {"family_key": "nba_props", "recent_average": 21.4}
    extra, missing = _detect_feature_drift(features, ["recent_average"])
    assert extra == set()
    assert missing == set()


def test_detect_drift_combines_extra_and_unexpected_missing() -> None:
    features = {
        "family_key": "nba_props",
        "recent_average": 21.4,
        "brand_new_key": 0.5,
    }
    ordered = [
        "recent_average",
        "recent_workload_minutes_per_game",
        "sport_is_nba",  # legitimate-only — must not appear in missing.
    ]
    extra, missing = _detect_feature_drift(features, ordered)
    assert extra == {"brand_new_key"}
    assert missing == {"recent_workload_minutes_per_game"}


def test_detect_drift_handles_empty_features_dict() -> None:
    extra, missing = _detect_feature_drift({}, ["recent_average", "threshold"])
    assert extra == set()
    assert missing == {"recent_average", "threshold"}


def test_detect_drift_handles_empty_ordered_keys() -> None:
    features = {"recent_average": 21.4, "threshold": 20.5}
    extra, missing = _detect_feature_drift(features, [])
    assert extra == {"recent_average", "threshold"}
    assert missing == set()


def test_known_training_only_set_includes_sport_indicators() -> None:
    # Drift guard on the legitimate-miss list itself. These sport_is_*
    # keys are row-derived training enrichments, so serving legitimately
    # omits every one of them.
    assert "sport_is_nba" in _TRAINING_ONLY_FEATURE_KEYS
    assert "sport_is_mlb" in _TRAINING_ONLY_FEATURE_KEYS
    assert "sport_is_wnba" in _TRAINING_ONLY_FEATURE_KEYS
    assert "sport_is_nfl" in _TRAINING_ONLY_FEATURE_KEYS


def test_vectorize_non_ordered_set_includes_family_key() -> None:
    # ``vectorize`` reads family_key from a separate slot; it's not in
    # ordered_keys by design. The detector must not flag it as extra.
    assert "family_key" in _VECTORIZE_NON_ORDERED_KEYS


# -- inference path integration ------------------------------------------


def _run(
    artifact_dir,
    features: dict[str, Any] | None = None,
) -> tuple[float, float, dict[str, Any]]:
    return _run_artifact_inference(
        {"behavior": "sklearn_predict_proba", "artifact_dir": str(artifact_dir)},
        features=features,
    )


def test_inference_emits_no_warning_when_schemas_match(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact_dir = _write_artifact(tmp_path, ordered_keys=["recent_average", "threshold"])
    with caplog.at_level(logging.WARNING, logger="app.services.ml.runtime"):
        prob, conf, metadata = _run(
            artifact_dir,
            features={"recent_average": 21.4, "threshold": 20.5},
        )
    assert prob == pytest.approx(0.6)
    assert conf == pytest.approx(0.6)
    assert metadata["feature_spec_version"] == "drift-test-v1"
    drift_records = [r for r in caplog.records if "feature_drift" in r.getMessage()]
    assert drift_records == []


def test_inference_logs_warning_on_extra_serving_key(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact_dir = _write_artifact(tmp_path, ordered_keys=["recent_average"])
    with caplog.at_level(logging.WARNING, logger="app.services.ml.runtime"):
        prob, _conf, _meta = _run(
            artifact_dir,
            features={"recent_average": 21.4, "brand_new_key": 0.7},
        )
    # Inference output is unaffected by the warning.
    assert prob == pytest.approx(0.6)
    drift_records = [r for r in caplog.records if "feature_drift" in r.getMessage()]
    assert len(drift_records) == 1
    record = drift_records[0]
    assert record.levelno == logging.WARNING
    assert "brand_new_key" in record.getMessage()
    assert getattr(record, "serving_extra", None) == ["brand_new_key"]
    assert getattr(record, "serving_missing", None) == []


def test_inference_logs_warning_on_unexpected_missing_key(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact_dir = _write_artifact(
        tmp_path,
        ordered_keys=["recent_average", "recent_workload_minutes_per_game"],
    )
    with caplog.at_level(logging.WARNING, logger="app.services.ml.runtime"):
        prob, _conf, _meta = _run(artifact_dir, features={"recent_average": 21.4})
    assert prob == pytest.approx(0.6)
    drift_records = [r for r in caplog.records if "feature_drift" in r.getMessage()]
    assert len(drift_records) == 1
    record = drift_records[0]
    assert getattr(record, "serving_missing", None) == [
        "recent_workload_minutes_per_game"
    ]
    assert getattr(record, "serving_extra", None) == []


def test_inference_does_not_warn_on_legitimate_training_only_misses(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    # Mirror the production state: trained spec includes the dataset-time
    # enrichments, scoring path doesn't emit them. No log entry expected.
    artifact_dir = _write_artifact(
        tmp_path,
        ordered_keys=[
            "recent_average",
            "sport_is_nba",
            "sport_is_mlb",
            "sport_is_wnba",
            "sport_is_nfl",
            "heuristic_fair_yes_price",
        ],
    )
    with caplog.at_level(logging.WARNING, logger="app.services.ml.runtime"):
        prob, _conf, _meta = _run(artifact_dir, features={"recent_average": 21.4})
    assert prob == pytest.approx(0.6)
    drift_records = [r for r in caplog.records if "feature_drift" in r.getMessage()]
    assert drift_records == []


def test_inference_does_not_warn_when_serving_emits_family_key(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact_dir = _write_artifact(
        tmp_path,
        ordered_keys=["recent_average"],
        family_one_hot_keys=["nba_singles"],
    )
    with caplog.at_level(logging.WARNING, logger="app.services.ml.runtime"):
        prob, _conf, _meta = _run(
            artifact_dir,
            features={"family_key": "nba_singles", "recent_average": 21.4},
        )
    assert prob == pytest.approx(0.6)
    drift_records = [r for r in caplog.records if "feature_drift" in r.getMessage()]
    assert drift_records == []


def test_inference_warning_carries_artifact_metadata_for_attribution(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact_dir = _write_artifact(tmp_path, ordered_keys=["recent_average"])
    with caplog.at_level(logging.WARNING, logger="app.services.ml.runtime"):
        _run(artifact_dir, features={"recent_average": 21.4, "brand_new_key": 0.7})
    drift_records = [r for r in caplog.records if "feature_drift" in r.getMessage()]
    assert len(drift_records) == 1
    record = drift_records[0]
    assert getattr(record, "feature_spec_version", None) == "drift-test-v1"
    assert str(artifact_dir) == getattr(record, "artifact_dir", None)
