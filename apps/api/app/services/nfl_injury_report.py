"""Smarter NFL PR 6 — ESPN NFL injury-report loader.

Parallel of ``services/wnba_injury_report.py`` for the NFL path (the
third sport on the pattern — the D1 duplication-cost note on the WNBA
module said a third sport makes the dedup worth considering; deferred
to keep this PR shaped like its siblings).

Role in the NFL stack: the OFFICIAL weekly club report (nflverse, via
``NflOfficialInjuryCache``) is the structured ground truth but only
refreshes nightly. This ESPN feed is the fresher INTRADAY supplement —
it moves when beat reporters do, which is exactly the Sunday-morning
inactives window the official file misses. The QB-status gate
(``nfl_qb_status`` feature group) reads both.

Near-kick TTL: the shared ``_effective_injury_report_ttl_minutes``
helper tightens the lease when an NFL kickoff is inside the final
hour, same as NBA/WNBA near tip-off.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients.espn import EspnPublicClient
from app.models import Event, NflInjuryReportCache, utcnow
from app.services.advanced_stats import _coerce_utc
from app.services.nba_injury_report import parse_espn_injury_report
from app.services.nba_long_tail import _effective_injury_report_ttl_minutes


logger = logging.getLogger(__name__)


_UPSTREAM_HEALTH_BUCKET = "espn_nfl_injuries"


def _nearest_upcoming_nfl_kickoff(db: Session, now: datetime) -> datetime | None:
    """Soonest upcoming non-terminal NFL event's start time. NFL-scoped
    (codex Pattern 9 — other sports' tip-offs must not tighten the NFL
    cache lease)."""
    event = db.scalar(
        select(Event)
        .where(
            Event.starts_at > now,
            Event.sport_key == "NFL",
            Event.status.notin_(("completed", "cancelled", "postponed")),
        )
        .order_by(Event.starts_at)
        .limit(1)
    )
    return _coerce_utc(event.starts_at) if event is not None else None


def load_nfl_injury_report(
    db: Session,
    *,
    client: EspnPublicClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cache-or-fetch the ESPN NFL injury feed; consumer shape matches
    the NBA/WNBA loaders (``{"report_updated_at", "players"}``) so the
    shared feature emitters stay sport-agnostic."""
    moment = _coerce_utc(now) or utcnow()
    fetched_date = moment.strftime("%Y-%m-%d")

    cached = db.scalar(
        select(NflInjuryReportCache).where(
            NflInjuryReportCache.fetched_date == fetched_date
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
        raw = espn.fetch_nfl_injury_report()
    except Exception as exc:  # noqa: BLE001 — surface upstream-error fallback
        logger.warning("NFL injury report fetch failed: %s", exc)
        from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
        record_upstream_failure(
            db, _UPSTREAM_HEALTH_BUCKET, str(exc) or exc.__class__.__name__
        )
        if cached is not None:
            return dict(cached.payload or {})
        return {"report_updated_at": None, "players": {}}

    if not isinstance(raw, dict):
        logger.warning("NFL injury report fetch returned non-dict payload: %r", type(raw))
        from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
        record_upstream_failure(
            db, _UPSTREAM_HEALTH_BUCKET, f"non-dict payload: {type(raw).__name__}"
        )
        if cached is not None:
            return dict(cached.payload or {})
        return {"report_updated_at": None, "players": {}}
    from app.services.upstream_health import record_upstream_success  # noqa: PLC0415
    record_upstream_success(db, _UPSTREAM_HEALTH_BUCKET)

    payload = parse_espn_injury_report(raw, fetched_at=moment)

    nearest_kick = _nearest_upcoming_nfl_kickoff(db, moment)
    ttl_minutes = _effective_injury_report_ttl_minutes(
        now=moment, event_start=nearest_kick
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
    existing: NflInjuryReportCache | None,
    fetched_date: str,
    payload: dict[str, Any],
    moment: datetime,
    expires_at: datetime,
) -> None:
    """Savepoint + IntegrityError-retry upsert (PR #98 race pattern)."""
    try:
        with db.begin_nested():
            if existing is None:
                db.add(NflInjuryReportCache(
                    fetched_date=fetched_date,
                    payload=payload,
                    cached_at=moment,
                    expires_at=expires_at,
                ))
            else:
                existing.payload = payload
                existing.cached_at = moment
                existing.expires_at = expires_at
            db.flush()
    except IntegrityError:
        logger.info(
            "nfl_injury_report cache race on fetched_date=%s; retrying as update",
            fetched_date,
        )
        winner = db.scalar(
            select(NflInjuryReportCache).where(
                NflInjuryReportCache.fetched_date == fetched_date
            )
        )
        if winner is not None:
            winner.payload = payload
            winner.cached_at = moment
            winner.expires_at = expires_at
            db.flush()
