"""Smarter NFL PR 8 — family registry + per-sport watchlist allowlist."""

from __future__ import annotations

from itertools import product

from sqlalchemy import literal, select

from app.services.model_families import (
    FAMILY_DEFINITIONS,
    FAMILY_DEFINITION_BY_KEY,
    parlay_family_key,
    single_family_key,
    single_family_sql_predicate,
)
from app.services.watchlist_coverage import (
    CURRENT_WATCHLIST_FAMILIES_BY_SPORT,
    CURRENT_WATCHLIST_MARKET_FAMILIES,
    current_families_for_sport,
)
from ml_features import DEFAULT_SERVE_FAMILY_KEYS


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


def test_active_single_registry_matches_default_training_families() -> None:
    active_single_keys = {
        definition.key
        for definition in FAMILY_DEFINITIONS
        if definition.scope == "single" and definition.study_track == "active"
    }
    assert active_single_keys == set(DEFAULT_SERVE_FAMILY_KEYS)


def test_single_family_key_routes_nfl() -> None:
    assert single_family_key("NFL", "player_prop") == "nfl_props"
    assert single_family_key("NFL", "winner") == "nfl_singles"
    assert single_family_key("NFL", "game_line") == "nfl_singles"


def test_single_family_sql_predicate_matches_python_mapper_full_cross_product(db_session) -> None:
    """The pre-cap SQL filter and in-Python family refinement cannot drift."""

    sports = (None, "", "NBA", "nba", "MLB", "WNBA", "NFL", "NHL", "unknown", " wnba")
    market_families = (None, "", "winner", "game_line", "player_prop", "PLAYER_PROP")
    combinations = list(product(sports, market_families))
    family_keys = {
        single_family_key(sport, market_family)
        for sport, market_family in combinations
    } | {
        definition.key
        for definition in FAMILY_DEFINITIONS
        if definition.scope == "single"
    }

    for sport, market_family in combinations:
        expected_key = single_family_key(sport, market_family)
        for family_key in family_keys:
            matched = db_session.scalar(
                select(
                    single_family_sql_predicate(
                        family_key,
                        sport_column=literal(sport),
                        market_family_column=literal(market_family),
                    )
                )
            )
            assert bool(matched) is (family_key == expected_key), (
                sport,
                market_family,
                family_key,
                expected_key,
            )


def test_parlay_family_key_routes_nfl_not_mixed() -> None:
    """The config.py warning: a sport in parlay_enabled_sports without
    its own family pollutes mixed_parlay_* calibration. NFL combos must
    route to nfl_parlay_* BEFORE the PR 10b gate flips."""
    assert parlay_family_key(2, ["NFL"]) == "nfl_parlay_2leg"
    assert parlay_family_key(3, ["NFL", "NFL"]) == "nfl_parlay_3leg"
    assert parlay_family_key(2, ["NFL", "NBA"]) == "mixed_parlay_2leg"


def test_family_allowlist_defaults_preserve_behavior() -> None:
    for sport in ("NBA", "MLB", "WNBA", "NFL", None):
        assert current_families_for_sport(sport) == CURRENT_WATCHLIST_MARKET_FAMILIES


def test_nfl_full_family_set_live() -> None:
    """PR 10b removed the PR 10a lines-first override — NFL props are
    live. The override map stays as the operator's staged-rollout knob."""
    assert "NFL" not in CURRENT_WATCHLIST_FAMILIES_BY_SPORT
    assert current_families_for_sport("NFL") == CURRENT_WATCHLIST_MARKET_FAMILIES


def test_parlay_enabled_sports_include_nfl() -> None:
    from app.config import Settings

    settings = Settings()
    assert "NFL" in settings.parlay_enabled_sports
    assert "WNBA" not in settings.parlay_enabled_sports  # still no wnba families


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
