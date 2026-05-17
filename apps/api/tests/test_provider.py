from datetime import date

import httpx

from app.clients.sports_data import TheSportsDBClient


def test_fetch_events_for_day_builds_expected_request(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, json={"events": [{"idEvent": "1"}]})

    monkeypatch.setattr(httpx, "get", fake_get)

    client = TheSportsDBClient(base_url="https://example.test/api/v1/json", api_key="secret")
    events = client.fetch_events_for_day("Tennis", date(2026, 3, 30))

    assert events == [{"idEvent": "1"}]
    assert captured["url"] == "https://example.test/api/v1/json/secret/eventsday.php"
    assert captured["params"] == {"d": "2026-03-30", "s": "Tennis"}
