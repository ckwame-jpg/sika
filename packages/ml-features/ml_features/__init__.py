# Bug #29 — single source of truth for the train/serve feature contract.
# Both ``apps/ml/ml/training.py`` and ``apps/api/app/services/ml/runtime.py``
# import ``FeatureSpec`` + ``vectorize`` from here, so a training artifact
# and the API serving path agree byte-for-byte on how raw feature dicts
# map to model input vectors. Previously these symbols lived in two
# copies of ``features.py`` (one per app) — that duplication is the
# train/serve skew risk this package retires.
from ml_features.families import (
    DEFAULT_SERVE_FAMILY_KEYS,
    SUPPORTED_SPORTS,
    single_family_key,
    sport_one_hot,
)
from ml_features.monotonic import (
    MONOTONIC_CONSTRAINTS_BY_FAMILY,
    build_monotonic_cst,
    has_any_constraint,
    monotonic_constraints_for,
)
from ml_features.spec import FeatureSpec, vectorize

__all__ = [
    "DEFAULT_SERVE_FAMILY_KEYS",
    "FeatureSpec",
    "MONOTONIC_CONSTRAINTS_BY_FAMILY",
    "SUPPORTED_SPORTS",
    "build_monotonic_cst",
    "has_any_constraint",
    "monotonic_constraints_for",
    "single_family_key",
    "sport_one_hot",
    "vectorize",
]
