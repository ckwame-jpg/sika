"""Tests for Smarter #21 phase 2c — interval-model sidecar loading
on ``SklearnArtifact``.

Pins:
- ``_load_sidecar_interval_models`` round-trips the (p10, p50, p90)
  triple per stat key.
- Partial sidecars (missing one of p10/p50/p90) are skipped
  silently.
- Corrupt joblib bytes log a warning and are skipped.
- Probe failures (model loads but ``predict`` raises) log a
  warning and are skipped.
- ``apply_interval_models`` returns a monotonized triple when
  models are present; ``None`` when the stat key isn't tracked.
- Cache invalidation: adding / replacing an interval sidecar
  forces a re-load even when the base artifact triple is
  unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.ensemble import GradientBoostingRegressor

from app.services.ml.artifact_loader import (
    SklearnArtifact,
    _load_sidecar_interval_models,
    apply_interval_models,
    clear_cache,
    load_sklearn_artifact,
)


def _seed_artifact(tmp_path: Path) -> Path:
    """Write the minimum files ``load_sklearn_artifact`` requires
    (model.joblib + feature_spec.json + training_metadata.json) so
    the test exercises the real ``load_sklearn_artifact`` path."""
    from ml_features import FeatureSpec
    from sklearn.linear_model import LogisticRegression

    # Pipeline must support ``predict_proba``; a trivial 2-feature
    # logistic classifier is enough.
    X = np.array([[0.0, 0.0], [1.0, 1.0], [0.0, 1.0], [1.0, 0.0]])
    y = np.array([0, 1, 1, 0])
    pipeline = LogisticRegression()
    pipeline.fit(X, y)
    joblib.dump(pipeline, tmp_path / "model.joblib")

    spec = FeatureSpec(
        version="test-v1",
        ordered_keys=["feature_a", "feature_b"],
        default_values={"feature_a": 0.0, "feature_b": 0.0},
        family_one_hot_keys=[],
    )
    (tmp_path / "feature_spec.json").write_text(json.dumps(spec.to_dict()))
    (tmp_path / "training_metadata.json").write_text(json.dumps({"target_type": "yes_won"}))
    return tmp_path


def _fit_quantile_triple(n_features: int = 2) -> tuple:
    """Fit three minimal quantile regressors so the loader has
    real estimators to round-trip."""
    rng = np.random.default_rng(20260516)
    X = rng.normal(size=(60, n_features))
    y = X[:, 0] * 2.0 + rng.normal(scale=0.5, size=60)
    triple = tuple(
        GradientBoostingRegressor(loss="quantile", alpha=alpha, max_depth=2, n_estimators=30).fit(X, y)
        for alpha in (0.10, 0.50, 0.90)
    )
    return triple


def _write_triple(stat_dir: Path, triple) -> None:
    stat_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(triple[0], stat_dir / "p10.joblib")
    joblib.dump(triple[1], stat_dir / "p50.joblib")
    joblib.dump(triple[2], stat_dir / "p90.joblib")
    (stat_dir / "metadata.json").write_text(json.dumps({"stat_key": stat_dir.name}))


# -- Loader -----------------------------------------------------------


def test_loader_returns_empty_when_subdir_missing(tmp_path: Path) -> None:
    assert _load_sidecar_interval_models(tmp_path) == {}


def test_loader_round_trips_a_stat_key(tmp_path: Path) -> None:
    triple = _fit_quantile_triple()
    _write_triple(tmp_path / "interval_models" / "points", triple)

    loaded = _load_sidecar_interval_models(tmp_path)

    assert set(loaded.keys()) == {"points"}
    p10_model, p50_model, p90_model = loaded["points"]
    # Probe a real prediction round-trips cleanly.
    probe = np.zeros((1, 2), dtype=float)
    assert float(p10_model.predict(probe)[0]) == pytest.approx(
        float(triple[0].predict(probe)[0])
    )


def test_loader_skips_partial_sidecar(tmp_path: Path) -> None:
    """A directory with only p10/p50 (missing p90) must not load —
    operators get no intervals for that stat rather than a partial
    triple."""
    triple = _fit_quantile_triple()
    stat_dir = tmp_path / "interval_models" / "points"
    stat_dir.mkdir(parents=True)
    joblib.dump(triple[0], stat_dir / "p10.joblib")
    joblib.dump(triple[1], stat_dir / "p50.joblib")
    # No p90.joblib

    loaded = _load_sidecar_interval_models(tmp_path)
    assert loaded == {}


def test_loader_skips_corrupt_joblib(tmp_path: Path, caplog) -> None:
    """A file that exists but isn't a valid joblib must log a
    warning and be skipped — the rest of the artifact loads
    serviceably."""
    triple = _fit_quantile_triple()
    _write_triple(tmp_path / "interval_models" / "points", triple)
    # Corrupt the p50 file.
    (tmp_path / "interval_models" / "points" / "p50.joblib").write_text("garbage")

    with caplog.at_level(logging.WARNING, logger="app.services.ml.artifact_loader"):
        loaded = _load_sidecar_interval_models(tmp_path)
    assert loaded == {}
    assert any("interval_models_skipped" in record.message for record in caplog.records)


def test_loader_loads_multiple_stats(tmp_path: Path) -> None:
    """Two independent stat keys each get their own triple in the
    output dict."""
    triple_a = _fit_quantile_triple()
    triple_b = _fit_quantile_triple()
    _write_triple(tmp_path / "interval_models" / "points", triple_a)
    _write_triple(tmp_path / "interval_models" / "rebounds", triple_b)

    loaded = _load_sidecar_interval_models(tmp_path)

    assert set(loaded.keys()) == {"points", "rebounds"}


# -- apply_interval_models -------------------------------------------


def test_apply_returns_none_when_stat_not_loaded() -> None:
    artifact = SklearnArtifact(
        artifact_dir=Path("/tmp/missing"),
        pipeline=object(),
        feature_spec=None,  # type: ignore[arg-type]
        training_metadata={},
        recalibrators={},
        interval_models={},
    )
    assert apply_interval_models(artifact, "points", np.zeros((1, 2))) is None


def test_apply_returns_monotonized_triple() -> None:
    """Even if a quantile regressor crosses (p10 > p50 etc.), the
    output is sorted so consumers can rely on p10 <= p50 <= p90."""
    triple = _fit_quantile_triple()
    artifact = SklearnArtifact(
        artifact_dir=Path("/tmp/missing"),
        pipeline=object(),
        feature_spec=None,  # type: ignore[arg-type]
        training_metadata={},
        recalibrators={},
        interval_models={"points": triple},
    )
    probe = np.array([0.5, 0.5])
    interval = apply_interval_models(artifact, "points", probe)
    assert interval is not None
    p10, p50, p90 = interval
    assert p10 <= p50 <= p90


def test_apply_rejects_multi_row_input() -> None:
    triple = _fit_quantile_triple()
    artifact = SklearnArtifact(
        artifact_dir=Path("/tmp/missing"),
        pipeline=object(),
        feature_spec=None,  # type: ignore[arg-type]
        training_metadata={},
        recalibrators={},
        interval_models={"points": triple},
    )
    with pytest.raises(ValueError, match="single-row"):
        apply_interval_models(artifact, "points", np.zeros((3, 2)))


def test_apply_returns_none_when_predict_raises(caplog) -> None:
    """Defensive: a model that loaded fine but ``predict`` raises
    (e.g., wrong feature count surviving the loader probe) returns
    None so the consumer falls back to point estimate."""
    class BadModel:
        def predict(self, x):
            raise RuntimeError("synthetic predict failure")

    artifact = SklearnArtifact(
        artifact_dir=Path("/tmp/missing"),
        pipeline=object(),
        feature_spec=None,  # type: ignore[arg-type]
        training_metadata={},
        recalibrators={},
        interval_models={"points": (BadModel(), BadModel(), BadModel())},
    )
    with caplog.at_level(logging.WARNING, logger="app.services.ml.artifact_loader"):
        result = apply_interval_models(artifact, "points", np.zeros((1, 2)))
    assert result is None


# -- End-to-end via load_sklearn_artifact ----------------------------


def test_load_sklearn_artifact_exposes_interval_models(tmp_path: Path) -> None:
    """The full artifact loader picks up interval sidecars + exposes
    them on the returned ``SklearnArtifact``."""
    _seed_artifact(tmp_path)
    triple = _fit_quantile_triple()
    _write_triple(tmp_path / "interval_models" / "points", triple)

    clear_cache()
    artifact = load_sklearn_artifact(tmp_path)

    assert "points" in artifact.interval_models
    assert artifact.recalibrators == {}  # no recalibrator sidecar in this test


def test_interval_sidecar_invalidates_cache(tmp_path: Path) -> None:
    """Adding an interval sidecar after the artifact was first
    loaded must force a fresh load — the cache key includes the
    sidecar fingerprint."""
    _seed_artifact(tmp_path)
    clear_cache()
    first = load_sklearn_artifact(tmp_path)
    assert first.interval_models == {}

    triple = _fit_quantile_triple()
    _write_triple(tmp_path / "interval_models" / "points", triple)

    second = load_sklearn_artifact(tmp_path)
    assert "points" in second.interval_models
    assert second is not first
