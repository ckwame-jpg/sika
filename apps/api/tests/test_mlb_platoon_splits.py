"""Smarter #5 — batter vs LHP/RHP platoon splits."""

from __future__ import annotations

import pytest

from app.services import mlb_advanced
from app.services.heuristic_factors import (
    _MLB_FACTORS_BY_STAT,
    _mlb_batter_platoon_factor,
    factor_applies,
)


# -- _normalize_pitch_hand ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("L", "L"),
        ("R", "R"),
        ("l", "L"),
        ("r", "R"),
        ("Left", "L"),
        ("Right", "R"),
        ("LHP", "L"),
        ("RHP", "R"),
        ("rhp", "R"),
        ("S", None),  # switch-pitcher — refuse to guess
        ("", None),
        (None, None),
        (1, None),
    ],
)
def test_normalize_pitch_hand_variants(raw, expected) -> None:
    assert mlb_advanced._normalize_pitch_hand(raw) == expected


# -- _split_row_for_hand -----------------------------------------------------


def _splits_payload() -> dict:
    """Mirror the shape produced by ``_flatten_stat_splits``."""

    return {
        "splits": [
            {
                "ops": "0.812",
                "avg": "0.275",
                "_split_meta": {"split": {"code": "vl", "description": "vs Left"}},
            },
            {
                "ops": "0.701",
                "avg": "0.241",
                "_split_meta": {"split": {"code": "vr", "description": "vs Right"}},
            },
        ]
    }


def test_split_row_for_hand_returns_left_row() -> None:
    row = mlb_advanced._split_row_for_hand(_splits_payload(), "L")
    assert row is not None
    assert row["ops"] == "0.812"


def test_split_row_for_hand_returns_right_row() -> None:
    row = mlb_advanced._split_row_for_hand(_splits_payload(), "R")
    assert row is not None
    assert row["ops"] == "0.701"


def test_split_row_for_hand_uses_description_fallback() -> None:
    """If only ``description`` (not ``code``) is populated upstream we
    still find the right row."""

    payload = {
        "splits": [
            {"ops": "0.900", "_split_meta": {"split": {"description": "vs Right"}}},
        ]
    }
    row = mlb_advanced._split_row_for_hand(payload, "R")
    assert row is not None
    assert row["ops"] == "0.900"


def test_split_row_for_hand_returns_none_when_no_match() -> None:
    assert mlb_advanced._split_row_for_hand({"splits": []}, "L") is None
    assert mlb_advanced._split_row_for_hand(None, "L") is None
    assert mlb_advanced._split_row_for_hand(_splits_payload(), "S") is None  # not L/R


# -- emit_mlb_platoon_features -----------------------------------------------


def test_emit_platoon_features_lhp_vs_rhb_emits_factor_below_one() -> None:
    """RHB facing LHP: vs Left OPS 0.812, season OPS 0.870 → ratio ~0.93."""

    out = mlb_advanced.emit_mlb_platoon_features(
        starter_pitch_hand="L",
        splits_payload=_splits_payload(),
        season_ops=0.870,
    )
    assert out["batter_vs_starter_ops"] == pytest.approx(0.812)
    assert out["batter_vs_starter_platoon_factor"] == pytest.approx(0.812 / 0.870, rel=1e-3)


def test_emit_platoon_features_rhb_vs_rhp_emits_factor() -> None:
    """RHB facing RHP: vs Right OPS 0.701 / 0.870 → ~0.806 (clamped to 0.806 since > 0.80)."""

    out = mlb_advanced.emit_mlb_platoon_features(
        starter_pitch_hand="R",
        splits_payload=_splits_payload(),
        season_ops=0.870,
    )
    assert out["batter_vs_starter_ops"] == pytest.approx(0.701)
    # Raw ratio 0.806, just above the 0.80 clamp floor.
    assert out["batter_vs_starter_platoon_factor"] == pytest.approx(0.806, rel=1e-2)


def test_emit_platoon_features_clamp_floor() -> None:
    """A pathological split (very low vs-hand OPS) gets clamped at 0.80 so
    a 12-PA noisy split can't dominate the prediction."""

    payload = {
        "splits": [
            {"ops": "0.200", "_split_meta": {"split": {"code": "vl", "description": "vs Left"}}},
        ]
    }
    out = mlb_advanced.emit_mlb_platoon_features(
        starter_pitch_hand="L",
        splits_payload=payload,
        season_ops=0.870,
    )
    assert out["batter_vs_starter_platoon_factor"] == pytest.approx(0.80)


def test_emit_platoon_features_clamp_ceiling() -> None:
    """Symmetric ceiling at 1.20."""

    payload = {
        "splits": [
            {"ops": "1.500", "_split_meta": {"split": {"code": "vl", "description": "vs Left"}}},
        ]
    }
    out = mlb_advanced.emit_mlb_platoon_features(
        starter_pitch_hand="L",
        splits_payload=payload,
        season_ops=0.870,
    )
    assert out["batter_vs_starter_platoon_factor"] == pytest.approx(1.20)


def test_emit_platoon_features_returns_empty_when_pitch_hand_missing() -> None:
    assert mlb_advanced.emit_mlb_platoon_features(None, _splits_payload(), 0.870) == {}


def test_emit_platoon_features_returns_empty_when_splits_missing() -> None:
    assert mlb_advanced.emit_mlb_platoon_features("L", None, 0.870) == {}
    assert mlb_advanced.emit_mlb_platoon_features("L", {"splits": []}, 0.870) == {}


def test_emit_platoon_features_returns_empty_when_season_ops_missing() -> None:
    assert mlb_advanced.emit_mlb_platoon_features("L", _splits_payload(), None) == {}
    assert mlb_advanced.emit_mlb_platoon_features("L", _splits_payload(), 0) == {}


def test_emit_platoon_features_returns_empty_when_vs_hand_ops_unparseable() -> None:
    payload = {
        "splits": [
            {"ops": "not-a-number", "_split_meta": {"split": {"code": "vl", "description": "vs Left"}}},
        ]
    }
    assert mlb_advanced.emit_mlb_platoon_features("L", payload, 0.870) == {}


# -- extract_pitch_hand_from_lineup ------------------------------------------


def _lineup_payload(*, home_pitcher_id: int, home_hand: str, away_pitcher_id: int, away_hand: str) -> dict:
    return {
        "raw": {
            "dates": [
                {
                    "games": [
                        {
                            "teams": {
                                "home": {"probablePitcher": {"id": home_pitcher_id, "pitchHand": {"code": home_hand}}},
                                "away": {"probablePitcher": {"id": away_pitcher_id, "pitchHand": {"code": away_hand}}},
                            }
                        }
                    ]
                }
            ]
        }
    }


def test_extract_pitch_hand_finds_home_starter() -> None:
    payload = _lineup_payload(home_pitcher_id=111, home_hand="L", away_pitcher_id=222, away_hand="R")
    assert mlb_advanced.extract_pitch_hand_from_lineup(payload, "111") == "L"


def test_extract_pitch_hand_finds_away_starter() -> None:
    payload = _lineup_payload(home_pitcher_id=111, home_hand="L", away_pitcher_id=222, away_hand="R")
    assert mlb_advanced.extract_pitch_hand_from_lineup(payload, "222") == "R"


def test_extract_pitch_hand_returns_none_when_pitcher_not_found() -> None:
    payload = _lineup_payload(home_pitcher_id=111, home_hand="L", away_pitcher_id=222, away_hand="R")
    assert mlb_advanced.extract_pitch_hand_from_lineup(payload, "999") is None


def test_extract_pitch_hand_handles_missing_pitch_hand_block() -> None:
    payload = {
        "raw": {
            "dates": [
                {"games": [{"teams": {"home": {"probablePitcher": {"id": 111}}, "away": {}}}]}
            ]
        }
    }
    assert mlb_advanced.extract_pitch_hand_from_lineup(payload, "111") is None


def test_extract_pitch_hand_handles_empty_payload() -> None:
    assert mlb_advanced.extract_pitch_hand_from_lineup(None, "111") is None
    assert mlb_advanced.extract_pitch_hand_from_lineup({}, "111") is None
    assert mlb_advanced.extract_pitch_hand_from_lineup(_lineup_payload(home_pitcher_id=1, home_hand="L", away_pitcher_id=2, away_hand="R"), None) is None


# -- _mlb_batter_platoon_factor ----------------------------------------------


def test_batter_platoon_factor_returns_feature_value() -> None:
    assert _mlb_batter_platoon_factor({"batter_vs_starter_platoon_factor": 0.92}) == pytest.approx(0.92)


def test_batter_platoon_factor_returns_1_when_feature_missing() -> None:
    assert _mlb_batter_platoon_factor({}) == 1.0
    assert _mlb_batter_platoon_factor({"batter_vs_starter_platoon_factor": None}) == 1.0
    assert _mlb_batter_platoon_factor({"batter_vs_starter_platoon_factor": 0}) == 1.0


def test_batter_platoon_factor_clamped_when_features_carry_out_of_range() -> None:
    """Defensive — emit clamps to [0.80, 1.20] but the factor reader also
    runs through ``_clamp`` (which the heuristic_factors module enforces
    at ±15%). If a malformed feature slips in, the factor stays bounded."""

    # _clamp limits to [0.85, 1.15] in heuristic_factors. Anything tighter from
    # emit just passes through. Anything looser gets clipped here.
    assert _mlb_batter_platoon_factor({"batter_vs_starter_platoon_factor": 5.0}) <= 1.15
    assert _mlb_batter_platoon_factor({"batter_vs_starter_platoon_factor": 0.1}) >= 0.85


# -- _MLB_FACTORS_BY_STAT gating ---------------------------------------------


@pytest.mark.parametrize("stat", ["hits", "home_runs", "total_bases", "rbis", "runs"])
def test_platoon_factor_gated_on_offense_stats(stat: str) -> None:
    assert factor_applies("MLB", stat, "batter_platoon_factor")


@pytest.mark.parametrize("stat", ["strikeouts", "walks", "doubles", "triples"])
def test_platoon_factor_not_gated_on_non_offense_stats(stat: str) -> None:
    """Strikeouts/walks have pitcher-side factors that already encode handedness
    implicitly via FIP/xFIP. Doubles/triples are too rare for platoon to be
    a reliable signal."""

    assert not factor_applies("MLB", stat, "batter_platoon_factor")


def test_platoon_factor_factor_fns_wired() -> None:
    """``compute_advanced_factors`` looks up factor names in ``_MLB_FACTOR_FNS``.
    A name in ``_MLB_FACTORS_BY_STAT`` that's missing from the FNS map silently
    no-ops — guard against that drift."""

    from app.services.heuristic_factors import _MLB_FACTOR_FNS

    gated = {name for tup in _MLB_FACTORS_BY_STAT.values() for name in tup}
    assert "batter_platoon_factor" in gated
    assert "batter_platoon_factor" in _MLB_FACTOR_FNS
