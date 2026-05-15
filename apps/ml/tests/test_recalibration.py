"""Tests for Smarter #20 — isotonic recalibration on a rolling 30-day window.

Covers:
- ``expected_calibration_error`` returns ~0 for perfectly calibrated
  predictions and rises with miscalibration.
- ``fit_isotonic_recalibrator`` rejects empty / shape-mismatched
  input and returns a fitted ``IsotonicRegression``.
- ``apply_recalibrator`` improves the Brier on a synthetic
  miscalibrated distribution (load-bearing math check).
- ``filter_to_rolling_window`` keeps only rows within the window,
  coerces naive datetimes to UTC, and rejects window_days < 0.
- ``recalibrate_with_rolling_window`` reports insufficient-samples
  below the threshold, otherwise returns a non-None calibrator and
  positive Brier improvement when the input is miscalibrated.
- Edge cases: all-zero outcomes, all-one outcomes, constant
  probabilities, single-bin ECE.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ml.recalibration import (
    DEFAULT_WINDOW_DAYS,
    MIN_RECALIBRATION_SAMPLES,
    CalibrationMetrics,
    RecalibrationResult,
    apply_recalibrator,
    evaluate_calibration,
    expected_calibration_error,
    filter_to_rolling_window,
    fit_isotonic_recalibrator,
    recalibrate_with_rolling_window,
)


# -- expected_calibration_error ----------------------------------------


def test_ece_returns_zero_for_perfect_calibration() -> None:
    # 100 predictions split evenly across deciles; outcomes match the
    # predicted probability exactly within each bin.
    rng = np.random.default_rng(42)
    probabilities = np.repeat(np.linspace(0.05, 0.95, 10), 100)
    outcomes = rng.binomial(1, probabilities).astype(float)
    ece = expected_calibration_error(probabilities, outcomes)
    # Tiny non-zero because of binomial noise; <0.05 in this setup.
    assert ece < 0.05


def test_ece_rises_on_miscalibrated_predictions() -> None:
    # All predictions claim 0.9 but only 30% of outcomes fire.
    probabilities = np.full(500, 0.9)
    outcomes = np.zeros(500)
    outcomes[:150] = 1.0
    ece = expected_calibration_error(probabilities, outcomes)
    assert ece > 0.55  # |0.9 - 0.3| = 0.6, expected near 0.6.


def test_ece_handles_empty_input() -> None:
    assert expected_calibration_error(np.array([]), np.array([])) == 0.0


def test_ece_raises_on_shape_mismatch() -> None:
    with pytest.raises(ValueError):
        expected_calibration_error(np.array([0.5, 0.7]), np.array([1.0]))


def test_ece_handles_probability_at_one_exactly() -> None:
    # 1.0 should land in the last bin, not overflow.
    probabilities = np.array([1.0, 1.0, 1.0])
    outcomes = np.array([1.0, 1.0, 1.0])
    ece = expected_calibration_error(probabilities, outcomes)
    assert ece == 0.0


# -- evaluate_calibration ----------------------------------------------


def test_evaluate_calibration_returns_zero_metrics_for_empty_input() -> None:
    metrics = evaluate_calibration(np.array([]), np.array([]))
    assert metrics == CalibrationMetrics(
        brier=0.0, expected_calibration_error=0.0, sample_size=0
    )


def test_evaluate_calibration_records_sample_size() -> None:
    probs = np.array([0.4, 0.6, 0.5])
    outcomes = np.array([0.0, 1.0, 1.0])
    metrics = evaluate_calibration(probs, outcomes)
    assert metrics.sample_size == 3
    assert 0.0 < metrics.brier < 1.0


def test_evaluate_calibration_raises_on_shape_mismatch() -> None:
    with pytest.raises(ValueError):
        evaluate_calibration(np.array([0.5]), np.array([0.0, 1.0]))


# -- fit_isotonic_recalibrator -----------------------------------------


def test_fit_isotonic_recalibrator_raises_on_empty_input() -> None:
    with pytest.raises(ValueError):
        fit_isotonic_recalibrator(np.array([]), np.array([]))


def test_fit_isotonic_recalibrator_raises_on_shape_mismatch() -> None:
    with pytest.raises(ValueError):
        fit_isotonic_recalibrator(np.array([0.5, 0.7]), np.array([1.0]))


def test_fit_isotonic_recalibrator_handles_all_zero_outcomes() -> None:
    probs = np.linspace(0.1, 0.9, 50)
    outcomes = np.zeros(50)
    recalibrator = fit_isotonic_recalibrator(probs, outcomes)
    # Recalibrator should predict ~0 across the board.
    predictions = apply_recalibrator(probs, recalibrator)
    assert np.all(predictions < 0.05)


def test_fit_isotonic_recalibrator_handles_all_one_outcomes() -> None:
    probs = np.linspace(0.1, 0.9, 50)
    outcomes = np.ones(50)
    recalibrator = fit_isotonic_recalibrator(probs, outcomes)
    predictions = apply_recalibrator(probs, recalibrator)
    assert np.all(predictions > 0.95)


def test_fit_isotonic_recalibrator_clamps_to_probability_range() -> None:
    # Even degenerate input shouldn't produce values outside [0, 1].
    rng = np.random.default_rng(0)
    probs = rng.uniform(0.0, 1.0, size=200)
    outcomes = rng.binomial(1, probs).astype(float)
    recalibrator = fit_isotonic_recalibrator(probs, outcomes)
    predictions = apply_recalibrator(probs, recalibrator)
    assert np.all(predictions >= 0.0)
    assert np.all(predictions <= 1.0)


# -- apply_recalibrator: load-bearing improvement ----------------------


def test_apply_recalibrator_improves_brier_on_miscalibrated_distribution() -> None:
    # Synthetic: raw probabilities are biased upward (claim 0.7, actual
    # rate 0.5). Recalibration should map 0.7 → ~0.5 and lower Brier.
    rng = np.random.default_rng(7)
    n = 600
    raw_probs = np.full(n, 0.7)
    # True event rate = 0.5, independent of the (constant) raw_prob.
    outcomes = rng.binomial(1, 0.5, size=n).astype(float)

    brier_before = evaluate_calibration(raw_probs, outcomes).brier
    recalibrator = fit_isotonic_recalibrator(raw_probs, outcomes)
    recalibrated = apply_recalibrator(raw_probs, recalibrator)
    brier_after = evaluate_calibration(recalibrated, outcomes).brier

    # Brier of constant-0.7 predictor on 50/50 outcomes is large
    # (~0.29). Brier of recalibrated (close to constant-0.5) on
    # 50/50 outcomes is ~0.25. Improvement should be visible.
    assert brier_after < brier_before
    assert brier_after < 0.27


def test_apply_recalibrator_keeps_monotonic_mapping() -> None:
    rng = np.random.default_rng(11)
    n = 400
    # Generate a noisy but monotonically increasing relationship.
    raw_probs = rng.uniform(0.05, 0.95, size=n)
    outcomes = rng.binomial(1, raw_probs).astype(float)
    recalibrator = fit_isotonic_recalibrator(raw_probs, outcomes)
    # Probe at sorted points — output must be non-decreasing.
    grid = np.linspace(0.05, 0.95, 20)
    recalibrated = apply_recalibrator(grid, recalibrator)
    assert np.all(np.diff(recalibrated) >= -1e-9)


# -- filter_to_rolling_window ------------------------------------------


def test_filter_keeps_only_rows_within_window() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    timestamps = [
        now - timedelta(days=5),
        now - timedelta(days=15),
        now - timedelta(days=35),
        now - timedelta(days=29),
    ]
    probs = np.array([0.5, 0.6, 0.7, 0.8])
    outcomes = np.array([0.0, 1.0, 0.0, 1.0])
    filtered_probs, filtered_outcomes, window_start, window_end = filter_to_rolling_window(
        probs, outcomes, timestamps, window_days=30, now=now
    )
    # Rows at -5d and -15d and -29d are inside the 30d window; -35d is not.
    assert filtered_probs.tolist() == [0.5, 0.6, 0.8]
    assert filtered_outcomes.tolist() == [0.0, 1.0, 1.0]
    assert window_end == now
    assert window_start == now - timedelta(days=30)


def test_filter_coerces_naive_timestamps_to_utc() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    timestamps = [
        datetime(2026, 5, 12, 12, 0),  # naive — should be treated as UTC
    ]
    probs = np.array([0.5])
    outcomes = np.array([1.0])
    filtered_probs, _, _, _ = filter_to_rolling_window(
        probs, outcomes, timestamps, window_days=30, now=now
    )
    assert filtered_probs.tolist() == [0.5]


def test_filter_handles_mixed_timezones() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    et = timezone(timedelta(hours=-5))
    timestamps = [
        datetime(2026, 5, 10, 7, 0, tzinfo=et),  # 12:00 UTC, in window
        datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),  # out of window
    ]
    probs = np.array([0.4, 0.9])
    outcomes = np.array([0.0, 1.0])
    filtered_probs, filtered_outcomes, _, _ = filter_to_rolling_window(
        probs, outcomes, timestamps, window_days=30, now=now
    )
    assert filtered_probs.tolist() == [0.4]
    assert filtered_outcomes.tolist() == [0.0]


def test_filter_rejects_negative_window() -> None:
    with pytest.raises(ValueError):
        filter_to_rolling_window(
            np.array([0.5]), np.array([1.0]), [datetime.now(timezone.utc)],
            window_days=-1,
        )


def test_filter_rejects_misaligned_timestamps() -> None:
    with pytest.raises(ValueError):
        filter_to_rolling_window(
            np.array([0.5, 0.6]),
            np.array([1.0, 0.0]),
            [datetime.now(timezone.utc)],  # only 1 ts for 2 probs
            window_days=30,
        )


def test_filter_returns_empty_arrays_when_no_rows_in_window() -> None:
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    timestamps = [now - timedelta(days=100), now - timedelta(days=200)]
    probs = np.array([0.4, 0.5])
    outcomes = np.array([1.0, 0.0])
    filtered_probs, filtered_outcomes, _, _ = filter_to_rolling_window(
        probs, outcomes, timestamps, window_days=30, now=now
    )
    assert filtered_probs.size == 0
    assert filtered_outcomes.size == 0


def test_filter_uses_now_default_to_current_utc() -> None:
    # Smoke test — passing no ``now`` should still work without raising.
    timestamps = [datetime.now(timezone.utc) - timedelta(days=1)]
    probs = np.array([0.5])
    outcomes = np.array([1.0])
    filtered_probs, _, _, _ = filter_to_rolling_window(
        probs, outcomes, timestamps, window_days=30
    )
    assert filtered_probs.tolist() == [0.5]


# -- recalibrate_with_rolling_window: orchestration --------------------


def _synthetic_rolling_input(
    n: int,
    *,
    bias: float,
    now: datetime,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, list[datetime]]:
    """Build a synthetic dataset where raw probabilities have a
    constant additive bias relative to the true rate."""
    rng = np.random.default_rng(seed)
    true_rate = rng.uniform(0.2, 0.8, size=n)
    raw_probs = np.clip(true_rate + bias, 0.01, 0.99)
    outcomes = rng.binomial(1, true_rate).astype(float)
    # Spread timestamps over the last 20 days (well inside the 30d default).
    timestamps = [now - timedelta(days=20 * i / n) for i in range(n)]
    return raw_probs, outcomes, timestamps


def test_recalibrate_returns_insufficient_below_min_samples() -> None:
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    timestamps = [now - timedelta(days=i) for i in range(20)]
    probs = np.full(20, 0.5)
    outcomes = np.zeros(20)
    result = recalibrate_with_rolling_window(
        probs, outcomes, timestamps,
        window_days=DEFAULT_WINDOW_DAYS,
        min_samples=MIN_RECALIBRATION_SAMPLES,
        now=now,
    )
    assert isinstance(result, RecalibrationResult)
    assert result.calibrator is None
    assert result.insufficient_samples is True
    assert result.sample_size == 20
    # Before and after are identical when we skip recalibration.
    assert result.metrics_before == result.metrics_after


def test_recalibrate_fits_when_min_samples_met() -> None:
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    raw_probs, outcomes, timestamps = _synthetic_rolling_input(
        200, bias=0.0, now=now, seed=3
    )
    result = recalibrate_with_rolling_window(
        raw_probs, outcomes, timestamps,
        window_days=30,
        min_samples=MIN_RECALIBRATION_SAMPLES,
        now=now,
    )
    assert result.calibrator is not None
    assert result.insufficient_samples is False
    assert result.sample_size == 200


def test_recalibrate_improves_brier_on_biased_input() -> None:
    # Biased raw probabilities (0.2 above true rate) should yield a
    # positive Brier improvement after recalibration.
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    raw_probs, outcomes, timestamps = _synthetic_rolling_input(
        400, bias=0.2, now=now, seed=5
    )
    result = recalibrate_with_rolling_window(
        raw_probs, outcomes, timestamps,
        window_days=30,
        min_samples=MIN_RECALIBRATION_SAMPLES,
        now=now,
    )
    assert result.calibrator is not None
    assert result.brier_improvement > 0.0
    assert result.ece_improvement > 0.0


def test_recalibrate_window_start_end_are_set_correctly() -> None:
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    raw_probs, outcomes, timestamps = _synthetic_rolling_input(
        150, bias=0.0, now=now, seed=1
    )
    result = recalibrate_with_rolling_window(
        raw_probs, outcomes, timestamps, window_days=30, now=now
    )
    assert result.window_end == now
    assert result.window_start == now - timedelta(days=30)


def test_recalibrate_drops_rows_outside_window() -> None:
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    # Half the rows are 100 days old → outside the 30d window.
    n = 300
    rng = np.random.default_rng(42)
    raw_probs = rng.uniform(0.1, 0.9, size=n)
    outcomes = rng.binomial(1, raw_probs).astype(float)
    timestamps = [
        now - timedelta(days=5) if i % 2 == 0 else now - timedelta(days=100)
        for i in range(n)
    ]
    result = recalibrate_with_rolling_window(
        raw_probs, outcomes, timestamps, window_days=30, now=now,
        min_samples=100,
    )
    assert result.sample_size == n // 2


def test_recalibration_result_improvement_properties() -> None:
    # Construct a result with known before / after metrics.
    before = CalibrationMetrics(brier=0.25, expected_calibration_error=0.15, sample_size=200)
    after = CalibrationMetrics(brier=0.18, expected_calibration_error=0.05, sample_size=200)
    result = RecalibrationResult(
        calibrator=None,
        metrics_before=before,
        metrics_after=after,
        window_start=datetime(2026, 4, 14, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 14, tzinfo=timezone.utc),
        sample_size=200,
        insufficient_samples=False,
    )
    assert result.brier_improvement == 0.07
    assert result.ece_improvement == 0.10
