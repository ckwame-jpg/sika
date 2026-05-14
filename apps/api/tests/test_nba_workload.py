"""Tests for Smarter #11 — NBA workload features and heuristic factor.

Covers:
- `emit_nba_workload_features` (advanced_stats.py) — pure-function feature
  emitter reading the in-memory NBA game-log list.
- `_nba_workload_factor` (heuristic_factors.py) — top-quartile-MPG
  suppression / below-median rest boost.
- Per-stat gating (`_NBA_FACTORS_BY_STAT`) plus the FNS drift guard.
- Scoring-kernel integration: when lineup is confirmed AND MPG is
  top-quartile, ``_single_scoring_adjustments`` adds
  ``workload_top_quartile_uncertainty`` to ``missing_context``. Gated to
  NBA props only (codex Pattern 9).
"""

from typing import Any
from unittest.mock import MagicMock

from app.services.advanced_stats import emit_nba_workload_features
from app.services.heuristic_factors import (
    _NBA_FACTOR_FNS,
    _NBA_FACTORS_BY_STAT,
    _nba_workload_factor,
    compute_advanced_factors,
    factor_applies,
)


def _game_log(minutes: float | None, *, points: float = 20.0) -> dict[str, Any]:
    """Build a single NBA game-log entry shaped like
    ``stats_query._build_nba_game_logs`` output."""
    return {
        "sport_key": "NBA",
        "game_id": "evt",
        "game_date": "2026-05-13T00:00:00+00:00",
        "location": "home",
        "opponent": "X",
        "raw_metrics": {"minutes": minutes or 0.0, "points": points},
        "metrics": {"minutes": minutes, "points": points},
    }


# -- emit_nba_workload_features ------------------------------------------


def test_emit_workload_empty_returns_empty_dict() -> None:
    assert emit_nba_workload_features(None) == {}
    assert emit_nba_workload_features([]) == {}


def test_emit_workload_returns_mean_streak_and_complete_marker() -> None:
    # Five games of 30 minutes each → mean 30, streak 5.
    game_logs = [_game_log(30.0) for _ in range(5)]
    out = emit_nba_workload_features(game_logs)
    assert out["recent_workload_minutes_per_game"] == 30.0
    assert out["consecutive_games_played"] == 5.0
    assert out["workload_data_complete"] == 1.0


def test_emit_workload_dnp_breaks_consecutive_streak() -> None:
    # Most-recent first: [played, played, DNP, played, played]
    # Streak counts from most-recent backwards and breaks at the DNP.
    game_logs = [
        _game_log(32.0),
        _game_log(28.0),
        _game_log(0.0),     # DNP — minutes == 0
        _game_log(30.0),
        _game_log(25.0),
    ]
    out = emit_nba_workload_features(game_logs)
    assert out["consecutive_games_played"] == 2.0
    # Mean over the 4 non-DNP games (the DNP is filtered out of MPG).
    assert out["recent_workload_minutes_per_game"] == round(
        (32.0 + 28.0 + 30.0 + 25.0) / 4, 1
    )


def test_emit_workload_skips_none_minutes_like_dnp() -> None:
    game_logs = [
        _game_log(34.0),
        _game_log(None),    # Missing minutes — treat as DNP.
        _game_log(31.0),
    ]
    out = emit_nba_workload_features(game_logs)
    assert out["consecutive_games_played"] == 1.0
    assert out["recent_workload_minutes_per_game"] == round((34.0 + 31.0) / 2, 1)


def test_emit_workload_all_dnp_returns_empty_no_signal() -> None:
    # If no game in the window has playable minutes, the emitter returns
    # {} so consumers don't see a "0 MPG" false rest signal.
    game_logs = [_game_log(0.0) for _ in range(5)]
    assert emit_nba_workload_features(game_logs) == {}


def test_emit_workload_respects_window_games_override() -> None:
    # 10 games but window = 3 → only the most recent 3 contribute to MPG.
    game_logs = [_game_log(40.0)] * 3 + [_game_log(20.0)] * 7
    out = emit_nba_workload_features(game_logs, window_games=3)
    assert out["recent_workload_minutes_per_game"] == 40.0


def test_emit_workload_rookie_with_under_window_games_uses_what_exists() -> None:
    # 2 games available, default window=5 → mean over the 2 we have.
    game_logs = [_game_log(36.0), _game_log(34.0)]
    out = emit_nba_workload_features(game_logs)
    assert out["recent_workload_minutes_per_game"] == 35.0
    assert out["consecutive_games_played"] == 2.0


def test_emit_workload_streak_walks_entire_log_not_just_window() -> None:
    # 12 games of 30 MPG. Streak should count all 12, not be clipped to 5.
    game_logs = [_game_log(30.0) for _ in range(12)]
    out = emit_nba_workload_features(game_logs)
    assert out["consecutive_games_played"] == 12.0


# -- _nba_workload_factor ------------------------------------------------


def test_nba_workload_factor_top_quartile_suppresses() -> None:
    assert _nba_workload_factor({"recent_workload_minutes_per_game": 36.0}) == 0.96


def test_nba_workload_factor_threshold_inclusive_at_34_mpg() -> None:
    # Exactly 34.0 → suppression fires (boundary inclusive).
    assert _nba_workload_factor({"recent_workload_minutes_per_game": 34.0}) == 0.96


def test_nba_workload_factor_below_median_boosts() -> None:
    assert _nba_workload_factor({"recent_workload_minutes_per_game": 20.0}) == 1.03


def test_nba_workload_factor_threshold_inclusive_at_22_mpg() -> None:
    # Exactly 22.0 → boost fires (boundary inclusive).
    assert _nba_workload_factor({"recent_workload_minutes_per_game": 22.0}) == 1.03


def test_nba_workload_factor_in_deadband_returns_unity() -> None:
    # 27 MPG is squarely between the boundaries — no signal.
    assert _nba_workload_factor({"recent_workload_minutes_per_game": 27.0}) == 1.0


def test_nba_workload_factor_missing_feature_returns_unity() -> None:
    assert _nba_workload_factor({}) == 1.0
    assert _nba_workload_factor({"recent_workload_minutes_per_game": None}) == 1.0
    # Wrong type — defensive return-1.0 rather than raise.
    assert _nba_workload_factor({"recent_workload_minutes_per_game": "36"}) == 1.0


def test_nba_workload_factor_rejects_bool_inputs() -> None:
    # ``bool`` is a subclass of ``int`` in Python so a stray ``True`` would
    # otherwise be coerced to MPG=1, falsely firing the ≤22 rest-boost
    # branch. Reject explicitly.
    assert _nba_workload_factor({"recent_workload_minutes_per_game": True}) == 1.0
    assert _nba_workload_factor({"recent_workload_minutes_per_game": False}) == 1.0


# -- per-stat gating + drift guard ---------------------------------------


def test_workload_factor_gated_on_fatigue_sensitive_stats() -> None:
    for stat in (
        "points",
        "rebounds",
        "assists",
        "made_threes",
        "three_points_made",
        "field_goals_made",
        "points_assists",
        "points_rebounds",
        "rebounds_assists",
        "points_rebounds_assists",
    ):
        assert factor_applies("NBA", stat, "workload_factor"), (
            f"workload_factor should be gated on {stat}"
        )


def test_workload_factor_excluded_from_defensive_and_turnover_stats() -> None:
    # Defensive stats and turnovers aren't materially fatigue-suppressed
    # — keep the factor off so we don't apply it where it doesn't make
    # physical sense (codex Pattern 3 catch).
    for stat in ("steals", "blocks", "turnovers"):
        assert not factor_applies("NBA", stat, "workload_factor"), (
            f"workload_factor should NOT be gated on {stat}"
        )


def test_workload_factor_factor_fns_wired() -> None:
    """Drift guard: a factor name in ``_NBA_FACTORS_BY_STAT`` that is
    missing from ``_NBA_FACTOR_FNS`` silently no-ops. Mirror of the
    canonical platoon-factor wiring test from Smarter #5."""
    gated = {name for tup in _NBA_FACTORS_BY_STAT.values() for name in tup}
    assert "workload_factor" in gated
    assert "workload_factor" in _NBA_FACTOR_FNS


def test_compute_advanced_factors_emits_workload_when_mpg_top_quartile() -> None:
    out = compute_advanced_factors(
        "NBA",
        "points",
        {"recent_workload_minutes_per_game": 36.0},
    )
    assert out.get("workload_factor") == 0.96


def test_compute_advanced_factors_omits_workload_in_deadband() -> None:
    # The 1e-4 prune in compute_advanced_factors drops no-op factors.
    out = compute_advanced_factors(
        "NBA",
        "points",
        {"recent_workload_minutes_per_game": 27.0},
    )
    assert "workload_factor" not in out


def test_compute_advanced_factors_skips_workload_for_turnovers() -> None:
    out = compute_advanced_factors(
        "NBA",
        "turnovers",
        {"recent_workload_minutes_per_game": 36.0},
    )
    assert "workload_factor" not in out


# -- scoring integration: workload_top_quartile_uncertainty ---------------


def _adjust(features: dict[str, Any], *, family_key: str = "nba_props"):
    from app.services.scoring import _single_scoring_adjustments

    db = MagicMock()
    event = MagicMock()
    event.starts_at = None
    metadata = {
        "copilot_requires_lineup": True,
        "copilot_market_family": "player_prop",
    }
    base_features = {
        "has_team_context": True,
        "has_opponent_context": True,
        "lineup_data_complete": 1.0,
        "player_in_starting_lineup": 1.0,
    }
    base_features.update(features)
    _, diagnostics = _single_scoring_adjustments(
        db,
        family_key=family_key,
        event=event,
        market=None,
        snapshot=None,
        metadata=metadata,
        features=base_features,
        probability_yes=0.5,
        base_confidence=0.7,
        left=None,
        right=None,
    )
    return diagnostics


def test_scoring_appends_workload_uncertainty_when_lineup_confirmed_and_mpg_top_quartile() -> None:
    diagnostics = _adjust({"recent_workload_minutes_per_game": 36.0})
    assert "workload_top_quartile_uncertainty" in diagnostics.get("missing_context", [])


def test_scoring_omits_workload_uncertainty_when_mpg_below_threshold() -> None:
    diagnostics = _adjust({"recent_workload_minutes_per_game": 28.0})
    assert "workload_top_quartile_uncertainty" not in diagnostics.get("missing_context", [])


def test_scoring_omits_workload_uncertainty_when_no_workload_feature() -> None:
    diagnostics = _adjust({})
    assert "workload_top_quartile_uncertainty" not in diagnostics.get("missing_context", [])


def test_scoring_omits_workload_uncertainty_for_mlb_props() -> None:
    """Codex Pattern 9 — the workload-uncertainty check is NBA-only by
    family_key gate. A stray ``recent_workload_minutes_per_game`` on an
    MLB row (which shouldn't happen by structure, but check anyway) must
    not trip the gate."""
    diagnostics = _adjust(
        {"recent_workload_minutes_per_game": 36.0},
        family_key="mlb_props",
    )
    assert "workload_top_quartile_uncertainty" not in diagnostics.get("missing_context", [])


def test_scoring_omits_workload_uncertainty_when_lineup_not_yet_confirmed() -> None:
    """If lineup data hasn't arrived yet, the existing ``lineup_confirmation``
    missing-context entry covers the uncertainty. The workload-uncertainty
    entry is for the additional risk that fires *after* confirmation."""
    from app.services.scoring import _single_scoring_adjustments

    db = MagicMock()
    event = MagicMock()
    event.starts_at = None
    metadata = {
        "copilot_requires_lineup": True,
        "copilot_market_family": "player_prop",
    }
    features = {
        "has_team_context": True,
        "has_opponent_context": True,
        # Note: no lineup_data_complete or player_in_starting_lineup.
        "recent_workload_minutes_per_game": 36.0,
    }
    _, diagnostics = _single_scoring_adjustments(
        db,
        family_key="nba_props",
        event=event,
        market=None,
        snapshot=None,
        metadata=metadata,
        features=features,
        probability_yes=0.5,
        base_confidence=0.7,
        left=None,
        right=None,
    )
    missing = diagnostics.get("missing_context", [])
    assert "lineup_confirmation" in missing
    assert "workload_top_quartile_uncertainty" not in missing


def test_scoring_omits_workload_uncertainty_when_player_scratched() -> None:
    """If lineup is confirmed AND player is NOT in lineup, the scratch
    suppression already drops the recommendation. The workload-uncertainty
    entry is redundant there — keep it gated only on the confirmed-IN-lineup
    case."""
    from app.services.scoring import _single_scoring_adjustments

    db = MagicMock()
    event = MagicMock()
    event.starts_at = None
    metadata = {
        "copilot_requires_lineup": True,
        "copilot_market_family": "player_prop",
    }
    features = {
        "has_team_context": True,
        "has_opponent_context": True,
        "lineup_data_complete": 1.0,
        "player_in_starting_lineup": 0.0,  # scratch
        "recent_workload_minutes_per_game": 36.0,
    }
    _, diagnostics = _single_scoring_adjustments(
        db,
        family_key="nba_props",
        event=event,
        market=None,
        snapshot=None,
        metadata=metadata,
        features=features,
        probability_yes=0.5,
        base_confidence=0.7,
        left=None,
        right=None,
    )
    assert "workload_top_quartile_uncertainty" not in diagnostics.get("missing_context", [])
    # Scratch path still fires.
    assert diagnostics.get("lineup_suppression_reason") == "player_not_in_starting_lineup"
