"""Smarter NFL PR 1 scaffolding pin — apps/api side.

PR 1 wires the NFL data plumbing every later NFL PR builds on, with
zero behavior change for live sports (NFL stays research_only until
PR 10a):

- ESPN URL constants already carried NFL endpoints; this suite pins
  them so a regression can't silently drop the sport.
- ``ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME`` gains the 32-team NFL map
  (plus alt spellings JAC / WAS / LA) so the resolver's team-hint
  matching works for NFL props.
- ``fetch_injury_report`` generalizes to carry the sport path segment —
  NFL lives under ``sports/football/`` — without moving the NBA / WNBA
  URLs.
- ``_build_nfl_game_logs`` parses receiving stats (receptions, targets,
  receiving yards / TDs, fumbles lost) and tolerates ESPN's ``"-"``
  placeholders; stat names verified against live WR / QB payloads.
- ``Settings`` gains the NFL cache TTLs and the per-sport Odds API TTL
  override map.
- ``data/nfl_stadiums.json`` covers all 32 teams with lat/lon + roof
  for the weather client.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from app.clients.espn import (
    ESPN_GAMELOG_URLS,
    ESPN_LEAGUE_NAMES,
    ESPN_SCOREBOARD_URLS,
    ESPN_SEARCH_SLUGS,
    ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME,
    ESPN_TEAM_SCHEDULE_URLS,
    EspnPublicClient,
)
from app.config import Settings
from app.services.stats_query import (
    _build_nfl_game_logs,
    _build_stat_line,
    _METRIC_LABELS,
    _parse_nfl_stat,
)


NFL_TEAM_COUNT = 32

_ALL_32_TEAMS = {
    "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills",
    "Carolina Panthers", "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns",
    "Dallas Cowboys", "Denver Broncos", "Detroit Lions", "Green Bay Packers",
    "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars", "Kansas City Chiefs",
    "Los Angeles Chargers", "Los Angeles Rams", "Las Vegas Raiders", "Miami Dolphins",
    "Minnesota Vikings", "New England Patriots", "New Orleans Saints", "New York Giants",
    "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers", "Seattle Seahawks",
    "San Francisco 49ers", "Tampa Bay Buccaneers", "Tennessee Titans",
    "Washington Commanders",
}


class _StubHttpClient:
    def __init__(self, payload: dict | None = None):
        self.payload = payload or {}
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        import httpx

        return httpx.Response(200, json=self.payload, request=httpx.Request("GET", url))


def test_espn_url_constants_include_nfl() -> None:
    assert ESPN_SEARCH_SLUGS["NFL"] == "nfl"
    assert "/football/nfl/" in ESPN_SCOREBOARD_URLS["NFL"]
    assert "/football/nfl/" in ESPN_GAMELOG_URLS["NFL"]
    assert "{athlete_id}" in ESPN_GAMELOG_URLS["NFL"]
    assert "/football/nfl/" in ESPN_TEAM_SCHEDULE_URLS["NFL"]
    assert "{team_id}" in ESPN_TEAM_SCHEDULE_URLS["NFL"]
    assert "NFL" in ESPN_LEAGUE_NAMES


def test_nfl_team_map_includes_all_32_teams() -> None:
    nfl_map = ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME.get("NFL")
    assert nfl_map is not None, "NFL team abbreviation map missing"
    actual_teams = set(nfl_map.values())
    assert _ALL_32_TEAMS.issubset(actual_teams), (
        f"NFL team map missing teams: {_ALL_32_TEAMS - actual_teams}"
    )
    assert len(actual_teams) == NFL_TEAM_COUNT
    # Alt spellings that show up in Kalshi tickers / older feeds.
    assert nfl_map["JAC"] == nfl_map["JAX"] == "Jacksonville Jaguars"
    assert nfl_map["WAS"] == nfl_map["WSH"] == "Washington Commanders"
    assert nfl_map["LA"] == nfl_map["LAR"] == "Los Angeles Rams"


def test_espn_injury_url_for_nfl_uses_football_path() -> None:
    stub = _StubHttpClient(payload={"injuries": []})
    client = EspnPublicClient(http_client=stub)
    client.fetch_nfl_injury_report()
    assert stub.calls[0]["url"].endswith("/football/nfl/injuries")


def test_espn_injury_urls_for_basketball_leagues_unchanged() -> None:
    # The tuple-valued slug map must not shift the existing NBA / WNBA
    # URLs as a side effect of the NFL generalization.
    stub = _StubHttpClient(payload={"injuries": []})
    client = EspnPublicClient(http_client=stub)
    client.fetch_nba_injury_report()
    client.fetch_wnba_injury_report()
    assert stub.calls[0]["url"].endswith("/basketball/nba/injuries")
    assert stub.calls[1]["url"].endswith("/basketball/wnba/injuries")


def _wr_gamelog_payload() -> dict:
    """WR-shaped fixture mirroring a live ESPN payload (names verified
    2026-07-09 against Justin Jefferson's 2025 gamelog): receiving-first
    stat names, ``"-"`` placeholders for never-accrued stats."""
    return {
        "names": [
            "receptions", "receivingTargets", "receivingYards", "yardsPerReception",
            "receivingTouchdowns", "longReception", "rushingAttempts", "rushingYards",
            "yardsPerRushAttempt", "longRushing", "rushingTouchdowns", "fumbles",
            "fumblesLost", "fumblesForced", "kicksBlocked",
        ],
        "events": {
            "401001": {
                "gameDate": "2025-11-02T18:00Z",
                "atVs": "vs",
                "homeTeamScore": "27",
                "awayTeamScore": "20",
                "gameResult": "W",
                "opponent": {"displayName": "Green Bay Packers", "abbreviation": "GB"},
            },
        },
        "seasonTypes": [
            {
                "categories": [
                    {
                        "events": [
                            {
                                "eventId": "401001",
                                "stats": ["8", "11", "101", "12.6", "1", "18", "1", "3", "3.0", "3", "0", "0", "0", "-", "-"],
                            },
                        ],
                    },
                ],
            },
        ],
    }


def test_nfl_gamelog_parser_extracts_receiving_stats() -> None:
    logs = _build_nfl_game_logs(_wr_gamelog_payload())
    assert len(logs) == 1
    raw = logs[0]["raw_metrics"]
    assert raw["receptions"] == 8.0
    assert raw["receiving_targets"] == 11.0
    assert raw["receiving_yards"] == 101.0
    assert raw["receiving_touchdowns"] == 1.0
    assert raw["rushing_yards"] == 3.0
    assert raw["fumbles_lost"] == 0.0
    # A WR line has no passing stats — parsed as zeros, not errors.
    assert raw["passing_yards"] == 0.0
    metrics = logs[0]["metrics"]
    assert metrics["yards_per_reception"] == 12.6
    assert metrics["receiving_yards"] == 101.0


def test_parse_nfl_stat_tolerates_dash_placeholder() -> None:
    assert _parse_nfl_stat("-") == 0.0
    assert _parse_nfl_stat(None) == 0.0
    assert _parse_nfl_stat("12") == 12.0
    assert _parse_nfl_stat("1,024") == 1024.0


def test_nfl_metric_labels_cover_receiving_keys() -> None:
    labels = _METRIC_LABELS["NFL"]
    for key in ("receptions", "receiving_targets", "receiving_yards",
                "yards_per_reception", "receiving_touchdowns", "fumbles_lost"):
        assert key in labels, f"missing NFL metric label for {key}"


def test_nfl_stat_line_skips_zero_position_components() -> None:
    # WR line: passing zeros drop out, receiving stats read naturally.
    wr_line = _build_stat_line("NFL", {
        "passing_yards": 0.0, "passing_touchdowns": 0.0, "rushing_yards": 3.0,
        "receptions": 8.0, "receiving_yards": 101.0, "receiving_touchdowns": 1.0,
        "qbr": 0.0,
    })
    assert wr_line is not None
    assert "pass" not in wr_line
    assert "8 receptions" in wr_line
    assert "101 rec yards" in wr_line
    # NBA keeps zeros — "0 points" is real information.
    nba_line = _build_stat_line("NBA", {"points": 0.0, "assists": 2.0, "rebounds": 5.0, "minutes": 31.0})
    assert nba_line is not None
    assert "0 point" in nba_line


def test_settings_include_nfl_cache_ttls() -> None:
    settings = Settings()
    assert settings.nfl_prop_gamelog_cache_minutes > 0
    assert settings.nfl_injury_report_cache_minutes > 0
    assert settings.nfl_weekly_stats_cache_minutes > 0
    assert settings.nfl_snap_counts_cache_minutes > 0
    assert settings.nfl_depth_chart_cache_minutes > 0
    assert settings.nfl_team_rating_cache_minutes > 0
    assert settings.nfl_schedule_cache_minutes > 0
    assert settings.nfl_weather_cache_minutes > 0
    assert settings.nfl_qb_out_margin_penalty > 0


def test_settings_odds_api_per_sport_ttl_covers_nfl() -> None:
    settings = Settings()
    assert settings.the_odds_api_cache_ttl_minutes_by_sport.get("NFL", 0) >= 60, (
        "NFL needs a long Odds API TTL to protect the free-tier budget"
    )


def test_resolver_gamelog_ttl_uses_nfl_setting(db_session) -> None:
    from app.services.scoring.resolver import PropStatsResolver

    resolver = PropStatsResolver(db_session, allow_network=False)
    settings = Settings()
    assert resolver._gamelog_ttl("NFL") == timedelta(
        minutes=settings.nfl_prop_gamelog_cache_minutes
    )


def test_nfl_stadiums_file_covers_all_teams() -> None:
    data_path = (
        Path(__file__).resolve().parents[1] / "app" / "data" / "nfl_stadiums.json"
    )
    payload = json.loads(data_path.read_text())
    team_keys = {key for key in payload if not key.startswith("_")}
    nfl_map = ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME["NFL"]
    canonical = {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
        "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
        "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
        "TEN", "WSH",
    }
    assert team_keys == canonical
    assert canonical <= set(nfl_map.keys()), "stadium keys must resolve in the ESPN team map"
    for team, info in payload.items():
        if team.startswith("_"):
            continue
        assert info["roof"] in {"dome", "retractable", "outdoor"}, f"{team} bad roof"
        assert info["surface"] in {"grass", "turf"}, f"{team} bad surface"
        assert -125.0 < info["lon"] < -66.0, f"{team} lon out of CONUS range"
        assert 24.0 < info["lat"] < 49.0, f"{team} lat out of CONUS range"
    # Shared stadiums stay consistent.
    assert payload["LAC"] == payload["LAR"]
    assert payload["NYG"] == payload["NYJ"]
