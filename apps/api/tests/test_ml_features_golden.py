from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from app.services.ml.features import FeatureSpec, vectorize


def _load_apps_ml_features_module():
    repo_root = Path(__file__).resolve().parents[3]
    feature_path = repo_root / "apps" / "ml" / "ml" / "features.py"
    spec = importlib.util.spec_from_file_location("apps_ml_features_golden", feature_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_api_and_apps_ml_feature_vectorizers_match():
    payload = {
        "version": "golden-v1",
        "ordered_keys": ["recent_average", "threshold", "latest_log_days_ago"],
        "default_values": {"threshold": 6.0, "latest_log_days_ago": 4.0},
        "family_one_hot_keys": ["nba_props", "mlb_props"],
    }
    features = {
        "family_key": "mlb_props",
        "recent_average": "7.25",
        "threshold": float("nan"),
    }
    apps_ml_features = _load_apps_ml_features_module()

    api_vector = vectorize(features, FeatureSpec.from_dict(payload))
    ml_vector = apps_ml_features.vectorize(features, apps_ml_features.FeatureSpec.from_dict(payload))

    np.testing.assert_array_equal(api_vector, ml_vector)
