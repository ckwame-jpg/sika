"""NFL advanced-stats orchestrator (Smarter NFL PR 3).

Mirrors the MLB pattern in :mod:`app.services.mlb_advanced`, adapted to
NFL's weekly cadence and nflverse's bulk-file distribution model:

- The daily ``nfl_data_refresh`` job calls :func:`refresh_nfl_data`,
  which downloads the nflverse release CSVs once and upserts every NFL
  cache table (weekly stats, snap counts, depth charts, official
  injuries, team ratings, schedule) plus prewarms game-time weather for
  events inside the 36-hour window.
- Read-side ``load_nfl_*`` helpers are cache-only (no network) — unlike
  the MLB fetch-on-demand loaders, because one player's stale row can't
  be refreshed without downloading the whole league file anyway. Stale
  rows are served with ``cache_status="stale"`` so the freshness layer
  can penalize; for a weekly sport, yesterday's snapshot usually still
  describes last week's game.

Weather reuses :class:`app.clients.weather.WeatherClient` with stadium
coordinates from ``app/data/nfl_stadiums.json``; domes and closed-roof
buildings short-circuit to fixed indoor values exactly like MLB.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.clients.espn import ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME
from app.clients.nflverse import NflverseClient
from app.clients.weather import WeatherClient
from app.config import get_settings
from app.models import (
    Event,
    EventParticipant,
    NflDepthChartCache,
    NflOfficialInjuryCache,
    NflScheduleCache,
    NflSnapCountsCache,
    NflTeamRatingCache,
    NflWeatherCache,
    NflWeeklyStatsCache,
    utcnow,
)
from app.services.advanced_stats import AdvancedLoadResult, _coerce_utc, _safe_float


logger = logging.getLogger(__name__)

WEATHER_PREWARM_WINDOW_HOURS = 36


def _record_nflverse_success(db: Session) -> None:
    from app.services.upstream_health import record_upstream_success  # noqa: PLC0415 — avoid circular import
    record_upstream_success(db, "nflverse")


def _record_nflverse_failure(db: Session, *, error: str) -> None:
    from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
    record_upstream_failure(db, "nflverse", error or "unknown error")


# -----------------------------------------------------------------------------
# Stadium data (static file, PR 1)

_STADIUMS_CACHE: dict[str, dict[str, Any]] | None = None


def _load_stadiums_file() -> dict[str, dict[str, Any]]:
    global _STADIUMS_CACHE
    if _STADIUMS_CACHE is None:
        data_path = Path(__file__).resolve().parents[1] / "data" / "nfl_stadiums.json"
        data = json.loads(data_path.read_text())
        _STADIUMS_CACHE = {k: v for k, v in data.items() if not k.startswith("_")}
    return _STADIUMS_CACHE


def nfl_stadium_info(team_abbr: str | None) -> dict[str, Any] | None:
    """Stadium record (name, lat/lon, roof, surface) for a home team."""
    if not team_abbr:
        return None
    return _load_stadiums_file().get(str(team_abbr).strip().upper())


_TEAM_NAME_TO_ABBR: dict[str, str] | None = None


def nfl_team_abbr_for_name(display_name: str | None) -> str | None:
    """Reverse-map an ESPN display name ("Kansas City Chiefs") — or a
    distinctive fragment of one ("Chiefs") — to the canonical
    abbreviation used by the stadium file and nflverse."""
    global _TEAM_NAME_TO_ABBR
    if _TEAM_NAME_TO_ABBR is None:
        mapping: dict[str, str] = {}
        stadium_keys = set(_load_stadiums_file().keys())
        for abbr, name in ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME.get("NFL", {}).items():
            # Alias abbreviations (JAC/WAS/LA) collapse onto the canonical
            # key — the one that exists in the stadium file.
            if abbr in stadium_keys:
                mapping[name.lower()] = abbr
        _TEAM_NAME_TO_ABBR = mapping
    if not display_name:
        return None
    normalized = str(display_name).strip().lower()
    direct = _TEAM_NAME_TO_ABBR.get(normalized)
    if direct:
        return direct
    for name, abbr in _TEAM_NAME_TO_ABBR.items():
        if normalized in name or name in normalized:
            return abbr
    return None


# nflverse team codes vs sika's canonical (ESPN-aligned) abbreviations.
# Live inventory (2026-07-09): nflverse uses ``LA`` for the Rams and
# ``WAS`` for Washington; the historical codes appear in games.csv rows
# before each franchise's relocation (needed by the margin-distribution
# build in Smarter NFL PR 5, which reads 2015+ seasons).
_NFLVERSE_TO_CANONICAL_TEAM = {
    "LA": "LAR",
    "WAS": "WSH",
    "JAC": "JAX",
    "STL": "LAR",  # Rams pre-2016
    "SD": "LAC",   # Chargers pre-2017
    "OAK": "LV",   # Raiders pre-2020
}


def normalize_nfl_team_code(code: str | None) -> str | None:
    """Map an nflverse team code onto sika's canonical abbreviation
    (the key set of ``nfl_stadiums.json`` / the ESPN NFL team map)."""
    if not code:
        return None
    normalized = str(code).strip().upper()
    if not normalized:
        return None
    return _NFLVERSE_TO_CANONICAL_TEAM.get(normalized, normalized)


def _normalize_player_name(name: str | None) -> str:
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.lower() if ch.isalnum())


# -----------------------------------------------------------------------------
# Generic cache upsert/read helpers

def _upsert_cache_row(
    db: Session,
    model: type,
    filters: dict[str, Any],
    payload: dict[str, Any],
    *,
    ttl: timedelta,
    now: datetime,
) -> None:
    query = db.query(model)
    for column, value in filters.items():
        query = query.filter(getattr(model, column) == value)
    row = query.one_or_none()
    if row is None:
        row = model(**filters, payload=payload, cached_at=now, expires_at=now + ttl)
        db.add(row)
    else:
        row.payload = payload
        row.cached_at = now
        row.expires_at = now + ttl
    db.flush()


def _read_cache_row(
    db: Session,
    model: type,
    filters: dict[str, Any],
    *,
    now: datetime,
) -> AdvancedLoadResult:
    query = db.query(model)
    for column, value in filters.items():
        query = query.filter(getattr(model, column) == value)
    row = query.one_or_none()
    if row is None:
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)
    fresh = (_coerce_utc(row.expires_at) or now) > now
    return AdvancedLoadResult(
        payload=dict(row.payload or {}),
        cache_status="hit" if fresh else "stale",
        complete=True,
        cached_at=_coerce_utc(row.cached_at),
    )


# -----------------------------------------------------------------------------
# Team ratings from nflverse team-week stats + schedule results

def _row_float(row: dict[str, Any], key: str) -> float:
    value = _safe_float(row.get(key))
    return value if value is not None else 0.0


def compute_nfl_team_ratings(
    team_week_rows: list[dict[str, Any]],
    games_rows: list[dict[str, Any]],
    *,
    season: int,
) -> dict[str, Any]:
    """Season-to-date team ratings from nflverse data.

    Offense: EPA/play over the team's own offensive plays
    (``attempts + carries + sacks_suffered``). Defense: derived by
    crediting each offense row AGAINST the ``opponent_team`` — a team's
    defensive EPA/play allowed is the aggregate of its opponents'
    offensive EPA in shared games. Points for/against come from the
    schedule results (they aren't in the team-week file). Regular
    season only — postseason samples are tiny and stylistically skewed.
    """
    offense: dict[str, dict[str, float]] = {}
    defense: dict[str, dict[str, float]] = {}
    through_week = 0
    for row in team_week_rows:
        if str(row.get("season_type") or "").upper() != "REG":
            continue
        team = normalize_nfl_team_code(row.get("team")) or ""
        opponent = normalize_nfl_team_code(row.get("opponent_team")) or ""
        if not team:
            continue
        week = int(_row_float(row, "week"))
        through_week = max(through_week, week)
        epa = _row_float(row, "passing_epa") + _row_float(row, "rushing_epa")
        plays = (
            _row_float(row, "attempts")
            + _row_float(row, "carries")
            + _row_float(row, "sacks_suffered")
        )
        team_off = offense.setdefault(team, {"epa": 0.0, "plays": 0.0, "games": 0.0})
        team_off["epa"] += epa
        team_off["plays"] += plays
        team_off["games"] += 1.0
        if opponent:
            opp_def = defense.setdefault(opponent, {"epa_allowed": 0.0, "plays": 0.0})
            opp_def["epa_allowed"] += epa
            opp_def["plays"] += plays

    points_for: dict[str, float] = {}
    points_against: dict[str, float] = {}
    games_played: dict[str, float] = {}
    for game in games_rows:
        if str(game.get("season") or "") != str(season):
            continue
        if str(game.get("game_type") or "").upper() != "REG":
            continue
        home_score = _safe_float(game.get("home_score"))
        away_score = _safe_float(game.get("away_score"))
        if home_score is None or away_score is None:
            continue  # not played yet
        home = normalize_nfl_team_code(game.get("home_team")) or ""
        away = normalize_nfl_team_code(game.get("away_team")) or ""
        if not home or not away:
            continue
        points_for[home] = points_for.get(home, 0.0) + home_score
        points_against[home] = points_against.get(home, 0.0) + away_score
        points_for[away] = points_for.get(away, 0.0) + away_score
        points_against[away] = points_against.get(away, 0.0) + home_score
        games_played[home] = games_played.get(home, 0.0) + 1.0
        games_played[away] = games_played.get(away, 0.0) + 1.0

    teams: dict[str, dict[str, float]] = {}
    for team in sorted(set(offense) | set(defense) | set(games_played)):
        team_off = offense.get(team, {"epa": 0.0, "plays": 0.0, "games": 0.0})
        team_def = defense.get(team, {"epa_allowed": 0.0, "plays": 0.0})
        games = games_played.get(team, team_off.get("games", 0.0))
        off_epa_per_play = team_off["epa"] / team_off["plays"] if team_off["plays"] > 0 else 0.0
        def_epa_per_play = (
            team_def["epa_allowed"] / team_def["plays"] if team_def["plays"] > 0 else 0.0
        )
        plays_per_game = team_off["plays"] / team_off["games"] if team_off["games"] > 0 else 0.0
        teams[team] = {
            "games": games,
            "off_epa_per_play": round(off_epa_per_play, 5),
            "def_epa_per_play_allowed": round(def_epa_per_play, 5),
            "net_epa_per_play": round(off_epa_per_play - def_epa_per_play, 5),
            "plays_per_game": round(plays_per_game, 2),
            "points_for_per_game": round(points_for.get(team, 0.0) / games, 2) if games > 0 else 0.0,
            "points_against_per_game": round(points_against.get(team, 0.0) / games, 2) if games > 0 else 0.0,
        }
    return {"season": season, "through_week": through_week, "teams": teams}


# -----------------------------------------------------------------------------
# The daily refresh job body

def _group_rows_by_week(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        week_value = _safe_float(row.get("week"))
        if week_value is None:
            continue
        grouped.setdefault(int(week_value), []).append(row)
    return grouped


def refresh_nfl_data(
    db: Session,
    *,
    season: int | None = None,
    client: NflverseClient | None = None,
    weather_client: WeatherClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Download the nflverse bundle for ``season`` and upsert every NFL
    cache table. Called by the ``nfl_data_refresh`` job; also usable
    ad-hoc (tests, backfills). Individual dataset failures degrade
    gracefully — the remaining datasets still refresh, and the failure
    lands on the upstream-health board."""
    from app.services.stats_query import default_season_for_sport  # noqa: PLC0415 — avoid circular import

    settings = get_settings()
    moment = now or utcnow()
    resolved_season = season or default_season_for_sport("NFL")
    nflverse = client or NflverseClient()
    summary: dict[str, Any] = {"season": resolved_season, "errors": []}

    def _run_dataset(name: str, fetch, store) -> None:
        try:
            rows = fetch()
        except Exception as exc:  # noqa: BLE001 — every dataset failure must degrade, not abort
            logger.warning("nflverse %s fetch failed for %s: %s", name, resolved_season, exc)
            summary["errors"].append(f"{name}: {exc}")
            _record_nflverse_failure(db, error=f"{name}: {exc}")
            return
        store(rows)
        _record_nflverse_success(db)

    def _store_weekly_stats(rows: list[dict[str, Any]]) -> None:
        ttl = timedelta(minutes=settings.nfl_weekly_stats_cache_minutes)
        grouped = _group_rows_by_week(rows)
        for week, week_rows in grouped.items():
            _upsert_cache_row(
                db, NflWeeklyStatsCache,
                {"season": resolved_season, "week": week},
                {"rows": week_rows}, ttl=ttl, now=moment,
            )
        summary["weekly_stats_weeks"] = len(grouped)

    def _store_snap_counts(rows: list[dict[str, Any]]) -> None:
        ttl = timedelta(minutes=settings.nfl_snap_counts_cache_minutes)
        grouped = _group_rows_by_week(rows)
        for week, week_rows in grouped.items():
            _upsert_cache_row(
                db, NflSnapCountsCache,
                {"season": resolved_season, "week": week},
                {"rows": week_rows}, ttl=ttl, now=moment,
            )
        summary["snap_count_weeks"] = len(grouped)

    def _store_depth_charts(rows: list[dict[str, Any]]) -> None:
        ttl = timedelta(minutes=settings.nfl_depth_chart_cache_minutes)
        by_team: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            team = str(row.get("team") or "").strip()
            if team:
                by_team.setdefault(team, []).append(row)
        for team, team_rows in by_team.items():
            _upsert_cache_row(
                db, NflDepthChartCache,
                {"season": resolved_season, "team": team},
                {"rows": team_rows}, ttl=ttl, now=moment,
            )
        summary["depth_chart_teams"] = len(by_team)

    def _store_official_injuries(rows: list[dict[str, Any]]) -> None:
        ttl = timedelta(minutes=settings.nfl_injury_report_cache_minutes)
        grouped = _group_rows_by_week(rows)
        for week, week_rows in grouped.items():
            _upsert_cache_row(
                db, NflOfficialInjuryCache,
                {"season": resolved_season, "week": week},
                {"rows": week_rows}, ttl=ttl, now=moment,
            )
        summary["official_injury_weeks"] = len(grouped)

    _run_dataset("weekly_stats", lambda: nflverse.fetch_weekly_player_stats(resolved_season), _store_weekly_stats)
    _run_dataset("snap_counts", lambda: nflverse.fetch_snap_counts(resolved_season), _store_snap_counts)
    _run_dataset("depth_charts", lambda: nflverse.fetch_latest_depth_charts(resolved_season), _store_depth_charts)
    _run_dataset("official_injuries", lambda: nflverse.fetch_official_injuries(resolved_season), _store_official_injuries)

    # Team ratings + schedule need two datasets together; fetch games
    # for the current AND prior season (prior-season ratings are the
    # weeks-1-4 shrink prior in the game-line model, Smarter NFL PR 5).
    try:
        games_rows = nflverse.fetch_games([resolved_season - 1, resolved_season])
        team_week_rows = nflverse.fetch_team_week_stats(resolved_season)
        _record_nflverse_success(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nflverse games/team-week fetch failed for %s: %s", resolved_season, exc)
        summary["errors"].append(f"games_or_team_week: {exc}")
        _record_nflverse_failure(db, error=f"games_or_team_week: {exc}")
        games_rows = []
        team_week_rows = []

    if games_rows:
        schedule_ttl = timedelta(minutes=settings.nfl_schedule_cache_minutes)
        season_games = [row for row in games_rows if str(row.get("season") or "") == str(resolved_season)]
        _upsert_cache_row(
            db, NflScheduleCache,
            {"season": resolved_season},
            {"games": season_games}, ttl=schedule_ttl, now=moment,
        )
        summary["schedule_games"] = len(season_games)

        ratings_ttl = timedelta(minutes=settings.nfl_team_rating_cache_minutes)
        ratings = compute_nfl_team_ratings(team_week_rows, games_rows, season=resolved_season)
        _upsert_cache_row(
            db, NflTeamRatingCache,
            {"season": resolved_season},
            ratings, ttl=ratings_ttl, now=moment,
        )
        summary["rated_teams"] = len(ratings.get("teams") or {})

        # Prior season: ratings + schedule are effectively frozen once
        # that season ends; refresh only when missing so the daily job
        # doesn't recompute history forever.
        prior_season = resolved_season - 1
        prior_exists = db.query(NflTeamRatingCache).filter(
            NflTeamRatingCache.season == prior_season
        ).one_or_none()
        if prior_exists is None:
            try:
                prior_team_week = nflverse.fetch_team_week_stats(prior_season)
            except Exception as exc:  # noqa: BLE001
                logger.warning("nflverse prior-season team-week fetch failed: %s", exc)
                summary["errors"].append(f"prior_team_week: {exc}")
                prior_team_week = []
            if prior_team_week:
                prior_ratings = compute_nfl_team_ratings(prior_team_week, games_rows, season=prior_season)
                _upsert_cache_row(
                    db, NflTeamRatingCache,
                    {"season": prior_season},
                    prior_ratings, ttl=ratings_ttl, now=moment,
                )
                summary["prior_season_rated_teams"] = len(prior_ratings.get("teams") or {})

    summary["weather_prewarmed"] = _prewarm_weather_for_upcoming_events(
        db, weather_client=weather_client, now=moment
    )
    return summary


def _prewarm_weather_for_upcoming_events(
    db: Session,
    *,
    weather_client: WeatherClient | None,
    now: datetime,
) -> int:
    """Fetch game-time weather for NFL events starting within the
    prewarm window so scoring's read path (allow_network=False) finds a
    warm cache row. Domes are skipped inside :func:`load_nfl_weather`."""
    window_end = now + timedelta(hours=WEATHER_PREWARM_WINDOW_HOURS)
    events = (
        db.execute(
            select(Event)
            .options(joinedload(Event.participants).joinedload(EventParticipant.participant))
            .where(
                Event.sport_key == "NFL",
                Event.starts_at >= now - timedelta(hours=6),
                Event.starts_at <= window_end,
            )
        )
        .unique()
        .scalars()
        .all()
    )
    warmed = 0
    for event in events:
        home_name = next(
            (
                ep.participant.display_name
                for ep in event.participants
                if ep.is_home and ep.participant is not None
            ),
            None,
        )
        home_abbr = nfl_team_abbr_for_name(home_name)
        if not home_abbr:
            continue
        result = load_nfl_weather(
            db,
            event_id=str(event.id),
            home_team_abbr=home_abbr,
            game_time_utc=_coerce_utc(event.starts_at),
            client=weather_client,
            allow_network=True,
            now=now,
        )
        if result.complete:
            warmed += 1
    return warmed


# -----------------------------------------------------------------------------
# Read-side loaders (cache-only; the daily job owns the network)

def load_nfl_team_ratings(db: Session, season: int, *, now: datetime | None = None) -> AdvancedLoadResult:
    return _read_cache_row(db, NflTeamRatingCache, {"season": season}, now=now or utcnow())


def load_nfl_schedule(db: Session, season: int, *, now: datetime | None = None) -> AdvancedLoadResult:
    return _read_cache_row(db, NflScheduleCache, {"season": season}, now=now or utcnow())


def load_nfl_depth_chart(db: Session, season: int, team: str, *, now: datetime | None = None) -> AdvancedLoadResult:
    return _read_cache_row(
        db, NflDepthChartCache,
        {"season": season, "team": str(team).strip().upper()},
        now=now or utcnow(),
    )


def load_nfl_weekly_stats(db: Session, season: int, week: int, *, now: datetime | None = None) -> AdvancedLoadResult:
    return _read_cache_row(db, NflWeeklyStatsCache, {"season": season, "week": week}, now=now or utcnow())


def load_nfl_snap_counts(db: Session, season: int, *, now: datetime | None = None) -> AdvancedLoadResult:
    """All cached snap-count weeks for a season, merged as
    ``{"weeks": {"<week>": rows}}`` — the participation gate wants the
    last-N-weeks trend, not one week."""
    moment = now or utcnow()
    rows = (
        db.query(NflSnapCountsCache)
        .filter(NflSnapCountsCache.season == season)
        .order_by(NflSnapCountsCache.week.asc())
        .all()
    )
    if not rows:
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)
    weeks = {str(row.week): list((row.payload or {}).get("rows") or []) for row in rows}
    freshest = max((_coerce_utc(row.cached_at) or moment) for row in rows)
    any_fresh = any((_coerce_utc(row.expires_at) or moment) > moment for row in rows)
    return AdvancedLoadResult(
        payload={"weeks": weeks},
        cache_status="hit" if any_fresh else "stale",
        complete=True,
        cached_at=freshest,
    )


def load_nfl_official_injuries(
    db: Session,
    season: int,
    week: int | None = None,
    *,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Official club injury report rows. ``week=None`` returns the
    latest cached week (the current report during the season)."""
    moment = now or utcnow()
    query = db.query(NflOfficialInjuryCache).filter(NflOfficialInjuryCache.season == season)
    if week is not None:
        row = query.filter(NflOfficialInjuryCache.week == week).one_or_none()
    else:
        row = query.order_by(NflOfficialInjuryCache.week.desc()).first()
    if row is None:
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)
    fresh = (_coerce_utc(row.expires_at) or moment) > moment
    return AdvancedLoadResult(
        payload={"week": row.week, "rows": list((row.payload or {}).get("rows") or [])},
        cache_status="hit" if fresh else "stale",
        complete=True,
        cached_at=_coerce_utc(row.cached_at),
    )


def load_nfl_weather(
    db: Session,
    *,
    event_id: str,
    home_team_abbr: str | None,
    game_time_utc: datetime | None,
    client: WeatherClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Game-time weather for an NFL event. Mirrors
    ``mlb_advanced.load_weather`` over ``NflWeatherCache``; dome and
    retractable-roof stadiums short-circuit to fixed indoor values
    (retractables close in exactly the weather that would matter)."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.nfl_weather_cache_minutes)

    stadium = nfl_stadium_info(home_team_abbr)
    roof = str((stadium or {}).get("roof") or "").lower()
    if roof in {"dome", "retractable"}:
        payload = {
            "temp_f": 70.0, "wind_speed_mph": 0.0, "wind_dir_deg": 0.0,
            "precip_pct": 0.0, "humidity_pct": 50.0, "is_dome": True,
            "roof": roof, "source": "dome",
        }
        return AdvancedLoadResult(payload=payload, cache_status="dome", complete=True, cached_at=None)

    cached = (
        db.query(NflWeatherCache).filter(NflWeatherCache.event_id == str(event_id)).one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(
            payload=dict(cached.payload or {}),
            cache_status="hit",
            complete=True,
            cached_at=_coerce_utc(cached.cached_at),
        )
    if not allow_network or not settings.advanced_stats_enabled:
        if cached is not None:
            return AdvancedLoadResult(
                payload=dict(cached.payload or {}),
                cache_status="stale",
                complete=True,
                cached_at=_coerce_utc(cached.cached_at),
            )
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)
    lat = _safe_float((stadium or {}).get("lat"))
    lon = _safe_float((stadium or {}).get("lon"))
    if lat is None or lon is None or game_time_utc is None:
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    weather_client = client or WeatherClient()
    try:
        payload = weather_client.fetch_game_weather(lat=lat, lon=lon, game_time_utc=game_time_utc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NFL weather fetch failed for event %s: %s", event_id, exc)
        if cached is not None:
            return AdvancedLoadResult(
                payload=dict(cached.payload or {}),
                cache_status="stale",
                complete=True,
                cached_at=_coerce_utc(cached.cached_at),
            )
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    payload = {**payload, "is_dome": False, "roof": roof or "outdoor"}
    if cached is None:
        db.add(
            NflWeatherCache(
                event_id=str(event_id),
                payload=payload,
                cached_at=moment,
                expires_at=moment + ttl,
            )
        )
    else:
        cached.payload = payload
        cached.cached_at = moment
        cached.expires_at = moment + ttl
    db.flush()
    return AdvancedLoadResult(payload=payload, cache_status="miss", complete=True, cached_at=moment)
