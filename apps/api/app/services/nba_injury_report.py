"""Smarter #17 phase 2 — ESPN NBA injury-report loader.

The consumer side (Smarter #17 phase 1) ships
``emit_nba_injury_features`` and the scoring suppression gate. This
module ships the PRODUCER:

- ``parse_espn_injury_report`` — flattens ESPN's nested per-team
  response into the consumer-facing
  ``{report_updated_at, players: {<name>: {status, designation}}}``
  shape.

- ``load_nba_injury_report`` — cache-or-fetch loader. Reads from
  ``NbaInjuryReportCache`` keyed by today's date; on miss / expiry
  it fetches a fresh report from ESPN, persists it, and returns the
  parsed payload. On network failure with a stale row available,
  serves the stale payload (better stale data than no data).

## TTL policy

The cache TTL is sourced from ``Smarter #29``'s
``_effective_injury_report_ttl_minutes`` helper — the default
``nba_injury_report_cache_minutes`` (60min) shortens to 15min when
the nearest upcoming NBA tip-off is inside the final hour. That
prevents a freshly-published report from sitting behind a stale
60-minute lease right when operators most need it.

## Why a separate file from ``nba_long_tail``

``nba_long_tail`` is for stats.nba.com fetches — its
``_cache_or_fetch`` helper gates on NBA Stats circuit-breaker /
daily cap. ESPN has different rate limits and different failure
modes, so the injury loader runs its own cache-or-fetch shape.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients.espn import EspnPublicClient
from app.models import Event, NbaInjuryReportCache, utcnow
from app.services.advanced_stats import _coerce_utc
from app.services.nba_long_tail import _effective_injury_report_ttl_minutes


logger = logging.getLogger(__name__)


def parse_espn_injury_report(
    raw: dict[str, Any],
    *,
    fetched_at: datetime,
) -> dict[str, Any]:
    """Flatten ESPN's per-team injury response into the consumer shape.

    Consumer (``emit_nba_injury_features``) expects:
    ::
        {
            "report_updated_at": "<ISO 8601 timestamp>",
            "players": {
                "<player full name>": {
                    "status": "<ESPN status string, e.g. 'Out'>",
                    "designation": "<short descriptor, e.g. 'Knee'>",
                },
            },
        }

    ``report_updated_at`` is set to ``fetched_at`` (when sika fetched
    the report) rather than a per-injury date. The per-injury ``date``
    field on ESPN's response is the date of the injury, not the date
    of the report — using ``fetched_at`` gives the consumer a true
    "is this report fresh" signal for the 12h stale-news gate.

    Tolerates the two ESPN response shapes seen in the wild —
    ``{"injuries": [...]}`` and ``{"teams": [...]}`` — and skips
    malformed entries silently rather than raising. Returns an
    empty ``players`` dict when no usable rows are found.
    """
    fetched_at_utc = _coerce_utc(fetched_at) or utcnow()
    players: dict[str, dict[str, str]] = {}

    team_entries: list[dict[str, Any]] = []
    if isinstance(raw.get("injuries"), list):
        team_entries = raw["injuries"]
    elif isinstance(raw.get("teams"), list):
        team_entries = raw["teams"]
    elif raw:
        # ESPN sometimes renames the top-level wrapper after a schema
        # bump. Empty team_entries → empty players → consumer thinks
        # nobody is injured → no suppression fires. Surface this as
        # a warning rather than silently emitting an empty report
        # so operators notice the schema drift.
        logger.warning(
            "ESPN injury response has neither 'injuries' nor 'teams' key; "
            "top-level keys: %s",
            sorted(raw.keys()),
        )

    for team_entry in team_entries:
        if not isinstance(team_entry, dict):
            continue
        team_injuries = team_entry.get("injuries") or []
        if not isinstance(team_injuries, list):
            continue
        for inj in team_injuries:
            if not isinstance(inj, dict):
                continue
            athlete = inj.get("athlete")
            if not isinstance(athlete, dict):
                continue
            full_name = athlete.get("fullName") or athlete.get("displayName")
            if not isinstance(full_name, str) or not full_name.strip():
                continue
            status = inj.get("status")
            if not isinstance(status, str) or not status.strip():
                continue
            # ``details.type`` is the canonical injury descriptor on the
            # current ESPN schema (e.g. ``"Knee"``); fall back to the
            # shortComment / longComment when details is missing or
            # unstructured. ``or ""`` so the consumer always sees a
            # string, never None.
            details = inj.get("details") or {}
            designation_candidates: list[Any] = []
            if isinstance(details, dict):
                designation_candidates.extend(
                    [details.get("type"), details.get("detail")]
                )
            designation_candidates.extend(
                [inj.get("shortComment"), inj.get("longComment")]
            )
            designation = next(
                (
                    candidate.strip()
                    for candidate in designation_candidates
                    if isinstance(candidate, str) and candidate.strip()
                ),
                "",
            )
            normalized_name = full_name.strip()
            if normalized_name in players:
                # ESPN occasionally lists a player twice (e.g. when a
                # player has been traded mid-day and appears on both
                # rosters). Last-wins is fine; debug-level so it's
                # observable when chasing weird suppression results.
                logger.debug(
                    "ESPN injury report duplicate athlete name: %r",
                    normalized_name,
                )
            players[normalized_name] = {
                "status": status.strip(),
                "designation": designation,
            }

    return {
        "report_updated_at": fetched_at_utc.isoformat(),
        "players": players,
    }


def _nearest_upcoming_nba_tip_off(db: Session, now: datetime) -> datetime | None:
    """Return the soonest upcoming non-terminal NBA event's start time.

    Used by the TTL policy (Smarter #29) — when an NBA game is inside
    the final hour pre-tip, the cache TTL shortens to 15min so a
    freshly-published injury report isn't stuck behind a stale lease.
    """
    event = db.scalar(
        select(Event)
        .where(
            Event.starts_at > now,
            Event.sport_key == "NBA",
            Event.status.notin_(("completed", "cancelled", "postponed")),
        )
        .order_by(Event.starts_at)
        .limit(1)
    )
    return _coerce_utc(event.starts_at) if event is not None else None


def load_nba_injury_report(
    db: Session,
    *,
    client: EspnPublicClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load the NBA injury report, returning the consumer-facing shape.

    Cache-or-fetch flow:

    1. Hit cache row for today's date (UTC). Return its payload when
       ``expires_at > now``.
    2. Else, if ``allow_network`` is False, return the stale cache
       payload (if any) or an empty shape.
    3. Else, fetch from ESPN. On success, parse + upsert into the
       cache and return the parsed payload. On failure, fall back
       to the stale cache (if any) or an empty shape.

    Empty shape: ``{"report_updated_at": None, "players": {}}``.
    ``emit_nba_injury_features`` returns ``{}`` for an empty
    ``players`` dict, so callers can pass the result through without
    a missing-data check.
    """
    moment = _coerce_utc(now) or utcnow()
    fetched_date = moment.strftime("%Y-%m-%d")

    cached = db.scalar(
        select(NbaInjuryReportCache).where(
            NbaInjuryReportCache.fetched_date == fetched_date
        )
    )
    if cached is not None:
        expires_at = _coerce_utc(cached.expires_at) or moment
        if expires_at > moment:
            return dict(cached.payload or {})

    if not allow_network:
        if cached is not None:
            return dict(cached.payload or {})
        return {"report_updated_at": None, "players": {}}

    espn = client or EspnPublicClient()
    try:
        raw = espn.fetch_nba_injury_report()
    except Exception as exc:  # noqa: BLE001 — surface upstream-error fallback
        logger.warning("NBA injury report fetch failed: %s", exc)
        # Smarter #23 phase 2 — surface the failure on the
        # upstream-health board so operators can see which ESPN
        # endpoint is dark. ``espn_injuries`` is its own bucket
        # (distinct from ``espn_scoreboard``) — the two endpoints
        # fail independently in practice.
        from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
        record_upstream_failure(db, "espn_injuries", str(exc) or exc.__class__.__name__)
        if cached is not None:
            return dict(cached.payload or {})
        return {"report_updated_at": None, "players": {}}

    if not isinstance(raw, dict):
        logger.warning("NBA injury report fetch returned non-dict payload: %r", type(raw))
        from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
        record_upstream_failure(
            db, "espn_injuries", f"non-dict payload: {type(raw).__name__}"
        )
        if cached is not None:
            return dict(cached.payload or {})
        return {"report_updated_at": None, "players": {}}
    # Smarter #23 phase 2 — fresh fetch succeeded.
    from app.services.upstream_health import record_upstream_success  # noqa: PLC0415
    record_upstream_success(db, "espn_injuries")

    payload = parse_espn_injury_report(raw, fetched_at=moment)

    nearest_tip = _nearest_upcoming_nba_tip_off(db, moment)
    ttl_minutes = _effective_injury_report_ttl_minutes(
        now=moment, event_start=nearest_tip
    )
    expires_at = moment + timedelta(minutes=ttl_minutes)

    _upsert_cache_row(
        db,
        existing=cached,
        fetched_date=fetched_date,
        payload=payload,
        moment=moment,
        expires_at=expires_at,
    )
    return payload


def _upsert_cache_row(
    db: Session,
    *,
    existing: NbaInjuryReportCache | None,
    fetched_date: str,
    payload: dict[str, Any],
    moment: datetime,
    expires_at: datetime,
) -> None:
    """Write the parsed payload to the cache, handling the rare race
    where two concurrent loaders both observed cache-miss and raced
    to INSERT.

    Uses ``begin_nested()`` so an ``IntegrityError`` from the unique
    constraint on ``fetched_date`` rolls back ONLY the cache write,
    not any other pending work on the session. On conflict, re-query
    and update the winner's row in place.
    """
    try:
        with db.begin_nested():
            if existing is None:
                row = NbaInjuryReportCache(
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
        # Another worker inserted a row for this fetched_date between
        # our SELECT and our INSERT. Re-fetch the winning row and
        # apply our update on top of it — last-writer-wins is fine
        # here because the payloads come from the same upstream
        # source within seconds of each other.
        logger.info(
            "nba_injury_report cache race on fetched_date=%s; retrying as update",
            fetched_date,
        )
        winner = db.scalar(
            select(NbaInjuryReportCache).where(
                NbaInjuryReportCache.fetched_date == fetched_date
            )
        )
        if winner is not None:
            winner.payload = payload
            winner.cached_at = moment
            winner.expires_at = expires_at
            db.flush()
