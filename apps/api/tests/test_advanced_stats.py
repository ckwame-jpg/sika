from datetime import timedelta
from typing import Any

import pytest

from app.config import get_settings
from app.models import (
    EspnPlayerSearchCache,
    NbaAdvancedGamelogCache,
    NbaLeaguePercentilesCache,
    NbaLineupAdvancedCache,
    NbaPlayerRosterCache,
    NbaTeamAdvancedCache,
    NbaTeamGamelogCache,
    OperatorSetting,
    utcnow,
)
from app.services import advanced_stats


class _StubNbaStatsClient:
    def __init__(
        self,
        *,
        gamelog: dict[str, Any] | None = None,
        team: dict[str, Any] | None = None,
        league: dict[str, Any] | None = None,
        team_gamelog: dict[str, Any] | None = None,
        lineup: dict[str, Any] | None = None,
        roster: dict[str, Any] | None = None,
        raise_with: Exception | None = None,
    ) -> None:
        self.gamelog = gamelog
        self.team = team
        self.league = league
        self.team_gamelog = team_gamelog
        self.lineup = lineup
        self.roster = roster
        self.raise_with = raise_with
        self.fetch_player_calls: list[tuple[str, int]] = []
        self.fetch_team_calls: list[int] = []
        self.fetch_league_calls: list[int] = []
        self.fetch_team_gamelog_calls: list[tuple[str, int]] = []
        self.fetch_lineup_calls: list[int] = []
        self.fetch_roster_calls: list[int] = []

    def fetch_player_advanced_gamelog(self, player_id, season, season_type="Regular Season"):
        self.fetch_player_calls.append((str(player_id), int(season)))
        if self.raise_with is not None:
            raise self.raise_with
        return self.gamelog or {"resultSets": []}

    def fetch_team_advanced(self, season, season_type="Regular Season"):
        self.fetch_team_calls.append(int(season))
        if self.raise_with is not None:
            raise self.raise_with
        return self.team or {"resultSets": []}

    def fetch_league_player_advanced(self, season, season_type="Regular Season"):
        self.fetch_league_calls.append(int(season))
        if self.raise_with is not None:
            raise self.raise_with
        return self.league or {"resultSets": []}

    def fetch_team_advanced_gamelog(self, team_id, season, season_type="Regular Season"):
        self.fetch_team_gamelog_calls.append((str(team_id), int(season)))
        if self.raise_with is not None:
            raise self.raise_with
        return self.team_gamelog or {"resultSets": []}

    def fetch_lineup_advanced(self, season, season_type="Regular Season", group_quantity=5):
        self.fetch_lineup_calls.append(int(season))
        if self.raise_with is not None:
            raise self.raise_with
        return self.lineup or {"resultSets": []}

    def fetch_common_all_players(self, season, is_only_current_season=1):
        self.fetch_roster_calls.append(int(season))
        if self.raise_with is not None:
            raise self.raise_with
        return self.roster or {"resultSets": []}


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _gamelog_payload(rows: list[dict[str, float | str]]) -> dict[str, Any]:
    headers = list(rows[0].keys()) if rows else []
    rowset = [[row.get(h) for h in headers] for row in rows]
    return {"resultSets": [{"name": "PlayerGameLogs", "headers": headers, "rowSet": rowset}]}


def test_load_nba_advanced_cache_hit_skips_network(db_session):
    season = 2024
    payload = {"season_avg": {"ts_pct": 0.61}, "recent_10_avg": {"ts_pct": 0.65}}
    db_session.add(
        NbaAdvancedGamelogCache(
            athlete_id="203999",
            season=season,
            payload=payload,
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    db_session.flush()
    stub = _StubNbaStatsClient()
    result = advanced_stats.load_nba_advanced(
        db_session,
        nba_stats_player_id="203999",
        season=season,
        client=stub,
        allow_network=True,
    )
    assert result.cache_status == "hit"
    assert result.payload == payload
    assert stub.fetch_player_calls == []


def test_load_nba_advanced_miss_fetches_and_persists(db_session):
    rows = [
        {"GAME_DATE": "2025-04-01", "TS_PCT": 0.62, "USG_PCT": 0.31, "OFF_RATING": 118.0,
         "DEF_RATING": 110.0, "NET_RATING": 8.0, "PIE": 0.18, "AST_PCT": 0.30,
         "OREB_PCT": 0.04, "DREB_PCT": 0.18, "REB_PCT": 0.12, "PACE": 101.0, "MIN": 36.0,
         "EFG_PCT": 0.55, "MATCHUP": "LAL @ DEN"},
        {"GAME_DATE": "2025-03-30", "TS_PCT": 0.58, "USG_PCT": 0.28, "OFF_RATING": 115.0,
         "DEF_RATING": 112.0, "NET_RATING": 3.0, "PIE": 0.15, "AST_PCT": 0.27,
         "OREB_PCT": 0.03, "DREB_PCT": 0.16, "REB_PCT": 0.11, "PACE": 99.0, "MIN": 34.0,
         "EFG_PCT": 0.52, "MATCHUP": "LAL vs PHX"},
    ]
    stub = _StubNbaStatsClient(gamelog=_gamelog_payload(rows))
    result = advanced_stats.load_nba_advanced(
        db_session,
        nba_stats_player_id="203999",
        season=2024,
        client=stub,
        allow_network=True,
    )
    assert result.cache_status == "miss"
    assert result.complete is True
    cached = db_session.query(NbaAdvancedGamelogCache).filter_by(athlete_id="203999", season=2024).one()
    assert cached.payload["games_played"] == 2
    assert cached.payload["season_avg"]["ts_pct"] == pytest.approx(0.60)
    assert cached.payload["recent_10_avg"]["off_rating"] == pytest.approx(116.5)
    assert stub.fetch_player_calls == [("203999", 2024)]


def test_load_nba_advanced_no_network_returns_miss_when_no_cache(db_session):
    stub = _StubNbaStatsClient()
    result = advanced_stats.load_nba_advanced(
        db_session,
        nba_stats_player_id="203999",
        season=2024,
        client=stub,
        allow_network=False,
    )
    assert result.cache_status == "miss"
    assert result.complete is False
    assert stub.fetch_player_calls == []


def test_load_nba_advanced_falls_back_to_stale_when_fetch_fails(db_session):
    payload = {"season_avg": {"ts_pct": 0.50}, "recent_10_avg": {"ts_pct": 0.50}}
    db_session.add(
        NbaAdvancedGamelogCache(
            athlete_id="203999",
            season=2024,
            payload=payload,
            cached_at=utcnow() - timedelta(days=2),
            expires_at=utcnow() - timedelta(hours=1),
        )
    )
    db_session.flush()
    stub = _StubNbaStatsClient(raise_with=RuntimeError("upstream boom"))
    result = advanced_stats.load_nba_advanced(
        db_session,
        nba_stats_player_id="203999",
        season=2024,
        client=stub,
        allow_network=True,
    )
    assert result.cache_status == "stale"
    assert result.payload == payload


def test_circuit_breaker_trips_after_three_failures(db_session):
    stub = _StubNbaStatsClient(raise_with=RuntimeError("boom"))
    for _ in range(3):
        advanced_stats.load_nba_advanced(
            db_session,
            nba_stats_player_id=f"player_{_}",
            season=2024,
            client=stub,
            allow_network=True,
        )
    assert advanced_stats.nba_circuit_breaker_open(db_session) is True
    # While breaker is open, no further fetches occur
    fresh_stub = _StubNbaStatsClient()
    advanced_stats.load_nba_advanced(
        db_session,
        nba_stats_player_id="999",
        season=2024,
        client=fresh_stub,
        allow_network=True,
    )
    assert fresh_stub.fetch_player_calls == []


def test_daily_cap_short_circuits(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "nba_stats_daily_request_cap", 1)
    rows = [{"GAME_DATE": "2025-04-01", "TS_PCT": 0.6, "USG_PCT": 0.3, "OFF_RATING": 110.0,
             "DEF_RATING": 110.0, "NET_RATING": 0.0, "PIE": 0.1, "AST_PCT": 0.2, "OREB_PCT": 0.04,
             "DREB_PCT": 0.18, "REB_PCT": 0.1, "PACE": 100.0, "MIN": 30.0, "EFG_PCT": 0.5, "MATCHUP": ""}]
    stub = _StubNbaStatsClient(gamelog=_gamelog_payload(rows))
    advanced_stats.load_nba_advanced(
        db_session, nba_stats_player_id="111", season=2024, client=stub, allow_network=True
    )
    # Second fetch should be skipped because cap is 1
    second = advanced_stats.load_nba_advanced(
        db_session, nba_stats_player_id="222", season=2024, client=stub, allow_network=True
    )
    assert second.cache_status == "skipped"
    assert stub.fetch_player_calls == [("111", 2024)]


def test_emit_nba_player_features_returns_keys_with_completeness_indicator():
    payload = {
        "season_avg": {"ts_pct": 0.6, "usg_pct": 0.28, "off_rating": 115.0, "def_rating": 112.0,
                       "net_rating": 3.0, "pace": 100.0, "pie": 0.15, "efg_pct": 0.55,
                       "ast_pct": 0.25, "reb_pct": 0.10, "oreb_pct": 0.04, "dreb_pct": 0.16},
        "recent_10_avg": {"ts_pct": 0.65, "usg_pct": 0.32, "off_rating": 120.0, "def_rating": 110.0,
                          "net_rating": 10.0, "pace": 102.0, "pie": 0.18, "efg_pct": 0.58,
                          "ast_pct": 0.30, "reb_pct": 0.12, "oreb_pct": 0.04, "dreb_pct": 0.18},
    }
    out = advanced_stats.emit_nba_player_features(payload)
    assert out["recent_true_shooting_pct"] == 0.65
    assert out["season_true_shooting_pct"] == 0.60
    assert out["recent_usage_pct"] == 0.32
    assert out["season_offensive_rating"] == 115.0
    assert out["recent_defensive_rating"] == 110.0
    assert out["recent_pace"] == 102.0
    assert out["advanced_data_complete"] == 1.0


def test_emit_nba_player_features_empty_for_missing_payload():
    assert advanced_stats.emit_nba_player_features(None) == {}
    assert advanced_stats.emit_nba_player_features({}) == {}


def test_warm_summary_loads_team_and_percentiles(db_session):
    team_payload = {
        "resultSets": [
            {
                "name": "LeagueDashTeamStats",
                "headers": ["TEAM_ID", "TEAM_NAME", "OFF_RATING", "DEF_RATING", "NET_RATING", "PACE"],
                "rowSet": [
                    [1, "Team A", 115.0, 110.0, 5.0, 100.0],
                    [2, "Team B", 112.0, 113.0, -1.0, 99.0],
                ],
            }
        ]
    }
    league_payload = {
        "resultSets": [
            {
                "name": "LeagueDashPlayerStats",
                "headers": ["PLAYER_ID", "TS_PCT", "USG_PCT", "OFF_RATING", "DEF_RATING",
                            "NET_RATING", "PIE", "PACE", "EFG_PCT"],
                "rowSet": [
                    [1, 0.55, 0.20, 110.0, 110.0, 0.0, 0.10, 100.0, 0.50],
                    [2, 0.60, 0.25, 115.0, 108.0, 7.0, 0.13, 101.0, 0.55],
                    [3, 0.65, 0.30, 120.0, 105.0, 15.0, 0.18, 102.0, 0.58],
                ],
            }
        ]
    }
    stub = _StubNbaStatsClient(team=team_payload, league=league_payload)
    summary = advanced_stats.warm_nba_advanced_for_athletes(
        db_session, nba_stats_player_ids=[], season=2024, client=stub
    )
    assert summary.nba_team_loaded is True
    assert summary.nba_percentiles_loaded is True
    team_cache = db_session.query(NbaTeamAdvancedCache).one()
    assert "1" in team_cache.payload["teams"]
    perc_cache = db_session.query(NbaLeaguePercentilesCache).one()
    assert "ts_pct" in perc_cache.payload["breakpoints"]
    assert perc_cache.payload["sample_size"] == 3


def test_operator_setting_circuit_breaker_round_trip(db_session):
    advanced_stats._operator_set(db_session, "test_key", {"value": 42})
    assert advanced_stats._operator_get(db_session, "test_key") == {"value": 42}
    advanced_stats._operator_set(db_session, "test_key", {"value": 100})
    assert advanced_stats._operator_get(db_session, "test_key") == {"value": 100}
    row = db_session.query(OperatorSetting).filter(OperatorSetting.key == "test_key").one()
    assert row.value == {"value": 100}


# -----------------------------------------------------------------------------
# Per-game team gamelog tests

def _team_gamelog_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    headers = list(rows[0].keys()) if rows else []
    rowset = [[row.get(h) for h in headers] for row in rows]
    return {"resultSets": [{"name": "TeamGameLogs", "headers": headers, "rowSet": rowset}]}


def test_load_nba_team_gamelog_aggregates_recent_windows(db_session):
    rows = [
        {"GAME_ID": "0022400900", "GAME_DATE": "2025-04-01", "MATCHUP": "LAL @ DEN",
         "OFF_RATING": 120.0, "DEF_RATING": 105.0, "NET_RATING": 15.0, "PACE": 102.0,
         "TS_PCT": 0.62, "EFG_PCT": 0.58, "AST_PCT": 0.65, "OREB_PCT": 0.30,
         "DREB_PCT": 0.70, "TM_TOV_PCT": 0.13},
        {"GAME_ID": "0022400891", "GAME_DATE": "2025-03-30", "MATCHUP": "LAL vs PHX",
         "OFF_RATING": 110.0, "DEF_RATING": 115.0, "NET_RATING": -5.0, "PACE": 99.0,
         "TS_PCT": 0.55, "EFG_PCT": 0.52, "AST_PCT": 0.55, "OREB_PCT": 0.22,
         "DREB_PCT": 0.65, "TM_TOV_PCT": 0.16},
    ]
    stub = _StubNbaStatsClient(team_gamelog=_team_gamelog_payload(rows))
    result = advanced_stats.load_nba_team_gamelog(
        db_session, team_id="1610612747", season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(NbaTeamGamelogCache).one()
    assert cached.payload["games_played"] == 2
    assert cached.payload["season_avg"]["off_rating"] == pytest.approx(115.0)
    assert cached.payload["recent_5_avg"]["def_rating"] == pytest.approx(110.0)
    assert stub.fetch_team_gamelog_calls == [("1610612747", 2024)]


def test_emit_nba_opponent_team_features_includes_form_deltas():
    payload = {
        "season_avg": {"off_rating": 115.0, "def_rating": 110.0, "pace": 100.0, "net_rating": 5.0},
        "recent_5_avg": {"off_rating": 120.0, "def_rating": 105.0, "pace": 102.0, "net_rating": 15.0},
    }
    out = advanced_stats.emit_nba_opponent_team_features(payload)
    assert out["opponent_off_rating_recent_5"] == 120.0
    assert out["opponent_def_rating_recent_5"] == 105.0
    assert out["opponent_pace_recent_5"] == 102.0
    assert out["opponent_form_delta_off"] == pytest.approx(5.0)
    assert out["opponent_form_delta_def"] == pytest.approx(-5.0)
    assert out["opponent_team_data_complete"] == 1.0


def test_emit_nba_opponent_team_features_empty_for_missing_payload():
    assert advanced_stats.emit_nba_opponent_team_features(None) == {}
    assert advanced_stats.emit_nba_opponent_team_features({}) == {}


def test_find_nba_team_id_by_name_uses_team_advanced_cache(db_session):
    payload = {
        "teams": {
            "1610612747": {"team_id": "1610612747", "team_name": "Los Angeles Lakers", "off_rating": 115.0},
            "1610612743": {"team_id": "1610612743", "team_name": "Denver Nuggets", "off_rating": 117.0},
        }
    }
    db_session.add(
        NbaTeamAdvancedCache(
            team_id="ALL",
            season=2024,
            payload=payload,
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    db_session.flush()
    assert (
        advanced_stats.find_nba_team_id_by_name(db_session, team_name="Los Angeles Lakers", season=2024)
        == "1610612747"
    )
    assert (
        advanced_stats.find_nba_team_id_by_name(db_session, team_name="DENVER NUGGETS!", season=2024)
        == "1610612743"
    )
    assert (
        advanced_stats.find_nba_team_id_by_name(db_session, team_name="Toronto Raptors", season=2024)
        is None
    )


# -----------------------------------------------------------------------------
# Lineup advanced loader

def test_load_nba_lineup_advanced_persists_lineups(db_session):
    payload = {
        "resultSets": [
            {
                "name": "Lineups",
                "headers": ["GROUP_ID", "GROUP_NAME", "TEAM_ID", "MIN", "OFF_RATING", "DEF_RATING",
                            "NET_RATING", "PACE", "TS_PCT"],
                "rowSet": [
                    ["1-2-3-4-5", "A - B - C - D - E", "1610612747", 320.5, 122.0, 110.0, 12.0, 100.0, 0.60],
                ],
            }
        ]
    }
    stub = _StubNbaStatsClient(lineup=payload)
    result = advanced_stats.load_nba_lineup_advanced(
        db_session, season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(NbaLineupAdvancedCache).one()
    assert cached.payload["sample_size"] == 1
    assert cached.payload["lineups"][0]["team_id"] == "1610612747"


# -----------------------------------------------------------------------------
# Player-ID resolution

def _roster_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    headers = list(rows[0].keys()) if rows else []
    rowset = [[row.get(h) for h in headers] for row in rows]
    return {"resultSets": [{"name": "CommonAllPlayers", "headers": headers, "rowSet": rowset}]}


def test_resolve_nba_stats_player_id_uses_cached_roster(db_session):
    roster = _roster_payload(
        [
            {"PERSON_ID": 203999, "DISPLAY_FIRST_LAST": "Nikola Jokic", "TEAM_ID": 1610612743,
             "TEAM_ABBREVIATION": "DEN", "ROSTERSTATUS": 1},
            {"PERSON_ID": 2544, "DISPLAY_FIRST_LAST": "LeBron James", "TEAM_ID": 1610612747,
             "TEAM_ABBREVIATION": "LAL", "ROSTERSTATUS": 1},
        ]
    )
    stub = _StubNbaStatsClient(roster=roster)
    resolved = advanced_stats.resolve_nba_stats_player_id(
        db_session,
        espn_athlete_id=None,
        full_name="LeBron James",
        team_abbreviation="LAL",
        season=2024,
        client=stub,
        allow_network=True,
    )
    assert resolved == "2544"
    assert stub.fetch_roster_calls == [2024]
    assert db_session.query(NbaPlayerRosterCache).count() == 1


def test_resolve_nba_stats_player_id_writes_back_to_search_cache(db_session):
    roster = _roster_payload(
        [
            {"PERSON_ID": 2544, "DISPLAY_FIRST_LAST": "LeBron James", "TEAM_ID": 1610612747,
             "TEAM_ABBREVIATION": "LAL", "ROSTERSTATUS": 1},
        ]
    )
    db_session.add(
        EspnPlayerSearchCache(
            sport_key="NBA",
            query_normalized="lebron james",
            payload={"athlete_id": "1966", "display_name": "LeBron James", "team_name": "Los Angeles Lakers"},
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(days=7),
        )
    )
    db_session.flush()

    stub = _StubNbaStatsClient(roster=roster)
    resolved = advanced_stats.resolve_nba_stats_player_id(
        db_session,
        espn_athlete_id="1966",
        full_name="LeBron James",
        team_abbreviation="LAL",
        season=2024,
        client=stub,
        allow_network=True,
    )
    assert resolved == "2544"
    cache_row = db_session.query(EspnPlayerSearchCache).one()
    assert cache_row.payload["nba_stats_id"] == "2544"

    # Second call should hit the search-cache mapping (no roster fetch needed)
    stub.fetch_roster_calls.clear()
    second = advanced_stats.resolve_nba_stats_player_id(
        db_session,
        espn_athlete_id="1966",
        full_name="LeBron James",
        team_abbreviation="LAL",
        season=2024,
        client=stub,
        allow_network=False,
    )
    assert second == "2544"
    assert stub.fetch_roster_calls == []


def test_resolve_nba_stats_player_id_returns_none_on_no_match(db_session):
    roster = _roster_payload(
        [{"PERSON_ID": 2544, "DISPLAY_FIRST_LAST": "LeBron James", "TEAM_ID": 1610612747,
          "TEAM_ABBREVIATION": "LAL", "ROSTERSTATUS": 1}]
    )
    stub = _StubNbaStatsClient(roster=roster)
    assert (
        advanced_stats.resolve_nba_stats_player_id(
            db_session,
            espn_athlete_id="9999",
            full_name="Mystery Player",
            team_abbreviation="MYS",
            season=2024,
            client=stub,
            allow_network=True,
        )
        is None
    )


def test_warm_summary_includes_team_gamelogs_and_lineups(db_session):
    team_payload = {
        "resultSets": [
            {
                "name": "LeagueDashTeamStats",
                "headers": ["TEAM_ID", "TEAM_NAME", "OFF_RATING", "DEF_RATING", "NET_RATING", "PACE"],
                "rowSet": [
                    [1, "Team A", 115.0, 110.0, 5.0, 100.0],
                    [2, "Team B", 112.0, 113.0, -1.0, 99.0],
                ],
            }
        ]
    }
    league_payload = {
        "resultSets": [
            {
                "name": "LeagueDashPlayerStats",
                "headers": ["PLAYER_ID", "TS_PCT", "USG_PCT", "OFF_RATING", "DEF_RATING",
                            "NET_RATING", "PIE", "PACE", "EFG_PCT"],
                "rowSet": [[1, 0.60, 0.25, 115.0, 110.0, 5.0, 0.13, 100.0, 0.55]],
            }
        ]
    }
    lineup_payload = {
        "resultSets": [
            {
                "name": "Lineups",
                "headers": ["GROUP_ID", "GROUP_NAME", "TEAM_ID", "MIN", "OFF_RATING", "DEF_RATING",
                            "NET_RATING", "PACE", "TS_PCT"],
                "rowSet": [["1-2-3-4-5", "Starters", "1", 200.0, 118.0, 108.0, 10.0, 100.0, 0.60]],
            }
        ]
    }
    roster_payload = _roster_payload(
        [{"PERSON_ID": 1, "DISPLAY_FIRST_LAST": "Test Player", "TEAM_ID": 1,
          "TEAM_ABBREVIATION": "TST", "ROSTERSTATUS": 1}]
    )
    team_gamelog_payload = _team_gamelog_payload([
        {"GAME_ID": "g1", "GAME_DATE": "2025-04-01", "MATCHUP": "A vs B", "OFF_RATING": 118.0,
         "DEF_RATING": 108.0, "NET_RATING": 10.0, "PACE": 100.0, "TS_PCT": 0.60, "EFG_PCT": 0.55,
         "AST_PCT": 0.60, "OREB_PCT": 0.25, "DREB_PCT": 0.70, "TM_TOV_PCT": 0.14}
    ])
    stub = _StubNbaStatsClient(
        team=team_payload,
        league=league_payload,
        lineup=lineup_payload,
        roster=roster_payload,
        team_gamelog=team_gamelog_payload,
    )
    summary = advanced_stats.warm_nba_advanced_for_athletes(
        db_session, nba_stats_player_ids=[], season=2024, client=stub
    )
    assert summary.nba_team_loaded is True
    assert summary.nba_lineup_loaded is True
    assert summary.nba_roster_loaded is True
    assert summary.nba_percentiles_loaded is True
    assert summary.nba_team_gamelogs_loaded == 2
    assert sorted(call[0] for call in stub.fetch_team_gamelog_calls) == ["1", "2"]
