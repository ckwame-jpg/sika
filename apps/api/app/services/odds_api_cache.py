"""Smarter #18 phase 2 part (a) — cache layer for The Odds API.

The Odds API has a 500-request monthly cap on the free tier. The
H2H quote list for a single sport is needed many times during a
scoring pass (once per active event), so an uncached client call
would burn through the cap inside an hour. This module ships a
per-sport cache with a configurable TTL.

## Storage

State lives in the existing ``OperatorSetting`` JSON blob keyed by
``odds_api_h2h_<sport_key>``. No migration required — the schema
shape is the same as the upstream-health board, Smarter #28's
quality-tier overrides, and Smarter #31's narrator toggle.

## Phase 2 follow-ups (separate PRs)

- (b) **Event matching layer** — fuzzy match Odds-API events
  (``home_team`` + ``away_team`` strings) to sika ``Event`` rows.
  Team-name normalization across the two providers is the hard part
  (ESPN says "LA Lakers", Odds API says "Los Angeles Lakers"; ESPN
  says "St. Louis Cardinals", Odds API says "Saint Louis Cardinals").
- (c) **Scoring diagnostic** — emit ``sportsbook_consensus_prob`` and
  ``sportsbook_book_count`` features so scoring + the operator surface
  can compare sika's prediction against the book consensus.
- (d) **Suppression rule** — when sika's prediction disagrees with
  the consensus by more than a threshold (e.g. 15 percentage points),
  suppress the recommendation as ``model_book_disagreement``.

This PR (part a) ships the cache only; consumer wiring is deferred.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.clients.the_odds_api import (
    MissingApiKeyError,
    TheOddsApiClient,
    odds_api_sport_key,
)
from app.config import get_settings
from app.models import OperatorSetting, utcnow


logger = logging.getLogger(__name__)


_CACHE_KEY_PREFIX = "odds_api_h2h_"


# Hard ceiling on stale-payload freshness. When the cache row's
# ``expires_at`` is more than ``_MAX_STALE_TTL_MULTIPLIER * ttl``
# ago, the loader returns ``[]`` instead of the stale payload. This
# prevents the consumer (part b/c/d, deferred) from acting on
# day-old odds during an extended outage. 2x TTL = 1h at the default
# 30min TTL; still useful for a brief Odds-API blip, but won't serve
# overnight-old data.
_MAX_STALE_TTL_MULTIPLIER: int = 2


def _cache_key(sika_sport_key: str) -> str:
    """OperatorSetting key for a sport's cached H2H quotes.

    Note: ``odds_api_sport_key`` in ``the_odds_api.py`` also uppercases
    its input on the membership check. Both call sites independently
    normalize a mixed-case input — harmless redundancy, not a bug, but
    worth knowing when reading the dispatch flow.
    """
    return f"{_CACHE_KEY_PREFIX}{sika_sport_key.upper()}"


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_dt(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _coerce_utc(value)


def _read_cache(db: Session, sika_sport_key: str) -> tuple[list[dict[str, Any]] | None, datetime | None]:
    """Return ``(events, expires_at)`` from cache, or ``(None, None)``
    when no row exists for this sport.
    """
    row = (
        db.query(OperatorSetting)
        .filter(OperatorSetting.key == _cache_key(sika_sport_key))
        .one_or_none()
    )
    if row is None:
        return None, None
    payload = dict(row.value or {})
    events = payload.get("events")
    expires_at = _parse_dt(payload.get("expires_at"))
    if not isinstance(events, list):
        return None, expires_at
    return events, expires_at


def _write_cache(
    db: Session,
    sika_sport_key: str,
    events: list[dict[str, Any]],
    *,
    fetched_at: datetime,
    expires_at: datetime,
) -> None:
    """Upsert the per-sport cache row in ``OperatorSetting``.

    Concurrency note: this uses the same read-then-write pattern as
    the rest of the codebase's ``OperatorSetting`` writers
    (``advanced_stats._operator_set``,
    ``upstream_health._operator_set``,
    ``operator_settings.set_ml_serving_mode``). Two concurrent loaders
    racing on a cold cache for the same sport could in principle hit
    an ``IntegrityError`` on the unique ``key`` constraint. In the
    current single-worker deployment with 30-min-TTL cache and
    scheduler-gated refresh-jobs, the race window is effectively
    nil — multiple scoring passes within a single process share the
    same session, so the second pass sees the first's pending row.
    The retry-as-update pattern from PR #98 (``NbaInjuryReportCache``)
    is the right fix to align if we ever go multi-worker; deferred
    here to keep the OperatorSetting-write shape consistent with the
    rest of the codebase.
    """
    key = _cache_key(sika_sport_key)
    payload = {
        "events": events,
        "fetched_at": fetched_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "event_count": len(events),
    }
    row = (
        db.query(OperatorSetting)
        .filter(OperatorSetting.key == key)
        .one_or_none()
    )
    if row is None:
        row = OperatorSetting(key=key, value=payload)
        db.add(row)
    else:
        row.value = payload
    db.flush()


def _stale_or_empty(
    cached_events: list[dict[str, Any]] | None,
    cached_expires_at: datetime | None,
    now: datetime,
    *,
    ttl: timedelta,
) -> list[dict[str, Any]]:
    """Reviewer MEDIUM catch: returning unbounded stale data was
    unsafe — a day-old cache could mislead the part-(d) suppression
    rule into firing on stale odds. Cap stale-serving at
    ``_MAX_STALE_TTL_MULTIPLIER * ttl``.

    Returns ``cached_events`` when:
      - we have something cached AND
      - the row's ``expires_at`` is within the hard ceiling.

    Returns ``[]`` otherwise — the consumer's signal to "skip the
    sportsbook prior for this pass."
    """
    if cached_events is None:
        return []
    if cached_expires_at is None:
        # Defensive: a cache row without an expires_at is malformed.
        # Serve the events but log because this shouldn't happen.
        logger.warning(
            "Odds API cache row has no expires_at; serving events anyway"
        )
        return cached_events
    max_stale_at = cached_expires_at + (_MAX_STALE_TTL_MULTIPLIER * ttl)
    if now > max_stale_at:
        return []
    return cached_events


def cached_h2h_odds(
    db: Session,
    sika_sport_key: str,
    *,
    client: TheOddsApiClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
    ttl_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Return cached H2H quotes for ``sika_sport_key``, fetching when
    stale or absent.

    Returns ``[]`` in any of these cases (caller treats as
    "skip sportsbook prior for this pass"):

    - The sport isn't mapped to an Odds API slug.
    - The API key is unset (``MissingApiKeyError``).
    - No cache row AND network is disabled / fetch failed.

    On a cache hit, the cached payload is returned without a network
    call — the typical hot path during a scoring pass.

    On a successful fetch, the upstream-health board is updated
    (``the_odds_api`` source). Failures record the error so operators
    can see why the prior is missing.
    """
    if odds_api_sport_key(sika_sport_key) is None:
        return []

    moment = _coerce_utc(now) or utcnow()
    settings = get_settings()
    ttl = timedelta(
        minutes=ttl_minutes
        if ttl_minutes is not None
        else settings.the_odds_api_cache_ttl_minutes
    )

    cached_events, cached_expires_at = _read_cache(db, sika_sport_key)
    if (
        cached_events is not None
        and cached_expires_at is not None
        and cached_expires_at > moment
    ):
        return cached_events

    if not allow_network:
        # Stale or missing cache + no network → serve stale (subject
        # to the hard ceiling) so a brief network-disabled window
        # doesn't blank out the prior. The ceiling prevents serving
        # day-old odds during an extended outage.
        return _stale_or_empty(cached_events, cached_expires_at, moment, ttl=ttl)

    odds_client = client or TheOddsApiClient()
    try:
        events = odds_client.fetch_h2h_odds(sika_sport_key)
    except MissingApiKeyError:
        # Empty key — caller skips the prior entirely. Don't record
        # this as a failure on the upstream-health board; it's a
        # configuration choice, not a fault. Logged at WARNING so
        # operators see the silent-degradation reason in logs
        # (the health board would say "last success: <whenever>"
        # which is misleading after a key revocation).
        logger.warning(
            "The Odds API key not configured; skipping fetch for %s "
            "(upstream-health board will show stale ``last_success_at`` "
            "rather than a failure entry)",
            sika_sport_key,
        )
        return _stale_or_empty(cached_events, cached_expires_at, moment, ttl=ttl)
    except Exception as exc:  # noqa: BLE001 — propagate as health failure
        logger.warning(
            "The Odds API fetch failed for %s: %s", sika_sport_key, exc,
        )
        # Reviewer HIGH catch: the original ``f"{sport_key}: {exc}" or exc.__class__.__name__``
        # was always-truthy (prefix kept the whole f-string non-empty), so
        # the fallback never fired. Move the fallback inside the f-string
        # so an exc with empty ``str(exc)`` still gets a meaningful class
        # name on the operator surface.
        error_detail = str(exc) or exc.__class__.__name__
        from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
        record_upstream_failure(
            db, "the_odds_api", f"{sika_sport_key}: {error_detail}",
        )
        return _stale_or_empty(cached_events, cached_expires_at, moment, ttl=ttl)

    if not isinstance(events, list):
        logger.warning(
            "The Odds API returned non-list payload for %s: %r",
            sika_sport_key,
            type(events).__name__,
        )
        return _stale_or_empty(cached_events, cached_expires_at, moment, ttl=ttl)

    _write_cache(
        db,
        sika_sport_key,
        events,
        fetched_at=moment,
        expires_at=moment + ttl,
    )
    from app.services.upstream_health import record_upstream_success  # noqa: PLC0415
    record_upstream_success(db, "the_odds_api")
    return events


def invalidate_cached_h2h_odds(db: Session, sika_sport_key: str) -> None:
    """Operator-side knob: drop a sport's cache row so the next
    ``cached_h2h_odds`` call forces a fresh fetch (subject to the
    monthly-cap budget). Useful for testing the fetch path without
    waiting for the TTL.
    """
    row = (
        db.query(OperatorSetting)
        .filter(OperatorSetting.key == _cache_key(sika_sport_key))
        .one_or_none()
    )
    if row is not None:
        db.delete(row)
        db.flush()
