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

    Kept as a thin wrapper around the new ``_collect_feature_values`` /
    ``_medians_from_accumulator`` pair for backward compatibility with the
    public test surface in ``test_pr3d_training_v2``.
    """
    accumulator: dict[str, list[float]] = {key: [] for key in keys}
    for features in frame["features"]:
        feats = dict(features or {})
        for key in keys:
            value = _safe_float(feats.get(key))
            if value is not None:
                accumulator[key].append(value)
    medians: dict[str, float] = {}
    for key, values in accumulator.items():
        if not values:
            medians[key] = 0.0
            continue
        medians[key] = float(np.median(values))
    return medians


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


def _metrics_for_predictions(frame: pd.DataFrame, indices: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    selected = frame.loc[indices]
    target = selected["target"].to_numpy()
    clipped = np.clip(probabilities, 1e-6, 1 - 1e-6)
    edge = clipped - selected["suggested_price"].fillna(0.5).astype(float).to_numpy()
    top_n = max(int(np.ceil(len(selected) * 0.10)), 1)
    top_idx = np.argsort(edge)[-top_n:]
    pnl = selected["realized_pnl"].fillna(selected["target"].map({1: 1.0, 0: -1.0})).astype(float).to_numpy()
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

    Sample weights are only forwarded to estimators that natively accept
    them via ``fit(sample_weight=...)``. Pipeline-wrapped estimators (our
    LR candidate is a ``StandardScaler → LogisticRegression`` pipeline)
    require ``<step>__sample_weight`` routing which doesn't compose
    cleanly through CalibratedClassifierCV; for those we silently fall
    back to uniform weights (the handoff says "if supported"). When
    ``sample_weight`` is None, behaviour is identical to the pre-PR3d
    code path.
    """
    from sklearn.pipeline import Pipeline

    class_counts = np.bincount(y_train, minlength=2)
    cv = int(min(3, class_counts.min()))
    fit_kwargs: dict[str, Any] = {}
    weight_supported = sample_weight is not None and not isinstance(estimator, Pipeline)
    if weight_supported:
        fit_kwargs["sample_weight"] = sample_weight
    if cv >= 2 and len(y_train) >= 500:
        method = "isotonic" if len(y_train) >= 500 else "sigmoid"
        calibrated = CalibratedClassifierCV(estimator=estimator, method=method, cv=cv)
        return calibrated.fit(x_train, y_train, **fit_kwargs)
    return estimator.fit(x_train, y_train, **fit_kwargs)


def _evaluate_candidates(
    frame: pd.DataFrame,
    feature_spec: FeatureSpec,
    *,
    sample_weight: np.ndarray | None = None,
) -> tuple[str, dict[str, Any]]:
    """Pick a candidate by held-out brier across three splits.

    ``sample_weight`` (when provided) is applied to the *training* slice of
    each split via ``_fit_estimator``. Held-out evaluation stays unweighted —
    we want the brier on the natural row distribution, not a weighted view.
    Pipeline candidates that don't accept ``sample_weight`` silently fall
    back to uniform weights (see ``_fit_estimator``).
    """
    x = _matrix(frame, feature_spec)
    y = frame["target"].to_numpy()
    player_train, player_test = _split_by_group(frame, "player_group")
    event_train, event_test = _split_by_group(frame, "event_group")
    time_train, time_test = _split_by_time(frame)

    def _train_weight(idx: np.ndarray) -> np.ndarray | None:
        return None if sample_weight is None else sample_weight[idx]

    evaluations: dict[str, Any] = {}
    for name, estimator in _candidate_estimators(len(frame)).items():
        fitted = _fit_estimator(estimator, x[player_train], y[player_train], sample_weight=_train_weight(player_train))
        player_prob = fitted.predict_proba(x[player_test])[:, 1]
        event_fitted = _fit_estimator(estimator, x[event_train], y[event_train], sample_weight=_train_weight(event_train))
        event_prob = event_fitted.predict_proba(x[event_test])[:, 1]
        time_fitted = _fit_estimator(estimator, x[time_train], y[time_train], sample_weight=_train_weight(time_train))
        time_prob = time_fitted.predict_proba(x[time_test])[:, 1]
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
    serve_family_key: str = "mlb_props",
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

    winner, evaluations = _evaluate_candidates(dataset, feature_spec, sample_weight=sample_weights)
    x = _matrix(dataset, feature_spec)
    y = dataset["target"].to_numpy()
    final_estimator = _fit_estimator(
        _candidate_estimators(len(dataset))[winner],
        x,
        y,
        sample_weight=sample_weights,
    )

    # PR 3d — promotion gate. If a baseline brier is supplied, only flip
    # serving_mode to "ml" (the runtime sentinel that activates the model)
    # when v2 strictly beats the baseline on the held-out time slice; ties
    # stay in shadow so the API runtime keeps using the heuristic /
    # previous serving model.
    #
    # NOTE on ``serving_mode`` values: ``apps/api/app/services/ml/runtime.py``
    # accepts only ``"shadow"`` and ``"ml"`` from the manifest. ``"serving"``
    # is rejected and falls back to auto-shadow, which would silently no-op
    # the entire promotion path.
    time_brier = float(evaluations[winner]["time"]["brier"])
    if promotion_baseline_brier is None:
        serving_mode = "shadow"
        promotion_decision: dict[str, Any] = {"baseline_brier": None, "candidate_brier": time_brier, "promoted": False}
    else:
        promoted = time_brier < float(promotion_baseline_brier)
        serving_mode = "ml" if promoted else "shadow"
        promotion_decision = {
            "baseline_brier": float(promotion_baseline_brier),
            "candidate_brier": time_brier,
            "promoted": promoted,
        }

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
        # PR 3d — observability for the v2 training pipeline. Note:
        # ``feature_medians`` contains medians for EVERY feature key, not
        # just advanced ones (it mirrors ``feature_spec.default_values``).
        # ``evaluation_imputation_caveat`` records that median defaults are
        # computed on the full dataset before train/test splits in
        # ``_evaluate_candidates``, so candidate brier is mildly optimistic
        # for keys missing on test rows. Magnitude is ~1/N per row; the
        # promotion gate's ``<`` strictness gives some headroom.
        "advanced_completeness_counts": completeness_counts,
        "advanced_only_threshold": advanced_only_threshold,
        "advanced_only_active": advanced_only_active,
        "advanced_sample_weight": float(advanced_sample_weight) if not advanced_only_active else 1.0,
        "use_median_imputation": bool(use_median_imputation),
        "feature_medians": dict(feature_spec.default_values) if use_median_imputation else {},
        "evaluation_imputation_caveat": (
            "median defaults computed on full dataset; held-out brier "
            "for rows with missing keys is mildly optimistic"
            if use_median_imputation else None
        ),
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
                        serves_family_key=serve_family_key,
                        model_name=model_name,
                        model_version=model_version,
                        calibration_version="calibrated_v1",
                        feature_set_version=feature_set_version,
                        artifact_path=str(relative_artifact),
                        mode=serving_mode,
                        metadata={"behavior": "sklearn_predict_proba", "feature_mode": "residual_calibration"},
                    )
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
