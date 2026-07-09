"""Smarter NFL PR 8 — family registry + per-sport watchlist allowlist."""

from __future__ import annotations

from app.services.model_families import (
    FAMILY_DEFINITION_BY_KEY,
    parlay_family_key,
    single_family_key,
)
from app.services.watchlist_coverage import (
    CURRENT_WATCHLIST_FAMILIES_BY_SPORT,
    CURRENT_WATCHLIST_MARKET_FAMILIES,
    current_families_for_sport,
)


def test_nfl_family_definitions_registered() -> None:
    singles = FAMILY_DEFINITION_BY_KEY["nfl_singles"]
    assert singles.sport_scope == "NFL" and singles.study_track == "active"
    props = FAMILY_DEFINITION_BY_KEY["nfl_props"]
    assert props.study_track == "active"
    two_leg = FAMILY_DEFINITION_BY_KEY["nfl_parlay_2leg"]
    assert two_leg.scope == "parlay" and two_leg.leg_count == 2
    assert two_leg.study_track == "active"
    # 3-leg pinned heuristic_only — settled volume never clears the gate.
    assert FAMILY_DEFINITION_BY_KEY["nfl_parlay_3leg"].study_track == "heuristic_only"


def test_single_family_key_routes_nfl() -> None:
    assert single_family_key("NFL", "player_prop") == "nfl_props"
    assert single_family_key("NFL", "winner") == "nfl_singles"
    assert single_family_key("NFL", "game_line") == "nfl_singles"


def test_parlay_family_key_routes_nfl_not_mixed() -> None:
    """The config.py warning: a sport in parlay_enabled_sports without
    its own family pollutes mixed_parlay_* calibration. NFL combos must
    route to nfl_parlay_* BEFORE the PR 10b gate flips."""
    assert parlay_family_key(2, ["NFL"]) == "nfl_parlay_2leg"
    assert parlay_family_key(3, ["NFL", "NFL"]) == "nfl_parlay_3leg"
    assert parlay_family_key(2, ["NFL", "NBA"]) == "mixed_parlay_2leg"


def test_family_allowlist_defaults_preserve_behavior() -> None:
    # PR 10a: NFL runs lines-first; every other sport keeps the full set.
    for sport in ("NBA", "MLB", "WNBA", None):
        assert current_families_for_sport(sport) == CURRENT_WATCHLIST_MARKET_FAMILIES


def test_nfl_lines_first_allowlist_active() -> None:
    """PR 10a: NFL winner + game_line surface on the watchlist; props
    stay research-mode until PR 10b removes this entry."""
    assert CURRENT_WATCHLIST_FAMILIES_BY_SPORT["NFL"] == {"winner", "game_line"}
    assert current_families_for_sport("nfl") == {"winner", "game_line"}
    assert "player_prop" not in current_families_for_sport("NFL")


def test_current_watchlist_sports_include_nfl() -> None:
    from app.services.watchlist_coverage import CURRENT_WATCHLIST_SPORTS

    assert "NFL" in CURRENT_WATCHLIST_SPORTS


def test_current_slate_sports_derived_from_watchlist_constant() -> None:
    """PR 10a killed the triplicated hardcoded sport lists — the slate
    list must track CURRENT_WATCHLIST_SPORTS automatically."""
    from app.services.ingestion.cycles import _current_slate_sports
    from app.services.watchlist_coverage import CURRENT_WATCHLIST_SPORTS

    assert set(_current_slate_sports()) == set(CURRENT_WATCHLIST_SPORTS)
    assert _current_slate_sports() == sorted(_current_slate_sports())
