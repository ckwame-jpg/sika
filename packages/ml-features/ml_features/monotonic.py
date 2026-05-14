"""Smarter #19 — per-family monotonic constraints for HGBC training.

The registry lives in the shared ``ml_features`` package because
``apps/ml/ml/training.py`` consumes it at training time AND
``apps/api/app/services/model_families.py`` re-exports it for
operator visibility — two top-level apps that can't import from each
other.

This PR ships the **mechanism only**. The registry is empty by design;
populating it requires either:

  (a) Adding side-aware derived features at scoring time (e.g.
      ``side_signed_recent_avg_delta = (recent_avg - threshold) *
      side_sign``) so a single direction tag applies to all training
      rows regardless of OVER / UNDER side.
  (b) Training separate models per market side so feature direction
      is stable within each model's training corpus.

Without either, a monotonic constraint on a side-dependent feature
(``recent_10_average``, ``threshold``, ``opponent_def_rating_*``)
would actively HURT the model — YES_OVER rows want the opposite
direction from YES_UNDER rows on the same feature.

Constraint values follow scikit's ``HistGradientBoostingClassifier``
convention:

  +1 → output monotonically INCREASING in this feature
   0 → no constraint
  -1 → output monotonically DECREASING in this feature
"""

from __future__ import annotations

from typing import Sequence

from ml_features.spec import FeatureSpec


# Empty by design. See module docstring for why and what's needed to
# populate. An operator adding constraints here MUST do a
# feature-importance analysis first to confirm the direction holds
# across the training corpus.
MONOTONIC_CONSTRAINTS_BY_FAMILY: dict[str, dict[str, int]] = {}


def monotonic_constraints_for(family_key: str) -> dict[str, int]:
    """Return the per-family ``feature_key → {-1, 0, +1}`` constraint map.

    Falls back to an empty dict for any unregistered family.
    """
    return MONOTONIC_CONSTRAINTS_BY_FAMILY.get(family_key, {})


def build_monotonic_cst(
    feature_spec: FeatureSpec,
    family_key: str,
) -> list[int]:
    """Build the ``monotonic_cst`` array aligned to
    ``vectorize``'s feature ordering.

    ``vectorize`` produces a feature vector of length
    ``len(ordered_keys) + len(family_one_hot_keys)``. The
    HistGradientBoostingClassifier expects ``monotonic_cst`` to have
    the same length, with each element in {-1, 0, +1}. Family-one-hot
    slots always get 0 — direction tags on a categorical's indicator
    columns don't have a meaningful interpretation.

    Returns an all-zero array when ``monotonic_constraints_for`` is
    empty for the family — the caller can detect "no constraints"
    by checking ``any(v != 0 for v in array)`` and skip passing it
    to HGBC to avoid the overhead of constraint enforcement.
    """
    constraints = monotonic_constraints_for(family_key)
    array: list[int] = []
    for key in feature_spec.ordered_keys:
        raw = constraints.get(key, 0)
        # Defensive: clamp to the valid sklearn range so a typo (e.g.
        # ``+2``) doesn't raise inside the training pipeline.
        if raw > 0:
            array.append(1)
        elif raw < 0:
            array.append(-1)
        else:
            array.append(0)
    for _ in feature_spec.family_one_hot_keys:
        array.append(0)
    return array


def has_any_constraint(constraints: Sequence[int]) -> bool:
    """True when at least one slot is non-zero — i.e. the array would
    meaningfully constrain HGBC. Callers use this to skip the array
    entirely when nothing is constrained (no overhead, behavior
    identical to the pre-Smarter-#19 path)."""
    return any(int(v) != 0 for v in constraints)
