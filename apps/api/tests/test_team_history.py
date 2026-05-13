"""Team-history endpoint coverage for the trade-ticket pick-history strip.

Two layers:
- ``StatsQueryService.query_team_history`` shape: pulls schedule via the
  ESPN client, parses out completed games, sorts newest first, clips to
  the requested ``n``.
- POST ``/research/teams/history`` endpoint: 200 happy path, 404 on
  unknown team, 422 on a too-short ``team_name``.
"""

from __future__ import annotations

from app.api.routes import get_stats_query_service
from app.main import app
from app.services.stats_query import StatsQueryService, _build_team_results, _filter_team_results


TEAM_SCHEDULE_PAYLOAD = {
    "team": {"id": "5", "displayName": "Cleveland Cavaliers", "abbreviation": "CLE"},
    "events": [
        {
            "id": "401",
            "date": "2026-05-09T19:00:00Z",
            "competitions": [
                {
                    "status": {"type": {"completed": True, "state": "post"}},
                    "competitors": [
                        {
                            "homeAway": "home",
                            "team": {"id": "5", "displayName": "Cleveland Cavaliers", "abbreviation": "CLE"},
                            "score": {"value": 116},
                            "winner": True,
                        },
                        {
                            "homeAway": "away",
                            "team": {"id": "8", "displayName": "Detroit Pistons", "abbreviation": "DET"},
                            "score": {"value": 109},
                            "winner": False,
                        },
                    ],
                }
            ],
        },
        {
            "id": "402",
            "date": "2026-05-07T23:00:00Z",
            "competitions": [
                {
                    "status": {"type": {"completed": True, "state": "post"}},
                    "competitors": [
                        {
                            "homeAway": "away",
                            "team": {"id": "5", "displayName": "Cleveland Cavaliers", "abbreviation": "CLE"},
                            "score": {"value": 97},
                            "winner": False,
                        },
                        {
                            "homeAway": "home",
                            "team": {"id": "8", "displayName": "Detroit Pistons", "abbreviation": "DET"},
                            "score": {"value": 107},
                            "winner": True,
                        },
                    ],
                }
            ],
        },
        {
            "id": "403",
            "date": "2026-05-12T19:00:00Z",
            "competitions": [
                {
                    "status": {"type": {"completed": False, "state": "pre"}},
                    "competitors": [
                        {"homeAway": "home", "team": {"id": "5"}},
                        {"homeAway": "away", "team": {"id": "8"}},
                    ],
                }
            ],
        },
    ],
}


class _FakeEspnClient:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, str]] = []
        self.schedule_calls: list[tuple[str, str]] = []

    def search_team(self, query: str, sport_key: str = "NBA") -> dict:
        self.search_calls.append((query, sport_key))
        if "cavaliers" in query.lower():
            return {
                "team_id": "5",
                "sport_key": sport_key.upper(),
                "display_name": "Cleveland Cavaliers",
                "abbreviation": "CLE",
                "raw": {},
            }
        raise LookupError(f"No team found for {query}")

    def fetch_team_schedule(self, sport_key: str, team_id: str, season=None) -> dict:
        self.schedule_calls.append((sport_key, team_id))
        return TEAM_SCHEDULE_PAYLOAD


def test_build_team_results_keeps_completed_games_in_newest_first_order():
    results = _build_team_results(TEAM_SCHEDULE_PAYLOAD, self_team_id="5")

    assert len(results) == 2, "Upcoming (non-completed) event must be filtered out."
    first, second = results
    assert first["game_date"] == "2026-05-09T19:00:00Z"
    assert first["opponent"] == "Detroit Pistons"
    assert first["opponent_abbreviation"] == "DET"
    assert first["location"] == "home"
    assert first["team_score"] == 116
    assert first["opp_score"] == 109
    assert first["result"] == "W"
    assert second["game_date"] == "2026-05-07T23:00:00Z"
    assert second["location"] == "away"
    assert second["result"] == "L"


def test_query_team_history_returns_clipped_results():
    fake = _FakeEspnClient()
    service = StatsQueryService(espn_client=fake)

    result = service.query_team_history("Cleveland Cavaliers", sport_key="NBA", n=1)

    assert result["entity_id"] == "5"
    assert result["team_name"] == "Cleveland Cavaliers"
    assert result["sport_key"] == "NBA"
    assert len(result["results"]) == 1
    assert result["results"][0]["opponent"] == "Detroit Pistons"
    # The service forwarded the normalized sport key and team id to ESPN.
    assert fake.search_calls == [("Cleveland Cavaliers", "NBA")]
    assert fake.schedule_calls == [("NBA", "5")]


def test_team_history_endpoint_happy_path(client):
    fake = _FakeEspnClient()

    class FakeService(StatsQueryService):
        def __init__(self) -> None:
            super().__init__(espn_client=fake)

    app.dependency_overrides[get_stats_query_service] = FakeService
    try:
        response = client.post(
            "/research/teams/history",
            json={"team_name": "Cleveland Cavaliers", "sport_key": "NBA", "n": 5},
        )
    finally:
        app.dependency_overrides.pop(get_stats_query_service, None)

    assert response.status_code == 200
    body = response.json()
    assert body["entity_id"] == "5"
    assert body["team_name"] == "Cleveland Cavaliers"
    assert len(body["results"]) == 2
    assert body["results"][0]["result"] == "W"


def test_team_history_endpoint_404_when_team_missing(client):
    fake = _FakeEspnClient()

    class FakeService(StatsQueryService):
        def __init__(self) -> None:
            super().__init__(espn_client=fake)

    app.dependency_overrides[get_stats_query_service] = FakeService
    try:
        response = client.post(
            "/research/teams/history",
            json={"team_name": "Nonexistent Squad", "sport_key": "NBA", "n": 3},
        )
    finally:
        app.dependency_overrides.pop(get_stats_query_service, None)

    assert response.status_code == 404


def test_team_history_endpoint_422_on_short_team_name(client):
    response = client.post(
        "/research/teams/history",
        json={"team_name": "X", "sport_key": "NBA"},
    )
    assert response.status_code == 422


def test_filter_team_results_by_opponent_substring():
    results = [
        {"opponent": "Detroit Pistons", "opponent_abbreviation": "DET", "location": "home"},
        {"opponent": "Boston Celtics", "opponent_abbreviation": "BOS", "location": "away"},
        {"opponent": "Detroit Pistons", "opponent_abbreviation": "DET", "location": "away"},
    ]
    filtered = _filter_team_results(results, opponent="pistons")
    assert len(filtered) == 2
    assert all("pistons" in row["opponent"].lower() for row in filtered)


def test_filter_team_results_by_location():
    results = [
        {"opponent": "Detroit Pistons", "opponent_abbreviation": "DET", "location": "home"},
        {"opponent": "Boston Celtics", "opponent_abbreviation": "BOS", "location": "away"},
    ]
    home_only = _filter_team_results(results, location="home")
    assert [row["location"] for row in home_only] == ["home"]
    away_only = _filter_team_results(results, location="away")
    assert [row["location"] for row in away_only] == ["away"]


def test_filter_team_results_no_filters_pass_through():
    results = [{"opponent": "Anyone", "opponent_abbreviation": "ANY", "location": "home"}]
    assert _filter_team_results(results) == results
    assert _filter_team_results(results, opponent=None, location=None) == results


def test_team_history_endpoint_passes_filters(client):
    captured: dict = {}

    fake = _FakeEspnClient()

    class FakeService(StatsQueryService):
        def __init__(self) -> None:
            super().__init__(espn_client=fake)

        def query_team_history(self, team_name, sport_key="NBA", n=5, *, opponent=None, location=None):
            captured["team_name"] = team_name
            captured["sport_key"] = sport_key
            captured["n"] = n
            captured["opponent"] = opponent
            captured["location"] = location
            return {
                "entity_id": "5",
                "team_name": team_name,
                "sport_key": sport_key,
                "results": [],
            }

    app.dependency_overrides[get_stats_query_service] = FakeService
    try:
        response = client.post(
            "/research/teams/history",
            json={
                "team_name": "Cleveland Cavaliers",
                "sport_key": "NBA",
                "n": 5,
                "opponent": "Pistons",
                "location": "home",
            },
        )
    finally:
        app.dependency_overrides.pop(get_stats_query_service, None)

    assert response.status_code == 200
    assert captured == {
        "team_name": "Cleveland Cavaliers",
        "sport_key": "NBA",
        "n": 5,
        "opponent": "Pistons",
        "location": "home",
    }


# -----------------------------------------------------------------------------
# Codex round-1 P2 on PR #24 — accept scalar scores in team schedules
# -----------------------------------------------------------------------------


def test_build_team_results_handles_scalar_score_strings():
    """ESPN's ``site/v2`` shape sometimes returns ``"score": "112"`` (a
    scalar string) instead of the dict form ``{"value": 112}`` — most
    notably on completed-game payloads. The previous extractor called
    ``.get("value")`` on the scalar and raised ``AttributeError`` so
    ``/research/teams/history`` 500'd."""
    scalar_payload = {
        "team": {"id": "5", "displayName": "Cleveland Cavaliers"},
        "events": [
            {
                "id": "401-scalar",
                "date": "2026-05-09T19:00:00Z",
                "competitions": [
                    {
                        "status": {"type": {"completed": True, "state": "post"}},
                        "competitors": [
                            {
                                "homeAway": "home",
                                "team": {
                                    "id": "5",
                                    "displayName": "Cleveland Cavaliers",
                                },
                                "score": "116",  # scalar string
                                "winner": True,
                            },
                            {
                                "homeAway": "away",
                                "team": {
                                    "id": "8",
                                    "displayName": "Detroit Pistons",
                                    "abbreviation": "DET",
                                },
                                "score": 109,  # scalar int
                                "winner": False,
                            },
                        ],
                    }
                ],
            }
        ],
    }

    results = _build_team_results(scalar_payload, self_team_id="5")

    assert len(results) == 1
    row = results[0]
    assert row["team_score"] == 116
    assert row["opp_score"] == 109
    assert row["result"] == "W"


# -----------------------------------------------------------------------------
# Codex round-3 P2 on PR #24 — cancelled/postponed events excluded
# -----------------------------------------------------------------------------


def test_build_team_results_excludes_cancelled_and_postponed_events():
    """Cancellations/postponements ship with ``state == "post"`` and
    no scores. The previous ``state == "post"`` fallback let them
    through and they surfaced in the strip as 0-0 losses."""
    payload = {
        "team": {"id": "5", "displayName": "Cleveland Cavaliers"},
        "events": [
            {
                "id": "cancelled-game",
                "date": "2026-05-09T19:00:00Z",
                "competitions": [
                    {
                        "status": {
                            "type": {
                                "completed": False,
                                "state": "post",
                                "name": "STATUS_CANCELED",
                            }
                        },
                        "competitors": [
                            {"homeAway": "home", "team": {"id": "5"}},
                            {"homeAway": "away", "team": {"id": "8"}},
                        ],
                    }
                ],
            },
            {
                "id": "postponed-game",
                "date": "2026-05-08T19:00:00Z",
                "competitions": [
                    {
                        "status": {
                            "type": {
                                "completed": False,
                                "state": "post",
                                "name": "STATUS_POSTPONED",
                            }
                        },
                        "competitors": [
                            {"homeAway": "home", "team": {"id": "5"}},
                            {"homeAway": "away", "team": {"id": "8"}},
                        ],
                    }
                ],
            },
            {
                "id": "real-final",
                "date": "2026-05-07T19:00:00Z",
                "competitions": [
                    {
                        "status": {
                            "type": {
                                "completed": False,
                                "state": "post",
                                "name": "STATUS_FINAL",
                            }
                        },
                        "competitors": [
                            {
                                "homeAway": "home",
                                "team": {"id": "5", "displayName": "Cleveland Cavaliers"},
                                "score": 112,
                                "winner": True,
                            },
                            {
                                "homeAway": "away",
                                "team": {"id": "8", "displayName": "Detroit Pistons"},
                                "score": 100,
                                "winner": False,
                            },
                        ],
                    }
                ],
            },
        ],
    }

    results = _build_team_results(payload, self_team_id="5")

    assert len(results) == 1, "Only the STATUS_FINAL game should survive."
    assert results[0]["team_score"] == 112
    assert results[0]["opp_score"] == 100
