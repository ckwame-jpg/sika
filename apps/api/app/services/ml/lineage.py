from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelLineage:
    model_name: str
    model_version: str | None = None
    calibration_version: str | None = None
    feature_set_version: str | None = None
    model_metadata: dict[str, Any] | None = None

    def kwargs(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "calibration_version": self.calibration_version,
            "feature_set_version": self.feature_set_version,
            "model_metadata": dict(self.model_metadata or {}),
        }


HEURISTIC_SINGLE_MODEL = ModelLineage(
    model_name="heuristic-v1",
    model_version="heuristic-v1",
    calibration_version="heuristic-manual-v1",
    feature_set_version="heuristic-feature-set-v1",
    model_metadata={
        "engine": "heuristic",
        "scope": "single",
        "serving_mode": "heuristic",
    },
)

HEURISTIC_PARLAY_MODEL = ModelLineage(
    model_name="heuristic-parlay-combiner-v1",
    model_version="heuristic-parlay-combiner-v1",
    calibration_version="heuristic-parlay-combiner-v1",
    feature_set_version="heuristic-parlay-feature-set-v1",
    model_metadata={
        "engine": "heuristic_combiner",
        "scope": "parlay",
        "serving_mode": "heuristic",
    },
)
