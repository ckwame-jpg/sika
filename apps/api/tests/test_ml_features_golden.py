from __future__ import annotations

from pathlib import Path

import numpy as np

from ml_features import FeatureSpec, vectorize


# Bug #29 — the original test in this file dynamically loaded
# ``apps/api/app/services/ml/features.py`` and ``apps/ml/ml/features.py``
# and asserted ``vectorize`` produced identical output. That parity is
# now structurally enforced: both apps import from the shared
# ``ml_features`` package, so the duplicate sources don't exist. What
# remains here is (a) a golden-payload smoke test pinning the vectorizer
# contract, and (b) a singleton check that fails CI if anyone
# accidentally re-creates a local ``features.py`` inside ``apps/``.


def test_vectorize_golden_payload_contract():
    """Pins the vectorizer's coercion + family-one-hot contract.
    Mutating this output is a model-breaking change."""
    spec = FeatureSpec.from_dict(
        {
            "version": "golden-v1",
            "ordered_keys": ["recent_average", "threshold", "latest_log_days_ago"],
            "default_values": {"threshold": 6.0, "latest_log_days_ago": 4.0},
            "family_one_hot_keys": ["nba_props", "mlb_props"],
        }
    )
    features = {
        "family_key": "mlb_props",
        "recent_average": "7.25",
        "threshold": float("nan"),
    }
    vector = vectorize(features, spec)
    np.testing.assert_array_equal(vector, np.asarray([7.25, 6.0, 4.0, 0.0, 1.0]))


def test_no_local_features_py_inside_apps():
    """Bug #29 backstop: the duplicate ``features.py`` modules in
    ``apps/api/app/services/ml/`` and ``apps/ml/ml/`` must stay
    deleted. If anyone re-creates either file the train/serve contract
    can silently drift; this test catches the re-introduction in CI
    before a divergent build ships.
    """
    repo_root = Path(__file__).resolve().parents[3]
    apps_root = repo_root / "apps"
    forbidden = [
        path for path in apps_root.rglob("features.py")
        if "node_modules" not in path.parts
    ]
    assert not forbidden, (
        "Found features.py inside apps/: "
        f"{[str(p.relative_to(repo_root)) for p in forbidden]}. "
        "Both apps must import from the shared ``ml_features`` package "
        "at ``packages/ml-features/``; the local copies were retired in "
        "bug #29 to remove the train/serve skew risk."
    )


def test_ml_features_imported_from_shared_package():
    """Direct positive check — confirm we're importing from
    ``packages/ml-features/`` rather than a shadow location."""
    import ml_features
    module_path = Path(ml_features.__file__).resolve()
    assert "packages/ml-features" in str(module_path), (
        f"ml_features must come from the shared package at "
        f"packages/ml-features/, got {module_path}"
    )
