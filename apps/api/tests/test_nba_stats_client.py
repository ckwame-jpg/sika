import time

import httpx
import pytest

from app.clients import _rate_limit
from app.clients.nba_stats import (
    NbaStatsClient,
    NbaStatsRateLimitError,
    parse_result_set,
    season_param,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    _rate_limit.reset_for_tests()
    yield
    _rate_limit.reset_for_tests()


def _ok_response(payload: dict, request: httpx.Request | None = None) -> httpx.Response:
    request = request or httpx.Request("GET", "https://stats.nba.com/stats/playergamelogs")
    return httpx.Response(200, request=request, json=payload)


def test_season_param_formats_year_to_two_digit_suffix():
    assert season_param(2024) == "2024-25"
    assert season_param(1999) == "1999-00"


def test_parse_result_set_zips_headers_with_rows():
    payload = {
        "resultSets": [
            {
                "name": "PlayerGameLogs",
                "headers": ["GAME_DATE", "TS_PCT", "USG_PCT"],
                "rowSet": [
                    ["2025-04-01", 0.612, 0.305],
                    ["2025-03-30", 0.589, 0.298],
                ],
            }
        ]
    }
    rows = parse_result_set(payload, name="PlayerGameLogs")
    assert rows == [
        {"GAME_DATE": "2025-04-01", "TS_PCT": 0.612, "USG_PCT": 0.305},
        {"GAME_DATE": "2025-03-30", "TS_PCT": 0.589, "USG_PCT": 0.298},
    ]


def test_parse_result_set_returns_empty_when_no_sets():
    assert parse_result_set({}) == []


def test_fetch_player_advanced_gamelog_sends_required_headers(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["headers"] = headers
        return _ok_response({"resultSets": [{"name": "PlayerGameLogs", "headers": [], "rowSet": []}]})

    monkeypatch.setattr(httpx, "get", fake_get)

    client = NbaStatsClient()
    client.fetch_player_advanced_gamelog("203999", 2024)

    assert seen["url"].endswith("/playergamelogs")
    assert seen["params"]["PlayerID"] == "203999"
    assert seen["params"]["Season"] == "2024-25"
    assert seen["params"]["MeasureType"] == "Advanced"
    headers = seen["headers"]
    assert headers["x-nba-stats-token"] == "true"
    assert headers["x-nba-stats-origin"] == "stats"
    assert headers["Origin"] == "https://www.nba.com"
    assert headers["Referer"] == "https://www.nba.com/"
    assert "Mozilla/5.0" in headers["User-Agent"]


def test_fetch_player_advanced_gamelog_retries_on_429_with_retry_after(monkeypatch):
    attempts: list[int] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        attempts.append(1)
        request = httpx.Request("GET", url)
        if len(attempts) < NbaStatsClient._MAX_ATTEMPTS:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request, json={"error": "rate"})
        return _ok_response({"resultSets": []}, request=request)

    sleeps: list[float] = []
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda value: sleeps.append(value))

    client = NbaStatsClient()
    client.fetch_player_advanced_gamelog("123", 2024)
    assert len(attempts) == NbaStatsClient._MAX_ATTEMPTS
    # The first attempt slept on the bucket-refill plus 0s Retry-After.
    assert any(sleep == 0 for sleep in sleeps), "Retry-After=0 should produce a 0-second sleep"


def test_fetch_player_advanced_gamelog_raises_after_max_attempts(monkeypatch):
    attempts: list[int] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        attempts.append(1)
        request = httpx.Request("GET", url)
        return httpx.Response(429, headers={"Retry-After": "0"}, request=request, json={"error": "rate"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda value: None)

    client = NbaStatsClient()
    with pytest.raises(NbaStatsRateLimitError):
        client.fetch_player_advanced_gamelog("123", 2024)
    assert len(attempts) == NbaStatsClient._MAX_ATTEMPTS


def test_fetch_team_advanced_uses_advanced_measure_type(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_response(
            {
                "resultSets": [
                    {
                        "name": "LeagueDashTeamStats",
                        "headers": ["TEAM_ID", "TEAM_NAME", "OFF_RATING", "DEF_RATING", "NET_RATING", "PACE"],
                        "rowSet": [
                            [1610612747, "LA Lakers", 116.5, 110.2, 6.3, 100.4],
                        ],
                    }
                ]
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    payload = NbaStatsClient().fetch_team_advanced(2024)
    assert seen["url"].endswith("/leaguedashteamstats")
    assert seen["params"]["MeasureType"] == "Advanced"
    rows = parse_result_set(payload)
    assert rows[0]["TEAM_NAME"] == "LA Lakers"


def test_fetch_team_advanced_gamelog_passes_team_id(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_response({"resultSets": [{"name": "TeamGameLogs", "headers": [], "rowSet": []}]})

    monkeypatch.setattr(httpx, "get", fake_get)

    NbaStatsClient().fetch_team_advanced_gamelog("1610612747", 2024)
    assert seen["url"].endswith("/teamgamelogs")
    assert seen["params"]["TeamID"] == "1610612747"
    assert seen["params"]["MeasureType"] == "Advanced"
    assert seen["params"]["Season"] == "2024-25"


def test_fetch_lineup_advanced_passes_group_quantity(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_response({"resultSets": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    NbaStatsClient().fetch_lineup_advanced(2024, group_quantity=3)
    assert seen["url"].endswith("/leaguedashlineups")
    assert seen["params"]["GroupQuantity"] == "3"
    assert seen["params"]["MeasureType"] == "Advanced"


def test_fetch_boxscore_advanced_passes_game_id(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_response({"resultSets": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    NbaStatsClient().fetch_boxscore_advanced("0022400900")
    assert seen["url"].endswith("/boxscoreadvancedv2")
    assert seen["params"]["GameID"] == "0022400900"


def test_fetch_common_all_players_passes_season(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_response({"resultSets": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    NbaStatsClient().fetch_common_all_players(2024)
    assert seen["url"].endswith("/commonallplayers")
    assert seen["params"]["Season"] == "2024-25"
    assert seen["params"]["IsOnlyCurrentSeason"] == "1"
