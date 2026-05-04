"""Long-tail NBA Stats endpoints — hustle, tracking, clutch, defense.

These are league-wide leaderboards rather than per-player gamelogs, so
each cache row covers all players for a season (or season + sub-kind).
The orchestrator loads once per refresh cycle and feature emitters
look up specific player IDs from the cached payload.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Query, Session

from app.clients.nba_stats import NbaClientLike, make_nba_client, parse_result_set
from app.config import get_settings
from app.models import (
    NbaClutchPlayerCache,
    NbaHustlePlayerCache,
    NbaPlayerDefenseCache,
    NbaTrackingCache,
    utcnow,
)
from app.services.advanced_stats import (
    AdvancedLoadResult,
    _coerce_utc,
    _safe_float,
    nba_circuit_breaker_open,
    nba_daily_cap_reached,
    _increment_daily_count,
    _record_nba_failure,
    _record_nba_success,
)


logger = logging.getLogger(__name__)


def _cache_or_fetch(
    db: Session,
    *,
    cached_query: Query,
    ttl: timedelta,
    fetch: Callable[[], dict[str, Any]],
    persist: Callable[[dict[str, Any]], dict[str, Any]],
    moment: datetime,
    allow_network: bool,
    settings: Any,
) -> AdvancedLoadResult:
    """Shared cache-hit / stale-fallback / upstream-fetch shape."""
    cached = cached_query.one_or_none()
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if (
        not allow_network
        or not settings.advanced_stats_enabled
        or nba_circuit_breaker_open(db, now=moment)
        or nba_daily_cap_reached(db)
    ):
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    try:
        raw = fetch()
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001
        _record_nba_failure(db)
        logger.warning("NBA long-tail fetch failed: %s", exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    structured = persist(raw)
    return AdvancedLoadResult(payload=structured, cache_status="miss", complete=True)


# -----------------------------------------------------------------------------
# Hustle stats

def load_nba_hustle_player(
    db: Session,
    *,
    season: int,
    client: NbaClientLike | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_hustle_player_cache_minutes)
    nba_client = client or make_nba_client()

    cached_query = db.query(NbaHustlePlayerCache).filter(NbaHustlePlayerCache.season == season)

    def _persist(raw: dict[str, Any]) -> dict[str, Any]:
        rows = parse_result_set(raw)
        players = {
            str(row.get("PLAYER_ID")): {
                "contested_shots": _safe_float(row.get("CONTESTED_SHOTS")),
                "contested_shots_2pt": _safe_float(row.get("CONTESTED_SHOTS_2PT")),
                "contested_shots_3pt": _safe_float(row.get("CONTESTED_SHOTS_3PT")),
                "deflections": _safe_float(row.get("DEFLECTIONS")),
                "charges_drawn": _safe_float(row.get("CHARGES_DRAWN")),
                "screen_assists": _safe_float(row.get("SCREEN_ASSISTS")),
                "screen_ast_pts": _safe_float(row.get("SCREEN_AST_PTS")),
                "loose_balls_recovered": _safe_float(row.get("LOOSE_BALLS_RECOVERED")),
                "off_loose_balls_recovered": _safe_float(row.get("OFF_LOOSE_BALLS_RECOVERED")),
                "def_loose_balls_recovered": _safe_float(row.get("DEF_LOOSE_BALLS_RECOVERED")),
                "box_outs": _safe_float(row.get("BOX_OUTS")),
                "off_box_outs": _safe_float(row.get("OFF_BOX_OUTS")),
                "def_box_outs": _safe_float(row.get("DEF_BOX_OUTS")),
            }
            for row in rows
            if row.get("PLAYER_ID") is not None
        }
        structured = {"players": players, "sample_size": len(rows)}
        existing = cached_query.one_or_none()
        if existing is None:
            db.add(
                NbaHustlePlayerCache(
                    season=season,
                    payload=structured,
                    cached_at=moment,
                    expires_at=moment + ttl,
                )
            )
        else:
            existing.payload = structured
            existing.cached_at = moment
            existing.expires_at = moment + ttl
        db.flush()
        return structured

    return _cache_or_fetch(
        db,
        cached_query=cached_query,
        ttl=ttl,
        fetch=lambda: nba_client.fetch_hustle_stats_player(season),
        persist=_persist,
        moment=moment,
        allow_network=allow_network,
        settings=settings,
    )


# -----------------------------------------------------------------------------
# Tracking — Drives only for PR 2c. Other PtMeasureTypes plug in via the same loader.

def load_nba_tracking(
    db: Session,
    *,
    season: int,
    pt_measure_type: str = "Drives",
    client: NbaClientLike | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_tracking_cache_minutes)
    nba_client = client or make_nba_client()

    cached_query = db.query(NbaTrackingCache).filter(
        NbaTrackingCache.season == season,
        NbaTrackingCache.pt_measure_type == pt_measure_type,
    )

    def _persist(raw: dict[str, Any]) -> dict[str, Any]:
        rows = parse_result_set(raw)
        players = {str(row.get("PLAYER_ID")): row for row in rows if row.get("PLAYER_ID") is not None}
        structured = {"pt_measure_type": pt_measure_type, "players": players, "sample_size": len(rows)}
        existing = cached_query.one_or_none()
        if existing is None:
            db.add(
                NbaTrackingCache(
                    season=season,
                    pt_measure_type=pt_measure_type,
                    payload=structured,
                    cached_at=moment,
                    expires_at=moment + ttl,
                )
            )
        else:
            existing.payload = structured
            existing.cached_at = moment
            existing.expires_at = moment + ttl
        db.flush()
        return structured

    return _cache_or_fetch(
        db,
        cached_query=cached_query,
        ttl=ttl,
        fetch=lambda: nba_client.fetch_player_tracking(season, pt_measure_type=pt_measure_type),
        persist=_persist,
        moment=moment,
        allow_network=allow_network,
        settings=settings,
    )


# -----------------------------------------------------------------------------
# Clutch

def load_nba_clutch_player(
    db: Session,
    *,
    season: int,
    client: NbaClientLike | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_clutch_cache_minutes)
    nba_client = client or make_nba_client()

    cached_query = db.query(NbaClutchPlayerCache).filter(NbaClutchPlayerCache.season == season)

    def _persist(raw: dict[str, Any]) -> dict[str, Any]:
        rows = parse_result_set(raw)
        players = {
            str(row.get("PLAYER_ID")): {
                "min": _safe_float(row.get("MIN")),
                "pts": _safe_float(row.get("PTS")),
                "fg_pct": _safe_float(row.get("FG_PCT")),
                "fg3_pct": _safe_float(row.get("FG3_PCT")),
                "ft_pct": _safe_float(row.get("FT_PCT")),
                "plus_minus": _safe_float(row.get("PLUS_MINUS")),
                "ast": _safe_float(row.get("AST")),
                "tov": _safe_float(row.get("TOV")),
                "stl": _safe_float(row.get("STL")),
                "blk": _safe_float(row.get("BLK")),
            }
            for row in rows
            if row.get("PLAYER_ID") is not None
        }
        structured = {"players": players, "sample_size": len(rows)}
        existing = cached_query.one_or_none()
        if existing is None:
            db.add(
                NbaClutchPlayerCache(
                    season=season,
                    payload=structured,
                    cached_at=moment,
                    expires_at=moment + ttl,
                )
            )
        else:
            existing.payload = structured
            existing.cached_at = moment
            existing.expires_at = moment + ttl
        db.flush()
        return structured

    return _cache_or_fetch(
        db,
        cached_query=cached_query,
        ttl=ttl,
        fetch=lambda: nba_client.fetch_player_clutch(season),
        persist=_persist,
        moment=moment,
        allow_network=allow_network,
        settings=settings,
    )


# -----------------------------------------------------------------------------
# Defense dashboard — defended FG% by zone

def load_nba_player_defense(
    db: Session,
    *,
    season: int,
    defense_category: str = "Overall",
    client: NbaClientLike | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_player_defense_cache_minutes)
    nba_client = client or make_nba_client()

    cached_query = db.query(NbaPlayerDefenseCache).filter(
        NbaPlayerDefenseCache.season == season,
        NbaPlayerDefenseCache.defense_category == defense_category,
    )

    def _persist(raw: dict[str, Any]) -> dict[str, Any]:
        rows = parse_result_set(raw)
        players = {
            str(row.get("CLOSE_DEF_PERSON_ID") or row.get("PLAYER_ID")): {
                "defended_fga": _safe_float(row.get("D_FGA")),
                "defended_fgm": _safe_float(row.get("D_FGM")),
                "defended_fg_pct": _safe_float(row.get("D_FG_PCT")),
                "normal_fg_pct": _safe_float(row.get("NORMAL_FG_PCT")),
                "fg_pct_diff": _safe_float(row.get("PCT_PLUSMINUS")),
            }
            for row in rows
            if row.get("CLOSE_DEF_PERSON_ID") is not None or row.get("PLAYER_ID") is not None
        }
        structured = {
            "defense_category": defense_category,
            "players": players,
            "sample_size": len(rows),
        }
        existing = cached_query.one_or_none()
        if existing is None:
            db.add(
                NbaPlayerDefenseCache(
                    season=season,
                    defense_category=defense_category,
                    payload=structured,
                    cached_at=moment,
                    expires_at=moment + ttl,
                )
            )
        else:
            existing.payload = structured
            existing.cached_at = moment
            existing.expires_at = moment + ttl
        db.flush()
        return structured

    return _cache_or_fetch(
        db,
        cached_query=cached_query,
        ttl=ttl,
        fetch=lambda: nba_client.fetch_player_defense_dashboard(season, defense_category=defense_category),
        persist=_persist,
        moment=moment,
        allow_network=allow_network,
        settings=settings,
    )


# -----------------------------------------------------------------------------
# Feature emitters

def emit_nba_hustle_features(
    hustle_payload: dict[str, Any] | None, nba_stats_player_id: str | None
) -> dict[str, float]:
    if not hustle_payload or not nba_stats_player_id:
        return {}
    record = (hustle_payload.get("players") or {}).get(str(nba_stats_player_id))
    if not record:
        return {}
    out: dict[str, float] = {}

    def _set(key: str, value: Any) -> None:
        if isinstance(value, (int, float)):
            out[f"hustle_{key}"] = round(float(value), 3)

    for src in (
        "contested_shots", "contested_shots_2pt", "contested_shots_3pt",
        "deflections", "charges_drawn", "screen_assists", "loose_balls_recovered",
        "box_outs",
    ):
        _set(src, record.get(src))
    if out:
        out["hustle_data_complete"] = 1.0
    return out


def emit_nba_drives_features(
    drives_payload: dict[str, Any] | None, nba_stats_player_id: str | None
) -> dict[str, float]:
    """Drives-tracking emitter (drives_per_game, drives_pts, drives_passes_pct)."""
    if not drives_payload or not nba_stats_player_id:
        return {}
    record = (drives_payload.get("players") or {}).get(str(nba_stats_player_id))
    if not record:
        return {}
    out: dict[str, float] = {}

    def _set(key: str, raw_col: str) -> None:
        value = record.get(raw_col)
        if isinstance(value, (int, float)):
            out[key] = round(float(value), 3)
        elif isinstance(value, str):
            try:
                out[key] = round(float(value), 3)
            except ValueError:
                pass

    _set("drives_per_game", "DRIVES")
    _set("drives_fga", "DRIVE_FGA")
    _set("drives_fg_pct", "DRIVE_FG_PCT")
    _set("drives_pts", "DRIVE_PTS")
    _set("drives_pts_pct", "DRIVE_PTS_PCT")
    _set("drives_pass_pct", "DRIVE_PASSES_PCT")
    _set("drives_ast_pct", "DRIVE_AST_PCT")
    _set("drives_to_pct", "DRIVE_TOV_PCT")
    _set("drives_ft_pct", "DRIVE_FT_PCT")
    if out:
        out["drives_data_complete"] = 1.0
    return out


def emit_nba_clutch_features(
    clutch_payload: dict[str, Any] | None, nba_stats_player_id: str | None
) -> dict[str, float]:
    if not clutch_payload or not nba_stats_player_id:
        return {}
    record = (clutch_payload.get("players") or {}).get(str(nba_stats_player_id))
    if not record:
        return {}
    out: dict[str, float] = {}

    def _set(key: str, value: Any) -> None:
        if isinstance(value, (int, float)):
            out[f"clutch_{key}"] = round(float(value), 3)

    for src in ("min", "pts", "fg_pct", "fg3_pct", "ft_pct", "plus_minus", "ast", "tov", "stl", "blk"):
        _set(src, record.get(src))
    if out:
        out["clutch_data_complete"] = 1.0
    return out


def emit_nba_player_defense_features(
    defense_payload: dict[str, Any] | None, opposing_defender_id: str | None
) -> dict[str, float]:
    """Emit defended FG% allowed by the matchup-up defender (opposing player)."""
    if not defense_payload or not opposing_defender_id:
        return {}
    record = (defense_payload.get("players") or {}).get(str(opposing_defender_id))
    if not record:
        return {}
    out: dict[str, float] = {}

    def _set(key: str, value: Any) -> None:
        if isinstance(value, (int, float)):
            out[f"opponent_defender_{key}"] = round(float(value), 4)

    _set("defended_fga", record.get("defended_fga"))
    _set("defended_fg_pct", record.get("defended_fg_pct"))
    _set("normal_fg_pct", record.get("normal_fg_pct"))
    _set("fg_pct_diff", record.get("fg_pct_diff"))
    if out:
        out["opponent_defender_data_complete"] = 1.0
    return out
