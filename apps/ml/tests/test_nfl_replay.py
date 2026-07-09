"""Smarter NFL PR 9 — replay harness regression: the leakage guard is
the load-bearing test (week-N pricing must never read week-N stats)."""

from __future__ import annotations

from ml.nfl_replay import (
    build_ratings_through_week,
    devig_moneyline,
    ece,
    internal_margin,
    prior_season_ratings,
    run_winner_and_spread_tracks,
)


def _team_row(team: str, week: int, epa: str, opponent: str = "OPP") -> dict:
    return {
        "team": team, "opponent_team": opponent, "week": str(week),
        "season_type": "REG", "attempts": "35", "carries": "25",
        "sacks_suffered": "2", "passing_epa": epa, "rushing_epa": "0",
    }


def test_ratings_walk_forward_excludes_current_week() -> None:
    rows = [
        _team_row("KC", 1, "10.0"),
        _team_row("KC", 2, "10.0"),
        _team_row("KC", 3, "99.0"),  # must NOT leak into week-3 pricing
    ]
    ratings = build_ratings_through_week(rows, max_week_exclusive=3)
    assert ratings["KC"]["games"] == 2.0
    with_leak = build_ratings_through_week(rows, max_week_exclusive=4)
    assert with_leak["KC"]["rating"] > ratings["KC"]["rating"]


def test_devig_moneyline() -> None:
    p = devig_moneyline(-160.0, 135.0)
    assert p is not None and 0.58 < p < 0.65
    assert devig_moneyline(None, 135.0) is None


def test_internal_margin_shrinks_to_prior_early() -> None:
    ratings = {"KC": {"rating": 10.0, "games": 1.0}}
    priors = {"KC": 2.0, "BUF": 0.0}
    # 1 of 4 shrink games → 0.25*10 + 0.75*2 = 4.0, + HFA 1.7
    assert abs(internal_margin("KC", "BUF", ratings, priors) - 5.7) < 1e-9


def test_smoke_two_game_season() -> None:
    games = [
        {"season": "2024", "game_type": "REG", "week": "1", "home_team": "KC",
         "away_team": "BUF", "home_score": "27", "away_score": "20", "result": "7",
         "total": "47", "spread_line": "-3.0", "total_line": "46.5",
         "home_moneyline": "-160", "away_moneyline": "135"},
        {"season": "2025", "game_type": "REG", "week": "1", "home_team": "KC",
         "away_team": "BUF", "home_score": "24", "away_score": "21", "result": "3",
         "total": "45", "spread_line": "3.0", "total_line": "47.0",
         "home_moneyline": "-150", "away_moneyline": "130"},
    ]
    team_rows = [_team_row("KC", 1, "5.0", "BUF"), _team_row("BUF", 1, "0.0", "KC")]
    report = run_winner_and_spread_tracks(games, team_rows, 2025)
    assert report["games_evaluated"] == 1
    assert report["winner_blended_production"]["n"] == 1
    assert report["spread_ladder"]["n"] == 6  # one game × six ladder rungs


def test_ece_math() -> None:
    perfectly_calibrated = [(0.75, 1.0), (0.75, 1.0), (0.75, 1.0), (0.75, 0.0)]
    assert ece(perfectly_calibrated) < 1e-9
    badly_calibrated = [(0.9, 0.0)] * 10
    assert ece(badly_calibrated) > 0.8
