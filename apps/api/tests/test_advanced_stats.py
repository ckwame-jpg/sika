from datetime import timedelta
from typing import Any

import pytest

from app.config import get_settings
from app.models import (
    NbaAdvancedGamelogCache,
    NbaLeaguePercentilesCache,
    NbaTeamAdvancedCache,
    OperatorSetting,
    utcnow,
)
from app.services import advanced_stats


class _StubNbaStatsClient:
    def __init__(self, *, gamelog: dict[str, Any] | None = None, team: dict[str, Any] | None = None,
                 league: dict[str, Any] | None = None, raise_with: Exception | None = None) -> None:
        self.gamelog = gamelog
        self.team = team
        self.league = league
        self.raise_with = raise_with
        self.fetch_player_calls: list[tuple[str, int]] = []
        self.fetch_team_calls: list[int] = []
        self.fetch_league_calls: list[int] = []

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
