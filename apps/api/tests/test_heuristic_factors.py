import math

import pytest

from app.services.heuristic_factors import (
    _FACTOR_CLAMP_HIGH,
    _FACTOR_CLAMP_LOW,
    apply_factors,
    compute_advanced_factors,
)


# -----------------------------------------------------------------------------
# Stat-keyed gating

def test_compute_returns_empty_for_unknown_sport():
    assert compute_advanced_factors("NFL", "passing_yards", {}) == {}


def test_compute_returns_empty_for_unknown_stat():
    features = {"recent_true_shooting_pct": 0.65, "season_true_shooting_pct": 0.60}
    assert compute_advanced_factors("NBA", "made_up_stat", features) == {}


def test_compute_filters_no_op_factors():
    """Factors that resolve to 1.0 (because source data is missing) are
    omitted from the result so the features dict stays clean."""
    factors = compute_advanced_factors("NBA", "points", {})
    assert factors == {}


# -----------------------------------------------------------------------------
# NBA factors

def test_efficiency_factor_uses_ts_pct_ratio():
    features = {
        "recent_true_shooting_pct": 0.66,
        "season_true_shooting_pct": 0.60,
    }
    factors = compute_advanced_factors("NBA", "points", features)
    assert factors["efficiency_factor"] == pytest.approx(round(0.66 / 0.60, 4))


def test_efficiency_factor_clamps_extreme_ratios():
    """A 0.30 → 0.05 ratio would blow up; clamp prevents runaway."""
    features = {
        "recent_true_shooting_pct": 0.30,
        "season_true_shooting_pct": 0.05,
    }
    factors = compute_advanced_factors("NBA", "points", features)
    assert factors["efficiency_factor"] == _FACTOR_CLAMP_HIGH


def test_opp_def_factor_suppresses_against_strong_defense():
    """Lower DRtg → harder matchup → factor below 1.0."""
    factors = compute_advanced_factors("NBA", "points", {"opponent_defensive_rating_season": 100.0})
    assert factors["opp_def_factor"] < 1.0  # 100/110 ≈ 0.91
    factors_weak = compute_advanced_factors("NBA", "points", {"opponent_defensive_rating_season": 120.0})
    assert factors_weak["opp_def_factor"] > 1.0  # 120/110 ≈ 1.09
    factors_neutral = compute_advanced_factors("NBA", "points", {"opponent_defensive_rating_season": 110.0})
    # 110/110 = 1.0 exactly → filtered as no-op
    assert "opp_def_factor" not in factors_neutral


def test_opp_recent_form_factor_uses_recent_5():
    features = {"opponent_def_rating_recent_5": 105.0}
    factors = compute_advanced_factors("NBA", "points", features)
    assert factors["opp_recent_form_factor"] == pytest.approx(round(105.0 / 110.0, 4))


def test_pace_factor_advanced_prefers_recent_5():
    features = {"opponent_pace_recent_5": 105.0, "opponent_pace_season": 99.0}
    factors = compute_advanced_factors("NBA", "points", features)
    assert factors["pace_factor_advanced"] == pytest.approx(1.05)


def test_pace_factor_advanced_falls_back_to_season():
    features = {"opponent_pace_season": 102.0}
    factors = compute_advanced_factors("NBA", "points", features)
    assert factors["pace_factor_advanced"] == pytest.approx(1.02)


def test_usage_factor_advanced_uses_real_usg_pct():
    features = {"recent_usage_pct": 0.32, "season_usage_pct": 0.28}
    factors = compute_advanced_factors("NBA", "points", features)
    assert factors["usage_factor_advanced"] == pytest.approx(round(0.32 / 0.28, 4))


def test_nba_stat_gating_excludes_efficiency_for_rebounds():
    features = {
        "recent_true_shooting_pct": 0.66,
        "season_true_shooting_pct": 0.60,
        "opponent_pace_recent_5": 102.0,
    }
    factors = compute_advanced_factors("NBA", "rebounds", features)
    assert "efficiency_factor" not in factors  # rebounds don't care about TS%
    assert "pace_factor_advanced" in factors


# -----------------------------------------------------------------------------
# MLB factors

def test_quality_of_contact_factor_uses_barrel_rate():
    features = {"season_barrel_rate": 0.14}  # 2x league avg
    factors = compute_advanced_factors("MLB", "home_runs", features)
    assert factors["quality_of_contact_factor"] == pytest.approx(_FACTOR_CLAMP_HIGH)


def test_quality_of_contact_factor_falls_back_to_hard_hit_rate():
    features = {"season_hard_hit_rate": 0.42}
    factors = compute_advanced_factors("MLB", "home_runs", features)
    assert factors["quality_of_contact_factor"] == pytest.approx(round(0.42 / 0.40, 4))


def test_starter_factor_advanced_prefers_xfip():
    features = {"opposing_starter_xfip": 4.4, "opposing_starter_fip": 3.0}
    factors = compute_advanced_factors("MLB", "hits", features)
    assert factors["starter_factor_advanced"] == pytest.approx(round(4.4 / 4.0, 4))


def test_k_rate_factor_uses_k_per_9():
    features = {"opposing_starter_k_per_9": 9.5}
    factors = compute_advanced_factors("MLB", "strikeouts", features)
    assert factors["k_rate_factor"] == pytest.approx(round(9.5 / 8.5, 4))


def test_k_rate_factor_clamps_extreme_values():
    """A K/9 of 12 yields 12/8.5 ≈ 1.41 — clamped to the 1.15 cap."""
    features = {"opposing_starter_k_per_9": 12.0}
    factors = compute_advanced_factors("MLB", "strikeouts", features)
    assert factors["k_rate_factor"] == _FACTOR_CLAMP_HIGH


def test_pitcher_dominance_factor_inverts_csw_pct():
    """Higher CSW% → more dominant pitcher → factor < 1.0 (suppresses hits)."""
    features = {"opposing_starter_csw_pct": 0.34}
    factors = compute_advanced_factors("MLB", "hits", features)
    assert factors["pitcher_dominance_factor"] == pytest.approx(round(0.30 / 0.34, 4))


def test_pitcher_dominance_factor_is_not_applied_to_strikeouts():
    """Bug #3: pitcher_dominance_factor returns < 1.0 for dominant pitchers
    (correct for hits/HR/walks where batter output drops), but a dominant
    pitcher should RAISE expected batter strikeouts. k_rate_factor already
    captures that upward signal — gating pitcher_dominance_factor onto
    'strikeouts' was double-wrong (a suppressor cancelling part of the
    amplifier)."""
    features = {
        "opposing_starter_csw_pct": 0.35,  # very dominant pitcher
        "opposing_starter_k_per_9": 11.0,  # strikeouts should be amplified
    }
    factors = compute_advanced_factors("MLB", "strikeouts", features)
    assert "pitcher_dominance_factor" not in factors
    # k_rate_factor still emits (and is clamped at the high end).
    assert factors["k_rate_factor"] >= 1.0


def test_strikeouts_against_dominant_pitcher_yields_net_amplifier():
    """End-to-end check: with a dominant pitcher, the *net* multiplier
    applied to expected strikeouts must be >= 1.0. Before the fix the
    pitcher_dominance suppressor cut the amplifier in half."""
    features = {
        "opposing_starter_csw_pct": 0.35,
        "opposing_starter_k_per_9": 11.0,
    }
    factors = compute_advanced_factors("MLB", "strikeouts", features)
    net = apply_factors(1.0, factors)
    assert net >= 1.0, f"expected amplifier ≥ 1.0 for dominant pitcher, got {net}"


def test_park_factor_hr_passes_through_park_multiplier():
    features = {"park_factor_hr": 1.13}
    factors = compute_advanced_factors("MLB", "home_runs", features)
    assert factors["park_factor_hr_mult"] == pytest.approx(1.13)


def test_weather_factor_short_circuits_in_dome():
    features = {"weather_is_dome": 1.0, "weather_temp_f": 30.0,
                "weather_wind_speed_mph": 30.0, "weather_wind_dir_deg": 0.0}
    factors = compute_advanced_factors("MLB", "home_runs", features)
    assert "weather_factor" not in factors  # 1.0 → filtered no-op


def test_weather_factor_warmth_boosts_hrs():
    features = {"weather_is_dome": 0.0, "weather_temp_f": 95.0,
                "weather_wind_speed_mph": 0.0, "weather_wind_dir_deg": 0.0}
    factors = compute_advanced_factors("MLB", "home_runs", features)
    # +20° from baseline → +20/30 * 0.10 = ~6.7% boost
    assert factors["weather_factor"] == pytest.approx(round(1.0 + (20.0 * 0.10 / 30.0), 4))


def test_weather_factor_wind_out_to_cf_helps():
    features = {"weather_is_dome": 0.0, "weather_temp_f": 75.0,
                "weather_wind_speed_mph": 20.0, "weather_wind_dir_deg": 0.0}
    factors = compute_advanced_factors("MLB", "home_runs", features)
    # cos(0°) * 20 * 0.005 = 0.10 boost → 1.10
    assert factors["weather_factor"] == pytest.approx(1.10)


def test_lineup_factor_leadoff_gets_pa_boost():
    features = {"batting_order_position": 1}
    factors = compute_advanced_factors("MLB", "rbis", features)
    assert factors["lineup_factor"] == pytest.approx(1.05)


def test_lineup_factor_eight_hole_suppressed():
    features = {"batting_order_position": 8}
    factors = compute_advanced_factors("MLB", "rbis", features)
    assert factors["lineup_factor"] == pytest.approx(0.96)


# -----------------------------------------------------------------------------
# apply_factors

def test_apply_factors_multiplies_in_order():
    out = apply_factors(20.0, {"a": 1.10, "b": 1.05})
    assert out == pytest.approx(20.0 * 1.10 * 1.05)


def test_apply_factors_with_empty_dict_is_identity():
    assert apply_factors(15.0, {}) == 15.0
