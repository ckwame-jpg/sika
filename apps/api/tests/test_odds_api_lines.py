"""Smarter NFL PR 4 — multi-market Odds API fetch + lines cache +
NFL consensus anchor.

Covers: the 3-market fetch param + quota header capture, median
spread/total consensus, the per-sport TTL override, the event-window
budget gate (the mechanism that keeps a 4th sport inside the free
tier's 500 req/mo), and the home-oriented anchor incl. the swapped-
orientation path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app.clients.the_odds_api import (
    TheOddsApiClient,
    consensus_spread_point,
    consensus_total_point,
)
from app.models import Event, EventParticipant, Participant
from app.services.nfl_market_anchor import nfl_consensus_anchor
from app.services.odds_api_cache import cached_event_lines, cached_h2h_odds


NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(hours=28)


def _bookmakers() -> list[dict[str, Any]]:
    def book(key: str, home_ml: float, away_ml: float, home_point: float, total: float):
        return {
            "key": key,
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Philadelphia Eagles", "price": home_ml},
                    {"name": "Dallas Cowboys", "price": away_ml},
                ]},
                {"key": "spreads", "outcomes": [
                    {"name": "Philadelphia Eagles", "price": 1.91, "point": home_point},
                    {"name": "Dallas Cowboys", "price": 1.91, "point": -home_point},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": 1.91, "point": total},
                    {"name": "Under", "price": 1.91, "point": total},
                ]},
            ],
        }

    return [
        book("draftkings", 1.5, 2.7, -3.5, 47.5),
        book("fanduel", 1.52, 2.62, -3.5, 47.0),
        book("caesars", 1.48, 2.75, -4.0, 48.0),
    ]


def _odds_event(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "abc123",
        "sport_key": "americanfootball_nfl",
        "commence_time": KICKOFF.isoformat().replace("+00:00", "Z"),
        "home_team": "Philadelphia Eagles",
        "away_team": "Dallas Cowboys",
        "bookmakers": _bookmakers(),
    }
    base.update(overrides)
    return base


class _StubHttp:
    def __init__(self, payload: Any, headers: dict[str, str] | None = None):
        self.payload = payload
        self.headers = headers or {}
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return httpx.Response(
            200, json=self.payload, headers=self.headers,
            request=httpx.Request("GET", url),
        )


@pytest.fixture()
def _api_key(monkeypatch):
    from app import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "the_odds_api_key", "test-key")
    yield
    # get_settings is lru_cached; the monkeypatched attribute reverts
    # automatically because monkeypatch restores the original value.


def _make_nfl_event(db_session) -> Event:
    event = Event(
        external_id="espn:nfl:401777",
        sport_key="NFL",
        name="Dallas Cowboys at Philadelphia Eagles",
        status="scheduled",
        starts_at=KICKOFF,
    )
    db_session.add(event)
    db_session.flush()
    for name, is_home in (("Philadelphia Eagles", True), ("Dallas Cowboys", False)):
        participant = Participant(
            external_id=f"espn:nfl:team:{name}",
            sport_key="NFL",
            display_name=name,
        )
        db_session.add(participant)
        db_session.flush()
        db_session.add(EventParticipant(
            event_id=event.id, participant_id=participant.id,
            role="competitor", is_home=is_home,
        ))
    db_session.flush()
    return event


# -- Client ---------------------------------------------------------------

def test_fetch_odds_with_quota_sends_markets_and_captures_headers(_api_key) -> None:
    stub = _StubHttp([_odds_event()], headers={
        "x-requests-remaining": "412", "x-requests-used": "88",
    })
    client = TheOddsApiClient(http_client=stub)
    events, quota = client.fetch_odds_with_quota("NFL", markets="h2h,spreads,totals")
    assert len(events) == 1
    assert quota == {"requests_remaining": 412, "requests_used": 88}
    params = stub.calls[0]["params"]
    assert params["markets"] == "h2h,spreads,totals"
    assert "americanfootball_nfl" in stub.calls[0]["url"]


def test_fetch_h2h_odds_still_single_market(_api_key) -> None:
    stub = _StubHttp([])
    TheOddsApiClient(http_client=stub).fetch_h2h_odds("NBA")
    assert stub.calls[0]["params"]["markets"] == "h2h"


def test_consensus_spread_point_takes_median_for_named_team() -> None:
    result = consensus_spread_point(_bookmakers(), team_name="Philadelphia Eagles")
    assert result is not None
    point, books = result
    assert point == -3.5  # median of -3.5, -3.5, -4.0
    assert books == 3
    away = consensus_spread_point(_bookmakers(), team_name="Dallas Cowboys")
    assert away is not None and away[0] == 3.5


def test_consensus_total_point_takes_median() -> None:
    result = consensus_total_point(_bookmakers())
    assert result is not None
    assert result == (47.5, 3)


def test_consensus_helpers_handle_missing_markets() -> None:
    bare = [{"key": "dk", "markets": [{"key": "h2h", "outcomes": []}]}]
    assert consensus_spread_point(bare, team_name="X") is None
    assert consensus_total_point(bare) is None


# -- Cache: per-sport TTL + budget gate --------------------------------------

def test_cached_h2h_odds_uses_nfl_ttl_override(db_session, _api_key) -> None:
    stub = _StubHttp([_odds_event()])
    events = cached_h2h_odds(
        db_session, "NFL",
        client=TheOddsApiClient(http_client=stub),
        allow_network=True, now=NOW,
    )
    assert len(events) == 1
    from app.models import OperatorSetting

    row = db_session.query(OperatorSetting).filter(
        OperatorSetting.key == "odds_api_h2h_NFL"
    ).one()
    expires_at = datetime.fromisoformat(row.value["expires_at"])
    # NFL override: 360 minutes, not the 30-minute global default.
    assert expires_at - NOW == timedelta(minutes=360)


def test_cached_event_lines_gated_when_no_upcoming_event(db_session, _api_key) -> None:
    stub = _StubHttp([_odds_event()])
    result = cached_event_lines(
        db_session, "NFL",
        client=TheOddsApiClient(http_client=stub),
        allow_network=True, now=NOW,
    )
    # No NFL event within 48h in the DB → no fetch spent.
    assert result["events"] == []
    assert stub.calls == []


def test_cached_event_lines_fetches_caches_and_reuses(db_session, _api_key) -> None:
    _make_nfl_event(db_session)
    stub = _StubHttp([_odds_event()], headers={"x-requests-remaining": "400"})
    client = TheOddsApiClient(http_client=stub)
    first = cached_event_lines(db_session, "NFL", client=client, allow_network=True, now=NOW)
    assert len(first["events"]) == 1
    assert first["requests_remaining"] == 400
    assert first["fetched_at"] is not None
    # Second call inside the TTL serves from cache — no extra credits.
    second = cached_event_lines(db_session, "NFL", client=client, allow_network=True, now=NOW + timedelta(minutes=30))
    assert len(second["events"]) == 1
    assert len(stub.calls) == 1


def test_cached_event_lines_serves_stale_within_ceiling_on_failure(db_session, _api_key) -> None:
    _make_nfl_event(db_session)
    good = TheOddsApiClient(http_client=_StubHttp([_odds_event()]))
    cached_event_lines(db_session, "NFL", client=good, allow_network=True, now=NOW)

    class _FailingHttp:
        def get(self, url: str, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    failing = TheOddsApiClient(http_client=_FailingHttp())
    # Just past TTL (6h) but inside the 2x stale ceiling.
    stale_now = NOW + timedelta(hours=7)
    result = cached_event_lines(db_session, "NFL", client=failing, allow_network=True, now=stale_now)
    assert len(result["events"]) == 1
    # Way past the ceiling → empty.
    far_now = NOW + timedelta(hours=30)
    result = cached_event_lines(db_session, "NFL", client=failing, allow_network=True, now=far_now)
    assert result["events"] == []


# -- Anchor -------------------------------------------------------------------

def test_nfl_consensus_anchor_home_oriented(db_session, _api_key) -> None:
    event = _make_nfl_event(db_session)
    stub = _StubHttp([_odds_event()])
    anchor = nfl_consensus_anchor(
        db_session, event,
        client=TheOddsApiClient(http_client=stub),
        allow_network=True, now=NOW,
    )
    assert anchor is not None
    assert anchor.spread_home == -3.5
    assert anchor.total_line == 47.5
    assert anchor.book_count == 3
    # Home ML ~1.50 vs away ~2.70 → devigged home prob ≈ 0.64.
    assert anchor.win_prob_home is not None
    assert 0.60 < anchor.win_prob_home < 0.68
    assert anchor.fetched_at is not None


def test_nfl_consensus_anchor_handles_swapped_orientation(db_session, _api_key) -> None:
    """When the Odds API flips home/away vs sika, the anchor must still
    be oriented to SIKA's home team (spread flips sign)."""
    event = _make_nfl_event(db_session)
    swapped = _odds_event(
        home_team="Dallas Cowboys",
        away_team="Philadelphia Eagles",
    )
    anchor = nfl_consensus_anchor(
        db_session, event,
        client=TheOddsApiClient(http_client=_StubHttp([swapped])),
        allow_network=True, now=NOW,
    )
    assert anchor is not None
    # Eagles (sika home) are still the -3.5 side regardless of upstream orientation.
    assert anchor.spread_home == -3.5
    assert anchor.win_prob_home is not None and anchor.win_prob_home > 0.5


def test_nfl_consensus_anchor_none_when_no_match(db_session, _api_key) -> None:
    event = _make_nfl_event(db_session)
    unrelated = _odds_event(
        home_team="Green Bay Packers", away_team="Chicago Bears",
    )
    anchor = nfl_consensus_anchor(
        db_session, event,
        client=TheOddsApiClient(http_client=_StubHttp([unrelated])),
        allow_network=True, now=NOW,
    )
    assert anchor is None
