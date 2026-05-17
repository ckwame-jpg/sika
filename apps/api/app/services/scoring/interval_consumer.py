"""Smarter #21 phase 2d — scoring kernel prediction-interval consumer.

Phase 2a established the on-disk sidecar contract; phase 2b shipped
the dataset extractor + ``train-intervals`` CLI; phase 2c shipped the
serve-time loader (``SklearnArtifact.interval_models`` +
``apply_interval_models``). This module is the consumer that wires
those serve-time intervals into ``_score_player_prop``.

## Gating policy (load-bearing — see PR description)

Two gates are required before the interval-derived YES probability
replaces the Poisson estimate:

1. **Artifact present** — ``apply_interval_models`` returns
   ``(p10, p50, p90)`` for ``stat_key``. Stat keys without a trained
   sidecar fall through; behavior is unchanged for them, so a
   rollback that removes this consumer leaves Poisson serving fine.

2. **Coverage gate (strict)** — ``coverage_status_for_stat`` returns
   ``"ok"`` (empirical_coverage in ``[0.70, 0.90]`` against the 0.80
   target). The 2026-05-16 inspect-intervals demo proved 4/7 trained
   stat keys land in ``"bad"`` coverage (over-covering) — naively
   consuming those intervals would ship worse projections than the
   Poisson approximation. The diagnostic dict is STILL emitted for
   ``"warn"`` / ``"bad"`` / ``"unknown"`` rows so operators can
   inspect side-by-side, but ``probability_yes`` is not swapped.

The strict policy is the safest first ship. Once more games settle
and more stat keys migrate into ``"ok"``, additional rows naturally
activate without any code change. A future phase can experiment with
a coverage-weighted blend.

## CDF distribution choice

A triangular distribution on ``(p10, p50, p90)`` treats them as
``(min, mode, max)`` of the support. This is the cheap-and-defensible
default — the closed-form CDF integrates in O(1) and the shape
respects the central tendency + spread implied by the quantiles.

It is approximate (it does NOT preserve the quantile semantics
exactly — true CDF(p10) would be 0.10, here it is 0). A future phase
can refine to piecewise-linear interpolation through the three
quantile points when more quantiles are trained.

Degenerate intervals (two or all three quantiles collapsing) are
handled explicitly so the consumer doesn't divide by zero.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ml_features import vectorize

from app.services.ml.artifact_loader import apply_interval_models, load_sklearn_artifact
from app.services.ml.interval_status import coverage_status_for_stat
from app.services.ml.registry import load_model_manifest


logger = logging.getLogger(__name__)


_SOURCE_TAG = "interval_model_v1"


def triangular_yes_probability(*, p10: float, p50: float, p90: float, threshold: float) -> float:
    """Return ``P(X > threshold)`` for a triangular distribution
    parameterized by ``(a=p10, c=p50, b=p90)``.

    Caller contract: ``p10 <= p50 <= p90`` (the artifact-loader's
    ``apply_interval_models`` already sorts the triple before
    returning, so the swap is always safe). The bare math helper
    asserts the contract too — raises ``ValueError`` on unsorted
    input to keep silent miscomputation off the table.

    Degenerate cases:
    - ``p10 == p50 == p90``: point mass. Threshold below → 1.0;
      above → 0.0; equal → 0.5 (we have no info to break the tie,
      and 0.5 preserves monotonicity vs. small perturbations).
    - ``p10 == p50 < p90``: right-skewed (mode at lower bound).
    - ``p10 < p50 == p90``: left-skewed (mode at upper bound).
    """
    if not (p10 <= p50 <= p90):
        raise ValueError(
            f"triangular_yes_probability requires p10 <= p50 <= p90; "
            f"got p10={p10}, p50={p50}, p90={p90}"
        )
    # Point mass — entire support collapses to one point.
    if p10 == p90:
        if threshold < p10:
            return 1.0
        if threshold > p10:
            return 0.0
        return 0.5

    # Standard triangular CDF on (a, c, b) where a=p10, c=p50, b=p90.
    a, c, b = p10, p50, p90
    if threshold <= a:
        return 1.0
    if threshold >= b:
        return 0.0
    if threshold <= c:
        # CDF below mode: (x-a)^2 / ((b-a)*(c-a))
        # When c == a (right-skewed, mode at lower bound) the only
        # x reaching this branch is x == a == c — handled above by
        # threshold <= a → 1.0.
        denom = (b - a) * (c - a)
        if denom == 0.0:
            return 1.0
        cdf = ((threshold - a) ** 2) / denom
    else:
        # CDF above mode: 1 - (b-x)^2 / ((b-a)*(b-c))
        # When c == b (left-skewed, mode at upper bound) the only
        # x reaching this branch is x > c == b — handled above by
        # threshold >= b → 0.0.
        denom = (b - a) * (b - c)
        if denom == 0.0:
            return 0.0
        cdf = 1.0 - ((b - threshold) ** 2) / denom
    # Clamp to [0, 1] — defensive against floating-point boundary drift.
    if cdf < 0.0:
        cdf = 0.0
    elif cdf > 1.0:
        cdf = 1.0
    return 1.0 - cdf


def _find_artifact_dir_for_family(family_key: str) -> Path | None:
    """Resolve the artifact directory the active manifest serves for
    ``family_key``. Returns ``None`` when no manifest is configured,
    no family entry matches, or the artifact_path is missing.

    Matches the manifest lookup ``runtime._artifact_payload_for_decision``
    uses: a family entry serves ``family_key`` when its
    ``serves_family_key`` (or ``family_key`` when ``serves_family_key``
    is missing) equals the query.
    """
    manifest = load_model_manifest()
    if manifest is None or not manifest.families:
        return None
    manifest_dir = Path(manifest.source_path).parent if manifest.source_path else None
    for family in manifest.families:
        served = (family.serves_family_key or family.family_key or "").strip()
        if served != family_key:
            continue
        if not family.artifact_path:
            continue
        artifact_dir = (
            (manifest_dir / family.artifact_path).resolve()
            if manifest_dir is not None
            else Path(family.artifact_path).resolve()
        )
        if artifact_dir.exists() and artifact_dir.is_dir():
            return artifact_dir
    return None


def consume_prediction_interval(
    *,
    family_key: str,
    stat_key: str,
    threshold: float,
    features: dict[str, Any],
    poisson_yes_probability: float,
) -> dict[str, Any] | None:
    """Look up the served artifact for ``family_key``, apply the
    per-stat interval model, and return a diagnostic dict the scoring
    kernel can drop into ``features["prediction_interval"]``.

    Returns ``None`` when any of the following are true (the scoring
    kernel falls back to its existing Poisson behavior):
    - No active manifest, or ``family_key`` has no serving entry.
    - The artifact directory is missing or fails to load.
    - The artifact has no interval model trained for ``stat_key``.
    - ``apply_interval_models`` raises (corrupt sidecar that survived
      load-time probe) — already logged by the loader.

    When a triple IS returned, the dict ALWAYS contains the
    ``coverage_status`` so the caller can decide whether to swap
    ``probability_yes``. The strict-gating policy lives in the
    caller (``_score_player_prop`` in scoring/__init__.py) — this
    function is the data layer; the gate is the policy.
    """
    artifact_dir = _find_artifact_dir_for_family(family_key)
    if artifact_dir is None:
        return None
    try:
        artifact = load_sklearn_artifact(artifact_dir)
    except Exception as exc:  # noqa: BLE001 — graceful fallback on corrupt artifact
        logger.warning(
            "scoring.interval_consumer.artifact_load_failed: "
            "family=%s stat=%s error=%s",
            family_key, stat_key, exc,
        )
        return None
    if stat_key not in artifact.interval_models:
        return None

    # Vectorize the same way runtime.py does — read keys via the
    # artifact's FeatureSpec so the input shape matches the trained
    # quantile regressors. The interval models share the artifact's
    # FeatureSpec (phase 2b's CLI fits them on the same vectorized
    # X as the base model — same ordered_keys, same imputation
    # defaults).
    vector = vectorize(dict(features), artifact.feature_spec).reshape(1, -1)
    triple = apply_interval_models(artifact, stat_key, vector)
    if triple is None:
        return None
    p10, p50, p90 = triple

    yes_from_interval = triangular_yes_probability(
        p10=p10, p50=p50, p90=p90, threshold=threshold,
    )
    coverage_status = coverage_status_for_stat(artifact_dir, stat_key)

    return {
        "p10": p10,
        "p50": p50,
        "p90": p90,
        "threshold": round(float(threshold), 4),
        "source": _SOURCE_TAG,
        "coverage_status": coverage_status,
        "yes_probability_from_interval": round(yes_from_interval, 4),
        "yes_probability_from_poisson": round(float(poisson_yes_probability), 4),
        "delta": round(yes_from_interval - float(poisson_yes_probability), 4),
    }
