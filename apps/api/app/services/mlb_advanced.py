"""MLB advanced-stats orchestrator.

Mirrors the NBA pattern in :mod:`app.services.advanced_stats` but for the
MLB stack: MLB Stats API for sabermetrics + lineups + venues + bullpen
state, Baseball Savant for Statcast (per-batter-ball / per-pitch
aggregates), OpenWeatherMap (with NWS fallback) for game-time weather,
and a curated FanGraphs JSON for park factors.

Each ``load_*`` function follows the cache-hit → cache-stale → upstream
fetch flow established by ``_load_player_gamelog`` in scoring.py.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.clients.baseball_savant import BaseballSavantClient, parse_csv_rows
from app.clients.mlb_stats import MlbStatsClient
from app.clients.weather import WeatherClient
from app.config import get_settings
from app.models import (
    EspnPlayerSearchCache,
    MlbBatterAdvancedCache,
    MlbBullpenStateCache,
    MlbInjuryReportCache,
    MlbLeaguePercentilesCache,
    MlbLineupCache,
    MlbPitcherAdvancedCache,
    MlbPlayerRosterCache,
    MlbPlayerSplitsCache,
    MlbStatcastBatterCache,
    MlbStatcastPitcherCache,
    MlbTeamGamelogCache,
    MlbWeatherCache,
    utcnow,
)
from app.services.advanced_stats import AdvancedLoadResult, _coerce_utc, _safe_float


logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Helpers shared across MLB loaders

def _normalize_name(name: str | None) -> str:
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.lower() if ch.isalnum())


def _stat_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Drill into MLB Stats API response — ``stats[].splits[].stat`` rows."""
    return list(payload.get("stats") or [])


def _flatten_stat_splits(payload: dict[str, Any], group: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for stats_block in _stat_groups(payload):
        if group is not None and ((stats_block.get("group") or {}).get("displayName") or "") != group:
            continue
        for split in stats_block.get("splits") or []:
            row = {**(split.get("stat") or {})}
            row["_split_meta"] = {k: v for k, v in split.items() if k != "stat"}
            out.append(row)
    return out


def _safe_pct(value: Any) -> float | None:
    """MLB API often returns rates as ``".300"`` strings — coerce to float in [0,1]."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# -----------------------------------------------------------------------------
# Batter sabermetrics + advanced

_MLB_BATTER_METRICS: tuple[str, ...] = (
    "woba", "wrc_plus", "iso", "babip", "ops", "obp", "slg", "avg",
    "walk_rate", "strikeout_rate",
)


def load_mlb_batter_advanced(
    db: Session,
    *,
    mlb_player_id: str,
    season: int,
    client: MlbStatsClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Sabermetrics + season hitting splits for one batter."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.mlb_batter_advanced_cache_minutes)

    cached = (
        db.query(MlbBatterAdvancedCache)
        .filter(
            MlbBatterAdvancedCache.athlete_id == str(mlb_player_id),
            MlbBatterAdvancedCache.season == season,
        )
        .one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if not allow_network or not settings.advanced_stats_enabled:
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    mlb_client = client or MlbStatsClient()
    try:
        sabermetrics = mlb_client.fetch_player_sabermetrics(mlb_player_id, season)
        season_stats = mlb_client.fetch_player_hitting_advanced(mlb_player_id, season)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MLB Stats batter fetch failed for player %s season %d: %s", mlb_player_id, season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    saber_rows = _flatten_stat_splits(sabermetrics)
    season_rows = _flatten_stat_splits(season_stats, group="hitting")

    season_avg: dict[str, float | None] = {key: None for key in _MLB_BATTER_METRICS}
    if saber_rows:
        row = saber_rows[0]
        season_avg["woba"] = _safe_pct(row.get("woba"))
        season_avg["wrc_plus"] = _safe_float(row.get("wRcPlus") or row.get("wrcPlus"))
        season_avg["iso"] = _safe_pct(row.get("iso"))
        season_avg["babip"] = _safe_pct(row.get("babip"))
    if season_rows:
        row = season_rows[0]
        season_avg["ops"] = _safe_pct(row.get("ops"))
        season_avg["obp"] = _safe_pct(row.get("obp"))
        season_avg["slg"] = _safe_pct(row.get("slg"))
        season_avg["avg"] = _safe_pct(row.get("avg"))
        plate_appearances = _safe_float(row.get("plateAppearances"))
        walks = _safe_float(row.get("baseOnBalls"))
        strikeouts = _safe_float(row.get("strikeOuts"))
        if plate_appearances and plate_appearances > 0:
            if walks is not None:
                season_avg["walk_rate"] = walks / plate_appearances
            if strikeouts is not None:
                season_avg["strikeout_rate"] = strikeouts / plate_appearances

    structured = {
        "season_avg": season_avg,
        "fetched_at": moment.isoformat(),
    }

    if cached is None:
        db.add(
            MlbBatterAdvancedCache(
                athlete_id=str(mlb_player_id),
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
# Pitcher sabermetrics

def load_mlb_pitcher_advanced(
    db: Session,
    *,
    mlb_player_id: str,
    season: int,
    client: MlbStatsClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Pitcher xERA, FIP, K/9, BB/9, HR/9, WHIP."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.mlb_pitcher_advanced_cache_minutes)

    cached = (
        db.query(MlbPitcherAdvancedCache)
        .filter(
            MlbPitcherAdvancedCache.athlete_id == str(mlb_player_id),
            MlbPitcherAdvancedCache.season == season,
        )
        .one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if not allow_network or not settings.advanced_stats_enabled:
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    mlb_client = client or MlbStatsClient()
    try:
        payload = mlb_client.fetch_pitcher_sabermetrics(mlb_player_id, season)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MLB Stats pitcher fetch failed for player %s season %d: %s", mlb_player_id, season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    season_avg: dict[str, float | None] = {
        "fip": None, "xfip": None, "xera": None, "era": None,
        "whip": None, "k_per_9": None, "bb_per_9": None, "hr_per_9": None,
    }
    for stats_block in _stat_groups(payload):
        for split in stats_block.get("splits") or []:
            row = split.get("stat") or {}
            for key, src in (
                ("fip", "fip"),
                ("xfip", "xfip"),
                ("xera", "xera"),
                ("era", "era"),
                ("whip", "whip"),
                ("k_per_9", "strikeoutsPer9Inn"),
                ("bb_per_9", "walksPer9Inn"),
                ("hr_per_9", "homeRunsPer9"),
            ):
                if season_avg[key] is None:
                    season_avg[key] = _safe_pct(row.get(src))

    structured = {"season_avg": season_avg, "fetched_at": moment.isoformat()}

    if cached is None:
        db.add(
            MlbPitcherAdvancedCache(
                athlete_id=str(mlb_player_id),
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
# Statcast batter aggregates

def load_mlb_statcast_batter(
    db: Session,
    *,
    mlb_player_id: str,
    season: int,
    client: BaseballSavantClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Per-batted-ball Statcast events aggregated to per-batter-season metrics."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.mlb_statcast_batter_cache_minutes)

    cached = (
        db.query(MlbStatcastBatterCache)
        .filter(
            MlbStatcastBatterCache.athlete_id == str(mlb_player_id),
            MlbStatcastBatterCache.season == season,
        )
        .one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if not allow_network or not settings.advanced_stats_enabled:
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    savant = client or BaseballSavantClient()
    try:
        csv_payload = savant.fetch_batter_statcast(mlb_player_id, season)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Savant batter fetch failed for player %s season %d: %s", mlb_player_id, season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    rows = parse_csv_rows(csv_payload)
    structured = _aggregate_statcast_batter_events(rows)
    structured["fetched_at"] = moment.isoformat()

    if cached is None:
        db.add(
            MlbStatcastBatterCache(
                athlete_id=str(mlb_player_id),
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


def _aggregate_statcast_batter_events(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Convert per-pitch CSV rows into per-batter rolling aggregates."""
    if not rows:
        return {"season_avg": {}, "events": 0}

    def _avg(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    exit_velocities: list[float] = []
    launch_angles: list[float] = []
    barrels = 0
    hard_hit = 0
    sweet_spot = 0
    xba_values: list[float] = []
    xslg_values: list[float] = []
    xwoba_values: list[float] = []

    for row in rows:
        ev = _safe_float(row.get("launch_speed"))
        la = _safe_float(row.get("launch_angle"))
        if ev is not None:
            exit_velocities.append(ev)
        if la is not None:
            launch_angles.append(la)
        if (row.get("launch_speed_angle") or "").strip() == "6":  # Statcast classification: 6 = barrel
            barrels += 1
        if ev is not None and ev >= 95.0:
            hard_hit += 1
        if la is not None and 8.0 <= la <= 32.0:
            sweet_spot += 1
        for src, sink in (("estimated_ba_using_speedangle", xba_values),
                           ("estimated_slg_using_speedangle", xslg_values),
                           ("estimated_woba_using_speedangle", xwoba_values)):
            v = _safe_float(row.get(src))
            if v is not None:
                sink.append(v)

    total = len(rows)
    return {
        "events": total,
        "season_avg": {
            "exit_velocity_avg": _avg(exit_velocities),
            "launch_angle_avg": _avg(launch_angles),
            "barrel_rate": (barrels / total) if total else None,
            "hard_hit_rate": (hard_hit / total) if total else None,
            "sweet_spot_rate": (sweet_spot / total) if total else None,
            "xba": _avg(xba_values),
            "xslg": _avg(xslg_values),
            "xwoba": _avg(xwoba_values),
        },
    }


# -----------------------------------------------------------------------------
# Statcast pitcher aggregates

def load_mlb_statcast_pitcher(
    db: Session,
    *,
    mlb_player_id: str,
    season: int,
    client: BaseballSavantClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Per-pitch Statcast events aggregated to per-pitcher metrics."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.mlb_statcast_pitcher_cache_minutes)

    cached = (
        db.query(MlbStatcastPitcherCache)
        .filter(
            MlbStatcastPitcherCache.athlete_id == str(mlb_player_id),
            MlbStatcastPitcherCache.season == season,
        )
        .one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if not allow_network or not settings.advanced_stats_enabled:
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    savant = client or BaseballSavantClient()
    try:
        csv_payload = savant.fetch_pitcher_statcast(mlb_player_id, season)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Savant pitcher fetch failed for player %s season %d: %s", mlb_player_id, season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    rows = parse_csv_rows(csv_payload)
    structured = _aggregate_statcast_pitcher_events(rows)
    structured["fetched_at"] = moment.isoformat()

    if cached is None:
        db.add(
            MlbStatcastPitcherCache(
                athlete_id=str(mlb_player_id),
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


def _aggregate_statcast_pitcher_events(rows: list[dict[str, str]]) -> dict[str, Any]:
    if not rows:
        return {"season_avg": {}, "pitches": 0}

    fastball_velocities: list[float] = []
    swings = 0
    whiffs = 0
    called_strikes = 0
    swinging_strikes = 0
    putaways = 0  # 2-strike pitches that ended in K
    total = len(rows)

    for row in rows:
        pitch_type = (row.get("pitch_type") or "").upper()
        velo = _safe_float(row.get("release_speed"))
        if pitch_type in {"FF", "FT", "FA"} and velo is not None:
            fastball_velocities.append(velo)

        description = (row.get("description") or "").lower()
        if "swing" in description or "foul" in description:
            swings += 1
        if "swinging_strike" in description or "swinging_strike_blocked" in description:
            whiffs += 1
            swinging_strikes += 1
        if description == "called_strike":
            called_strikes += 1

        if (row.get("strikes") or "0") == "2" and (row.get("events") or "").strip() == "strikeout":
            putaways += 1

    return {
        "pitches": total,
        "season_avg": {
            "avg_fastball_velo": (sum(fastball_velocities) / len(fastball_velocities))
                if fastball_velocities else None,
            "whiff_pct": (whiffs / swings) if swings else None,
            "csw_pct": ((called_strikes + swinging_strikes) / total) if total else None,
            "putaway_pct": (putaways / total) if total else None,
        },
    }


# -----------------------------------------------------------------------------
# Player splits

def load_mlb_player_splits(
    db: Session,
    *,
    mlb_player_id: str,
    season: int,
    split_kind: str,
    group: str = "hitting",
    client: MlbStatsClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Vs LHP/RHP, home/away, day/night splits for one player-season."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.mlb_player_splits_cache_minutes)

    cached = (
        db.query(MlbPlayerSplitsCache)
        .filter(
            MlbPlayerSplitsCache.athlete_id == str(mlb_player_id),
            MlbPlayerSplitsCache.season == season,
            MlbPlayerSplitsCache.split_kind == split_kind,
        )
        .one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if not allow_network or not settings.advanced_stats_enabled:
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    mlb_client = client or MlbStatsClient()
    try:
        payload = mlb_client.fetch_player_splits(mlb_player_id, season, split_kind=split_kind, group=group)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "MLB Stats splits fetch failed for player %s season %d split %s: %s",
            mlb_player_id, season, split_kind, exc,
        )
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    splits_rows = _flatten_stat_splits(payload, group=group)
    structured = {"splits": splits_rows, "fetched_at": moment.isoformat()}

    if cached is None:
        db.add(
            MlbPlayerSplitsCache(
                athlete_id=str(mlb_player_id),
                season=season,
                split_kind=split_kind,
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
# Park factors (file-backed, no DB cache)

_PARK_FACTORS_CACHE: dict[str, dict[str, Any]] | None = None
_PARK_FACTORS_PATH = Path(__file__).resolve().parent.parent / "data" / "park_factors.json"


def _load_park_factors_file() -> dict[str, dict[str, Any]]:
    global _PARK_FACTORS_CACHE
    if _PARK_FACTORS_CACHE is None:
        try:
            text = _PARK_FACTORS_PATH.read_text()
            data = json.loads(text)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("park_factors.json read failed: %s", exc)
            data = {}
        _PARK_FACTORS_CACHE = {k: v for k, v in data.items() if not k.startswith("_")}
    return _PARK_FACTORS_CACHE


def _materialize_park_factors(entry: dict[str, Any] | None) -> dict[str, float]:
    if not entry:
        return _NEUTRAL_PARK_FACTORS
    return {
        "hr": _safe_float(entry.get("hr")) or 1.0,
        "r": _safe_float(entry.get("r")) or 1.0,
        "1b": _safe_float(entry.get("1b")) or 1.0,
        "2b": _safe_float(entry.get("2b")) or 1.0,
        "3b": _safe_float(entry.get("3b")) or 1.0,
        "bb": _safe_float(entry.get("bb")) or 1.0,
        "so": _safe_float(entry.get("so")) or 1.0,
        "_data_complete": 1.0,
        "_venue_name": entry.get("name") or "",
    }


def load_park_factors(venue_id: str | int | None) -> dict[str, float]:
    """Return the park-factor multipliers for an MLB venue, or league-neutral defaults."""
    if venue_id is None:
        return _NEUTRAL_PARK_FACTORS
    return _materialize_park_factors(_load_park_factors_file().get(str(venue_id)))


# Codex PR #30 P2: ESPN emits two-letter codes for some MLB teams while
# park_factors.json (FanGraphs schema) uses three-letter codes. Without
# this alias map six real ESPN abbreviations silently fall back to neutral.
_ESPN_TO_PARK_FACTORS_TEAM_ALIAS: dict[str, str] = {
    "SF": "SFG",   # San Francisco Giants
    "SD": "SDP",   # San Diego Padres
    "TB": "TBR",   # Tampa Bay Rays
    "KC": "KCR",   # Kansas City Royals
    "WSH": "WSN",  # Washington Nationals
    "ATH": "OAK",  # Oakland Athletics (ESPN's 2025+ rebrand)
}


def load_park_factors_for_team(team_abbreviation: str | None) -> dict[str, float]:
    """Return the park-factor multipliers for the home team's stadium.

    Bug #4: ESPN's ``venue.id`` is keyed by ESPN's own venue catalog
    (e.g. ``230`` for CoolToday Park) and does not align with
    ``park_factors.json`` (keyed by FanGraphs' numeric venue ids 1-33).
    The reliable join — present on every regular-season ESPN event — is
    the home team's three-letter abbreviation, so we look up park
    factors by team instead. Some ESPN abbreviations differ from the
    FanGraphs codes (SF/SFG, SD/SDP, etc.) — see
    ``_ESPN_TO_PARK_FACTORS_TEAM_ALIAS``.
    """
    if not team_abbreviation:
        return _NEUTRAL_PARK_FACTORS
    normalized = str(team_abbreviation).strip().upper()
    if not normalized:
        return _NEUTRAL_PARK_FACTORS
    resolved = _ESPN_TO_PARK_FACTORS_TEAM_ALIAS.get(normalized, normalized)
    entry = _PARK_FACTORS_BY_TEAM.get(resolved)
    return _materialize_park_factors(entry)


_NEUTRAL_PARK_FACTORS: dict[str, float] = {
    "hr": 1.0, "r": 1.0, "1b": 1.0, "2b": 1.0, "3b": 1.0, "bb": 1.0, "so": 1.0,
    "_data_complete": 0.0,
}


def _build_park_factors_by_team() -> dict[str, dict[str, Any]]:
    """Reverse-index ``park_factors.json`` by team abbreviation. Cached at
    import time — the source file is bundled and immutable at runtime.

    NOTE: when a team has more than one entry (e.g. TBR's Tropicana Field
    *and* Steinbrenner Field for the 2025 hurricane-displacement season),
    later entries win. Callers should prefer the venue-name index when
    ESPN provides a venue name to disambiguate.
    """
    by_team: dict[str, dict[str, Any]] = {}
    for key, entry in _load_park_factors_file().items():
        if key == "_metadata" or not isinstance(entry, dict):
            continue
        team = str(entry.get("team") or "").strip().upper()
        if team:
            by_team[team] = entry
    return by_team


def _build_park_factors_by_venue_name() -> dict[str, dict[str, Any]]:
    """Reverse-index by venue display name (lowercased). Lets us pick the
    right entry for teams with multiple park entries — ESPN's
    ``venue.fullName`` is the discriminator."""
    by_name: dict[str, dict[str, Any]] = {}
    for key, entry in _load_park_factors_file().items():
        if key == "_metadata" or not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip().lower()
        if name:
            by_name[name] = entry
    return by_name


_PARK_FACTORS_BY_TEAM: dict[str, dict[str, Any]] = _build_park_factors_by_team()
_PARK_FACTORS_BY_NAME: dict[str, dict[str, Any]] = _build_park_factors_by_venue_name()


def load_park_factors_for_event(
    event_raw_data: dict[str, Any] | None,
    home_team_abbreviation: str | None,
) -> dict[str, float]:
    """Resolve MLB park factors for a scored event.

    Precedence:
    1. ``venue.fullName`` from ESPN's competition payload — disambiguates
       teams that have more than one park entry (TBR Tropicana vs.
       Steinbrenner).
    2. Home team abbreviation — covers normal ESPN rows where the venue
       name doesn't appear in the bundled park factors file.
    3. Legacy top-level ``venue_id`` — pre-dates the ESPN refactor; some
       non-ESPN rows still set this.
    4. Neutral defaults when nothing matches.
    """
    raw = event_raw_data or {}
    competition = ((raw.get("raw") or {}).get("competitions") or [{}])[0]
    venue_name = str(((competition.get("venue") or {}).get("fullName") or "")).strip().lower()
    if venue_name:
        entry = _PARK_FACTORS_BY_NAME.get(venue_name)
        if entry:
            return _materialize_park_factors(entry)

    by_team = load_park_factors_for_team(home_team_abbreviation)
    if by_team.get("_data_complete"):
        return by_team

    legacy_venue_id = raw.get("venue_id")
    if legacy_venue_id is not None:
        return load_park_factors(legacy_venue_id)
    return _NEUTRAL_PARK_FACTORS


# -----------------------------------------------------------------------------
# Venue coordinates — used by the Smarter #15 weather pre-warm job to look up
# (lat, lon, is_dome) for each scheduled game. Keyed by the same three-letter
# team abbreviation that ``_PARK_FACTORS_BY_TEAM`` uses (so ESPN aliases via
# ``_ESPN_TO_PARK_FACTORS_TEAM_ALIAS`` map onto these entries too).
#
# Dome policy: ``is_dome=True`` ONLY for parks whose roof stays closed during
# essentially every regular-season game (currently only TBR Tropicana Field).
# Retractable-roof parks (TEX, MIA, MIL, TOR, ARI, HOU, SEA, MIN) are still
# treated as open-air for forecasting purposes — when the roof closes for a
# specific game the forecast doesn't matter (cached payload sits unused).
# We'd rather have a stale forecast than a wrong-dome misclassification.

_MLB_PARK_COORDS: dict[str, tuple[float, float, bool]] = {
    "ARI": (33.4453, -112.0667, False),  # Chase Field (retractable)
    "ATL": (33.8908, -84.4678, False),   # Truist Park
    "BAL": (39.2839, -76.6217, False),   # Camden Yards
    "BOS": (42.3467, -71.0972, False),   # Fenway Park
    "CHC": (41.9484, -87.6553, False),   # Wrigley Field
    "CHW": (41.8300, -87.6339, False),   # Guaranteed Rate / Rate Field
    "CIN": (39.0975, -84.5069, False),   # Great American Ball Park
    "CLE": (41.4962, -81.6852, False),   # Progressive Field
    "COL": (39.7559, -104.9942, False),  # Coors Field
    "DET": (42.3390, -83.0485, False),   # Comerica Park
    "HOU": (29.7572, -95.3554, False),   # Daikin / Minute Maid (retractable)
    "KCR": (39.0517, -94.4803, False),   # Kauffman Stadium
    "LAA": (33.8003, -117.8827, False),  # Angel Stadium
    "LAD": (34.0739, -118.2400, False),  # Dodger Stadium
    "MIA": (25.7781, -80.2197, False),   # loanDepot Park (retractable)
    "MIL": (43.0280, -87.9712, False),   # American Family Field (retractable)
    "MIN": (44.9817, -93.2776, False),   # Target Field
    "NYM": (40.7571, -73.8458, False),   # Citi Field
    "NYY": (40.8296, -73.9262, False),   # Yankee Stadium
    "OAK": (37.7516, -122.2005, False),  # Oakland Coliseum
    "PHI": (39.9061, -75.1665, False),   # Citizens Bank Park
    "PIT": (40.4469, -80.0058, False),   # PNC Park
    "SDP": (32.7073, -117.1566, False),  # Petco Park
    "SEA": (47.5914, -122.3325, False),  # T-Mobile Park (retractable)
    "SFG": (37.7786, -122.3893, False),  # Oracle Park
    "STL": (38.6226, -90.1928, False),   # Busch Stadium
    "TBR": (27.7682, -82.6534, True),    # Tropicana Field (always closed)
    "TEX": (32.7473, -97.0817, False),   # Globe Life Field (retractable)
    "TOR": (43.6414, -79.3894, False),   # Rogers Centre (retractable)
    "WSN": (38.8730, -77.0074, False),   # Nationals Park
}


def mlb_park_coords(team_abbreviation: str | None) -> tuple[float, float, bool] | None:
    """Return ``(lat, lon, is_dome)`` for the home team's park, or ``None``
    when the abbreviation doesn't resolve. Accepts ESPN's two-letter aliases
    (SF, SD, etc.) by routing through ``_ESPN_TO_PARK_FACTORS_TEAM_ALIAS``."""

    if not team_abbreviation:
        return None
    normalized = str(team_abbreviation).strip().upper()
    if not normalized:
        return None
    resolved = _ESPN_TO_PARK_FACTORS_TEAM_ALIAS.get(normalized, normalized)
    return _MLB_PARK_COORDS.get(resolved)


# -----------------------------------------------------------------------------
# Weather

def load_weather(
    db: Session,
    *,
    event_id: str,
    lat: float | None,
    lon: float | None,
    game_time_utc: datetime | None,
    is_dome: bool = False,
    client: WeatherClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Game-time weather. Returns dome-fixed payload immediately for indoor venues."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.mlb_weather_cache_minutes)

    if is_dome:
        payload = {
            "temp_f": 72.0, "wind_speed_mph": 0.0, "wind_dir_deg": 0.0,
            "precip_pct": 0.0, "humidity_pct": 50.0, "is_dome": True, "source": "dome",
        }
        return AdvancedLoadResult(payload=payload, cache_status="dome", complete=True)

    cached = (
        db.query(MlbWeatherCache).filter(MlbWeatherCache.event_id == str(event_id)).one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if not allow_network or not settings.advanced_stats_enabled:
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)
    if lat is None or lon is None or game_time_utc is None:
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    weather_client = client or WeatherClient()
    try:
        payload = weather_client.fetch_game_weather(lat=lat, lon=lon, game_time_utc=game_time_utc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Weather fetch failed for event %s: %s", event_id, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    if cached is None:
        db.add(
            MlbWeatherCache(
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
    return AdvancedLoadResult(payload=payload, cache_status="miss", complete=True)


# -----------------------------------------------------------------------------
# Lineup context

def load_lineup_for_event(
    db: Session,
    *,
    event_id: str,
    schedule_payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Persist a lineup payload (schedule hydration owns the actual fetch)."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.mlb_lineup_cache_minutes)

    cached = db.query(MlbLineupCache).filter(MlbLineupCache.event_id == str(event_id)).one_or_none()
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment and schedule_payload is None:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)

    if schedule_payload is None:
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    structured = {"raw": schedule_payload, "fetched_at": moment.isoformat()}
    if cached is None:
        db.add(
            MlbLineupCache(
                event_id=str(event_id),
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
# Player-ID resolution: ESPN athlete_id → MLB Stats PERSON_ID

def load_mlb_player_roster(
    db: Session,
    *,
    season: int,
    client: MlbStatsClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> AdvancedLoadResult:
    """Daily snapshot of all MLB teams' rosters, used for ID resolution."""
    moment = now or utcnow()
    settings = get_settings()
    ttl = timedelta(minutes=settings.mlb_player_roster_cache_minutes)

    cached = (
        db.query(MlbPlayerRosterCache).filter(MlbPlayerRosterCache.season == season).one_or_none()
    )
    if cached is not None and (_coerce_utc(cached.expires_at) or moment) > moment:
        return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="hit", complete=True)
    if not allow_network or not settings.advanced_stats_enabled:
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    mlb_client = client or MlbStatsClient()
    try:
        teams_payload = mlb_client.fetch_all_teams(season)
        all_players: list[dict[str, Any]] = []
        for team_block in teams_payload.get("teams") or []:
            team_id = team_block.get("id")
            if team_id is None:
                continue
            try:
                roster_payload = mlb_client.fetch_team_roster(str(team_id), season=season)
            except Exception:  # noqa: BLE001 — skip individual team failures
                continue
            for entry in roster_payload.get("roster") or []:
                person = entry.get("person") or {}
                all_players.append(
                    {
                        "person_id": str(person.get("id") or ""),
                        "display_name": person.get("fullName") or "",
                        "team_id": str(team_id),
                        "team_abbreviation": (team_block.get("abbreviation") or "").upper(),
                        "team_name": team_block.get("name") or "",
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("MLB Stats roster fetch failed for season %d: %s", season, exc)
        if cached is not None:
            return AdvancedLoadResult(payload=dict(cached.payload or {}), cache_status="stale", complete=True)
        return AdvancedLoadResult(payload={}, cache_status="miss", complete=False)

    structured = {"players": all_players, "fetched_at": moment.isoformat()}

    if cached is None:
        db.add(
            MlbPlayerRosterCache(
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


def resolve_mlb_stats_player_id(
    db: Session,
    *,
    espn_athlete_id: str | None,
    full_name: str,
    team_abbreviation: str | None = None,
    season: int,
    client: MlbStatsClient | None = None,
    allow_network: bool = False,
) -> str | None:
    """Map an ESPN-known MLB player to its MLB Stats PERSON_ID.

    Mirrors the NBA flow in ``app.services.advanced_stats.resolve_nba_stats_player_id``.
    """
    if not full_name:
        return None

    if espn_athlete_id:
        for entry in (
            db.query(EspnPlayerSearchCache)
            .filter(EspnPlayerSearchCache.sport_key == "MLB")
            .all()
        ):
            payload = entry.payload or {}
            if str(payload.get("athlete_id")) == str(espn_athlete_id):
                stats_id = payload.get("mlb_stats_id")
                if stats_id:
                    return str(stats_id)

    roster_result = load_mlb_player_roster(db, season=season, client=client, allow_network=allow_network)
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
        for entry in (
            db.query(EspnPlayerSearchCache)
            .filter(EspnPlayerSearchCache.sport_key == "MLB")
            .all()
        ):
            payload = dict(entry.payload or {})
            if str(payload.get("athlete_id")) == str(espn_athlete_id):
                payload["mlb_stats_id"] = str(resolved)
                entry.payload = payload
                db.flush()
                break
    return resolved


# -----------------------------------------------------------------------------
# Feature emitters — what gets written into predictions.features

def emit_mlb_batter_features(
    sabermetrics: dict[str, Any] | None,
    statcast: dict[str, Any] | None,
) -> dict[str, float]:
    """Combine sabermetrics + Statcast into a single batter feature dict."""
    out: dict[str, float] = {}

    def _set(key: str, value: Any) -> None:
        if isinstance(value, (int, float)):
            out[key] = round(float(value), 4)

    saber = (sabermetrics or {}).get("season_avg") or {}
    _set("season_woba", saber.get("woba"))
    _set("season_iso", saber.get("iso"))
    _set("season_walk_rate", saber.get("walk_rate"))
    _set("season_strikeout_rate", saber.get("strikeout_rate"))
    _set("season_obp", saber.get("obp"))
    _set("season_slg", saber.get("slg"))
    _set("season_ops", saber.get("ops"))
    _set("season_avg", saber.get("avg"))
    _set("season_wrc_plus", saber.get("wrc_plus"))
    _set("season_babip", saber.get("babip"))

    sc = (statcast or {}).get("season_avg") or {}
    _set("season_xwoba", sc.get("xwoba"))
    _set("season_xba", sc.get("xba"))
    _set("season_xslg", sc.get("xslg"))
    _set("season_barrel_rate", sc.get("barrel_rate"))
    _set("season_hard_hit_rate", sc.get("hard_hit_rate"))
    _set("season_exit_velocity_avg", sc.get("exit_velocity_avg"))
    _set("season_launch_angle_avg", sc.get("launch_angle_avg"))
    _set("season_sweet_spot_rate", sc.get("sweet_spot_rate"))

    if out:
        out["mlb_batter_data_complete"] = 1.0
    return out


def emit_mlb_pitcher_features(payload: dict[str, Any] | None, statcast: dict[str, Any] | None) -> dict[str, float]:
    """Opposing-pitcher feature dict (xFIP, FIP, K/9, BB/9, plus Statcast pitcher metrics)."""
    out: dict[str, float] = {}

    def _set(key: str, value: Any) -> None:
        if isinstance(value, (int, float)):
            out[key] = round(float(value), 4)

    saber = (payload or {}).get("season_avg") or {}
    _set("opposing_starter_xfip", saber.get("xfip"))
    _set("opposing_starter_fip", saber.get("fip"))
    _set("opposing_starter_xera", saber.get("xera"))
    _set("opposing_starter_era", saber.get("era"))
    _set("opposing_starter_whip", saber.get("whip"))
    _set("opposing_starter_k_per_9", saber.get("k_per_9"))
    _set("opposing_starter_bb_per_9", saber.get("bb_per_9"))
    _set("opposing_starter_hr_per_9", saber.get("hr_per_9"))

    sc = (statcast or {}).get("season_avg") or {}
    _set("opposing_starter_avg_fastball_velo", sc.get("avg_fastball_velo"))
    _set("opposing_starter_whiff_pct", sc.get("whiff_pct"))
    _set("opposing_starter_csw_pct", sc.get("csw_pct"))
    _set("opposing_starter_putaway_pct", sc.get("putaway_pct"))

    if out:
        out["pitcher_data_complete"] = 1.0
    return out


def extract_pitch_hand_from_lineup(
    lineup_payload: dict[str, Any] | None,
    mlb_pitcher_id: str | int | None,
) -> str | None:
    """Walk a cached lineup payload (schedule shape with ``hydrate=probablePitcher``)
    and return the ``"L"`` / ``"R"`` pitchHand code for ``mlb_pitcher_id``.

    Returns ``None`` when the payload, the pitcher id, or the pitchHand
    field is missing — the platoon factor is a no-op in that case.
    """

    if not lineup_payload or mlb_pitcher_id is None:
        return None
    target = str(mlb_pitcher_id)
    raw = lineup_payload.get("raw") or {}
    for date_block in raw.get("dates") or []:
        for game in date_block.get("games") or []:
            teams = game.get("teams") or {}
            for side_key in ("home", "away"):
                pitcher = (teams.get(side_key) or {}).get("probablePitcher") or {}
                if str(pitcher.get("id") or "") != target:
                    continue
                hand_block = pitcher.get("pitchHand") or {}
                return _normalize_pitch_hand(hand_block.get("code"))
    return None


def _normalize_pitch_hand(value: Any) -> str | None:
    """Coerce MLB Stats API's ``pitchHand.code`` (or any nearby variant)
    to a single uppercase ``"L"`` / ``"R"`` character. Returns ``None`` for
    switch-pitchers / unknown / missing values — the platoon factor stays
    a no-op rather than guessing."""

    if value is None:
        return None
    code = str(value).strip().upper()
    if code in ("L", "R"):
        return code
    if code in ("LEFT", "LHP"):
        return "L"
    if code in ("RIGHT", "RHP"):
        return "R"
    return None


def _split_row_for_hand(splits_payload: dict[str, Any] | None, vs_hand: str) -> dict[str, Any] | None:
    """Find the ``vs Left`` / ``vs Right`` row inside a cached splits
    payload. ``vs_hand`` is the OPPOSING pitcher's hand, so this returns
    the batter's stats against that hand. Returns ``None`` when no
    matching row exists or the payload shape is unexpected."""

    if vs_hand not in ("L", "R") or not splits_payload:
        return None
    splits = list(splits_payload.get("splits") or [])
    target_code = f"v{vs_hand.lower()}"
    target_desc = f"vs {'left' if vs_hand == 'L' else 'right'}"
    for row in splits:
        meta = row.get("_split_meta") or {}
        split = meta.get("split") or {}
        code = str(split.get("code") or "").strip().lower()
        desc = str(split.get("description") or "").strip().lower()
        if code == target_code or desc == target_desc:
            return row
    return None


def emit_mlb_platoon_features(
    starter_pitch_hand: str | None,
    splits_payload: dict[str, Any] | None,
    season_ops: float | None,
) -> dict[str, float]:
    """Smarter #5 — emit batter-vs-starter-platoon features.

    Returns ``{}`` when any input is missing (no pitcher hand, no splits,
    no overall OPS baseline). When data is present, emits:

    - ``batter_vs_starter_ops`` — batter's OPS in the vs-hand split.
    - ``batter_vs_starter_platoon_factor`` — ratio of vs-hand OPS to
      season OPS, clamped to ``[0.80, 1.20]`` so a single noisy split
      can't move the prediction more than ~20%.

    The factor is consumed by ``_mlb_batter_platoon_factor`` in
    ``heuristic_factors.py`` and gated to the batter offense stats
    (``hits``, ``home_runs``, ``total_bases``, ``rbis``, ``runs``).
    """

    hand = _normalize_pitch_hand(starter_pitch_hand)
    if hand is None:
        return {}
    if season_ops is None or season_ops <= 0:
        return {}
    row = _split_row_for_hand(splits_payload, hand)
    if row is None:
        return {}
    vs_ops_raw = row.get("ops")
    vs_ops = _safe_pct(vs_ops_raw)
    if vs_ops is None or vs_ops <= 0:
        return {}
    raw_ratio = vs_ops / float(season_ops)
    # Clamp to the same ±20% envelope ``_clamp`` uses elsewhere so a
    # tiny-sample split (e.g. 12 PA vs LHP) can't dominate the prediction.
    clamped = max(0.80, min(1.20, raw_ratio))
    return {
        "batter_vs_starter_ops": round(vs_ops, 4),
        "batter_vs_starter_platoon_factor": round(clamped, 4),
    }


def emit_park_features(park: dict[str, float] | None) -> dict[str, float]:
    if not park:
        return {}
    return {
        "park_factor_hr": round(float(park.get("hr") or 1.0), 4),
        "park_factor_runs": round(float(park.get("r") or 1.0), 4),
        "park_factor_doubles": round(float(park.get("2b") or 1.0), 4),
        "park_factor_singles": round(float(park.get("1b") or 1.0), 4),
        "park_factor_strikeouts": round(float(park.get("so") or 1.0), 4),
        "park_data_complete": float(park.get("_data_complete") or 0.0),
    }


def emit_weather_features(weather: dict[str, Any] | None) -> dict[str, float]:
    if not weather:
        return {}
    out: dict[str, float] = {}
    if isinstance(weather.get("temp_f"), (int, float)):
        out["weather_temp_f"] = round(float(weather["temp_f"]), 2)
    if isinstance(weather.get("wind_speed_mph"), (int, float)):
        out["weather_wind_speed_mph"] = round(float(weather["wind_speed_mph"]), 2)
    if isinstance(weather.get("wind_dir_deg"), (int, float)):
        out["weather_wind_dir_deg"] = round(float(weather["wind_dir_deg"]), 2)
    if isinstance(weather.get("precip_pct"), (int, float)):
        out["weather_precip_pct"] = round(float(weather["precip_pct"]), 2)
    if isinstance(weather.get("humidity_pct"), (int, float)):
        out["weather_humidity_pct"] = round(float(weather["humidity_pct"]), 2)
    out["weather_is_dome"] = 1.0 if weather.get("is_dome") else 0.0
    out["weather_data_complete"] = 1.0
    return out


def emit_lineup_features(lineup_payload: dict[str, Any] | None, mlb_player_id: str | None) -> dict[str, float]:
    """Batting-order position + lineup-confirmation signal.

    Three output shapes:

    1. ``{}`` — no lineup data in the payload at all (pre-lineup window).
    2. ``{"lineup_data_complete": 1.0, "player_in_starting_lineup": 0.0}`` —
       lineup data IS present for at least one game in the payload, but the
       target player is NOT in any starting lineup. That's the scratch/DNP
       signal Smarter #16 uses to suppress (not penalize) the
       recommendation.
    3. ``{"batting_order_position": N, "lineup_data_complete": 1.0,
       "player_in_starting_lineup": 1.0}`` — player IS confirmed in the
       starting lineup at batting-order position ``N``.

    Schema note (per CODEX_REVIEW_NOTES.md item 1): MLB Stats API's
    schedule hydrate puts confirmed lineups under
    ``game.lineups.{homePlayers, awayPlayers}`` as flat ordered arrays of
    person dicts — NOT under ``game.teams.{home,away}.probableLineup``.
    The previous code targeted the wrong path and silently returned an
    empty dict for every real schedule response. We accept either shape
    here so existing fixtures keep working.
    """

    if not lineup_payload or not mlb_player_id:
        return {}
    raw = lineup_payload.get("raw") or {}
    games = raw.get("dates") or []
    if not games:
        return {}

    target_id = str(mlb_player_id)
    lineup_has_data = False

    def _emit_starting(order: int) -> dict[str, float]:
        return {
            "batting_order_position": float(order),
            "lineup_data_complete": 1.0,
            "player_in_starting_lineup": 1.0,
        }

    for date_block in games:
        for game in date_block.get("games") or []:
            # Preferred path — the actual schema MLB Stats API returns.
            game_lineups = game.get("lineups") or {}
            for side_key in ("homePlayers", "awayPlayers"):
                side_lineup = game_lineups.get(side_key) or []
                if side_lineup:
                    lineup_has_data = True
                for idx, slot in enumerate(side_lineup, start=1):
                    slot_id = ""
                    if isinstance(slot, dict):
                        slot_id = str(slot.get("id") or slot.get("personId") or "")
                    else:
                        slot_id = str(slot)
                    if slot_id == target_id:
                        return _emit_starting(idx)

            # Fallback — older fixture shape some tests / mocks rely on.
            for side in ("home", "away"):
                team_block = ((game.get("teams") or {}).get(side) or {})
                lineup = team_block.get("probableLineup") or team_block.get("battingOrder") or []
                if lineup:
                    lineup_has_data = True
                for idx, slot in enumerate(lineup, start=1):
                    if isinstance(slot, dict):
                        slot_id = str(slot.get("id") or slot.get("personId") or "")
                        order = int(slot.get("battingOrder") or idx)
                    else:
                        slot_id = str(slot)
                        order = idx
                    if slot_id == target_id:
                        return _emit_starting(order)

    if lineup_has_data:
        # Smarter #16: lineup data is in the payload AND the player is NOT
        # in any starting lineup. This is the scratch / DNP signal — far
        # stronger than a generic "context missing" penalty. Scoring uses
        # this to suppress the recommendation entirely.
        return {
            "lineup_data_complete": 1.0,
            "player_in_starting_lineup": 0.0,
        }
    return {}


# -----------------------------------------------------------------------------
# Warm summary extension

def warm_mlb_advanced_for_athletes(
    db: Session,
    *,
    mlb_stats_player_ids: Iterable[str],
    season: int,
    pitcher_ids: Iterable[str] | None = None,
    client: MlbStatsClient | None = None,
    savant: BaseballSavantClient | None = None,
    savant_pitcher: BaseballSavantClient | None = None,
    savant_batter: BaseballSavantClient | None = None,
) -> dict[str, int]:
    """Refresh batter + pitcher caches for a list of player IDs.

    ``mlb_stats_player_ids`` warms batter sabermetrics + Statcast batter
    caches; ``pitcher_ids`` warms ``mlb_pitcher_advanced_cache`` and
    ``mlb_statcast_pitcher_cache``. Both lists may overlap (a two-way
    player gets warmed in both halves) — dedup is per-list.

    Statcast clients are split:
    - ``savant_pitcher`` triggers per-pitcher Statcast warming.
    - ``savant_batter`` triggers per-batter Statcast warming.
    - ``savant`` is a backwards-compat shorthand that fills both when the
      more specific kwargs are not supplied.

    Codex round 4 note: the cron now passes ``savant_pitcher`` only so
    growing the prop-subject sidecar list doesn't fan out an O(N) Savant
    fetch budget on the daily warm tick.
    """
    summary = {
        "mlb_batters_attempted": 0,
        "mlb_batters_succeeded": 0,
        "mlb_pitchers_attempted": 0,
        "mlb_pitchers_succeeded": 0,
        "mlb_roster_loaded": 0,
    }
    mlb_client = client or MlbStatsClient()
    pitcher_savant = savant_pitcher or savant
    batter_savant = savant_batter or savant

    roster_result = load_mlb_player_roster(db, season=season, client=mlb_client, allow_network=True)
    summary["mlb_roster_loaded"] = 1 if roster_result.complete else 0

    seen_batters: set[str] = set()
    for raw_id in mlb_stats_player_ids:
        if raw_id is None:
            continue
        player_id = str(raw_id)
        if player_id in seen_batters:
            continue
        seen_batters.add(player_id)
        summary["mlb_batters_attempted"] += 1
        result = load_mlb_batter_advanced(
            db, mlb_player_id=player_id, season=season, client=mlb_client, allow_network=True
        )
        if result.complete and result.cache_status in {"hit", "miss"}:
            summary["mlb_batters_succeeded"] += 1
        if batter_savant is not None:
            load_mlb_statcast_batter(
                db, mlb_player_id=player_id, season=season, client=batter_savant, allow_network=True
            )

    seen_pitchers: set[str] = set()
    for raw_id in pitcher_ids or []:
        if raw_id is None:
            continue
        player_id = str(raw_id)
        if player_id in seen_pitchers:
            continue
        seen_pitchers.add(player_id)
        summary["mlb_pitchers_attempted"] += 1
        pitcher_result = load_mlb_pitcher_advanced(
            db, mlb_player_id=player_id, season=season, client=mlb_client, allow_network=True
        )
        if pitcher_result.complete and pitcher_result.cache_status in {"hit", "miss"}:
            summary["mlb_pitchers_succeeded"] += 1
        if pitcher_savant is not None:
            load_mlb_statcast_pitcher(
                db, mlb_player_id=player_id, season=season, client=pitcher_savant, allow_network=True
            )

    return summary
