"""Tests for Smarter #21 phase 2a — interval-model sidecar I/O.

Pin the on-disk contract that phase 2c's inference loader will
consume:

- ``train_prop_interval_models`` writes exactly the three joblib
  files + metadata.json layout phase 2c expects.
- ``load_interval_models`` round-trips them and rejects corrupt /
  missing artifacts gracefully.
- Empirical coverage of the [p10, p90] band on synthetic data is
  in the ~80% ballpark (load-bearing — proves the training
  pipeline produced something useful).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from ml.interval_training import (
    INTERVAL_MODELS_SUBDIR,
    IntervalArtifactPaths,
    IntervalTrainingResult,
    interval_models_dir,
    interval_models_paths,
    interval_models_present,
    load_interval_metadata,
    load_interval_models,
    train_prop_interval_models,
)
from ml.quantile_regression import (
    DEFAULT_QUANTILES,
    compute_prediction_interval,
)


_RNG = np.random.default_rng(20260516)


def _seed_synthetic_data(n: int = 200, k: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """``targets = features @ coeffs + noise``. Linear with
    homoscedastic Gaussian noise — easy for the quantile regressors
    to fit, gives a known-shape distribution to verify coverage
    against."""
    features = _RNG.normal(size=(n, k))
    coeffs = np.array([1.0, -0.5, 0.3, 0.8])[:k]
    noise = _RNG.normal(scale=2.0, size=n)
    targets = features @ coeffs + noise
    return features, targets


# -- Path helpers ------------------------------------------------------


def test_interval_models_dir_includes_subdir_and_stat_key(tmp_path: Path) -> None:
    directory = interval_models_dir(tmp_path, "points")
    assert directory == tmp_path / INTERVAL_MODELS_SUBDIR / "points"


def test_interval_models_dir_rejects_empty_stat_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stat_key"):
        interval_models_dir(tmp_path, "")
    with pytest.raises(ValueError, match="stat_key"):
        interval_models_dir(tmp_path, "   ")


def test_interval_models_paths_uses_canonical_filenames(tmp_path: Path) -> None:
    paths = interval_models_paths(tmp_path, "points")
    assert isinstance(paths, IntervalArtifactPaths)
    assert paths.p10.name == "p10.joblib"
    assert paths.p50.name == "p50.joblib"
    assert paths.p90.name == "p90.joblib"
    assert paths.metadata.name == "metadata.json"


def test_interval_models_present_returns_false_for_empty_dir(tmp_path: Path) -> None:
    assert interval_models_present(tmp_path, "points") is False


# -- Training round-trip -----------------------------------------------


def test_train_writes_all_three_models_and_metadata(tmp_path: Path) -> None:
    features, targets = _seed_synthetic_data()
    result = train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="points",
        artifact_dir=tmp_path,
    )
    assert isinstance(result, IntervalTrainingResult)
    assert result.paths.p10.exists()
    assert result.paths.p50.exists()
    assert result.paths.p90.exists()
    assert result.paths.metadata.exists()


def test_train_metadata_records_provenance(tmp_path: Path) -> None:
    features, targets = _seed_synthetic_data()
    window_start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    window_end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    result = train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="points",
        artifact_dir=tmp_path,
        window_start=window_start, window_end=window_end,
    )
    metadata = json.loads(result.paths.metadata.read_text())
    assert metadata["family_key"] == "nba_props"
    assert metadata["stat_key"] == "points"
    assert metadata["sample_size"] == features.shape[0]
    assert metadata["window_start"] == window_start.isoformat()
    assert metadata["window_end"] == window_end.isoformat()
    assert metadata["quantiles"] == list(DEFAULT_QUANTILES)
    # ``trained_at`` must parse as a tz-aware ISO timestamp.
    parsed = datetime.fromisoformat(metadata["trained_at"])
    assert parsed.tzinfo is not None


def test_train_empirical_coverage_in_calibrated_band(tmp_path: Path) -> None:
    """Load-bearing: the [p10, p90] band should cover ~80% of
    training targets on the synthetic linear-with-noise distribution.

    Tolerance is wide (>=65%) because:
    - In-sample coverage is biased upward, but the quantile
      regressors here are small (default GradientBoostingRegressor
      depth=3, n_estimators=100) and noise is large relative to
      signal, so they don't overfit hard.
    - The actual phase-2c gate threshold is a held-out coverage
      metric we'll measure on real data; this test just pins that
      the math produced a useful band, not a degenerate one.
    """
    features, targets = _seed_synthetic_data(n=400)
    result = train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="points",
        artifact_dir=tmp_path,
    )
    assert result.empirical_coverage >= 0.65
    assert result.empirical_coverage <= 1.0


def test_train_rejects_shape_mismatches(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="features must be 2-D"):
        train_prop_interval_models(
            np.array([1.0, 2.0]), np.array([1.0, 2.0]),
            family_key="x", stat_key="y", artifact_dir=tmp_path,
        )
    with pytest.raises(ValueError, match="targets must be 1-D"):
        train_prop_interval_models(
            np.zeros((3, 2)), np.zeros((3, 2)),
            family_key="x", stat_key="y", artifact_dir=tmp_path,
        )
    with pytest.raises(ValueError, match="row mismatch"):
        train_prop_interval_models(
            np.zeros((3, 2)), np.zeros((5,)),
            family_key="x", stat_key="y", artifact_dir=tmp_path,
        )
    with pytest.raises(ValueError, match="at least one row"):
        train_prop_interval_models(
            np.zeros((0, 2)), np.zeros((0,)),
            family_key="x", stat_key="y", artifact_dir=tmp_path,
        )


def test_train_rejects_empty_family_key(tmp_path: Path) -> None:
    features, targets = _seed_synthetic_data(n=50)
    with pytest.raises(ValueError, match="family_key"):
        train_prop_interval_models(
            features, targets,
            family_key="", stat_key="points", artifact_dir=tmp_path,
        )


# -- Load round-trip ---------------------------------------------------


def test_load_returns_none_when_models_missing(tmp_path: Path) -> None:
    assert load_interval_models(tmp_path, "points") is None


def test_load_round_trips_after_train(tmp_path: Path) -> None:
    features, targets = _seed_synthetic_data()
    train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="points",
        artifact_dir=tmp_path,
    )
    loaded = load_interval_models(tmp_path, "points")
    assert loaded is not None
    p10_model, p50_model, p90_model = loaded
    # Probe an actual prediction — round-trip must produce a valid
    # ``(p10, p50, p90)`` triple on a single row.
    interval = compute_prediction_interval(
        p10_model, p50_model, p90_model, features[:1],
    )
    assert interval.p10 <= interval.p50 <= interval.p90


def test_load_returns_none_when_one_file_missing(tmp_path: Path) -> None:
    """Partial sidecar (e.g. a deploy script that copied p10/p50 but
    not p90) must produce ``None`` rather than partial load."""
    features, targets = _seed_synthetic_data()
    train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="points",
        artifact_dir=tmp_path,
    )
    paths = interval_models_paths(tmp_path, "points")
    paths.p90.unlink()  # simulate partial deploy
    assert load_interval_models(tmp_path, "points") is None


def test_load_returns_none_for_corrupt_joblib(tmp_path: Path) -> None:
    """A file that exists but isn't a valid joblib must not crash
    the inference path."""
    features, targets = _seed_synthetic_data(n=50)
    train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="points",
        artifact_dir=tmp_path,
    )
    paths = interval_models_paths(tmp_path, "points")
    paths.p10.write_text("definitely not a pickled estimator")
    assert load_interval_models(tmp_path, "points") is None


# -- Metadata-only load ------------------------------------------------


def test_load_metadata_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_interval_metadata(tmp_path, "points") is None


def test_load_metadata_round_trips(tmp_path: Path) -> None:
    features, targets = _seed_synthetic_data()
    train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="points",
        artifact_dir=tmp_path,
    )
    metadata = load_interval_metadata(tmp_path, "points")
    assert metadata is not None
    assert metadata["family_key"] == "nba_props"
    assert metadata["stat_key"] == "points"


def test_load_metadata_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    features, targets = _seed_synthetic_data(n=50)
    train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="points",
        artifact_dir=tmp_path,
    )
    paths = interval_models_paths(tmp_path, "points")
    paths.metadata.write_text("not valid json")
    assert load_interval_metadata(tmp_path, "points") is None


# -- Cross-stat key isolation ------------------------------------------


def test_train_separates_artifacts_per_stat_key(tmp_path: Path) -> None:
    """Two stat keys train into independent subdirectories so a
    re-train of one doesn't clobber the other."""
    features, targets = _seed_synthetic_data(n=80)
    train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="points", artifact_dir=tmp_path,
    )
    train_prop_interval_models(
        features, targets,
        family_key="nba_props", stat_key="rebounds", artifact_dir=tmp_path,
    )
    assert interval_models_present(tmp_path, "points")
    assert interval_models_present(tmp_path, "rebounds")
    assert interval_models_dir(tmp_path, "points") != interval_models_dir(tmp_path, "rebounds")
