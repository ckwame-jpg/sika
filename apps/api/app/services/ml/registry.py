from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings


@dataclass(slots=True)
class ModelManifestFamily:
    family_key: str
    model_name: str
    model_version: str
    serves_family_key: str | None = None
    calibration_version: str | None = None
    feature_set_version: str | None = None
    artifact_path: str | None = None
    mode: str = "shadow"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelManifest:
    version: str
    serving_mode: str
    source_path: str | None = None
    families: list[ModelManifestFamily] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _default_manifest_path() -> Path | None:
    bundled_path = Path(__file__).resolve().parents[4] / "ml" / "manifests" / "current.json"
    return bundled_path if bundled_path.exists() else None


def load_model_manifest(manifest_path: str | None = None) -> ModelManifest | None:
    settings = get_settings()
    resolved_path = (manifest_path if manifest_path is not None else settings.ml_manifest_path).strip()
    if not resolved_path:
        default_path = _default_manifest_path()
        if default_path is None:
            return None
        path = default_path
    else:
        path = Path(resolved_path)

    if not path.exists():
        return None

    payload = json.loads(path.read_text(encoding="utf-8"))
    families = [
        ModelManifestFamily(
            family_key=str(item.get("family_key") or ""),
            serves_family_key=str(item.get("serves_family_key") or item.get("metadata", {}).get("serves_family_key") or "").strip() or None,
            model_name=str(item.get("model_name") or ""),
            model_version=str(item.get("model_version") or ""),
            calibration_version=item.get("calibration_version"),
            feature_set_version=item.get("feature_set_version"),
            artifact_path=item.get("artifact_path"),
            mode=str(item.get("mode") or "shadow"),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("families") or []
        if item.get("family_key") and item.get("model_name") and item.get("model_version")
    ]
    return ModelManifest(
        version=str(payload.get("version") or "unversioned"),
        serving_mode=str(payload.get("serving_mode") or settings.ml_serving_mode),
        source_path=str(path),
        families=families,
        metadata=dict(payload.get("metadata") or {}),
    )
