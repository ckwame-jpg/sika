from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ModelArtifact:
    family_key: str
    model_name: str
    model_version: str
    calibration_version: str
    feature_set_version: str
    artifact_path: str
    mode: str = "shadow"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelManifest:
    version: str
    serving_mode: str
    families: list[ModelArtifact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "serving_mode": self.serving_mode,
            "families": [asdict(family) for family in self.families],
            "metadata": dict(self.metadata),
        }
