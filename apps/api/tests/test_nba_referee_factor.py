"""Tests for Smarter #13 phase 2d — heuristic factor on points / fouls
from NBA referee tendencies.

Phases 2a/2b/2c shipped the data pipeline (assignments → tendencies →
emitter). Phase 2d (this test target) consumes the emitted features
in ``heuristic_factors._nba_referee_factor`` and threads the
``emit_nba_referee_features`` call into ``_score_player_prop``'s NBA
branch so the factor actually fires on real games.

The factor envelope is ±5% — same shape as the existing workload /
opp_def factors. League-average fouls per game is ~42; a tight crew
(>42) boosts points (more FT trips); a loose crew (<42) suppresses.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.services.heuristic_factors import (
    _FACTOR_CLAMP_HIGH,
    _FACTOR_CLAMP_LOW,
    compute_advanced_factors,
)


def _features(
    *,
    avg_fouls_per_game: float | None = None,
    avg_fta_per_game: float | None = None,
    crew_count: float | None = None,
    data_complete: float | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if avg_fouls_per_game is not None:
        out["referee_avg_fouls_per_game"] = avg_fouls_per_game
    if avg_fta_per_game is not None:
        out["referee_avg_fta_per_game"] = avg_fta_per_game
    if crew_count is not None:
        out["referee_crew_count"] = crew_count
    if data_complete is not None:
        out["referee_data_complete"] = data_complete
    return out


# -- Pure factor -------------------------------------------------------


def test_nba_referee_factor_neutral_at_league_average() -> None:
    """League average is ~42 fouls per game. Neutral input must
    produce factor=1.0, which the no-op filter then removes from the
    output dict."""
    factors = compute_advanced_factors(
        "NBA", "points",
        _features(avg_fouls_per_game=42.0, data_complete=1.0),
    )
    assert "nba_referee_factor" not in factors


def test_nba_referee_factor_boosts_above_league_average() -> None:
    """A tight-calling crew (>42 fpg) means more FT trips → more
    total points scored. Factor should be above 1.0."""
    factors = compute_advanced_factors(
        "NBA", "points",
        _features(avg_fouls_per_game=47.0, data_complete=1.0),
    )
    # +5 fouls × 0.005 per foul = +0.025 → 1.025
    assert factors["nba_referee_factor"] == pytest.approx(1.025)


def test_nba_referee_factor_suppresses_below_league_average() -> None:
    """A loose-calling crew suppresses scoring."""
    factors = compute_advanced_factors(
        "NBA", "points",
        _features(avg_fouls_per_game=37.0, data_complete=1.0),
    )
    # -5 fouls × 0.005 = -0.025 → 0.975
    assert factors["nba_referee_factor"] == pytest.approx(0.975)


def test_nba_referee_factor_clamps_at_envelope() -> None:
    """Extreme tendency values cap at ±5% so a misparsed BR row
    can't blow up the projection."""
    high = compute_advanced_factors(
        "NBA", "points",
        _features(avg_fouls_per_game=80.0, data_complete=1.0),  # crazy tight
    )
    low = compute_advanced_factors(
        "NBA", "points",
        _features(avg_fouls_per_game=10.0, data_complete=1.0),  # crazy loose
    )
    assert high["nba_referee_factor"] == pytest.approx(1.05)
    assert low["nba_referee_factor"] == pytest.approx(0.95)


def test_nba_referee_factor_skipped_when_data_complete_zero() -> None:
    """``referee_data_complete == 0.0`` (only 1 of 3 crew matched)
    indicates a low-confidence signal — the factor must NOT fire."""
    factors = compute_advanced_factors(
        "NBA", "points",
        _features(avg_fouls_per_game=47.0, data_complete=0.0),
    )
    assert "nba_referee_factor" not in factors


def test_nba_referee_factor_skipped_when_fouls_per_game_missing() -> None:
    """No usable foul data → no factor (the emitter shouldn't have
    populated the features dict in the first place, but defensive)."""
    factors = compute_advanced_factors(
        "NBA", "points",
        _features(data_complete=1.0),
    )
    assert "nba_referee_factor" not in factors


def test_nba_referee_factor_skipped_when_data_complete_missing() -> None:
    """Missing data_complete flag (legacy / pre-emitter row) treated
    as not-complete. Defensive — never apply factor without the
    explicit completeness signal."""
    factors = compute_advanced_factors(
        "NBA", "points",
        _features(avg_fouls_per_game=47.0),
    )
    assert "nba_referee_factor" not in factors


def test_nba_referee_factor_skipped_for_non_numeric_fouls() -> None:
    """Defensive against bad cache rows that somehow serialize
    non-numeric fouls into features."""
    factors = compute_advanced_factors(
        "NBA", "points",
        {"referee_avg_fouls_per_game": "not a number", "referee_data_complete": 1.0},
    )
    assert "nba_referee_factor" not in factors


# -- Stat-key gating ---------------------------------------------------


def test_nba_referee_factor_applied_to_points_stat() -> None:
    factors = compute_advanced_factors(
        "NBA", "points",
        _features(avg_fouls_per_game=47.0, data_complete=1.0),
    )
    assert "nba_referee_factor" in factors


@pytest.mark.parametrize(
    "stat_key", ["points_assists", "points_rebounds", "points_rebounds_assists"],
)
def test_nba_referee_factor_applied_to_points_composites(stat_key: str) -> None:
    """Composite stats that include points (PA, PR, PRA) inherit the
    referee tilt. Pure non-points composites (RA) don't."""
    factors = compute_advanced_factors(
        "NBA", stat_key,
        _features(avg_fouls_per_game=47.0, data_complete=1.0),
    )
    assert "nba_referee_factor" in factors


@pytest.mark.parametrize(
    "stat_key",
    ["rebounds", "assists", "steals", "blocks", "turnovers", "rebounds_assists"],
)
def test_nba_referee_factor_not_applied_to_non_scoring_stats(stat_key: str) -> None:
    """Defensive stats (steals/blocks), turnovers, and pure
    rebounds/assists don't have a clear theoretical link to ref
    tendency — gated out so a ref-tight game doesn't accidentally
    suppress unrelated counting stats."""
    factors = compute_advanced_factors(
        "NBA", stat_key,
        _features(avg_fouls_per_game=47.0, data_complete=1.0),
    )
    assert "nba_referee_factor" not in factors


# -- Scoring wiring ----------------------------------------------------


def test_score_player_prop_imports_nba_referee_emitter() -> None:
    """Confirm scoring.py:_score_player_prop imports both
    ``emit_nba_referee_features`` and the loaders. A future refactor
    that drops these breaks the wiring without obvious test signal."""
    from app.services import scoring

    source = inspect.getsource(scoring._score_player_prop)
    assert "from app.services.nba_referee_emit import emit_nba_referee_features" in source
    assert "load_nba_referee_assignments" in source
    assert "load_nba_referee_tendencies" in source
    # Both loaders MUST run with allow_network=False (scoring stays
    # off the network — the daily refresh job populates the caches).
    assert "allow_network=False" in source
    assert "load_nba_referee_tendencies(" in source


def test_score_player_prop_uses_event_date_in_eastern_time_for_assignments() -> None:
    """Codex review round 1 P2: a 10pm PT NBA game starts at 05:00
    UTC the next day; without ET-conversion the scorer would read
    the wrong daily assignment row. The wiring derives ``target_date``
    from ``event.starts_at.astimezone(America/New_York).date()`` so
    the assignments cache lookup uses the NBA game date (US/Eastern)
    rather than UTC-today."""
    from app.services import scoring

    source = inspect.getsource(scoring._score_player_prop)
    assert "America/New_York" in source
    assert "target_date=" in source


def test_score_player_prop_coerces_naive_starts_at_to_utc_before_et_conversion() -> None:
    """Codex review round 2 P2: SQLite returns ``DateTime(timezone=True)``
    values as naive UTC. ``astimezone`` on a naive datetime treats
    it as the host's local time, which can shift the assignment
    cache date for games near the UTC boundary. The wiring must
    coerce naive starts_at to UTC before the ET conversion."""
    from app.services import scoring

    source = inspect.getsource(scoring._score_player_prop)
    # Defensive coercion present.
    assert "tzinfo is None" in source
    assert "tzinfo=timezone.utc" in source
