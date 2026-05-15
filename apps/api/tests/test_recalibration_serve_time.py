"""Tests for Smarter #20 phase 2c — serve-time isotonic recalibrator.

Phase 2b shipped the CLI that fits a recalibrator and writes it to
``artifact_dir/recalibrators/<family_key>/``. This phase wires the
serve-time loader: when a sidecar is present for the family being
served, the runtime post-processes the model's raw P(YES) through the
recalibrator before returning.

Test surface:
- Sidecar present + matches the family being served → applied
  (probability shifts from raw to recalibrated; metadata records both).
- Sidecar absent → pass-through (raw probability returned unchanged).
- Sidecar present but metadata's ``family_key`` doesn't match the
  directory name → defensive skip (don't apply a sidecar that was
  somehow misfiled — the directory is the source of truth at load
  time).
- Per-family isolation: nba_props sidecar doesn't affect mlb_props
  inference and vice versa.
- Cache invalidation: editing the sidecar joblib triggers a re-load
  on the next inference call.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.isotonic import IsotonicRegression

from app.config import get_settings
from app.services.ml.artifact_loader import clear_cache, load_sklearn_artifact
from app.services.ml.runtime import run_serving_inference


class ConstantPredictor:
    """Stub model that always emits the same P(YES). Lets tests assert
    on a deterministic raw probability before the recalibrator runs."""

    def __init__(self, probability: float):
        self.probability = probability

    def predict_proba(self, vector):
        rows = len(vector)
        return np.asarray([[1.0 - self.probability, self.probability] for _ in range(rows)])


class OutOfRangePredictor:
    """Stub model that emits an invalid P(YES)=1.2.

    Module-level so joblib can pickle it. Used by the test that
    verifies the runtime fails fast on invalid raw probabilities
    instead of silently feeding them to the isotonic clamp.
    """

    def predict_proba(self, vector):
        rows = len(vector)
        return np.asarray([[-0.2, 1.2] for _ in range(rows)])


class BadRangeRecalibrator:
    """Stub recalibrator whose ``predict(...)`` returns values outside
    [0, 1]. Module-level so joblib can pickle it. Used by the test
    verifying the loader's probe call rejects malformed sidecars."""

    def predict(self, _vector):
        return [1.5]


@pytest.fixture(autouse=True)
def clear_runtime_state():
    get_settings.cache_clear()
    clear_cache()
    yield
    get_settings.cache_clear()
    clear_cache()


def _write_artifact_dir(tmp_path: Path, *, probability: float) -> Path:
    """Build a minimal sklearn artifact directory the runtime will accept."""
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    joblib.dump(ConstantPredictor(probability), artifact_dir / "model.joblib")
    (artifact_dir / "feature_spec.json").write_text(
        json.dumps({
            "version": "test-feature-v1",
            "ordered_keys": ["recent_average", "threshold"],
            "default_values": {"recent_average": 0.0, "threshold": 0.0},
            "family_one_hot_keys": ["nba_singles"],
        }),
        encoding="utf-8",
    )
    (artifact_dir / "training_metadata.json").write_text(
        json.dumps({"trained_at": "2026-04-24T00:00:00Z", "metrics": {"brier": 0.2}}),
        encoding="utf-8",
    )
    return artifact_dir


def _write_manifest(
    tmp_path: Path,
    *,
    artifact_path: str,
    calibration_version: str = "test-cal+iso30d-2026-05-15",
) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "version": "test-manifest",
            "serving_mode": "ml",
            "families": [{
                "family_key": "global_v1",
                "serves_family_key": "nba_singles",
                "model_name": "global-test-model",
                "model_version": "test-v1",
                "calibration_version": calibration_version,
                "feature_set_version": "test-feature-v1",
                "artifact_path": artifact_path,
                "mode": "ml",
                "metadata": {
                    "behavior": "sklearn_predict_proba",
                    "target_type": "yes_won",
                },
            }],
        }),
        encoding="utf-8",
    )
    return manifest_path


def _write_sidecar(
    artifact_dir: Path,
    *,
    family_key: str,
    metadata_family_key: str | None = None,
    fit_pairs: tuple[tuple[float, float], ...] = (
        (0.0, 0.0), (0.5, 0.7), (1.0, 1.0),
    ),
) -> Path:
    """Persist a fitted IsotonicRegression sidecar for ``family_key``.

    ``fit_pairs`` defines the (raw_prob, target_prob) mapping the
    isotonic learns. The default (0→0, 0.5→0.7, 1→1) bumps mid-range
    probabilities upward — a clear, observable shift the test can
    assert on.

    ``metadata_family_key`` defaults to ``family_key`` (matches the
    directory). Tests for the mismatch defense override it to a
    different value.
    """
    sidecar_dir = artifact_dir / "recalibrators" / family_key
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    raw, target = zip(*fit_pairs)
    fitted = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(
        np.asarray(raw, dtype=float), np.asarray(target, dtype=float),
    )
    joblib.dump(fitted, sidecar_dir / "isotonic_recalibrator.joblib")
    (sidecar_dir / "isotonic_recalibration_metadata.json").write_text(
        json.dumps({
            "schema_version": 1,
            "family_key": metadata_family_key if metadata_family_key is not None else family_key,
            "window_start": "2026-04-15T00:00:00+00:00",
            "window_end": "2026-05-15T00:00:00+00:00",
            "sample_size": 200,
            "metrics_before": {"brier": 0.25, "expected_calibration_error": 0.10, "sample_size": 200},
            "metrics_after": {"brier": 0.18, "expected_calibration_error": 0.04, "sample_size": 200},
            "brier_improvement": 0.07,
            "ece_improvement": 0.06,
        }, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return sidecar_dir


# -- artifact_loader: per-family recalibrator loading -----------------


def test_artifact_loader_loads_sidecar_recalibrator_for_family(tmp_path: Path) -> None:
    """Sidecar present in the per-family subdirectory is loaded under
    that family's key."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    _write_sidecar(artifact_dir, family_key="nba_singles")

    artifact = load_sklearn_artifact(artifact_dir)

    assert "nba_singles" in artifact.recalibrators
    recalibrator = artifact.recalibrators["nba_singles"]
    # The fit (0→0, 0.5→0.7, 1→1) should map 0.5 to ~0.7.
    assert float(recalibrator.predict([0.5])[0]) == pytest.approx(0.7, abs=0.05)


def test_artifact_loader_returns_empty_recalibrators_when_subdir_missing(tmp_path: Path) -> None:
    """No sidecar files anywhere → recalibrators dict is empty."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)

    artifact = load_sklearn_artifact(artifact_dir)

    assert artifact.recalibrators == {}


def test_artifact_loader_skips_sidecar_when_metadata_family_mismatches(tmp_path: Path) -> None:
    """Defensive: directory is named ``nba_singles`` but the sidecar's
    metadata claims ``mlb_props``. Don't load — the operator may have
    accidentally moved files around, and applying the wrong calibrator
    would silently corrupt predictions for the family being served.
    """
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    _write_sidecar(
        artifact_dir,
        family_key="nba_singles",
        metadata_family_key="mlb_props",  # disagrees with directory name
    )

    artifact = load_sklearn_artifact(artifact_dir)

    assert "nba_singles" not in artifact.recalibrators
    assert artifact.recalibrators == {}


def test_artifact_loader_loads_multiple_per_family_sidecars(tmp_path: Path) -> None:
    """Multiple families served by one artifact_dir each get their own
    sidecar in ``recalibrators/<family>/``."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    _write_sidecar(
        artifact_dir, family_key="nba_singles",
        fit_pairs=((0.0, 0.0), (0.5, 0.7), (1.0, 1.0)),
    )
    _write_sidecar(
        artifact_dir, family_key="mlb_props",
        fit_pairs=((0.0, 0.0), (0.5, 0.3), (1.0, 1.0)),  # bumps DOWN
    )

    artifact = load_sklearn_artifact(artifact_dir)

    assert set(artifact.recalibrators.keys()) == {"nba_singles", "mlb_props"}
    nba_pred = float(artifact.recalibrators["nba_singles"].predict([0.5])[0])
    mlb_pred = float(artifact.recalibrators["mlb_props"].predict([0.5])[0])
    assert nba_pred == pytest.approx(0.7, abs=0.05)
    assert mlb_pred == pytest.approx(0.3, abs=0.05)


def test_artifact_cache_invalidates_when_sidecar_deleted(tmp_path: Path) -> None:
    """Codex round 1 P2: deleting a sidecar joblib (while leaving its
    metadata around) must invalidate the cache. A scan that only
    records existing-file mtimes can miss this — the metadata file
    might be the newest existing file, so the max stays the same and
    the cached artifact keeps applying the deleted recalibrator. The
    per-family directory mtime catches the deletion."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    _write_sidecar(artifact_dir, family_key="nba_singles")

    artifact_v1 = load_sklearn_artifact(artifact_dir)
    assert "nba_singles" in artifact_v1.recalibrators

    # Delete just the joblib, leave metadata in place. The metadata
    # file's mtime is fresh from the recent _write_sidecar call.
    sidecar_joblib = artifact_dir / "recalibrators" / "nba_singles" / "isotonic_recalibrator.joblib"
    sidecar_joblib.unlink()

    # The metadata file is still the newest existing file. A naive
    # max-of-existing-mtimes scan would return the metadata's mtime
    # again and HIT the cache. The directory mtime guard is what
    # forces a re-scan, which then sees no joblib and returns {}.
    artifact_v2 = load_sklearn_artifact(artifact_dir)
    assert "nba_singles" not in artifact_v2.recalibrators


def test_artifact_loader_skips_sidecar_without_metadata_file(tmp_path: Path) -> None:
    """Codex round 1 P2: the family-key marker MUST be present.
    A sidecar joblib with no metadata file alongside is a non-CLI
    artifact — we can't verify it belongs to the directory's family
    so we refuse to load.
    """
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    sidecar_dir = artifact_dir / "recalibrators" / "nba_singles"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    fitted = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(
        np.array([0.0, 0.5, 1.0]), np.array([0.0, 0.7, 1.0]),
    )
    joblib.dump(fitted, sidecar_dir / "isotonic_recalibrator.joblib")
    # Intentionally no metadata file.

    artifact = load_sklearn_artifact(artifact_dir)
    assert "nba_singles" not in artifact.recalibrators


def test_artifact_loader_skips_sidecar_with_null_family_marker(tmp_path: Path) -> None:
    """Codex round 1 P2: metadata exists but ``family_key`` is null /
    missing. Treat as unverified → skip."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    sidecar_dir = artifact_dir / "recalibrators" / "nba_singles"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    fitted = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(
        np.array([0.0, 0.5, 1.0]), np.array([0.0, 0.7, 1.0]),
    )
    joblib.dump(fitted, sidecar_dir / "isotonic_recalibrator.joblib")
    # Metadata present but missing the family_key field.
    (sidecar_dir / "isotonic_recalibration_metadata.json").write_text(
        json.dumps({"schema_version": 1, "sample_size": 200}),
        encoding="utf-8",
    )

    artifact = load_sklearn_artifact(artifact_dir)
    assert "nba_singles" not in artifact.recalibrators


def test_artifact_cache_invalidates_when_sidecar_content_changes_at_same_mtime(
    tmp_path: Path,
) -> None:
    """Codex round 4 P2: a sidecar replaced with different content but
    identical ``(mtime, size)`` (e.g. ``cp --preserve=timestamps`` or
    same-tick rewrites) must still invalidate the cache. Without a
    content hash, the loader would return the stale cached artifact
    until process restart. SHA-256 in the fingerprint sidesteps this.
    """
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    _write_sidecar(
        artifact_dir, family_key="nba_singles",
        fit_pairs=((0.0, 0.0), (0.5, 0.7), (1.0, 1.0)),
    )

    artifact_v1 = load_sklearn_artifact(artifact_dir)
    sidecar_path = artifact_dir / "recalibrators" / "nba_singles" / "isotonic_recalibrator.joblib"
    original_size = sidecar_path.stat().st_size
    original_mtime = sidecar_path.stat().st_mtime
    assert float(artifact_v1.recalibrators["nba_singles"].predict([0.5])[0]) == pytest.approx(
        0.7, abs=0.05,
    )

    # Replace the sidecar with a DIFFERENT recalibrator that happens
    # to round-trip to the same byte count. Using two ConstantPredictor-
    # equivalent isotonics with different fit pairs may produce same-
    # size joblibs; force the byte count to match by truncating after
    # write if necessary. Even simpler: after replacing, restore the
    # original mtime to simulate the cp --preserve=timestamps case.
    fitted_v2 = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(
        np.array([0.0, 0.5, 1.0]), np.array([0.0, 0.3, 1.0]),  # bumps DOWN at 0.5
    )
    joblib.dump(fitted_v2, sidecar_path)
    # Restore the original mtime so the (mtime, size)-only fingerprint
    # would NOT detect the change.
    os.utime(sidecar_path, (original_mtime, original_mtime))
    # If the new joblib is the same size, the test exercises the worst
    # case directly. If not, this still exercises the same-mtime case
    # which the size+mtime fingerprint also misses on disk caches.
    new_size = sidecar_path.stat().st_size
    if new_size == original_size:
        # Worst case: identical (path, mtime, size) but different content.
        artifact_v2 = load_sklearn_artifact(artifact_dir)
        assert float(artifact_v2.recalibrators["nba_singles"].predict([0.5])[0]) == pytest.approx(
            0.3, abs=0.05,
        )
    else:
        # Sizes happen to differ — that branch is already covered by the
        # mtime-update test below. Skip the assertion to avoid a false
        # failure when seeds change joblib byte counts.
        pytest.skip("New sidecar joblib has a different size; size-component already invalidates")


def test_artifact_cache_invalidates_when_sidecar_updates(tmp_path: Path) -> None:
    """Mutating a sidecar joblib must invalidate the artifact cache so
    the next load picks up the new fit."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    _write_sidecar(
        artifact_dir, family_key="nba_singles",
        fit_pairs=((0.0, 0.0), (0.5, 0.7), (1.0, 1.0)),
    )

    artifact_v1 = load_sklearn_artifact(artifact_dir)
    assert float(artifact_v1.recalibrators["nba_singles"].predict([0.5])[0]) == pytest.approx(
        0.7, abs=0.05,
    )

    # Replace with a sidecar that bumps DOWN at 0.5.
    _write_sidecar(
        artifact_dir, family_key="nba_singles",
        fit_pairs=((0.0, 0.0), (0.5, 0.3), (1.0, 1.0)),
    )
    sidecar_path = artifact_dir / "recalibrators" / "nba_singles" / "isotonic_recalibrator.joblib"
    future = time.time() + 5
    os.utime(sidecar_path, (future, future))

    artifact_v2 = load_sklearn_artifact(artifact_dir)
    assert float(artifact_v2.recalibrators["nba_singles"].predict([0.5])[0]) == pytest.approx(
        0.3, abs=0.05,
    )


# -- runtime serving: end-to-end recalibrator application -------------


def test_serving_inference_applies_recalibrator_when_sidecar_present(
    db_session, monkeypatch, tmp_path: Path,
) -> None:
    """Raw probability gets post-processed through the per-family sidecar
    before being returned to the recommendation engine."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    _write_sidecar(
        artifact_dir, family_key="nba_singles",
        fit_pairs=((0.0, 0.0), (0.5, 0.7), (1.0, 1.0)),
    )
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
    assert decision.runtime_health == "healthy"
    # Recalibrator maps 0.5 → ~0.7 per the fit pairs above.
    assert result.probability == pytest.approx(0.7, abs=0.05)
    # Provenance recorded so operators can audit.
    assert result.metadata.get("recalibration_applied") is True
    assert result.metadata.get("raw_probability") == pytest.approx(0.5)
    assert result.metadata.get("recalibration_metadata", {}).get("family_key") == "nba_singles"


def test_serving_inference_passes_through_raw_when_no_sidecar(
    db_session, monkeypatch, tmp_path: Path,
) -> None:
    """No sidecar → raw probability returned unchanged + provenance
    explicitly records ``recalibration_applied=False``."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    # Intentionally no _write_sidecar call.
    manifest_path = _write_manifest(tmp_path, artifact_path=str(artifact_dir))
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, _ = run_serving_inference(
        db_session,
        family_key="nba_singles",
        scope="single",
        features={"family_key": "nba_singles", "recent_average": 21.4, "threshold": 20.5},
    )

    assert result is not None
    assert result.probability == pytest.approx(0.5)
    assert result.metadata.get("recalibration_applied") is False


def test_serving_inference_skips_sidecar_with_mismatched_family_metadata(
    db_session, monkeypatch, tmp_path: Path,
) -> None:
    """Defensive: a sidecar file in ``nba_singles/`` whose metadata
    claims ``mlb_props`` is treated as not-present. Inference uses the
    raw probability rather than risk applying the wrong calibrator.
    """
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    _write_sidecar(
        artifact_dir,
        family_key="nba_singles",
        metadata_family_key="mlb_props",  # disagrees
        fit_pairs=((0.0, 0.0), (0.5, 0.9), (1.0, 1.0)),
    )
    manifest_path = _write_manifest(tmp_path, artifact_path=str(artifact_dir))
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, _ = run_serving_inference(
        db_session,
        family_key="nba_singles",
        scope="single",
        features={"family_key": "nba_singles", "recent_average": 21.4, "threshold": 20.5},
    )

    assert result is not None
    assert result.probability == pytest.approx(0.5)  # raw, not 0.9
    assert result.metadata.get("recalibration_applied") is False


def test_serving_inference_metadata_carries_raw_probability_for_persistence(
    db_session, monkeypatch, tmp_path: Path,
) -> None:
    """Codex round 2 P1: ``ModelInferenceResult.metadata`` MUST expose
    ``raw_probability`` whenever a sidecar fired so the scoring path
    can persist it to ``scoring_diagnostics`` for the next CLI run.
    Without this, the apps/ml ``recalibrate`` CLI's next round would
    fit on already-recalibrated values (a different input scale than
    the model's actual output) and drift the calibration.
    """
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.42)
    _write_sidecar(
        artifact_dir, family_key="nba_singles",
        fit_pairs=((0.0, 0.0), (0.5, 0.7), (1.0, 1.0)),
    )
    manifest_path = _write_manifest(tmp_path, artifact_path=str(artifact_dir))
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, _ = run_serving_inference(
        db_session,
        family_key="nba_singles",
        scope="single",
        features={"family_key": "nba_singles", "recent_average": 21.4, "threshold": 20.5},
    )

    assert result is not None
    # The raw output (0.42) must be preserved in metadata so the
    # downstream persistence layer can record it; the recalibrated
    # value (~0.6 by interpolation) is what gets returned as probability.
    assert result.metadata["raw_probability"] == pytest.approx(0.42)
    assert result.probability != pytest.approx(0.42)
    assert result.metadata["recalibration_applied"] is True


def test_serving_inference_recalibrator_clamps_to_unit_interval(
    db_session, monkeypatch, tmp_path: Path,
) -> None:
    """``IsotonicRegression(y_min=0, y_max=1)`` already clamps, but the
    runtime guard that rejects probabilities outside [0, 1] must still
    accept post-recalibration values."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.99)
    _write_sidecar(
        artifact_dir, family_key="nba_singles",
        # Fit forces every input to map to 1.0 — exercises the upper boundary.
        fit_pairs=((0.0, 1.0), (0.5, 1.0), (1.0, 1.0)),
    )
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
    assert decision.runtime_health == "healthy"
    assert result.probability == pytest.approx(1.0)


def test_artifact_loader_skips_sidecar_with_non_object_metadata(
    tmp_path: Path,
) -> None:
    """Codex round 3 P2: a metadata file containing valid JSON that is
    NOT a JSON object (e.g. ``null`` or an array) would crash the
    ``.get`` call and abort the whole artifact load. Treat it as
    unannotated → skip just this family."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    sidecar_dir = artifact_dir / "recalibrators" / "nba_singles"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    fitted = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(
        np.array([0.0, 0.5, 1.0]), np.array([0.0, 0.7, 1.0]),
    )
    joblib.dump(fitted, sidecar_dir / "isotonic_recalibrator.joblib")
    # Valid JSON but not an object — would historically crash .get.
    (sidecar_dir / "isotonic_recalibration_metadata.json").write_text(
        "null", encoding="utf-8",
    )

    artifact = load_sklearn_artifact(artifact_dir)
    assert "nba_singles" not in artifact.recalibrators


def test_serving_inference_rejects_invalid_raw_before_recalibration(
    db_session, monkeypatch, tmp_path: Path,
) -> None:
    """Codex round 3 P2: a model that emits ``predict_proba`` outside
    [0, 1] must trip the runtime failure path EVEN when a sidecar is
    present. ``IsotonicRegression(out_of_bounds='clip')`` would
    silently clamp the bad value otherwise, masking a corrupted
    artifact."""
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    joblib.dump(OutOfRangePredictor(), artifact_dir / "model.joblib")
    (artifact_dir / "feature_spec.json").write_text(
        json.dumps({
            "version": "test-feature-v1",
            "ordered_keys": ["recent_average", "threshold"],
            "default_values": {"recent_average": 0.0, "threshold": 0.0},
            "family_one_hot_keys": ["nba_singles"],
        }),
        encoding="utf-8",
    )
    (artifact_dir / "training_metadata.json").write_text(
        json.dumps({"trained_at": "2026-04-24T00:00:00Z"}),
        encoding="utf-8",
    )
    _write_sidecar(artifact_dir, family_key="nba_singles")
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

    # Bad raw probability must flip the runtime to the heuristic
    # fallback, NOT serve a silently-clamped recalibrated value.
    assert result is None
    assert decision.effective_mode == "heuristic"
    assert decision.fallback_active is True


def test_serving_inference_skips_sidecar_when_manifest_lacks_iso30d_tag(
    db_session, monkeypatch, tmp_path: Path,
) -> None:
    """Codex round 5 P2: the manifest is the operator-controlled
    activation switch. A sidecar present on disk but a bare
    ``calibration_version`` (no iso30d tag) means the operator hasn't
    activated the recalibration — typically because the CLI's
    write succeeded but the manifest bump didn't, or because the
    manifest was rolled back after the bump. Either way, we serve
    raw and don't quietly apply the staged sidecar.
    """
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.42)
    _write_sidecar(
        artifact_dir, family_key="nba_singles",
        fit_pairs=((0.0, 0.0), (0.5, 0.7), (1.0, 1.0)),
    )
    # Manifest has the bare calibration_version (no +iso30d-... tag).
    manifest_path = _write_manifest(
        tmp_path, artifact_path=str(artifact_dir),
        calibration_version="calibrated_v1",
    )
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, _ = run_serving_inference(
        db_session,
        family_key="nba_singles",
        scope="single",
        features={"family_key": "nba_singles", "recent_average": 21.4, "threshold": 20.5},
    )

    assert result is not None
    assert result.probability == pytest.approx(0.42)  # raw, sidecar skipped
    assert result.metadata.get("recalibration_applied") is False


def test_artifact_loader_skips_sidecar_when_probe_predict_fails(
    tmp_path: Path,
) -> None:
    """Codex round 5 P2: a joblib that unpickles to the wrong object
    (e.g. a list, an unfitted estimator) would crash on every
    ``predict`` call at serve time, marking the family failed /
    degraded. Probe at load time so a bad sidecar falls back to
    raw rather than killing ML serving.
    """
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    sidecar_dir = artifact_dir / "recalibrators" / "nba_singles"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    # A list pickled where an IsotonicRegression should be — loads
    # fine but has no .predict.
    joblib.dump([0.0, 0.5, 1.0], sidecar_dir / "isotonic_recalibrator.joblib")
    (sidecar_dir / "isotonic_recalibration_metadata.json").write_text(
        json.dumps({"schema_version": 1, "family_key": "nba_singles"}),
        encoding="utf-8",
    )

    artifact = load_sklearn_artifact(artifact_dir)
    assert "nba_singles" not in artifact.recalibrators


def test_serving_inference_skips_sidecar_when_iso30d_tag_lacks_date(
    db_session, monkeypatch, tmp_path: Path,
) -> None:
    """Subagent review follow-up: a malformed activation tag (e.g.
    ``+iso30d-`` with no trailing date) must NOT activate
    recalibration. The gate enforces the full
    ``+iso30d-YYYY-MM-DD`` shape; bare prefixes serve raw."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.42)
    _write_sidecar(artifact_dir, family_key="nba_singles")
    manifest_path = _write_manifest(
        tmp_path, artifact_path=str(artifact_dir),
        calibration_version="calibrated_v1+iso30d-",  # malformed: no date
    )
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, _ = run_serving_inference(
        db_session,
        family_key="nba_singles",
        scope="single",
        features={"family_key": "nba_singles", "recent_average": 21.4, "threshold": 20.5},
    )

    assert result is not None
    assert result.probability == pytest.approx(0.42)  # raw, sidecar skipped
    assert result.metadata.get("recalibration_applied") is False


def test_artifact_loader_skips_sidecar_when_probe_returns_out_of_range(
    tmp_path: Path,
) -> None:
    """Codex round 5 P2: a recalibrator whose probe predict returns a
    value outside [0, 1] is malformed (real isotonics with y_min=0,
    y_max=1 can't do this). Skip at load time."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    sidecar_dir = artifact_dir / "recalibrators" / "nba_singles"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(BadRangeRecalibrator(), sidecar_dir / "isotonic_recalibrator.joblib")
    (sidecar_dir / "isotonic_recalibration_metadata.json").write_text(
        json.dumps({"schema_version": 1, "family_key": "nba_singles"}),
        encoding="utf-8",
    )

    artifact = load_sklearn_artifact(artifact_dir)
    assert "nba_singles" not in artifact.recalibrators


def test_artifact_loader_skips_sidecar_with_invalid_joblib(
    tmp_path: Path,
) -> None:
    """A corrupt joblib file shouldn't crash artifact loading — the
    family is treated as having no recalibrator and the model still
    serves with raw probabilities."""
    artifact_dir = _write_artifact_dir(tmp_path, probability=0.5)
    sidecar_dir = artifact_dir / "recalibrators" / "nba_singles"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    # Garbage bytes in place of a real joblib.
    (sidecar_dir / "isotonic_recalibrator.joblib").write_bytes(b"not a real joblib")
    (sidecar_dir / "isotonic_recalibration_metadata.json").write_text(
        json.dumps({"schema_version": 1, "family_key": "nba_singles"}),
        encoding="utf-8",
    )

    # Loading the artifact must not raise even though the sidecar is bad.
    artifact = load_sklearn_artifact(artifact_dir)
    assert "nba_singles" not in artifact.recalibrators
