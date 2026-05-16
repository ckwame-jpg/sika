"""Smarter #8 (phase 3) — cache layer for empirical parlay
correlations.

Phase 2 (PR #129) shipped ``compute_empirical_pair_correlations``
that scans settled parlay history; phase 3 caches the result so the
hot parlay-scoring path can blend empirical with theoretical priors
without re-running the scan on every combo.

On-disk shape: a single ``OperatorSetting`` row keyed by
``parlay_correlation_empirical_v1`` carrying:

    {
      "computed_at": "<UTC ISO>",
      "lookback_days": <int>,
      "min_sample": <int>,
      "estimates": {
        "shared_subject": {"coefficient": float, "sample_size": int} | null,
        "same_team":      {...} | null,
        "shared_opponent": {...} | null
      }
    }

``None`` entries mean the corresponding pair type had fewer than
``min_sample`` observations; the consumer falls back to the
theoretical prior for that pair type (via
``blend_theoretical_with_empirical``).

Refresh semantics: the cache uses a fresh-or-stale flow — within
the TTL the cached blob is returned untouched; past the TTL the
cache is recomputed and rewritten. Operators can force a refresh
via ``invalidate_parlay_correlation_cache``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OperatorSetting
from app.services.parlay_correlation import (
    DEFAULT_MIN_SAMPLE,
    PairCorrelation,
)
from app.services.parlay_correlation_db import (
    DEFAULT_LOOKBACK_DAYS,
    PAIR_TYPES,
    compute_empirical_pair_correlations,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CACHE_KEY",
    "DEFAULT_CACHE_TTL_MINUTES",
    "cached_empirical_pair_correlations",
    "invalidate_parlay_correlation_cache",
]

CACHE_KEY = "parlay_correlation_empirical_v1"

# Refresh daily by default — the empirical scan is heavy
# (full settled-parlay history) and the estimate doesn't move much
# day-to-day at the per-pair-type granularity. Operators that want
# more frequent updates can invalidate via the helper below.
DEFAULT_CACHE_TTL_MINUTES = 1440


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _serialize(estimates: dict[str, PairCorrelation | None]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair_type in PAIR_TYPES:
        value = estimates.get(pair_type)
        if value is None:
            out[pair_type] = None
        else:
            out[pair_type] = {
                "coefficient": float(value.coefficient),
                "sample_size": int(value.sample_size),
            }
    return out


def _deserialize(payload: dict[str, Any]) -> dict[str, PairCorrelation | None]:
    out: dict[str, PairCorrelation | None] = {}
    for pair_type in PAIR_TYPES:
        entry = payload.get(pair_type)
        if not isinstance(entry, dict):
            out[pair_type] = None
            continue
        coefficient = entry.get("coefficient")
        sample_size = entry.get("sample_size")
        if coefficient is None or sample_size is None:
            out[pair_type] = None
            continue
        try:
            out[pair_type] = PairCorrelation(
                coefficient=float(coefficient),
                sample_size=int(sample_size),
            )
        except (TypeError, ValueError):
            out[pair_type] = None
    return out


def _refresh_cache(
    db: Session,
    *,
    lookback_days: int,
    min_sample: int,
    now: datetime,
) -> tuple[dict[str, PairCorrelation | None], dict[str, Any]]:
    estimates = compute_empirical_pair_correlations(
        db,
        end_date=now,
        lookback_days=lookback_days,
        min_sample=min_sample,
    )
    blob = {
        "computed_at": now.isoformat(),
        "lookback_days": int(lookback_days),
        "min_sample": int(min_sample),
        "estimates": _serialize(estimates),
    }
    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == CACHE_KEY))
    if row is None:
        row = OperatorSetting(key=CACHE_KEY)
        db.add(row)
    row.value = json.dumps(blob)
    db.flush()
    return estimates, blob


def cached_empirical_pair_correlations(
    db: Session,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    ttl_minutes: int = DEFAULT_CACHE_TTL_MINUTES,
    now: datetime | None = None,
) -> dict[str, PairCorrelation | None]:
    """Return the cached empirical pair-correlation map; recompute
    when stale or missing.

    Always returns a dict with every entry in ``PAIR_TYPES`` (None
    when the pair type had insufficient samples). Callers should
    pipe the result through ``blend_theoretical_with_empirical`` to
    get the final per-pair weight.

    ``ttl_minutes <= 0`` forces a refresh on every call (operator
    debug knob; never used by the production path).
    """
    if ttl_minutes < 0:
        raise ValueError(f"ttl_minutes must be >= 0, got {ttl_minutes}")
    reference_now = _coerce_utc(now) if now is not None else datetime.now(timezone.utc)
    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == CACHE_KEY))
    if row is not None and row.value:
        try:
            blob = json.loads(row.value)
        except json.JSONDecodeError as exc:
            logger.warning(
                "parlay_correlation_cache: stored payload unparseable; recomputing (%s)",
                exc,
            )
            blob = None
        if isinstance(blob, dict):
            computed_at_raw = blob.get("computed_at")
            try:
                computed_at = _coerce_utc(datetime.fromisoformat(str(computed_at_raw)))
            except (TypeError, ValueError):
                computed_at = None
            if computed_at is not None and ttl_minutes > 0:
                age = reference_now - computed_at
                if age < timedelta(minutes=ttl_minutes):
                    return _deserialize(blob.get("estimates") or {})

    estimates, _blob = _refresh_cache(
        db,
        lookback_days=lookback_days,
        min_sample=min_sample,
        now=reference_now,
    )
    return estimates


def invalidate_parlay_correlation_cache(db: Session) -> bool:
    """Drop the cached blob so the next ``cached_*`` call recomputes
    fresh. Returns True when a row existed and was cleared, False
    when nothing was cached (no-op).
    """
    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == CACHE_KEY))
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True
