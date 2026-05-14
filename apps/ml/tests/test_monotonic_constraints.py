"""Tests for Smarter #19 — per-family monotonic constraints in training.

Covers:
- The shared registry + lookup function in ``ml_features.monotonic``.
- ``build_monotonic_cst`` aligns the array to ``vectorize``'s feature
  ordering (ordered_keys + family_one_hot_keys).
- ``has_any_constraint`` skip path so the pre-Smarter-#19 candidate
  estimator construction is unchanged when no constraints are tagged.
- HGBC training honors the constraint when one is supplied (smoke
  test on a synthetic dataset).

The registry is **empty by design** in this PR (mechanism only). The
"populated" tests monkeypatch the registry so they exercise the
override path without committing a real default tag.
"""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier

from ml_features import (
    FeatureSpec,
    build_monotonic_cst,
    has_any_constraint,
    monotonic_constraints_for,
)
from ml_features import monotonic as monotonic_mod


def _spec(*, ordered_keys: list[str], family_one_hot_keys: list[str] | None = None) -> FeatureSpec:
    return FeatureSpec(
        version="test-v1",
        ordered_keys=ordered_keys,
        default_values={k: 0.0 for k in ordered_keys},
        family_one_hot_keys=family_one_hot_keys or [],
    )


# -- registry contract --------------------------------------------------


def test_default_registry_is_empty() -> None:
    """Mechanism-only PR — no default tags. An operator populating
    here without a feature-importance analysis would hurt the model;
    the empty default guards that path."""
    assert monotonic_mod.MONOTONIC_CONSTRAINTS_BY_FAMILY == {}


def test_monotonic_constraints_for_returns_empty_when_unset() -> None:
    assert monotonic_constraints_for("nba_singles") == {}
    assert monotonic_constraints_for("unknown_family") == {}


def test_monotonic_constraints_for_returns_registered_map(monkeypatch) -> None:
    monkeypatch.setitem(
        monotonic_mod.MONOTONIC_CONSTRAINTS_BY_FAMILY,
        "nba_singles",
        {"left_win_rate": 1, "right_win_rate": -1},
    )
    assert monotonic_constraints_for("nba_singles") == {
        "left_win_rate": 1,
        "right_win_rate": -1,
    }


# -- build_monotonic_cst alignment -------------------------------------


def test_build_returns_all_zeros_when_no_constraints() -> None:
    spec = _spec(ordered_keys=["a", "b", "c"])
    assert build_monotonic_cst(spec, "nba_singles") == [0, 0, 0]


def test_build_aligns_constraints_to_ordered_keys(monkeypatch) -> None:
    monkeypatch.setitem(
        monotonic_mod.MONOTONIC_CONSTRAINTS_BY_FAMILY,
        "nba_singles",
        {"feature_a": 1, "feature_c": -1},
    )
    spec = _spec(ordered_keys=["feature_a", "feature_b", "feature_c"])
    assert build_monotonic_cst(spec, "nba_singles") == [1, 0, -1]


def test_build_appends_zeros_for_family_one_hot_slots(monkeypatch) -> None:
    monkeypatch.setitem(
        monotonic_mod.MONOTONIC_CONSTRAINTS_BY_FAMILY,
        "nba_singles",
        {"feature_a": 1},
    )
    spec = _spec(
        ordered_keys=["feature_a"],
        family_one_hot_keys=["nba_singles", "mlb_singles"],
    )
    # 1 (feature_a) + 2 zeros (family one-hots).
    assert build_monotonic_cst(spec, "nba_singles") == [1, 0, 0]


def test_build_clamps_out_of_range_tags(monkeypatch) -> None:
    # Defensive against typos: tags must collapse to {-1, 0, +1}.
    monkeypatch.setitem(
        monotonic_mod.MONOTONIC_CONSTRAINTS_BY_FAMILY,
        "nba_singles",
        {"feature_a": 5, "feature_b": -3, "feature_c": 0},
    )
    spec = _spec(ordered_keys=["feature_a", "feature_b", "feature_c"])
    assert build_monotonic_cst(spec, "nba_singles") == [1, -1, 0]


def test_build_returns_zeros_for_unregistered_family() -> None:
    spec = _spec(ordered_keys=["a", "b"])
    assert build_monotonic_cst(spec, "no_such_family") == [0, 0]


# -- has_any_constraint ------------------------------------------------


def test_has_any_constraint_returns_false_for_all_zeros() -> None:
    assert has_any_constraint([0, 0, 0]) is False
    assert has_any_constraint([]) is False


def test_has_any_constraint_returns_true_when_any_nonzero() -> None:
    assert has_any_constraint([0, 1, 0]) is True
    assert has_any_constraint([-1, 0, 0]) is True
    assert has_any_constraint([1, -1, 1]) is True


def test_has_any_constraint_coerces_int_like_values() -> None:
    # The HGBC constructor takes the list as-is — we coerce to int
    # before checking so caller flexibility doesn't break the gate.
    assert has_any_constraint(["0", "1"]) is True  # type: ignore[list-item]


# -- HGBC integration smoke test ---------------------------------------


def test_hgb_respects_monotonic_constraint_on_synthetic_data() -> None:
    """Train a HistGradientBoostingClassifier on a tiny dataset where
    feature 0 strongly correlates with the target. Verify that with
    a +1 monotonic constraint on feature 0, the model's prediction
    is monotonically non-decreasing in feature 0.

    Without the constraint, the model can overfit to noise and
    produce a non-monotonic relationship even on monotonic data.
    The constraint is the load-bearing piece.
    """
    rng = np.random.default_rng(42)
    n = 500
    feature_0 = np.linspace(-1, 1, n)
    feature_1 = rng.normal(size=n)
    # Target probability sigmoid-like in feature_0.
    logits = 3.0 * feature_0 + 0.1 * feature_1
    probs = 1.0 / (1.0 + np.exp(-logits))
    y = (rng.uniform(size=n) < probs).astype(int)
    x = np.column_stack([feature_0, feature_1])

    model = HistGradientBoostingClassifier(
        max_iter=50,
        random_state=42,
        monotonic_cst=[1, 0],  # feature_0 monotonically increasing
    )
    model.fit(x, y)

    # Probe predictions at sorted values of feature_0 (with feature_1
    # held at 0). The constraint guarantees non-decreasing output.
    sorted_feature_0 = np.linspace(-1, 1, 21)
    grid = np.column_stack([sorted_feature_0, np.zeros_like(sorted_feature_0)])
    predictions = model.predict_proba(grid)[:, 1]
    diffs = np.diff(predictions)
    assert all(d >= -1e-9 for d in diffs), (
        "Monotonic constraint should produce non-decreasing predictions; "
        f"got diffs={diffs.tolist()}"
    )


def test_hgb_without_constraint_can_be_non_monotonic_on_noisy_data() -> None:
    """Sanity check: without the constraint, the model is free to
    fit non-monotonic patterns. This proves the +1 constraint in the
    test above is doing real work."""
    rng = np.random.default_rng(42)
    n = 200
    feature_0 = np.linspace(-1, 1, n)
    # Inject a U-shape: y depends on feature_0**2 with noise.
    probs = np.clip(feature_0 ** 2 + 0.1 * rng.normal(size=n), 0.05, 0.95)
    y = (rng.uniform(size=n) < probs).astype(int)
    x = feature_0.reshape(-1, 1)

    model = HistGradientBoostingClassifier(max_iter=50, random_state=42)
    model.fit(x, y)

    grid = np.linspace(-1, 1, 21).reshape(-1, 1)
    predictions = model.predict_proba(grid)[:, 1]
    diffs = np.diff(predictions)
    # Without a constraint, the U-shape should produce at least one
    # negative diff (model picks up the non-monotonic pattern).
    # If this ever passes — i.e. all diffs are non-negative — the
    # baseline test for the constrained case stops proving anything,
    # because the constraint isn't needed.
    has_decrease = any(d < -1e-6 for d in diffs)
    assert has_decrease, (
        "Expected baseline (unconstrained) HGBC to learn the U-shape on "
        "this synthetic dataset — otherwise the monotonic-constraint test "
        "above stops proving the constraint is load-bearing."
    )
