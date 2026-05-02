from datetime import datetime, timezone

import httpx
import pytest

from app.clients import _rate_limit
from app.clients.weather import WeatherClient
from app.config import get_settings


@pytest.fixture(autouse=True)
def _reset_registry_and_settings():
    _rate_limit.reset_for_tests()
    get_settings.cache_clear()
    yield
    _rate_limit.reset_for_tests()
    get_settings.cache_clear()


def _ok_json(payload: dict) -> httpx.Response:
    request = httpx.Request("GET", "https://example.com")
    return httpx.Response(200, request=request, json=payload)


def test_openweather_path_when_api_key_set(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "openweather_api_key", "sentinel-key")

    seen: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _ok_json({
            "current": {"dt": 1750000000, "temp": 78.0, "wind_speed": 9.0, "wind_deg": 90,
                        "humidity": 60, "pop": 0.05},
            "hourly": [
                {"dt": 1750000000, "temp": 78.0, "wind_speed": 9.0, "wind_deg": 90,
                 "humidity": 60, "pop": 0.10},
            ],
        })

    monkeypatch.setattr(httpx, "get", fake_get)

    client = WeatherClient()
    weather = client.fetch_game_weather(
        lat=40.83,
        lon=-73.93,
        game_time_utc=datetime.fromtimestamp(1750000000, tz=timezone.utc),
    )
    assert seen["url"].startswith("https://api.openweathermap.org")
    assert seen["params"]["appid"] == "sentinel-key"
    assert weather["source"] == "openweather"
    assert weather["temp_f"] == 78.0
    assert weather["wind_speed_mph"] == 9.0
    assert weather["precip_pct"] == pytest.approx(10.0)


def test_falls_back_to_nws_when_no_api_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "openweather_api_key", "")

    forecast_url = "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly"

    def fake_get(url, params=None, headers=None, timeout=None):
        if "/points/" in url:
            return _ok_json({"properties": {"forecastHourly": forecast_url}})
        if url == forecast_url:
            return _ok_json({
                "properties": {
                    "periods": [
                        {
                            "startTime": "2025-06-15T19:00:00+00:00",
                            "temperature": 76,
                            "windSpeed": "10 mph",
                            "windDirection": "S",
                            "probabilityOfPrecipitation": {"value": 20},
                            "relativeHumidity": {"value": 55},
                        }
                    ]
                }
            })
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx, "get", fake_get)

    client = WeatherClient()
    weather = client.fetch_game_weather(
        lat=40.83,
        lon=-73.93,
        game_time_utc=datetime(2025, 6, 15, 19, 0, tzinfo=timezone.utc),
    )
    assert weather["source"] == "nws"
    assert weather["temp_f"] == 76.0
    assert weather["wind_speed_mph"] == 10.0
    assert weather["wind_dir_deg"] == 180.0


def test_openweather_failure_falls_back_to_nws(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "openweather_api_key", "sentinel-key")
    forecast_url = "https://api.weather.gov/gridpoints/X/1,1/forecast/hourly"

    def fake_get(url, params=None, headers=None, timeout=None):
        if "openweathermap.org" in url:
            request = httpx.Request("GET", url)
            return httpx.Response(500, request=request, text="server error")
        if "/points/" in url:
            return _ok_json({"properties": {"forecastHourly": forecast_url}})
        if url == forecast_url:
            return _ok_json({
                "properties": {
                    "periods": [
                        {"startTime": "2025-06-15T19:00:00+00:00", "temperature": 70,
                         "windSpeed": "5 mph", "windDirection": "W",
                         "probabilityOfPrecipitation": {"value": 0},
                         "relativeHumidity": {"value": 50}}
                    ]
                }
            })
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx, "get", fake_get)

    weather = WeatherClient().fetch_game_weather(
        lat=40.83, lon=-73.93,
        game_time_utc=datetime(2025, 6, 15, 19, 0, tzinfo=timezone.utc),
    )
    assert weather["source"] == "nws"
    assert weather["temp_f"] == 70.0
