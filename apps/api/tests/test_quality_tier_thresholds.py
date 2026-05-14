"""Tests for Smarter #28 — per-family ``_quality_tier`` calibration.

The Smarter #28 PR introduces the mechanism (a per-family override
dict in ``model_families.py``) without actually tuning any family's
thresholds — every family resolves to ``DEFAULT_QUALITY_TIER_THRESHOLDS``,
which matches the constants the kernel hardcoded before this change.
The regression tests below pin the default values so an accidental
threshold edit will trip CI.
"""

import pytest

from app.services.model_families import (
    DEFAULT_QUALITY_TIER_THRESHOLDS,
    FAMILY_DEFINITIONS,
    QUALITY_TIER_THRESHOLDS_BY_FAMILY,
    QualityTierThresholds,
    quality_tier_thresholds_for,
)
from app.services.scoring import _quality_tier


# -- threshold registry contract -----------------------------------------


def test_default_thresholds_match_pre_smarter_28_constants() -> None:
    """Regression: the heuristic-path defaults match the constants the
    kernel previously hardcoded. Tweaking these is a real behavior
    change and should be conscious."""
    assert DEFAULT_QUALITY_TIER_THRESHOLDS == QualityTierThresholds(
        high_selected_side_probability=0.36,
        high_context_coverage=0.72,
        high_adjusted_confidence=0.58,
        high_total_penalty=0.09,
        low_selected_side_probability=0.20,
        low_context_coverage=0.45,
        low_adjusted_confidence=0.40,
        low_total_penalty=0.18,
        ml_high_context_coverage=0.75,
        ml_high_adjusted_confidence=0.60,
        ml_medium_context_coverage=0.50,
    )


def test_override_registry_starts_empty() -> None:
    # The Smarter #28 PR ships only the mechanism; no family is tuned
    # yet. Adding entries here should be a conscious calibration
    # decision based on settled-data analysis.
    assert QUALITY_TIER_THRESHOLDS_BY_FAMILY == {}


def test_every_registered_family_resolves_to_defaults() -> None:
    for definition in FAMILY_DEFINITIONS:
        assert (
            quality_tier_thresholds_for(definition.key)
            is DEFAULT_QUALITY_TIER_THRESHOLDS
        )


def test_unknown_family_falls_back_to_defaults() -> None:
    assert (
        quality_tier_thresholds_for("unknown_scope_family")
        is DEFAULT_QUALITY_TIER_THRESHOLDS
    )


# -- heuristic-path tier classification (defaults) -----------------------


def _tier(
    *,
    family_key: str = "nba_singles",
    selected_side_probability: float = 0.50,
    adjusted_confidence: float = 0.70,
    context_coverage_score: float = 0.80,
    total_penalty: float = 0.05,
    served_mode: str = "heuristic",
) -> str:
    return _quality_tier(
        family_key=family_key,
        selected_side_probability=selected_side_probability,
        adjusted_confidence=adjusted_confidence,
        context_coverage_score=context_coverage_score,
        total_penalty=total_penalty,
        served_mode=served_mode,
    )


def test_heuristic_high_tier_requires_all_four_conditions() -> None:
    # Right at every high threshold → high.
    assert _tier(
        selected_side_probability=0.36,
        adjusted_confidence=0.58,
        context_coverage_score=0.72,
        total_penalty=0.09,
    ) == "high"


def test_heuristic_high_tier_breaks_on_any_low_input() -> None:
    # One input fails the high test but stays above the low threshold
    # for that input → drops to medium, not low.
    assert _tier(
        selected_side_probability=0.35,
        adjusted_confidence=0.58,
        context_coverage_score=0.72,
        total_penalty=0.09,
    ) == "medium"


def test_heuristic_low_tier_fires_on_any_low_input() -> None:
    # Side probability below 0.20 → low regardless of the others.
    assert _tier(
        selected_side_probability=0.19,
        adjusted_confidence=0.70,
        context_coverage_score=0.80,
        total_penalty=0.05,
    ) == "low"


def test_heuristic_low_tier_fires_on_high_total_penalty() -> None:
    assert _tier(
        selected_side_probability=0.50,
        adjusted_confidence=0.70,
        context_coverage_score=0.80,
        total_penalty=0.18,
    ) == "low"


def test_heuristic_medium_tier_is_the_default_between_floors() -> None:
    # Above every low floor but below at least one high ceiling.
    assert _tier(
        selected_side_probability=0.30,
        adjusted_confidence=0.55,
        context_coverage_score=0.60,
        total_penalty=0.10,
    ) == "medium"


# -- ML-path tier classification (defaults) ------------------------------


def test_ml_high_tier_requires_calibrated_confidence_and_coverage() -> None:
    assert _tier(
        served_mode="ml",
        adjusted_confidence=0.60,
        context_coverage_score=0.75,
    ) == "high"


def test_ml_medium_tier_when_only_coverage_clears() -> None:
    assert _tier(
        served_mode="ml",
        adjusted_confidence=0.40,
        context_coverage_score=0.50,
    ) == "medium"


def test_ml_low_tier_when_coverage_below_floor() -> None:
    assert _tier(
        served_mode="ml",
        adjusted_confidence=0.80,
        context_coverage_score=0.40,
    ) == "low"


# -- per-family override behavior ----------------------------------------


def test_override_can_promote_a_family_to_more_lenient_thresholds(monkeypatch) -> None:
    # Stub a per-family override that lowers every high threshold.
    # The same inputs that would be ``medium`` under defaults must
    # promote to ``high`` for the overridden family only.
    lenient = QualityTierThresholds(
        high_selected_side_probability=0.25,
        high_context_coverage=0.50,
        high_adjusted_confidence=0.45,
        high_total_penalty=0.15,
    )
    monkeypatch.setitem(QUALITY_TIER_THRESHOLDS_BY_FAMILY, "nba_props", lenient)
    # Inputs sit below the defaults' high ceiling on every axis but
    # comfortably above the lenient family's relaxed ceilings.
    inputs = dict(
        selected_side_probability=0.30,
        adjusted_confidence=0.50,
        context_coverage_score=0.60,
        total_penalty=0.10,
    )
    assert _tier(family_key="nba_props", **inputs) == "high"
    # An unrelated family still uses defaults → same inputs stay medium.
    assert _tier(family_key="nba_singles", **inputs) == "medium"


def test_override_can_demote_a_family_to_stricter_thresholds(monkeypatch) -> None:
    strict = QualityTierThresholds(
        high_selected_side_probability=0.45,
        high_context_coverage=0.85,
        high_adjusted_confidence=0.70,
        high_total_penalty=0.05,
    )
    monkeypatch.setitem(QUALITY_TIER_THRESHOLDS_BY_FAMILY, "mlb_singles", strict)
    # Inputs that would clear the defaults' high ceiling fail the
    # stricter family's tighter thresholds.
    inputs = dict(
        selected_side_probability=0.36,
        adjusted_confidence=0.58,
        context_coverage_score=0.72,
        total_penalty=0.09,
    )
    assert _tier(family_key="mlb_singles", **inputs) == "medium"
    assert _tier(family_key="nba_singles", **inputs) == "high"


def test_ml_path_overrides_apply_independently_from_heuristic(monkeypatch) -> None:
    # Tighten only the ML path; the heuristic thresholds inherit defaults.
    ml_only_strict = QualityTierThresholds(
        ml_high_context_coverage=0.90,
        ml_high_adjusted_confidence=0.80,
        ml_medium_context_coverage=0.70,
    )
    monkeypatch.setitem(QUALITY_TIER_THRESHOLDS_BY_FAMILY, "nba_props", ml_only_strict)
    assert _tier(
        family_key="nba_props",
        served_mode="ml",
        adjusted_confidence=0.60,
        context_coverage_score=0.75,
    ) == "medium"  # was high under defaults
    assert _tier(
        family_key="nba_singles",
        served_mode="ml",
        adjusted_confidence=0.60,
        context_coverage_score=0.75,
    ) == "high"  # default family unchanged
