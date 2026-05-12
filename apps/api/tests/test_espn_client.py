from datetime import date

import httpx
import pytest

from app.clients.espn import EspnPublicClient


def test_espn_client_normalizes_scoreboard_events(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "events": [
                    {
                        "id": "401705123",
                        "name": "Brooklyn Nets at Boston Celtics",
                        "date": "2026-03-30T23:00:00Z",
                        "leagues": [{"id": "46"}],
                        "status": {"type": {"completed": True, "description": "Final", "state": "post", "name": "STATUS_FINAL"}},
                        "competitions": [
                            {
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "score": "112",
                                        "team": {
                                            "id": "2",
                                            "displayName": "Boston Celtics",
                                            "shortDisplayName": "Celtics",
                                        },
                                    },
                                    {
                                        "homeAway": "away",
                                        "score": "101",
                                        "team": {
                                            "id": "17",
                                            "displayName": "Brooklyn Nets",
                                            "shortDisplayName": "Nets",
                                        },
                                    },
                                ]
                            }
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    client = EspnPublicClient()
    events = client.fetch_events_for_day("NBA", date(2026, 3, 30))

    assert len(events) == 1
    assert events[0]["strLeague"] == "NBA"
    assert events[0]["strHomeTeam"] == "Boston Celtics"
    assert events[0]["strAwayTeam"] == "Brooklyn Nets"
    assert events[0]["intHomeScore"] == "112"
    assert events[0]["strStatus"] == "completed"
    assert events[0]["source"] == "espn_public"


@pytest.mark.parametrize(
    ("status_type", "expected_status"),
    [
        ({"completed": False, "description": "Scheduled", "state": "pre", "name": "STATUS_SCHEDULED"}, "scheduled"),
        ({"completed": False, "description": "In Progress", "state": "in", "name": "STATUS_IN_PROGRESS"}, "in_progress"),
        ({"completed": True, "description": "Final", "state": "post", "name": "STATUS_FINAL"}, "completed"),
        ({"completed": False, "description": "Postponed", "state": "pre", "name": "STATUS_POSTPONED"}, "postponed"),
        ({"completed": False, "description": "Canceled", "state": "post", "name": "STATUS_CANCELED"}, "cancelled"),
    ],
)
def test_espn_client_normalizes_status_types(monkeypatch, status_type, expected_status):
    def fake_get(url, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "events": [
                    {
                        "id": "401705124",
                        "name": "Los Angeles Lakers at Oklahoma City Thunder",
                        "date": "2026-04-03T01:30:00Z",
                        "leagues": [{"id": "46"}],
                        "status": {"type": status_type},
                        "competitions": [
                            {
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "team": {
                                            "id": "25",
                                            "displayName": "Oklahoma City Thunder",
                                            "shortDisplayName": "Thunder",
                                        },
                                    },
                                    {
                                        "homeAway": "away",
                                        "team": {
                                            "id": "13",
                                            "displayName": "Los Angeles Lakers",
                                            "shortDisplayName": "Lakers",
                                        },
                                    },
                                ]
                            }
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    client = EspnPublicClient()
    events = client.fetch_events_for_day("NBA", date(2026, 4, 2))

    assert events[0]["strStatus"] == expected_status


def test_espn_client_preserves_live_scores(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "events": [
                    {
                        "id": "401705125",
                        "name": "Los Angeles Lakers at Oklahoma City Thunder",
                        "date": "2026-04-03T01:30:00Z",
                        "leagues": [{"id": "46"}],
                        "status": {"type": {"completed": False, "description": "In Progress", "state": "in", "name": "STATUS_IN_PROGRESS"}},
                        "competitions": [
                            {
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "score": "58",
                                        "team": {
                                            "id": "25",
                                            "displayName": "Oklahoma City Thunder",
                                            "shortDisplayName": "Thunder",
                                        },
                                    },
                                    {
                                        "homeAway": "away",
                                        "score": "51",
                                        "team": {
                                            "id": "13",
                                            "displayName": "Los Angeles Lakers",
                                            "shortDisplayName": "Lakers",
                                        },
                                    },
                                ]
                            }
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    client = EspnPublicClient()
    events = client.fetch_events_for_day("NBA", date(2026, 4, 2))

    assert events[0]["strStatus"] == "in_progress"
    assert events[0]["intHomeScore"] == "58"
    assert events[0]["intAwayScore"] == "51"


def test_espn_client_searches_nba_players(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "type": "player",
                        "contents": [
                            {
                                "uid": "s:40~l:46~a:3934672",
                                "displayName": "Jalen Brunson",
                                "subtitle": "New York Knicks",
                                "defaultLeagueSlug": "nba",
                                "link": {"web": "https://www.espn.com/nba/player/_/id/3934672/jalen-brunson"},
                            }
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    client = EspnPublicClient()
    player = client.search_player("Jalen Brunson")

    assert player["athlete_id"] == "3934672"
    assert player["display_name"] == "Jalen Brunson"
    assert player["team_name"] == "New York Knicks"


def test_espn_client_search_player_picks_team_hint_when_multiple_candidates(monkeypatch):
    """Bug #13: with two same-name candidates on different teams, the
    team_hint must disambiguate — otherwise the first result wins by
    accident and downstream features get attached to the wrong athlete."""
    def fake_get(url, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "type": "player",
                        "contents": [
                            {
                                "uid": "s:40~l:46~a:111111",
                                "displayName": "John Smith",
                                "subtitle": "Los Angeles Lakers",
                                "defaultLeagueSlug": "nba",
                                "link": {"web": "https://www.espn.com/nba/player/_/id/111111/john-smith"},
                            },
                            {
                                "uid": "s:40~l:46~a:222222",
                                "displayName": "John Smith",
                                "subtitle": "Boston Celtics",
                                "defaultLeagueSlug": "nba",
                                "link": {"web": "https://www.espn.com/nba/player/_/id/222222/john-smith"},
                            },
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    client = EspnPublicClient()

    # Without team_hint, the existing first-result behavior is preserved.
    default_pick = client.search_player("John Smith")
    assert default_pick["athlete_id"] == "111111"
    assert default_pick["team_name"] == "Los Angeles Lakers"

    # With a team_hint that matches the second candidate's subtitle,
    # the Celtics' John Smith is returned instead.
    boston_pick = client.search_player("John Smith", team_hint="Boston Celtics")
    assert boston_pick["athlete_id"] == "222222"
    assert boston_pick["team_name"] == "Boston Celtics"

    # The hint also accepts short forms — "Celtics" matches "Boston Celtics"
    # by substring.
    short_pick = client.search_player("John Smith", team_hint="Celtics")
    assert short_pick["athlete_id"] == "222222"

    # Codex PR #35 P2: the 3-letter ticker abbreviation (which prop
    # metadata actually sends) must resolve through the abbreviation
    # table — "BOS" → "Boston Celtics" — without that, every real
    # production hint silently fell through to the first candidate.
    abbr_pick = client.search_player("John Smith", team_hint="BOS")
    assert abbr_pick["athlete_id"] == "222222"
    assert abbr_pick["team_name"] == "Boston Celtics"


def test_espn_client_search_player_falls_back_when_team_hint_misses(monkeypatch, caplog):
    """When no candidate matches the team_hint, return the first
    candidate (existing behavior) and log a warning so ops can see the
    mismatch."""
    def fake_get(url, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "type": "player",
                        "contents": [
                            {
                                "uid": "s:40~l:46~a:333333",
                                "displayName": "Jane Doe",
                                "subtitle": "Phoenix Suns",
                                "defaultLeagueSlug": "nba",
                                "link": {"web": "https://www.espn.com/nba/player/_/id/333333/jane-doe"},
                            },
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    client = EspnPublicClient()

    import logging
    with caplog.at_level(logging.WARNING):
        result = client.search_player("Jane Doe", team_hint="Boston Celtics")
    assert result["athlete_id"] == "333333"  # fall back to first
    assert any("team_hint" in record.message for record in caplog.records)


def test_espn_client_fetches_nba_gamelog(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, json={"seasonTypes": [], "events": {}, "names": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    client = EspnPublicClient()
    payload = client.fetch_player_gamelog("NBA", "3934672", 2026)

    assert payload["seasonTypes"] == []


def test_espn_client_fetch_events_window_with_diagnostics_skips_timeout_days(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        request = httpx.Request("GET", url)
        if params == {"dates": "20260331"}:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "events": [
                    {
                        "id": params["dates"],
                        "name": "Brooklyn Nets at Boston Celtics",
                        "date": "2026-03-30T23:00:00Z",
                        "leagues": [{"id": "46"}],
                        "status": {"type": {"completed": False, "description": "Scheduled", "state": "pre", "name": "STATUS_SCHEDULED"}},
                        "competitions": [
                            {
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "team": {
                                            "id": "2",
                                            "displayName": "Boston Celtics",
                                            "shortDisplayName": "Celtics",
                                        },
                                    },
                                    {
                                        "homeAway": "away",
                                        "team": {
                                            "id": "17",
                                            "displayName": "Brooklyn Nets",
                                            "shortDisplayName": "Nets",
                                        },
                                    },
                                ]
                            }
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    client = EspnPublicClient()
    events, errors = client.fetch_events_window_with_diagnostics("NBA", date(2026, 3, 30), date(2026, 3, 31))

    assert len(events) == 1
    assert events[0]["idEvent"] == "20260330"
    assert events[0]["strStatus"] == "scheduled"
    assert errors == ["2026-03-31: ReadTimeout: The read operation timed out"]
