"""Augment Stats Assistant summary metrics with advanced data + percentiles.

The base ``_build_summary_metrics`` in ``stats_query`` returns a flat dict of
basic box-score keys. PR 3c layers on top:

  - Tags every basic key with ``metric_categories[key] = "basic"``
  - Adds advanced keys (TS%, USG%, ORtg, DRtg... for NBA; xBA, xwOBA,
    barrel rate, hard-hit rate for MLB batters) tagged as ``"advanced"``
  - Computes a 0-100 league percentile rank for each advanced metric
    where the league percentile cache is populated; otherwise leaves the
    metric without a rank (the frontend renders ``—`` in that case)

Cache misses must NOT raise — the frontend's ``StatsSummaryRead.percentiles``
and ``metric_categories`` are both optional, and the basic ``metrics`` dict
must always be returned intact.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Metric maps — which keys are advanced for each sport, and what shape we
# pull them from (advanced_payload key path).

_NBA_ADVANCED_KEYS: tuple[str, ...] = (
    "ts_pct",
    "efg_pct",
    "usg_pct",
    "off_rating",
    "def_rating",
    "net_rating",
    "pie",
    "pace",
)

# Metrics where lower is better — invert the percentile rank so the UI's
# "high = good" shading reads correctly.
#   - ``def_rating``: lower defensive rating = better defense
#   - ``strikeout_rate``: lower batter K% = better contact
# (``babip`` is intentionally NOT inverted — it's contextual luck rather than
#  a one-directional skill metric, so a raw rank is more honest than a forced
#  inversion. Reviewer judgement; revisit if the UI calls for it.)
_LOWER_IS_BETTER: frozenset[str] = frozenset({"def_rating", "strikeout_rate"})

_MLB_BATTER_ADVANCED_KEYS: dict[str, tuple[str, str]] = {
    # summary key → (sub-payload key, source field name)
    "woba": ("sabermetrics", "woba"),
    "iso": ("sabermetrics", "iso"),
    "walk_rate": ("sabermetrics", "walk_rate"),
    "strikeout_rate": ("sabermetrics", "strikeout_rate"),
    "wrc_plus": ("sabermetrics", "wrc_plus"),
    "babip": ("sabermetrics", "babip"),
    "xwoba": ("statcast", "xwoba"),
    "xba": ("statcast", "xba"),
    "xslg": ("statcast", "xslg"),
    "barrel_rate": ("statcast", "barrel_rate"),
    "hard_hit_rate": ("statcast", "hard_hit_rate"),
    "exit_velocity_avg": ("statcast", "exit_velocity_avg"),
    "launch_angle_avg": ("statcast", "launch_angle_avg"),
    "sweet_spot_rate": ("statcast", "sweet_spot_rate"),
}


# -----------------------------------------------------------------------------
# Public API


def augment_summary_with_advanced(
    db: Session | None,
    *,
    sport_key: str,
    player: dict[str, Any] | None,
    season: int,
    summary_metrics: dict[str, float | None],
) -> tuple[dict[str, float | None], dict[str, float], dict[str, str]]:
    """Return ``(metrics, percentiles, metric_categories)``.

    ``metrics`` is the input dict augmented with advanced keys (when
    available). ``percentiles`` is an empty dict unless a league
    percentile cache hit happened. ``metric_categories`` always tags
    every key in ``metrics`` as ``"basic"`` or ``"advanced"``.
    """
    augmented: dict[str, float | None] = dict(summary_metrics)
    percentiles: dict[str, float] = {}
    categories: dict[str, str] = {key: "basic" for key in summary_metrics}

    if db is None:
        return augmented, percentiles, categories

    sport = sport_key.upper()
    try:
        if sport == "NBA":
            _augment_nba(db, player or {}, season, augmented, categories, percentiles)
        elif sport == "MLB":
            _augment_mlb(db, player or {}, season, augmented, categories, percentiles)
    except Exception:
        # Cache reads must never break the user-facing query response, but
        # silent failure makes regressions invisible. Log with the player
        # context so an oncall has a thread to pull on if augmentation
        # starts dropping out in production.
        logger.warning(
            "stats_summary_augment failed for sport=%s player=%s season=%s",
            sport_key,
            (player or {}).get("display_name"),
            season,
            exc_info=True,
        )

    return augmented, percentiles, categories


# -----------------------------------------------------------------------------
# NBA


def _augment_nba(
    db: Session,
    player: dict[str, Any],
    season: int,
    metrics: dict[str, float | None],
    categories: dict[str, str],
    percentiles: dict[str, float],
) -> None:
    from app.services.advanced_stats import (
        load_nba_advanced,
        load_nba_league_percentiles,
        resolve_nba_stats_player_id,
    )

    nba_stats_id = player.get("nba_stats_id")
    if not nba_stats_id:
        nba_stats_id = resolve_nba_stats_player_id(
            db,
            espn_athlete_id=player.get("athlete_id"),
            full_name=player.get("display_name") or "",
            team_abbreviation=None,
            season=season,
            allow_network=False,
        )
    if not nba_stats_id:
        return

    advanced = load_nba_advanced(
        db,
        nba_stats_player_id=str(nba_stats_id),
        season=season,
        allow_network=False,
    )
    season_avg = (advanced.payload or {}).get("season_avg") or {}
    for key in _NBA_ADVANCED_KEYS:
        value = season_avg.get(key)
        if isinstance(value, (int, float)):
            metrics[key] = round(float(value), 4)
            categories[key] = "advanced"

    league = load_nba_league_percentiles(db, season=season, allow_network=False)
    breakpoints = (league.payload or {}).get("breakpoints") or {}
    for key in _NBA_ADVANCED_KEYS:
        value = metrics.get(key)
        if not isinstance(value, (int, float)):
            continue
        rank = _percentile_rank(value, breakpoints.get(key), key)
        if rank is not None:
            percentiles[key] = round(rank, 1)


# -----------------------------------------------------------------------------
# MLB


def _augment_mlb(
    db: Session,
    player: dict[str, Any],
    season: int,
    metrics: dict[str, float | None],
    categories: dict[str, str],
    percentiles: dict[str, float],
) -> None:
    from app.services.mlb_advanced import (
        load_mlb_batter_advanced,
        load_mlb_statcast_batter,
        resolve_mlb_stats_player_id,
    )

    mlb_player_id = player.get("mlb_stats_id")
    if not mlb_player_id:
        mlb_player_id = resolve_mlb_stats_player_id(
            db,
            espn_athlete_id=player.get("athlete_id"),
            full_name=player.get("display_name") or "",
            team_abbreviation=None,
            season=season,
            allow_network=False,
        )
    if not mlb_player_id:
        return

    saber = load_mlb_batter_advanced(
        db,
        mlb_player_id=str(mlb_player_id),
        season=season,
        allow_network=False,
    )
    statcast = load_mlb_statcast_batter(
        db,
        mlb_player_id=str(mlb_player_id),
        season=season,
        allow_network=False,
    )
    saber_avg = (saber.payload or {}).get("season_avg") or {}
    statcast_avg = (statcast.payload or {}).get("season_avg") or {}

    for summary_key, (source, field) in _MLB_BATTER_ADVANCED_KEYS.items():
        bucket = saber_avg if source == "sabermetrics" else statcast_avg
        value = bucket.get(field)
        if isinstance(value, (int, float)):
            metrics[summary_key] = round(float(value), 4)
            categories[summary_key] = "advanced"

    # MLB league percentiles cache exists in models (MlbLeaguePercentilesCache)
    # but no loader is wired yet. Read directly so when a writer lands the
    # ranks light up automatically; until then this is a graceful no-op.
    breakpoints = _read_mlb_league_breakpoints(db, season)
    for summary_key in _MLB_BATTER_ADVANCED_KEYS:
        value = metrics.get(summary_key)
        if not isinstance(value, (int, float)):
            continue
        rank = _percentile_rank(value, breakpoints.get(summary_key), summary_key)
        if rank is not None:
            percentiles[summary_key] = round(rank, 1)


def _read_mlb_league_breakpoints(db: Session, season: int) -> dict[str, dict[str, float]]:
    """Best-effort read of cached MLB league percentile breakpoints. Returns
    an empty dict when the cache row doesn't exist."""
    from app.models import MlbLeaguePercentilesCache

    row = (
        db.query(MlbLeaguePercentilesCache)
        .filter(
            MlbLeaguePercentilesCache.season == season,
            MlbLeaguePercentilesCache.metric_key == "advanced",
        )
        .one_or_none()
    )
    if row is None:
        return {}
    payload = row.payload or {}
    breakpoints = payload.get("breakpoints") if isinstance(payload, dict) else None
    return breakpoints or {}


# -----------------------------------------------------------------------------
# Percentile rank


def _percentile_rank(
    value: float,
    breakpoints: dict[str, Any] | None,
    metric_key: str,
) -> float | None:
    """Linearly interpolate a percentile rank (0-100) from a discrete set
    of percentile breakpoints (e.g. ``{p10: ..., p25: ..., p50: ...}``).

    For metrics where lower is better (``def_rating``), invert the result
    so a low value returns a high rank.
    """
    if not isinstance(breakpoints, dict):
        return None

    points: list[tuple[int, float]] = []
    for label, raw in breakpoints.items():
        if not isinstance(raw, (int, float)):
            continue
        if not isinstance(label, str) or not label.startswith("p"):
            continue
        try:
            pct = int(label[1:])
        except ValueError:
            continue
        points.append((pct, float(raw)))
    if len(points) < 2:
        return None

    # Sort by pct so we walk in order p10 → p25 → p50 → p75 → p90.
    points.sort(key=lambda item: item[0])

    if value <= points[0][1]:
        rank = float(points[0][0])
    elif value >= points[-1][1]:
        rank = float(points[-1][0])
    else:
        rank = float(points[-1][0])
        for i in range(len(points) - 1):
            lo_pct, lo_val = points[i]
            hi_pct, hi_val = points[i + 1]
            if lo_val <= value <= hi_val:
                if hi_val == lo_val:
                    rank = float(hi_pct)
                else:
                    fraction = (value - lo_val) / (hi_val - lo_val)
                    rank = lo_pct + fraction * (hi_pct - lo_pct)
                break

    if metric_key in _LOWER_IS_BETTER:
        rank = 100.0 - rank
    return max(0.0, min(100.0, rank))
