"""Orchestrator for advanced sports statistics.

Each ``load_*`` function mirrors the cache → TTL → stale-fallback semantics
established in ``app.services.scoring._load_player_gamelog``: the resolver
first checks the corresponding cache table, refreshes it from the upstream
client when stale and ``allow_network`` is True, and falls back to whatever
payload (even expired) is still on disk if the upstream call fails.

PR 1 scope: NBA player advanced stats only. MLB, weather, lineups, park
factors, and pitcher metrics will be added in subsequent PRs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.clients.nba_stats import (
    NbaStatsClient,
    parse_result_set,
)
from app.config import get_settings
from app.models import (
    NbaAdvancedGamelogCache,
    NbaLeaguePercentilesCache,
    NbaTeamAdvancedCache,
    OperatorSetting,
    utcnow,
)


logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Public structures

NBA_PLAYER_METRICS: tuple[str, ...] = (
    "ts_pct",
    "efg_pct",
    "usg_pct",
    "off_rating",
    "def_rating",
    "net_rating",
    "pie",
    "ast_pct",
    "oreb_pct",
    "dreb_pct",
    "reb_pct",
    "pace",
)

NBA_TEAM_METRICS: tuple[str, ...] = (
    "off_rating",
    "def_rating",
    "net_rating",
    "pace",
)


@dataclass(slots=True)
class AdvancedLoadResult:
    payload: dict[str, Any]
    cache_status: str  # "hit" | "miss" | "stale" | "skipped"
    complete: bool


# -----------------------------------------------------------------------------
# Circuit breaker — uses OperatorSetting to persist state across restarts

_NBA_CIRCUIT_KEY = "nba_stats_disabled_until"
_NBA_CONSECUTIVE_FAIL_KEY = "nba_stats_consecutive_failures"
_NBA_CONSECUTIVE_FAIL_THRESHOLD = 3
_NBA_DISABLE_HOURS = 24


def _operator_get(db: Session, key: str) -> dict[str, Any] | None:
    row = db.query(OperatorSetting).filter(OperatorSetting.key == key).one_or_none()
    return dict(row.value or {}) if row is not None else None


def _operator_set(db: Session, key: str, value: dict[str, Any]) -> None:
    row = db.query(OperatorSetting).filter(OperatorSetting.key == key).one_or_none()
    if row is None:
        row = OperatorSetting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.flush()


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def nba_circuit_breaker_open(db: Session, *, now: datetime | None = None) -> bool:
    """Return True if the circuit breaker is currently tripped (skip network)."""
    payload = _operator_get(db, _NBA_CIRCUIT_KEY)
    if not payload:
        return False
    raw_until = payload.get("until")
    if not raw_until:
        return False
    try:
        until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    moment = _coerce_utc(now) or utcnow()
    return moment < until


def _record_nba_failure(db: Session) -> None:
    payload = _operator_get(db, _NBA_CONSECUTIVE_FAIL_KEY) or {}
    count = int(payload.get("count", 0)) + 1
    _operator_set(db, _NBA_CONSECUTIVE_FAIL_KEY, {"count": count})
    if count >= _NBA_CONSECUTIVE_FAIL_THRESHOLD:
        until = utcnow() + timedelta(hours=_NBA_DISABLE_HOURS)
        _operator_set(db, _NBA_CIRCUIT_KEY, {"until": until.isoformat(), "tripped_at": utcnow().isoformat()})
        logger.warning("NBA Stats circuit breaker tripped until %s", until.isoformat())


def _record_nba_success(db: Session) -> None:
    if _operator_get(db, _NBA_CONSECUTIVE_FAIL_KEY):
        _operator_set(db, _NBA_CONSECUTIVE_FAIL_KEY, {"count": 0})
    if _operator_get(db, _NBA_CIRCUIT_KEY):
        _operator_set(db, _NBA_CIRCUIT_KEY, {})


# -----------------------------------------------------------------------------
# Daily request cap — tracks per-day fetches and short-circuits when exceeded

_NBA_DAILY_COUNT_PREFIX = "nba_stats_daily_count_"


def _today_key() -> str:
    return f"{_NBA_DAILY_COUNT_PREFIX}{utcnow().date().isoformat()}"


def nba_daily_cap_reached(db: Session) -> bool:
    settings = get_settings()
    payload = _operator_get(db, _today_key()) or {}
    return int(payload.get("count", 0)) >= int(settings.nba_stats_daily_request_cap)


def _increment_daily_count(db: Session) -> None:
    key = _today_key()
    payload = _operator_get(db, key) or {}
    payload["count"] = int(payload.get("count", 0)) + 1
    _operator_set(db, key, payload)


# -----------------------------------------------------------------------------
# NBA player advanced loader
#
# Player-ID resolution (ESPN athlete_id → NBA Stats PERSON_ID) is deferred to
# the next PR. Until then, the resolver in ``app.services.scoring`` reads
# ``EspnPlayerSearchCache.payload["nba_stats_id"]`` directly — when missing,
# the load is skipped and the heuristic continues with box-score data only.

def _row_to_metric_dict(row: dict[str, Any]) -> dict[str, float]:
    """Map an NBA Stats Advanced gamelog row to our snake_case metric dict."""
    return {
        "ts_pct": _safe_float(row.get("TS_PCT")),
        "efg_pct": _safe_float(row.get("EFG_PCT")),
        "usg_pct": _safe_float(row.get("USG_PCT")),
        "off_rating": _safe_float(row.get("OFF_RATING")),
        "def_rating": _safe_float(row.get("DEF_RATING")),
        "net_rating": _safe_float(row.get("NET_RATING")),
        "pie": _safe_float(row.get("PIE")),
        "ast_pct": _safe_float(row.get("AST_PCT")),
        "oreb_pct": _safe_float(row.get("OREB_PCT")),
        "dreb_pct": _safe_float(row.get("DREB_PCT")),
        "reb_pct": _safe_float(row.get("REB_PCT")),
        "pace": _safe_float(row.get("PACE")),
        "minutes": _safe_float(row.get("MIN")),
        "game_date": row.get("GAME_DATE"),
        "matchup": row.get("MATCHUP"),
    }


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aggregate(games: list[dict[str, Any]], metric_keys: Iterable[str]) -> dict[str, float | None]:
    """Compute the simple mean of each metric across the supplied games."""
    out: dict[str, float | None] = {}
    for key in metric_keys:
        values = [g.get(key) for g in games if isinstance(g.get(key), (int, float))]
        out[key] = sum(values) / len(values) if values else None
    return out


def _build_player_advanced_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Convert raw NBA Stats response into rolling-window structured payload."""
    raw_rows = parse_result_set(raw_payload, name="PlayerGameLogs")
    games = [_row_to_metric_dict(r) for r in raw_rows]
    # Rows are returned with most-recent first.
    games_sorted_recent_first = list(games)
    return {
        "season_avg": _aggregate(games_sorted_recent_first, NBA_PLAYER_METRICS),
        "recent_3_avg": _aggregate(games_sorted_recent_first[:3], NBA_PLAYER_METRICS),
        "recent_5_avg": _aggregate(games_sorted_recent_first[:5], NBA_PLAYER_METRICS),
        "recent_10_avg": _aggregate(games_sorted_recent_first[:10], NBA_PLAYER_METRICS),
        "games_played": len(games_sorted_recent_first),
        "recent_games": games_sorted_recent_first[:10],
    }


def load_nba_advanced(
    db: Session,
    *,
    nba_stats_player_id: str,
    season: int,
    client: NbaStatsClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Return advanced stats for one player-season, refreshing if stale."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_advanced_cache_minutes)

    cached = (
        db.query(NbaAdvancedGamelogCache)
        .filter(
            NbaAdvancedGamelogCache.athlete_id == str(nba_stats_player_id),
            NbaAdvancedGamelogCache.season == season,
        )
        .one_or_none()
    )

    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)

    if not allow_network or not settings.advanced_stats_enabled:
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    if nba_circuit_breaker_open(db, now=moment):
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="skipped", complete=False)

    if nba_daily_cap_reached(db):
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="skipped", complete=False)

    nba_client = client or NbaStatsClient()
    try:
        raw = nba_client.fetch_player_advanced_gamelog(nba_stats_player_id, season)
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001 — broad on purpose, network is unpredictable
        _record_nba_failure(db)
        logger.warning(
            "NBA Stats advanced gamelog fetch failed for player %s season %d: %s",
            nba_stats_player_id,
            season,
            exc,
        )
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    structured = _build_player_advanced_payload(raw)
    expires_at = moment + ttl
    if cached is None:
        db.add(
            NbaAdvancedGamelogCache(
                athlete_id=str(nba_stats_player_id),
                season=season,
                payload=structured,
                cached_at=moment,
                expires_at=expires_at,
            )
        )
    else:
        cached.payload = structured
        cached.cached_at = moment
        cached.expires_at = expires_at
    db.flush()
    return AdvancedLoadResult(payload=structured, cache_status="miss", complete=True)


# -----------------------------------------------------------------------------
# Feature key emission — what gets written to the prediction features dict

def emit_nba_player_features(payload: dict[str, Any] | None) -> dict[str, float]:
    """Translate the cached structured payload into the feature dict used by scoring.

    Returns an empty dict (no keys) when ``payload`` is missing — the scoring
    layer treats absent keys as ``advanced_data_complete = 0.0``.
    """
    if not payload:
        return {}
    season_avg = payload.get("season_avg") or {}
    recent_avg = payload.get("recent_10_avg") or {}

    out: dict[str, float] = {}

    def _set(key: str, value: float | None) -> None:
        if isinstance(value, (int, float)):
            out[key] = round(float(value), 4)

    _set("recent_true_shooting_pct", recent_avg.get("ts_pct"))
    _set("season_true_shooting_pct", season_avg.get("ts_pct"))
    _set("recent_effective_fg_pct", recent_avg.get("efg_pct"))
    _set("season_effective_fg_pct", season_avg.get("efg_pct"))
    _set("recent_usage_pct", recent_avg.get("usg_pct"))
    _set("season_usage_pct", season_avg.get("usg_pct"))
    _set("recent_offensive_rating", recent_avg.get("off_rating"))
    _set("season_offensive_rating", season_avg.get("off_rating"))
    _set("recent_defensive_rating", recent_avg.get("def_rating"))
    _set("season_defensive_rating", season_avg.get("def_rating"))
    _set("recent_net_rating", recent_avg.get("net_rating"))
    _set("season_net_rating", season_avg.get("net_rating"))
    _set("recent_pace", recent_avg.get("pace"))
    _set("season_pace", season_avg.get("pace"))
    _set("recent_pie", recent_avg.get("pie"))
    _set("season_pie", season_avg.get("pie"))
    _set("recent_assist_pct", recent_avg.get("ast_pct"))
    _set("recent_rebound_pct", recent_avg.get("reb_pct"))

    if out:
        out["advanced_data_complete"] = 1.0
    return out


# -----------------------------------------------------------------------------
# Team advanced loader

def load_nba_team_advanced(
    db: Session,
    *,
    season: int,
    client: NbaStatsClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Return season-level advanced metrics for every NBA team.

    Single-row payload keyed by ``team_id="ALL"`` because this fetch returns
    all teams in one shot. Per-team rows are stored inside the ``payload``
    dict under ``"teams"``.
    """
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_team_advanced_cache_minutes)

    cached = (
        db.query(NbaTeamAdvancedCache)
        .filter(NbaTeamAdvancedCache.team_id == "ALL", NbaTeamAdvancedCache.season == season)
        .one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if not allow_network or not settings.advanced_stats_enabled or nba_circuit_breaker_open(db, now=moment) or nba_daily_cap_reached(db):
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    nba_client = client or NbaStatsClient()
    try:
        raw = nba_client.fetch_team_advanced(season)
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001
        _record_nba_failure(db)
        logger.warning("NBA Stats team advanced fetch failed for season %d: %s", season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    rows = parse_result_set(raw)
    teams = {
        str(row.get("TEAM_ID")): {
            "team_id": str(row.get("TEAM_ID")),
            "team_name": row.get("TEAM_NAME"),
            "off_rating": _safe_float(row.get("OFF_RATING")),
            "def_rating": _safe_float(row.get("DEF_RATING")),
            "net_rating": _safe_float(row.get("NET_RATING")),
            "pace": _safe_float(row.get("PACE")),
        }
        for row in rows
        if row.get("TEAM_ID") is not None
    }
    structured = {"teams": teams}

    if cached is None:
        db.add(
            NbaTeamAdvancedCache(
                team_id="ALL",
                season=season,
                payload=structured,
                cached_at=moment,
                expires_at=moment + ttl,
            )
        )
    else:
        cached.payload = structured
        cached.cached_at = moment
        cached.expires_at = moment + ttl
    db.flush()
    return AdvancedLoadResult(payload=structured, cache_status="miss", complete=True)


# -----------------------------------------------------------------------------
# League percentiles loader (used by Stats Assistant UI)

def load_nba_league_percentiles(
    db: Session,
    *,
    season: int,
    client: NbaStatsClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Return per-metric percentile breakpoints across all NBA players for ``season``."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_league_percentiles_cache_minutes)

    cached = (
        db.query(NbaLeaguePercentilesCache)
        .filter(NbaLeaguePercentilesCache.season == season, NbaLeaguePercentilesCache.metric_key == "advanced")
        .one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if not allow_network or not settings.advanced_stats_enabled or nba_circuit_breaker_open(db, now=moment) or nba_daily_cap_reached(db):
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    nba_client = client or NbaStatsClient()
    try:
        raw = nba_client.fetch_league_player_advanced(season)
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001
        _record_nba_failure(db)
        logger.warning("NBA Stats league percentiles fetch failed for season %d: %s", season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    rows = parse_result_set(raw)
    metric_columns = {
        "ts_pct": "TS_PCT",
        "efg_pct": "EFG_PCT",
        "usg_pct": "USG_PCT",
        "off_rating": "OFF_RATING",
        "def_rating": "DEF_RATING",
        "net_rating": "NET_RATING",
        "pie": "PIE",
        "pace": "PACE",
    }
    distributions: dict[str, list[float]] = {key: [] for key in metric_columns}
    for row in rows:
        for snake, raw_col in metric_columns.items():
            value = _safe_float(row.get(raw_col))
            if value is not None:
                distributions[snake].append(value)

    percentiles_at = (10, 25, 50, 75, 90)
    breakpoints: dict[str, dict[str, float]] = {}
    for metric, values in distributions.items():
        if not values:
            continue
        sorted_values = sorted(values)
        n = len(sorted_values)
        breakpoints[metric] = {
            f"p{p}": sorted_values[min(int(round(p / 100 * (n - 1))), n - 1)] for p in percentiles_at
        }

    structured = {"breakpoints": breakpoints, "sample_size": len(rows)}

    if cached is None:
        db.add(
            NbaLeaguePercentilesCache(
                season=season,
                metric_key="advanced",
                payload=structured,
                cached_at=moment,
                expires_at=moment + ttl,
            )
        )
    else:
        cached.payload = structured
        cached.cached_at = moment
        cached.expires_at = moment + ttl
    db.flush()
    return AdvancedLoadResult(payload=structured, cache_status="miss", complete=True)


# -----------------------------------------------------------------------------
# Eager warm-up — driven by refresh_jobs

@dataclass(slots=True)
class WarmAdvancedStatsSummary:
    nba_players_attempted: int = 0
    nba_players_succeeded: int = 0
    nba_players_skipped: int = 0
    nba_team_loaded: bool = False
    nba_percentiles_loaded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "nba_players_attempted": self.nba_players_attempted,
            "nba_players_succeeded": self.nba_players_succeeded,
            "nba_players_skipped": self.nba_players_skipped,
            "nba_team_loaded": self.nba_team_loaded,
            "nba_percentiles_loaded": self.nba_percentiles_loaded,
        }


def warm_nba_advanced_for_athletes(
    db: Session,
    *,
    nba_stats_player_ids: Iterable[str],
    season: int,
    client: NbaStatsClient | None = None,
) -> WarmAdvancedStatsSummary:
    """Refresh advanced caches for the given player IDs and league/team rollups."""
    summary = WarmAdvancedStatsSummary()
    nba_client = client or NbaStatsClient()

    team_result = load_nba_team_advanced(db, season=season, client=nba_client, allow_network=True)
    summary.nba_team_loaded = team_result.complete

    percentiles_result = load_nba_league_percentiles(db, season=season, client=nba_client, allow_network=True)
    summary.nba_percentiles_loaded = percentiles_result.complete

    seen: set[str] = set()
    for raw_id in nba_stats_player_ids:
        if raw_id is None:
            continue
        player_id = str(raw_id)
        if player_id in seen:
            continue
        seen.add(player_id)
        summary.nba_players_attempted += 1
        result = load_nba_advanced(
            db, nba_stats_player_id=player_id, season=season, client=nba_client, allow_network=True
        )
        if result.complete and result.cache_status in {"hit", "miss"}:
            summary.nba_players_succeeded += 1
        else:
            summary.nba_players_skipped += 1
    return summary
