from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ml.dataset import load_settled_predictions
from ml.features import FeatureSpec, vectorize
from ml.manifest import ModelArtifact, ModelManifest


HEURISTIC_DERIVED_KEYS = {
    "yes_probability",
    "fair_yes_price",
    "fair_no_price",
    "edge",
    "confidence",
    "selection_score",
    "heuristic_fair_yes_price",
    "heuristic_edge",
    "heuristic_confidence",
    "heuristic_selection_score",
}


# PR 3d — keys emitted by the advanced-stats pass that signal a row had
# real advanced data when the prediction was captured. Any of these set
# to 1.0 means "this row is advanced-complete" for sample weighting and
# v2-only filtering.
#
# IMPORTANT: this list must stay in sync with every ``*_data_complete``
# write in ``apps/api/app/services``. The
# ``test_advanced_completeness_markers_match_api_emitters`` test scans
# the API service tree and fails if a new emitter lands without being
# added here, so drift is caught at CI time.
ADVANCED_COMPLETENESS_MARKERS = (
    # advanced_stats.py
    "advanced_data_complete",         # NBA player advanced
    "opponent_team_data_complete",    # NBA opponent recent form
    # nba_long_tail.py
    "hustle_data_complete",
    "drives_data_complete",
    "clutch_data_complete",
    "opponent_defender_data_complete",
    # mlb_advanced.py
    "mlb_batter_data_complete",       # batter sabermetrics + Statcast
    "pitcher_data_complete",          # opposing-starter advanced
    "park_data_complete",             # park factors
    "weather_data_complete",          # weather (non-dome)
    "lineup_data_complete",           # batting-order position
)


@dataclass(frozen=True, slots=True)
class TrainingResult:
    artifact_dir: Path
    manifest_path: Path | None
    model_name: str
    feature_spec: FeatureSpec
    metrics: dict[str, Any]


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def build_feature_spec(
    frame: pd.DataFrame,
    *,
    version: str,
    mode: str = "residual",
    use_median_imputation: bool = True,
) -> FeatureSpec:
    keys: set[str] = set()
    default_values: dict[str, float] = {}
    for features in frame["features"]:
        for key, value in dict(features or {}).items():
            if key == "family_key":
                continue
            if mode == "independent" and key in HEURISTIC_DERIVED_KEYS:
                continue
            numeric = _safe_float(value)
            if numeric is None:
                continue
            keys.add(str(key))
            default_values.setdefault(str(key), 0.0)
    family_keys = sorted({str(value) for value in frame["family_key"].dropna().unique()})

    if use_median_imputation:
        # PR 3d — replace the static 0.0 default with the median of rows
        # that DID have the key set. Without this, advanced features default
        # to 0.0 for historical rows captured before the advanced cache was
        # populated, which biases the model toward "no advanced data → bad"
        # patterns instead of letting the median fill in a sensible prior.
        #
        # Exclusions:
        #   1. ``ADVANCED_COMPLETENESS_MARKERS`` are emitted only as the
        #      literal 1.0 (absent → false). Their median is always 1.0,
        #      so imputing it would collapse the column to a constant.
        #   2. Any key whose training-time values are *all* in {0.0, 1.0}
        #      — one-hot/boolean indicators like ``sport_is_nba`` and
        #      ``has_team_context``. The median of a balanced binary set
        #      is 0.5, a value the model never sees during training. Worse,
        #      some of these keys (``sport_is_nba``/``sport_is_mlb``) are
        #      training-only — the API scoring path doesn't emit them, so
        #      every inference call would otherwise read the 0.5 default.
        # Both classes keep the historical 0.0 default so the present/absent
        # distinction stays meaningful and inference stays in-distribution.
        accumulator = _collect_feature_values(frame, sorted(keys))
        binary_only_keys = {
            key for key, values in accumulator.items()
            if values and all(v in (0.0, 1.0) for v in values)
        }
        skip_set = set(ADVANCED_COMPLETENESS_MARKERS) | binary_only_keys
        medians = _medians_from_accumulator(accumulator)
        for key, median_value in medians.items():
            if key in skip_set:
                continue
            default_values[key] = median_value

    return FeatureSpec(
        version=version,
        ordered_keys=sorted(keys),
        default_values=default_values,
        family_one_hot_keys=family_keys,
    )


def _collect_feature_values(frame: pd.DataFrame, keys: list[str]) -> dict[str, list[float]]:
    """Per-key list of present non-null numeric values across rows.

    Used by both ``_compute_feature_medians`` and the binary-key detection
    above. Returning the raw lists (instead of just medians) lets the caller
    decide whether each column is binary, has zero variance, etc.
    """
    accumulator: dict[str, list[float]] = {key: [] for key in keys}
    for features in frame["features"]:
        feats = dict(features or {})
        for key in keys:
            value = _safe_float(feats.get(key))
            if value is not None:
                accumulator[key].append(value)
    return accumulator


def _medians_from_accumulator(accumulator: dict[str, list[float]]) -> dict[str, float]:
    """Take per-key value lists and reduce to medians; empty lists → 0.0."""
    medians: dict[str, float] = {}
    for key, values in accumulator.items():
        medians[key] = float(np.median(values)) if values else 0.0
    return medians


def _compute_feature_medians(frame: pd.DataFrame, keys: list[str]) -> dict[str, float]:
    """Median of non-null numeric values for each key across all rows.
    Keys absent from every row return 0.0 (the historical default).

    Thin wrapper around ``_collect_feature_values`` /
    ``_medians_from_accumulator`` so future tweaks to either helper
    flow through here automatically. Kept as a public symbol for the
    test surface in ``test_pr3d_training_v2``.
    """
    return _medians_from_accumulator(_collect_feature_values(frame, keys))


def _row_is_advanced_complete(features: dict[str, Any]) -> bool:
    """Return True when at least one ADVANCED_COMPLETENESS_MARKERS key is 1.0."""
    feats = dict(features or {})
    for marker in ADVANCED_COMPLETENESS_MARKERS:
        if _safe_float(feats.get(marker)) == 1.0:
            return True
    return False


def _advanced_completeness_mask(frame: pd.DataFrame) -> np.ndarray:
    """Boolean array — True for rows that have at least one advanced
    completeness marker set to 1.0."""
    return np.asarray(
        [_row_is_advanced_complete(features) for features in frame["features"]],
        dtype=bool,
    )


def _advanced_completeness_counts(frame: pd.DataFrame) -> dict[str, int]:
    """Per-family count of advanced-complete rows, plus a ``__total__`` key."""
    mask = _advanced_completeness_mask(frame)
    counts: dict[str, int] = {"__total__": int(mask.sum())}
    family_series = frame["family_key"].astype(str)
    for family in sorted(family_series.unique()):
        family_mask = family_series.eq(family).to_numpy() & mask
        counts[family] = int(family_mask.sum())
    return counts


def _build_sample_weights(
    frame: pd.DataFrame,
    *,
    advanced_weight: float,
) -> np.ndarray:
    """1.0 for rows without advanced data, ``advanced_weight`` for rows
    with any completeness marker set. Used to up-weight high-signal rows
    during mixed-mode training."""
    mask = _advanced_completeness_mask(frame)
    weights = np.ones(len(frame), dtype=np.float64)
    weights[mask] = float(advanced_weight)
    return weights


def _matrix(frame: pd.DataFrame, feature_spec: FeatureSpec) -> np.ndarray:
    return np.vstack([vectorize(dict(features or {}), feature_spec) for features in frame["features"]])


def _split_by_group(frame: pd.DataFrame, group_column: str, *, test_size: float = 0.2, random_state: int = 42) -> tuple[np.ndarray, np.ndarray]:
    groups = frame[group_column].fillna(frame["ticker"]).astype(str).to_numpy()
    if len(set(groups)) < 2:
        return _split_by_time(frame, test_size=test_size)
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state)
    train_idx, test_idx = next(splitter.split(frame, frame["target"], groups=groups))
    return train_idx, test_idx


def _split_by_time(frame: pd.DataFrame, *, test_size: float = 0.2) -> tuple[np.ndarray, np.ndarray]:
    ordered = frame.sort_values(["captured_at", "id"]).index.to_numpy()
    split_at = max(int(len(ordered) * (1.0 - test_size)), 1)
    split_at = min(split_at, len(ordered) - 1)
    return ordered[:split_at], ordered[split_at:]


# Bug #20 — walk-forward evaluation
#
# The original promotion gate consumed the single 80/20 time-split Brier
# (``_split_by_time``). That is not robust against a "one lucky week"
# regime — a model that happens to look good on the final 20% of rows
# can promote and then regress on the next slate. The fix is an
# expanding-window walk-forward: bucket rows into weekly windows, fit
# on every prior week, evaluate on the current week, and consume the
# *worst* per-fold Brier as the promotion candidate.
#
# Low-volume families (game-winner markets settle ~30 picks/week) need
# 2-week buckets to clear the 25-row threshold per fold; we auto-widen
# when weekly bucketing fails to assemble enough valid folds.
#
# A parallel implementation lives in ``apps/api/app/services/ml/promotion.py``
# (post-hoc heuristic-vs-shadow Brier per week). The two implementations
# can be consolidated when bug #29 introduces a shared package.
MIN_WALK_FORWARD_ROWS_PER_FOLD = 25
MIN_WALK_FORWARD_VALID_FOLDS = 8


def _walk_forward_folds(
    captured_at: Any,
    *,
    target_values: np.ndarray | None = None,
    eligibility_mask: np.ndarray | None = None,
    min_rows_per_fold: int = MIN_WALK_FORWARD_ROWS_PER_FOLD,
    min_valid_folds: int = MIN_WALK_FORWARD_VALID_FOLDS,
) -> tuple[list[tuple[np.ndarray, np.ndarray]], dict[str, Any]]:
    """Expanding-window walk-forward folds keyed off capture time.

    Returns ``(folds, meta)``. ``folds`` is a list of
    ``(train_idx, test_idx)`` tuples whose indices refer to positions in
    the input ``captured_at`` array (suitable for ``frame.iloc``).
    ``meta`` records the windowing decision so callers can surface it
    in training metadata.

    Bucketing rule:
      1. Try 7-day buckets first. If at least ``min_valid_folds`` test
         buckets clear ``min_rows_per_fold`` rows, use weekly.
      2. Otherwise try 14-day buckets — the low-volume escape hatch.
      3. If neither attempt clears the floor, mark
         ``insufficient_history`` and return the better-populated of the
         two attempts so observability survives the failure.

    The earliest bucket is always training-only — expanding-window has
    no history before the first observation. Subsequent buckets become
    test folds whose training slice is every row in earlier buckets.

    When ``target_values`` is supplied, folds whose training slice has
    fewer than two distinct target classes are dropped (a one-class
    training set crashes LogisticRegression/HGBC during refit). The
    dropped buckets are still counted in ``meta['single_class_skipped_folds']``
    so the failure mode stays visible.

    When ``eligibility_mask`` is supplied, only rows where the mask is
    True count toward the per-fold row floor and toward ``test_idx``.
    Training stays global (all rows in earlier buckets). This supports
    per-family floors where the global training cohort feeds a
    family-restricted evaluation — the auto-widening from weekly to
    biweekly is then driven by the *family's* density rather than the
    combined dataset's, so a low-volume family can use biweekly while
    the global eval stays weekly.
    """
    timestamps = pd.to_datetime(captured_at, utc=True)
    timestamp_values = np.asarray(timestamps, dtype="datetime64[ns]")
    if timestamp_values.size == 0:
        return [], {
            "fold_count": 0,
            "week_size_days": None,
            "min_rows_per_fold": min_rows_per_fold,
            "min_valid_folds": min_valid_folds,
            "rows_per_fold": [],
            "single_class_skipped_folds": 0,
            "insufficient_history": True,
        }
    sort_order = np.argsort(timestamp_values, kind="stable")
    sorted_ts = timestamp_values[sort_order]
    offsets_days = (sorted_ts - sorted_ts[0]) / np.timedelta64(1, "D")
    sorted_eligibility: np.ndarray | None = None
    if eligibility_mask is not None:
        if eligibility_mask.shape != timestamp_values.shape:
            raise ValueError("eligibility_mask must align with captured_at")
        sorted_eligibility = np.asarray(eligibility_mask, dtype=bool)[sort_order]

    best_attempt: tuple[list[tuple[np.ndarray, np.ndarray]], dict[str, Any]] | None = None
    for week_size in (7, 14):
        bucket_ids = (offsets_days // week_size).astype(int)
        unique_buckets = sorted(set(int(b) for b in bucket_ids.tolist()))
        folds: list[tuple[np.ndarray, np.ndarray]] = []
        rows_per_fold: list[int] = []
        single_class_skipped = 0
        for bucket in unique_buckets[1:]:
            bucket_mask = bucket_ids == bucket
            test_mask = bucket_mask if sorted_eligibility is None else (bucket_mask & sorted_eligibility)
            train_mask = bucket_ids < bucket
            test_count = int(test_mask.sum())
            if test_count < min_rows_per_fold:
                continue
            if int(train_mask.sum()) == 0:
                continue
            train_idx = sort_order[train_mask]
            test_idx = sort_order[test_mask]
            if target_values is not None and np.unique(target_values[train_idx]).size < 2:
                # Single-class training set would crash the sklearn fits
                # downstream; drop the fold rather than promote on a
                # partial walk-forward. Once a row of the missing class
                # appears later, subsequent folds become viable.
                single_class_skipped += 1
                continue
            folds.append((train_idx, test_idx))
            rows_per_fold.append(test_count)
        meta = {
            "fold_count": len(folds),
            "week_size_days": week_size,
            "min_rows_per_fold": min_rows_per_fold,
            "min_valid_folds": min_valid_folds,
            "rows_per_fold": rows_per_fold,
            "single_class_skipped_folds": single_class_skipped,
            "insufficient_history": len(folds) < min_valid_folds,
        }
        if len(folds) >= min_valid_folds:
            return folds, meta
        if best_attempt is None or len(folds) > len(best_attempt[0]):
            best_attempt = (folds, meta)

    assert best_attempt is not None
    folds, meta = best_attempt
    return folds, {**meta, "insufficient_history": True}


def walk_forward_evaluation(
    frame: pd.DataFrame,
    feature_spec: FeatureSpec,
    *,
    sample_weight: np.ndarray | None = None,
    use_median_imputation: bool = True,
    candidates: dict[str, Any] | None = None,
    min_rows_per_fold: int = MIN_WALK_FORWARD_ROWS_PER_FOLD,
    min_valid_folds: int = MIN_WALK_FORWARD_VALID_FOLDS,
    family_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Expanding-window walk-forward Brier per candidate.

    Each fold refits the candidate on the training slice (every row in
    earlier weeks), evaluates on the test bucket (the current week), and
    records the Brier. The promotion gate downstream consumes
    ``worst_fold_brier`` — the maximum across folds — so a candidate
    that ships only because of one favourable stretch fails the gate.

    When ``family_keys`` is supplied, a per-family block is emitted in
    addition to the global one. Each entry restricts the *test* fold
    to rows belonging to that family (training stays global, matching
    the served-model topology — one shared artifact per training run).
    A family with fewer than ``min_valid_folds`` family-filtered test
    folds (each with ``min_rows_per_fold`` rows of that family) is
    flagged ``insufficient_history`` and must not promote regardless
    of how the global gate scores. This enforces the per-family
    history floor demanded by bug #20.

    Returns a dict with ``insufficient_history`` set when the
    fold-building stage can't assemble at least ``min_valid_folds``
    folds with ``min_rows_per_fold`` rows each (after the weekly →
    biweekly fall-back). When insufficient, ``candidates`` is empty —
    no fits are run — and callers must treat the result as "do not
    promote".
    """
    target_array = frame["target"].to_numpy()
    folds, fold_meta = _walk_forward_folds(
        frame["captured_at"],
        target_values=target_array,
        min_rows_per_fold=min_rows_per_fold,
        min_valid_folds=min_valid_folds,
    )
    payload: dict[str, Any] = {
        "fold_window_days": fold_meta["week_size_days"],
        "fold_count": fold_meta["fold_count"],
        "rows_per_fold": list(fold_meta["rows_per_fold"]),
        "min_rows_per_fold": fold_meta["min_rows_per_fold"],
        "min_valid_folds": fold_meta["min_valid_folds"],
        "single_class_skipped_folds": int(fold_meta.get("single_class_skipped_folds", 0)),
        "insufficient_history": fold_meta["insufficient_history"],
        "candidates": {},
        "per_family": {},
    }
    if payload["insufficient_history"]:
        # When the global fold-building fails, no family can promote.
        for family_key in family_keys:
            payload["per_family"][family_key] = {
                "fold_count": 0,
                "rows_per_fold": [],
                "insufficient_history": True,
                "candidates": {},
            }
        return payload

    candidate_set = candidates if candidates is not None else _candidate_estimators(len(frame))
    family_series = frame["family_key"].astype(str).to_numpy() if "family_key" in frame.columns else None

    for name, template in candidate_set.items():
        fold_briers: list[float] = []
        for train_idx, test_idx in folds:
            train_frame = frame.iloc[train_idx]
            test_frame = frame.iloc[test_idx]
            fold_spec = _fold_feature_spec(
                train_frame,
                feature_spec,
                use_median_imputation=use_median_imputation,
            )
            x_train = _matrix(train_frame, fold_spec)
            x_test = _matrix(test_frame, fold_spec)
            fold_weights = None if sample_weight is None else sample_weight[train_idx]
            fitted = _fit_estimator(clone(template), x_train, target_array[train_idx], sample_weight=fold_weights)
            probabilities = np.clip(fitted.predict_proba(x_test)[:, 1], 1e-6, 1 - 1e-6)
            fold_briers.append(round(float(brier_score_loss(target_array[test_idx], probabilities)), 6))
        payload["candidates"][name] = {
            "fold_briers": fold_briers,
            "worst_fold_brier": round(float(max(fold_briers)), 6),
            "mean_fold_brier": round(float(np.mean(fold_briers)), 6),
        }

    if not family_keys:
        return payload

    if family_series is None:
        raise ValueError("family_keys requested but frame has no 'family_key' column")

    # Per-family walk-forward — the per-family floor (≥25 family rows
    # per fold, ≥8 folds) is checked AT THE FAMILY's chosen windowing.
    # A low-volume family runs biweekly even when the combined dataset
    # cleared weekly; otherwise the documented widening would be
    # short-circuited by the global gate.
    for family_key in family_keys:
        family_mask = family_series == family_key
        family_folds, family_meta = _walk_forward_folds(
            frame["captured_at"],
            target_values=target_array,
            eligibility_mask=family_mask,
            min_rows_per_fold=min_rows_per_fold,
            min_valid_folds=min_valid_folds,
        )
        family_payload: dict[str, Any] = {
            "fold_count": int(family_meta["fold_count"]),
            "window_days": family_meta["week_size_days"],
            "rows_per_fold": list(family_meta["rows_per_fold"]),
            "single_class_skipped_folds": int(family_meta.get("single_class_skipped_folds", 0)),
            "insufficient_history": bool(family_meta["insufficient_history"]),
            "candidates": {},
        }
        if family_payload["insufficient_history"]:
            payload["per_family"][family_key] = family_payload
            continue

        for name, template in candidate_set.items():
            fold_briers_family: list[float] = []
            for train_idx, test_idx in family_folds:
                train_frame = frame.iloc[train_idx]
                test_frame = frame.iloc[test_idx]
                fold_spec = _fold_feature_spec(
                    train_frame,
                    feature_spec,
                    use_median_imputation=use_median_imputation,
                )
                x_train = _matrix(train_frame, fold_spec)
                x_test = _matrix(test_frame, fold_spec)
                fold_weights = None if sample_weight is None else sample_weight[train_idx]
                fitted = _fit_estimator(
                    clone(template),
                    x_train,
                    target_array[train_idx],
                    sample_weight=fold_weights,
                )
                probabilities = np.clip(fitted.predict_proba(x_test)[:, 1], 1e-6, 1 - 1e-6)
                fold_briers_family.append(
                    round(float(brier_score_loss(target_array[test_idx], probabilities)), 6)
                )
            family_payload["candidates"][name] = {
                "fold_briers": fold_briers_family,
                "worst_fold_brier": round(float(max(fold_briers_family)), 6),
                "mean_fold_brier": round(float(np.mean(fold_briers_family)), 6),
            }
        payload["per_family"][family_key] = family_payload

    return payload


def _metrics_for_predictions(frame: pd.DataFrame, indices: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    selected = frame.loc[indices]
    target = selected["target"].to_numpy()
    clipped = np.clip(probabilities, 1e-6, 1 - 1e-6)
    # Bug #2 P3: probabilities are now P(YES) but ``suggested_price`` is the
    # contract price for whichever side the recommendation took. Edge has to
    # be ``selected_side_probability - suggested_price`` so NO-side bets are
    # ranked against the right baseline.
    side_yes = selected["side"].astype(str).str.lower().to_numpy() == "yes"
    selected_side_probability = np.where(side_yes, clipped, 1.0 - clipped)
    edge = selected_side_probability - selected["suggested_price"].fillna(0.5).astype(float).to_numpy()
    top_n = max(int(np.ceil(len(selected) * 0.10)), 1)
    top_idx = np.argsort(edge)[-top_n:]
    # Fallback when realized_pnl is missing: derive from prediction_outcome
    # (the trade-level result) rather than target (now YES-won, not trade-won).
    # fillna(0.0) on the map covers push/cancelled or any non-binary outcome
    # so unknown values don't propagate NaN into top_decile_roi.
    pnl = selected["realized_pnl"].fillna(
        selected["prediction_outcome"].map({"won": 1.0, "lost": -1.0}).fillna(0.0)
    ).astype(float).to_numpy()
    buckets = pd.cut(clipped, bins=[0.0, 0.4, 0.5, 0.6, 1.0], include_lowest=True)
    calibration = (
        pd.DataFrame({"bucket": buckets.astype(str), "target": target, "probability": clipped})
        .groupby("bucket", observed=False)
        .agg(count=("target", "size"), actual_rate=("target", "mean"), avg_probability=("probability", "mean"))
        .reset_index()
        .to_dict(orient="records")
    )
    return {
        "rows": int(len(selected)),
        "brier": round(float(brier_score_loss(target, clipped)), 6),
        "log_loss": round(float(log_loss(target, clipped, labels=[0, 1])), 6),
        "top_decile_roi": round(float(np.mean(pnl[top_idx])), 6),
        "avg_edge_capture": round(float(np.mean(edge[top_idx])), 6),
        "calibration_buckets": calibration,
    }


def _candidate_estimators(sample_count: int):
    min_leaf = max(10, min(50, sample_count // 20 or 10))
    return {
        "logistic_regression": make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42),
        ),
        "hist_gradient_boosting": HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=150,
            max_leaf_nodes=15,
            min_samples_leaf=min_leaf,
            l2_regularization=1.0,
            random_state=42,
        ),
    }


def _fit_estimator(
    estimator,
    x_train: np.ndarray,
    y_train: np.ndarray,
    *,
    sample_weight: np.ndarray | None = None,
) -> Any:
    """Fit an estimator with optional sample weights.

    Direct estimators (HistGradientBoostingClassifier) accept
    ``sample_weight`` natively via ``fit(sample_weight=...)``.

    Pipeline candidates (our LR is ``StandardScaler → LogisticRegression``)
    need step-prefixed routing — ``pipeline.fit(X, y, logisticregression__sample_weight=w)``
    — because the unprefixed kwarg is ambiguous and sklearn raises. We
    route weights to the pipeline's final classifier step so the LR also
    sees the weighted distribution. Without this, candidate selection
    becomes asymmetric: HGBC trains on a weighted dataset while LR
    trains uniform, and they're then compared on the same held-out
    brier — biasing winner selection toward whichever candidate
    accidentally aligned with the weighted distribution.

    Calibration path (``cv >= 2 and len(y_train) >= 500``):
    ``CalibratedClassifierCV.fit(sample_weight=...)`` accepts the kwarg
    and forwards it to the base estimator's ``.fit``. For a Pipeline
    base, that becomes ``pipeline.fit(X, y, sample_weight=w)`` —
    unprefixed — which raises. There's no clean way to thread a
    prefixed kwarg through CCCV in current sklearn, so the pipeline
    path drops weights only when calibration is active. The
    non-calibration branch (which is the test fixtures' path and the
    early-rollout production path with <500 settled rows) still
    weights the LR pipeline correctly.

    When ``sample_weight`` is None, behaviour is identical to the
    pre-PR3d code path.
    """
    from sklearn.pipeline import Pipeline

    class_counts = np.bincount(y_train, minlength=2)
    cv = int(min(3, class_counts.min()))

    is_pipeline = isinstance(estimator, Pipeline)

    direct_fit_kwargs: dict[str, Any] = {}
    if sample_weight is not None:
        if is_pipeline:
            classifier_step = estimator.steps[-1][0]
            direct_fit_kwargs[f"{classifier_step}__sample_weight"] = sample_weight
        else:
            direct_fit_kwargs["sample_weight"] = sample_weight

    if cv >= 2 and len(y_train) >= 500:
        method = "isotonic" if len(y_train) >= 500 else "sigmoid"
        calibrated = CalibratedClassifierCV(estimator=estimator, method=method, cv=cv)
        # CCCV doesn't propagate prefixed kwargs cleanly. Drop weights for
        # pipelines in this path; HGBC still receives them.
        if is_pipeline:
            return calibrated.fit(x_train, y_train)
        cccv_fit_kwargs: dict[str, Any] = {}
        if sample_weight is not None:
            cccv_fit_kwargs["sample_weight"] = sample_weight
        return calibrated.fit(x_train, y_train, **cccv_fit_kwargs)

    return estimator.fit(x_train, y_train, **direct_fit_kwargs)


def _fold_feature_spec(
    train_frame: pd.DataFrame,
    base_spec: FeatureSpec,
    *,
    use_median_imputation: bool,
) -> FeatureSpec:
    """Return a ``FeatureSpec`` whose schema (``ordered_keys`` +
    ``family_one_hot_keys``) matches ``base_spec`` but whose
    ``default_values`` are recomputed using *only* the training fold.

    Bug #16: ``build_feature_spec`` fitted medians on the full dataset
    before train/test split, leaking holdout statistics into the
    imputation prior. Held-out Brier was correspondingly optimistic
    and the promotion gate sat downstream of that leak. The train-
    fold-only medians here keep the test fold genuinely held out.

    The schema (column set) is taken from ``base_spec`` so train and
    test vectors stay aligned. Only the imputation prior changes.
    Skip-list semantics from ``build_feature_spec`` are preserved:
    completeness markers and binary-only keys keep the 0.0 prior so
    the present/absent signal stays meaningful.
    """
    if not use_median_imputation:
        return base_spec
    accumulator = _collect_feature_values(train_frame, list(base_spec.ordered_keys))
    binary_only_keys = {
        key for key, values in accumulator.items()
        if values and all(v in (0.0, 1.0) for v in values)
    }
    skip_set = set(ADVANCED_COMPLETENESS_MARKERS) | binary_only_keys
    medians = _medians_from_accumulator(accumulator)
    new_defaults = {key: 0.0 for key in base_spec.ordered_keys}
    for key, median_value in medians.items():
        if key in skip_set:
            continue
        new_defaults[key] = median_value
    return FeatureSpec(
        version=base_spec.version,
        ordered_keys=base_spec.ordered_keys,
        default_values=new_defaults,
        family_one_hot_keys=base_spec.family_one_hot_keys,
    )


def _evaluate_candidates(
    frame: pd.DataFrame,
    feature_spec: FeatureSpec,
    *,
    sample_weight: np.ndarray | None = None,
    use_median_imputation: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Pick a candidate by held-out brier across three splits.

    ``sample_weight`` (when provided) is applied to the *training* slice of
    each split via ``_fit_estimator``. Held-out evaluation stays unweighted —
    we want the brier on the natural row distribution, not a weighted view.
    Pipeline candidates that don't accept ``sample_weight`` silently fall
    back to uniform weights (see ``_fit_estimator``).

    Bug #16: imputation medians are recomputed inside each train fold
    via ``_fold_feature_spec``. The vectors that go into both train
    and test are produced with the train-only prior, so the held-out
    Brier reflects genuine generalization rather than a leak from
    the test rows' values back into the imputation defaults.
    """
    y = frame["target"].to_numpy()
    player_train, player_test = _split_by_group(frame, "player_group")
    event_train, event_test = _split_by_group(frame, "event_group")
    time_train, time_test = _split_by_time(frame)

    def _train_weight(idx: np.ndarray) -> np.ndarray | None:
        return None if sample_weight is None else sample_weight[idx]

    def _fold_matrices(train_idx: np.ndarray, test_idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        fold_spec = _fold_feature_spec(
            frame.iloc[train_idx],
            feature_spec,
            use_median_imputation=use_median_imputation,
        )
        return _matrix(frame.iloc[train_idx], fold_spec), _matrix(frame.iloc[test_idx], fold_spec)

    x_player_train, x_player_test = _fold_matrices(player_train, player_test)
    x_event_train, x_event_test = _fold_matrices(event_train, event_test)
    x_time_train, x_time_test = _fold_matrices(time_train, time_test)

    evaluations: dict[str, Any] = {}
    for name, estimator in _candidate_estimators(len(frame)).items():
        fitted = _fit_estimator(estimator, x_player_train, y[player_train], sample_weight=_train_weight(player_train))
        player_prob = fitted.predict_proba(x_player_test)[:, 1]
        event_fitted = _fit_estimator(estimator, x_event_train, y[event_train], sample_weight=_train_weight(event_train))
        event_prob = event_fitted.predict_proba(x_event_test)[:, 1]
        time_fitted = _fit_estimator(estimator, x_time_train, y[time_train], sample_weight=_train_weight(time_train))
        time_prob = time_fitted.predict_proba(x_time_test)[:, 1]
        evaluations[name] = {
            "player_group": _metrics_for_predictions(frame, player_test, player_prob),
            "event_group": _metrics_for_predictions(frame, event_test, event_prob),
            "time": _metrics_for_predictions(frame, time_test, time_prob),
        }
    winner = min(evaluations, key=lambda item: evaluations[item]["player_group"]["brier"])
    return winner, evaluations


def train_and_package(
    frame: pd.DataFrame | None = None,
    *,
    database_url: str | None = None,
    artifact_root: str | Path = "artifacts",
    manifest_out: str | Path | None = "manifests/current.json",
    serve_family_key: str | None = None,
    serve_family_keys: tuple[str, ...] | list[str] | None = None,
    feature_set_version: str = "public-feature-set-v2",
    model_version: str | None = None,
    dry_run: bool = False,
    # PR 3d additions — opt-in but ON by default for v2:
    use_median_imputation: bool = True,
    advanced_sample_weight: float = 3.0,
    advanced_only_threshold: int = 2000,
    advanced_only: bool | None = None,
    promotion_baseline_brier: float | None = None,
) -> TrainingResult:
    """Train and package a model.

    PR 3d behaviour additions:
      - ``use_median_imputation``: when True, FeatureSpec.default_values are
        the median of present values (per key) instead of 0.0. This stops
        rows that simply lack advanced data from being treated as "all
        zeros".
      - ``advanced_sample_weight``: rows whose features include any
        completeness marker (``advanced_data_complete`` etc.) are weighted
        ``advanced_sample_weight``x during fit. Default 3.0.
      - ``advanced_only_threshold``: if any single family has at least this
        many advanced-complete rows, training auto-filters to that subset.
        Median imputation still runs because a row "advanced-complete" by
        marker doesn't guarantee every advanced feature key is populated;
        weighting drops to uniform since every retained row is high-signal.
        Pass ``advanced_only=True`` to force this mode even below threshold.
      - ``promotion_baseline_brier``: when provided, the manifest
        ``serving_mode`` flips from ``"shadow"`` to ``"ml"`` (the runtime
        sentinel that activates the model) only if the candidate's
        time-split Brier is **strictly less than** the baseline; ties
        keep ``"shadow"``.
    """
    if serve_family_keys is not None and serve_family_key is not None:
        raise ValueError("Pass either serve_family_keys or serve_family_key, not both.")
    if serve_family_keys is not None:
        resolved_serve_keys: tuple[str, ...] = tuple(dict.fromkeys(serve_family_keys))
    elif serve_family_key is not None:
        resolved_serve_keys = (serve_family_key,)
    else:
        resolved_serve_keys = ("mlb_props", "nba_props", "mlb_singles", "nba_singles")
    if not resolved_serve_keys:
        raise ValueError("serve_family_keys must contain at least one family key.")

    dataset = frame if frame is not None else load_settled_predictions(database_url)
    if dataset is None or dataset.empty:
        raise ValueError("No settled prediction rows available for training.")
    if dataset["target"].nunique() < 2:
        raise ValueError("Training requires both won and lost outcomes.")

    completeness_counts = _advanced_completeness_counts(dataset)
    family_max_count = max((count for key, count in completeness_counts.items() if key != "__total__"), default=0)
    auto_advanced_only = family_max_count >= advanced_only_threshold
    advanced_only_active = bool(advanced_only) if advanced_only is not None else auto_advanced_only

    if advanced_only_active:
        mask = _advanced_completeness_mask(dataset)
        if mask.sum() == 0:
            raise ValueError("advanced_only requested but no advanced-complete rows found")
        dataset = dataset[mask].reset_index(drop=True)
        if dataset["target"].nunique() < 2:
            raise ValueError("advanced-only filter dropped a target class.")

    model_version = model_version or datetime.now(timezone.utc).date().isoformat()
    # PR 3d — keep median imputation on even in advanced-only mode. The
    # completeness markers we filter on (e.g. ``advanced_data_complete``)
    # only assert that the corresponding emitter fired; per-key coverage
    # within advanced features is still sparse, and sparse rows benefit
    # from medians as much as the mixed-mode dataset does.
    feature_spec = build_feature_spec(
        dataset,
        version=feature_set_version,
        mode="residual",
        use_median_imputation=use_median_imputation,
    )

    # Build sample weights up-front so candidate evaluation and the final
    # refit see the same weighted view of the data. In advanced-only mode
    # every row carries a completeness marker, so weighting collapses to
    # uniform — skip the work.
    sample_weights: np.ndarray | None = None
    if not advanced_only_active and advanced_sample_weight != 1.0:
        sample_weights = _build_sample_weights(dataset, advanced_weight=advanced_sample_weight)

    winner, evaluations = _evaluate_candidates(
        dataset,
        feature_spec,
        sample_weight=sample_weights,
        use_median_imputation=use_median_imputation,
    )
    walk_forward = walk_forward_evaluation(
        dataset,
        feature_spec,
        sample_weight=sample_weights,
        use_median_imputation=use_median_imputation,
        family_keys=resolved_serve_keys,
    )
    x = _matrix(dataset, feature_spec)
    y = dataset["target"].to_numpy()
    final_estimator = _fit_estimator(
        _candidate_estimators(len(dataset))[winner],
        x,
        y,
        sample_weight=sample_weights,
    )

    # Bug #20 — promotion is decided per ``serves_family_key`` against
    # the *worst-fold* walk-forward Brier. A family must accumulate
    # ≥``MIN_WALK_FORWARD_VALID_FOLDS`` test folds (each with
    # ≥``MIN_WALK_FORWARD_ROWS_PER_FOLD`` rows of that family) before it
    # is even eligible. The trained model is global, so the family's
    # walk-forward uses the global training slice and a family-restricted
    # test slice — measuring "how well does the global model serve this
    # family over time." Sparse families fall back to shadow even when a
    # sibling family is rich enough to clear the gate on its own.
    #
    # NOTE on ``serving_mode`` values: ``apps/api/app/services/ml/runtime.py``
    # accepts only ``"shadow"`` and ``"ml"`` from a manifest. ``"serving"``
    # is rejected and falls back to auto-shadow, which would silently
    # no-op the entire promotion path. The top-level ``serving_mode``
    # flips to ``"ml"`` when at least one served family promotes; each
    # ``ModelArtifact`` carries its own per-family ``mode``.
    baseline_value: float | None = (
        float(promotion_baseline_brier) if promotion_baseline_brier is not None else None
    )

    def _decide_family(family_key: str) -> dict[str, Any]:
        family_block = walk_forward["per_family"].get(family_key, {})
        family_candidates = family_block.get("candidates", {})
        winner_block = family_candidates.get(winner)
        candidate_brier_value: float | None = (
            float(winner_block["worst_fold_brier"]) if winner_block is not None else None
        )
        family_insufficient = bool(family_block.get("insufficient_history", True)) or candidate_brier_value is None
        decision: dict[str, Any] = {
            "candidate_brier": candidate_brier_value,
            "promoted": False,
            "insufficient_history": family_insufficient,
            "fold_count": int(family_block.get("fold_count", 0)),
            "rows_per_fold": list(family_block.get("rows_per_fold", [])),
        }
        if baseline_value is None:
            return decision
        if family_insufficient:
            decision["reason"] = "insufficient_history"
            return decision
        decision["promoted"] = candidate_brier_value < baseline_value
        return decision

    per_family_decisions: dict[str, dict[str, Any]] = {
        family_key: _decide_family(family_key) for family_key in resolved_serve_keys
    }
    any_family_promoted = any(decision["promoted"] for decision in per_family_decisions.values())
    serving_mode = "ml" if any_family_promoted else "shadow"

    # ``candidate_brier`` reported here is the worst (max) per-family
    # value across the served families. For a single-family training run
    # this matches the gate's actual comparand exactly, so the existing
    # tie workflow (``baseline = previous candidate_brier``) keeps
    # working — codex round 3 caught that reporting the global worst-fold
    # could be strictly less than the family value used for promotion,
    # so a tie at the top level could still promote at the family level.
    # ``insufficient_history`` at the top level mirrors "no served family
    # cleared the floor" rather than the global eval, for the same
    # reason: the global eval is informational, the gate is per-family.
    valid_family_briers = [
        decision["candidate_brier"]
        for decision in per_family_decisions.values()
        if decision["candidate_brier"] is not None
    ]
    all_families_insufficient = all(
        decision["insufficient_history"] for decision in per_family_decisions.values()
    )
    aggregate_candidate_brier: float | None = (
        max(valid_family_briers) if valid_family_briers else None
    )
    promotion_decision: dict[str, Any] = {
        "baseline_brier": baseline_value,
        "candidate_brier": aggregate_candidate_brier,
        "promoted": any_family_promoted,
        "insufficient_history": all_families_insufficient or aggregate_candidate_brier is None,
        "fold_count": int(walk_forward["fold_count"]),
        "fold_window_days": walk_forward["fold_window_days"],
        "min_rows_per_fold": int(walk_forward["min_rows_per_fold"]),
        "min_valid_folds": int(walk_forward["min_valid_folds"]),
        "metric": "worst_fold_brier",
        "candidate_brier_aggregation": "max_over_served_families",
        "per_family": per_family_decisions,
    }
    if promotion_decision["insufficient_history"]:
        promotion_decision["reason"] = "insufficient_history"

    model_name = f"global_{winner}_residual"
    timestamp = model_version.replace("-", "")
    artifact_dir = Path(artifact_root) / f"global_v1_{timestamp}"
    manifest_path = Path(manifest_out) if manifest_out is not None else None
    metadata = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_rows": int(len(dataset)),
        "dropped_pushes": True,
        "dedupe_markets": True,
        "model_name": model_name,
        "winner": winner,
        "feature_mode": "residual_calibration",
        "metrics": evaluations,
        "hyperparameters": {"candidates": list(_candidate_estimators(len(dataset)).keys())},
        # PR 3d / bug #16 — observability for the v2 training pipeline.
        # ``feature_medians`` contains medians for EVERY feature key, not
        # just advanced ones (it mirrors ``feature_spec.default_values``,
        # which is computed from the full dataset for the final refit /
        # serving model — there's no holdout in the served artifact).
        # ``evaluation_imputation_caveat`` previously warned that the
        # held-out Brier was mildly optimistic because of full-dataset
        # medians; bug #16 fixed that by recomputing medians inside each
        # train fold (see ``_fold_feature_spec``), so the caveat now
        # records the fix rather than the leak.
        "advanced_completeness_counts": completeness_counts,
        "advanced_only_threshold": advanced_only_threshold,
        "advanced_only_active": advanced_only_active,
        "advanced_sample_weight": float(advanced_sample_weight) if not advanced_only_active else 1.0,
        "use_median_imputation": bool(use_median_imputation),
        "feature_medians": dict(feature_spec.default_values) if use_median_imputation else {},
        "evaluation_imputation_caveat": (
            "candidate brier reflects train-fold medians only (bug #16); "
            "feature_medians above are the full-dataset prior used by the "
            "final refit / serving model"
            if use_median_imputation else None
        ),
        "walk_forward_evaluation": walk_forward,
        "promotion": promotion_decision,
    }
    if not dry_run:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(final_estimator, artifact_dir / "model.joblib")
        (artifact_dir / "feature_spec.json").write_text(json.dumps(feature_spec.to_dict(), indent=2), encoding="utf-8")
        (artifact_dir / "training_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        if manifest_path is not None:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            relative_artifact = Path(os.path.relpath(artifact_dir.resolve(), manifest_path.parent.resolve()))
            manifest = ModelManifest(
                version=model_version,
                serving_mode=serving_mode,
                families=[
                    ModelArtifact(
                        family_key="global_v1",
                        serves_family_key=serves_key,
                        model_name=model_name,
                        model_version=model_version,
                        calibration_version="calibrated_v1",
                        feature_set_version=feature_set_version,
                        artifact_path=str(relative_artifact),
                        # Per-family mode: a family that doesn't clear its
                        # own walk-forward floor stays in shadow even when
                        # a sibling family promotes the global manifest.
                        mode="ml" if per_family_decisions[serves_key]["promoted"] else "shadow",
                        metadata={
                            "behavior": "sklearn_predict_proba",
                            "feature_mode": "residual_calibration",
                            # target_type pins what predict_proba[:,1] represents.
                            # "yes_won" means the model emits P(YES wins); serving
                            # paths in ml/runtime.py and ml/shadow.py rely on this.
                            # Manifests missing this field were trained against the
                            # selected-side-won target (bug #2) and should be retrained.
                            "target_type": "yes_won",
                        },
                    )
                    for serves_key in resolved_serve_keys
                ],
                metadata={"source": "apps/ml training pipeline"},
            )
            manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    return TrainingResult(
        artifact_dir=artifact_dir,
        manifest_path=manifest_path,
        model_name=model_name,
        feature_spec=feature_spec,
        metrics=metadata,
    )
