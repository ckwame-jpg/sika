"""Smarter #21 (phase 2a) — sidecar artifact I/O for prop prediction
intervals.

Phase 1 (PR #84-ish — pre-history of this repo's punch list)
shipped the math in ``quantile_regression.py`` —
``fit_prediction_interval_models`` returns three fitted regressors
(p10 / p50 / p90), ``compute_prediction_interval`` calls them and
returns a monotonized ``(p10, p50, p90)`` triple.

Phase 2a defines the on-disk contract that:

- The future training-pipeline integration (phase 2b) will write
  alongside the existing classifier artifact.
- The future serve-time inference path (phase 2c) will load to
  surface intervals in scoring diagnostics.

## On-disk layout

Per artifact directory + stat key:

    <artifact_dir>/interval_models/<stat_key>/
      p10.joblib            # GradientBoostingRegressor(loss="quantile", alpha=0.10)
      p50.joblib            # GradientBoostingRegressor(loss="quantile", alpha=0.50)
      p90.joblib            # GradientBoostingRegressor(loss="quantile", alpha=0.90)
      metadata.json         # provenance: training window, sample size, empirical coverage

The per-stat subdirectory mirrors Smarter #20's recalibrator sidecar
pattern (``recalibrators/<family_key>/``) so the loader code can use
the same "does this exist? probe the model at canonical inputs"
check.

## Why three separate files instead of one bundle

Each quantile regressor is fit independently — they can drift from
one another (a known failure mode that ``compute_prediction_interval``
guards against by re-sorting the predictions). Keeping them as
separate joblib files lets ops swap in a single fitted model
without re-running the full training pipeline, useful when one
quantile's training data was corrupted but the others are fine.

## What's deferred

- Phase 2b: dataset extraction — the actual continuous stat output
  per settled prediction (today the training pipeline only has the
  binary YES/NO label; quantile regression needs the underlying
  count).
- Phase 2c: serve-time loader + diagnostic surface in
  ``ml/runtime.py``.
- Phase 2d: UI band on the trade ticket.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import joblib
import numpy as np

from ml.quantile_regression import (
    DEFAULT_QUANTILES,
    PredictionInterval,
    compute_prediction_interval,
    fit_prediction_interval_models,
)

logger = logging.getLogger(__name__)

__all__ = [
    "IntervalArtifactPaths",
    "IntervalTrainingResult",
    "INTERVAL_MODELS_SUBDIR",
    "interval_models_dir",
    "interval_models_paths",
    "interval_models_present",
    "train_prop_interval_models",
    "load_interval_models",
    "load_interval_metadata",
]

INTERVAL_MODELS_SUBDIR = "interval_models"
_METADATA_FILENAME = "metadata.json"
_PROBE_FEATURES_ROWS = 3


@dataclass(frozen=True, slots=True)
class IntervalArtifactPaths:
    """Resolved on-disk locations for one stat key's interval
    models. Filenames mirror ``DEFAULT_QUANTILES`` so a future
    extension to additional quantiles only requires adding the
    helper, not changing the contract."""

    directory: Path
    p10: Path
    p50: Path
    p90: Path
    metadata: Path


@dataclass(frozen=True, slots=True)
class IntervalTrainingResult:
    """Output of ``train_prop_interval_models``. ``paths`` records
    where each artifact landed so the caller can stage them
    alongside the classifier in the manifest. ``empirical_coverage``
    is the in-sample coverage rate of the [p10, p90] band — a
    well-calibrated model has ~80% coverage; significantly lower
    means the band is too tight (overconfident intervals)."""

    paths: IntervalArtifactPaths
    sample_size: int
    empirical_coverage: float


# -- Path helpers ------------------------------------------------------


def interval_models_dir(artifact_dir: Path | str, stat_key: str) -> Path:
    """Resolve the per-stat-key directory inside ``artifact_dir``.
    Does not create it — callers do that explicitly before writing."""
    if not stat_key.strip():
        raise ValueError("stat_key must be non-empty")
    return Path(artifact_dir) / INTERVAL_MODELS_SUBDIR / stat_key.strip()


def interval_models_paths(
    artifact_dir: Path | str, stat_key: str
) -> IntervalArtifactPaths:
    directory = interval_models_dir(artifact_dir, stat_key)
    return IntervalArtifactPaths(
        directory=directory,
        p10=directory / "p10.joblib",
        p50=directory / "p50.joblib",
        p90=directory / "p90.joblib",
        metadata=directory / _METADATA_FILENAME,
    )


def interval_models_present(artifact_dir: Path | str, stat_key: str) -> bool:
    """Cheap existence probe — does NOT un-pickle anything. Used by
    hot paths that want to skip the load when the artifact is
    missing (e.g. a stat key that doesn't have intervals yet)."""
    paths = interval_models_paths(artifact_dir, stat_key)
    return all(p.exists() for p in (paths.p10, paths.p50, paths.p90))


# -- Training entry point ----------------------------------------------


def train_prop_interval_models(
    features: np.ndarray,
    targets: np.ndarray,
    *,
    family_key: str,
    stat_key: str,
    artifact_dir: Path | str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
) -> IntervalTrainingResult:
    """Fit + persist the three quantile regressors for one stat key.

    Inputs:
    - ``features``: ``(n, k)`` array — same feature spec as the
      classifier consumes.
    - ``targets``: ``(n,)`` array of CONTINUOUS stat outputs (e.g.
      points scored). Phase 2b will produce these from settled
      predictions joined to gamelogs; phase 2a accepts them as
      input directly.
    - ``artifact_dir``: same root the classifier artifact lives in.
      The per-stat-key subdirectory is created automatically.

    Side effects:
    - Writes ``p10.joblib``, ``p50.joblib``, ``p90.joblib`` (sklearn
      ``GradientBoostingRegressor`` instances).
    - Writes ``metadata.json`` with provenance (sample size, training
      window, empirical coverage, family/stat key, quantiles trained,
      timestamp).

    Returns ``IntervalTrainingResult`` so the caller can log /
    surface the empirical coverage in operator UI.
    """
    if features.ndim != 2:
        raise ValueError(
            f"features must be 2-D (n, k), got shape {features.shape}"
        )
    if targets.ndim != 1:
        raise ValueError(
            f"targets must be 1-D (n,), got shape {targets.shape}"
        )
    if features.shape[0] != targets.shape[0]:
        raise ValueError(
            f"row mismatch: features={features.shape[0]} targets={targets.shape[0]}"
        )
    if features.shape[0] == 0:
        raise ValueError("training requires at least one row")
    if not family_key.strip():
        raise ValueError("family_key must be non-empty")

    paths = interval_models_paths(artifact_dir, stat_key)
    paths.directory.mkdir(parents=True, exist_ok=True)

    p10_model, p50_model, p90_model = fit_prediction_interval_models(
        features, targets, quantiles=quantiles,
    )

    # Persist each regressor at its canonical filename. Order
    # matters: the loader reads p10.joblib → p50.joblib → p90.joblib
    # in that order, so a future quantile remapping is a contract
    # break the loader can detect.
    joblib.dump(p10_model, paths.p10)
    joblib.dump(p50_model, paths.p50)
    joblib.dump(p90_model, paths.p90)

    # Compute empirical coverage on the training data — a poor proxy
    # for held-out coverage but a useful smoke test (a model whose
    # in-sample coverage is much less than 80% has a clear fit
    # problem; out-of-sample can only be worse).
    intervals = [
        compute_prediction_interval(
            p10_model, p50_model, p90_model, features[i : i + 1],
        )
        for i in range(features.shape[0])
    ]
    covered = sum(
        1 for interval, target in zip(intervals, targets)
        if interval.p10 <= float(target) <= interval.p90
    )
    coverage = covered / float(features.shape[0])

    metadata: dict[str, Any] = {
        "family_key": family_key,
        "stat_key": stat_key,
        "quantiles": list(quantiles),
        "sample_size": int(features.shape[0]),
        "empirical_coverage": coverage,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    paths.metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True))

    return IntervalTrainingResult(
        paths=paths,
        sample_size=int(features.shape[0]),
        empirical_coverage=coverage,
    )


# -- Load helpers ------------------------------------------------------


def load_interval_models(
    artifact_dir: Path | str, stat_key: str
) -> tuple[Any, Any, Any] | None:
    """Load the three regressors for ``stat_key`` as a
    ``(p10_model, p50_model, p90_model)`` tuple — matches the shape
    ``fit_prediction_interval_models`` returns and
    ``compute_prediction_interval`` consumes. Returns ``None`` when
    any file is missing or unloadable — the caller (typically the
    inference path) should fall back to the point estimate.

    Probes each loaded model at a canonical 3-row zero-feature
    matrix to catch corruption early (a joblib file that loads but
    crashes on ``predict`` won't be caught by ``exists()``). Returns
    ``None`` on probe failure with a warning log so the operator can
    investigate without crashing inference.
    """
    paths = interval_models_paths(artifact_dir, stat_key)
    if not all(p.exists() for p in (paths.p10, paths.p50, paths.p90)):
        return None
    try:
        p10_model = joblib.load(paths.p10)
        p50_model = joblib.load(paths.p50)
        p90_model = joblib.load(paths.p90)
    except Exception as exc:  # noqa: BLE001 — corruption is a runtime concern
        logger.warning(
            "interval_models load failed for stat_key=%s: %s", stat_key, exc,
        )
        return None
    loaded = (p10_model, p50_model, p90_model)
    # Probe each model at a canonical zero matrix to surface
    # serialization corruption (file loaded but predict() blows
    # up). The probe shape must match what training saw — for a
    # 2-D feature input the probe is a zero matrix with the same
    # column count. We don't know k from the joblib alone, so we
    # use the model's ``n_features_in_`` attribute.
    try:
        probe_cols = int(p10_model.n_features_in_)
    except AttributeError:
        # Older sklearn versions don't expose n_features_in_;
        # silently skip the probe.
        return loaded
    probe = np.zeros((_PROBE_FEATURES_ROWS, probe_cols), dtype=float)
    try:
        for model in loaded:
            model.predict(probe)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "interval_models probe failed for stat_key=%s: %s", stat_key, exc,
        )
        return None
    return loaded


def load_interval_metadata(
    artifact_dir: Path | str, stat_key: str
) -> dict[str, Any] | None:
    """Load the metadata JSON without un-pickling the models. Useful
    for operator inspection ("when was this fit?" / "what's the
    in-sample coverage?") and for the readiness panel to flag a
    stale interval artifact.
    """
    paths = interval_models_paths(artifact_dir, stat_key)
    if not paths.metadata.exists():
        return None
    try:
        return json.loads(paths.metadata.read_text())
    except json.JSONDecodeError as exc:
        logger.warning(
            "interval_models metadata corrupt for stat_key=%s: %s", stat_key, exc,
        )
        return None
