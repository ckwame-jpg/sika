"""Tests for Smarter #17 phase 2 — ESPN NBA injury-report loader.

Covers:

- ``parse_espn_injury_report`` flattens nested per-team responses,
  tolerates the two shapes ESPN serves (``injuries`` vs ``teams``
  wrapper), and skips malformed entries silently.
- ``load_nba_injury_report`` cache-hit / cache-miss / stale-fallback /
  fresh-fetch / network-failure flows.
- TTL policy: cache row ``expires_at`` shortens when an NBA tip-off
  is in the next hour (via Smarter #29's
  ``_effective_injury_report_ttl_minutes``).
- ``allow_network=False`` never opens an HTTP call and serves stale
  cache if present.
- The cache write upserts on the unique ``fetched_date``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from sqlalchemy import select

from app.clients.espn import EspnPublicClient
from app.models import Event, NbaInjuryReportCache
from app.services.nba_injury_report import (
    load_nba_injury_report,
    parse_espn_injury_report,
)


_NOW = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)


# -- parse_espn_injury_report -----------------------------------------


def test_parse_flattens_injuries_under_injuries_wrapper() -> None:
    raw = {
        "injuries": [
            {
                "team": {"displayName": "Boston Celtics"},
                "injuries": [
                    {
                        "athlete": {"fullName": "Jayson Tatum"},
                        "status": "Day-To-Day",
                        "details": {"type": "Knee"},
                    },
                ],
            },
            {
                "team": {"displayName": "Brooklyn Nets"},
                "injuries": [
                    {
                        "athlete": {"fullName": "Mikal Bridges"},
                        "status": "Out",
                        "details": {"type": "Ankle"},
                    },
                ],
            },
        ]
    }
    payload = parse_espn_injury_report(raw, fetched_at=_NOW)
    assert payload["report_updated_at"] == _NOW.isoformat()
    assert payload["players"] == {
        "Jayson Tatum": {"status": "Day-To-Day", "designation": "Knee"},
        "Mikal Bridges": {"status": "Out", "designation": "Ankle"},
    }


def test_parse_handles_teams_wrapper_alternative() -> None:
    # ESPN occasionally serves the per-team blocks under ``teams``
    # instead of ``injuries`` at the top level.
    raw = {
        "teams": [
            {
                "team": {"displayName": "Phoenix Suns"},
                "injuries": [
                    {
                        "athlete": {"displayName": "Devin Booker"},
                        "status": "Questionable",
                        "details": {"type": "Shoulder"},
                    },
                ],
            }
        ]
    }
    payload = parse_espn_injury_report(raw, fetched_at=_NOW)
    assert "Devin Booker" in payload["players"]


def test_parse_skips_entries_missing_athlete_name() -> None:
    raw = {
        "injuries": [
            {
                "injuries": [
                    {"athlete": {}, "status": "Out"},
                    {"athlete": {"fullName": "   "}, "status": "Out"},
                    {"athlete": {"fullName": "LeBron James"}, "status": "Day-To-Day"},
                ]
            }
        ]
    }
    payload = parse_espn_injury_report(raw, fetched_at=_NOW)
    assert payload["players"] == {
        "LeBron James": {"status": "Day-To-Day", "designation": ""},
    }


def test_parse_skips_entries_missing_status() -> None:
    raw = {
        "injuries": [
            {
                "injuries": [
                    {"athlete": {"fullName": "Player A"}, "status": ""},
                    {"athlete": {"fullName": "Player B"}, "status": None},
                    {"athlete": {"fullName": "Player C"}, "status": "Out"},
                ]
            }
        ]
    }
    payload = parse_espn_injury_report(raw, fetched_at=_NOW)
    assert list(payload["players"].keys()) == ["Player C"]


def test_parse_returns_empty_players_for_empty_response() -> None:
    payload = parse_espn_injury_report({}, fetched_at=_NOW)
    assert payload["players"] == {}
    assert payload["report_updated_at"] == _NOW.isoformat()


def test_parse_falls_back_to_short_comment_when_no_details_type() -> None:
    raw = {
        "injuries": [
            {
                "injuries": [
                    {
                        "athlete": {"fullName": "Player X"},
                        "status": "Out",
                        "shortComment": "left ankle sprain",
                    }
                ]
            }
        ]
    }
    payload = parse_espn_injury_report(raw, fetched_at=_NOW)
    assert payload["players"]["Player X"]["designation"] == "left ankle sprain"


def test_parse_falls_back_to_long_comment_last() -> None:
    raw = {
        "injuries": [
            {
                "injuries": [
                    {
                        "athlete": {"fullName": "Player Y"},
                        "status": "Out",
                        "longComment": "More verbose explanation",
                    }
                ]
            }
        ]
    }
    payload = parse_espn_injury_report(raw, fetched_at=_NOW)
    assert payload["players"]["Player Y"]["designation"] == "More verbose explanation"


def test_parse_tolerates_malformed_team_entry_types() -> None:
    raw = {
        "injuries": [
            "not a dict",
            None,
            42,
            {"injuries": "not a list"},
            {"injuries": [None, "string", 42]},
            {
                "injuries": [
                    {"athlete": {"fullName": "Player Z"}, "status": "Out"}
                ]
            },
        ]
    }
    payload = parse_espn_injury_report(raw, fetched_at=_NOW)
    assert list(payload["players"].keys()) == ["Player Z"]


# -- load_nba_injury_report: cache flows -------------------------------


def _make_event(db_session, *, sport_key: str = "NBA", offset: timedelta, status: str = "scheduled") -> Event:
    event = Event(
        sport_key=sport_key,
        external_id=f"injury-evt-{id(offset)}",
        name="Test Event",
        starts_at=_NOW + offset,
        status=status,
    )
    db_session.add(event)
    db_session.flush()
    return event


class _StubEspnClient:
    """Captures fetch calls and returns canned responses."""

    def __init__(self, *, response: Any = None, raise_exc: Exception | None = None) -> None:
        self._response = response or {"injuries": []}
        self._raise = raise_exc
        self.calls = 0

    def fetch_nba_injury_report(self) -> dict[str, Any]:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return self._response


def test_load_returns_empty_when_no_cache_and_network_disabled(db_session) -> None:
    payload = load_nba_injury_report(db_session, allow_network=False, now=_NOW)
    assert payload == {"report_updated_at": None, "players": {}}


def test_load_returns_cached_payload_when_fresh(db_session) -> None:
    db_session.add(
        NbaInjuryReportCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload={
                "report_updated_at": _NOW.isoformat(),
                "players": {"Tatum": {"status": "Out", "designation": "Knee"}},
            },
            cached_at=_NOW - timedelta(minutes=5),
            expires_at=_NOW + timedelta(minutes=30),
        )
    )
    db_session.flush()

    stub = _StubEspnClient()
    payload = load_nba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload["players"] == {"Tatum": {"status": "Out", "designation": "Knee"}}
    assert stub.calls == 0  # fresh cache hit → no network


def test_load_fetches_on_cache_miss_and_persists(db_session) -> None:
    stub = _StubEspnClient(
        response={
            "injuries": [
                {
                    "injuries": [
                        {
                            "athlete": {"fullName": "Devin Booker"},
                            "status": "Questionable",
                            "details": {"type": "Shoulder"},
                        }
                    ]
                }
            ]
        }
    )
    payload = load_nba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert stub.calls == 1
    assert "Devin Booker" in payload["players"]
    cached = db_session.scalar(
        select(NbaInjuryReportCache).where(
            NbaInjuryReportCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    assert cached is not None
    assert cached.fetched_date == _NOW.strftime("%Y-%m-%d")
    assert cached.cached_at.replace(tzinfo=timezone.utc) == _NOW
    # Default TTL of 60min when no upcoming game is near tip-off.
    assert cached.expires_at.replace(tzinfo=timezone.utc) == _NOW + timedelta(minutes=60)


def test_load_fetches_on_expired_cache_and_upserts_in_place(db_session) -> None:
    db_session.add(
        NbaInjuryReportCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload={
                "report_updated_at": (_NOW - timedelta(hours=4)).isoformat(),
                "players": {"Old": {"status": "Out", "designation": ""}},
            },
            cached_at=_NOW - timedelta(hours=4),
            expires_at=_NOW - timedelta(minutes=1),  # expired
        )
    )
    db_session.flush()

    stub = _StubEspnClient(
        response={
            "injuries": [
                {
                    "injuries": [
                        {
                            "athlete": {"fullName": "Fresh Player"},
                            "status": "Out",
                            "details": {"type": "Ankle"},
                        }
                    ]
                }
            ]
        }
    )
    payload = load_nba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert stub.calls == 1
    assert "Fresh Player" in payload["players"]
    assert "Old" not in payload["players"]
    # The cache row was overwritten, not duplicated.
    rows = db_session.execute(select(NbaInjuryReportCache)).all()
    assert len(rows) == 1


def test_load_falls_back_to_stale_cache_on_network_failure(db_session) -> None:
    db_session.add(
        NbaInjuryReportCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload={
                "report_updated_at": (_NOW - timedelta(hours=3)).isoformat(),
                "players": {"Stale Player": {"status": "Out", "designation": ""}},
            },
            cached_at=_NOW - timedelta(hours=3),
            expires_at=_NOW - timedelta(minutes=10),  # expired
        )
    )
    db_session.flush()

    stub = _StubEspnClient(
        raise_exc=httpx.HTTPStatusError(
            "boom", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(503),
        )
    )
    payload = load_nba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert stub.calls == 1
    # Stale payload still served.
    assert "Stale Player" in payload["players"]


def test_load_returns_empty_on_network_failure_with_no_cache(db_session) -> None:
    stub = _StubEspnClient(raise_exc=httpx.ConnectError("dns fail"))
    payload = load_nba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert stub.calls == 1
    assert payload == {"report_updated_at": None, "players": {}}


def test_load_does_not_overwrite_fresh_cache_when_network_disabled(db_session) -> None:
    db_session.add(
        NbaInjuryReportCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload={
                "report_updated_at": _NOW.isoformat(),
                "players": {"X": {"status": "Out", "designation": ""}},
            },
            cached_at=_NOW,
            expires_at=_NOW + timedelta(minutes=30),
        )
    )
    db_session.flush()

    payload = load_nba_injury_report(db_session, allow_network=False, now=_NOW)
    assert payload["players"] == {"X": {"status": "Out", "designation": ""}}


def test_load_returns_empty_when_fetch_returns_non_dict(db_session) -> None:
    stub = _StubEspnClient(response=["not", "a", "dict"])
    payload = load_nba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload == {"report_updated_at": None, "players": {}}
    # Nothing persisted because the payload was unparseable.
    rows = db_session.execute(select(NbaInjuryReportCache)).all()
    assert rows == []


# -- TTL policy integration (Smarter #29) -----------------------------


def test_load_uses_near_tip_ttl_when_nba_game_within_one_hour(db_session) -> None:
    # NBA event tips in 45 min → near-tip TTL of 15 min applies.
    _make_event(db_session, sport_key="NBA", offset=timedelta(minutes=45))

    stub = _StubEspnClient(response={"injuries": []})
    load_nba_injury_report(db_session, client=stub, allow_network=True, now=_NOW)

    cached = db_session.scalar(
        select(NbaInjuryReportCache).where(
            NbaInjuryReportCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    assert cached is not None
    assert cached.expires_at.replace(tzinfo=timezone.utc) == _NOW + timedelta(minutes=15)


def test_load_uses_default_ttl_when_nearest_tip_is_beyond_one_hour(db_session) -> None:
    # NBA event tips in 3h → default 60min TTL.
    _make_event(db_session, sport_key="NBA", offset=timedelta(hours=3))

    stub = _StubEspnClient(response={"injuries": []})
    load_nba_injury_report(db_session, client=stub, allow_network=True, now=_NOW)

    cached = db_session.scalar(
        select(NbaInjuryReportCache).where(
            NbaInjuryReportCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    assert cached.expires_at.replace(tzinfo=timezone.utc) == _NOW + timedelta(minutes=60)


def test_load_ignores_non_nba_events_for_ttl(db_session) -> None:
    # MLB game in 30 min should NOT trigger NBA near-tip TTL.
    _make_event(db_session, sport_key="MLB", offset=timedelta(minutes=30))

    stub = _StubEspnClient(response={"injuries": []})
    load_nba_injury_report(db_session, client=stub, allow_network=True, now=_NOW)

    cached = db_session.scalar(
        select(NbaInjuryReportCache).where(
            NbaInjuryReportCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    # Default 60min TTL, not 15min.
    assert cached.expires_at.replace(tzinfo=timezone.utc) == _NOW + timedelta(minutes=60)


def test_load_ignores_completed_nba_events_for_ttl(db_session) -> None:
    # Completed NBA games shouldn't engage the near-tip TTL even if
    # starts_at lies "in the future" because of clock skew or
    # stale ingestion data.
    _make_event(
        db_session, sport_key="NBA", offset=timedelta(minutes=30), status="completed"
    )

    stub = _StubEspnClient(response={"injuries": []})
    load_nba_injury_report(db_session, client=stub, allow_network=True, now=_NOW)

    cached = db_session.scalar(
        select(NbaInjuryReportCache).where(
            NbaInjuryReportCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    assert cached.expires_at.replace(tzinfo=timezone.utc) == _NOW + timedelta(minutes=60)


# -- EspnPublicClient.fetch_nba_injury_report -------------------------


class _StubHttpClient:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self.payload = payload or {"injuries": []}
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, "kwargs": kwargs})
        return httpx.Response(
            status_code=self.status_code,
            json=self.payload,
            request=httpx.Request("GET", url),
        )


def test_espn_client_fetch_nba_injury_report_calls_correct_url() -> None:
    stub = _StubHttpClient(payload={"injuries": [{"team": {}, "injuries": []}]})
    client = EspnPublicClient(http_client=stub)
    payload = client.fetch_nba_injury_report()
    assert len(stub.calls) == 1
    assert stub.calls[0]["url"].endswith("/basketball/nba/injuries")
    assert payload == {"injuries": [{"team": {}, "injuries": []}]}


def test_espn_client_fetch_nba_injury_report_raises_on_http_error() -> None:
    stub = _StubHttpClient(status_code=503)
    client = EspnPublicClient(http_client=stub)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_nba_injury_report()


# -- Reviewer-requested follow-ups ------------------------------------


def test_load_serves_stale_cache_when_expired_and_network_disabled(db_session) -> None:
    # Reviewer gap: this branch (expired cache + allow_network=False) was
    # previously untested. The loader should still serve the stale payload
    # rather than returning empty — operators with a stale row in the
    # DB are better off seeing yesterday's injuries than nothing.
    db_session.add(
        NbaInjuryReportCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload={
                "report_updated_at": (_NOW - timedelta(hours=6)).isoformat(),
                "players": {"Stale Player": {"status": "Out", "designation": ""}},
            },
            cached_at=_NOW - timedelta(hours=6),
            expires_at=_NOW - timedelta(minutes=10),  # expired
        )
    )
    db_session.flush()

    payload = load_nba_injury_report(db_session, allow_network=False, now=_NOW)
    assert "Stale Player" in payload["players"]


def test_parse_warns_on_unrecognized_top_level_key(caplog) -> None:
    # ESPN schema bump: a future API version drops both ``injuries``
    # and ``teams``. The loader must surface this rather than silently
    # emitting an empty report (which would suppress nobody, hiding
    # the upstream drift).
    raw = {"data": [{"some_key": "..."}], "meta": {"version": "v3"}}
    with caplog.at_level("WARNING"):
        payload = parse_espn_injury_report(raw, fetched_at=_NOW)
    assert payload["players"] == {}
    assert any(
        "neither 'injuries' nor 'teams'" in rec.message for rec in caplog.records
    )


def test_parse_does_not_warn_on_empty_dict(caplog) -> None:
    # An empty dict is a valid "no payload yet" signal — don't spam
    # warnings on transient empties.
    with caplog.at_level("WARNING"):
        parse_espn_injury_report({}, fetched_at=_NOW)
    assert not any(
        "neither 'injuries' nor 'teams'" in rec.message for rec in caplog.records
    )


def test_parse_keeps_last_value_on_duplicate_player_name() -> None:
    # ESPN occasionally lists a traded player on both old + new teams.
    # Last-wins is intentional; the test pins that behavior.
    raw = {
        "injuries": [
            {
                "injuries": [
                    {
                        "athlete": {"fullName": "Traded Player"},
                        "status": "Day-To-Day",
                        "details": {"type": "Old Team Designation"},
                    }
                ]
            },
            {
                "injuries": [
                    {
                        "athlete": {"fullName": "Traded Player"},
                        "status": "Out",
                        "details": {"type": "New Team Designation"},
                    }
                ]
            },
        ]
    }
    payload = parse_espn_injury_report(raw, fetched_at=_NOW)
    assert payload["players"]["Traded Player"] == {
        "status": "Out",
        "designation": "New Team Designation",
    }


def test_load_retries_as_update_on_unique_constraint_race(db_session, monkeypatch) -> None:
    # Concurrency simulation: while our loader is between SELECT and
    # INSERT, another worker inserts a row with the same fetched_date.
    # The loader should catch the IntegrityError, re-query the winner's
    # row, and update it in place rather than crashing the request.
    fetched_date = _NOW.strftime("%Y-%m-%d")
    # No cache row exists at SELECT time, so the loader will go down
    # the INSERT branch. We monkey-patch ``db.flush`` to insert the
    # "winner" row first and then surface the IntegrityError to our
    # loader, mimicking what would happen with two real concurrent
    # writers.
    real_flush = db_session.flush
    winner_inserted = {"done": False}

    def _flush_with_race(*args, **kwargs):
        # Insert the "winner" row exactly once, on the first flush
        # after the loader adds its candidate. Direct INSERT through
        # the engine to bypass the session's identity map.
        if not winner_inserted["done"]:
            winner_inserted["done"] = True
            db_session.execute(
                NbaInjuryReportCache.__table__.insert().values(
                    fetched_date=fetched_date,
                    payload={"report_updated_at": _NOW.isoformat(), "players": {}},
                    cached_at=_NOW,
                    expires_at=_NOW + timedelta(minutes=60),
                )
            )
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", _flush_with_race)

    stub = _StubEspnClient(
        response={
            "injuries": [
                {
                    "injuries": [
                        {
                            "athlete": {"fullName": "Race Player"},
                            "status": "Out",
                            "details": {"type": "Knee"},
                        }
                    ]
                }
            ]
        }
    )
    # Should NOT raise — the loader catches IntegrityError and retries
    # as an update.
    payload = load_nba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    # The retry-as-update overwrites the winner's empty players with
    # our parsed payload.
    assert "Race Player" in payload["players"]
    rows = db_session.execute(select(NbaInjuryReportCache)).all()
    assert len(rows) == 1
