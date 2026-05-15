"""Smarter #13 phase 2b — NBA referee tendency cache + loader.

Phase 2a (PR #101 + #103) ships the daily ASSIGNMENTS cache (which
crew works which game). This phase ships the per-referee TENDENCY
cache: how many fouls a given crew chief tends to call, their FT-rate
impact. Phase 2c (deferred) joins the two via a feature emitter;
phase 2d (deferred) wires the heuristic factor on points / fouls /
FT props.

## Storage

The cache lives in a single ``OperatorSetting`` row per season,
keyed ``nba_referee_tendencies_<season>``. Mirrors PR #100's Odds
API cache shape exactly — no migration required, the JSON blob
stores the consumer-facing payload directly. Per-season isolation
keeps a 2025 read from accidentally serving 2026's data (cache key
includes the season).

## Fetcher contract

The actual fetch from basketball-reference.com is deferred to phase
2b-2 (small follow-up PR). This module accepts a ``fetcher: Callable[[int], list[dict]]``
so:

- Tests inject a deterministic stub.
- Production wires ``BasketballReferenceClient.fetch_referee_season_stats``
  once the BR URL + table layout have been validated against a manual
  fetch (basketball-reference returns 403 to anonymous WebFetch from a
  fresh IP, so the URL+table layout decoding requires the operator's
  configured base_url path that the existing client already uses for
  player / team gamelogs).

The fetcher returns BR-shaped raw rows (a list of dicts with column
names like ``"Referee"``, ``"G"``, ``"PF/G"``, ``"FT/G"``, ``"T"``);
``parse_referee_tendency_rows`` translates them into the
consumer-facing payload.

## Stale-fallback ceiling

Same pattern as PR #100: serve stale past expiry up to ``2 * ttl``,
then fall back to empty. A multi-day BR outage doesn't cause sika to
serve week-old tendency data forever; refs change behavior over
the season and stale data is worse than no data past that ceiling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OperatorSetting, utcnow


logger = logging.getLogger(__name__)


# Fetcher protocol used by the loader. Production wires the BR
# scraper here in phase 2b-2; tests inject deterministic stubs.
NbaRefereeTendencyFetcher = Callable[[int], list[dict[str, Any]]]


# 24h TTL — referee tendency stats change slowly within a season; a
# daily refresh catches the slow drift without hammering BR. The
# stale-fallback ceiling at 2x means even a 48h outage still serves
# yesterday's data; past that we fall back to empty rather than
# zombie-fresh week-old tendencies (matches the PR #100 ceiling).
DEFAULT_TENDENCY_CACHE_MINUTES: int = 1440

_CACHE_KEY_PREFIX = "nba_referee_tendencies_"

# Schema version for the OperatorSetting JSON blob shape. Subagent
# review P1: a future PR that adds / renames a top-level field in
# the blob would silently break the read path because rows written
# under the old schema would be opaque to the new loader. Bumping
# this version when the shape changes lets the loader treat
# legacy-shape rows as cache misses (forcing a fresh fetch) instead
# of returning empty payloads with no warning.
_CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class NbaRefereeTendency:
    """Per-referee tendency stats for one season.

    Field semantics:
    - ``games_officiated``: count of games this ref worked the season.
      Drops to None / 0 are dropped at parse time (the row is
      informational at best).
    - ``fouls_per_game``: total personal fouls called per game (both
      teams combined). The league average is ~42; a ref consistently
      above 44 is "tight," below 40 is "loose." Phase 2d will wire
      this into the total-points + FT-rate factor.
    - ``fta_per_game``: free-throw attempts per game (both teams).
      Tracks fouls_per_game closely but adds shooting-foul split.
    - ``technicals``: season total. Less load-bearing for scoring
      but operator-facing for referee diagnostics.
    """
    name: str
    games_officiated: int
    fouls_per_game: float | None
    fta_per_game: float | None
    technicals: int | None


def tendency_cache_key(season: int) -> str:
    """OperatorSetting key for a season's cached referee tendencies."""
    return f"{_CACHE_KEY_PREFIX}{int(season)}"


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_int(value: Any) -> int | None:
    """Coerce to int. Returns None for ``None``, empty string, BR's
    missing-value sentinels (``"--"``, ``"n/a"``), or any value that
    parses to NaN/inf (defensive against malformed BR rows that
    serialize ``"nan"``/``"inf"`` literally)."""
    parsed = _safe_float(value)
    if parsed is None:
        return None
    return int(parsed)


def _safe_float(value: Any) -> float | None:
    """Coerce to float, filtering NaN/inf in BOTH the type-direct
    path (``float("nan")``, ``float("inf")``) AND the text-parse
    path (``"nan"``, ``"inf"``). Subagent review P2: applying
    ``math.isfinite`` only to the text branch let raw ``inf``
    floats slip through; phase 2d's heuristic factor multiplying
    by ``inf`` would silently produce ``inf``/``nan`` output."""
    import math

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        as_float = float(value)
    else:
        text = str(value).strip()
        if not text or text in {"--", "n/a", "N/A", "-"}:
            return None
        try:
            as_float = float(text)
        except (TypeError, ValueError):
            return None
    if not math.isfinite(as_float):
        return None
    return as_float


def parse_referee_tendency_rows(
    raw_rows: list[dict[str, Any]],
    *,
    season: int,
    fetched_at: datetime,
) -> dict[str, Any]:
    """Translate BR-shaped raw rows into the consumer-facing payload.

    Expected raw column names (BR's ``Officials Per Game`` table):
    - ``"Referee"`` — display name
    - ``"G"`` — games officiated
    - ``"PF/G"`` — personal fouls per game
    - ``"FT/G"`` — free-throw attempts per game (both teams)
    - ``"T"`` — total technicals (season)

    Defensive: rows missing a referee name are skipped; rows with
    ``games_officiated == 0`` (didn't officiate this season) are
    dropped to avoid divide-by-zero ratios in the consumer code;
    individual ratio fields that fail to parse become None rather
    than failing the whole row.

    Returns the payload that gets stored in the cache row's JSON:

    ::

        {
            "season": 2026,
            "fetched_at": "2026-05-15T12:00:00+00:00",
            "referees": {
                "Scott Foster": {
                    "name": "Scott Foster",
                    "games_officiated": 60,
                    "fouls_per_game": 42.1,
                    "fta_per_game": 44.5,
                    "technicals": 12,
                },
                ...
            },
        }
    """
    fetched_iso = (_coerce_utc(fetched_at) or fetched_at).isoformat()
    referees: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        # Subagent review P1: defensive against mixed-type lists.
        # A scraper that emits a header row, a totals row, or a
        # stray ``None`` from a table parse error would otherwise
        # crash ``row.get(...)`` with AttributeError and propagate
        # uncaught (the loader's try/except wraps the fetcher call,
        # not the parse).
        if not isinstance(row, dict):
            continue
        name = str(row.get("Referee") or "").strip()
        if not name:
            continue
        games = _safe_int(row.get("G"))
        if games is None or games <= 0:
            continue
        referees[name] = {
            "name": name,
            "games_officiated": games,
            "fouls_per_game": _safe_float(row.get("PF/G")),
            "fta_per_game": _safe_float(row.get("FT/G")),
            "technicals": _safe_int(row.get("T")),
        }
    return {
        "season": int(season),
        "fetched_at": fetched_iso,
        "referees": referees,
    }


def _empty_payload(season: int, *, fetched_at: datetime | None = None) -> dict[str, Any]:
    """Consumer-safe shape with no referees. ``fetched_at`` defaults
    to ``utcnow()`` so even the empty path carries a timestamp the
    operator can look at to see when the loader gave up."""
    moment = _coerce_utc(fetched_at) or utcnow()
    return {
        "season": int(season),
        "fetched_at": moment.isoformat(),
        "referees": {},
    }


def _read_cache_row(db: Session, season: int) -> OperatorSetting | None:
    return db.scalar(
        select(OperatorSetting).where(
            OperatorSetting.key == tendency_cache_key(season)
        )
    )


def _write_cache_row(
    db: Session,
    *,
    season: int,
    payload: dict[str, Any],
    moment: datetime,
    expires_at: datetime,
) -> None:
    """Upsert the per-season cache row.

    Mirrors PR #100's Odds API cache writer — store the payload AND
    the explicit ``cached_at`` / ``expires_at`` fields inside the
    JSON blob since OperatorSetting only has ``value`` (no schema
    columns). The loader reads them back via the same JSON keys.
    The ``schema_version`` field gates legacy-shape rejection at
    read time (subagent review P1).
    """
    blob = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "payload": payload,
        "cached_at": moment.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    key = tendency_cache_key(season)
    existing = _read_cache_row(db, season)
    if existing is None:
        db.add(OperatorSetting(key=key, value=blob))
    else:
        existing.value = blob
    db.flush()


def invalidate_nba_referee_tendencies(db: Session, *, season: int) -> None:
    """Operator knob — drop the cache row entirely so the next
    ``load_nba_referee_tendencies`` call with ``allow_network=True``
    forces a fresh fetch. Idempotent on missing cache."""
    existing = _read_cache_row(db, season)
    if existing is not None:
        db.delete(existing)
        db.flush()


def load_nba_referee_tendencies(
    db: Session,
    *,
    season: int,
    fetcher: NbaRefereeTendencyFetcher,
    allow_network: bool = False,
    now: datetime | None = None,
    ttl_minutes: int = DEFAULT_TENDENCY_CACHE_MINUTES,
) -> dict[str, Any]:
    """Cache-or-fetch loader for per-season referee tendencies.

    Decision tree (matches PR #100's ``cached_h2h_odds`` shape):

    1. Cache hit, fresh (``now < expires_at``) → return cached payload.
    2. Cache hit, stale within ``2 * ttl`` past expiry, ``allow_network=False``
       → return cached payload (degrade gracefully on outage).
    3. Cache miss / past stale ceiling, ``allow_network=False``
       → return empty payload.
    4. ``allow_network=True``, fetcher succeeds → parse + cache + return.
    5. ``allow_network=True``, fetcher raises → log + return cached
       payload (if any) or empty.

    The loader doesn't know about basketball-reference, NBA Stats, or
    any specific upstream — the ``fetcher`` callable is the only
    coupling. Production wires it to the BR scraper in phase 2b-2.
    """
    moment = _coerce_utc(now) or utcnow()
    cached = _read_cache_row(db, season)
    cached_payload: dict[str, Any] | None = None
    cached_expires_at: datetime | None = None
    if cached is not None and isinstance(cached.value, dict):
        # Subagent review P1: schema_version gate. A row written under
        # an older blob shape is treated as a cache miss rather than
        # silently misinterpreted by the new loader. Forces a fresh
        # fetch when network is allowed; falls through to empty
        # otherwise (which is safer than serving a payload whose
        # field semantics may have shifted between writes and reads).
        cached_schema = cached.value.get("schema_version")
        if cached_schema == _CACHE_SCHEMA_VERSION:
            cached_payload = cached.value.get("payload")
            cached_expires_at = _coerce_utc(_parse_iso(cached.value.get("expires_at")))
            if isinstance(cached_payload, dict):
                if cached_expires_at is None:
                    # Subagent review P1: payload present but no
                    # parseable expires_at means a partial / hand-
                    # edited row. Don't silently serve it as fresh
                    # OR silently drop it — log + treat as miss so
                    # operators can debug.
                    logger.warning(
                        "nba_referee_tendencies cache row for season %d has payload "
                        "but missing/unparseable expires_at; treating as cache miss",
                        season,
                    )
                    cached_payload = None
                elif cached_expires_at > moment:
                    return dict(cached_payload)
            else:
                cached_payload = None
        elif cached_schema is not None:
            logger.warning(
                "nba_referee_tendencies cache row for season %d has schema_version=%r "
                "(expected %d); treating as cache miss to force fresh fetch",
                season, cached_schema, _CACHE_SCHEMA_VERSION,
            )

    # Past expiry. ``allow_network=False``: serve stale within the
    # ceiling, otherwise empty.
    if not allow_network:
        if cached_payload is not None and cached_expires_at is not None:
            stale_ceiling = cached_expires_at + timedelta(minutes=ttl_minutes)
            if moment <= stale_ceiling:
                return dict(cached_payload)
        return _empty_payload(season, fetched_at=moment)

    # Network allowed — call the fetcher.
    try:
        raw_rows = fetcher(int(season))
    except Exception as exc:  # noqa: BLE001 — surface as graceful fallback
        logger.warning(
            "nba_referee_tendencies fetch failed for season %d: %s", season, exc,
        )
        if cached_payload is not None:
            return dict(cached_payload)
        return _empty_payload(season, fetched_at=moment)

    if not isinstance(raw_rows, list):
        logger.warning(
            "nba_referee_tendencies fetcher returned non-list for season %d: %r",
            season, type(raw_rows).__name__,
        )
        if cached_payload is not None:
            return dict(cached_payload)
        return _empty_payload(season, fetched_at=moment)

    payload = parse_referee_tendency_rows(raw_rows, season=season, fetched_at=moment)
    expires_at = moment + timedelta(minutes=ttl_minutes)
    _write_cache_row(
        db, season=season, payload=payload, moment=moment, expires_at=expires_at,
    )
    return payload


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
