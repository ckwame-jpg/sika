from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib

from app.services.ml.features import FeatureSpec


@dataclass(frozen=True, slots=True)
class SklearnArtifact:
    artifact_dir: Path
    pipeline: Any
    feature_spec: FeatureSpec
    training_metadata: dict[str, Any]


_CACHE: dict[tuple[str, float], SklearnArtifact] = {}
_LOCK = threading.Lock()


def _artifact_cache_key(abs_dir: Path) -> tuple[str, float]:
    mtimes = []
    for filename in ("model.joblib", "feature_spec.json", "training_metadata.json"):
        mtimes.append((abs_dir / filename).stat().st_mtime)
    return (str(abs_dir), max(mtimes))


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def load_sklearn_artifact(abs_dir: str | Path) -> SklearnArtifact:
    artifact_dir = Path(abs_dir).resolve()
    key = _artifact_cache_key(artifact_dir)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

    feature_spec = FeatureSpec.from_dict(json.loads((artifact_dir / "feature_spec.json").read_text(encoding="utf-8")))
    training_metadata = json.loads((artifact_dir / "training_metadata.json").read_text(encoding="utf-8"))
    pipeline = joblib.load(artifact_dir / "model.joblib")
    artifact = SklearnArtifact(
        artifact_dir=artifact_dir,
        pipeline=pipeline,
        feature_spec=feature_spec,
        training_metadata=dict(training_metadata or {}),
    )
    with _LOCK:
        _CACHE[key] = artifact
    return artifact
