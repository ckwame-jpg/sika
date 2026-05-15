"""Smarter #13 phase 2a — NBA referee-assignments cache + loader.

Phase 1 (PR #91) shipped the scraper ``NbaRefereeAssignmentsClient``
that parses official.nba.com's daily crew-assignment page into
structured dataclasses. This module adds the persistence layer so
multiple scoring passes don't re-scrape the same page within a
single afternoon.

## Storage

A new ``NbaRefereeAssignmentCache`` model keyed by ``fetched_date``
(UTC YYYY-MM-DD) — same shape as ``NbaInjuryReportCache``. Schema
creation is automatic via ``Base.metadata.create_all`` (sika uses
no alembic migrations).

## Payload shape

Consumers (Phase 2b/c/d, deferred) read the dict shape::

    {
        "page_date": "May 14, 2026" | None,
        "assignments": [
            {
                "matchup": "Brooklyn @ Boston",
                "away_team": "Brooklyn",
                "home_team": "Boston",
                "crew_chief": {"name": "Tony Brothers", "number": 25} | None,
                "referee": {"name": "...", "number": ...} | None,
                "umpire": {"name": "...", "number": ...} | None,
                "alternate": {"name": "...", "number": ...} | None,
            },
            ...
        ],
    }

The serialized shape matches the ``NbaRefereeAssignmentDay`` /
``NbaRefereeAssignment`` / ``NbaCrewMember`` dataclasses 1-to-1
so a future ``deserialize_assignment_day`` helper is mechanical.

## Phase 2 follow-ups (separate PRs)

- (a-2) Daily refresh-job scheduler entry — single CronTrigger
  + ``nba_referee_refresh`` job-kind handler in
  ``refresh_jobs.py`` that calls ``load_nba_referee_assignments(db,
  allow_network=True)``.
- (b) Per-referee tendency stats source — total-points-per-game,
  fouls, free-throw rate aggregated from basketball-reference or
  computed from boxscores. Lives in a separate
  ``NbaRefereeTendencyCache``.
- (c) ``emit_nba_referee_features`` — joins assignments × tendencies
  into a feature dict for scoring.
- (d) Factor wiring on total-points / fouls / FT-rate props.

This PR (phase 2a, part 1) ships only the cache + loader.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients.nba_referee_scraper import (
    NbaRefereeAssignmentDay,
    NbaRefereeAssignmentsClient,
)
from app.config import get_settings
from app.models import NbaRefereeAssignmentCache, utcnow


logger = logging.getLogger(__name__)


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def serialize_assignment_day(day: NbaRefereeAssignmentDay) -> dict[str, Any]:
    """Convert an ``NbaRefereeAssignmentDay`` dataclass into the
    JSON-storable dict shape used by consumers.

    ``dataclasses.asdict`` would work for the top-level shape, but
    we serialize manually to keep ``None`` crew slots explicit (asdict
    serializes them as ``None`` too, but the explicit map makes the
    contract clearer for consumers reading the cache without the
    dataclass definition available).
    """
    return {
        "page_date": day.page_date,
        "assignments": [
            {
                "matchup": assignment.matchup,
                "away_team": assignment.away_team,
                "home_team": assignment.home_team,
                "crew_chief": asdict(assignment.crew_chief) if assignment.crew_chief else None,
                "referee": asdict(assignment.referee) if assignment.referee else None,
                "umpire": asdict(assignment.umpire) if assignment.umpire else None,
                "alternate": asdict(assignment.alternate) if assignment.alternate else None,
            }
            for assignment in day.assignments
        ],
    }


def _read_cache(db: Session, fetched_date: str) -> NbaRefereeAssignmentCache | None:
    return db.scalar(
        select(NbaRefereeAssignmentCache).where(
            NbaRefereeAssignmentCache.fetched_date == fetched_date
        )
    )


def _upsert_cache_row(
    db: Session,
    *,
    existing: NbaRefereeAssignmentCache | None,
    fetched_date: str,
    payload: dict[str, Any],
    moment: datetime,
    expires_at: datetime,
) -> None:
    """Write the parsed payload to the cache, handling the rare race
    where two concurrent loaders both observed cache-miss.

    Mirrors PR #98's ``NbaInjuryReportCache`` pattern: ``begin_nested``
    + ``IntegrityError`` retry-as-update. Last-writer-wins is safe
    because both payloads come from the same upstream within seconds.
    """
    try:
        with db.begin_nested():
            if existing is None:
                row = NbaRefereeAssignmentCache(
                    fetched_date=fetched_date,
                    payload=payload,
                    cached_at=moment,
                    expires_at=expires_at,
                )
                db.add(row)
            else:
                existing.payload = payload
                existing.cached_at = moment
                existing.expires_at = expires_at
            db.flush()
    except IntegrityError:
        logger.info(
            "nba_referee_assignments cache race on fetched_date=%s; retrying as update",
            fetched_date,
        )
        winner = _read_cache(db, fetched_date)
        if winner is not None:
            winner.payload = payload
            winner.cached_at = moment
            winner.expires_at = expires_at
            db.flush()


def load_nba_referee_assignments(
    db: Session,
    *,
    client: NbaRefereeAssignmentsClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
    target_date: date_cls | None = None,
) -> dict[str, Any]:
    """Load the NBA referee assignments for ``target_date`` (defaults
    to today's UTC date) with cache-or-fetch semantics.

    Returns the serialized ``NbaRefereeAssignmentDay`` shape (see
    module docstring) on success, or
    ``{"page_date": None, "assignments": []}`` on no-cache+no-network
    or unrecoverable fetch failure.

    The ``target_date`` parameter is the scrape date — the cache key
    uses the same string so an operator can backfill a specific day
    without invalidating today's row. Defaults to ``now.date()``.
    """
    moment = _coerce_utc(now) or utcnow()
    scrape_date = target_date if target_date is not None else moment.date()
    fetched_date = scrape_date.strftime("%Y-%m-%d")

    cached = _read_cache(db, fetched_date)
    if cached is not None:
        expires_at = _coerce_utc(cached.expires_at) or moment
        if expires_at > moment:
            return dict(cached.payload or {})

    if not allow_network:
        if cached is not None:
            return dict(cached.payload or {})
        return {"page_date": None, "assignments": []}

    scraper = client or NbaRefereeAssignmentsClient()
    try:
        day = scraper.fetch_assignments(target_date=scrape_date)
    except Exception as exc:  # noqa: BLE001 — surface as upstream-health failure
        logger.warning(
            "NBA referee assignments fetch failed for %s: %s", fetched_date, exc,
        )
        from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
        error_detail = str(exc) or exc.__class__.__name__
        record_upstream_failure(
            db, "nba_referee_assignments", f"{fetched_date}: {error_detail}",
        )
        if cached is not None:
            return dict(cached.payload or {})
        return {"page_date": None, "assignments": []}

    if not isinstance(day, NbaRefereeAssignmentDay):
        # Defensive: the scraper should always return a dataclass.
        logger.warning(
            "NBA referee assignments fetch returned unexpected type: %r",
            type(day).__name__,
        )
        from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
        record_upstream_failure(
            db,
            "nba_referee_assignments",
            f"unexpected payload type: {type(day).__name__}",
        )
        if cached is not None:
            return dict(cached.payload or {})
        return {"page_date": None, "assignments": []}

    payload = serialize_assignment_day(day)
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_referee_assignments_cache_minutes)
    _upsert_cache_row(
        db,
        existing=cached,
        fetched_date=fetched_date,
        payload=payload,
        moment=moment,
        expires_at=moment + ttl,
    )
    from app.services.upstream_health import record_upstream_success  # noqa: PLC0415
    record_upstream_success(db, "nba_referee_assignments")
    return payload
