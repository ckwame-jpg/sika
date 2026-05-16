"""Smarter #8 (phase 1) — empirical pair-correlation math for parlays.

Bug #5 / PR #31 shipped a correlation-aware joint-probability combiner
that uses *theoretical priors*:

    shared_subject:   0.70 weight per pair
    same_team:        0.30
    shared_opponent:  0.20

These hand-set numbers were the right call for the bug fix (zero data
to learn from at the time, and the strict-product baseline was clearly
wrong). They're also a ceiling on what the parlay engine can know
about correlation. Once we have enough settled parlay history,
empirical correlation estimates per leg-type pair will:

- Catch league/sport-specific correlation that the global priors miss
  (e.g. NBA same-team scoring props correlate harder than MLB ones
  because basketball pace ties usage rates together)
- Decay over time as markets adapt
- Surface unexpected correlations the priors don't model
  (subject-vs-opponent-DRtg, weather-vs-park, etc.)

This module ships the math only — pure functions over ``(outcomes_a,
outcomes_b)`` arrays. Phase 2 will wire DB queries (against the
forthcoming bug #19 prediction archive) to populate empirical
correlations per leg-type pairing; phase 3 will replace the
theoretical priors in ``_correlation_adjusted_joint_probability``
with a blend that converges to the empirical estimate as sample size
grows.

## Why phi (== Pearson on binary), not mutual information

Binary outcomes admit closed-form correlation via the phi coefficient,
which is identical to Pearson's r computed on the {0, 1} encoding.
Mutual information would also work but loses sign (positive vs
negative correlation matters for parlay pricing — same-side legs
correlate positively, opposite-side legs correlate negatively).

## Sample-size-aware blending

Empirical correlation from N=10 settled pairs is dominated by sampling
noise; from N=300 it's reliable. The ``blend_theoretical_with_empirical``
helper interpolates smoothly: at N=0 the result is the theoretical
prior (no evidence to update); at N >> ``sample_floor`` it's the
empirical estimate; between, a linear blend by ``min(N / sample_floor,
1.0)``. Avoids the cliff edge of "below threshold → theoretical,
above → empirical."
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

__all__ = [
    "PairCorrelation",
    "pearson_correlation",
    "phi_coefficient_from_contingency",
    "estimate_pair_correlation",
    "blend_theoretical_with_empirical",
]

# Default minimum sample size for ``estimate_pair_correlation``. Below
# this floor the function returns ``None`` (insufficient signal).
# Empirically chosen — Pearson's standard-error scales as ~1/sqrt(N),
# so N=30 keeps the SE around 0.18 even for r=0 (the worst case).
DEFAULT_MIN_SAMPLE = 30

# Default ``sample_floor`` for ``blend_theoretical_with_empirical``.
# Same magnitude as DEFAULT_MIN_SAMPLE; tuned so a fully-decayed prior
# kicks in once the empirical estimate has reasonable precision.
DEFAULT_SAMPLE_FLOOR = 100


@dataclass(frozen=True, slots=True)
class PairCorrelation:
    """Empirical correlation estimate plus the sample size it was
    fit on. ``coefficient`` is in ``[-1.0, 1.0]`` (Pearson / phi).
    ``sample_size`` lets the caller decide whether to trust the
    estimate or fall back to a theoretical prior."""

    coefficient: float
    sample_size: int


# -- Pure correlation math ---------------------------------------------


def pearson_correlation(
    outcomes_a: Sequence[int], outcomes_b: Sequence[int]
) -> float:
    """Pearson's correlation coefficient. For binary inputs this is
    identical to the phi coefficient; the function works for any
    numeric inputs.

    Returns ``0.0`` (uncorrelated) when either array has zero
    variance — Pearson is undefined in that case (division by zero),
    but for the parlay use case "no signal" is the right
    interpretation: if every leg-A outcome was the same value, we
    can't say anything about how it co-moves with leg-B.
    """
    if len(outcomes_a) != len(outcomes_b):
        raise ValueError(
            f"shape mismatch: outcomes_a={len(outcomes_a)} "
            f"outcomes_b={len(outcomes_b)}"
        )
    if not outcomes_a:
        raise ValueError("pearson_correlation requires at least one row")
    a = np.asarray(outcomes_a, dtype=float)
    b = np.asarray(outcomes_b, dtype=float)
    a_mean = float(np.mean(a))
    b_mean = float(np.mean(b))
    a_centered = a - a_mean
    b_centered = b - b_mean
    a_norm = float(np.sqrt(np.sum(a_centered ** 2)))
    b_norm = float(np.sqrt(np.sum(b_centered ** 2)))
    if a_norm == 0.0 or b_norm == 0.0:
        return 0.0
    return float(np.sum(a_centered * b_centered) / (a_norm * b_norm))


def phi_coefficient_from_contingency(
    *, both: int, only_a: int, only_b: int, neither: int
) -> float:
    """Phi from a 2x2 contingency table. Equivalent to Pearson on
    the {0, 1} encoding but cheaper when the caller already has
    counts (e.g. SQL ``COUNT(*) GROUP BY a, b``).

    Returns ``0.0`` when any marginal is zero (one of the variables
    is constant — same logic as the Pearson zero-variance path).
    """
    n = both + only_a + only_b + neither
    if n == 0:
        raise ValueError("phi_coefficient_from_contingency requires at least one row")
    a_pos = both + only_a
    a_neg = only_b + neither
    b_pos = both + only_b
    b_neg = only_a + neither
    if a_pos == 0 or a_neg == 0 or b_pos == 0 or b_neg == 0:
        return 0.0
    numerator = both * neither - only_a * only_b
    denominator = math.sqrt(a_pos * a_neg * b_pos * b_neg)
    if denominator == 0.0:
        return 0.0
    return float(numerator / denominator)


def estimate_pair_correlation(
    outcomes_a: Sequence[int],
    outcomes_b: Sequence[int],
    *,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> PairCorrelation | None:
    """Return ``PairCorrelation(coefficient, sample_size)`` when at
    least ``min_sample`` rows are available; ``None`` otherwise.

    The threshold prevents callers from acting on noise — a 5-row
    estimate of "0.4 correlation" is statistically meaningless and
    would shift parlay pricing on a phantom signal. Callers can
    override ``min_sample`` for diagnostic or low-stakes paths.
    """
    if len(outcomes_a) != len(outcomes_b):
        raise ValueError(
            f"shape mismatch: outcomes_a={len(outcomes_a)} "
            f"outcomes_b={len(outcomes_b)}"
        )
    if min_sample < 0:
        raise ValueError(f"min_sample must be >= 0, got {min_sample}")
    n = len(outcomes_a)
    if n < min_sample:
        return None
    coefficient = pearson_correlation(outcomes_a, outcomes_b)
    return PairCorrelation(coefficient=coefficient, sample_size=n)


# -- Sample-size-aware blending ----------------------------------------


def blend_theoretical_with_empirical(
    theoretical: float,
    empirical: PairCorrelation | None,
    *,
    sample_floor: int = DEFAULT_SAMPLE_FLOOR,
) -> float:
    """Smooth interpolation from theoretical prior → empirical
    estimate as the sample size grows.

    - ``empirical is None`` (insufficient data): returns
      ``theoretical`` unchanged.
    - ``empirical.sample_size == 0``: same as None (degenerate; would
      blend by 0 weight anyway, but defensively handled).
    - ``empirical.sample_size >= sample_floor``: returns
      ``empirical.coefficient`` outright.
    - Between: linear blend with weight
      ``min(sample_size / sample_floor, 1.0)`` toward empirical.

    No clamping on the return value because:
    - ``theoretical`` is set by the caller and assumed to be in a
      sensible range
    - ``empirical.coefficient`` is in ``[-1.0, 1.0]`` by construction
    - The blend of two values in ``[-1.0, 1.0]`` is also in that
      range

    Sample-size-aware blending matters because the *sign* of an
    empirical estimate can flip from noise — a 12-row sample with
    apparent r=−0.4 might revert to r=+0.6 once 200 rows accumulate.
    Holding the prior firm at low N protects parlay pricing from
    chasing noise.
    """
    if sample_floor <= 0:
        raise ValueError(f"sample_floor must be > 0, got {sample_floor}")
    if empirical is None or empirical.sample_size == 0:
        return float(theoretical)
    weight = min(empirical.sample_size / sample_floor, 1.0)
    return float((1.0 - weight) * theoretical + weight * empirical.coefficient)
