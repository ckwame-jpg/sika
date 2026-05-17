"""Smarter WNBA PR 7 — ESPN WNBA injury-report loader.

Parallel of ``services/nba_injury_report.py`` for the WNBA path. The
ESPN response schema is identical across NBA and WNBA (verified in
``SMARTER_WNBA_PREP.md`` §3) so the parser
(:func:`app.services.nba_injury_report.parse_espn_injury_report`) is
shared — this module only wraps the cache-or-fetch flow around the
``WnbaInjuryReportCache`` table + WNBA-scoped tip-off TTL lookup.

The separate-table topology (over a unified ``injury_report_cache``
with a ``sport_key`` discriminator) is per D1 in
``SMARTER_WNBA_PREP.md``: smallest blast radius, separate refresh
cadences, no migration of existing NBA data. The duplication cost is
this one file; if the pattern grows to a third sport the dedup
becomes worth the refactor.

Like the NBA path, the near-tip TTL helper (Smarter #29's
:func:`_effective_injury_report_ttl_minutes`) shortens the cache lease
from 60 minutes to 15 minutes when a WNBA tip-off is inside the final
hour — so a freshly-published report isn't stuck behind a stale lease
right when operators most need it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients.espn import EspnPublicClient
from app.models import Event, WnbaInjuryReportCache, utcnow
from app.services.advanced_stats import _coerce_utc
from app.services.nba_injury_report import parse_espn_injury_report
from app.services.nba_long_tail import _effective_injury_report_ttl_minutes


logger = logging.getLogger(__name__)


_UPSTREAM_HEALTH_BUCKET = "espn_wnba_injuries"


def _nearest_upcoming_wnba_tip_off(db: Session, now: datetime) -> datetime | None:
    """Return the soonest upcoming non-terminal WNBA event's start time.

    WNBA-scoped sibling of
    :func:`app.services.nba_injury_report._nearest_upcoming_nba_tip_off`.
    Codex Pattern 9: NBA / MLB tip-offs must not engage the WNBA
    near-tip TTL — independent sport schedules, independent cache
    leases."""
    event = db.scalar(
        select(Event)
        .where(
            Event.starts_at > now,
            Event.sport_key == "WNBA",
            Event.status.notin_(("completed", "cancelled", "postponed")),
        )
        .order_by(Event.starts_at)
        .limit(1)
    )
    return _coerce_utc(event.starts_at) if event is not None else None


def load_wnba_injury_report(
    db: Session,
    *,
    client: EspnPublicClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load the WNBA injury report, returning the consumer-facing shape.

    Cache-or-fetch flow mirrors :func:`load_nba_injury_report`:

    1. Hit ``WnbaInjuryReportCache`` row for today's date (UTC). Return
       its payload when ``expires_at > now``.
    2. Else, if ``allow_network`` is False, return the stale cache
       payload (if any) or an empty shape.
    3. Else, fetch from ESPN. On success, parse + upsert into the
       cache and return the parsed payload. On failure, fall back to
       the stale cache (if any) or an empty shape.

    Empty shape: ``{"report_updated_at": None, "players": {}}`` —
    matches the NBA loader so ``emit_nba_injury_features`` (which is
    sport-agnostic) returns ``{}`` on the empty case and the
    downstream suppression gate never fires on missing data.
    """
    moment = _coerce_utc(now) or utcnow()
    fetched_date = moment.strftime("%Y-%m-%d")

    cached = db.scalar(
        select(WnbaInjuryReportCache).where(
            WnbaInjuryReportCache.fetched_date == fetched_date
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
        raw = espn.fetch_wnba_injury_report()
    except Exception as exc:  # noqa: BLE001 — surface upstream-error fallback
        logger.warning("WNBA injury report fetch failed: %s", exc)
        from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
        record_upstream_failure(
            db, _UPSTREAM_HEALTH_BUCKET, str(exc) or exc.__class__.__name__
        )
        if cached is not None:
            return dict(cached.payload or {})
        return {"report_updated_at": None, "players": {}}

    if not isinstance(raw, dict):
        logger.warning(
            "WNBA injury report fetch returned non-dict payload: %r", type(raw)
        )
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

    nearest_tip = _nearest_upcoming_wnba_tip_off(db, moment)
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
    existing: WnbaInjuryReportCache | None,
    fetched_date: str,
    payload: dict[str, Any],
    moment: datetime,
    expires_at: datetime,
) -> None:
    """Write the parsed payload to the cache, handling the rare race
    where two concurrent loaders both observed cache-miss and raced
    to INSERT (mirrors the NBA loader pattern from PR #98)."""
    try:
        with db.begin_nested():
            if existing is None:
                row = WnbaInjuryReportCache(
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
            "wnba_injury_report cache race on fetched_date=%s; retrying as update",
            fetched_date,
        )
        winner = db.scalar(
            select(WnbaInjuryReportCache).where(
                WnbaInjuryReportCache.fetched_date == fetched_date
            )
        )
        if winner is not None:
            winner.payload = payload
            winner.cached_at = moment
            winner.expires_at = expires_at
            db.flush()
