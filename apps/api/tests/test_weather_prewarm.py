"""Smarter #15 — MLB game-day weather pre-warm.

Covers:
- ``mlb_park_coords`` resolution + ESPN alias mapping + unknown teams.
- ``weather_refresh`` walks today's slate, looks up coords, populates the
  weather cache, and tracks per-bucket counters.
- Dome venues skip the network call but count under ``events_dome``.
- One bad game does not poison the rest of the slate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import Event, MlbWeatherCache, RefreshJob
from app.services import mlb_advanced, refresh_jobs


@pytest.fixture()
def _share_session_local(db_session, monkeypatch):
    """Make ``refresh_jobs.SessionLocal()`` return sessions bound to the
    test ``db_session``'s engine so the job handler sees test fixtures."""

    bind = db_session.get_bind()
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=bind, future=True)
    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: TestingSessionLocal())
    return TestingSessionLocal


# -- mlb_park_coords ---------------------------------------------------------


def test_mlb_park_coords_resolves_canonical_team() -> None:
    coords = mlb_advanced.mlb_park_coords("NYY")
    assert coords is not None
    lat, lon, is_dome = coords
    assert 40 < lat < 41 and -74 < lon < -73 and is_dome is False


def test_mlb_park_coords_resolves_dome_for_tampa() -> None:
    coords = mlb_advanced.mlb_park_coords("TBR")
    assert coords is not None
    assert coords[2] is True


def test_mlb_park_coords_resolves_espn_two_letter_aliases() -> None:
    """ESPN uses SF/SD/TB; the table is keyed on the FanGraphs SFG/SDP/TBR."""

    for espn_code in ("SF", "SD", "TB", "KC", "WSH"):
        assert mlb_advanced.mlb_park_coords(espn_code) is not None


def test_mlb_park_coords_returns_none_for_unknown() -> None:
    assert mlb_advanced.mlb_park_coords("XYZ") is None
    assert mlb_advanced.mlb_park_coords(None) is None
    assert mlb_advanced.mlb_park_coords("") is None
    assert mlb_advanced.mlb_park_coords("  ") is None


def test_mlb_park_coords_table_covers_all_30_active_franchises() -> None:
    """Pattern 6 (data shape): the slate walks home-team abbreviations
    against the table; missing entries silently increment
    ``events_missing_coords``. Lock in coverage of all 30 active
    franchises so a stale day silently doesn't drop half the slate."""

    expected = {
        "ARI", "ATL", "BAL", "BOS", "CHC", "CHW", "CIN", "CLE", "COL", "DET",
        "HOU", "KCR", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY", "OAK",
        "PHI", "PIT", "SDP", "SEA", "SFG", "STL", "TBR", "TEX", "TOR", "WSN",
    }
    for team in expected:
        assert mlb_advanced.mlb_park_coords(team) is not None, f"missing coords for {team}"


# -- weather_refresh job execution -------------------------------------------


def _queued_weather_job(db_session) -> RefreshJob:
    job = RefreshJob(kind="weather_refresh", scope="maintenance", reason="cron", status="claimed")
    db_session.add(job)
    db_session.commit()
    return job


_event_counter = {"n": 0}


def _make_event(db_session, *, starts_at: datetime) -> Event:
    _event_counter["n"] += 1
    event = Event(
        external_id=f"mlb-{starts_at.isoformat()}-{_event_counter['n']}",
        sport_key="MLB",
        name="New York Yankees at Boston Red Sox",
        starts_at=starts_at,
        status="scheduled",
        raw_data={},
    )
    db_session.add(event)
    db_session.flush()
    return event


def _schedule_with_one_game(*, home_abbr: str, game_pk: int = 700000) -> dict:
    return {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": game_pk,
                        "teams": {
                            "home": {"team": {"abbreviation": home_abbr, "name": "Home"}},
                            "away": {"team": {"abbreviation": "AWY", "name": "Away"}},
                        },
                    }
                ]
            }
        ]
    }


def test_weather_refresh_warms_cache_for_outdoor_park(db_session, _share_session_local) -> None:
    """End-to-end happy path: schedule has one outdoor game, the home park
    resolves to coords, load_weather is invoked with allow_network=True and
    the result is counted as warmed."""

    # Seed a Yankees home event matching today.
    starts_at = datetime.now(timezone.utc).replace(microsecond=0)
    _make_event(db_session, starts_at=starts_at)
    job = _queued_weather_job(db_session)

    # Stub the MLB Stats client + team-token matching so the test doesn't
    # need a real schedule.
    fake_schedule = _schedule_with_one_game(home_abbr="NYY")
    with patch("app.clients.mlb_stats.MlbStatsClient") as MockClient, patch.object(
        refresh_jobs, "_match_mlb_event"
    ) as match_event, patch(
        "app.services.mlb_advanced.load_weather"
    ) as mock_load_weather:
        MockClient.return_value.fetch_schedule.return_value = fake_schedule
        match_event.side_effect = lambda _index, _game: db_session.query(Event).first()
        mock_load_weather.return_value = mlb_advanced.AdvancedLoadResult(
            payload={"temp_f": 65, "source": "stub"},
            cache_status="miss",
            complete=True,
        )
        result = refresh_jobs._execute_claimed_job(job.id)

    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    details = dict(persisted.details or {})
    assert details["events_warmed"] == 1
    assert details["events_dome"] == 0
    assert details["events_missing_coords"] == 0
    assert details["games_unmatched"] == 0
    assert details["games_failed"] == 0
    assert details["schedule_failed"] is False


def test_weather_refresh_counts_dome_separately(db_session, _share_session_local) -> None:
    """Dome parks short-circuit ``load_weather`` to a synthetic payload
    (cache_status == "dome"). They count under ``events_dome``, not
    ``events_warmed`` — useful for distinguishing "warmed forecast" from
    "fixed-roof default" in ops dashboards."""

    starts_at = datetime.now(timezone.utc).replace(microsecond=0)
    _make_event(db_session, starts_at=starts_at)
    job = _queued_weather_job(db_session)

    fake_schedule = _schedule_with_one_game(home_abbr="TBR")
    with patch("app.clients.mlb_stats.MlbStatsClient") as MockClient, patch.object(
        refresh_jobs, "_match_mlb_event"
    ) as match_event:
        MockClient.return_value.fetch_schedule.return_value = fake_schedule
        match_event.side_effect = lambda _index, _game: db_session.query(Event).first()
        refresh_jobs._execute_claimed_job(job.id)

    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    details = dict(persisted.details or {})
    assert details["events_dome"] == 1
    assert details["events_warmed"] == 0


def test_weather_refresh_per_game_failure_does_not_poison_slate(db_session, _share_session_local) -> None:
    """Pattern 6 data-shape: a malformed game (raises in load_weather) must
    increment ``games_failed`` but not abort the iteration over remaining
    games. Mirrors the lineup_refresh hardening codex flagged in round 3."""

    starts_at = datetime.now(timezone.utc).replace(microsecond=0)
    event_a = _make_event(db_session, starts_at=starts_at)
    event_b = _make_event(db_session, starts_at=starts_at)
    job = _queued_weather_job(db_session)

    fake_schedule = {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 100,
                        "teams": {"home": {"team": {"abbreviation": "NYY", "name": "Home"}}, "away": {"team": {}}},
                    },
                    {
                        "gamePk": 101,
                        "teams": {"home": {"team": {"abbreviation": "LAD", "name": "Home"}}, "away": {"team": {}}},
                    },
                ]
            }
        ]
    }

    calls = {"n": 0}

    def _flaky_load(db, *args, **kwargs):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated upstream failure")
        return mlb_advanced.AdvancedLoadResult(
            payload={"temp_f": 70, "source": "stub"},
            cache_status="miss",
            complete=True,
        )

    matches = iter([event_a, event_b])
    with patch("app.clients.mlb_stats.MlbStatsClient") as MockClient, patch.object(
        refresh_jobs, "_match_mlb_event"
    ) as match_event, patch(
        "app.services.mlb_advanced.load_weather", side_effect=_flaky_load
    ):
        MockClient.return_value.fetch_schedule.return_value = fake_schedule
        match_event.side_effect = lambda _index, _game: next(matches)
        refresh_jobs._execute_claimed_job(job.id)

    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    details = dict(persisted.details or {})
    assert details["games_failed"] == 1
    # Second game still succeeds — slate iteration continued.
    assert details["events_warmed"] == 1


def test_weather_refresh_unknown_home_team_increments_missing_coords(db_session, _share_session_local) -> None:
    """A home team without an entry in ``_MLB_PARK_COORDS`` (minor-league
    affiliate, exhibition game) must NOT call load_weather and must
    increment ``events_missing_coords``."""

    starts_at = datetime.now(timezone.utc).replace(microsecond=0)
    _make_event(db_session, starts_at=starts_at)
    job = _queued_weather_job(db_session)

    fake_schedule = _schedule_with_one_game(home_abbr="ZZZ")  # unknown
    with patch("app.clients.mlb_stats.MlbStatsClient") as MockClient, patch.object(
        refresh_jobs, "_match_mlb_event"
    ) as match_event, patch(
        "app.services.mlb_advanced.load_weather"
    ) as mock_load_weather:
        MockClient.return_value.fetch_schedule.return_value = fake_schedule
        match_event.side_effect = lambda _index, _game: db_session.query(Event).first()
        refresh_jobs._execute_claimed_job(job.id)

    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    details = dict(persisted.details or {})
    assert details["events_missing_coords"] == 1
    assert details["events_warmed"] == 0
    mock_load_weather.assert_not_called()


def test_weather_refresh_schedule_fetch_failure_records_flag(db_session, _share_session_local) -> None:
    """When the MLB Stats schedule call raises, the job records
    ``schedule_failed=True`` and exits cleanly with zero events warmed."""

    job = _queued_weather_job(db_session)

    with patch("app.clients.mlb_stats.MlbStatsClient") as MockClient:
        MockClient.return_value.fetch_schedule.side_effect = RuntimeError("upstream 500")
        refresh_jobs._execute_claimed_job(job.id)

    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    details = dict(persisted.details or {})
    assert details["schedule_failed"] is True
    assert details["events_warmed"] == 0
