from __future__ import annotations

import numpy as np

from ml.features import FeatureSpec, vectorize


def test_vectorize_uses_defaults_and_family_one_hot():
    spec = FeatureSpec(
        version="test-v1",
        ordered_keys=["recent_average", "threshold", "missing_numeric"],
        default_values={"missing_numeric": 3.5},
        family_one_hot_keys=["nba_props", "mlb_props"],
    )

    vector = vectorize(
        {
            "family_key": "mlb_props",
            "recent_average": "8.25",
            "threshold": 7,
            "missing_numeric": float("nan"),
        },
        spec,
    )

    np.testing.assert_array_equal(vector, np.asarray([8.25, 7.0, 3.5, 0.0, 1.0]))
