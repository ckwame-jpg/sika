"""Tests for Smarter #7 — MLB park × weather interaction term for HR
and total bases.

Park HR factors and weather features have been independent
multipliers up to now. The interaction is real: Coors Field at 90°F
plays differently than either alone — thin air + warm temperatures
let the ball carry MORE than the multiplicative combination of the
two independent factors would predict.

This factor adds a small non-linear bonus when BOTH the park and
the weather push in the same direction:
- HR-favorable park (>1.0) + warm temperatures (>70°F) → extra boost
- HR-suppressing park (<1.0) + cold temperatures (<70°F) → extra suppression
- Mixed (favorable park + cold, or pitcher park + warm) → no extra
  effect (the independent multipliers already handle the basic case)

Envelope is ±5% so the interaction doesn't dominate the base park
or weather factors. Gated on dome games (weather doesn't matter).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.heuristic_factors import (
    _FACTOR_CLAMP_HIGH,
    _FACTOR_CLAMP_LOW,
    compute_advanced_factors,
)


def _features(
    *,
    park_hr: float | None = None,
    temp_f: float | None = None,
    park_complete: float | None = None,
    weather_complete: float | None = None,
    is_dome: float | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if park_hr is not None:
        out["park_factor_hr"] = park_hr
    if temp_f is not None:
        out["weather_temp_f"] = temp_f
    if park_complete is not None:
        out["park_data_complete"] = park_complete
    if weather_complete is not None:
        out["weather_data_complete"] = weather_complete
    if is_dome is not None:
        out["weather_is_dome"] = is_dome
    return out


# -- Pure factor: same-direction amplification --------------------------


def test_factor_amplifies_when_hr_park_and_warm_weather() -> None:
    """Coors Field (park_hr=1.27) + 90°F → bonus on top of the
    multiplicative product of the independent factors."""
    factors = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.27, temp_f=90.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    # 0.27 * 0.67 * 0.10 ≈ 0.018 → factor ≈ 1.018
    assert factors["park_weather_hr_interaction"] == pytest.approx(1.018, abs=0.005)


def test_factor_suppresses_when_pitcher_park_and_cold_weather() -> None:
    """Petco Park (park_hr=0.85) + 50°F → extra suppression on top of
    the independent multipliers."""
    factors = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=0.85, temp_f=50.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    # |-0.15| * |-0.67| * 0.10 ≈ 0.010, sign negative → factor ≈ 0.990
    assert factors["park_weather_hr_interaction"] == pytest.approx(0.990, abs=0.005)


def test_factor_neutral_when_mixed_signals() -> None:
    """Hitter park (park_hr>1) + cold (temp<70), OR pitcher park +
    warm — the signals cancel; the independent multipliers already
    handle this case. No extra interaction."""
    factors_hot_park_cold = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.20, temp_f=55.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" not in factors_hot_park_cold

    factors_cold_park_hot = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=0.85, temp_f=85.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" not in factors_cold_park_hot


def test_factor_neutral_at_league_average_park_or_temp() -> None:
    """Park at 1.0 OR temp at 70°F → no signal magnitude → no
    interaction (one side is zero)."""
    factors_neutral_park = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.0, temp_f=90.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" not in factors_neutral_park

    factors_neutral_temp = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.27, temp_f=70.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" not in factors_neutral_temp


# -- Gates ------------------------------------------------------------


def test_factor_skipped_for_dome_games() -> None:
    """Dome games don't have meaningful weather — the interaction
    must not fire even with full park + temp signals."""
    factors = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.27, temp_f=90.0,
            park_complete=1.0, weather_complete=1.0, is_dome=1.0,
        ),
    )
    assert "park_weather_hr_interaction" not in factors


def test_factor_skipped_when_park_data_incomplete() -> None:
    factors = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.27, temp_f=90.0,
            park_complete=0.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" not in factors


def test_factor_skipped_when_weather_data_incomplete() -> None:
    factors = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.27, temp_f=90.0,
            park_complete=1.0, weather_complete=0.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" not in factors


def test_factor_skipped_when_temp_missing() -> None:
    factors = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.27,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" not in factors


def test_factor_clamps_at_envelope_for_extreme_values() -> None:
    """Coors (park_hr=1.5) + 110°F shouldn't blow up the projection."""
    factors = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.5, temp_f=110.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    # 0.50 * 1.33 * 0.10 = 0.067 → would exceed the +5% cap
    assert factors["park_weather_hr_interaction"] == pytest.approx(1.05, abs=0.0001)


# -- Stat-key gating ---------------------------------------------------


def test_factor_applied_to_home_runs() -> None:
    factors = compute_advanced_factors(
        "MLB", "home_runs",
        _features(
            park_hr=1.27, temp_f=90.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" in factors


def test_factor_applied_to_total_bases() -> None:
    """Total bases includes HR contribution; same interaction applies."""
    factors = compute_advanced_factors(
        "MLB", "total_bases",
        _features(
            park_hr=1.27, temp_f=90.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" in factors


@pytest.mark.parametrize(
    "stat_key", ["hits", "rbis", "runs", "walks", "strikeouts", "doubles", "triples"],
)
def test_factor_not_applied_to_non_hr_stats(stat_key: str) -> None:
    """Non-HR stats don't share the same park/weather sensitivity —
    hits and RBIs aren't dominated by the long ball alone."""
    factors = compute_advanced_factors(
        "MLB", stat_key,
        _features(
            park_hr=1.27, temp_f=90.0,
            park_complete=1.0, weather_complete=1.0, is_dome=0.0,
        ),
    )
    assert "park_weather_hr_interaction" not in factors
