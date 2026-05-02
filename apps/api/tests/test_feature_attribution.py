"""Tests for ``app.services.feature_attribution``.

Covers:
  - shape of the returned ``_drivers`` list (key/label/delta_pct/direction/detail)
  - sorting (largest absolute delta first)
  - near-zero filter
  - label table + humanize fallback
  - detail strings pull the right keys from the features dict
  - ``driver_reason_strings`` renders the top rows for ``reasons[]``
"""

from __future__ import annotations

import pytest

from app.services.feature_attribution import (
    driver_reason_strings,
    top_drivers,
)


# -----------------------------------------------------------------------------
# Shape


def test_returns_empty_when_no_advanced_factors():
    assert top_drivers({}, 20.0, 20.0) == []


def test_returns_empty_when_advanced_factors_is_not_dict():
    assert top_drivers({"advanced_factors": "not-a-dict"}, 20.0, 20.0) == []


def test_driver_row_shape():
    features = {
        "advanced_factors": {"efficiency_factor": 1.10},
        "recent_true_shooting_pct": 0.66,
        "season_true_shooting_pct": 0.60,
    }
    drivers = top_drivers(features, 20.0, 22.0)
    assert len(drivers) == 1
    row = drivers[0]
    assert row["key"] == "efficiency_factor"
    assert row["label"] == "Shooting efficiency"
    assert row["delta_pct"] == pytest.approx(10.0)
    assert row["direction"] == "up"
    assert row["detail"] == "Recent TS% 66.0% vs season 60.0%"


# -----------------------------------------------------------------------------
# Sorting


def test_drivers_sorted_by_absolute_delta_pct_descending():
    features = {
        "advanced_factors": {
            "efficiency_factor": 1.05,
            "opp_def_factor": 0.92,  # |8|
            "usage_factor_advanced": 1.13,  # |13|
        }
    }
    drivers = top_drivers(features, 20.0, 22.0)
    keys = [d["key"] for d in drivers]
    assert keys == ["usage_factor_advanced", "opp_def_factor", "efficiency_factor"]


def test_limit_caps_returned_drivers():
    features = {
        "advanced_factors": {
            "efficiency_factor": 1.10,
            "opp_def_factor": 0.92,
            "usage_factor_advanced": 1.13,
            "pace_factor_advanced": 1.05,
        }
    }
    drivers = top_drivers(features, 20.0, 22.0, limit=2)
    assert len(drivers) == 2


def test_near_zero_factors_are_filtered():
    features = {
        "advanced_factors": {
            "efficiency_factor": 1.10,
            "opp_def_factor": 1.001,  # 0.1% — below min_abs_delta_pct
        }
    }
    drivers = top_drivers(features, 20.0, 22.0)
    assert [d["key"] for d in drivers] == ["efficiency_factor"]


# -----------------------------------------------------------------------------
# Direction


def test_direction_up_for_positive_delta():
    features = {"advanced_factors": {"efficiency_factor": 1.05}}
    drivers = top_drivers(features, 20.0, 21.0)
    assert drivers[0]["direction"] == "up"


def test_direction_down_for_negative_delta():
    features = {"advanced_factors": {"opp_def_factor": 0.92}}
    drivers = top_drivers(features, 20.0, 18.4)
    assert drivers[0]["direction"] == "down"


# -----------------------------------------------------------------------------
# Labels


def test_known_factor_uses_curated_label():
    features = {"advanced_factors": {"quality_of_contact_factor": 1.12}}
    drivers = top_drivers(features, 1.0, 1.12)
    assert drivers[0]["label"] == "Quality of contact"


def test_unknown_factor_uses_humanize_fallback():
    features = {"advanced_factors": {"some_made_up_factor": 1.10}}
    drivers = top_drivers(features, 1.0, 1.1)
    assert drivers[0]["label"] == "Some Made Up"


# -----------------------------------------------------------------------------
# Detail strings


def test_detail_for_efficiency_factor_uses_ts_pct():
    features = {
        "advanced_factors": {"efficiency_factor": 1.10},
        "recent_true_shooting_pct": 0.66,
        "season_true_shooting_pct": 0.60,
    }
    drivers = top_drivers(features, 20.0, 22.0)
    assert drivers[0]["detail"] == "Recent TS% 66.0% vs season 60.0%"


def test_detail_for_quality_of_contact_uses_barrel_rate():
    features = {
        "advanced_factors": {"quality_of_contact_factor": 1.12},
        "season_barrel_rate": 0.14,
    }
    drivers = top_drivers(features, 1.0, 1.12)
    assert drivers[0]["detail"] == "Season barrel rate: 14.0%"


def test_detail_for_starter_factor_advanced_prefers_xfip():
    features = {
        "advanced_factors": {"starter_factor_advanced": 1.10},
        "opposing_starter_xfip": 4.40,
        "opposing_starter_fip": 3.80,
    }
    drivers = top_drivers(features, 1.0, 1.1)
    assert drivers[0]["detail"] == "Opposing starter xFIP: 4.40"


def test_detail_for_lineup_factor_uses_batting_order():
    features = {
        "advanced_factors": {"lineup_factor": 1.05},
        "batting_order_position": 1,
    }
    drivers = top_drivers(features, 1.0, 1.05)
    assert drivers[0]["detail"] == "Batting order position: 1"


def test_detail_for_weather_factor_uses_temp_and_wind():
    features = {
        "advanced_factors": {"weather_factor": 1.07},
        "weather_is_dome": 0.0,
        "weather_temp_f": 95.0,
        "weather_wind_speed_mph": 15.0,
        "weather_wind_dir_deg": 30.0,
    }
    drivers = top_drivers(features, 1.0, 1.07)
    assert drivers[0]["detail"] == "95°F, wind 15 mph @ 30°"


def test_detail_is_none_when_source_data_missing():
    features = {"advanced_factors": {"efficiency_factor": 1.10}}  # no TS% keys
    drivers = top_drivers(features, 1.0, 1.1)
    assert drivers[0]["detail"] is None


# -----------------------------------------------------------------------------
# driver_reason_strings


def test_reason_strings_include_label_delta_and_detail():
    drivers = [
        {
            "key": "quality_of_contact_factor",
            "label": "Quality of contact",
            "delta_pct": 12.0,
            "direction": "up",
            "detail": "Season barrel rate: 14.0%",
        }
    ]
    strings = driver_reason_strings(drivers)
    assert strings == ["Quality of contact +12.0%: Season barrel rate: 14.0%"]


def test_reason_strings_omit_detail_when_missing():
    drivers = [
        {
            "key": "efficiency_factor",
            "label": "Shooting efficiency",
            "delta_pct": -8.5,
            "direction": "down",
            "detail": None,
        }
    ]
    strings = driver_reason_strings(drivers)
    assert strings == ["Shooting efficiency -8.5%"]


def test_reason_strings_respect_limit():
    drivers = [
        {"key": "a", "label": "A", "delta_pct": 12.0, "direction": "up", "detail": None},
        {"key": "b", "label": "B", "delta_pct": -10.0, "direction": "down", "detail": None},
        {"key": "c", "label": "C", "delta_pct": 5.0, "direction": "up", "detail": None},
    ]
    strings = driver_reason_strings(drivers, limit=2)
    assert len(strings) == 2
