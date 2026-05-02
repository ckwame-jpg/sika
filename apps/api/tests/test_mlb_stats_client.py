import httpx
import pytest

from app.clients import _rate_limit
from app.clients.mlb_stats import MlbStatsClient


@pytest.fixture(autouse=True)
def _reset_registry():
    _rate_limit.reset_for_tests()
    yield
    _rate_limit.reset_for_tests()


def _ok_response(payload: dict) -> httpx.Response:
    request = httpx.Request("GET", "https://statsapi.mlb.com/api/v1/people/1/stats")
    return httpx.Response(200, request=request, json=payload)


def test_fetch_player_sabermetrics_passes_expected_params(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_response({"stats": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    MlbStatsClient().fetch_player_sabermetrics("660271", 2024)
    assert seen["url"].endswith("/people/660271/stats")
    assert seen["params"]["stats"] == "sabermetrics"
    assert seen["params"]["season"] == "2024"
    assert seen["params"]["group"] == "hitting,pitching"


def test_fetch_pitcher_sabermetrics_uses_pitching_group(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, timeout=None):
        seen["params"] = params
        return _ok_response({"stats": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    MlbStatsClient().fetch_pitcher_sabermetrics("592450", 2024)
    assert seen["params"]["group"] == "pitching"
    assert "sabermetrics" in seen["params"]["stats"]


def test_fetch_schedule_passes_hydrate_lineups(monkeypatch):
    from datetime import date

    seen: dict = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_response({"dates": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    MlbStatsClient().fetch_schedule(date(2025, 6, 1))
    assert seen["url"].endswith("/schedule")
    assert "lineups" in seen["params"]["hydrate"]
    assert seen["params"]["date"] == "2025-06-01"


def test_search_player_calls_people_search(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_response({"people": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    MlbStatsClient().search_player("Aaron Judge")
    assert seen["url"].endswith("/people/search")
    assert seen["params"]["names"] == "Aaron Judge"


def test_fetch_team_roster_includes_active_roster_type(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, timeout=None):
        seen["params"] = params
        return _ok_response({"roster": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    MlbStatsClient().fetch_team_roster("147", season=2024)
    assert seen["params"]["rosterType"] == "active"
    assert seen["params"]["season"] == "2024"
