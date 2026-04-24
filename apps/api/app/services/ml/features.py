from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    version: str
    ordered_keys: list[str]
    default_values: dict[str, float]
    family_one_hot_keys: list[str]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FeatureSpec":
        return cls(
            version=str(payload.get("version") or ""),
            ordered_keys=[str(item) for item in payload.get("ordered_keys") or []],
            default_values={
                str(key): _coerce_float(value, default=0.0)
                for key, value in dict(payload.get("default_values") or {}).items()
            },
            family_one_hot_keys=[str(item) for item in payload.get("family_one_hot_keys") or []],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "ordered_keys": list(self.ordered_keys),
            "default_values": dict(self.default_values),
            "family_one_hot_keys": list(self.family_one_hot_keys),
        }


def _coerce_float(value: Any, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if np.isfinite(result) else default


def vectorize(features_dict: dict[str, Any], feature_spec: FeatureSpec) -> np.ndarray:
    values: list[float] = []
    for key in feature_spec.ordered_keys:
        values.append(
            _coerce_float(
                features_dict.get(key),
                default=feature_spec.default_values.get(key, 0.0),
            )
        )

    family_key = str(features_dict.get("family_key") or "")
    for expected_family in feature_spec.family_one_hot_keys:
        values.append(1.0 if family_key == expected_family else 0.0)

    return np.asarray(values, dtype=np.float64)
