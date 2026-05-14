"""Smarter #21 — quantile-regression intervals for prop expected values.

A single point estimate of ``expected_stat_output`` (e.g. "Tatum will
score ~26.3 points") is fragile to player variability. Two players
both projected at 26.3 can have very different distributions: one a
24-28 range every game, the other a 14-38 swinger. The over/under
recommendation should reflect that distribution.

This module ships the building blocks: ``fit_quantile_regressor`` to
fit one ``GradientBoostingRegressor(loss="quantile", alpha=q)`` per
target quantile, and ``compute_prediction_interval`` to call three
fitted regressors and return ``(p10, p50, p90)``.

## Phase 2 (follow-up PRs)

- Training pipeline integration: fit three quantile regressors per
  prop family alongside the classifier, package the .joblib files
  in the artifact directory, plumb through ``feature_spec``.
- Inference path: load the regressors at serve time, surface
  ``(p10, p50, p90)`` in scoring diagnostics.
- UI band on the trade ticket showing the interval visually so
  operators can see "this projection is tight" vs "wide".

Phase 1 (this module) is the smallest piece that doesn't change
existing behavior — operators with the helpers can do offline analysis
and validate the interval shape before wiring into production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor


# Canonical quantile triples. Smarter #21 surfaces (p10, p50, p90) as
# the operator-facing default — 80% of the probability mass between
# the rails, with the median as the point estimate.
DEFAULT_QUANTILES: tuple[float, float, float] = (0.1, 0.5, 0.9)


@dataclass(frozen=True, slots=True)
class PredictionInterval:
    """The (p10, p50, p90) tuple plus the spread.

    ``spread`` (p90 − p10) is the operator-facing "how wide is this"
    metric — a wide spread means low confidence in the point estimate
    even if the median lands on the threshold.
    """
    p10: float
    p50: float
    p90: float

    @property
    def spread(self) -> float:
        return round(self.p90 - self.p10, 4)


def fit_quantile_regressor(
    x_train: np.ndarray,
    y_train: np.ndarray,
    *,
    quantile: float,
    n_estimators: int = 100,
    max_depth: int = 3,
    learning_rate: float = 0.05,
    random_state: int = 42,
) -> GradientBoostingRegressor:
    """Fit a single ``GradientBoostingRegressor`` with quantile loss.

    Hyperparameters mirror the classifier candidate in
    ``_candidate_estimators`` so the quantile regressors and the
    classifier are calibrated on the same complexity budget. Callers
    fit one regressor per target quantile (``0.1``, ``0.5``, ``0.9``)
    and pass the fitted models to ``compute_prediction_interval``.
    """
    if not 0.0 < quantile < 1.0:
        raise ValueError(
            f"quantile must be strictly between 0 and 1; got {quantile}"
        )
    regressor = GradientBoostingRegressor(
        loss="quantile",
        alpha=quantile,
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        random_state=random_state,
    )
    regressor.fit(x_train, y_train)
    return regressor


def fit_prediction_interval_models(
    x_train: np.ndarray,
    y_train: np.ndarray,
    *,
    quantiles: tuple[float, float, float] = DEFAULT_QUANTILES,
    **fit_kwargs,
) -> tuple[GradientBoostingRegressor, GradientBoostingRegressor, GradientBoostingRegressor]:
    """Fit three quantile regressors at the supplied quantiles.

    Convenience wrapper for the canonical (p10, p50, p90) triple.
    Returns models in the same order as ``quantiles``.
    """
    if len(quantiles) != 3:
        raise ValueError(f"quantiles must be a length-3 tuple; got {quantiles}")
    sorted_quantiles = sorted(quantiles)
    if sorted_quantiles != list(quantiles):
        raise ValueError(
            f"quantiles must be sorted ascending; got {quantiles}"
        )
    return tuple(  # type: ignore[return-value]
        fit_quantile_regressor(x_train, y_train, quantile=q, **fit_kwargs)
        for q in quantiles
    )


def compute_prediction_interval(
    p10_model: GradientBoostingRegressor,
    p50_model: GradientBoostingRegressor,
    p90_model: GradientBoostingRegressor,
    x: np.ndarray,
) -> PredictionInterval:
    """Predict the (p10, p50, p90) interval for a single feature vector.

    Output is monotonized — quantile regressors can in rare cases
    "cross" (a p10 prediction higher than the p50 for noisy data).
    Clamp to a sorted triple so downstream consumers can rely on
    ``p10 <= p50 <= p90`` without per-row defensive checks.
    """
    if x.ndim == 1:
        x = x.reshape(1, -1)
    if x.shape[0] != 1:
        raise ValueError(
            f"compute_prediction_interval expects a single-row input; "
            f"got {x.shape[0]} rows"
        )
    raw_p10 = float(p10_model.predict(x)[0])
    raw_p50 = float(p50_model.predict(x)[0])
    raw_p90 = float(p90_model.predict(x)[0])
    sorted_triple = sorted((raw_p10, raw_p50, raw_p90))
    return PredictionInterval(
        p10=round(sorted_triple[0], 4),
        p50=round(sorted_triple[1], 4),
        p90=round(sorted_triple[2], 4),
    )


def compute_prediction_intervals_batch(
    p10_model: GradientBoostingRegressor,
    p50_model: GradientBoostingRegressor,
    p90_model: GradientBoostingRegressor,
    x: np.ndarray,
) -> list[PredictionInterval]:
    """Vectorized variant for evaluation / backtest loops.

    Calls each regressor's ``predict`` once per batch rather than
    per row — the per-row helper is convenient for a single inference
    call but quadratically slower over a corpus.
    """
    if x.ndim == 1:
        x = x.reshape(1, -1)
    raw_p10 = p10_model.predict(x)
    raw_p50 = p50_model.predict(x)
    raw_p90 = p90_model.predict(x)
    stacked = np.column_stack([raw_p10, raw_p50, raw_p90])
    # Monotonize row-wise — sorting each row ensures p10 <= p50 <= p90.
    sorted_rows = np.sort(stacked, axis=1)
    return [
        PredictionInterval(
            p10=round(float(row[0]), 4),
            p50=round(float(row[1]), 4),
            p90=round(float(row[2]), 4),
        )
        for row in sorted_rows
    ]


def empirical_coverage(
    intervals: Sequence[PredictionInterval],
    actuals: Sequence[float],
) -> float:
    """Fraction of actuals that fall inside their predicted intervals.

    A calibrated 80% interval (p10–p90) should cover roughly 80% of
    held-out actuals. Substantial deviation (e.g. 60% or 95%) means
    the regressor's quantile estimates are mis-calibrated and the
    operator surface should disclose that.
    """
    if not intervals:
        return 0.0
    if len(intervals) != len(actuals):
        raise ValueError(
            f"intervals ({len(intervals)}) and actuals ({len(actuals)}) "
            "must have the same length"
        )
    covered = sum(
        1 for interval, actual in zip(intervals, actuals)
        if interval.p10 <= float(actual) <= interval.p90
    )
    return round(covered / len(intervals), 4)
