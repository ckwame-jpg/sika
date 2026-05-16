"""Tests for Smarter #2 phase 1 — walk-forward backtest math.

The load-bearing test is ``test_walk_forward_catches_drift_that_mean_hides``:
we construct synthetic data where the model is well-calibrated for
the first half and biased for the second half. A single-split or
mean-Brier metric gives an acceptable number; walk-forward correctly
flags the bad fold. That's the whole reason this module exists, so
the test pins the behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ml.backtest import (
    WalkForwardFold,
    best_fold_brier,
    brier_score,
    fold_brier_spread,
    log_loss,
    mean_brier,
    single_fold_metrics,
    walk_forward_evaluate,
    worst_fold_brier,
)

_RNG = np.random.default_rng(20260515)


# -- Per-row metrics ---------------------------------------------------


def test_brier_score_perfect_predictions_yields_zero() -> None:
    assert brier_score([0.0, 1.0, 0.0, 1.0], [0, 1, 0, 1]) == pytest.approx(0.0)


def test_brier_score_worst_predictions_yields_one() -> None:
    """All probabilities the opposite of the actual outcome → mean
    squared error of 1.0."""
    assert brier_score([0.0, 1.0, 0.0, 1.0], [1, 0, 1, 0]) == pytest.approx(1.0)


def test_brier_score_indifferent_predictions() -> None:
    """0.5 for everything → MSE of 0.25 regardless of outcomes."""
    assert brier_score([0.5] * 10, [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]) == pytest.approx(0.25)


def test_brier_score_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError, match="must match"):
        brier_score([0.5, 0.5], [1])


def test_brier_score_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one"):
        brier_score([], [])


def test_log_loss_perfect_predictions_yields_zero() -> None:
    assert log_loss([1.0, 0.0], [1, 0]) == pytest.approx(0.0, abs=1e-10)


def test_log_loss_clamps_extreme_predictions() -> None:
    """A prob of 0.0 against outcome 1 would naively be -inf; the
    eps clip keeps it finite. Documents the clip is load-bearing,
    not cosmetic."""
    value = log_loss([0.0], [1], eps=1e-15)
    assert math_is_finite(value)
    assert value > 30  # large penalty, not infinity


def math_is_finite(value: float) -> bool:
    """Local helper so tests don't depend on importing ``math``."""
    return value == value and value != float("inf") and value != float("-inf")


def test_single_fold_metrics_returns_three_floats() -> None:
    brier, ece, ll = single_fold_metrics([0.6, 0.4, 0.7, 0.3], [1, 0, 1, 0])
    assert all(isinstance(v, float) for v in (brier, ece, ll))


# -- Walk-forward orchestration ----------------------------------------


def _seed_well_calibrated(n: int, base: datetime) -> tuple[list, list, list]:
    """Generate ``n`` (timestamp, prob, outcome) rows where
    ``outcome ~ Bernoulli(prob)`` — perfect calibration in the limit."""
    timestamps = [base + timedelta(hours=i) for i in range(n)]
    probs = _RNG.uniform(0.1, 0.9, size=n).tolist()
    outcomes = (_RNG.uniform(0.0, 1.0, size=n) < np.asarray(probs)).astype(int).tolist()
    return timestamps, probs, outcomes


def test_walk_forward_evaluate_empty_input_returns_empty() -> None:
    assert walk_forward_evaluate([], [], []) == []


def test_walk_forward_evaluate_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError, match="shape mismatch"):
        walk_forward_evaluate([datetime.now(timezone.utc)], [0.5, 0.5], [1, 0])


def test_walk_forward_evaluate_rejects_zero_fold_days() -> None:
    with pytest.raises(ValueError, match="fold_days"):
        walk_forward_evaluate([], [], [], fold_days=0)


def test_walk_forward_evaluate_rejects_negative_fold_days() -> None:
    with pytest.raises(ValueError, match="fold_days"):
        walk_forward_evaluate([], [], [], fold_days=-1)


def test_walk_forward_evaluate_rejects_negative_min_per_fold() -> None:
    with pytest.raises(ValueError, match="min_per_fold"):
        walk_forward_evaluate([], [], [], min_per_fold=-1)


def test_walk_forward_evaluate_drops_undersized_folds() -> None:
    """Only 5 rows total, min_per_fold=10 → no folds emitted."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts, probs, outcomes = _seed_well_calibrated(5, base)

    folds = walk_forward_evaluate(ts, probs, outcomes, min_per_fold=10)

    assert folds == []


def test_walk_forward_evaluate_returns_chronological_folds() -> None:
    """Fold ``start`` values must be strictly increasing — drift
    detection only works if folds reflect time order."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # 21 days × ~10 rows/day → enough to clear default min_per_fold=30
    # at fold_days=7.
    ts, probs, outcomes = _seed_well_calibrated(220, base)

    folds = walk_forward_evaluate(ts, probs, outcomes, fold_days=7, min_per_fold=30)

    assert len(folds) >= 2
    starts = [f.start for f in folds]
    assert starts == sorted(starts)
    for i in range(len(folds) - 1):
        assert folds[i].end == folds[i + 1].start


def test_walk_forward_evaluate_handles_unsorted_input() -> None:
    """Caller hands rows in arbitrary order. The function must sort
    them; otherwise fold buckets get random rows and metrics are
    meaningless."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts, probs, outcomes = _seed_well_calibrated(220, base)
    # Shuffle into a random order.
    permutation = _RNG.permutation(len(ts)).tolist()
    ts_shuf = [ts[i] for i in permutation]
    probs_shuf = [probs[i] for i in permutation]
    outcomes_shuf = [outcomes[i] for i in permutation]

    folds_sorted = walk_forward_evaluate(ts, probs, outcomes)
    folds_shuffled = walk_forward_evaluate(ts_shuf, probs_shuf, outcomes_shuf)

    assert len(folds_sorted) == len(folds_shuffled)
    for a, b in zip(folds_sorted, folds_shuffled):
        assert a.start == b.start
        assert a.end == b.end
        assert a.sample_size == b.sample_size
        assert a.brier == pytest.approx(b.brier)


def test_walk_forward_coerces_naive_timestamps_as_utc() -> None:
    """SQLite returns naive datetimes for tz-aware columns. The
    helper must coerce to UTC; otherwise the sort blows up on a
    mixed naive/aware list."""
    base_naive = datetime(2026, 1, 1, 0, 0, 0)  # no tzinfo
    base_aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ts_mixed = [base_naive + timedelta(hours=i) for i in range(60)] + [
        base_aware + timedelta(hours=i) for i in range(60)
    ]
    probs = _RNG.uniform(0.1, 0.9, size=120).tolist()
    outcomes = (_RNG.uniform(0.0, 1.0, size=120) < np.asarray(probs)).astype(int).tolist()

    # Should not raise.
    folds = walk_forward_evaluate(ts_mixed, probs, outcomes, fold_days=7, min_per_fold=10)

    assert all(f.start.tzinfo is not None for f in folds)


# -- Load-bearing drift catch ------------------------------------------


def test_walk_forward_catches_drift_that_mean_hides() -> None:
    """The structural reason this module exists: a model that's
    well-calibrated for one period and badly biased for another
    produces a mean-Brier indistinguishable from a uniformly
    mediocre model. Walk-forward separates the two: the worst-fold
    metric must be meaningfully worse than the mean-fold metric on
    the drifted dataset.

    Setup:
    - 10 days of well-calibrated predictions (probs match outcome rate)
    - 10 days of badly-biased predictions (model claims 0.7 but
      outcomes are ~0.3 — confidently wrong)
    - Compare worst_fold_brier vs single-split-on-everything Brier
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    n_per_day = 30  # well above min_per_fold default

    # First half: well-calibrated.
    well_probs = _RNG.uniform(0.1, 0.9, size=n_per_day * 10)
    well_outcomes = (_RNG.uniform(0.0, 1.0, size=well_probs.size) < well_probs).astype(int)
    well_ts = [base + timedelta(hours=i * 0.8) for i in range(well_probs.size)]

    # Second half: confidently wrong (claims 0.7, actual rate 0.3).
    bias_size = n_per_day * 10
    bias_probs = np.full(bias_size, 0.7)
    bias_outcomes = (_RNG.uniform(0.0, 1.0, size=bias_size) < 0.3).astype(int)
    bias_ts = [base + timedelta(days=10) + timedelta(hours=i * 0.8) for i in range(bias_size)]

    ts = well_ts + bias_ts
    probs = well_probs.tolist() + bias_probs.tolist()
    outcomes = well_outcomes.tolist() + bias_outcomes.tolist()

    folds = walk_forward_evaluate(ts, probs, outcomes, fold_days=5, min_per_fold=30)

    assert len(folds) >= 3
    worst = worst_fold_brier(folds)
    mean = mean_brier(folds)
    spread = fold_brier_spread(folds)

    assert worst is not None and mean is not None and spread is not None

    # Biased fold Brier ≈ 0.37
    #   = E[(0.7 - Y)²] with P(Y=1) = 0.3
    #   = 0.3 × (0.7 - 1)² + 0.7 × (0.7 - 0)²
    #   = 0.027 + 0.343
    # Well-calibrated folds: E[(p - Y)²] = E[p(1-p)] ≈ 0.20 over
    # p ~ uniform[0.1, 0.9]. Worst > mean + 0.03 is the structural
    # drift signal — the spread between calibrated and biased folds
    # is ~0.17 in expectation, well clear of the 0.03 floor.
    assert worst > mean + 0.03, (
        f"Walk-forward should expose drift: worst={worst:.4f} mean={mean:.4f}"
    )

    # And the spread itself is non-trivial — single-split would have
    # collapsed both halves into one number.
    assert spread > 0.03


# -- Aggregations ------------------------------------------------------


def test_aggregations_return_none_for_empty_input() -> None:
    assert worst_fold_brier([]) is None
    assert best_fold_brier([]) is None
    assert mean_brier([]) is None
    assert fold_brier_spread([]) is None


def test_aggregations_on_single_fold() -> None:
    fold = WalkForwardFold(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 8, tzinfo=timezone.utc),
        sample_size=100,
        brier=0.18,
        ece=0.04,
        log_loss=0.55,
    )
    assert worst_fold_brier([fold]) == pytest.approx(0.18)
    assert best_fold_brier([fold]) == pytest.approx(0.18)
    assert mean_brier([fold]) == pytest.approx(0.18)
    assert fold_brier_spread([fold]) == pytest.approx(0.0)


def test_aggregations_on_multi_fold() -> None:
    folds = [
        WalkForwardFold(
            start=datetime(2026, 1, i + 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, i + 2, tzinfo=timezone.utc),
            sample_size=100,
            brier=brier,
            ece=0.05,
            log_loss=0.5,
        )
        for i, brier in enumerate([0.10, 0.15, 0.20])
    ]
    assert worst_fold_brier(folds) == pytest.approx(0.20)
    assert best_fold_brier(folds) == pytest.approx(0.10)
    assert mean_brier(folds) == pytest.approx(0.15)
    assert fold_brier_spread(folds) == pytest.approx(0.10)
