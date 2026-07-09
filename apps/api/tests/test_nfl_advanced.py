"""Smarter NFL PR 3 — nfl_advanced service regression.

Covers the ratings math (offense EPA/play + opponent-join defense +
schedule-derived points), the daily refresh job body's upsert flow, the
cache-only read loaders' hit/stale/miss statuses, stadium lookups, and
the weather loader's dome short-circuit + cache write.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import NflTeamRatingCache, NflWeatherCache
from app.services.nfl_advanced import (
    compute_nfl_team_ratings,
    load_nfl_depth_chart,
    load_nfl_official_injuries,
    load_nfl_schedule,
    load_nfl_snap_counts,
    load_nfl_team_ratings,
    load_nfl_weather,
    load_nfl_weekly_stats,
    nfl_stadium_info,
    nfl_team_abbr_for_name,
    refresh_nfl_data,
)


NOW = datetime(2025, 11, 6, 12, 0, tzinfo=timezone.utc)


class _StubNflverse:
    """Two-team, two-game fixture season (KC vs BUF home-and-home)."""

    def fetch_weekly_player_stats(self, season: int) -> list[dict[str, Any]]:
        return [
            {"player_id": "00-0033873", "player_display_name": "Patrick Mahomes",
             "position": "QB", "team": "KC", "week": "9", "season_type": "REG",
             "passing_yards": "290", "passing_epa": "8.1"},
            {"player_id": "00-0036322", "player_display_name": "Justin Jefferson",
             "position": "WR", "team": "MIN", "week": "10", "season_type": "REG",
             "receiving_yards": "101", "receptions": "8", "targets": "11"},
        ]

    def fetch_snap_counts(self, season: int) -> list[dict[str, Any]]:
        return [
            {"player": "Patrick Mahomes", "pfr_player_id": "MahoPa00", "team": "KC",
             "week": "9", "offense_snaps": "68", "offense_pct": "100.0"},
            {"player": "Patrick Mahomes", "pfr_player_id": "MahoPa00", "team": "KC",
             "week": "10", "offense_snaps": "61", "offense_pct": "98.0"},
        ]

    def fetch_latest_depth_charts(self, season: int) -> list[dict[str, Any]]:
        return [
            {"team": "KC", "player_name": "Patrick Mahomes", "espn_id": "3139477",
             "gsis_id": "00-0033873", "pos_abb": "QB", "pos_rank": "1", "dt": "2025-11-05"},
            {"team": "BUF", "player_name": "Josh Allen", "espn_id": "3918298",
             "gsis_id": "00-0034857", "pos_abb": "QB", "pos_rank": "1", "dt": "2025-11-05"},
        ]

    def fetch_official_injuries(self, season: int) -> list[dict[str, Any]]:
        return [
            {"season": str(season), "week": "10", "team": "KC", "gsis_id": "00-0033873",
             "full_name": "Patrick Mahomes", "position": "QB",
             "report_status": "Questionable", "practice_status": "Limited"},
        ]

    def fetch_team_week_stats(self, season: int) -> list[dict[str, Any]]:
        return _fixture_team_week_rows()

    def fetch_games(self, seasons=None) -> list[dict[str, Any]]:
        return _fixture_games_rows()


def _fixture_team_week_rows() -> list[dict[str, Any]]:
    # KC offense strong (+10 EPA over 60 plays/gm), BUF offense flat.
    return [
        {"team": "KC", "opponent_team": "BUF", "game_id": "2025_09_BUF_KC", "week": "9",
         "season_type": "REG", "attempts": "35", "carries": "22", "sacks_suffered": "3",
         "passing_epa": "8.0", "rushing_epa": "2.0"},
        {"team": "BUF", "opponent_team": "KC", "game_id": "2025_09_BUF_KC", "week": "9",
         "season_type": "REG", "attempts": "38", "carries": "20", "sacks_suffered": "2",
         "passing_epa": "0.0", "rushing_epa": "0.0"},
        {"team": "KC", "opponent_team": "BUF", "game_id": "2025_10_KC_BUF", "week": "10",
         "season_type": "REG", "attempts": "33", "carries": "24", "sacks_suffered": "3",
         "passing_epa": "7.0", "rushing_epa": "3.0"},
        {"team": "BUF", "opponent_team": "KC", "game_id": "2025_10_KC_BUF", "week": "10",
         "season_type": "REG", "attempts": "40", "carries": "18", "sacks_suffered": "2",
         "passing_epa": "1.0", "rushing_epa": "-1.0"},
        # Postseason row must be ignored.
        {"team": "KC", "opponent_team": "BUF", "game_id": "2025_22_BUF_KC", "week": "22",
         "season_type": "POST", "attempts": "99", "carries": "99", "sacks_suffered": "0",
         "passing_epa": "99.0", "rushing_epa": "99.0"},
    ]


def _fixture_games_rows() -> list[dict[str, Any]]:
    return [
        {"game_id": "2025_09_BUF_KC", "season": "2025", "game_type": "REG", "week": "9",
         "away_team": "BUF", "home_team": "KC", "away_score": "20", "home_score": "27",
         "result": "7", "total": "47", "spread_line": "-3.0", "total_line": "46.5",
         "home_moneyline": "-160", "away_moneyline": "135", "home_rest": "7", "away_rest": "7"},
        {"game_id": "2025_10_KC_BUF", "season": "2025", "game_type": "REG", "week": "10",
         "away_team": "KC", "home_team": "BUF", "away_score": "24", "home_score": "21",
         "result": "-3", "total": "45", "spread_line": "2.5", "total_line": "47.0",
         "home_moneyline": "120", "away_moneyline": "-140", "home_rest": "7", "away_rest": "7"},
        # Unplayed future game — must not count toward PF/PA.
        {"game_id": "2025_11_KC_DEN", "season": "2025", "game_type": "REG", "week": "11",
         "away_team": "KC", "home_team": "DEN", "away_score": "", "home_score": "",
         "result": "", "total": "", "spread_line": "-7.5", "total_line": "44.5",
         "home_moneyline": "300", "away_moneyline": "-380", "home_rest": "7", "away_rest": "7"},
        # Prior season row — ignored for 2025 ratings.
        {"game_id": "2024_01_BAL_KC", "season": "2024", "game_type": "REG", "week": "1",
         "away_team": "BAL", "home_team": "KC", "away_score": "20", "home_score": "27",
         "result": "7", "total": "47", "spread_line": "-3.0", "total_line": "46.5",
         "home_moneyline": "-160", "away_moneyline": "135", "home_rest": "7", "away_rest": "7"},
    ]


# -- Ratings math ------------------------------------------------------------

def test_compute_team_ratings_offense_defense_and_points() -> None:
    ratings = compute_nfl_team_ratings(
        _fixture_team_week_rows(), _fixture_games_rows(), season=2025
    )
    assert ratings["through_week"] == 10  # POST row ignored
    kc = ratings["teams"]["KC"]
    buf = ratings["teams"]["BUF"]
    # KC offense: 20 EPA over 120 plays (payload rounds to 5 decimals).
    assert abs(kc["off_epa_per_play"] - 20.0 / 120.0) < 1e-4
    # KC defense allowed BUF's 0 EPA over 120 plays.
    assert abs(kc["def_epa_per_play_allowed"] - 0.0) < 1e-4
    # BUF defense allowed KC's 20 EPA over 120 plays.
    assert abs(buf["def_epa_per_play_allowed"] - 20.0 / 120.0) < 1e-4
    assert kc["net_epa_per_play"] > buf["net_epa_per_play"]
    # Points from the two completed games only: KC 51 for / 41 against.
    assert kc["games"] == 2
    assert abs(kc["points_for_per_game"] - 25.5) < 1e-9
    assert abs(kc["points_against_per_game"] - 20.5) < 1e-9


# -- Refresh job body ---------------------------------------------------------

def test_refresh_nfl_data_upserts_all_caches(db_session) -> None:
    summary = refresh_nfl_data(
        db_session, season=2025, client=_StubNflverse(), now=NOW,
    )
    assert summary["errors"] == []
    assert summary["weekly_stats_weeks"] == 2
    assert summary["snap_count_weeks"] == 2
    assert summary["depth_chart_teams"] == 2
    assert summary["official_injury_weeks"] == 1
    assert summary["rated_teams"] == 2
    assert summary["schedule_games"] == 3

    ratings = load_nfl_team_ratings(db_session, 2025, now=NOW)
    assert ratings.cache_status == "hit"
    assert "KC" in ratings.payload["teams"]

    schedule = load_nfl_schedule(db_session, 2025, now=NOW)
    assert len(schedule.payload["games"]) == 3

    depth = load_nfl_depth_chart(db_session, 2025, "kc", now=NOW)
    assert depth.payload["rows"][0]["player_name"] == "Patrick Mahomes"

    weekly = load_nfl_weekly_stats(db_session, 2025, 10, now=NOW)
    assert weekly.payload["rows"][0]["player_display_name"] == "Justin Jefferson"

    snaps = load_nfl_snap_counts(db_session, 2025, now=NOW)
    assert set(snaps.payload["weeks"].keys()) == {"9", "10"}

    injuries = load_nfl_official_injuries(db_session, 2025, now=NOW)
    assert injuries.payload["week"] == 10
    assert injuries.payload["rows"][0]["report_status"] == "Questionable"

    # Prior-season ratings backfilled from the same fixture data.
    prior = load_nfl_team_ratings(db_session, 2024, now=NOW)
    assert prior.cache_status in {"hit", "stale"}


def test_refresh_survives_partial_dataset_failure(db_session) -> None:
    class _FlakyStub(_StubNflverse):
        def fetch_snap_counts(self, season: int):
            raise RuntimeError("github 500")

    summary = refresh_nfl_data(db_session, season=2025, client=_FlakyStub(), now=NOW)
    assert any("snap_counts" in error for error in summary["errors"])
    # Other datasets still landed.
    assert summary["weekly_stats_weeks"] == 2
    assert load_nfl_team_ratings(db_session, 2025, now=NOW).complete is True


def test_read_loaders_report_stale_and_miss(db_session) -> None:
    assert load_nfl_team_ratings(db_session, 2025, now=NOW).cache_status == "miss"
    db_session.add(NflTeamRatingCache(
        season=2025, payload={"teams": {}},
        cached_at=NOW - timedelta(days=3), expires_at=NOW - timedelta(days=2),
    ))
    db_session.flush()
    result = load_nfl_team_ratings(db_session, 2025, now=NOW)
    assert result.cache_status == "stale"
    assert result.complete is True


# -- Stadiums + weather --------------------------------------------------------

def test_stadium_info_and_name_reverse_map() -> None:
    info = nfl_stadium_info("GB")
    assert info is not None and info["roof"] == "outdoor"
    assert nfl_team_abbr_for_name("Kansas City Chiefs") == "KC"
    assert nfl_team_abbr_for_name("Chiefs") == "KC"
    assert nfl_team_abbr_for_name("Los Angeles Rams") == "LAR"
    assert nfl_team_abbr_for_name(None) is None


def test_normalize_nfl_team_code_maps_nflverse_variants() -> None:
    """nflverse uses LA / WAS (live-verified) and historical relocation
    codes in older games.csv rows — all must land on sika's canonical
    abbreviations so ratings keys join with stadium/ESPN data."""
    from app.services.nfl_advanced import normalize_nfl_team_code

    assert normalize_nfl_team_code("LA") == "LAR"
    assert normalize_nfl_team_code("WAS") == "WSH"
    assert normalize_nfl_team_code("STL") == "LAR"
    assert normalize_nfl_team_code("SD") == "LAC"
    assert normalize_nfl_team_code("OAK") == "LV"
    assert normalize_nfl_team_code("KC") == "KC"
    assert normalize_nfl_team_code(None) is None


def test_load_nfl_weather_dome_short_circuits(db_session) -> None:
    result = load_nfl_weather(
        db_session, event_id="42", home_team_abbr="MIN",
        game_time_utc=NOW, allow_network=True, now=NOW,
    )
    assert result.cache_status == "dome"
    assert result.payload["wind_speed_mph"] == 0.0
    # Nothing cached for domes — the value is constant by construction.
    assert db_session.query(NflWeatherCache).count() == 0


def test_load_nfl_weather_outdoor_fetches_and_caches(db_session) -> None:
    class _StubWeather:
        def __init__(self):
            self.calls = 0

        def fetch_game_weather(self, *, lat, lon, game_time_utc):
            self.calls += 1
            return {"temp_f": 28.0, "wind_speed_mph": 18.0, "precip_pct": 10.0}

    stub = _StubWeather()
    result = load_nfl_weather(
        db_session, event_id="77", home_team_abbr="GB",
        game_time_utc=NOW + timedelta(hours=20),
        client=stub, allow_network=True, now=NOW,
    )
    assert result.complete is True
    assert result.payload["wind_speed_mph"] == 18.0
    assert result.payload["is_dome"] is False
    # Second read is a cache hit — no second upstream call.
    again = load_nfl_weather(
        db_session, event_id="77", home_team_abbr="GB",
        game_time_utc=NOW + timedelta(hours=20),
        client=stub, allow_network=True, now=NOW,
    )
    assert again.cache_status == "hit"
    assert stub.calls == 1
