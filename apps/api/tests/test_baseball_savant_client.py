import httpx
import pytest

from app.clients import _rate_limit
from app.clients.baseball_savant import BaseballSavantClient, parse_csv_rows


@pytest.fixture(autouse=True)
def _reset_registry():
    _rate_limit.reset_for_tests()
    yield
    _rate_limit.reset_for_tests()


def _ok_csv(text: str) -> httpx.Response:
    request = httpx.Request("GET", "https://baseballsavant.mlb.com/statcast_search/csv")
    return httpx.Response(200, request=request, text=text)


def test_parse_csv_rows_handles_basic_csv():
    text = "a,b\n1,foo\n2,bar\n"
    rows = parse_csv_rows(text)
    assert rows == [{"a": "1", "b": "foo"}, {"a": "2", "b": "bar"}]


def test_parse_csv_rows_returns_empty_list_for_blank_input():
    assert parse_csv_rows("") == []


def test_fetch_batter_statcast_includes_batter_id_param(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_csv("launch_speed,launch_angle\n95.0,12.0\n")

    monkeypatch.setattr(httpx, "get", fake_get)

    text = BaseballSavantClient().fetch_batter_statcast("660271", 2024)
    assert "launch_speed" in text
    assert seen["url"].endswith("/statcast_search/csv")
    assert seen["params"]["batters_lookup[]"] == "660271"
    assert seen["params"]["pitchers_lookup[]"] == ""
    assert seen["params"]["player_type"] == "batter"
    assert seen["params"]["hfSea"] == "2024|"


def test_fetch_pitcher_statcast_swaps_player_id_field(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, timeout=None):
        seen["params"] = params
        return _ok_csv("pitch_type\nFF\n")

    monkeypatch.setattr(httpx, "get", fake_get)

    BaseballSavantClient().fetch_pitcher_statcast("592450", 2024)
    assert seen["params"]["pitchers_lookup[]"] == "592450"
    assert seen["params"]["batters_lookup[]"] == ""
    assert seen["params"]["player_type"] == "pitcher"


def test_fetch_batter_percentile_rankings_uses_batter_type(monkeypatch):
    seen: dict = {}

    def fake_get(url, params=None, timeout=None):
        seen["params"] = params
        return _ok_csv("xwoba\n0.380\n")

    monkeypatch.setattr(httpx, "get", fake_get)

    BaseballSavantClient().fetch_batter_percentile_rankings(2024)
    assert seen["params"]["type"] == "batter"
    assert seen["params"]["year"] == "2024"
