"""Smarter #20 — isotonic recalibration on a rolling 30-day window.

Markets drift seasonally — NBA prop markets sharpen as the season
progresses; MLB lines tighten through the post-AS-break stretch.
The classifier was fit on a static training window, so its baked-in
isotonic calibrator becomes progressively less accurate as deployment
ages. The fix is to periodically refit *only* the calibrator on the
most-recent window of settled outcomes, leaving the underlying model
weights untouched.

This module ships the math:

- ``filter_to_rolling_window`` — keep only (prob, outcome, ts) rows
  within the last ``window_days``.
- ``fit_isotonic_recalibrator`` — fit a fresh ``IsotonicRegression``
  on the rolling window.
- ``apply_recalibrator`` — post-process raw probabilities through the
  recalibrator.
- ``expected_calibration_error`` / ``evaluate_calibration`` — Brier +
  ECE diagnostics so the caller knows whether swapping in the new
  calibrator actually improves things (occasionally a 30-day window
  is noisier than the training-time calibrator; the caller skips the
  swap when ``brier_improvement`` is negative).
- ``recalibrate_with_rolling_window`` — single-call orchestration.

## Why isotonic, not Platt

Sika's classifier already produces calibrated-ish probabilities via
``CalibratedClassifierCV(method="isotonic")`` at train time. Re-using
isotonic keeps the recalibration scheme consistent — a fresh fit on
the rolling window is exactly the same math, just on more recent
data. Platt (sigmoid) would over-constrain to a 2-parameter family;
isotonic stays flexible.

## Phase 2 (follow-up PRs)

- CLI command ``recalibrate-isotonic --family-key X`` that loads an
  existing artifact, queries last-30-days predictions from the DB,
  fits a new isotonic recalibrator, writes a sidecar joblib to the
  artifact directory, and bumps the manifest's ``calibration_version``.
- Serve-time loader in ``apps/api/app/services/ml/runtime.py`` that
  reads the sidecar isotonic and post-processes the model's raw
  probabilities before they reach the recommendation engine.
- Per-family wiring — sika has multiple model families; each gets
  its own rolling-window recalibrator, since drift rates differ
  across sports / prop types.

Phase 1 (this module) is the smallest piece that doesn't change
existing behavior — operators with the helpers can run an offline
calibration audit on settled predictions and validate the sharpening
before turning on automated recalibration.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import joblib
import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss


logger = logging.getLogger(__name__)


# Canonical filenames inside an artifact directory. Both the CLI
# command (Phase 2b) and the serve-time loader (Phase 2c) read /
# write through these constants so the on-disk contract is one
# place. Kept module-level so callers can compose paths without
# importing the helpers.
SIDECAR_RECALIBRATOR_FILENAME: str = "isotonic_recalibrator.joblib"
SIDECAR_METADATA_FILENAME: str = "isotonic_recalibration_metadata.json"


# Default rolling-window width. 30 days is the punch-list spec — long
# enough to accumulate ~100+ settled rows per family in a typical NBA
# week (and ~30+ rows for MLB), short enough that seasonal drift
# doesn't dominate the calibrator.
DEFAULT_WINDOW_DAYS: int = 30


# Minimum window sample size before recalibration is statistically
# meaningful. Below this, the rolling fit is more noise than signal;
# ``recalibrate_with_rolling_window`` returns ``calibrator=None`` and
# the caller skips the swap.
MIN_RECALIBRATION_SAMPLES: int = 100


@dataclass(frozen=True, slots=True)
class CalibrationMetrics:
    """Diagnostic metrics for a calibrator's fit quality.

    ``brier`` is the mean squared error between predicted probability
    and observed binary outcome — lower is better, 0 means perfect.

    ``expected_calibration_error`` (ECE) is the bucketed gap between
    predicted probability and empirical frequency — 0 means perfectly
    calibrated, 1 means maximally miscalibrated. Complements Brier:
    Brier measures sharpness + calibration jointly; ECE isolates the
    calibration component.
    """

    brier: float
    expected_calibration_error: float
    sample_size: int


@dataclass(frozen=True, slots=True)
class RecalibrationResult:
    """Result of a rolling-window recalibration call.

    Callers should swap in ``calibrator`` only when it is non-None
    AND ``brier_improvement`` is positive (recalibration sharpened
    the calibration on the rolling window). Negative improvement
    means the rolling-window noise dominated — keep the original
    calibrator.
    """

    calibrator: IsotonicRegression | None
    metrics_before: CalibrationMetrics
    metrics_after: CalibrationMetrics
    window_start: datetime
    window_end: datetime
    sample_size: int
    insufficient_samples: bool

    @property
    def brier_improvement(self) -> float:
        """Positive when recalibration reduces Brier (better).

        ``metrics_before`` is the Brier of the raw probabilities as
        observed in the rolling window. ``metrics_after`` is the
        Brier of those same probabilities after passing through the
        newly fitted recalibrator. A positive delta means the new
        calibrator brings predictions closer to observed frequencies.
        """
        return round(self.metrics_before.brier - self.metrics_after.brier, 6)

    @property
    def ece_improvement(self) -> float:
        """Positive when recalibration reduces ECE (better)."""
        return round(
            self.metrics_before.expected_calibration_error
            - self.metrics_after.expected_calibration_error,
            6,
        )


def expected_calibration_error(
    probabilities: np.ndarray,
    outcomes: np.ndarray,
    *,
    n_bins: int = 10,
) -> float:
    """Compute the expected calibration error (ECE).

    Bin predictions into ``n_bins`` equal-width buckets over [0, 1].
    For each non-empty bin, take the absolute gap between the bin's
    mean predicted probability and the bin's empirical event rate.
    ECE is the bin-size-weighted average of those gaps — 0 means
    perfectly calibrated, ~1 is the theoretical maximum.

    A perfectly calibrated model has ECE ≈ 0; a model that always
    predicts 50% but events fire 20% of the time has ECE ≈ 0.3.
    """
    probabilities = np.asarray(probabilities, dtype=float)
    outcomes = np.asarray(outcomes, dtype=float)
    if probabilities.shape != outcomes.shape:
        raise ValueError(
            f"probabilities {probabilities.shape} and outcomes "
            f"{outcomes.shape} must have the same shape"
        )
    if probabilities.size == 0:
        return 0.0
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    # ``np.digitize`` returns 1-indexed bin numbers; subtract 1 then
    # clip the tail so probabilities of exactly 1.0 land in the last
    # bin rather than overflowing to ``n_bins``.
    bin_indices = np.digitize(probabilities, bins) - 1
    bin_indices = np.clip(bin_indices, 0, n_bins - 1)
    ece = 0.0
    total = float(probabilities.size)
    for bin_idx in range(n_bins):
        mask = bin_indices == bin_idx
        if not mask.any():
            continue
        bin_probs = probabilities[mask]
        bin_outcomes = outcomes[mask]
        bin_size = float(mask.sum())
        mean_prob = float(bin_probs.mean())
        empirical = float(bin_outcomes.mean())
        ece += (bin_size / total) * abs(mean_prob - empirical)
    return round(float(ece), 6)


def evaluate_calibration(
    probabilities: np.ndarray,
    outcomes: np.ndarray,
    *,
    n_bins: int = 10,
) -> CalibrationMetrics:
    """Compute Brier + ECE on the supplied (probability, outcome) pairs."""
    probabilities = np.asarray(probabilities, dtype=float)
    outcomes = np.asarray(outcomes, dtype=float)
    if probabilities.shape != outcomes.shape:
        raise ValueError(
            f"probabilities {probabilities.shape} and outcomes "
            f"{outcomes.shape} must have the same shape"
        )
    if probabilities.size == 0:
        return CalibrationMetrics(
            brier=0.0, expected_calibration_error=0.0, sample_size=0
        )
    brier = float(brier_score_loss(outcomes, probabilities))
    ece = expected_calibration_error(probabilities, outcomes, n_bins=n_bins)
    return CalibrationMetrics(
        brier=round(brier, 6),
        expected_calibration_error=ece,
        sample_size=int(probabilities.size),
    )


def fit_isotonic_recalibrator(
    raw_probabilities: np.ndarray,
    outcomes: np.ndarray,
) -> IsotonicRegression:
    """Fit a fresh isotonic recalibrator on (probability, outcome) pairs.

    The recalibrator learns a monotone mapping from raw probability
    to recalibrated probability so that, on the supplied window,
    predictions closer to the empirical event rate are returned.

    ``out_of_bounds='clip'`` so predict on a probability outside the
    fit range (rare; happens when raw probabilities lie at the open
    interval edges) clamps to the nearest fit boundary instead of
    raising.

    ``y_min=0, y_max=1`` clamps the output to a valid probability
    range — without this, isotonic can in principle predict values
    slightly outside [0, 1] on flat-tail bins.
    """
    raw_probabilities = np.asarray(raw_probabilities, dtype=float)
    outcomes = np.asarray(outcomes, dtype=float)
    if raw_probabilities.shape != outcomes.shape:
        raise ValueError(
            f"raw_probabilities {raw_probabilities.shape} and outcomes "
            f"{outcomes.shape} must have the same shape"
        )
    if raw_probabilities.size == 0:
        raise ValueError("cannot fit recalibrator on empty input")
    recalibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    recalibrator.fit(raw_probabilities, outcomes)
    return recalibrator


def apply_recalibrator(
    raw_probabilities: np.ndarray,
    recalibrator: IsotonicRegression,
) -> np.ndarray:
    """Map raw probabilities through a fitted recalibrator.

    Returns a numpy array of the same shape with the recalibrated
    probabilities. ``recalibrator``'s ``out_of_bounds='clip'`` handles
    values at or beyond the fit range.
    """
    raw_probabilities = np.asarray(raw_probabilities, dtype=float)
    return np.asarray(recalibrator.predict(raw_probabilities), dtype=float)


def _coerce_utc(ts: datetime) -> datetime:
    """Make a datetime UTC-aware for safe comparison.

    Treats naive datetimes as UTC (sika captures timestamps in UTC
    everywhere — naive values are a serialization artifact, not a
    different timezone).
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def filter_to_rolling_window(
    raw_probabilities: np.ndarray,
    outcomes: np.ndarray,
    timestamps: Sequence[datetime],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> tuple[np.ndarray, np.ndarray, datetime, datetime]:
    """Filter input arrays to the last ``window_days`` of data.

    ``timestamps`` is expected to be in the same order as the
    probability / outcome arrays. Mixed-timezone timestamps are
    coerced to UTC; naive datetimes are treated as UTC.

    Returns ``(filtered_probs, filtered_outcomes, window_start,
    window_end)``. ``window_end`` is the ``now`` used as the upper
    edge — useful for downstream provenance.
    """
    raw_probabilities = np.asarray(raw_probabilities, dtype=float)
    outcomes = np.asarray(outcomes, dtype=float)
    timestamps_list = list(timestamps)
    if len(timestamps_list) != raw_probabilities.size:
        raise ValueError(
            f"timestamps ({len(timestamps_list)}) and probabilities "
            f"({raw_probabilities.size}) must align"
        )
    if raw_probabilities.shape != outcomes.shape:
        raise ValueError(
            f"raw_probabilities {raw_probabilities.shape} and outcomes "
            f"{outcomes.shape} must have the same shape"
        )
    if window_days < 0:
        raise ValueError(f"window_days must be non-negative; got {window_days}")
    window_end = _coerce_utc(now) if now is not None else datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=window_days)
    mask = np.array(
        [_coerce_utc(ts) >= window_start for ts in timestamps_list],
        dtype=bool,
    )
    return (
        raw_probabilities[mask],
        outcomes[mask],
        window_start,
        window_end,
    )


def recalibrate_with_rolling_window(
    raw_probabilities: np.ndarray,
    outcomes: np.ndarray,
    timestamps: Sequence[datetime],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_samples: int = MIN_RECALIBRATION_SAMPLES,
    now: datetime | None = None,
) -> RecalibrationResult:
    """Refit isotonic recalibrator on the last ``window_days`` of
    (raw_probability, outcome) pairs.

    Returns a ``RecalibrationResult`` containing the new calibrator
    (or ``None`` when the rolling window is too small) plus before /
    after metrics so the caller can decide whether to swap it in.

    ``insufficient_samples=True`` (with ``calibrator=None``) means
    fewer than ``min_samples`` rows fell in the window — the caller
    should keep the existing calibrator. A non-None calibrator with
    ``brier_improvement < 0`` is a hint that the rolling window was
    noisier than the training-time calibrator; callers can still
    apply it but should record the negative delta in the manifest.
    """
    filtered_probs, filtered_outcomes, window_start, window_end = filter_to_rolling_window(
        raw_probabilities,
        outcomes,
        timestamps,
        window_days=window_days,
        now=now,
    )
    sample_size = int(filtered_probs.size)
    metrics_before = evaluate_calibration(filtered_probs, filtered_outcomes)
    if sample_size < min_samples:
        return RecalibrationResult(
            calibrator=None,
            metrics_before=metrics_before,
            metrics_after=metrics_before,
            window_start=window_start,
            window_end=window_end,
            sample_size=sample_size,
            insufficient_samples=True,
        )
    recalibrator = fit_isotonic_recalibrator(filtered_probs, filtered_outcomes)
    recalibrated = apply_recalibrator(filtered_probs, recalibrator)
    metrics_after = evaluate_calibration(recalibrated, filtered_outcomes)
    return RecalibrationResult(
        calibrator=recalibrator,
        metrics_before=metrics_before,
        metrics_after=metrics_after,
        window_start=window_start,
        window_end=window_end,
        sample_size=sample_size,
        insufficient_samples=False,
    )


# -----------------------------------------------------------------------------
# Smarter #20 phase 2a — sidecar file I/O
#
# Phase 1 (PR #96) shipped the math. This section defines the
# on-disk contract so the future CLI command (phase 2b — fits a
# recalibrator from settled DB predictions and persists alongside an
# existing artifact) and the serve-time loader (phase 2c — reads
# the sidecar at inference and post-processes raw probabilities)
# can both depend on a single canonical format.
#
# Sidecar files live INSIDE the per-family artifact directory next
# to ``model.joblib`` / ``feature_spec.json`` / ``training_metadata.json``,
# so the artifact directory remains the unit of deployment:
#
# ::
#
#     artifacts/<model-name>-<version>/
#       ├── model.joblib                       (existing — Phase 1 of training)
#       ├── feature_spec.json                  (existing)
#       ├── training_metadata.json             (existing)
#       ├── isotonic_recalibrator.joblib       (NEW — joblib-pickled IsotonicRegression)
#       └── isotonic_recalibration_metadata.json (NEW — JSON sidecar manifest)
#
# The JSON sidecar carries the fit-time provenance (window dates,
# sample size, before / after metrics) so an operator can read it
# at the filesystem level without un-pickling. The joblib payload
# is the IsotonicRegression instance itself.


def write_sidecar_recalibrator(
    artifact_dir: Path | str,
    result: RecalibrationResult,
) -> tuple[Path, Path]:
    """Persist a successful recalibration result into ``artifact_dir``.

    Writes two files atomically (per-file, not as a transaction):
    - ``isotonic_recalibrator.joblib`` — the sklearn estimator.
    - ``isotonic_recalibration_metadata.json`` — provenance.

    Raises ``ValueError`` when ``result.calibrator`` is ``None``
    (e.g. ``insufficient_samples=True``) — there's nothing to write.
    The caller should gate on ``result.calibrator is not None`` and
    ideally on ``result.brier_improvement > 0`` so a noisier
    recalibrator doesn't replace a quieter one.

    Returns the two written paths so callers can reference them in
    log lines or manifest entries.
    """
    if result.calibrator is None:
        raise ValueError(
            "Cannot write sidecar for a RecalibrationResult with calibrator=None "
            "(insufficient_samples or empty window)"
        )
    target = Path(artifact_dir)
    target.mkdir(parents=True, exist_ok=True)
    joblib_path = target / SIDECAR_RECALIBRATOR_FILENAME
    metadata_path = target / SIDECAR_METADATA_FILENAME

    joblib.dump(result.calibrator, joblib_path)
    metadata = {
        "schema_version": 1,
        "window_start": result.window_start.isoformat(),
        "window_end": result.window_end.isoformat(),
        "sample_size": int(result.sample_size),
        "metrics_before": {
            "brier": result.metrics_before.brier,
            "expected_calibration_error": result.metrics_before.expected_calibration_error,
            "sample_size": result.metrics_before.sample_size,
        },
        "metrics_after": {
            "brier": result.metrics_after.brier,
            "expected_calibration_error": result.metrics_after.expected_calibration_error,
            "sample_size": result.metrics_after.sample_size,
        },
        "brier_improvement": result.brier_improvement,
        "ece_improvement": result.ece_improvement,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return joblib_path, metadata_path


def load_sidecar_recalibrator(
    artifact_dir: Path | str,
) -> IsotonicRegression | None:
    """Load a previously-written sidecar recalibrator from
    ``artifact_dir``, or ``None`` when the sidecar isn't present.

    Returning ``None`` for the missing-file case is deliberate — the
    serve-time path can call this unconditionally and skip the
    post-process when the sidecar doesn't exist. No exception, no
    log spam.

    Raises ``FileNotFoundError`` only when ``artifact_dir`` itself
    doesn't exist — that's a deployment configuration error worth
    surfacing rather than silently swallowing.
    """
    target = Path(artifact_dir)
    if not target.exists():
        raise FileNotFoundError(f"artifact directory not found: {target}")
    joblib_path = target / SIDECAR_RECALIBRATOR_FILENAME
    if not joblib_path.exists():
        return None
    return joblib.load(joblib_path)


def load_sidecar_metadata(artifact_dir: Path | str) -> dict | None:
    """Load the sidecar's JSON metadata for operator inspection.

    Returns ``None`` when the metadata file doesn't exist (no sidecar
    was ever written). Returns the parsed dict otherwise. Operators
    use this to see when the last recalibration ran and what the
    fit-time improvement looked like.
    """
    target = Path(artifact_dir)
    if not target.exists():
        raise FileNotFoundError(f"artifact directory not found: {target}")
    metadata_path = target / SIDECAR_METADATA_FILENAME
    if not metadata_path.exists():
        return None
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def sidecar_is_present(artifact_dir: Path | str) -> bool:
    """Cheap existence-check for the sidecar joblib. Doesn't open
    the file or load the estimator — for use in hot paths where the
    serve-time loader wants to short-circuit before any pickle I/O.
    """
    target = Path(artifact_dir)
    return (target / SIDECAR_RECALIBRATOR_FILENAME).exists()
