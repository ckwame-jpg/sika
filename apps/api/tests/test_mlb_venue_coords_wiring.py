"""Tests for bug #4 — wire MLB park coordinates into the weather
loader call.

Phase 1 of bug #4 (long ago) wired the park-factor lookup via venue
NAME — that path works. The remaining issue is the weather call
that's invoked with ``lat=None, lon=None`` in the player-prop scoring
branch, so the weather cache effectively can't return data for any
specific game (the cache key requires non-null coordinates).

This PR wires ``mlb_park_coords(home_team_abbreviation)`` into the
existing ``load_weather`` call so the scoring path passes real
lat/lon. Coordinates come from the same hardcoded ``_MLB_PARK_COORDS``
table the (deferred) Smarter #15 weather pre-warm job will use, so
the read path and the warm path stay aligned on a single source of
truth.

The is_dome signal stays driven by ESPN's per-game ``venue.indoor``
flag (catches retractable-roof openings/closings) rather than the
hardcoded coords table's coarse always-dome flag.
"""

from __future__ import annotations

import inspect

from app.services import scoring
from app.services.mlb_advanced import mlb_park_coords


def test_mlb_park_coords_returns_lat_lon_for_known_team() -> None:
    """Sanity check on the helper the wiring depends on."""
    coords = mlb_park_coords("BOS")
    assert coords is not None
    lat, lon, _is_dome = coords
    # Fenway is at ~42.34, -71.10
    assert 42.0 < lat < 43.0
    assert -72.0 < lon < -71.0


def test_score_player_prop_imports_mlb_park_coords_helper() -> None:
    """Source-level pin: scoring.py:_score_player_prop must import
    and call ``mlb_park_coords`` so the weather loader gets real
    lat/lon instead of None. A future refactor that drops this
    silently re-introduces bug #4 without test signal."""
    source = inspect.getsource(scoring._score_player_prop)
    assert "mlb_park_coords" in source


def test_score_player_prop_passes_real_coords_to_load_weather() -> None:
    """Confirm the wired lat/lon flow into ``load_weather``."""
    source = inspect.getsource(scoring._score_player_prop)
    # The load_weather call must reference the resolved coordinates,
    # not the bare ``lat=None, lon=None`` placeholder.
    assert "load_weather(" in source
    assert "lat=None" not in source.split("load_weather(")[-1].split(")")[0]
    assert "lon=None" not in source.split("load_weather(")[-1].split(")")[0]


def test_event_venue_context_includes_venue_id() -> None:
    """Bug #4 fix: ``_event_venue_context`` should surface the ESPN
    venue.id alongside name/city/state/indoor so park-factors can
    fall back to ID-keyed lookup if the name lookup fails."""
    from types import SimpleNamespace

    event = SimpleNamespace(raw_data={
        "raw": {
            "competitions": [
                {
                    "venue": {
                        "id": "12345",
                        "fullName": "Test Park",
                        "indoor": False,
                        "address": {"city": "Boston", "state": "MA"},
                    }
                }
            ]
        }
    })

    context = scoring._event_venue_context(event)
    assert context["venue_id"] == "12345"
    assert context["venue_name"] == "Test Park"
    assert context["venue_city"] == "Boston"
    assert context["venue_state"] == "MA"
    assert context["venue_indoor"] is False


def test_event_venue_context_handles_missing_venue_id() -> None:
    """A venue dict without an ``id`` key (older ESPN rows) returns
    None for venue_id without raising."""
    from types import SimpleNamespace

    event = SimpleNamespace(raw_data={
        "raw": {
            "competitions": [
                {"venue": {"fullName": "Old Park", "address": {"city": "Anywhere"}}}
            ]
        }
    })

    context = scoring._event_venue_context(event)
    assert context["venue_id"] is None
    assert context["venue_name"] == "Old Park"


def test_event_venue_context_handles_missing_competition() -> None:
    """Bare event.raw_data without competitions returns all-None
    context — defensive against incomplete fixtures / new sports."""
    from types import SimpleNamespace

    event = SimpleNamespace(raw_data={})

    context = scoring._event_venue_context(event)
    assert context["venue_id"] is None
    assert context["venue_name"] is None
