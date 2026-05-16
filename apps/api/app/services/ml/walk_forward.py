"""Smarter #2 (phase 2) — DB queries that feed the walk-forward
backtest math from ``apps/ml/ml/backtest.py``.

Phase 1 shipped the pure math (``walk_forward_evaluate``,
``worst_fold_brier``, etc.). Phase 2 wires the DB so an operator
or readiness check can ask:

    "Walk-forward Brier per fold for ``nba_singles`` over the last
    180 days, in 14-day folds."

and get back ``list[WalkForwardFold]`` populated from real settled
``Prediction`` / ``ParlayPrediction`` rows. Phase 3 (a separate PR)
will swap the promotion gate from single-split Brier to
``worst_fold_brier`` consumed via this helper.

## Why a separate module from readiness.py

``readiness.py`` is already the Smarter #1 calibration / reliability
surface. Adding walk-forward queries there would push it over the
800-line file ceiling and conflate two concepts (reliability is a
single-window summary; walk-forward is the temporal-decomposition
version of the same metric).

## Why the math is duplicated here, not imported from apps/ml

The canonical math lives in ``apps/ml/ml/backtest.py``. apps/api
doesn't pip-install ``apps/ml`` (only ``packages/ml-features`` is
shared via editable install — see ``apps/api/requirements.txt``),
so a direct ``from ml.backtest import …`` would crash at import
time in serve. The pragmatic choice for this PR is to mirror the
slim subset apps/api needs (``WalkForwardFold`` +
``walk_forward_evaluate`` + per-row metrics) inline. The drift
guard in ``tests/test_walk_forward_db.py`` imports both modules
side-by-side and asserts they produce identical output on a shared
synthetic input — bug #29's standard pattern for tolerated
duplication. A future packages restructure (move evaluation math
into a shared ``ml-eval`` package) can collapse the duplication
without changing apps/api consumers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

import numpy as np
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import ParlayPrediction, Prediction
from app.services.model_families import family_definition

__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_FOLD_DAYS",
    "DEFAULT_MIN_PER_FOLD",
    "WalkForwardFold",
    "brier_score",
    "log_loss",
    "expected_calibration_error",
    "walk_forward_evaluate",
    "worst_fold_brier",
    "mean_brier",
    "fold_brier_spread",
    "query_family_walk_forward_inputs",
    "compute_family_walk_forward",
]

# Defaults sized for the current deployment cadence: 180-day window
# captures roughly one MLB-half-season + an NBA segment, 14-day folds
# give ~13 folds (enough that one bad slate doesn't dominate, few
# enough that ``min_per_fold`` is achievable).
DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_FOLD_DAYS = 14
DEFAULT_MIN_PER_FOLD = 30


# ---------------------------------------------------------------------
# Math layer — mirror of ``apps/ml/ml/backtest.py``. The drift guard
# in tests asserts these two modules agree on a shared synthetic
# input.
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    start: datetime
    end: datetime
    sample_size: int
    brier: float
    ece: float
    log_loss: float


def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
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
    if len(probs) != len(outcomes):
        raise ValueError(
            f"probs ({len(probs)}) and outcomes ({len(outcomes)}) must match"
        )
    if not probs:
        raise ValueError("log_loss requires at least one row")
    p = np.clip(np.asarray(probs, dtype=float), eps, 1.0 - eps)
    o = np.asarray(outcomes, dtype=float)
    return float(-np.mean(o * np.log(p) + (1.0 - o) * np.log(1.0 - p)))


def expected_calibration_error(
    probs: Sequence[float], outcomes: Sequence[int], *, n_bins: int = 10
) -> float:
    """Bin-size-weighted absolute gap between predicted probability
    and empirical event rate. Bit-identical mirror of
    ``apps/ml/ml/recalibration.expected_calibration_error`` — uses
    ``np.digitize`` for bucketing (not loop-based bin filtering) and
    rounds to 6 decimal places to match the canonical output. The
    drift guard in tests pins this equivalence."""
    if len(probs) != len(outcomes):
        raise ValueError(
            f"probs ({len(probs)}) and outcomes ({len(outcomes)}) must match"
        )
    if not probs:
        return 0.0
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    # ``np.digitize`` returns 1-indexed bin numbers; subtract 1 then
    # clip the tail so probabilities of exactly 1.0 land in the last
    # bin rather than overflowing to ``n_bins``.
    bin_indices = np.digitize(p, bins) - 1
    bin_indices = np.clip(bin_indices, 0, n_bins - 1)
    ece = 0.0
    total = float(p.size)
    for bin_idx in range(n_bins):
        mask = bin_indices == bin_idx
        if not mask.any():
            continue
        bin_size = float(mask.sum())
        mean_prob = float(p[mask].mean())
        empirical = float(o[mask].mean())
        ece += (bin_size / total) * abs(mean_prob - empirical)
    return round(float(ece), 6)


def _coerce_utc(value: datetime) -> datetime:
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
    """Bin (timestamps, probs, outcomes) into chronological folds.
    Mirror of ``apps/ml/ml/backtest.walk_forward_evaluate``."""
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
        bucket_probs: list[float] = []
        bucket_outcomes: list[int] = []
        while cursor < len(sorted_ts) and sorted_ts[cursor] < fold_end:
            bucket_probs.append(sorted_probs[cursor])
            bucket_outcomes.append(sorted_outcomes[cursor])
            cursor += 1
        if len(bucket_probs) >= min_per_fold:
            folds.append(
                WalkForwardFold(
                    start=fold_start,
                    end=fold_end,
                    sample_size=len(bucket_probs),
                    brier=brier_score(bucket_probs, bucket_outcomes),
                    ece=expected_calibration_error(
                        bucket_probs, bucket_outcomes, n_bins=ece_bins,
                    ),
                    log_loss=log_loss(bucket_probs, bucket_outcomes),
                )
            )
        fold_start = fold_end

    return folds


def worst_fold_brier(folds) -> float | None:
    values = [fold.brier for fold in folds]
    return max(values) if values else None


def mean_brier(folds) -> float | None:
    values = [fold.brier for fold in folds]
    return float(np.mean(values)) if values else None


def fold_brier_spread(folds) -> float | None:
    values = [fold.brier for fold in folds]
    if not values:
        return None
    return float(max(values) - min(values))


# ---------------------------------------------------------------------
# DB layer — Smarter #2 phase 2 net-new code below.
# ---------------------------------------------------------------------


def _safe_probability(value: object) -> float | None:
    """Cast ``value`` to ``float`` if finite and in ``[0, 1]``;
    ``None`` otherwise. Matches the readiness module's guard so the
    walk-forward math can't be poisoned by a malformed historical
    row."""
    if value is None:
        return None
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(coerced):
        return None
    if coerced < 0.0 or coerced > 1.0:
        return None
    return coerced


def _outcome_for_single(side: str | None, outcome: str) -> int | None:
    """Map a Prediction row to a YES-axis binary outcome.

    YES side: outcome=='won' → 1, 'lost' → 0
    NO side:  outcome=='won' → 0 (model thought NO would happen, and
              it did → from the YES axis the outcome is 0), 'lost' → 1

    push / cancelled / pending / unresolved / unrecognized side →
    None (caller drops the row).
    """
    outcome_lower = outcome.lower() if outcome else ""
    if outcome_lower not in ("won", "lost"):
        return None
    side_lower = (side or "").lower()
    if side_lower == "yes":
        return 1 if outcome_lower == "won" else 0
    if side_lower == "no":
        return 0 if outcome_lower == "won" else 1
    return None


def _outcome_for_parlay(outcome: str) -> int | None:
    """Parlay rows have no per-row ``side`` —
    ``combined_model_probability`` is the joint probability of the
    chosen leg combination, so ``outcome == 'won'`` is the YES axis
    directly."""
    outcome_lower = outcome.lower() if outcome else ""
    if outcome_lower == "won":
        return 1
    if outcome_lower == "lost":
        return 0
    return None


def _build_single_predicate(family_key: str):
    """SQL filter for ``Prediction`` rows belonging to ``family_key``.

    Returns ``None`` for unknown / non-single families so the caller
    short-circuits the query rather than fetching every row when a
    typo is passed.
    """
    definition = family_definition(family_key)
    if definition.scope != "single":
        return None
    sport = definition.sport_scope.upper()
    if family_key in ("nba_props", "mlb_props"):
        return and_(
            Prediction.sport_key == sport,
            Prediction.market_family == "player_prop",
        )
    if family_key in ("nba_singles", "mlb_singles"):
        # "singles" excludes player_prop (which has its own family)
        # — anything else (winner, total, spread) belongs here.
        return and_(
            Prediction.sport_key == sport,
            or_(
                Prediction.market_family != "player_prop",
                Prediction.market_family.is_(None),
            ),
        )
    return None


def _build_parlay_predicate(family_key: str):
    """SQL filter for ``ParlayPrediction`` rows in ``family_key``.

    Mirrors the family-key derivation in
    ``model_families.parlay_family_key``: NBA-only, MLB-only, mixed,
    and 4-6-leg combiner. The 4-6-leg variant matches by leg-count
    band only since every sport-scope combination falls under one
    key.
    """
    definition = family_definition(family_key)
    if definition.scope != "parlay":
        return None
    if definition.leg_count is not None:
        leg_filter = ParlayPrediction.leg_count == definition.leg_count
    else:
        # 4-6-leg combiner — match the leg-count band.
        leg_filter = and_(
            ParlayPrediction.leg_count >= 4,
            ParlayPrediction.leg_count <= 6,
        )
    sport_scope = definition.sport_scope.upper()
    if sport_scope in ("NBA", "MLB"):
        return and_(leg_filter, ParlayPrediction.sport_scope == sport_scope)
    if sport_scope == "MIXED":
        return and_(leg_filter, ParlayPrediction.sport_scope == "MIXED")
    return leg_filter


def query_family_walk_forward_inputs(
    db: Session,
    family_key: str,
    *,
    end_date: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[tuple[datetime, float, int]]:
    """Return ``(captured_at, predicted_yes_probability, yes_outcome)``
    tuples for ``family_key`` covering ``[end_date - lookback_days,
    end_date]``.

    Filters applied:

    - ``prediction_outcome`` in ``{won, lost}`` — push / cancelled /
      pending / unresolved cannot inform Brier.
    - For singles, ``side in {yes, no}`` (other values mean the row
      didn't reach the side-selection path; they have no
      well-defined YES-axis outcome).
    - Predicted probability must be finite and in ``[0, 1]``.

    Unknown / unsupported family keys return an empty list rather
    than raising — operator endpoints that pass a typo'd key get an
    empty fold series back, which the UI can render as
    "insufficient data."
    """
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be > 0, got {lookback_days}")

    end_at = _coerce_utc(end_date) if end_date is not None else datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=lookback_days)

    single_predicate = _build_single_predicate(family_key)
    parlay_predicate = _build_parlay_predicate(family_key)
    if single_predicate is None and parlay_predicate is None:
        return []

    rows: list[tuple[datetime, float, int]] = []

    if single_predicate is not None:
        stmt = (
            select(
                Prediction.captured_at,
                Prediction.fair_yes_price,
                Prediction.side,
                Prediction.prediction_outcome,
            )
            .where(
                single_predicate,
                Prediction.captured_at >= start_at,
                Prediction.captured_at <= end_at,
                Prediction.prediction_outcome.in_(("won", "lost")),
            )
        )
        for captured_at, fair_yes, side, outcome in db.execute(stmt).all():
            prob = _safe_probability(fair_yes)
            if prob is None:
                continue
            yes_outcome = _outcome_for_single(side, outcome)
            if yes_outcome is None:
                continue
            rows.append((_coerce_utc(captured_at), prob, yes_outcome))

    if parlay_predicate is not None:
        stmt = (
            select(
                ParlayPrediction.captured_at,
                ParlayPrediction.combined_model_probability,
                ParlayPrediction.prediction_outcome,
            )
            .where(
                parlay_predicate,
                ParlayPrediction.captured_at >= start_at,
                ParlayPrediction.captured_at <= end_at,
                ParlayPrediction.prediction_outcome.in_(("won", "lost")),
            )
        )
        for captured_at, prob_raw, outcome in db.execute(stmt).all():
            prob = _safe_probability(prob_raw)
            if prob is None:
                continue
            yes_outcome = _outcome_for_parlay(outcome)
            if yes_outcome is None:
                continue
            rows.append((_coerce_utc(captured_at), prob, yes_outcome))

    return rows


def compute_family_walk_forward(
    db: Session,
    family_key: str,
    *,
    end_date: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    fold_days: int = DEFAULT_FOLD_DAYS,
    min_per_fold: int = DEFAULT_MIN_PER_FOLD,
) -> list[WalkForwardFold]:
    """Compose ``query_family_walk_forward_inputs`` with
    ``walk_forward_evaluate``.

    Returns ``list[WalkForwardFold]`` — empty when the query
    returned no eligible rows or all folds were below
    ``min_per_fold``.
    """
    inputs = query_family_walk_forward_inputs(
        db, family_key, end_date=end_date, lookback_days=lookback_days,
    )
    if not inputs:
        return []
    timestamps = [row[0] for row in inputs]
    probs = [row[1] for row in inputs]
    outcomes = [row[2] for row in inputs]
    return walk_forward_evaluate(
        timestamps,
        probs,
        outcomes,
        fold_days=fold_days,
        min_per_fold=min_per_fold,
    )
