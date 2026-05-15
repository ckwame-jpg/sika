"""Tests for Smarter #13 phase 2a — NBA referee-assignments cache + loader.

Covers:
- ``serialize_assignment_day`` round-trips the dataclass shape into
  the consumer-facing dict with explicit ``None`` for empty crew slots.
- ``load_nba_referee_assignments`` cache-hit / cache-miss / stale /
  fetch-raises / no-network paths.
- TTL is sourced from
  ``settings.nba_referee_assignments_cache_minutes`` (default 240).
- The cache upserts in place on the unique ``fetched_date``.
- Upstream-health board records success on fresh fetch, failure on
  exception.
- ``target_date`` lets operators backfill a specific day without
  invalidating today's row.
- Concurrent-upsert race is retried via the savepoint pattern (same
  as PR #98's NbaInjuryReportCache).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

from app.clients.nba_referee_scraper import (
    NbaCrewMember,
    NbaRefereeAssignment,
    NbaRefereeAssignmentDay,
)
from app.models import NbaRefereeAssignmentCache
from app.services.nba_referee_assignments import (
    load_nba_referee_assignments,
    serialize_assignment_day,
)
from app.services.upstream_health import get_upstream_health


_NOW = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)


def _sample_day(
    *,
    page_date: str | None = "May 14, 2026",
    matchup: str = "Brooklyn @ Boston",
    crew_chief: str = "Tony Brothers",
) -> NbaRefereeAssignmentDay:
    return NbaRefereeAssignmentDay(
        page_date=page_date,
        assignments=[
            NbaRefereeAssignment(
                matchup=matchup,
                away_team=matchup.split(" @ ")[0],
                home_team=matchup.split(" @ ")[1],
                crew_chief=NbaCrewMember(name=crew_chief, number=25),
                referee=NbaCrewMember(name="Scott Foster", number=48),
                umpire=NbaCrewMember(name="JB DeRosa", number=62),
                alternate=None,
            ),
        ],
    )


class _StubRefereeClient:
    def __init__(
        self,
        *,
        response: NbaRefereeAssignmentDay | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._response = response if response is not None else _sample_day()
        self._raise = raise_exc
        self.calls: list[date | None] = []

    def fetch_assignments(self, *, target_date: date | None = None) -> Any:
        self.calls.append(target_date)
        if self._raise is not None:
            raise self._raise
        return self._response


# -- serialize_assignment_day -----------------------------------------


def test_serialize_round_trips_full_crew() -> None:
    day = _sample_day()
    out = serialize_assignment_day(day)
    assert out["page_date"] == "May 14, 2026"
    assert len(out["assignments"]) == 1
    row = out["assignments"][0]
    assert row["matchup"] == "Brooklyn @ Boston"
    assert row["away_team"] == "Brooklyn"
    assert row["home_team"] == "Boston"
    assert row["crew_chief"] == {"name": "Tony Brothers", "number": 25}
    assert row["referee"] == {"name": "Scott Foster", "number": 48}
    assert row["umpire"] == {"name": "JB DeRosa", "number": 62}
    assert row["alternate"] is None


def test_serialize_handles_empty_assignments() -> None:
    day = NbaRefereeAssignmentDay(page_date="off-day", assignments=[])
    out = serialize_assignment_day(day)
    assert out == {"page_date": "off-day", "assignments": []}


def test_serialize_handles_none_page_date() -> None:
    day = NbaRefereeAssignmentDay(page_date=None, assignments=[])
    out = serialize_assignment_day(day)
    assert out["page_date"] is None


def test_serialize_preserves_all_none_crew_slots() -> None:
    day = NbaRefereeAssignmentDay(
        page_date="May 14, 2026",
        assignments=[
            NbaRefereeAssignment(
                matchup="A @ B",
                away_team="A",
                home_team="B",
                crew_chief=None,
                referee=None,
                umpire=None,
                alternate=None,
            ),
        ],
    )
    out = serialize_assignment_day(day)
    row = out["assignments"][0]
    assert row["crew_chief"] is None
    assert row["referee"] is None
    assert row["umpire"] is None
    assert row["alternate"] is None


# -- load_nba_referee_assignments: cache hit / miss / refetch ---------


def test_load_returns_empty_when_no_cache_and_network_disabled(db_session) -> None:
    payload = load_nba_referee_assignments(db_session, allow_network=False, now=_NOW)
    assert payload == {"page_date": None, "assignments": []}


def test_load_returns_cached_payload_when_fresh(db_session) -> None:
    sample = serialize_assignment_day(_sample_day())
    db_session.add(
        NbaRefereeAssignmentCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload=sample,
            cached_at=_NOW - timedelta(minutes=5),
            expires_at=_NOW + timedelta(hours=3),
        )
    )
    db_session.flush()

    stub = _StubRefereeClient()
    payload = load_nba_referee_assignments(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload == sample
    assert stub.calls == []  # cache hit → no fetch


def test_load_fetches_on_miss_and_persists(db_session) -> None:
    stub = _StubRefereeClient(response=_sample_day(crew_chief="Marc Davis"))
    payload = load_nba_referee_assignments(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload["assignments"][0]["crew_chief"]["name"] == "Marc Davis"
    assert len(stub.calls) == 1

    cached = db_session.scalar(
        select(NbaRefereeAssignmentCache).where(
            NbaRefereeAssignmentCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    assert cached is not None
    assert cached.payload == payload


def test_load_refetches_on_cache_expiry_and_upserts_in_place(db_session) -> None:
    db_session.add(
        NbaRefereeAssignmentCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload={"page_date": "old", "assignments": []},
            cached_at=_NOW - timedelta(hours=6),
            expires_at=_NOW - timedelta(minutes=10),  # expired
        )
    )
    db_session.flush()

    stub = _StubRefereeClient(response=_sample_day(crew_chief="Scott Twardoski"))
    payload = load_nba_referee_assignments(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload["assignments"][0]["crew_chief"]["name"] == "Scott Twardoski"

    rows = db_session.execute(select(NbaRefereeAssignmentCache)).all()
    assert len(rows) == 1  # upserted, not duplicated


def test_load_falls_back_to_stale_on_network_failure(db_session) -> None:
    stale = {"page_date": "old", "assignments": []}
    db_session.add(
        NbaRefereeAssignmentCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload=stale,
            cached_at=_NOW - timedelta(hours=6),
            expires_at=_NOW - timedelta(minutes=10),
        )
    )
    db_session.flush()

    stub = _StubRefereeClient(raise_exc=RuntimeError("HTTP 503"))
    payload = load_nba_referee_assignments(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload == stale


def test_load_returns_empty_on_network_failure_with_no_cache(db_session) -> None:
    stub = _StubRefereeClient(raise_exc=ConnectionError("dns fail"))
    payload = load_nba_referee_assignments(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload == {"page_date": None, "assignments": []}


def test_load_handles_unexpected_payload_type(db_session) -> None:
    class _BadClient:
        def fetch_assignments(self, *, target_date=None):
            return {"not": "a dataclass"}

    payload = load_nba_referee_assignments(
        db_session, client=_BadClient(), allow_network=True, now=_NOW,
    )
    assert payload == {"page_date": None, "assignments": []}


# -- target_date override --------------------------------------------


def test_load_uses_target_date_as_cache_key(db_session) -> None:
    # Backfill yesterday — should NOT touch today's row.
    yesterday = date(2026, 5, 13)
    today = _NOW.date()
    db_session.add(
        NbaRefereeAssignmentCache(
            fetched_date=today.strftime("%Y-%m-%d"),
            payload={"page_date": "today's data", "assignments": []},
            cached_at=_NOW,
            expires_at=_NOW + timedelta(hours=3),
        )
    )
    db_session.flush()

    stub = _StubRefereeClient(response=_sample_day(page_date="May 13, 2026"))
    payload = load_nba_referee_assignments(
        db_session,
        client=stub,
        allow_network=True,
        now=_NOW,
        target_date=yesterday,
    )
    assert payload["page_date"] == "May 13, 2026"

    # Both rows exist now: today (untouched) + yesterday (new).
    today_row = db_session.scalar(
        select(NbaRefereeAssignmentCache).where(
            NbaRefereeAssignmentCache.fetched_date == today.strftime("%Y-%m-%d")
        )
    )
    yesterday_row = db_session.scalar(
        select(NbaRefereeAssignmentCache).where(
            NbaRefereeAssignmentCache.fetched_date == yesterday.strftime("%Y-%m-%d")
        )
    )
    assert today_row.payload["page_date"] == "today's data"
    assert yesterday_row.payload["page_date"] == "May 13, 2026"


def test_load_passes_target_date_through_to_scraper(db_session) -> None:
    yesterday = date(2026, 5, 13)
    stub = _StubRefereeClient()
    load_nba_referee_assignments(
        db_session,
        client=stub,
        allow_network=True,
        now=_NOW,
        target_date=yesterday,
    )
    assert stub.calls == [yesterday]


# -- upstream-health wiring -------------------------------------------


def test_load_records_health_success_on_fresh_fetch(db_session) -> None:
    stub = _StubRefereeClient()
    load_nba_referee_assignments(db_session, client=stub, allow_network=True, now=_NOW)
    health = next(
        row
        for row in get_upstream_health(db_session)
        if row.source == "nba_referee_assignments"
    )
    assert health.last_success_at is not None


def test_load_records_health_failure_on_exception(db_session) -> None:
    stub = _StubRefereeClient(raise_exc=RuntimeError("HTTP 500"))
    load_nba_referee_assignments(db_session, client=stub, allow_network=True, now=_NOW)
    health = next(
        row
        for row in get_upstream_health(db_session)
        if row.source == "nba_referee_assignments"
    )
    assert health.last_failure_at is not None
    assert "HTTP 500" in (health.last_error or "")


def test_load_records_health_failure_with_class_name_when_str_empty(db_session) -> None:
    class _EmptyMessage(RuntimeError):
        def __str__(self) -> str:
            return ""

    stub = _StubRefereeClient(raise_exc=_EmptyMessage())
    load_nba_referee_assignments(db_session, client=stub, allow_network=True, now=_NOW)
    health = next(
        row
        for row in get_upstream_health(db_session)
        if row.source == "nba_referee_assignments"
    )
    assert "_EmptyMessage" in (health.last_error or "")


# -- canonical UPSTREAM_SOURCES inclusion -----------------------------


def test_nba_referee_assignments_in_canonical_sources() -> None:
    from app.services.upstream_health import UPSTREAM_SOURCES
    assert "nba_referee_assignments" in UPSTREAM_SOURCES


# -- concurrent-upsert race (PR #98 pattern) --------------------------


def test_load_retries_as_update_on_unique_constraint_race(db_session, monkeypatch) -> None:
    fetched_date = _NOW.strftime("%Y-%m-%d")
    # Monkeypatch flush to insert a "winner" row mid-flight, mimicking
    # what would happen with two real concurrent writers.
    real_flush = db_session.flush
    winner_inserted = {"done": False}

    def _flush_with_race(*args, **kwargs):
        if not winner_inserted["done"]:
            winner_inserted["done"] = True
            db_session.execute(
                NbaRefereeAssignmentCache.__table__.insert().values(
                    fetched_date=fetched_date,
                    payload={"page_date": "race-winner", "assignments": []},
                    cached_at=_NOW,
                    expires_at=_NOW + timedelta(hours=3),
                )
            )
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", _flush_with_race)

    stub = _StubRefereeClient(response=_sample_day(crew_chief="our payload"))
    payload = load_nba_referee_assignments(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload["assignments"][0]["crew_chief"]["name"] == "our payload"

    rows = db_session.execute(select(NbaRefereeAssignmentCache)).all()
    assert len(rows) == 1
