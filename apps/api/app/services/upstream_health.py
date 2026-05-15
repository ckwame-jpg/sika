"""Smarter #23 — per-upstream-source freshness tracking.

ESPN scoreboard fails silently. Kalshi 429s. basketball-reference cache
expires. The high-level ``/health`` data-stale flag only catches issues
that bubble up to the refresh-job runtime — individual upstream
failures (a single ESPN endpoint going dark for hours while the rest
of the system keeps serving cached data) don't surface anywhere.

This module provides a tiny success/failure recording API plus a read
function. State lives in ``OperatorSetting`` (JSON blob keyed by
``upstream_health_<source>``) so adding a new source doesn't need a
migration; recording is a no-op at the call site (one line) so wiring
new sources is cheap.

The PR that introduced this module wires only NBA Stats — the other
sources listed in ``UPSTREAM_SOURCES`` ship the canonical name in
``/health`` but show ``last_success_at = None`` until a follow-up
wires the call site. That's intentional: operators see "this source
has never reported in" as an explicit signal rather than a missing
field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import OperatorSetting, utcnow


logger = logging.getLogger(__name__)


# Canonical upstream-source identifiers. Adding a new source means
# adding it here AND calling ``record_upstream_success`` /
# ``record_upstream_failure`` at the corresponding loader.
UPSTREAM_SOURCES: tuple[str, ...] = (
    "espn_scoreboard",
    "espn_player_search",
    "espn_player_gamelog",
    "espn_injuries",
    "kalshi_markets",
    "kalshi_market_snapshots",
    "nba_stats",
    "basketball_reference",
    "mlb_stats",
    "the_odds_api",
)


# Default age before a source is considered "stale" in the /health
# surface. Operators can tune per-source thresholds later via an
# operator-settings panel (deferred to a follow-up PR).
DEFAULT_STALE_AFTER = timedelta(hours=24)


_KEY_PREFIX = "upstream_health_"


def _key(source: str) -> str:
    return f"{_KEY_PREFIX}{source}"


@dataclass(frozen=True, slots=True)
class UpstreamSourceHealth:
    """Snapshot of a single upstream's recent health.

    ``last_success_at`` / ``last_failure_at`` are the most-recent
    timestamps in each category — they overlap (a source can succeed
    after a failure and both fields stay populated). ``last_error`` is
    cleared when a fresh success lands so it always reflects the
    error from the most-recent failure that has not yet been replaced
    by a success.
    """
    source: str
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None

    def is_stale(self, *, now: datetime | None = None, stale_after: timedelta = DEFAULT_STALE_AFTER) -> bool:
        """True when we don't have a success inside the staleness window.

        A source that has NEVER succeeded is considered stale by
        definition — operators should see the explicit ``last_success_at
        is None`` signal.
        """
        moment = now if now is not None else utcnow()
        if self.last_success_at is None:
            return True
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        last = self.last_success_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (moment - last) > stale_after


def _operator_get(db: Session, key: str) -> dict | None:
    row = db.query(OperatorSetting).filter(OperatorSetting.key == key).one_or_none()
    return dict(row.value or {}) if row is not None else None


def _operator_set(db: Session, key: str, value: dict) -> None:
    row = db.query(OperatorSetting).filter(OperatorSetting.key == key).one_or_none()
    if row is None:
        row = OperatorSetting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.flush()


def record_upstream_success(db: Session, source: str, *, now: datetime | None = None) -> None:
    """Record a successful fetch from ``source``.

    Updates ``last_success_at`` to ``now`` and CLEARS ``last_error``
    so the operator surface doesn't keep showing a stale error message
    after the source recovers. ``last_failure_at`` is preserved — the
    timeline of "when did this last fail" is useful even after recovery.
    """
    moment = now or utcnow()
    payload = _operator_get(db, _key(source)) or {}
    payload["last_success_at"] = moment.isoformat()
    payload["last_error"] = None
    _operator_set(db, _key(source), payload)


def record_upstream_failure(db: Session, source: str, error: str, *, now: datetime | None = None) -> None:
    """Record a failed fetch from ``source`` with the operator-visible
    error message. ``last_success_at`` is preserved so the surface can
    show both 'last good response' and 'most recent failure'."""
    moment = now or utcnow()
    payload = _operator_get(db, _key(source)) or {}
    payload["last_failure_at"] = moment.isoformat()
    payload["last_error"] = error
    _operator_set(db, _key(source), payload)


def _parse_dt(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def get_upstream_health(
    db: Session,
    sources: Iterable[str] | None = None,
) -> list[UpstreamSourceHealth]:
    """Return the health snapshot for every known upstream source.

    Sources that have never been recorded return a ``None``-filled row
    so the operator surface always shows the full registry. Order
    matches ``UPSTREAM_SOURCES`` for stable display.
    """
    selected = tuple(sources) if sources is not None else UPSTREAM_SOURCES
    rows: list[UpstreamSourceHealth] = []
    for source in selected:
        payload = _operator_get(db, _key(source)) or {}
        rows.append(
            UpstreamSourceHealth(
                source=source,
                last_success_at=_parse_dt(payload.get("last_success_at")),
                last_failure_at=_parse_dt(payload.get("last_failure_at")),
                last_error=payload.get("last_error"),
            )
        )
    return rows
