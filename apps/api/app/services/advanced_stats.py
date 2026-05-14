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
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.clients.nba_stats import (
    NbaClientLike,
    make_nba_client,
    parse_result_set,
)
from app.config import get_settings
from app.models import (
    EspnPlayerSearchCache,
    NbaAdvancedGamelogCache,
    NbaBoxscoreAdvancedCache,
    NbaLeaguePercentilesCache,
    NbaLineupAdvancedCache,
    NbaPlayerRosterCache,
    NbaTeamAdvancedCache,
    NbaTeamGamelogCache,
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


def _record_nba_failure(db: Session, *, error: str | None = None) -> None:
    payload = _operator_get(db, _NBA_CONSECUTIVE_FAIL_KEY) or {}
    count = int(payload.get("count", 0)) + 1
    _operator_set(db, _NBA_CONSECUTIVE_FAIL_KEY, {"count": count})
    if count >= _NBA_CONSECUTIVE_FAIL_THRESHOLD:
        until = utcnow() + timedelta(hours=_NBA_DISABLE_HOURS)
        _operator_set(db, _NBA_CIRCUIT_KEY, {"until": until.isoformat(), "tripped_at": utcnow().isoformat()})
        logger.warning("NBA Stats circuit breaker tripped until %s", until.isoformat())
    # Smarter #23 — surface the failure on the per-upstream health
    # board. The circuit-breaker counter above tells us whether to skip
    # network on the NEXT call; this tells operators whether the source
    # is currently fresh.
    from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415 — avoid circular import
    record_upstream_failure(db, "nba_stats", error or "unknown error")


def _record_nba_success(db: Session) -> None:
    if _operator_get(db, _NBA_CONSECUTIVE_FAIL_KEY):
        _operator_set(db, _NBA_CONSECUTIVE_FAIL_KEY, {"count": 0})
    if _operator_get(db, _NBA_CIRCUIT_KEY):
        _operator_set(db, _NBA_CIRCUIT_KEY, {})
    # Smarter #23 — clear the last_error and stamp last_success_at.
    from app.services.upstream_health import record_upstream_success  # noqa: PLC0415
    record_upstream_success(db, "nba_stats")


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
    client: NbaClientLike | None = None,
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

    nba_client = client or make_nba_client()
    try:
        raw = nba_client.fetch_player_advanced_gamelog(nba_stats_player_id, season)
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001 — broad on purpose, network is unpredictable
        _record_nba_failure(db, error=str(exc))
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
    client: NbaClientLike | None = None,
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

    nba_client = client or make_nba_client()
    try:
        raw = nba_client.fetch_team_advanced(season)
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001
        _record_nba_failure(db, error=str(exc))
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
    client: NbaClientLike | None = None,
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

    nba_client = client or make_nba_client()
    try:
        raw = nba_client.fetch_league_player_advanced(season)
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001
        _record_nba_failure(db, error=str(exc))
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
# Per-game team advanced (NEW) — opponent recent form, last-5/10 rolling

def load_nba_team_gamelog(
    db: Session,
    *,
    team_id: str,
    season: int,
    client: NbaClientLike | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Per-game Advanced log for one team. Returns rolling-window aggregates."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_team_gamelog_cache_minutes)

    cached = (
        db.query(NbaTeamGamelogCache)
        .filter(NbaTeamGamelogCache.team_id == str(team_id), NbaTeamGamelogCache.season == season)
        .one_or_none()
    )
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

    nba_client = client or make_nba_client()
    try:
        raw = nba_client.fetch_team_advanced_gamelog(team_id, season)
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001
        _record_nba_failure(db, error=str(exc))
        logger.warning("NBA team gamelog fetch failed for team %s season %d: %s", team_id, season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    rows = parse_result_set(raw, name="TeamGameLogs")
    games = [
        {
            "game_id": str(row.get("GAME_ID") or ""),
            "game_date": row.get("GAME_DATE"),
            "matchup": row.get("MATCHUP"),
            "off_rating": _safe_float(row.get("OFF_RATING")),
            "def_rating": _safe_float(row.get("DEF_RATING")),
            "net_rating": _safe_float(row.get("NET_RATING")),
            "pace": _safe_float(row.get("PACE")),
            "ts_pct": _safe_float(row.get("TS_PCT")),
            "efg_pct": _safe_float(row.get("EFG_PCT")),
            "ast_pct": _safe_float(row.get("AST_PCT")),
            "oreb_pct": _safe_float(row.get("OREB_PCT")),
            "dreb_pct": _safe_float(row.get("DREB_PCT")),
            "tm_tov_pct": _safe_float(row.get("TM_TOV_PCT")),
        }
        for row in rows
    ]
    metric_keys = ("off_rating", "def_rating", "net_rating", "pace", "ts_pct", "efg_pct",
                   "ast_pct", "oreb_pct", "dreb_pct", "tm_tov_pct")
    structured = {
        "games_played": len(games),
        "season_avg": _aggregate(games, metric_keys),
        "recent_5_avg": _aggregate(games[:5], metric_keys),
        "recent_10_avg": _aggregate(games[:10], metric_keys),
        "recent_games": games[:10],
    }

    if cached is None:
        db.add(
            NbaTeamGamelogCache(
                team_id=str(team_id),
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


def find_nba_team_id_by_name(db: Session, *, team_name: str, season: int) -> str | None:
    """Look up an NBA Stats TEAM_ID from the cached season-aggregate payload.

    Uses ``NbaTeamAdvancedCache`` (populated by warm-up) to map display names
    like "Los Angeles Lakers" → ``"1610612747"``. Falls back to ``None`` when
    the cache is empty or no fuzzy match is found.
    """
    if not team_name:
        return None
    cached = (
        db.query(NbaTeamAdvancedCache)
        .filter(NbaTeamAdvancedCache.team_id == "ALL", NbaTeamAdvancedCache.season == season)
        .one_or_none()
    )
    if cached is None:
        return None
    payload = cached.payload or {}
    target = _normalize_name(team_name)
    for team_id, info in (payload.get("teams") or {}).items():
        if _normalize_name(info.get("team_name")) == target:
            return str(team_id)
    return None


def emit_nba_opponent_team_features(payload: dict[str, Any] | None) -> dict[str, float]:
    """Emit opponent recent-form features from a team's per-game cached payload."""
    if not payload:
        return {}
    season_avg = payload.get("season_avg") or {}
    recent_5 = payload.get("recent_5_avg") or {}

    out: dict[str, float] = {}

    def _set(key: str, value: Any) -> None:
        if isinstance(value, (int, float)):
            out[key] = round(float(value), 4)

    _set("opponent_off_rating_recent_5", recent_5.get("off_rating"))
    _set("opponent_def_rating_recent_5", recent_5.get("def_rating"))
    _set("opponent_net_rating_recent_5", recent_5.get("net_rating"))
    _set("opponent_pace_recent_5", recent_5.get("pace"))
    # Season-level pace is consumed both by the heuristic_factors fallback
    # in _nba_pace_factor_advanced and by the proxy-suppression gate in
    # scoring.py — emit it so those paths actually fire when the recent-5
    # pace is missing (early season, partial cache).
    _set("opponent_pace_season", season_avg.get("pace"))

    if (
        isinstance(recent_5.get("off_rating"), (int, float))
        and isinstance(season_avg.get("off_rating"), (int, float))
    ):
        out["opponent_form_delta_off"] = round(recent_5["off_rating"] - season_avg["off_rating"], 4)
    if (
        isinstance(recent_5.get("def_rating"), (int, float))
        and isinstance(season_avg.get("def_rating"), (int, float))
    ):
        out["opponent_form_delta_def"] = round(recent_5["def_rating"] - season_avg["def_rating"], 4)
    if (
        isinstance(recent_5.get("pace"), (int, float))
        and isinstance(season_avg.get("pace"), (int, float))
    ):
        out["opponent_form_delta_pace"] = round(recent_5["pace"] - season_avg["pace"], 4)
    if out:
        out["opponent_team_data_complete"] = 1.0
    return out


# -----------------------------------------------------------------------------
# Smarter #11 — per-player workload features from the ESPN game log


def emit_nba_workload_features(
    game_logs: list[dict[str, Any]] | None,
    *,
    window_games: int = 5,
) -> dict[str, float]:
    """Emit per-player workload features for Smarter #11.

    Reads the most recent ``window_games`` from the in-memory NBA game-log
    list (sorted reverse-chronologically by ``stats_query._build_nba_game_logs``)
    and emits:

    * ``recent_workload_minutes_per_game`` — mean MIN over the recent
      window, restricted to games where the player actually saw the floor
      (``minutes > 0``). DNP rows (``minutes == 0``) are intentionally
      excluded so a rest day doesn't deflate the workload signal.
    * ``consecutive_games_played`` — count of consecutive most-recent games
      with positive minutes. DNP / DNP-CD breaks the streak. INACTIVE
      games are absent from the ESPN payload entirely and therefore do
      not appear here — one-night rest is the signal we want.
    * ``workload_data_complete`` — 1.0 when at least one game in the
      window has playable minutes; absent otherwise.

    Returns ``{}`` on empty / missing input so callers can treat absent
    keys as "no workload signal" without sentinel handling.
    """
    if not game_logs:
        return {}

    recent = game_logs[:window_games]
    minutes_played: list[float] = []
    for entry in recent:
        metrics = entry.get("metrics") or {}
        minutes = metrics.get("minutes")
        if isinstance(minutes, (int, float)) and minutes > 0:
            minutes_played.append(float(minutes))

    if not minutes_played:
        return {}

    consecutive = 0
    for entry in game_logs:
        metrics = entry.get("metrics") or {}
        minutes = metrics.get("minutes")
        if isinstance(minutes, (int, float)) and minutes > 0:
            consecutive += 1
        else:
            break

    return {
        "recent_workload_minutes_per_game": round(
            sum(minutes_played) / len(minutes_played), 1
        ),
        "consecutive_games_played": float(consecutive),
        "workload_data_complete": 1.0,
    }


# -----------------------------------------------------------------------------
# Smarter #12 — usage × pace × (1 / opponent_DRtg) interaction term.
#
# The point of this emitter is that the heuristic scoring already applies
# each component independently (capped at ±15%) — that understates the
# extreme combinations (top-quartile usage AGAINST a fast bad-defense
# opponent). The product is emitted UNCAPPED so the ML model captures the
# shape; the heuristic factors continue to apply their per-component caps.


def emit_nba_interaction_term(
    *,
    usage_pct: float | None,
    opponent_pace: float | None,
    opponent_drtg: float | None,
) -> dict[str, float]:
    """Emit the NBA offense × pace × defense interaction term.

    Centered so league-average inputs produce ~1.0:
      * usage_pct ~0.25 (rotation regular, decimal scale)
      * opponent_pace ~100
      * opponent_drtg ~110 (lower = better defense, suppresses output)

    Formula:
        (usage / 0.25) * (pace / 100) * (drtg / 110)

    Direction matches the existing ``_nba_opp_def_factor`` convention:
    a low DRtg (e.g. 100) yields ``drtg / 110 ≈ 0.91``, suppressing the
    term against an elite defense; a high DRtg (e.g. 120) yields
    ``drtg / 110 ≈ 1.09``, boosting the term against a weak defense.
    Note: the original handoff pseudocode for Smarter #12 had this
    inverted (``110 / drtg``) — corrected here so the interaction term
    moves the same direction as the heuristic component factors.

    The product is emitted UNCAPPED — the existing heuristic factors
    already cap each component independently; the interaction term feeds
    the ML model so it can learn the multiplicative shape directly. No
    heuristic factor consumes this key.

    Returns ``{}`` when any input is missing or ``opponent_drtg <= 0``.
    """
    # ``bool`` is a subclass of ``int`` in Python — reject explicitly so
    # a stray ``True`` for usage doesn't expand to 1.0 (a 400% multiplier
    # of league-average usage), producing a wildly wrong interaction term.
    candidates = (usage_pct, opponent_pace, opponent_drtg)
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in candidates
    ):
        return {}
    if opponent_drtg <= 0:
        return {}
    return {
        "nba_offense_interaction_term": round(
            (usage_pct / 0.25) * (opponent_pace / 100.0) * (opponent_drtg / 110.0),
            4,
        ),
    }


# -----------------------------------------------------------------------------
# Smarter #17 — late-breaking injury news.
#
# ESPN's injury report updates faster than the gamelog: a star ruled
# OUT 60 minutes before tip should auto-suppress every prop on them,
# not penalize with the usual 0.025 missing-context nudge. This module
# ships the CONSUMER-SIDE mechanism — the emitter that turns an injury
# payload into scoring features, plus the suppression gate in the
# scoring kernel. The actual NBA-injury-report LOADER (HTTP fetch
# from espn.com/injuries, cache write to ``NbaInjuryReportCache``)
# is a separate follow-up PR — the model + config knob + TTL helper
# already exist from Smarter #29.
#
# Expected ``injury_payload`` shape (contract for the future loader):
# ::
#     {
#         "report_updated_at": "2026-05-14T18:00:00+00:00",
#         "players": {
#             "<player_name>": {
#                 "status": "out" | "doubtful" | "questionable" | ...,
#                 "designation": "left knee soreness",
#             },
#         },
#     }


_INJURY_FRESHNESS_WINDOW = timedelta(hours=12)


def _normalize_injury_status(raw: Any) -> str:
    """Map ESPN-style status strings to a small canonical vocabulary.

    ESPN sends variants like ``"Out"`` / ``"Out (illness)"`` / ``"Out
    for season"`` — match by leading-word so the policy holds across
    those formats. Substring matches were tried first but had a
    false-positive risk: ``"workout status"`` would have collided
    with ``out``. Leading-word matching catches the canonical set and
    rejects unrelated strings.
    """
    if not isinstance(raw, str):
        return ""
    normalized = raw.strip().lower().replace("-", " ")
    if normalized.startswith("out"):
        return "out"
    if normalized.startswith("doubtful"):
        return "doubtful"
    if normalized.startswith("questionable"):
        return "questionable"
    if normalized.startswith("probable"):
        return "probable"
    if "day to day" in normalized:
        # ESPN sometimes reports "day-to-day" — treat as questionable.
        return "questionable"
    return ""


def emit_nba_injury_features(
    injury_payload: dict[str, Any] | None,
    *,
    player_name: str | None,
    now: datetime | None = None,
) -> dict[str, float]:
    """Emit injury-status + freshness features for a single NBA player.

    The scoring kernel suppresses NBA props when the report is FRESH
    (updated inside ``_INJURY_FRESHNESS_WINDOW``) AND the status is
    ``out`` or ``doubtful``. A stale report still emits the status flag
    but the suppression gate requires freshness — operators decide
    whether to act on stale signals via the usual missing-context path.

    Returns ``{}`` when payload is missing, the player has no entry,
    or the status is unrecognized.
    """
    if not injury_payload or not isinstance(player_name, str) or not player_name.strip():
        return {}
    players = injury_payload.get("players") or {}
    record = players.get(player_name) or players.get(player_name.strip())
    if not isinstance(record, dict):
        return {}
    status = _normalize_injury_status(record.get("status"))
    if not status:
        return {}
    out: dict[str, float] = {
        "player_injury_status_out": 1.0 if status == "out" else 0.0,
        "player_injury_status_doubtful": 1.0 if status == "doubtful" else 0.0,
        "player_injury_status_questionable": 1.0 if status == "questionable" else 0.0,
        "injury_data_complete": 1.0,
    }
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    report_updated_at = injury_payload.get("report_updated_at")
    if isinstance(report_updated_at, str):
        try:
            updated = datetime.fromisoformat(report_updated_at.replace("Z", "+00:00"))
        except ValueError:
            updated = None
    elif isinstance(report_updated_at, datetime):
        updated = report_updated_at
    else:
        updated = None
    if updated is not None:
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        is_fresh = (moment - updated) <= _INJURY_FRESHNESS_WINDOW and (moment - updated).total_seconds() >= 0
        out["injury_report_is_fresh"] = 1.0 if is_fresh else 0.0
    else:
        out["injury_report_is_fresh"] = 0.0
    return out


# -----------------------------------------------------------------------------
# Lineup-level advanced (NEW) — 5-man combinations

def load_nba_lineup_advanced(
    db: Session,
    *,
    season: int,
    group_quantity: int = 5,
    client: NbaClientLike | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """League-wide 5-man lineup Advanced data."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_lineup_advanced_cache_minutes)

    cached = (
        db.query(NbaLineupAdvancedCache)
        .filter(
            NbaLineupAdvancedCache.season == season,
            NbaLineupAdvancedCache.group_quantity == group_quantity,
        )
        .one_or_none()
    )
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

    nba_client = client or make_nba_client()
    try:
        raw = nba_client.fetch_lineup_advanced(season, group_quantity=group_quantity)
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001
        _record_nba_failure(db, error=str(exc))
        logger.warning("NBA lineup advanced fetch failed for season %d: %s", season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    rows = parse_result_set(raw)
    lineups = [
        {
            "group_id": row.get("GROUP_ID"),
            "group_name": row.get("GROUP_NAME"),
            "team_id": str(row.get("TEAM_ID") or ""),
            "min": _safe_float(row.get("MIN")),
            "off_rating": _safe_float(row.get("OFF_RATING")),
            "def_rating": _safe_float(row.get("DEF_RATING")),
            "net_rating": _safe_float(row.get("NET_RATING")),
            "pace": _safe_float(row.get("PACE")),
            "ts_pct": _safe_float(row.get("TS_PCT")),
        }
        for row in rows
    ]
    structured = {"lineups": lineups, "sample_size": len(lineups)}

    if cached is None:
        db.add(
            NbaLineupAdvancedCache(
                season=season,
                group_quantity=group_quantity,
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
# Player-ID resolution: ESPN athlete_id → NBA Stats PERSON_ID

def _normalize_name(name: str | None) -> str:
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.lower() if ch.isalnum())


def load_nba_player_roster(
    db: Session,
    *,
    season: int,
    client: NbaClientLike | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Daily snapshot of NBA Stats commonallplayers — feeds player-ID resolution."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nba_player_roster_cache_minutes)

    cached = (
        db.query(NbaPlayerRosterCache).filter(NbaPlayerRosterCache.season == season).one_or_none()
    )
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

    nba_client = client or make_nba_client()
    try:
        raw = nba_client.fetch_common_all_players(season)
        _increment_daily_count(db)
        _record_nba_success(db)
    except Exception as exc:  # noqa: BLE001
        _record_nba_failure(db, error=str(exc))
        logger.warning("NBA commonallplayers fetch failed for season %d: %s", season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    rows = parse_result_set(raw)
    players = [
        {
            "person_id": str(row.get("PERSON_ID") or ""),
            "display_name": row.get("DISPLAY_FIRST_LAST") or "",
            "team_id": str(row.get("TEAM_ID") or ""),
            "team_abbreviation": (row.get("TEAM_ABBREVIATION") or "").upper(),
            "roster_status": _safe_float(row.get("ROSTERSTATUS")),
        }
        for row in rows
        if row.get("PERSON_ID") is not None
    ]
    structured = {"players": players, "fetched_at": moment.isoformat()}

    if cached is None:
        db.add(
            NbaPlayerRosterCache(
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


def resolve_nba_stats_player_id(
    db: Session,
    *,
    espn_athlete_id: str | None,
    full_name: str,
    team_abbreviation: str | None = None,
    season: int,
    client: NbaClientLike | None = None,
    allow_network: bool = False,
) -> str | None:
    """Find an NBA Stats PERSON_ID for an ESPN-known player.

    Order of attempts:
      1. Cached on ``EspnPlayerSearchCache.payload["nba_stats_id"]`` (O(1)).
      2. Roster lookup via ``NbaPlayerRosterCache`` matched on
         (normalized full_name, team_abbreviation). Falls through to a
         name-only match if no team match.
    """
    if not full_name:
        return None

    # Step 1: cached on search row
    if espn_athlete_id:
        search_rows = (
            db.query(EspnPlayerSearchCache)
            .filter(EspnPlayerSearchCache.sport_key == "NBA")
            .all()
        )
        for entry in search_rows:
            payload = entry.payload or {}
            if str(payload.get("athlete_id")) == str(espn_athlete_id):
                stats_id = payload.get("nba_stats_id")
                if stats_id:
                    return str(stats_id)

    # Step 2: roster lookup
    roster_result = load_nba_player_roster(db, season=season, client=client, allow_network=allow_network)
    players = (roster_result.payload or {}).get("players") or []
    if not players:
        return None

    target = _normalize_name(full_name)
    team_target = (team_abbreviation or "").upper()

    exact_team_match: str | None = None
    name_only_match: str | None = None
    for player in players:
        if _normalize_name(player.get("display_name")) != target:
            continue
        if team_target and player.get("team_abbreviation") == team_target:
            exact_team_match = player.get("person_id")
            break
        if name_only_match is None:
            name_only_match = player.get("person_id")

    resolved = exact_team_match or name_only_match
    if resolved and espn_athlete_id:
        # Persist mapping back to search-cache row so future lookups are O(1)
        for entry in (
            db.query(EspnPlayerSearchCache)
            .filter(EspnPlayerSearchCache.sport_key == "NBA")
            .all()
        ):
            payload = dict(entry.payload or {})
            if str(payload.get("athlete_id")) == str(espn_athlete_id):
                payload["nba_stats_id"] = str(resolved)
                entry.payload = payload
                db.flush()
                break
    return resolved


# -----------------------------------------------------------------------------
# Eager warm-up — driven by refresh_jobs

@dataclass(slots=True)
class WarmAdvancedStatsSummary:
    nba_players_attempted: int = 0
    nba_players_succeeded: int = 0
    nba_players_skipped: int = 0
    nba_team_loaded: bool = False
    nba_team_gamelogs_loaded: int = 0
    nba_lineup_loaded: bool = False
    nba_roster_loaded: bool = False
    nba_percentiles_loaded: bool = False
    nba_hustle_loaded: bool = False
    nba_drives_loaded: bool = False
    nba_clutch_loaded: bool = False
    nba_defense_loaded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "nba_players_attempted": self.nba_players_attempted,
            "nba_players_succeeded": self.nba_players_succeeded,
            "nba_players_skipped": self.nba_players_skipped,
            "nba_team_loaded": self.nba_team_loaded,
            "nba_team_gamelogs_loaded": self.nba_team_gamelogs_loaded,
            "nba_lineup_loaded": self.nba_lineup_loaded,
            "nba_roster_loaded": self.nba_roster_loaded,
            "nba_percentiles_loaded": self.nba_percentiles_loaded,
            "nba_hustle_loaded": self.nba_hustle_loaded,
            "nba_drives_loaded": self.nba_drives_loaded,
            "nba_clutch_loaded": self.nba_clutch_loaded,
            "nba_defense_loaded": self.nba_defense_loaded,
        }


def warm_nba_advanced_for_athletes(
    db: Session,
    *,
    nba_stats_player_ids: Iterable[str],
    season: int,
    nba_team_ids: Iterable[str] | None = None,
    client: NbaClientLike | None = None,
) -> WarmAdvancedStatsSummary:
    """Refresh advanced caches for the given player + team IDs, plus league rollups."""
    from time import perf_counter

    summary = WarmAdvancedStatsSummary()
    nba_client = client or make_nba_client()
    started = perf_counter()
    logger.info(
        "refresh_job_phase",
        extra={
            "kind": "advanced_stats_warm",
            "phase": "nba_warm_started",
            "season": season,
            "client": type(nba_client).__name__,
        },
    )

    team_result = load_nba_team_advanced(db, season=season, client=nba_client, allow_network=True)
    summary.nba_team_loaded = team_result.complete

    percentiles_result = load_nba_league_percentiles(db, season=season, client=nba_client, allow_network=True)
    summary.nba_percentiles_loaded = percentiles_result.complete

    lineup_result = load_nba_lineup_advanced(db, season=season, client=nba_client, allow_network=True)
    summary.nba_lineup_loaded = lineup_result.complete

    roster_result = load_nba_player_roster(db, season=season, client=nba_client, allow_network=True)
    summary.nba_roster_loaded = roster_result.complete

    # Long-tail leaderboards — hustle / drives tracking / clutch / defense.
    # Each is a single league-wide call so they're cheap to refresh.
    from app.services.nba_long_tail import (
        load_nba_clutch_player,
        load_nba_hustle_player,
        load_nba_player_defense,
        load_nba_tracking,
    )

    summary.nba_hustle_loaded = load_nba_hustle_player(
        db, season=season, client=nba_client, allow_network=True
    ).complete
    summary.nba_drives_loaded = load_nba_tracking(
        db, season=season, pt_measure_type="Drives", client=nba_client, allow_network=True
    ).complete
    summary.nba_clutch_loaded = load_nba_clutch_player(
        db, season=season, client=nba_client, allow_network=True
    ).complete
    summary.nba_defense_loaded = load_nba_player_defense(
        db, season=season, defense_category="Overall", client=nba_client, allow_network=True
    ).complete

    # Derive team-id list when not explicitly supplied: pull all 30 from the
    # team-advanced cache populated above. Saves callers from having to know
    # NBA Stats team IDs ahead of time.
    team_ids_to_warm: list[str] = list(nba_team_ids or [])
    if not team_ids_to_warm and team_result.payload:
        team_ids_to_warm = list((team_result.payload.get("teams") or {}).keys())

    seen_teams: set[str] = set()
    for raw_id in team_ids_to_warm:
        if raw_id is None:
            continue
        team_id = str(raw_id)
        if team_id in seen_teams:
            continue
        seen_teams.add(team_id)
        result = load_nba_team_gamelog(db, team_id=team_id, season=season, client=nba_client, allow_network=True)
        if result.complete and result.cache_status in {"hit", "miss"}:
            summary.nba_team_gamelogs_loaded += 1

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
    logger.info(
        "refresh_job_phase",
        extra={
            "kind": "advanced_stats_warm",
            "phase": "nba_warm_completed",
            "season": season,
            "elapsed_seconds": round(perf_counter() - started, 3),
            "players_attempted": summary.nba_players_attempted,
            "players_succeeded": summary.nba_players_succeeded,
            "team_gamelogs_loaded": summary.nba_team_gamelogs_loaded,
        },
    )
    return summary
