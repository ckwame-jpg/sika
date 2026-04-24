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
    return FeatureSpec(
        version=version,
        ordered_keys=sorted(keys),
        default_values=default_values,
        family_one_hot_keys=family_keys,
    )


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


def _fit_estimator(estimator, x_train: np.ndarray, y_train: np.ndarray):
    class_counts = np.bincount(y_train, minlength=2)
    cv = int(min(3, class_counts.min()))
    if cv >= 2 and len(y_train) >= 500:
        method = "isotonic" if len(y_train) >= 500 else "sigmoid"
        calibrated = CalibratedClassifierCV(estimator=estimator, method=method, cv=cv)
        return calibrated.fit(x_train, y_train)
    return estimator.fit(x_train, y_train)


def _evaluate_candidates(frame: pd.DataFrame, feature_spec: FeatureSpec) -> tuple[str, dict[str, Any]]:
    x = _matrix(frame, feature_spec)
    y = frame["target"].to_numpy()
    player_train, player_test = _split_by_group(frame, "player_group")
    event_train, event_test = _split_by_group(frame, "event_group")
    time_train, time_test = _split_by_time(frame)
    evaluations: dict[str, Any] = {}
    for name, estimator in _candidate_estimators(len(frame)).items():
        fitted = _fit_estimator(estimator, x[player_train], y[player_train])
        player_prob = fitted.predict_proba(x[player_test])[:, 1]
        event_fitted = _fit_estimator(estimator, x[event_train], y[event_train])
        event_prob = event_fitted.predict_proba(x[event_test])[:, 1]
        time_fitted = _fit_estimator(estimator, x[time_train], y[time_train])
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
    feature_set_version: str = "public-feature-set-v1",
    model_version: str | None = None,
    dry_run: bool = False,
) -> TrainingResult:
    dataset = frame if frame is not None else load_settled_predictions(database_url)
    if dataset is None or dataset.empty:
        raise ValueError("No settled prediction rows available for training.")
    if dataset["target"].nunique() < 2:
        raise ValueError("Training requires both won and lost outcomes.")

    model_version = model_version or datetime.now(timezone.utc).date().isoformat()
    feature_spec = build_feature_spec(dataset, version=feature_set_version, mode="residual")
    winner, evaluations = _evaluate_candidates(dataset, feature_spec)
    x = _matrix(dataset, feature_spec)
    y = dataset["target"].to_numpy()
    final_estimator = _fit_estimator(_candidate_estimators(len(dataset))[winner], x, y)
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
                serving_mode="shadow",
                families=[
                    ModelArtifact(
                        family_key="global_v1",
                        serves_family_key=serve_family_key,
                        model_name=model_name,
                        model_version=model_version,
                        calibration_version="calibrated_v1",
                        feature_set_version=feature_set_version,
                        artifact_path=str(relative_artifact),
                        mode="shadow",
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
