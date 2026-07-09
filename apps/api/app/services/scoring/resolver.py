"""Prop-subject resolution + per-family heuristic profiles.

Extracted from ``scoring/__init__.py`` as part of R1 phase 2. The
resolver owns the ESPN player-search + gamelog caches (with bug
#13's team-hint-aware key) plus the optional advanced-stats fan-out
into NBA-Stats / MLB-Stats / Statcast. Pulling it out of the
3,000-line kernel makes the "how do we fetch a player's recent
context?" surface independently reviewable.

Module contents:
- ``HeuristicProfile`` + per-family ``SINGLE_HEURISTIC_PROFILES``
  + ``_profile_for_single_family`` lookup.
- ``PropStatsResolver`` — the cache-or-fetch resolver class.
- ``_merge_cache_status`` — most-degraded-wins combiner for the
  per-source statuses the resolver emits.
- ``_team_abbreviation_from_player`` + ``warm_prop_context_cache``
  helpers.

Cross-package imports kept LAZY where they reach into sport-specific
helpers (NBA-Stats / MLB-Stats / Statcast loaders) — those modules
each pull in their own DB models / clients, and eager-loading them
here would re-create the same import-time blast radius the original
kernel had.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.espn import EspnPublicClient
from app.config import get_settings
from app.models import (
    EspnPlayerGamelogCache,
    EspnPlayerSearchCache,
    Market,
)
from app.services.predictions import OPEN_MARKET_STATUSES
from app.services.scoring.types import (
    PropResolverStats,
    ResolvedPropSubject,
)
from app.services.stats_query import _build_game_logs, default_season_for_sport

__all__ = [
    "HeuristicProfile",
    "SINGLE_HEURISTIC_PROFILES",
    "_profile_for_single_family",
    "PropStatsResolver",
    "_merge_cache_status",
    "_team_abbreviation_from_player",
    "warm_prop_context_cache",
]


@dataclass(frozen=True, slots=True)
class HeuristicProfile:
    family_key: str
    thin_sample_target: int
    thin_sample_max_penalty: float
    stale_after_days: int
    stale_max_penalty: float
    market_disagreement_threshold: float
    market_disagreement_max_penalty: float
    volatility_max_penalty: float


SINGLE_HEURISTIC_PROFILES = {
    "nba_singles": HeuristicProfile("nba_singles", 10, 0.10, 4, 0.04, 0.18, 0.05, 0.0),
    "mlb_singles": HeuristicProfile("mlb_singles", 10, 0.10, 5, 0.05, 0.18, 0.05, 0.0),
    "nba_props": HeuristicProfile("nba_props", 8, 0.11, 5, 0.05, 0.16, 0.06, 0.09),
    "mlb_props": HeuristicProfile("mlb_props", 8, 0.11, 6, 0.05, 0.16, 0.06, 0.08),
    # WNBA profiles ship with NBA values as the starting point (same
    # game length, same per-game stat surface, similar variance
    # characteristics). Once WNBA settled rows accumulate, Smarter #28
    # backtest output can tune these per-family — same mechanism NBA
    # and MLB use today.
    "wnba_singles": HeuristicProfile("wnba_singles", 10, 0.10, 4, 0.04, 0.18, 0.05, 0.0),
    "wnba_props": HeuristicProfile("wnba_props", 8, 0.11, 5, 0.05, 0.16, 0.06, 0.09),
    # Smarter NFL PR 7 — weekly cadence: stale_after_days 8-9 (one game
    # a week means a 6-day-old log is CURRENT), thin-sample targets
    # sized for 17-game seasons. Tuned by the PR 9 backtest.
    "nfl_singles": HeuristicProfile("nfl_singles", 8, 0.10, 8, 0.05, 0.18, 0.05, 0.0),
    "nfl_props": HeuristicProfile("nfl_props", 6, 0.12, 9, 0.05, 0.16, 0.06, 0.10),
}


def _profile_for_single_family(family_key: str) -> HeuristicProfile:
    return SINGLE_HEURISTIC_PROFILES.get(family_key, SINGLE_HEURISTIC_PROFILES["nba_singles"])


class PropStatsResolver:
    def __init__(
        self,
        db: Session,
        espn_client: EspnPublicClient | None = None,
        *,
        allow_network: bool = True,
        now: datetime | None = None,
    ) -> None:
        self.db = db
        self.espn_client = espn_client or EspnPublicClient()
        self.allow_network = allow_network
        self.now = now
        self._cache: dict[tuple[str, str, str], ResolvedPropSubject] = {}
        self.stats = PropResolverStats()

    def _now(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def _normalize_query(self, value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    def _coerce_utc(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _search_ttl(self) -> timedelta:
        return timedelta(hours=get_settings().espn_player_search_cache_hours)

    def _gamelog_ttl(self, sport_key: str) -> timedelta:
        settings = get_settings()
        sport = sport_key.upper()
        if sport == "NBA":
            return timedelta(minutes=settings.nba_prop_gamelog_cache_minutes)
        # WNBA has its own TTL setting from PR 1; without this branch
        # WNBA stale-context status would lag the MLB fallback (codex
        # PR 4 review Medium).
        if sport == "WNBA":
            return timedelta(minutes=settings.wnba_prop_gamelog_cache_minutes)
        # NFL games are weekly — its TTL (6h default) is deliberately
        # looser than the daily sports' (Smarter NFL PR 1).
        if sport == "NFL":
            return timedelta(minutes=settings.nfl_prop_gamelog_cache_minutes)
        return timedelta(minutes=settings.mlb_prop_gamelog_cache_minutes)

    def _load_player_search(
        self,
        sport_key: str,
        query: str,
        *,
        team_hint: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        # Bug #13: include team_hint in the cache key when provided so that
        # same-name players on different teams (e.g. two "John Smith"s)
        # don't poison each other's cache row.
        #
        # Codex PR #35 P2: never reuse the bare-query row as a fallback
        # write target — that just rotates the poisoning to whichever
        # caller landed last. A legacy bare row IS allowed as a read-only
        # cache HIT, but only if its cached team_name matches the hint;
        # fresh network fetches always land on the hinted key.
        from app.clients.espn import _team_hint_matches_subtitle as _team_hint_matches

        bare_query = self._normalize_query(query)
        if team_hint:
            normalized_query = f"{bare_query}|{str(team_hint).strip().upper()}"
        else:
            normalized_query = bare_query
        now = self._now()
        hinted_row = self.db.scalar(
            select(EspnPlayerSearchCache).where(
                EspnPlayerSearchCache.sport_key == sport_key,
                EspnPlayerSearchCache.query_normalized == normalized_query,
            )
        )
        read_row = hinted_row
        if read_row is None and team_hint:
            # Look for a legacy bare-query row and treat it as a read-only
            # hit when its payload matches the team hint. Never write
            # through to it on miss/refresh.
            bare_row = self.db.scalar(
                select(EspnPlayerSearchCache).where(
                    EspnPlayerSearchCache.sport_key == sport_key,
                    EspnPlayerSearchCache.query_normalized == bare_query,
                )
            )
            bare_team = str((bare_row.payload or {}).get("team_name") or "") if bare_row else ""
            if bare_row is not None and _team_hint_matches(str(team_hint), bare_team, sport_key):
                read_row = bare_row
        expires_at = self._coerce_utc(read_row.expires_at) if read_row else None
        if read_row and expires_at and expires_at > now:
            self.stats.player_search_cache_hits += 1
            return dict(read_row.payload or {}), "hit"
        if read_row and not self.allow_network:
            self.stats.player_search_cache_hits += 1
            return dict(read_row.payload or {}), "hit"
        if not self.allow_network:
            self.stats.player_search_cache_misses += 1
            raise LookupError(f"No cached ESPN player search found for {sport_key}:{query}")

        self.stats.player_search_cache_misses += 1
        try:
            payload = self.espn_client.search_player(query, sport_key=sport_key, team_hint=team_hint)
        except Exception:
            if read_row:
                self.stats.player_search_cache_hits += 1
                return dict(read_row.payload or {}), "hit"
            raise

        # Always write to the hinted key (or bare key when no hint was
        # provided). Don't mutate the legacy bare row even if we adopted
        # it for reading earlier.
        if hinted_row is None:
            hinted_row = EspnPlayerSearchCache(
                sport_key=sport_key,
                query_normalized=normalized_query,
            )
            self.db.add(hinted_row)
        hinted_row.payload = dict(payload)
        hinted_row.cached_at = now
        hinted_row.expires_at = now + self._search_ttl()
        self.db.flush()
        return payload, "miss"

    def _load_player_gamelog(
        self, sport_key: str, athlete_id: str, season: int,
    ) -> tuple[dict[str, Any], str, datetime | None]:
        """Return the cached gamelog payload, the cache status, and
        the row's ``cached_at`` timestamp.

        Architecture #5 added ``cached_at`` to the return tuple so
        ``ResolvedPropSubject.gamelog_cached_at`` can feed
        ``FeatureGroupSnapshot.fresh_at`` for the ``nba_workload``
        group. ``None`` is returned only when there is no cache row
        AND the network fetch produced a fresh write (in which case
        ``now`` IS the cached_at — see the post-fetch branch below).
        """
        now = self._now()
        row = self.db.scalar(
            select(EspnPlayerGamelogCache).where(
                EspnPlayerGamelogCache.sport_key == sport_key,
                EspnPlayerGamelogCache.athlete_id == athlete_id,
                EspnPlayerGamelogCache.season == season,
            )
        )
        expires_at = self._coerce_utc(row.expires_at) if row else None
        if row and expires_at and expires_at > now:
            self.stats.gamelog_cache_hits += 1
            # ``_coerce_utc(None)`` returns ``None`` (matches the
            # codebase convention); a pre-migration row with
            # ``cached_at=NULL`` round-trips as fresh_at-None →
            # freshness opt-out for that row (no penalty fires).
            return dict(row.payload or {}), "hit", self._coerce_utc(row.cached_at)
        if row and not self.allow_network:
            self.stats.gamelog_cache_hits += 1
            row_cached_at = self._coerce_utc(row.cached_at)
            if expires_at and expires_at <= now:
                self.stats.stale_gamelog_fallbacks += 1
                return dict(row.payload or {}), "stale", row_cached_at
            return dict(row.payload or {}), "hit", row_cached_at
        if not self.allow_network:
            self.stats.gamelog_cache_misses += 1
            raise LookupError(f"No cached ESPN gamelog found for {sport_key}:{athlete_id}:{season}")

        self.stats.gamelog_cache_misses += 1
        try:
            payload = self.espn_client.fetch_player_gamelog(sport_key, athlete_id, season)
        except Exception:
            if row:
                self.stats.stale_gamelog_fallbacks += 1
                return dict(row.payload or {}), "stale", self._coerce_utc(row.cached_at)
            raise

        if row is None:
            row = EspnPlayerGamelogCache(
                sport_key=sport_key,
                athlete_id=athlete_id,
                season=season,
            )
            self.db.add(row)
        row.payload = dict(payload)
        row.cached_at = now
        row.expires_at = now + self._gamelog_ttl(sport_key)
        self.db.flush()
        # Just-fetched: the wire-format cached_at IS now.
        return payload, "miss", now

    def resolve(self, sport_key: str, subject_name: str, team_hint: str | None = None) -> ResolvedPropSubject:
        key = (sport_key, subject_name.lower(), (team_hint or "").upper())
        cached = self._cache.get(key)
        if cached:
            return cached

        player, player_cache_status = self._load_player_search(sport_key, subject_name, team_hint=team_hint)
        season = default_season_for_sport(sport_key)
        gamelog_payload, gamelog_cache_status, gamelog_cached_at = self._load_player_gamelog(
            sport_key, player["athlete_id"], season,
        )
        game_logs = _build_game_logs(sport_key, gamelog_payload)
        advanced_payload, advanced_status, resolved_ids = self._load_advanced(sport_key, player, season)
        resolved = ResolvedPropSubject(
            sport_key=sport_key,
            athlete_id=player["athlete_id"],
            display_name=player["display_name"],
            team_name=player.get("team_name"),
            season=season,
            game_logs=game_logs,
            player_search_cache_status=player_cache_status,
            gamelog_cache_status=gamelog_cache_status,
            context_stale=gamelog_cache_status == "stale",
            advanced_payload=advanced_payload,
            advanced_cache_status=advanced_status,
            nba_stats_id=resolved_ids.get("nba_stats_id"),
            mlb_stats_id=resolved_ids.get("mlb_stats_id"),
            gamelog_cached_at=gamelog_cached_at,
        )
        self._cache[key] = resolved
        self.stats.prop_subjects_warmed += 1
        return resolved

    def _load_advanced(
        self, sport_key: str, player: dict[str, Any], season: int
    ) -> tuple[dict[str, Any], str, dict[str, str]]:
        """Load sport-specific advanced stats and surface resolved player IDs.

        Returns ``(payload, cache_status, resolved_ids)`` where ``resolved_ids``
        contains optional ``nba_stats_id`` / ``mlb_stats_id`` so the caller can
        thread them onto ``ResolvedPropSubject`` and skip the
        ``EspnPlayerSearchCache`` linear scan downstream.
        """
        if not get_settings().advanced_stats_enabled:
            return {}, "disabled", {}
        if sport_key.upper() == "NBA":
            return self._load_nba_advanced(player, season)
        if sport_key.upper() == "MLB":
            return self._load_mlb_advanced(player, season)
        return {}, "unsupported_sport", {}

    def _load_nba_advanced(
        self, player: dict[str, Any], season: int
    ) -> tuple[dict[str, Any], str, dict[str, str]]:
        from app.services.advanced_stats import (
            load_nba_advanced,
            resolve_nba_stats_player_id,
        )

        nba_stats_id = (player or {}).get("nba_stats_id")
        if not nba_stats_id:
            nba_stats_id = resolve_nba_stats_player_id(
                self.db,
                espn_athlete_id=(player or {}).get("athlete_id"),
                full_name=(player or {}).get("display_name") or "",
                team_abbreviation=_team_abbreviation_from_player(player),
                season=season,
                allow_network=self.allow_network,
            )
        if not nba_stats_id:
            return {}, "missing_id", {}

        result = load_nba_advanced(
            self.db,
            nba_stats_player_id=str(nba_stats_id),
            season=season,
            allow_network=self.allow_network,
            now=self.now,
        )
        return dict(result.payload or {}), result.cache_status, {"nba_stats_id": str(nba_stats_id)}

    def _load_mlb_advanced(
        self, player: dict[str, Any], season: int
    ) -> tuple[dict[str, Any], str, dict[str, str]]:
        from app.services.mlb_advanced import (
            load_mlb_batter_advanced,
            load_mlb_statcast_batter,
            resolve_mlb_stats_player_id,
        )

        mlb_stats_id = (player or {}).get("mlb_stats_id")
        if not mlb_stats_id:
            mlb_stats_id = resolve_mlb_stats_player_id(
                self.db,
                espn_athlete_id=(player or {}).get("athlete_id"),
                full_name=(player or {}).get("display_name") or "",
                team_abbreviation=_team_abbreviation_from_player(player),
                season=season,
                allow_network=self.allow_network,
            )
        if not mlb_stats_id:
            return {}, "missing_id", {}

        sabermetrics_result = load_mlb_batter_advanced(
            self.db,
            mlb_player_id=str(mlb_stats_id),
            season=season,
            allow_network=self.allow_network,
            now=self.now,
        )
        statcast_result = load_mlb_statcast_batter(
            self.db,
            mlb_player_id=str(mlb_stats_id),
            season=season,
            allow_network=self.allow_network,
            now=self.now,
        )
        combined = {
            "batter_sabermetrics": sabermetrics_result.payload,
            "batter_statcast": statcast_result.payload,
        }
        return (
            combined,
            _merge_cache_status(sabermetrics_result.cache_status, statcast_result.cache_status),
            {"mlb_stats_id": str(mlb_stats_id)},
        )


_CACHE_STATUS_PRIORITY: dict[str, int] = {
    "stale": 4,
    "skipped": 3,
    "miss": 2,
    "missing_id": 2,
    "hit": 1,
    "dome": 0,
    "disabled": 0,
    "unsupported_sport": 0,
}


def _merge_cache_status(*statuses: str) -> str:
    """Combine multiple per-source cache statuses using a 'most-degraded wins'
    rule. ``stale`` outranks ``skipped`` outranks ``miss`` outranks ``hit`` —
    so a partial-miss result is never reported as ``hit``. Unknown statuses
    pass through unchanged.
    """
    if not statuses:
        return "miss"
    return max(statuses, key=lambda s: _CACHE_STATUS_PRIORITY.get(s, 5))


def _team_abbreviation_from_player(player: dict[str, Any] | None) -> str | None:
    """Best-effort team abbreviation extraction from an ESPN search payload.

    ESPN's player search returns ``team_name`` ("Los Angeles Lakers") and
    sometimes ``team_abbreviation``. We try a few fields and fall back to
    ``None`` when nothing matches — name-only resolution is the fallback.
    """
    if not player:
        return None
    raw_team = player.get("team_abbreviation") or ""
    if raw_team:
        return raw_team.upper()
    nested_raw = (player.get("raw") or {}).get("subtitle") or ""
    if nested_raw:
        return None  # subtitle is the team name; downstream uses name-only fallback
    return None


def warm_prop_context_cache(
    db: Session,
    resolver: PropStatsResolver | None = None,
) -> dict[str, int]:
    active_resolver = resolver or PropStatsResolver(db)
    unique_subjects: dict[tuple[str, str, str], tuple[str, str, str | None]] = {}
    markets = db.scalars(
        select(Market).where(Market.status.in_(tuple(OPEN_MARKET_STATUSES)))
    ).all()
    for market in markets:
        raw_data = market.raw_data or {}
        if raw_data.get("copilot_market_family") != "player_prop":
            continue
        sport_key = str(market.sport_key or "")
        subject_name = str(raw_data.get("copilot_subject_name") or "").strip()
        team_hint = str(raw_data.get("copilot_subject_team") or "").strip() or None
        if not sport_key or not subject_name:
            continue
        key = (sport_key, subject_name.lower(), (team_hint or "").upper())
        unique_subjects[key] = (sport_key, subject_name, team_hint)

    for sport_key, subject_name, team_hint in unique_subjects.values():
        try:
            active_resolver.resolve(sport_key, subject_name, team_hint=team_hint)
        except Exception:
            continue
    return active_resolver.stats.as_dict()
