"""Tests for Smarter #21 — quantile-regression prediction intervals.

Covers:
- ``fit_quantile_regressor`` accepts only valid quantiles.
- ``fit_prediction_interval_models`` returns models in sorted order.
- ``compute_prediction_interval`` returns a monotonized
  ``(p10, p50, p90)`` triple even when raw quantile predictions
  cross (a known failure mode of independently-fitted quantile
  regressors on noisy data).
- ``empirical_coverage`` correctly counts covered actuals.
- An 80% calibrated interval on a held-out synthetic distribution
  achieves close-to-expected coverage.
"""

from __future__ import annotations

import numpy as np
import pytest

from ml.quantile_regression import (
    DEFAULT_QUANTILES,
    PredictionInterval,
    compute_prediction_interval,
    compute_prediction_intervals_batch,
    empirical_coverage,
    fit_prediction_interval_models,
    fit_quantile_regressor,
)


# -- fit_quantile_regressor --------------------------------------------


def test_fit_quantile_regressor_rejects_quantile_out_of_range() -> None:
    x = np.array([[1.0], [2.0], [3.0]])
    y = np.array([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        fit_quantile_regressor(x, y, quantile=0.0)
    with pytest.raises(ValueError):
        fit_quantile_regressor(x, y, quantile=1.0)
    with pytest.raises(ValueError):
        fit_quantile_regressor(x, y, quantile=-0.1)
    with pytest.raises(ValueError):
        fit_quantile_regressor(x, y, quantile=1.5)


def test_fit_quantile_regressor_returns_fitted_model() -> None:
    rng = np.random.default_rng(42)
    x = rng.normal(size=(200, 2))
    y = x[:, 0] + 0.5 * rng.normal(size=200)
    model = fit_quantile_regressor(x, y, quantile=0.5)
    predictions = model.predict(x)
    assert predictions.shape == (200,)
    # Median regressor should track the actuals roughly.
    assert np.corrcoef(predictions, y)[0, 1] > 0.5


def test_fit_quantile_regressor_p10_predicts_lower_than_p90() -> None:
    rng = np.random.default_rng(42)
    x = rng.normal(size=(300, 1))
    y = x[:, 0] + rng.normal(scale=2.0, size=300)
    p10 = fit_quantile_regressor(x, y, quantile=0.1)
    p90 = fit_quantile_regressor(x, y, quantile=0.9)
    # Averaged over the dataset, the p10 model produces lower
    # predictions than the p90 model — even though individual rows
    # may cross.
    assert p10.predict(x).mean() < p90.predict(x).mean()


# -- fit_prediction_interval_models ------------------------------------


def test_fit_interval_models_requires_three_quantiles() -> None:
    x = np.array([[1.0], [2.0], [3.0]])
    y = np.array([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        fit_prediction_interval_models(x, y, quantiles=(0.1, 0.9))  # type: ignore[arg-type]


def test_fit_interval_models_requires_sorted_quantiles() -> None:
    x = np.array([[1.0], [2.0], [3.0]])
    y = np.array([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        fit_prediction_interval_models(x, y, quantiles=(0.5, 0.1, 0.9))


def test_fit_interval_models_returns_three_fitted_models() -> None:
    rng = np.random.default_rng(42)
    x = rng.normal(size=(100, 2))
    y = x[:, 0] + rng.normal(scale=0.5, size=100)
    p10_m, p50_m, p90_m = fit_prediction_interval_models(x, y)
    # All three return predictions of correct shape.
    assert p10_m.predict(x).shape == (100,)
    assert p50_m.predict(x).shape == (100,)
    assert p90_m.predict(x).shape == (100,)


# -- compute_prediction_interval ---------------------------------------


def test_compute_interval_returns_monotonized_triple() -> None:
    rng = np.random.default_rng(42)
    x = rng.normal(size=(300, 2))
    y = x[:, 0] + rng.normal(scale=0.5, size=300)
    p10_m, p50_m, p90_m = fit_prediction_interval_models(x, y)

    # Probe at a single point.
    test_x = np.array([[0.0, 0.0]])
    interval = compute_prediction_interval(p10_m, p50_m, p90_m, test_x)
    assert isinstance(interval, PredictionInterval)
    # Even if raw predictions crossed, the returned triple is sorted.
    assert interval.p10 <= interval.p50 <= interval.p90


def test_compute_interval_handles_crossing_quantiles() -> None:
    """Quantile crossing is a known issue with independently-fitted
    regressors. We monotonize via row-sort to guarantee
    ``p10 <= p50 <= p90`` even if the raw regressors disagree."""
    # Hand-craft three pre-fitted models whose raw predictions cross
    # by using a tiny dataset where the regressors over-fit.
    rng = np.random.default_rng(42)
    x = rng.normal(size=(20, 1))
    y = x[:, 0] + 5.0 * rng.normal(size=20)  # very noisy
    p10_m, p50_m, p90_m = fit_prediction_interval_models(x, y, n_estimators=20)
    # Test on points spread across the input range — at least one
    # is likely to produce crossing raw predictions on this small
    # noisy dataset. The helper monotonizes regardless.
    for test_value in np.linspace(-2, 2, 5):
        test_x = np.array([[test_value]])
        interval = compute_prediction_interval(p10_m, p50_m, p90_m, test_x)
        assert interval.p10 <= interval.p50 <= interval.p90


def test_compute_interval_spread_property() -> None:
    interval = PredictionInterval(p10=12.0, p50=20.0, p90=28.0)
    assert interval.spread == 16.0


def test_compute_interval_rejects_multi_row_input() -> None:
    rng = np.random.default_rng(42)
    x = rng.normal(size=(50, 2))
    y = x[:, 0]
    p10_m, p50_m, p90_m = fit_prediction_interval_models(x, y, n_estimators=10)
    multi_row = np.array([[0.0, 0.0], [1.0, 1.0]])
    with pytest.raises(ValueError):
        compute_prediction_interval(p10_m, p50_m, p90_m, multi_row)


def test_compute_interval_accepts_1d_input() -> None:
    """Convenience: pass a 1-D vector and we'll reshape internally."""
    rng = np.random.default_rng(42)
    x = rng.normal(size=(50, 2))
    y = x[:, 0]
    p10_m, p50_m, p90_m = fit_prediction_interval_models(x, y, n_estimators=10)
    one_d = np.array([0.0, 0.0])
    interval = compute_prediction_interval(p10_m, p50_m, p90_m, one_d)
    assert isinstance(interval, PredictionInterval)


# -- compute_prediction_intervals_batch --------------------------------


def test_batch_returns_same_results_as_per_row() -> None:
    rng = np.random.default_rng(42)
    x = rng.normal(size=(100, 2))
    y = x[:, 0] + rng.normal(scale=0.5, size=100)
    p10_m, p50_m, p90_m = fit_prediction_interval_models(x, y, n_estimators=30)
    test_x = rng.normal(size=(5, 2))

    batch_intervals = compute_prediction_intervals_batch(p10_m, p50_m, p90_m, test_x)
    per_row_intervals = [
        compute_prediction_interval(p10_m, p50_m, p90_m, test_x[i:i + 1])
        for i in range(test_x.shape[0])
    ]
    for batch, per_row in zip(batch_intervals, per_row_intervals):
        assert batch.p10 == per_row.p10
        assert batch.p50 == per_row.p50
        assert batch.p90 == per_row.p90


# -- empirical_coverage ------------------------------------------------


def test_empirical_coverage_returns_zero_for_empty_input() -> None:
    assert empirical_coverage([], []) == 0.0


def test_empirical_coverage_requires_matching_lengths() -> None:
    intervals = [PredictionInterval(p10=0, p50=5, p90=10)]
    with pytest.raises(ValueError):
        empirical_coverage(intervals, [1.0, 2.0])


def test_empirical_coverage_counts_inside_band() -> None:
    intervals = [
        PredictionInterval(p10=0, p50=5, p90=10),    # actual=5 → in
        PredictionInterval(p10=0, p50=5, p90=10),    # actual=12 → out
        PredictionInterval(p10=0, p50=5, p90=10),    # actual=-1 → out
        PredictionInterval(p10=0, p50=5, p90=10),    # actual=0 → in (inclusive)
        PredictionInterval(p10=0, p50=5, p90=10),    # actual=10 → in (inclusive)
    ]
    actuals = [5.0, 12.0, -1.0, 0.0, 10.0]
    assert empirical_coverage(intervals, actuals) == 0.6


# -- end-to-end calibration check --------------------------------------


def test_80_percent_band_achieves_close_to_expected_coverage_on_synthetic() -> None:
    """Train 80% intervals on a noisy synthetic distribution and
    confirm the empirical coverage on a held-out test set is in the
    expected ballpark.

    This is the load-bearing calibration check — if the quantile
    regressors are mis-calibrated, the operator surface would lie
    about the interval's confidence level.
    """
    rng = np.random.default_rng(42)
    x_train = rng.normal(size=(800, 3))
    noise = rng.normal(scale=2.0, size=800)
    y_train = x_train[:, 0] * 2 + x_train[:, 1] - 0.5 * x_train[:, 2] + noise

    x_test = rng.normal(size=(400, 3))
    test_noise = rng.normal(scale=2.0, size=400)
    y_test = x_test[:, 0] * 2 + x_test[:, 1] - 0.5 * x_test[:, 2] + test_noise

    p10_m, p50_m, p90_m = fit_prediction_interval_models(x_train, y_train)
    intervals = compute_prediction_intervals_batch(p10_m, p50_m, p90_m, x_test)
    coverage = empirical_coverage(intervals, y_test.tolist())
    # Expected 80% with the (0.1, 0.5, 0.9) triple. Allow ±10pp
    # given the small sample — production calibration would tune
    # this with conformal correction on a true held-out fold.
    assert 0.70 <= coverage <= 0.90, (
        f"Expected ~80% coverage on a calibrated 80% band; got {coverage}. "
        "The quantile regressors may be mis-calibrated for this distribution "
        "or the test sample is too small."
    )


def test_default_quantiles_constant() -> None:
    assert DEFAULT_QUANTILES == (0.1, 0.5, 0.9)
