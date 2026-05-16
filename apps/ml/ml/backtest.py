"""Smarter #2 (phase 1) — walk-forward backtest math.

The promotion gate currently scores a candidate model on a single
random train/test split and a single aggregate Brier number. That
metric collapses two distinct failure modes into one:

- a model that's calibrated *on average* but degrades sharply over
  time (drift, regime shifts, concept change)
- a model that's miscalibrated everywhere by the same small amount

Both can produce the same single-split Brier. The first is a real
stop-the-world signal — the second is acceptable noise. Walk-forward
evaluation surfaces the difference: split the settled-prediction
history into chronological folds, compute Brier / ECE / log-loss on
each fold independently, and use the *worst* fold as the gating
metric. A model whose worst fold is meaningfully worse than its mean
is one we don't want serving live picks.

This module ships the math only — pure functions over ``(timestamps,
probs, outcomes)`` tuples. No DB, no SQLAlchemy, no scoring-kernel
replay. Phase 2 will wire DB queries against the (forthcoming) bug
#19 prediction archive; phase 3 will gate promotion on
``worst_fold_brier`` instead of the single-split number.

## Why "walk-forward" specifically

Random k-fold leaks future information into past folds — a sample
captured at T-2d that lands in the test fold tells the model what's
about to happen at T-1d, which never happens in production. Walk-
forward respects time order: each fold is strictly later than the
training data it would have been scored against. That's the only
honest way to ask "would this model have flagged the next slate
correctly given only what was knowable when?"

## Insufficient-data semantics

Folds with fewer than ``min_per_fold`` rows are dropped — a 3-row
fold's Brier is dominated by sampling noise and would inflate the
worst-fold metric meaninglessly. Callers that want to surface the
"insufficient data for this period" signal can compare the count of
returned folds to the expected count from
``(end - start) / fold_days``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

import numpy as np

from ml.recalibration import expected_calibration_error

__all__ = [
    "WalkForwardFold",
    "brier_score",
    "log_loss",
    "single_fold_metrics",
    "walk_forward_evaluate",
    "worst_fold_brier",
    "best_fold_brier",
    "mean_brier",
    "fold_brier_spread",
]


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One chronological slice of the backtest. ``start`` is inclusive,
    ``end`` is exclusive — adjacent folds tile the input timeline
    without overlap or gap.

    ``brier``, ``ece``, ``log_loss`` are the standard metrics computed
    on this fold's predictions only. ``sample_size`` is the row count;
    callers that want to weight aggregate metrics by sample size can
    do so externally (this module's aggregations are unweighted by
    design — a small fold with terrible Brier is *more* informative
    about model failure than a giant fold with mediocre Brier, which
    is the whole point of the worst-fold gating choice).
    """

    start: datetime
    end: datetime
    sample_size: int
    brier: float
    ece: float
    log_loss: float


# -- Per-row metrics ---------------------------------------------------


def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error between predicted probability and binary
    outcome. Lower is better; perfect predictions yield 0.0."""
    if len(probs) != len(outcomes):
        raise ValueError(
            f"probs ({len(probs)}) and outcomes ({len(outcomes)}) must match"
        )
    if not probs:
        raise ValueError("brier_score requires at least one row")
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    return float(np.mean((p - o) ** 2))


def log_loss(
    probs: Sequence[float], outcomes: Sequence[int], *, eps: float = 1e-15
) -> float:
    """Binary cross-entropy. Lower is better. Probabilities are
    clipped to ``[eps, 1 - eps]`` so a confident wrong prediction
    contributes a large finite penalty rather than ``+inf``."""
    if len(probs) != len(outcomes):
        raise ValueError(
            f"probs ({len(probs)}) and outcomes ({len(outcomes)}) must match"
        )
    if not probs:
        raise ValueError("log_loss requires at least one row")
    p = np.clip(np.asarray(probs, dtype=float), eps, 1.0 - eps)
    o = np.asarray(outcomes, dtype=float)
    return float(-np.mean(o * np.log(p) + (1.0 - o) * np.log(1.0 - p)))


def single_fold_metrics(
    probs: Sequence[float], outcomes: Sequence[int], *, ece_bins: int = 10
) -> tuple[float, float, float]:
    """Compute (brier, ece, log_loss) for a single fold's worth of
    rows. Convenience wrapper that keeps the fold construction loop
    short and centralizes the metric set so callers can't drift."""
    return (
        brier_score(probs, outcomes),
        expected_calibration_error(
            np.asarray(probs, dtype=float),
            np.asarray(outcomes, dtype=int),
            n_bins=ece_bins,
        ),
        log_loss(probs, outcomes),
    )


# -- Walk-forward orchestration ----------------------------------------


def _coerce_utc(value: datetime) -> datetime:
    """Naive timestamps are interpreted as UTC. SQLite stores
    timezone-aware columns as naive UTC on read, so calling code
    routinely hands back mixed naive/aware mixes — silently failing
    here would let one stray naive row blow up the whole sort."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def walk_forward_evaluate(
    timestamps: Sequence[datetime],
    probs: Sequence[float],
    outcomes: Sequence[int],
    *,
    fold_days: int = 7,
    min_per_fold: int = 30,
    ece_bins: int = 10,
) -> list[WalkForwardFold]:
    """Bin (timestamps, probs, outcomes) into chronological folds of
    ``fold_days`` width and compute per-fold metrics.

    Folds whose row count is below ``min_per_fold`` are dropped — a
    handful of rows can't distinguish a calibration problem from
    sampling noise, and the worst-fold aggregation would treat their
    inflated Brier as a real signal. Returned folds are in
    chronological order by ``start``.

    Empty input returns an empty list. Negative ``fold_days`` /
    ``min_per_fold`` raise ``ValueError`` early — they're almost
    always a caller bug (off-by-one in date math) and silently
    coercing them would mask it.
    """
    if fold_days <= 0:
        raise ValueError(f"fold_days must be > 0, got {fold_days}")
    if min_per_fold < 0:
        raise ValueError(f"min_per_fold must be >= 0, got {min_per_fold}")
    if not (len(timestamps) == len(probs) == len(outcomes)):
        raise ValueError(
            f"shape mismatch: timestamps={len(timestamps)} probs={len(probs)} "
            f"outcomes={len(outcomes)}"
        )
    if not timestamps:
        return []

    # Sort by timestamp so fold construction is deterministic regardless
    # of caller ordering.
    coerced = [_coerce_utc(ts) for ts in timestamps]
    order = sorted(range(len(coerced)), key=coerced.__getitem__)
    sorted_ts = [coerced[i] for i in order]
    sorted_probs = [probs[i] for i in order]
    sorted_outcomes = [outcomes[i] for i in order]

    fold_width = timedelta(days=fold_days)
    fold_start = sorted_ts[0]
    horizon = sorted_ts[-1]

    folds: list[WalkForwardFold] = []
    cursor = 0
    while fold_start <= horizon:
        fold_end = fold_start + fold_width
        # Walk the cursor forward to collect rows whose ts is in
        # ``[fold_start, fold_end)``. Sorted input guarantees a single
        # forward pass suffices.
        bucket_probs: list[float] = []
        bucket_outcomes: list[int] = []
        while cursor < len(sorted_ts) and sorted_ts[cursor] < fold_end:
            bucket_probs.append(sorted_probs[cursor])
            bucket_outcomes.append(sorted_outcomes[cursor])
            cursor += 1
        if len(bucket_probs) >= min_per_fold:
            brier, ece, ll = single_fold_metrics(
                bucket_probs, bucket_outcomes, ece_bins=ece_bins
            )
            folds.append(
                WalkForwardFold(
                    start=fold_start,
                    end=fold_end,
                    sample_size=len(bucket_probs),
                    brier=brier,
                    ece=ece,
                    log_loss=ll,
                )
            )
        fold_start = fold_end

    return folds


# -- Aggregations ------------------------------------------------------


def worst_fold_brier(folds: Iterable[WalkForwardFold]) -> float | None:
    """Return the highest (worst) Brier across folds, or ``None`` when
    no folds remain after the ``min_per_fold`` filter. This is the
    metric the promotion gate consumes — a model that ever performs
    badly on a real-world chronological slice does not get promoted,
    even if its average is fine."""
    values = [fold.brier for fold in folds]
    return max(values) if values else None


def best_fold_brier(folds: Iterable[WalkForwardFold]) -> float | None:
    values = [fold.brier for fold in folds]
    return min(values) if values else None


def mean_brier(folds: Iterable[WalkForwardFold]) -> float | None:
    """Unweighted mean of fold Briers. Provided for comparison
    against ``worst_fold_brier`` — the gap between the two is the
    drift / regime-shift signal."""
    values = [fold.brier for fold in folds]
    return float(np.mean(values)) if values else None


def fold_brier_spread(folds: Iterable[WalkForwardFold]) -> float | None:
    """``worst - best`` across folds. A non-trivial spread (say,
    >0.02) is the structural signal that a single-split metric would
    have hidden — even if the mean is acceptable, the model is
    materially worse on some real slices than others."""
    values = [fold.brier for fold in folds]
    if not values:
        return None
    return float(max(values) - min(values))
