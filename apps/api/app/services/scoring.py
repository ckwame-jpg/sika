from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import exp, tanh
from statistics import NormalDist, pstdev
from typing import Any, Literal

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, joinedload

from app.clients.espn import EspnPublicClient
from app.config import get_settings
from app.models import (
    EspnPlayerGamelogCache,
    EspnPlayerSearchCache,
    Event,
    EventParticipant,
    Market,
    MarketSnapshot,
    Prediction,
    Recommendation,
    SignalSnapshot,
)
from app.services.ml.lineage import HEURISTIC_SINGLE_MODEL
from app.services.ml.runtime import run_serving_inference
from app.services.market_support import infer_yes_label, market_metadata
from app.services.model_families import single_family_key
from app.services.parlays import ParlayCandidateInput, capture_parlay_artifacts, clear_active_parlay_watchlist
from app.services.predictions import MODEL_NAME, OPEN_MARKET_STATUSES, capture_prediction
from app.services.stats_query import _build_game_logs, default_season_for_sport
from app.services.watchlist_coverage import (
    CURRENT_WATCHLIST_SPORTS,
    current_watchlist_event_ids,
    is_current_watchlist_market,
    latest_snapshot_by_market_id,
)
from app.sports.base import alias_tokens


@dataclass(slots=True)
class ResolvedPropSubject:
    sport_key: str
    athlete_id: str
    display_name: str
    team_name: str | None
    season: int
    game_logs: list[dict[str, Any]]
    player_search_cache_status: str = "miss"
    gamelog_cache_status: str = "miss"
    context_stale: bool = False
    advanced_payload: dict[str, Any] = field(default_factory=dict)
    advanced_cache_status: str = "miss"
    # Cross-source player IDs resolved during _load_advanced. Threaded
    # through here so downstream emitters (long-tail NBA, MLB lineup) can
    # look up per-player records in O(1) without re-scanning the search
    # cache. ``None`` until the resolver runs and finds a match.
    nba_stats_id: str | None = None
    mlb_stats_id: str | None = None


@dataclass(slots=True)
class PropResolverStats:
    prop_subjects_warmed: int = 0
    player_search_cache_hits: int = 0
    player_search_cache_misses: int = 0
    gamelog_cache_hits: int = 0
    gamelog_cache_misses: int = 0
    stale_gamelog_fallbacks: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prop_subjects_warmed": self.prop_subjects_warmed,
            "player_search_cache_hits": self.player_search_cache_hits,
            "player_search_cache_misses": self.player_search_cache_misses,
            "gamelog_cache_hits": self.gamelog_cache_hits,
            "gamelog_cache_misses": self.gamelog_cache_misses,
            "stale_gamelog_fallbacks": self.stale_gamelog_fallbacks,
        }


@dataclass(slots=True)
class ScoredRecommendation:
    recommendation: Recommendation | None
    signal: SignalSnapshot
    metadata: dict[str, Any]


# Slice 6: ``_score_watchlist_markets_batch`` was previously responsible for
# both producing scored recommendations *and* persisting them via
# ``db.add(scored.signal)`` + ``capture_prediction(...)``. That made the
# scoring kernel impossible to test in isolation: any unit test that wanted
# to exercise the scoring math also got a database write. The split now
# returns a list of ``ScoredWatchlistCapture`` records describing what the
# persist step *would* do, and a separate ``_persist_scored_watchlist_captures``
# helper handles the side effects. The ``stage_*_watchlist_batch`` wrappers
# call the two in sequence so external behavior is unchanged.
@dataclass(slots=True)
class ScoredWatchlistCapture:
    market: Market
    scored: ScoredRecommendation
    capture_scope: Literal["recommendation", "coverage"] | None


@dataclass(slots=True)
class WatchlistGenerationSummary:
    recommendation_count: int = 0
    prediction_count: int = 0
    parlay_recommendation_count: int = 0
    parlay_prediction_count: int = 0
    loaded_candidate_market_count: int = 0
    filtered_candidate_market_count: int = 0
    scored_market_count: int = 0
    coverage_prediction_count: int = 0
    heuristic_longshots_suppressed: int = 0
    inverse_winner_duplicates_collapsed: int = 0
    combo_prop_candidates_emitted: int = 0
    combo_prop_candidates_suppressed: int = 0
    critical_context_suppressed: int = 0
    candidate_filter_reason_counts: dict[str, int] = field(default_factory=dict)
    outcome_reason_counts: dict[str, int] = field(default_factory=dict)
    quality_tier_counts: dict[str, int] = field(default_factory=dict)


def _record_scorer_outcome(summary: WatchlistGenerationSummary, reason: str) -> None:
    summary.outcome_reason_counts[reason] = summary.outcome_reason_counts.get(reason, 0) + 1


def _record_candidate_filter(summary: WatchlistGenerationSummary, reason: str) -> None:
    summary.filtered_candidate_market_count += 1
    summary.candidate_filter_reason_counts[reason] = summary.candidate_filter_reason_counts.get(reason, 0) + 1


def _merge_count_maps(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = dict(left)
    for key, value in right.items():
        merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


def _scoring_none_reason(market: Market) -> str:
    if market.event is None:
        return "mapping_failed"
    metadata = _market_metadata(market)
    family = str(metadata.get("copilot_market_family") or "")
    if family not in {"winner", "game_line", "player_prop"}:
        return "unsupported_shape"
    if family == "player_prop":
        subject_name = str(metadata.get("copilot_subject_name") or "").strip()
        stat_key = str(metadata.get("copilot_stat_key") or "").strip()
        threshold = float(metadata.get("copilot_threshold") or 0.0)
        if not subject_name or not stat_key or threshold <= 0:
            return "unsupported_shape"
        return "prop_context_missing"
    return "scoring_returned_none"


def _suppression_outcome_reason(scored: ScoredRecommendation, *, current_watchlist_market: bool) -> str:
    diagnostics = dict(scored.signal.scoring_diagnostics or {})
    suppression_reasons = {str(value) for value in list(diagnostics.get("suppression_reasons") or [])}
    if "critical_market_snapshot_missing" in suppression_reasons:
        return "missing_snapshot"
    if "player_not_in_starting_lineup" in suppression_reasons:
        return "suppressed_player_not_in_starting_lineup"
    if "no_side_not_actionable_on_kalshi" in suppression_reasons:
        return "suppressed_no_side_not_actionable"
    if "min_edge" in suppression_reasons or "yes_side_negative_edge" in suppression_reasons:
        return "suppressed_min_edge"
    if "min_confidence" in suppression_reasons:
        return "suppressed_min_confidence"
    if not current_watchlist_market:
        return "not_current_slate"
    return "coverage"


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
}


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
        if sport_key.upper() == "NBA":
            return timedelta(minutes=settings.nba_prop_gamelog_cache_minutes)
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

    def _load_player_gamelog(self, sport_key: str, athlete_id: str, season: int) -> tuple[dict[str, Any], str]:
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
            return dict(row.payload or {}), "hit"
        if row and not self.allow_network:
            self.stats.gamelog_cache_hits += 1
            if expires_at and expires_at <= now:
                self.stats.stale_gamelog_fallbacks += 1
                return dict(row.payload or {}), "stale"
            return dict(row.payload or {}), "hit"
        if not self.allow_network:
            self.stats.gamelog_cache_misses += 1
            raise LookupError(f"No cached ESPN gamelog found for {sport_key}:{athlete_id}:{season}")

        self.stats.gamelog_cache_misses += 1
        try:
            payload = self.espn_client.fetch_player_gamelog(sport_key, athlete_id, season)
        except Exception:
            if row:
                self.stats.stale_gamelog_fallbacks += 1
                return dict(row.payload or {}), "stale"
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
        return payload, "miss"

    def resolve(self, sport_key: str, subject_name: str, team_hint: str | None = None) -> ResolvedPropSubject:
        key = (sport_key, subject_name.lower(), (team_hint or "").upper())
        cached = self._cache.get(key)
        if cached:
            return cached

        player, player_cache_status = self._load_player_search(sport_key, subject_name, team_hint=team_hint)
        season = default_season_for_sport(sport_key)
        gamelog_payload, gamelog_cache_status = self._load_player_gamelog(sport_key, player["athlete_id"], season)
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


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _profile_for_single_family(family_key: str) -> HeuristicProfile:
    return SINGLE_HEURISTIC_PROFILES.get(family_key, SINGLE_HEURISTIC_PROFILES["nba_singles"])


def _market_implied_yes_price(snapshot: MarketSnapshot | None) -> float | None:
    if snapshot is None:
        return None
    if snapshot.yes_ask is not None:
        return float(snapshot.yes_ask)
    if snapshot.last_price is not None:
        return float(snapshot.last_price)
    return None


def _staleness_penalty(stale_days: float | None, profile: HeuristicProfile) -> float:
    if stale_days is None or stale_days <= profile.stale_after_days:
        return 0.0
    overflow = min(stale_days - profile.stale_after_days, float(profile.stale_after_days + 2))
    return round((overflow / max(profile.stale_after_days + 2, 1)) * profile.stale_max_penalty, 4)


def _sample_penalty(sample_size: int, profile: HeuristicProfile) -> float:
    if sample_size >= profile.thin_sample_target:
        return 0.0
    deficit = profile.thin_sample_target - max(sample_size, 0)
    return round((deficit / max(profile.thin_sample_target, 1)) * profile.thin_sample_max_penalty, 4)


def _market_disagreement_penalty(
    disagreement: float,
    profile: HeuristicProfile,
    *,
    sample_penalty: float,
) -> float:
    if disagreement <= profile.market_disagreement_threshold:
        return 0.0
    overflow = min(disagreement - profile.market_disagreement_threshold, 0.25)
    reliability_factor = 0.5 + min(sample_penalty / max(profile.thin_sample_max_penalty, 0.001), 0.5)
    return round((overflow / 0.25) * profile.market_disagreement_max_penalty * reliability_factor, 4)


def _mean_abs_deviation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = sum(values) / len(values)
    return sum(abs(value - average) for value in values) / len(values)


def _prop_volatility_penalty(values: list[float], threshold: float, profile: HeuristicProfile) -> float:
    if len(values) < 3 or profile.volatility_max_penalty <= 0:
        return 0.0
    mad = _mean_abs_deviation(values)
    threshold_difficulty = 1.0 / max(abs(threshold) + 1.0, 1.0)
    scaled = min(mad * threshold_difficulty, 1.0)
    return round(scaled * profile.volatility_max_penalty, 4)


def _days_since_participant_game(db: Session, participant_id: int, before: datetime | None) -> float | None:
    if before is None:
        return None
    latest = db.scalar(
        select(Event.starts_at)
        .join(EventParticipant, Event.id == EventParticipant.event_id)
        .where(EventParticipant.participant_id == participant_id, Event.starts_at < before, Event.status == "completed")
        .order_by(desc(Event.starts_at))
        .limit(1)
    )
    if latest is None:
        return None
    return max((before - latest).total_seconds() / 86400.0, 0.0)


def _days_since_latest_log(game_logs: list[dict[str, Any]], before: datetime | None) -> float | None:
    if not game_logs:
        return None
    if before is None:
        return None
    game_date = game_logs[0].get("game_date")
    if not isinstance(game_date, datetime):
        return None
    if before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)
    if game_date.tzinfo is None:
        game_date = game_date.replace(tzinfo=timezone.utc)
    return max((before - game_date).total_seconds() / 86400.0, 0.0)


def _recent_participant_results(db: Session, participant_id: int, before: datetime | None, limit: int = 10) -> list[tuple[float, str | None]]:
    if before is None:
        return []
    rows = db.execute(
        select(EventParticipant.score, EventParticipant.result)
        .join(Event)
        .where(EventParticipant.participant_id == participant_id, Event.starts_at < before, Event.status == "completed")
        .order_by(desc(Event.starts_at))
        .limit(limit)
    ).all()
    return [(score or 0.0, result) for score, result in rows]


def _win_rate(results: list[tuple[float, str | None]]) -> float:
    if not results:
        return 0.5
    wins = sum(1 for _, result in results if result == "win")
    return wins / len(results)


def _avg_score(results: list[tuple[float, str | None]]) -> float:
    if not results:
        return 0.0
    return sum(score for score, _ in results) / len(results)


def _market_payload(market: Market | None) -> dict[str, Any]:
    if market is None:
        return {}
    return {
        "ticker": market.ticker,
        "title": market.title,
        "event_ticker": market.event_ticker,
        "series_ticker": market.series_ticker,
        **(market.raw_data or {}),
    }


def _market_metadata(market: Market | None) -> dict[str, Any]:
    if not market:
        return {}
    raw_data = market.raw_data or {}
    if raw_data.get("copilot_market_kind"):
        return dict(raw_data)
    payload = _market_payload(market)
    return market_metadata(payload) or dict(raw_data)


def _token_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    shared = left & right
    if not shared:
        return 0.0
    strong_shared = [token for token in shared if len(token) >= 4]
    return min(1.0, (len(shared) * 0.15) + (len(strong_shared) * 0.2))


def _market_yes_entry(event: Event, market: Market) -> EventParticipant | None:
    payload = _market_payload(market)
    market_kind = str((_market_metadata(market) or {}).get("copilot_market_kind") or "")
    if market_kind not in {"game_winner", "first_five_winner"}:
        return None

    yes_label = infer_yes_label(payload)
    if not yes_label or yes_label.lower() == "tie":
        return None

    yes_tokens = alias_tokens(yes_label)
    best_entry = None
    best_score = 0.0
    for entry in event.participants:
        participant = entry.participant
        score = _token_score(yes_tokens, alias_tokens(participant.display_name, participant.short_name))
        if score > best_score:
            best_score = score
            best_entry = entry
    if best_score < 0.15:
        return None
    return best_entry


def _competition_from_event(event: Event) -> dict[str, Any]:
    raw = event.raw_data or {}
    return ((raw.get("raw") or {}).get("competitions") or [{}])[0]


def _event_venue_context(event: Event) -> dict[str, Any]:
    venue = _competition_from_event(event).get("venue") or {}
    address = venue.get("address") or {}
    return {
        "venue_name": venue.get("fullName"),
        "venue_city": address.get("city"),
        "venue_state": address.get("state"),
        "venue_indoor": venue.get("indoor"),
    }


def _competitor_for_role(event: Event, role: str) -> dict[str, Any]:
    competitors = _competition_from_event(event).get("competitors") or []
    expected = "home" if role in {"home", "competitor_1"} else "away"
    return next((item for item in competitors if item.get("homeAway") == expected), {})


def _parse_first_five_runs(event: Event, role: str) -> tuple[float | None, float | None]:
    home = _competitor_for_role(event, "home")
    away = _competitor_for_role(event, "away")
    home_lines = home.get("linescores") or []
    away_lines = away.get("linescores") or []
    if not home_lines or not away_lines:
        return None, None

    def total(lines: list[dict[str, Any]]) -> float:
        return sum(float(item.get("value") or 0.0) for item in lines if int(item.get("period") or 0) <= 5)

    home_total = total(home_lines)
    away_total = total(away_lines)
    if role in {"home", "competitor_1"}:
        return home_total, away_total
    return away_total, home_total


def _recent_first_five_results(db: Session, participant_id: int, before: datetime | None, limit: int = 10) -> list[tuple[float, float, str]]:
    if before is None:
        return []
    rows = db.execute(
        select(Event, EventParticipant.role)
        .join(EventParticipant, Event.id == EventParticipant.event_id)
        .where(EventParticipant.participant_id == participant_id, Event.starts_at < before, Event.status == "completed")
        .order_by(desc(Event.starts_at))
        .limit(limit)
    ).all()
    results: list[tuple[float, float, str]] = []
    for event, role in rows:
        team_runs, opp_runs = _parse_first_five_runs(event, role)
        if team_runs is None or opp_runs is None:
            continue
        diff = team_runs - opp_runs
        outcome = "win" if diff > 0 else "loss" if diff < 0 else "push"
        results.append((team_runs, diff, outcome))
    return results


def _fractional_win_rate(results: list[tuple[float, float, str]]) -> float:
    if not results:
        return 0.5
    score = 0.0
    for _, _, outcome in results:
        if outcome == "win":
            score += 1.0
        elif outcome == "push":
            score += 0.5
    return score / len(results)


def _avg_first_five_runs(results: list[tuple[float, float, str]]) -> float:
    if not results:
        return 0.0
    return sum(runs for runs, _, _ in results) / len(results)


def _avg_first_five_diff(results: list[tuple[float, float, str]]) -> float:
    if not results:
        return 0.0
    return sum(diff for _, diff, _ in results) / len(results)


def _games_in_recent_window(db: Session, participant_id: int, before: datetime | None, *, days: int) -> int:
    if before is None:
        return 0
    window_start = before - timedelta(days=days)
    count = db.scalar(
        select(func.count())
        .select_from(EventParticipant)
        .join(Event, Event.id == EventParticipant.event_id)
        .where(
            EventParticipant.participant_id == participant_id,
            Event.starts_at < before,
            Event.starts_at >= window_start,
        )
    )
    return int(count or 0)


def _latest_home_state(db: Session, participant_id: int, before: datetime | None) -> bool | None:
    if before is None:
        return None
    return db.scalar(
        select(EventParticipant.is_home)
        .join(Event, Event.id == EventParticipant.event_id)
        .where(EventParticipant.participant_id == participant_id, Event.starts_at < before)
        .order_by(desc(Event.starts_at))
        .limit(1)
    )


def _schedule_context(db: Session, participant_id: int, before: datetime | None) -> dict[str, Any]:
    if before is None:
        return {
            "days_rest": None,
            "games_last_3": 0,
            "games_last_4": 0,
            "games_last_5": 0,
            "games_last_7": 0,
            "back_to_back": False,
            "is_third_in_four": False,
            "is_fourth_in_six": False,
            "last_home_state": None,
            "last_game_away": None,
        }
    days_rest = _days_since_participant_game(db, participant_id, before)
    games_last_3 = _games_in_recent_window(db, participant_id, before, days=3)
    games_last_4 = _games_in_recent_window(db, participant_id, before, days=4)
    games_last_5 = _games_in_recent_window(db, participant_id, before, days=5)
    games_last_7 = _games_in_recent_window(db, participant_id, before, days=7)
    last_home_state = _latest_home_state(db, participant_id, before)
    # Smarter #10 derived flags:
    # - 3rd-in-4: tonight is the 3rd game over a 4-night window. That is,
    #   2 prior games sat inside the last 3 days before tonight.
    # - 4th-in-6: tonight is the 4th game over a 6-night window. That is,
    #   3 prior games sat inside the last 5 days before tonight.
    # Both checks use ``>=`` rather than ``==`` so that supersets (e.g.
    # corrupted or rescheduled-doubleheader data showing 3+ games in 3
    # nights) still trip the suppressor — the more games in the window,
    # the more fatigue. ``_nba_rest_factor`` then picks the strongest
    # suppression case.
    is_third_in_four = games_last_3 >= 2
    is_fourth_in_six = games_last_5 >= 3
    # Smarter #10 travel proxy (Phase 1): expose only whether the prior
    # game was away. The travel factor consults this alongside today's
    # home/away (sourced from EventParticipant.is_home) to decide whether
    # a continuous road-trip suppression applies. Phase 2 will replace
    # with mileage from venue lat/lons (Smarter #15 pattern).
    last_game_away = None if last_home_state is None else (not last_home_state)
    return {
        "days_rest": round(days_rest, 3) if days_rest is not None else None,
        "games_last_3": games_last_3,
        "games_last_4": games_last_4,
        "games_last_5": games_last_5,
        "games_last_7": games_last_7,
        "back_to_back": bool(days_rest is not None and days_rest < 1.5),
        "is_third_in_four": is_third_in_four,
        "is_fourth_in_six": is_fourth_in_six,
        "last_home_state": last_home_state,
        "last_game_away": last_game_away,
    }


def _selected_side_probability(probability_yes: float, side: str) -> float:
    return round(probability_yes if side == "yes" else 1 - probability_yes, 4)


def _selected_subject_name(
    *,
    event: Event,
    market: Market | None,
    metadata: dict[str, Any],
    side: str,
    default_subject_name: str | None = None,
) -> str | None:
    market_family = str(metadata.get("copilot_market_family") or "")
    if market_family == "player_prop":
        return str(metadata.get("copilot_subject_name") or default_subject_name or "").strip() or default_subject_name
    if market_family == "game_line":
        market_kind = str(metadata.get("copilot_market_kind") or "")
        threshold = float(metadata.get("copilot_threshold") or 0.0)
        if market_kind == "spread":
            subject_name = str(metadata.get("copilot_subject_name") or default_subject_name or "").strip()
            if side == "yes":
                return subject_name or default_subject_name
            return f"{subject_name} +{threshold:g}" if subject_name else default_subject_name
        if market_kind == "total":
            direction = str(metadata.get("copilot_direction") or "over").lower()
            selected_direction = direction if side == "yes" else ("under" if direction == "over" else "over")
            return f"{selected_direction.title()} {threshold:g}"
    if market is None:
        return default_subject_name
    yes_entry = _market_yes_entry(event, market)
    if not yes_entry:
        return default_subject_name
    if side == "yes":
        return yes_entry.participant.display_name
    opponent_entry = next((entry for entry in event.participants if entry.participant_id != yes_entry.participant_id), None)
    if opponent_entry:
        return opponent_entry.participant.display_name
    return default_subject_name


def _winner_thesis_key(
    *,
    event: Event,
    metadata: dict[str, Any],
    selected_subject_name: str | None,
) -> str | None:
    market_family = str(metadata.get("copilot_market_family") or "")
    market_kind = str(metadata.get("copilot_market_kind") or "")
    if market_family != "winner" or market_kind not in {"game_winner", "first_five_winner"}:
        return None
    subject = (selected_subject_name or "").strip().lower()
    if not subject:
        return None
    return f"{event.id}:{market_kind}:{subject}"


def _quality_tier(
    *,
    selected_side_probability: float,
    adjusted_confidence: float,
    context_coverage_score: float,
    total_penalty: float,
    served_mode: str,
) -> str:
    if served_mode == "ml":
        if context_coverage_score >= 0.75 and adjusted_confidence >= 0.6:
            return "high"
        if context_coverage_score >= 0.5:
            return "medium"
        return "low"

    if (
        selected_side_probability < 0.2
        or context_coverage_score < 0.45
        or adjusted_confidence < 0.4
        or total_penalty >= 0.18
    ):
        return "low"
    if (
        selected_side_probability >= 0.36
        and context_coverage_score >= 0.72
        and adjusted_confidence >= 0.58
        and total_penalty <= 0.09
    ):
        return "high"
    return "medium"


def _probable_pitcher_era(event: Event, role: str) -> float | None:
    competitor = _competitor_for_role(event, role)
    probables = competitor.get("probables") or []
    if not probables:
        return None
    probable = probables[0]
    statistics = probable.get("statistics") or []
    for item in statistics:
        if item.get("abbreviation") == "ERA":
            display = str(item.get("displayValue") or "").strip()
            try:
                return float(display)
            except ValueError:
                return None
    return None


def _probable_pitcher_identity(event: Event, role: str) -> tuple[str | None, str | None]:
    """Return ``(display_name, espn_athlete_id)`` for the probable starter.

    Used by MLB advanced-stats wiring to resolve the starter's
    MLB Stats PERSON_ID via name + team match. Falls back to ``(None, None)``
    when ESPN's payload lacks the field.
    """
    competitor = _competitor_for_role(event, role)
    probables = competitor.get("probables") or []
    if not probables:
        return None, None
    probable = probables[0]
    athlete = probable.get("athlete") or probable
    display_name = (
        athlete.get("displayName")
        or athlete.get("fullName")
        or athlete.get("shortName")
        or probable.get("displayName")
    )
    athlete_id_raw = athlete.get("id") or athlete.get("uid")
    athlete_id = str(athlete_id_raw) if athlete_id_raw is not None else None
    return (str(display_name) if display_name else None, athlete_id)


def _score_mlb_first_five(
    db: Session,
    event: Event,
    left: EventParticipant,
    right: EventParticipant,
) -> tuple[float, float, list[str], dict[str, Any]]:
    left_results = _recent_first_five_results(db, left.participant_id, event.starts_at)
    right_results = _recent_first_five_results(db, right.participant_id, event.starts_at)

    left_f5_win_rate = _fractional_win_rate(left_results)
    right_f5_win_rate = _fractional_win_rate(right_results)
    left_f5_runs = _avg_first_five_runs(left_results)
    right_f5_runs = _avg_first_five_runs(right_results)
    left_f5_diff = _avg_first_five_diff(left_results)
    right_f5_diff = _avg_first_five_diff(right_results)
    probable_era_left = _probable_pitcher_era(event, left.role)
    probable_era_right = _probable_pitcher_era(event, right.role)
    starter_edge = 0.0
    if probable_era_left is not None and probable_era_right is not None:
        starter_edge = probable_era_right - probable_era_left

    raw_probability = (
        0.5
        + ((left_f5_win_rate - right_f5_win_rate) * 0.22)
        + tanh((left_f5_diff - right_f5_diff) / 2.0) * 0.14
        + tanh(starter_edge / 1.5) * 0.10
        + (0.01 if left.is_home else 0.0)
    )
    probability = clamp(raw_probability, 0.05, 0.95)
    sample_size = min(len(left_results), 10) + min(len(right_results), 10)
    confidence = clamp(0.2 + (sample_size / 20.0) + abs(probability - 0.5) * 0.45, 0.2, 0.92)
    if probable_era_left is None or probable_era_right is None:
        confidence = clamp(confidence - 0.08, 0.2, 0.92)

    reasons = [
        f"{left.participant.display_name} first-5 win rate: {left_f5_win_rate:.0%}",
        f"{right.participant.display_name} first-5 win rate: {right_f5_win_rate:.0%}",
    ]
    if probable_era_left is not None and probable_era_right is not None:
        reasons.append(
            f"Probable starter ERA edge: {left.participant.display_name} {probable_era_left:.2f} vs {right.participant.display_name} {probable_era_right:.2f}"
        )
    if abs(left_f5_diff - right_f5_diff) > 0.25:
        stronger = left.participant.display_name if left_f5_diff >= right_f5_diff else right.participant.display_name
        reasons.append(f"Recent first-5 run differential favors {stronger}")

    features = {
        "left_f5_win_rate": left_f5_win_rate,
        "right_f5_win_rate": right_f5_win_rate,
        "left_f5_runs": left_f5_runs,
        "right_f5_runs": right_f5_runs,
        "left_f5_diff": left_f5_diff,
        "right_f5_diff": right_f5_diff,
        "left_probable_era": probable_era_left,
        "right_probable_era": probable_era_right,
        "starter_edge": starter_edge,
        "sample_size": sample_size,
        "left_sample_size": len(left_results),
        "right_sample_size": len(right_results),
        "has_probable_starter_context": probable_era_left is not None and probable_era_right is not None,
    }
    return probability, confidence, reasons, features


def _score_team_winner(
    db: Session,
    event: Event,
    left: EventParticipant,
    right: EventParticipant,
) -> tuple[float, float, list[str], dict[str, Any]]:
    left_results = _recent_participant_results(db, left.participant_id, event.starts_at)
    right_results = _recent_participant_results(db, right.participant_id, event.starts_at)
    left_schedule = _schedule_context(db, left.participant_id, event.starts_at)
    right_schedule = _schedule_context(db, right.participant_id, event.starts_at)
    venue_context = _event_venue_context(event)

    left_win_rate = _win_rate(left_results)
    right_win_rate = _win_rate(right_results)
    left_avg_score = _avg_score(left_results)
    right_avg_score = _avg_score(right_results)
    score_gap = left_avg_score - right_avg_score
    home_advantage = 0.03 if event.sport_key in {"NBA", "NFL", "MLB", "SOCCER"} and left.is_home else 0.0
    left_rest_days = float(left_schedule.get("days_rest") or 0.0)
    right_rest_days = float(right_schedule.get("days_rest") or 0.0)
    rest_edge = clamp((left_rest_days - right_rest_days) * 0.015, -0.05, 0.05)
    workload_edge = clamp(
        (float(right_schedule.get("games_last_4") or 0) - float(left_schedule.get("games_last_4") or 0)) * 0.01,
        -0.04,
        0.04,
    )
    back_to_back_edge = 0.0
    if left_schedule.get("back_to_back") and not right_schedule.get("back_to_back"):
        back_to_back_edge = -0.03
    elif right_schedule.get("back_to_back") and not left_schedule.get("back_to_back"):
        back_to_back_edge = 0.03

    raw_probability = (
        0.5
        + ((left_win_rate - right_win_rate) * 0.25)
        + tanh(score_gap / 10.0) * 0.08
        + home_advantage
        + rest_edge
        + workload_edge
        + back_to_back_edge
    )
    left_win_probability = clamp(raw_probability, 0.05, 0.95)
    sample_size = min(len(left_results), 10) + min(len(right_results), 10)
    confidence = clamp(0.2 + (sample_size / 20.0) + abs(left_win_probability - 0.5) * 0.5, 0.2, 0.95)
    if event.sport_key in {"TENNIS", "UFC"}:
        confidence = clamp(confidence - 0.05, 0.2, 0.95)

    reasons = [
        f"{left.participant.display_name} win rate: {left_win_rate:.0%}",
        f"{right.participant.display_name} win rate: {right_win_rate:.0%}",
    ]
    if home_advantage:
        reasons.append(f"{left.participant.display_name} gets a home-field bump")
    if abs(score_gap) > 0.1:
        stronger = left.participant.display_name if score_gap >= 0 else right.participant.display_name
        reasons.append(f"Recent scoring form favors {stronger}")
    if abs(rest_edge) >= 0.01:
        fresher = left.participant.display_name if rest_edge >= 0 else right.participant.display_name
        reasons.append(f"Rest schedule favors {fresher}")
    if abs(workload_edge) >= 0.01:
        fresher = left.participant.display_name if workload_edge >= 0 else right.participant.display_name
        reasons.append(f"Recent workload favors {fresher}")
    features: dict[str, Any] = {
        "left_win_rate": left_win_rate,
        "right_win_rate": right_win_rate,
        "left_avg_score": left_avg_score,
        "right_avg_score": right_avg_score,
        "home_advantage": home_advantage,
        "left_days_rest": left_schedule.get("days_rest"),
        "right_days_rest": right_schedule.get("days_rest"),
        "left_games_last_4": left_schedule.get("games_last_4"),
        "right_games_last_4": right_schedule.get("games_last_4"),
        "left_back_to_back": left_schedule.get("back_to_back"),
        "right_back_to_back": right_schedule.get("back_to_back"),
        "rest_edge": round(rest_edge, 4),
        "workload_edge": round(workload_edge, 4),
        "back_to_back_edge": round(back_to_back_edge, 4),
        "has_schedule_context": left_schedule.get("days_rest") is not None and right_schedule.get("days_rest") is not None,
        "venue_indoor": venue_context.get("venue_indoor"),
        "venue_city": venue_context.get("venue_city"),
        "venue_state": venue_context.get("venue_state"),
        "sample_size": sample_size,
        "left_sample_size": len(left_results),
        "right_sample_size": len(right_results),
    }

    # Apply advanced-stats team context as an additive nudge to win probability.
    # Cached only (allow_network=False) — caches are populated by the daily
    # advanced_stats_warm cron and the per-prop scoring path.
    advanced_edge = _winner_advanced_team_edge(db, event, left, right, features)
    if advanced_edge is not None:
        edge_value = clamp(advanced_edge, -0.06, 0.06)
        left_win_probability = clamp(left_win_probability + edge_value, 0.05, 0.95)
        features["advanced_team_edge"] = round(edge_value, 4)
        if abs(edge_value) >= 0.01:
            stronger = left.participant.display_name if edge_value > 0 else right.participant.display_name
            reasons.append(f"Advanced team form favors {stronger} ({edge_value:+.1%})")

    return left_win_probability, confidence, reasons, features


def _winner_advanced_team_edge(
    db: Session,
    event: Event,
    left: EventParticipant,
    right: EventParticipant,
    features: dict[str, Any],
) -> float | None:
    """Compute an additive win-probability nudge from advanced team stats.

    NBA: uses each team's recent_5 NetRating from ``nba_team_gamelog_cache``.
    MLB: uses opposing probable starters' xFIP gap (lower xFIP → favors the
    team facing the *other* starter).

    Returns ``None`` when no advanced caches are populated for either team
    so the caller can leave the prediction untouched.
    """
    sport = (event.sport_key or "").upper()
    if sport == "NBA":
        from app.services.advanced_stats import find_nba_team_id_by_name, load_nba_team_gamelog

        # NBA season convention: a 2025-26 season starts in October 2025 but
        # is keyed by 2026 (the ending year) throughout the codebase. Use the
        # repo's authoritative resolver instead of raw ``event.starts_at.year``,
        # which would mis-key October-December games to the prior season and
        # miss the warm cache.
        ref_date = event.starts_at.date() if event.starts_at else None
        season = default_season_for_sport("NBA", ref_date)
        left_team_id = find_nba_team_id_by_name(db, team_name=left.participant.display_name or "", season=season)
        right_team_id = find_nba_team_id_by_name(db, team_name=right.participant.display_name or "", season=season)
        if not left_team_id or not right_team_id:
            return None
        left_log = load_nba_team_gamelog(db, team_id=left_team_id, season=season, allow_network=False)
        right_log = load_nba_team_gamelog(db, team_id=right_team_id, season=season, allow_network=False)
        left_recent = (left_log.payload.get("recent_5_avg") if left_log.payload else None) or {}
        right_recent = (right_log.payload.get("recent_5_avg") if right_log.payload else None) or {}
        left_net = left_recent.get("net_rating")
        right_net = right_recent.get("net_rating")
        if not isinstance(left_net, (int, float)) or not isinstance(right_net, (int, float)):
            return None
        features["left_recent_net_rating"] = round(float(left_net), 3)
        features["right_recent_net_rating"] = round(float(right_net), 3)
        # 10 NetRating points ≈ 6% win probability shift (rough literature value).
        return (float(left_net) - float(right_net)) * 0.006

    if sport == "MLB":
        # Pitcher xFIP gap — lower xFIP suppresses opposing offense, so the
        # team facing the *higher*-xFIP starter has the edge.
        from app.services.mlb_advanced import load_mlb_pitcher_advanced, resolve_mlb_stats_player_id

        left_starter_name, left_starter_espn_id = _probable_pitcher_identity(event, left.role)
        right_starter_name, right_starter_espn_id = _probable_pitcher_identity(event, right.role)
        if not left_starter_name or not right_starter_name:
            return None
        ref_date = event.starts_at.date() if event.starts_at else None
        season = default_season_for_sport("MLB", ref_date)

        def _xfip(name: str, team_short: str | None, espn_athlete_id: str | None) -> float | None:
            # Codex round 3: thread the ESPN athlete ID so a successful resolve
            # writes the mlb_stats_id sidecar back to EspnPlayerSearchCache.
            # That makes the warm cron's sidecar-derived list pick the starter
            # up next pass instead of repeatedly resolving on every score.
            mlb_id = resolve_mlb_stats_player_id(
                db,
                espn_athlete_id=espn_athlete_id,
                full_name=name,
                team_abbreviation=(team_short or "").upper() or None,
                season=season,
                allow_network=False,
            )
            if not mlb_id:
                return None
            result = load_mlb_pitcher_advanced(
                db, mlb_player_id=str(mlb_id), season=season, allow_network=False
            )
            saber = (result.payload or {}).get("season_avg") or {}
            value = saber.get("xfip") or saber.get("fip")
            return float(value) if isinstance(value, (int, float)) else None

        left_xfip = _xfip(left_starter_name, left.participant.short_name, left_starter_espn_id)
        right_xfip = _xfip(right_starter_name, right.participant.short_name, right_starter_espn_id)
        if left_xfip is None or right_xfip is None:
            return None
        features["left_starter_xfip"] = round(left_xfip, 3)
        features["right_starter_xfip"] = round(right_xfip, 3)
        # 1 run/9 xFIP gap ≈ ~5% win prob (very rough); flipped sign because
        # the team whose starter has the LOWER xFIP wins more.
        return (right_xfip - left_xfip) * 0.05

    return None


def _recent_score_pairs(
    db: Session,
    participant_id: int,
    before: datetime | None,
    limit: int = 10,
) -> list[tuple[float, float]]:
    if before is None:
        return []
    events = db.scalars(
        select(Event)
        .join(EventParticipant, Event.id == EventParticipant.event_id)
        .where(
            EventParticipant.participant_id == participant_id,
            Event.starts_at < before,
            Event.status == "completed",
        )
        .order_by(desc(Event.starts_at))
        .limit(limit)
    ).all()
    pairs: list[tuple[float, float]] = []
    for past_event in events:
        own_entry = next((entry for entry in past_event.participants if entry.participant_id == participant_id), None)
        opp_entry = next((entry for entry in past_event.participants if entry.participant_id != participant_id), None)
        if own_entry is None or opp_entry is None:
            continue
        if own_entry.score is None or opp_entry.score is None:
            continue
        pairs.append((float(own_entry.score), float(opp_entry.score)))
    return pairs


def _avg_points_for(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    return sum(points_for for points_for, _ in pairs) / len(pairs)


def _avg_points_allowed(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    return sum(points_against for _, points_against in pairs) / len(pairs)


def _avg_margin_from_pairs(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    return sum(points_for - points_against for points_for, points_against in pairs) / len(pairs)


def _avg_total_from_pairs(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    return sum(points_for + points_against for points_for, points_against in pairs) / len(pairs)


def _score_game_line(
    db: Session,
    event: Event,
    market: Market,
    left: EventParticipant,
    right: EventParticipant,
) -> tuple[float, float, list[str], dict[str, Any]] | None:
    metadata = _market_metadata(market)
    market_kind = str(metadata.get("copilot_market_kind") or "")
    threshold = float(metadata.get("copilot_threshold") or 0.0)
    direction = str(metadata.get("copilot_direction") or "over").lower()
    left_pairs = _recent_score_pairs(db, left.participant_id, event.starts_at)
    right_pairs = _recent_score_pairs(db, right.participant_id, event.starts_at)
    sample_size = min(len(left_pairs), 10) + min(len(right_pairs), 10)

    if market_kind == "spread":
        yes_entry_target = _market_yes_entry(event, market)
        if yes_entry_target is None:
            return None

        expected_left_score = (
            _avg_points_for(left_pairs) + _avg_points_allowed(right_pairs)
        ) / 2
        expected_right_score = (
            _avg_points_for(right_pairs) + _avg_points_allowed(left_pairs)
        ) / 2
        if left.is_home:
            expected_left_score += 1.5
        elif right.is_home:
            expected_right_score += 1.5

        expected_margin = expected_left_score - expected_right_score
        if yes_entry_target.participant_id != left.participant_id:
            expected_margin *= -1

        sigma = max(4.5, 10.5 - min(sample_size, 10) * 0.25)
        probability_yes = clamp(1 - NormalDist(mu=expected_margin, sigma=sigma).cdf(threshold), 0.05, 0.95)
        confidence = clamp(0.28 + (sample_size / 20.0) + abs(probability_yes - 0.5) * 0.35, 0.25, 0.9)
        reasons = [
            f"Projected margin for {yes_entry_target.participant.display_name}: {expected_margin:.1f}",
            f"Market line asks for over {threshold:.1f}",
        ]
        features = {
            "expected_margin": round(expected_margin, 4),
            "line_threshold": threshold,
            "distribution_sigma": round(sigma, 4),
            "left_average_margin": round(_avg_margin_from_pairs(left_pairs), 4),
            "right_average_margin": round(_avg_margin_from_pairs(right_pairs), 4),
            "sample_size": sample_size,
            "left_sample_size": len(left_pairs),
            "right_sample_size": len(right_pairs),
        }
        return probability_yes, confidence, reasons, features

    if market_kind == "total":
        expected_total = (_avg_total_from_pairs(left_pairs) + _avg_total_from_pairs(right_pairs)) / 2
        sigma = max(7.5, 15.0 - min(sample_size, 10) * 0.35)
        over_probability = clamp(1 - NormalDist(mu=expected_total, sigma=sigma).cdf(threshold), 0.05, 0.95)
        probability_yes = over_probability if direction == "over" else round(1 - over_probability, 4)
        confidence = clamp(0.26 + (sample_size / 20.0) + abs(probability_yes - 0.5) * 0.35, 0.24, 0.88)
        reasons = [
            f"Projected combined total: {expected_total:.1f}",
            f"Market line: {direction.title()} {threshold:.1f}",
        ]
        features = {
            "expected_total": round(expected_total, 4),
            "line_threshold": threshold,
            "distribution_sigma": round(sigma, 4),
            "left_average_total": round(_avg_total_from_pairs(left_pairs), 4),
            "right_average_total": round(_avg_total_from_pairs(right_pairs), 4),
            "sample_size": sample_size,
            "left_sample_size": len(left_pairs),
            "right_sample_size": len(right_pairs),
        }
        return probability_yes, confidence, reasons, features

    return None


def _prop_value_from_raw(sport_key: str, stat_key: str, raw: dict[str, float]) -> float:
    if sport_key == "NBA":
        if stat_key == "points":
            return raw.get("points", 0.0)
        if stat_key == "rebounds":
            return raw.get("rebounds", 0.0)
        if stat_key == "assists":
            return raw.get("assists", 0.0)
        if stat_key == "made_threes":
            return raw.get("three_points_made", 0.0)
        if stat_key == "steals":
            return raw.get("steals", 0.0)
        if stat_key == "blocks":
            return raw.get("blocks", 0.0)
        if stat_key == "turnovers":
            return raw.get("turnovers", 0.0)
        if stat_key == "points_assists":
            return raw.get("points", 0.0) + raw.get("assists", 0.0)
        if stat_key == "points_rebounds":
            return raw.get("points", 0.0) + raw.get("rebounds", 0.0)
        if stat_key == "rebounds_assists":
            return raw.get("rebounds", 0.0) + raw.get("assists", 0.0)
        if stat_key == "points_rebounds_assists":
            return raw.get("points", 0.0) + raw.get("rebounds", 0.0) + raw.get("assists", 0.0)
    if sport_key == "MLB":
        if stat_key == "hits":
            return raw.get("hits", 0.0)
        if stat_key == "runs":
            return raw.get("runs", 0.0)
        if stat_key == "home_runs":
            return raw.get("home_runs", 0.0)
        if stat_key == "rbis":
            return raw.get("rbis", 0.0)
        if stat_key == "walks":
            return raw.get("walks", 0.0)
        if stat_key == "strikeouts":
            return raw.get("strikeouts", 0.0)
        if stat_key == "total_bases":
            return _total_bases(raw)
        if "_" in stat_key:
            return sum(_prop_value_from_raw(sport_key, component, raw) for component in stat_key.split("_"))
        return raw.get(stat_key, 0.0)
    return raw.get(stat_key, 0.0)


def _total_bases(raw: dict[str, float]) -> float:
    singles = max(raw.get("hits", 0.0) - raw.get("doubles", 0.0) - raw.get("triples", 0.0) - raw.get("home_runs", 0.0), 0.0)
    return singles + (raw.get("doubles", 0.0) * 2.0) + (raw.get("triples", 0.0) * 3.0) + (raw.get("home_runs", 0.0) * 4.0)


def _log_average(game_logs: list[dict[str, Any]], sport_key: str, stat_key: str) -> float:
    if not game_logs:
        return 0.0
    total = sum(_prop_value_from_raw(sport_key, stat_key, item["raw_metrics"]) for item in game_logs)
    return total / len(game_logs)


def _plate_appearances(raw: dict[str, float]) -> float:
    return raw.get("at_bats", 0.0) + raw.get("walks", 0.0) + raw.get("hit_by_pitch", 0.0)


def _usage_proxy(raw: dict[str, float]) -> float:
    return raw.get("field_goals_attempted", 0.0) + (raw.get("assists", 0.0) * 0.7) + raw.get("turnovers", 0.0)


def _weighted_expectation(recent_value: float, season_value: float, trend_value: float) -> float:
    return (recent_value * 0.55) + (season_value * 0.30) + (trend_value * 0.15)


def _poisson_yes_probability(expected_value: float, threshold: float) -> float:
    if threshold <= 0:
        return 1.0
    lambda_value = max(expected_value, 0.01)
    cutoff = max(int(round(threshold)) - 1, 0)
    running_term = exp(-lambda_value)
    cumulative = running_term
    for k in range(1, cutoff + 1):
        running_term *= lambda_value / k
        cumulative += running_term
    return clamp(1.0 - cumulative, 0.01, 0.99)


def _event_entry_for_team_hint(event: Event, team_hint: str | None, team_name: str | None) -> EventParticipant | None:
    tokens = alias_tokens(team_hint or "", team_name or "")
    best_entry = None
    best_score = 0.0
    for entry in event.participants:
        participant_tokens = alias_tokens(entry.participant.display_name, entry.participant.short_name)
        score = _token_score(tokens, participant_tokens)
        if score > best_score:
            best_score = score
            best_entry = entry
    if best_score < 0.1:
        return None
    return best_entry


def _logs_for_location(game_logs: list[dict[str, Any]], location: str) -> list[dict[str, Any]]:
    return [item for item in game_logs if item.get("location") == location]


def _logs_for_opponent(game_logs: list[dict[str, Any]], opponent_name: str, opponent_short_name: str | None = None) -> list[dict[str, Any]]:
    opponent_tokens = alias_tokens(opponent_name, opponent_short_name or "")
    matched: list[dict[str, Any]] = []
    for item in game_logs:
        if _token_score(opponent_tokens, alias_tokens(item.get("opponent") or "", item.get("opponent_abbreviation") or "")) >= 0.15:
            matched.append(item)
    return matched


def _player_prop_participation_gate(sport_key: str, recent_logs: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if len(recent_logs) < 5:
        return False, "Not enough recent appearances to trust the player-prop sample."

    if sport_key == "NBA":
        active_games = [item for item in recent_logs if item["raw_metrics"].get("minutes", 0.0) >= 10]
        recent_minutes = _log_average(recent_logs[:3], sport_key, "minutes")
        if len(active_games) < 5 or recent_minutes < 18:
            return False, "Player role looks unstable in recent NBA minutes."
        return True, None

    recent_pa = [_plate_appearances(item["raw_metrics"]) for item in recent_logs[:5]]
    if len([value for value in recent_pa if value >= 2.0]) < 3:
        return False, "Batter role looks unstable because recent plate appearances are too thin."
    return True, None


def _score_player_prop(
    db: Session,
    event: Event,
    market: Market,
    snapshot: MarketSnapshot | None,
    resolver: PropStatsResolver,
) -> tuple[float, float, list[str], dict[str, Any]] | None:
    metadata = _market_metadata(market)
    sport_key = str(market.sport_key or event.sport_key)
    subject_name = str(metadata.get("copilot_subject_name") or "")
    team_hint = metadata.get("copilot_subject_team")
    stat_key = str(metadata.get("copilot_stat_key") or "")
    threshold = float(metadata.get("copilot_threshold") or 0.0)
    if not subject_name or not stat_key or threshold <= 0:
        return None

    try:
        resolved = resolver.resolve(sport_key, subject_name, team_hint=team_hint if isinstance(team_hint, str) else None)
    except Exception:
        return None
    if not resolved.game_logs:
        return None

    season_logs = resolved.game_logs
    recent_logs = season_logs[:10]
    short_term_logs = recent_logs[:3]
    is_eligible, _ = _player_prop_participation_gate(sport_key, recent_logs)
    if not is_eligible:
        return None

    expected = _weighted_expectation(
        _log_average(recent_logs, sport_key, stat_key),
        _log_average(season_logs, sport_key, stat_key),
        _log_average(short_term_logs, sport_key, stat_key),
    )
    features: dict[str, Any] = {
        "sport_key": sport_key,
        "subject_name": resolved.display_name,
        "stat_key": stat_key,
        "threshold": threshold,
        "recent_10_average": round(_log_average(recent_logs, sport_key, stat_key), 3),
        "season_average": round(_log_average(season_logs, sport_key, stat_key), 3),
        "recent_3_average": round(_log_average(short_term_logs, sport_key, stat_key), 3),
        "sample_size": len(recent_logs),
        "recent_values": [round(_prop_value_from_raw(sport_key, stat_key, item["raw_metrics"]), 3) for item in recent_logs],
        "player_search_cache_status": resolved.player_search_cache_status,
        "gamelog_cache_status": resolved.gamelog_cache_status,
        "uses_stale_prop_context": resolved.context_stale,
    }
    reasons = [
        f"{resolved.display_name} recent 10-game {stat_key.replace('_', ' ')} average: {features['recent_10_average']:.2f}",
        f"{resolved.display_name} season {stat_key.replace('_', ' ')} average: {features['season_average']:.2f}",
    ]

    team_entry = _event_entry_for_team_hint(event, team_hint if isinstance(team_hint, str) else None, resolved.team_name)
    opponent_entry = None
    if team_entry:
        opponent_entry = next((entry for entry in event.participants if entry.participant_id != team_entry.participant_id), None)
        location = "home" if team_entry.is_home else "away"
        location_logs = _logs_for_location(season_logs, location)
        if len(location_logs) >= 3:
            location_average = _log_average(location_logs, sport_key, stat_key)
            expected = (expected * 0.88) + (location_average * 0.12)
            features["location_average"] = round(location_average, 3)
            features["location"] = location
            reasons.append(f"{resolved.display_name} {location} split: {location_average:.2f}")
        if opponent_entry:
            opponent_logs = _logs_for_opponent(
                season_logs,
                opponent_entry.participant.display_name,
                opponent_entry.participant.short_name,
            )
            if len(opponent_logs) >= 2:
                opponent_average = _log_average(opponent_logs, sport_key, stat_key)
                expected = (expected * 0.92) + (opponent_average * 0.08)
                features["opponent_average"] = round(opponent_average, 3)
                reasons.append(
                    f"{resolved.display_name} vs {opponent_entry.participant.display_name}: {opponent_average:.2f}"
                )
        team_schedule = _schedule_context(db, team_entry.participant_id, event.starts_at)
        features["team_days_rest"] = team_schedule.get("days_rest")
        features["team_games_last_3"] = team_schedule.get("games_last_3")
        features["team_games_last_4"] = team_schedule.get("games_last_4")
        features["team_back_to_back"] = team_schedule.get("back_to_back")
        # Smarter #10 — finer-grained NBA schedule density features consumed
        # by ``_nba_rest_factor`` and ``_nba_travel_factor`` (heuristic_factors).
        features["team_is_third_in_four"] = team_schedule.get("is_third_in_four")
        features["team_is_fourth_in_six"] = team_schedule.get("is_fourth_in_six")
        features["team_last_game_away"] = team_schedule.get("last_game_away")
        features["team_is_home"] = bool(team_entry.is_home)
        if opponent_entry:
            opponent_schedule = _schedule_context(db, opponent_entry.participant_id, event.starts_at)
            features["opponent_days_rest"] = opponent_schedule.get("days_rest")
            features["opponent_games_last_4"] = opponent_schedule.get("games_last_4")
            features["opponent_back_to_back"] = opponent_schedule.get("back_to_back")
            rest_diff = float(team_schedule.get("days_rest") or 0.0) - float(opponent_schedule.get("days_rest") or 0.0)
            workload_diff = float(opponent_schedule.get("games_last_4") or 0.0) - float(team_schedule.get("games_last_4") or 0.0)
            rest_factor = clamp(1 + (rest_diff * 0.015) + (workload_diff * 0.01), 0.93, 1.08)
            expected *= rest_factor
            features["rest_factor"] = round(rest_factor, 3)
            features["has_schedule_context"] = (
                team_schedule.get("days_rest") is not None and opponent_schedule.get("days_rest") is not None
            )
            if abs(rest_factor - 1.0) >= 0.02:
                reasons.append(f"Schedule context factor: {rest_factor:.2f}x")

    # Lift MLB venue context before the advanced-stats emission so the weather
    # loader downstream can consult ``venue_indoor`` to short-circuit dome games.
    if sport_key.upper() == "MLB":
        venue_context = _event_venue_context(event)
        features["venue_indoor"] = venue_context.get("venue_indoor")
        features["venue_city"] = venue_context.get("venue_city")
        features["venue_state"] = venue_context.get("venue_state")

    # Emit advanced features FIRST so the proxy block below can detect when a
    # real advanced replacement (USG%, opponent pace, opposing starter xFIP/FIP)
    # is present and skip the corresponding box-score proxy. The handoff
    # (PR3_HANDOFF.md) calls this out as the highest-risk backend change:
    # advanced stats must be PRIMARY, proxies fallback only — so we don't
    # multiply both a proxy and its advanced replacement for the same concept.
    if resolved.sport_key.upper() == "NBA":
        from app.services.advanced_stats import (
            emit_nba_interaction_term,
            emit_nba_opponent_team_features,
            emit_nba_player_features,
            emit_nba_workload_features,
            find_nba_team_id_by_name,
            load_nba_team_gamelog,
        )
        from app.services.nba_long_tail import (
            emit_nba_clutch_features,
            emit_nba_drives_features,
            emit_nba_hustle_features,
            load_nba_clutch_player,
            load_nba_hustle_player,
            load_nba_tracking,
        )

        if resolved.advanced_payload:
            features.update(emit_nba_player_features(resolved.advanced_payload))

        # Smarter #11: workload features from the ESPN game log. Pure
        # function read against ``resolved.game_logs`` (already in scope
        # and sorted reverse-chrono) — no network or cache reads.
        features.update(emit_nba_workload_features(resolved.game_logs))

        if opponent_entry is not None:
            opponent_name = opponent_entry.participant.display_name or ""
            opponent_team_id = find_nba_team_id_by_name(
                db, team_name=opponent_name, season=resolved.season
            )
            if opponent_team_id:
                opponent_team_result = load_nba_team_gamelog(
                    db,
                    team_id=opponent_team_id,
                    season=resolved.season,
                    allow_network=False,
                )
                if opponent_team_result.payload:
                    features.update(emit_nba_opponent_team_features(opponent_team_result.payload))
                    features["opponent_team_cache_status"] = opponent_team_result.cache_status

        # Smarter #12: emit the usage × pace × (1 / opponent_DRtg) interaction
        # term once both source caches have contributed. The emitter returns
        # {} when any input is missing, so this is safe to run unconditionally
        # — the model handles absent keys via median imputation.
        features.update(
            emit_nba_interaction_term(
                usage_pct=features.get("recent_usage_pct") or features.get("season_usage_pct"),
                opponent_pace=features.get("opponent_pace_recent_5")
                    or features.get("opponent_pace_season"),
                opponent_drtg=features.get("opponent_def_rating_recent_5"),
            )
        )

        # Long-tail NBA features — hustle, drives, clutch — for the prop subject.
        # Cached only at scoring time (allow_network=False); the daily warm job
        # populates these league-wide leaderboards. ``resolved.nba_stats_id``
        # is set by ``_load_nba_advanced`` when resolution succeeds, so we
        # avoid re-scanning EspnPlayerSearchCache here.
        nba_stats_id = resolved.nba_stats_id

        if nba_stats_id:
            hustle_result = load_nba_hustle_player(db, season=resolved.season, allow_network=False)
            features.update(emit_nba_hustle_features(hustle_result.payload, str(nba_stats_id)))

            drives_result = load_nba_tracking(
                db, season=resolved.season, pt_measure_type="Drives", allow_network=False
            )
            features.update(emit_nba_drives_features(drives_result.payload, str(nba_stats_id)))

            clutch_result = load_nba_clutch_player(db, season=resolved.season, allow_network=False)
            features.update(emit_nba_clutch_features(clutch_result.payload, str(nba_stats_id)))

    elif resolved.sport_key.upper() == "MLB":
        from app.models import EspnPlayerSearchCache, MlbLineupCache, MlbWeatherCache  # noqa: F401
        from app.services.mlb_advanced import (
            emit_lineup_features,
            emit_mlb_batter_features,
            emit_mlb_pitcher_features,
            emit_mlb_platoon_features,
            extract_pitch_hand_from_lineup,
            load_mlb_player_splits,
            emit_park_features,
            emit_weather_features,
            load_lineup_for_event,
            load_mlb_pitcher_advanced,
            load_mlb_statcast_pitcher,
            load_park_factors_for_event,
            load_weather,
            resolve_mlb_stats_player_id,
        )

        if resolved.advanced_payload:
            sabermetrics = resolved.advanced_payload.get("batter_sabermetrics")
            statcast = resolved.advanced_payload.get("batter_statcast")
            features.update(emit_mlb_batter_features(sabermetrics, statcast))

        # Bug #4: park factors are not keyed by ESPN's venue id; the
        # helper prefers venue-name match (disambiguates TBR Tropicana
        # vs. Steinbrenner), then home team abbreviation, then legacy
        # top-level venue_id for any non-ESPN rows.
        home_competitor = _competitor_for_role(event, "home")
        home_team_abbr = (home_competitor.get("team") or {}).get("abbreviation")
        park = load_park_factors_for_event(event.raw_data, home_team_abbr)
        features.update(emit_park_features(park))

        venue_indoor_flag = bool(features.get("venue_indoor"))
        # Bug #4: game_time_utc enables the weather cache lookup to filter
        # by expiration. lat/lon are not provided by ESPN's venue payload
        # so weather still requires a separately-warmed cache (smarter #15
        # — weather_refresh job); allow_network stays False to keep the
        # synchronous scoring path off the network.
        starts_at = event.starts_at
        if starts_at is not None and starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=timezone.utc)
        game_time_utc = starts_at.astimezone(timezone.utc) if starts_at else None
        weather_result = load_weather(
            db,
            event_id=str(event.id),
            lat=None,
            lon=None,
            game_time_utc=game_time_utc,
            is_dome=venue_indoor_flag,
            allow_network=False,
        )
        if weather_result.payload:
            features.update(emit_weather_features(weather_result.payload))
            features["weather_cache_status"] = weather_result.cache_status

        # Opposing probable starter — name comes from ESPN's competitor
        # `probables` list. We try to resolve their MLB Stats PERSON_ID via
        # the cached league roster (the resolver also writes the mapping
        # back to EspnPlayerSearchCache for next time) and emit pitcher
        # sabermetrics + Statcast features. allow_network=False on the read
        # path keeps the synchronous scoring fast — pitcher caches are warmed
        # by the daily cron + warm_mlb_advanced_for_athletes path.
        starter_name, starter_espn_id = (
            _probable_pitcher_identity(event, opponent_entry.role)
            if opponent_entry
            else (None, None)
        )
        # Smarter #5: initialize to None so the downstream platoon-features
        # block can safely read it even when no starter resolves (codex
        # caught this — previously the variable was only defined inside the
        # ``if starter_name`` branch).
        starter_id: int | None = None
        if starter_name:
            starter_team = opponent_entry.participant.short_name if opponent_entry else None
            # Codex round 3: pass the ESPN athlete ID so a successful resolve
            # persists the mlb_stats_id sidecar — keeps the warm cron's
            # sidecar-derived starter list up to date without manual seeding.
            starter_id = resolve_mlb_stats_player_id(
                db,
                espn_athlete_id=starter_espn_id,
                full_name=starter_name,
                team_abbreviation=(starter_team or "").upper() or None,
                season=resolved.season,
                allow_network=False,
            )
            if starter_id:
                pitcher_result = load_mlb_pitcher_advanced(
                    db,
                    mlb_player_id=str(starter_id),
                    season=resolved.season,
                    allow_network=False,
                )
                pitcher_statcast_result = load_mlb_statcast_pitcher(
                    db,
                    mlb_player_id=str(starter_id),
                    season=resolved.season,
                    allow_network=False,
                )
                features.update(
                    emit_mlb_pitcher_features(
                        pitcher_result.payload,
                        pitcher_statcast_result.payload,
                    )
                )

        # Lineup context — batting-order position drives the lineup_factor.
        # ``resolved.mlb_stats_id`` is set by ``_load_mlb_advanced``; no need
        # to re-scan the search cache here.
        lineup_result = load_lineup_for_event(db, event_id=str(event.id))
        if lineup_result.payload and resolved.mlb_stats_id:
            features.update(emit_lineup_features(lineup_result.payload, str(resolved.mlb_stats_id)))

        # Smarter #5 — batter-vs-starter platoon factor. Resolved starter id
        # + cached splits payload + cached season OPS combine into a single
        # multiplier gated on offense stats. Each lookup is allow_network=False
        # so the synchronous scoring path stays off the wire; the cron warm
        # path is responsible for keeping the splits cache fresh.
        if starter_id and resolved.mlb_stats_id:
            starter_pitch_hand = extract_pitch_hand_from_lineup(
                lineup_result.payload if lineup_result else None,
                str(starter_id),
            )
            if starter_pitch_hand is not None:
                splits_result = load_mlb_player_splits(
                    db,
                    mlb_player_id=str(resolved.mlb_stats_id),
                    season=resolved.season,
                    split_kind="vsLeftRight",
                    allow_network=False,
                )
                features.update(
                    emit_mlb_platoon_features(
                        starter_pitch_hand,
                        splits_result.payload,
                        features.get("season_ops"),
                    )
                )

        # Smarter #6 — opposing-bullpen rest index. Counts the opposing
        # team's completed games in the 3-day window before this event
        # (so a tired opposing pen on day-4 of a road trip translates to
        # a slight boost on the batter's runs/RBIs). The DB query hits
        # already-indexed (participant_id, starts_at) so it's cheap;
        # nothing here is network-bound. Producer is the local Event +
        # EventParticipant tables maintained by the sports ingestion job.
        if opponent_entry and event.starts_at is not None:
            from app.services.mlb_advanced import (
                count_team_games_in_window,
                emit_mlb_bullpen_features,
            )

            opp_recent = count_team_games_in_window(
                db,
                participant_id=opponent_entry.participant_id,
                end_at=event.starts_at,
            )
            bullpen_features = emit_mlb_bullpen_features(
                home_games_in_window=None,
                away_games_in_window=opp_recent,
            )
            if bullpen_features:
                # The scoring kernel reads ``opposing_bullpen_rest_index_3d``
                # — alias the away_* emission so the feature name matches
                # the matchup framing (the batter's perspective).
                rest_index = bullpen_features.get("away_bullpen_rest_index_3d")
                if rest_index is not None:
                    features["opposing_bullpen_rest_index_3d"] = rest_index
                    features["bullpen_rest_data_complete"] = 1.0

    features["advanced_cache_status"] = resolved.advanced_cache_status

    # Sport-specific proxy block. Proxies that have a real advanced replacement
    # in the features dict are skipped (set to 1.0) so we don't multiply both
    # the proxy and the advanced factor for the same concept. Each gate keys
    # off the exact feature names the heuristic_factors module reads AND
    # confirms via ``factor_applies`` that the per-stat gating tuple actually
    # wires the advanced replacement for ``stat_key`` — otherwise the proxy
    # must continue to apply, since suppressing it would drop the signal
    # entirely (no replacement runs and the proxy is gone).
    from app.services.heuristic_factors import factor_applies

    if sport_key == "NBA":
        has_advanced_usage_data = (
            isinstance(features.get("recent_usage_pct"), (int, float))
            and isinstance(features.get("season_usage_pct"), (int, float))
        )
        has_advanced_opp_pace_data = isinstance(
            features.get("opponent_pace_recent_5"), (int, float)
        ) or isinstance(features.get("opponent_pace_season"), (int, float))

        # Per-stat gating: only suppress the proxy when the advanced
        # replacement is wired for this stat_key in heuristic_factors.
        suppress_usage_proxy = has_advanced_usage_data and factor_applies(
            sport_key, stat_key, "usage_factor_advanced"
        )
        suppress_pace_proxy = has_advanced_opp_pace_data and factor_applies(
            sport_key, stat_key, "pace_factor_advanced"
        )

        recent_minutes = _log_average(short_term_logs, sport_key, "minutes")
        season_minutes = _log_average(season_logs, sport_key, "minutes")
        minute_factor = 1.0
        if season_minutes > 0:
            minute_factor = clamp(1 + ((recent_minutes - season_minutes) / season_minutes) * 0.25, 0.88, 1.12)
            expected *= minute_factor

        usage_factor = 1.0
        if suppress_usage_proxy:
            features["usage_factor_proxy_superseded"] = True
        else:
            recent_usage = sum(_usage_proxy(item["raw_metrics"]) for item in short_term_logs) / max(len(short_term_logs), 1)
            season_usage = sum(_usage_proxy(item["raw_metrics"]) for item in season_logs) / max(len(season_logs), 1)
            if season_usage > 0:
                usage_factor = clamp(1 + ((recent_usage - season_usage) / season_usage) * 0.15, 0.90, 1.10)
                expected *= usage_factor

        features["recent_minutes"] = round(recent_minutes, 2)
        features["season_minutes"] = round(season_minutes, 2)
        features["minute_factor"] = round(minute_factor, 3)
        features["usage_factor"] = round(usage_factor, 3)
        reasons.append(f"Recent minutes trend factor: {minute_factor:.2f}x")

        if opponent_entry:
            if suppress_pace_proxy:
                features["pace_factor"] = 1.0
                features["pace_factor_proxy_superseded"] = True
                features["has_pace_context"] = True
            else:
                opponent_recent_scores = _recent_participant_results(db, opponent_entry.participant_id, event.starts_at)
                if opponent_recent_scores:
                    opponent_offense = _avg_score(opponent_recent_scores)
                    pace_factor = clamp(1 + ((opponent_offense - 110.0) / 110.0) * 0.08, 0.95, 1.05)
                    expected *= pace_factor
                    features["opponent_recent_avg_score"] = round(opponent_offense, 3)
                    features["pace_factor"] = round(pace_factor, 3)
                    features["has_pace_context"] = True
                    if abs(pace_factor - 1.0) >= 0.015:
                        reasons.append(f"Opponent pace context factor: {pace_factor:.2f}x")
                else:
                    features["has_pace_context"] = False
    else:
        has_advanced_starter_data = (
            isinstance(features.get("opposing_starter_xfip"), (int, float))
            or isinstance(features.get("opposing_starter_fip"), (int, float))
        )
        suppress_starter_proxy = has_advanced_starter_data and factor_applies(
            sport_key, stat_key, "starter_factor_advanced"
        )

        recent_pa = sum(_plate_appearances(item["raw_metrics"]) for item in short_term_logs) / max(len(short_term_logs), 1)
        season_pa = sum(_plate_appearances(item["raw_metrics"]) for item in season_logs) / max(len(season_logs), 1)
        pa_factor = 1.0
        if season_pa > 0:
            pa_factor = clamp(1 + ((recent_pa - season_pa) / season_pa) * 0.18, 0.88, 1.12)
            expected *= pa_factor

        starter_era = _probable_pitcher_era(event, opponent_entry.role) if opponent_entry else None
        era_factor = 1.0
        if starter_era is not None and not suppress_starter_proxy:
            era_factor = clamp(1 + ((starter_era - 4.00) * 0.03), 0.90, 1.10)
            expected *= era_factor

        features["recent_plate_appearances"] = round(recent_pa, 2)
        features["season_plate_appearances"] = round(season_pa, 2)
        features["plate_appearance_factor"] = round(pa_factor, 3)
        features["opposing_probable_era"] = starter_era
        features["starter_era_factor"] = round(era_factor, 3)
        if suppress_starter_proxy:
            features["starter_era_factor_proxy_superseded"] = True
        reasons.append(f"Recent plate appearance factor: {pa_factor:.2f}x")
        if starter_era is not None and not suppress_starter_proxy:
            reasons.append(f"Opposing probable starter ERA context: {starter_era:.2f}")

    # ``expected_before_advanced`` snapshots ``expected`` AFTER the box-score
    # proxy block has multiplied in (gated proxies for usage / pace / ERA;
    # always-on volume proxies for minutes / PAs; schedule + opponent context)
    # but BEFORE the heuristic_factors advanced multipliers fire below. The
    # name reflects "before the advanced-factor pass," not "before any
    # adjustment." Same meaning as pre-PR3a — only the numeric value moves
    # because the proxy block is now gated.
    expected_before_advanced = expected
    features["expected_before_advanced"] = round(expected_before_advanced, 3)

    probability_yes = _poisson_yes_probability(expected, threshold)
    sample_size = min(len(recent_logs), 10)
    confidence = clamp(0.32 + (sample_size / 18.0) + abs(probability_yes - 0.5) * 0.45, 0.25, 0.93)
    if len(short_term_logs) < 3:
        confidence = clamp(confidence - 0.08, 0.25, 0.93)

    features["expected_stat_output"] = round(expected, 3)
    features["yes_probability"] = round(probability_yes, 4)
    features["has_team_context"] = team_entry is not None
    features["has_opponent_context"] = opponent_entry is not None
    features["latest_log_days_ago"] = round(_days_since_latest_log(season_logs, event.starts_at) or 0.0, 3)

    # Apply advanced-stats factors AFTER all box-score / proxy factors. Each
    # factor defaults to 1.0 when its source data is missing, so this is a
    # safe no-op when advanced caches haven't populated yet for this player.
    from app.services.feature_attribution import driver_reason_strings, top_drivers
    from app.services.heuristic_factors import apply_factors, compute_advanced_factors

    advanced_factors = compute_advanced_factors(resolved.sport_key, stat_key, features)
    if advanced_factors:
        expected_after_advanced = apply_factors(expected, advanced_factors)
        features["advanced_factors"] = advanced_factors
        features["expected_stat_output"] = round(expected_after_advanced, 3)
        # Recompute probability with the adjusted expected.
        probability_yes = _poisson_yes_probability(expected_after_advanced, threshold)
        features["yes_probability"] = round(probability_yes, 4)
        # Driver attribution — turn the multipliers into a sorted, labeled
        # list with detail strings so the frontend can render rich rows
        # without re-deriving them. Top-2 drivers also become reason
        # strings on the recommendation rationale.
        #
        # Always write ``_drivers`` (even as ``[]``) when advanced factors
        # fired. The frontend treats the field as authoritative when
        # present: an empty list means "we computed but nothing met the
        # near-zero filter" — render the empty state, do NOT fall back to
        # deriving rows from raw ``advanced_factors``.
        drivers = top_drivers(features, expected_before_advanced, expected_after_advanced)
        features["_drivers"] = drivers
        for line in driver_reason_strings(drivers):
            reasons.append(line)
    reasons.append(f"Model probability of clearing {threshold:.1f}: {probability_yes:.0%}")
    if resolved.context_stale:
        reasons.append("Using stale cached prop context while live ESPN refresh catches up.")
    if metadata.get("copilot_requires_lineup"):
        # Smarter #16: only warn the operator about lineup uncertainty when
        # we don't already have a confirmed-in-lineup signal. Once
        # ``player_in_starting_lineup == 1.0`` the prop is resolved on that
        # axis — surfacing a stale "only valid if confirmed" line would
        # contradict the scoring outcome.
        lineup_data_complete = float(features.get("lineup_data_complete") or 0.0) >= 1.0
        player_in_starting_lineup = (
            float(features.get("player_in_starting_lineup") or 0.0) >= 1.0
        )
        if not (lineup_data_complete and player_in_starting_lineup):
            reasons.append(
                "Recommendation is only valid if the player is confirmed active / in the starting lineup."
            )

    return probability_yes, confidence, reasons, features


def _single_scoring_adjustments(
    db: Session,
    *,
    family_key: str,
    event: Event,
    market: Market | None,
    snapshot: MarketSnapshot | None,
    metadata: dict[str, Any],
    features: dict[str, Any],
    probability_yes: float,
    base_confidence: float,
    left: EventParticipant | None,
    right: EventParticipant | None,
) -> tuple[float, dict[str, Any]]:
    profile = _profile_for_single_family(family_key)
    sample_size = int(features.get("sample_size") or 0)
    feature_flags: dict[str, bool] = {
        "market_snapshot": snapshot is not None,
    }
    missing_context: list[str] = []
    # Smarter #16: flipped to True when ``copilot_requires_lineup`` is set
    # AND lineup data IS confirmed AND the player is NOT in the starting
    # lineup. Threaded back via diagnostics so the scoring kernel can add a
    # ``player_not_in_starting_lineup`` entry to ``suppression_reasons``.
    lineup_scratch_suppression = False

    if "has_schedule_context" in features:
        feature_flags["schedule_context"] = bool(features.get("has_schedule_context"))
    if "venue_indoor" in features and features.get("venue_indoor") is not None:
        feature_flags["venue_context"] = True

    if family_key == "mlb_singles":
        probable_context = bool(features.get("has_probable_starter_context"))
        feature_flags["probable_starter_context"] = probable_context
        if str(metadata.get("copilot_market_kind") or "") == "first_five_winner" and not probable_context:
            missing_context.append("probable_starter_context")
    elif family_key.endswith("_props"):
        has_team_context = bool(features.get("has_team_context"))
        has_opponent_context = bool(features.get("has_opponent_context"))
        feature_flags["team_context"] = has_team_context
        feature_flags["opponent_context"] = has_opponent_context
        if "has_pace_context" in features:
            feature_flags["pace_context"] = bool(features.get("has_pace_context"))
        if "uses_stale_prop_context" in features:
            feature_flags["fresh_prop_context"] = not bool(features.get("uses_stale_prop_context"))
        if not has_team_context:
            missing_context.append("team_context")
        if not has_opponent_context:
            missing_context.append("opponent_context")
        if bool(features.get("uses_stale_prop_context")):
            missing_context.append("fresh_prop_context")
        if metadata.get("copilot_requires_lineup"):
            # Smarter #16 — three states, each gets a distinct response:
            #
            # 1. Lineup data not yet in payload (``lineup_data_complete``
            #    absent) — pre-lineup window. Keep the existing
            #    missing-context penalty so confidence reflects the
            #    uncertainty.
            # 2. Lineup data IS in payload AND player NOT in starting
            #    lineup. Scratch / DNP. The original 0.025 penalty is far
            #    too lenient — we know the prop is already a near-zero.
            #    Signal a suppression hint here; the scoring kernel adds
            #    it to ``suppression_reasons`` so the recommendation is
            #    dropped instead of merely penalized.
            # 3. Lineup data IS in payload AND player IS in lineup —
            #    confirmation, no penalty.
            lineup_data_complete = float(features.get("lineup_data_complete") or 0.0) >= 1.0
            player_in_starting_lineup = (
                float(features.get("player_in_starting_lineup") or 0.0) >= 1.0
            )
            feature_flags["lineup_confirmation"] = (
                lineup_data_complete and player_in_starting_lineup
            )
            if not lineup_data_complete:
                missing_context.append("lineup_confirmation")
            elif not player_in_starting_lineup:
                lineup_scratch_suppression = True
            elif family_key == "nba_props":
                # Smarter #11: NBA load-management uncertainty. Even when
                # lineup is confirmed, a top-quartile workload player has a
                # higher latent "manager pulls them at the half" risk. The
                # workload factor handles the magnitude; this just records
                # the uncertainty so confidence reflects it (0.025 penalty
                # via missing_context). Gated to NBA so an MLB prop with a
                # stray ``recent_workload_minutes_per_game`` (shouldn't
                # exist) can't trip the check — codex Pattern 9.
                mpg = features.get("recent_workload_minutes_per_game")
                if isinstance(mpg, (int, float)) and mpg >= 34.0:
                    missing_context.append("workload_top_quartile_uncertainty")

    if snapshot is None:
        missing_context.append("market_snapshot")

    thin_sample_penalty = _sample_penalty(sample_size, profile)

    stale_days: float | None = None
    if family_key.endswith("_props"):
        latest_log_days = features.get("latest_log_days_ago")
        stale_days = float(latest_log_days) if latest_log_days is not None else None
    elif left and right:
        left_days = _days_since_participant_game(db, left.participant_id, event.starts_at)
        right_days = _days_since_participant_game(db, right.participant_id, event.starts_at)
        relevant = [value for value in (left_days, right_days) if value is not None]
        stale_days = max(relevant) if relevant else None
    stale_penalty = _staleness_penalty(stale_days, profile)

    volatility_penalty = 0.0
    if family_key.endswith("_props"):
        recent_values = [
            float(value)
            for value in list(features.get("recent_values") or [])
            if value is not None
        ]
        volatility_penalty = _prop_volatility_penalty(
            recent_values,
            float(features.get("threshold") or metadata.get("copilot_threshold") or 0.0),
            profile,
        )
        if recent_values:
            features["recent_value_stddev"] = round(pstdev(recent_values), 3) if len(recent_values) > 1 else 0.0

    implied_yes = _market_implied_yes_price(snapshot)
    disagreement = abs(probability_yes - implied_yes) if implied_yes is not None else 0.0
    market_disagreement_penalty = _market_disagreement_penalty(
        disagreement,
        profile,
        sample_penalty=thin_sample_penalty,
    )
    missing_context_penalty = round(min(len(missing_context) * 0.025, 0.10), 4)

    penalties = {
        "thin_sample": thin_sample_penalty,
        "missing_context": missing_context_penalty,
        "stale_data": stale_penalty,
        "volatility": volatility_penalty,
        "market_disagreement": market_disagreement_penalty,
    }
    total_penalty = round(sum(penalties.values()), 4)
    confidence_floor = 0.25 if family_key.endswith("_props") else 0.2
    adjusted_confidence = clamp(base_confidence - total_penalty, confidence_floor, 0.95)
    diagnostics: dict[str, Any] = {
        "family_key": family_key,
        "confidence_semantics": "heuristic_reliability",
        "base_confidence": round(base_confidence, 4),
        "adjusted_confidence": round(adjusted_confidence, 4),
        "sample_size": sample_size,
        "stale_days": round(stale_days, 3) if stale_days is not None else None,
        "market_disagreement": round(disagreement, 4),
        "feature_flags": feature_flags,
        "missing_context": missing_context,
        "penalties": penalties,
    }
    if lineup_scratch_suppression:
        diagnostics["lineup_suppression_reason"] = "player_not_in_starting_lineup"
    return adjusted_confidence, diagnostics


def _finalize_single_scoring_diagnostics(
    *,
    diagnostics: dict[str, Any],
    selected_side_probability: float,
    selected_edge: float,
    adjusted_confidence: float,
) -> tuple[float, dict[str, Any]]:
    total_penalty = sum(float(value or 0.0) for value in dict(diagnostics.get("penalties") or {}).values())
    selection_score = round(
        max(
            (selected_edge * 0.65)
            + (adjusted_confidence * 0.20)
            + (abs(selected_side_probability - 0.5) * 0.15)
            - (total_penalty * 0.60),
            0.0,
        ),
        4,
    )
    finalized = {
        **diagnostics,
        "selection_score": selection_score,
        "selected_probability_distance": round(abs(selected_side_probability - 0.5), 4),
    }
    return selection_score, finalized


def _build_scored_recommendation(
    db: Session,
    event: Event,
    market: Market | None,
    snapshot: MarketSnapshot | None,
    resolver: PropStatsResolver | None = None,
) -> ScoredRecommendation | None:
    settings = get_settings()
    participants = sorted(event.participants, key=lambda item: item.is_home, reverse=True)
    if len(participants) < 2 and not market:
        return None

    left = participants[0] if participants else None
    right = participants[1] if len(participants) > 1 else None
    metadata = _market_metadata(market)
    market_kind = str(metadata.get("copilot_market_kind") or "")
    market_family = str(metadata.get("copilot_market_family") or "")

    if market and market_family == "player_prop":
        prop_score = _score_player_prop(db, event, market, snapshot, resolver or PropStatsResolver(db))
        if prop_score is None:
            return None
        probability_yes, confidence, reasons, features = prop_score
        probability_subject = str(metadata.get("copilot_subject_name") or "Player")
    elif market and market_family == "game_line":
        if not left or not right:
            return None
        game_line_score = _score_game_line(db, event, market, left, right)
        if game_line_score is None:
            return None
        probability_yes, confidence, reasons, features = game_line_score
        probability_subject = str(metadata.get("copilot_subject_name") or metadata.get("copilot_display_line_label") or market.title)
    else:
        if not left or not right:
            return None
        if event.sport_key == "MLB" and market_kind == "first_five_winner":
            left_win_probability, confidence, reasons, features = _score_mlb_first_five(db, event, left, right)
        else:
            left_win_probability, confidence, reasons, features = _score_team_winner(db, event, left, right)

        probability_yes = left_win_probability
        probability_subject = left.participant.display_name
        if market:
            yes_entry_target = _market_yes_entry(event, market)
            if not yes_entry_target:
                return None
            if yes_entry_target.participant_id != left.participant_id:
                probability_yes = round(1 - left_win_probability, 4)
                probability_subject = yes_entry_target.participant.display_name
            else:
                probability_subject = left.participant.display_name

    family_key = single_family_key(market.sport_key if market else event.sport_key, market_family)
    features["family_key"] = family_key
    confidence, scoring_diagnostics = _single_scoring_adjustments(
        db,
        family_key=family_key,
        event=event,
        market=market,
        snapshot=snapshot,
        metadata=metadata,
        features=features,
        probability_yes=probability_yes,
        base_confidence=confidence,
        left=left,
        right=right,
    )

    runtime_decision = None
    active_lineage = HEURISTIC_SINGLE_MODEL
    served_mode = "heuristic"
    if market:
        ml_result, runtime_decision = run_serving_inference(db, family_key=family_key, scope="single", features=features)
        if ml_result is not None:
            probability_yes = round(ml_result.probability, 4)
            confidence = round(ml_result.confidence, 4)
            active_lineage = ml_result.lineage
            served_mode = "ml"
            scoring_diagnostics = {
                **scoring_diagnostics,
                "confidence_semantics": "calibrated_probability",
                "base_confidence": confidence,
                "adjusted_confidence": confidence,
                "penalties": {
                    "thin_sample": 0.0,
                    "missing_context": 0.0,
                    "stale_data": 0.0,
                    "volatility": 0.0,
                    "market_disagreement": 0.0,
                },
                "serving_mode": "ml",
                "artifact_path": ml_result.artifact_path,
            }
            reasons = [*reasons, f"Served by {ml_result.lineage.model_name}."]
        else:
            if runtime_decision and runtime_decision.fallback_active and runtime_decision.last_error:
                scoring_diagnostics["fallback_reason"] = runtime_decision.last_error
                scoring_diagnostics["serving_mode"] = "heuristic_fallback"
                reasons = [*reasons, f"Served by heuristic fallback because ML was unavailable: {runtime_decision.last_error}"]
            else:
                scoring_diagnostics["serving_mode"] = "heuristic"

    fair_yes_price = round(probability_yes, 4)
    fair_no_price = round(1 - probability_yes, 4)

    yes_entry = snapshot.yes_ask if snapshot and snapshot.yes_ask is not None else snapshot.last_price if snapshot else None
    no_entry = snapshot.no_ask if snapshot and snapshot.no_ask is not None else (1 - snapshot.last_price) if snapshot and snapshot.last_price is not None else None
    yes_edge = fair_yes_price - yes_entry if yes_entry is not None else 0.0
    no_edge = fair_no_price - no_entry if no_entry is not None else 0.0

    if market_family == "player_prop":
        probability_label = "Model YES probability"
    elif market_kind == "spread":
        probability_label = "Model cover probability"
    elif market_kind == "total":
        probability_label = "Model total-side probability"
    else:
        probability_label = "Model win probability"
    reasons = [*reasons, f"{probability_label} for {probability_subject}: {probability_yes:.0%}"]

    if not market:
        selected_side = "yes" if yes_edge >= no_edge else "no"
        selected_side_probability = _selected_side_probability(probability_yes, selected_side)
        selection_score, signal_diagnostics = _finalize_single_scoring_diagnostics(
            diagnostics=scoring_diagnostics,
            selected_side_probability=selected_side_probability,
            selected_edge=max(yes_edge, no_edge),
            adjusted_confidence=confidence,
        )
        heuristic_metadata = dict(active_lineage.model_metadata or {})
        heuristic_metadata.update(
            {
                "family_key": family_key,
                "desired_mode": runtime_decision.desired_mode if runtime_decision else "heuristic",
                "effective_mode": served_mode,
                "runtime_health": runtime_decision.runtime_health if runtime_decision else "healthy",
            }
        )
        signal = SignalSnapshot(
            event_id=event.id,
            market_id=market.id if market else None,
            model_name=active_lineage.model_name if active_lineage.model_name else MODEL_NAME,
            model_version=active_lineage.model_version,
            calibration_version=active_lineage.calibration_version,
            feature_set_version=active_lineage.feature_set_version,
            model_metadata=heuristic_metadata,
            confidence=confidence,
            fair_yes_price=fair_yes_price,
            fair_no_price=fair_no_price,
            edge=max(yes_edge, no_edge),
            selection_score=selection_score,
            reasons=reasons,
            features=features,
            scoring_diagnostics=signal_diagnostics,
            captured_at=datetime.now(timezone.utc),
        )
        return ScoredRecommendation(
            recommendation=None,
            signal=signal,
            metadata=metadata,
        )

    force_yes_prop = market_family == "player_prop" and settings.prefer_yes_side_props
    if force_yes_prop:
        side = "yes"
        edge = yes_edge
        suggested_price = yes_entry if yes_entry is not None else fair_yes_price
        invalidation = f"Pull if YES entry moves above {min(fair_yes_price + 0.04, 0.99):.4f}"
        scoring_diagnostics["yes_side_forced"] = True
        if yes_edge < 0:
            selection_score, signal_diagnostics = _finalize_single_scoring_diagnostics(
                diagnostics={
                    **scoring_diagnostics,
                    "suppression_reasons": [
                        *list(scoring_diagnostics.get("suppression_reasons") or []),
                        "yes_side_negative_edge",
                    ],
                },
                selected_side_probability=_selected_side_probability(probability_yes, "yes"),
                selected_edge=yes_edge,
                adjusted_confidence=confidence,
            )
            heuristic_metadata = dict(active_lineage.model_metadata or {})
            heuristic_metadata.update(
                {
                    "family_key": family_key,
                    "desired_mode": runtime_decision.desired_mode if runtime_decision else "heuristic",
                    "effective_mode": served_mode,
                    "runtime_health": runtime_decision.runtime_health if runtime_decision else "healthy",
                }
            )
            signal = SignalSnapshot(
                event_id=event.id,
                market_id=market.id if market else None,
                model_name=active_lineage.model_name if active_lineage.model_name else MODEL_NAME,
                model_version=active_lineage.model_version,
                calibration_version=active_lineage.calibration_version,
                feature_set_version=active_lineage.feature_set_version,
                model_metadata=heuristic_metadata,
                confidence=confidence,
                fair_yes_price=fair_yes_price,
                fair_no_price=fair_no_price,
                edge=max(yes_edge, no_edge),
                selection_score=selection_score,
                reasons=reasons,
                features=features,
                scoring_diagnostics=signal_diagnostics,
                captured_at=datetime.now(timezone.utc),
            )
            return ScoredRecommendation(
                recommendation=None,
                signal=signal,
                metadata=metadata,
            )
    elif yes_edge >= no_edge:
        side = "yes"
        edge = yes_edge
        suggested_price = yes_entry if yes_entry is not None else fair_yes_price
        invalidation = f"Pull if YES entry moves above {min(fair_yes_price + 0.04, 0.99):.4f}"
    else:
        side = "no"
        edge = no_edge
        suggested_price = no_entry if no_entry is not None else fair_no_price
        invalidation = f"Pull if NO entry moves above {min(fair_no_price + 0.04, 0.99):.4f}"

    # Bug #2 P2: in ML mode, ml_result.confidence == ml_result.probability == P(YES).
    # Convert to the selected-side probability so watchlist_min_confidence and
    # _quality_tier don't unfairly suppress strong NO recommendations. Mirrors
    # how shadow capture already handles this in shadow.py:131.
    if served_mode == "ml":
        confidence = round(_selected_side_probability(probability_yes, side), 4)

    if market_family == "player_prop" and metadata.get("copilot_requires_lineup"):
        # Smarter #16: only append the "cancel if not confirmed" rider when
        # we don't already have a confirmed-in-lineup signal. A player
        # already confirmed in the starting lineup shouldn't carry a
        # disclaimer that contradicts the scoring outcome.
        lineup_data_complete = float(features.get("lineup_data_complete") or 0.0) >= 1.0
        player_in_starting_lineup = (
            float(features.get("player_in_starting_lineup") or 0.0) >= 1.0
        )
        if not (lineup_data_complete and player_in_starting_lineup):
            invalidation = (
                f"{invalidation}. Cancel if the player is not confirmed active / in the starting lineup."
            )

    selected_side_probability = _selected_side_probability(probability_yes, side)
    selected_subject_name = _selected_subject_name(
        event=event,
        market=market,
        metadata=metadata,
        side=side,
        default_subject_name=probability_subject,
    )
    selected_thesis_key = _winner_thesis_key(
        event=event,
        metadata=metadata,
        selected_subject_name=selected_subject_name,
    )
    source_type = str(metadata.get("copilot_source_type") or "standalone")
    source_market_ticker = str(metadata.get("copilot_source_market_ticker") or market.ticker)
    source_market_title = str(metadata.get("copilot_source_market_title") or market.title)
    display_market_title = str(metadata.get("copilot_display_market_title") or market.title)
    source_badge_label = str(metadata.get("copilot_source_badge_label") or ("Combo-derived" if source_type == "combo_derived" else ""))
    total_penalty = round(
        sum(float(value or 0.0) for value in dict(scoring_diagnostics.get("penalties") or {}).values()),
        4,
    )
    feature_flags = dict(scoring_diagnostics.get("feature_flags") or {})
    available_context_flags = sum(1 for value in feature_flags.values() if value)
    context_coverage_score = round(available_context_flags / len(feature_flags), 4) if feature_flags else 1.0
    quality_tier = _quality_tier(
        selected_side_probability=selected_side_probability,
        adjusted_confidence=confidence,
        context_coverage_score=context_coverage_score,
        total_penalty=total_penalty,
        served_mode=served_mode,
    )
    scoring_diagnostics = {
        **scoring_diagnostics,
        "selected_side": side,
        "selected_side_probability": selected_side_probability,
        "selected_subject_name": selected_subject_name,
        "selected_thesis_key": selected_thesis_key,
        "context_coverage_score": context_coverage_score,
        "quality_tier": quality_tier,
        "source_type": source_type,
        "source_market_ticker": source_market_ticker,
        "source_market_title": source_market_title,
        "display_market_title": display_market_title,
        "source_badge_label": source_badge_label or None,
        "suggested_price": round(suggested_price, 4),
        "invalidation": invalidation,
    }
    selection_score, signal_diagnostics = _finalize_single_scoring_diagnostics(
        diagnostics=scoring_diagnostics,
        selected_side_probability=selected_side_probability,
        selected_edge=edge,
        adjusted_confidence=confidence,
    )
    active_metadata = dict(active_lineage.model_metadata or {})
    active_metadata.update(
        {
            "family_key": family_key,
            "desired_mode": runtime_decision.desired_mode if runtime_decision else "heuristic",
            "effective_mode": served_mode,
            "runtime_health": runtime_decision.runtime_health if runtime_decision else "healthy",
        }
    )
    suppression_reasons = list(signal_diagnostics.get("suppression_reasons") or [])
    if (
        served_mode != "ml"
        and market_family == "winner"
        and market_kind in {"game_winner", "first_five_winner"}
        and selected_side_probability < settings.watchlist_min_selected_prob_heuristic_winner
    ):
        suppression_reasons.append("winner_selected_probability_floor")
    if edge < settings.watchlist_min_edge:
        suppression_reasons.append("min_edge")
    if confidence < settings.watchlist_min_confidence:
        suppression_reasons.append("min_confidence")
    if not snapshot:
        suppression_reasons.append("critical_market_snapshot_missing")
    # Bug #49: Kalshi only sells YES contracts — a NO recommendation has no
    # direct path to act on. For winner markets the paired-market YES is
    # scored independently and covers the same signal, so suppressing here
    # keeps the watchlist actionable without losing the underlying edge.
    # For player props there's no clean YES counterpart; suppressing prevents
    # surfacing a non-actionable pick.
    if side == "no":
        suppression_reasons.append("no_side_not_actionable_on_kalshi")
    # Smarter #16: lineup data IS confirmed AND the player is NOT in the
    # starting lineup. The 0.025 missing-context penalty was far too lenient
    # for a clear scratch/DNP signal — suppress entirely rather than nudge.
    if str(scoring_diagnostics.get("lineup_suppression_reason") or "") == "player_not_in_starting_lineup":
        suppression_reasons.append("player_not_in_starting_lineup")
    signal_diagnostics = {
        **signal_diagnostics,
        "suppression_reasons": suppression_reasons,
    }
    now_captured_at = datetime.now(timezone.utc)
    signal = SignalSnapshot(
        event_id=event.id,
        market_id=market.id if market else None,
        model_name=active_lineage.model_name if active_lineage.model_name else MODEL_NAME,
        model_version=active_lineage.model_version,
        calibration_version=active_lineage.calibration_version,
        feature_set_version=active_lineage.feature_set_version,
        model_metadata=active_metadata,
        confidence=confidence,
        fair_yes_price=fair_yes_price,
        fair_no_price=fair_no_price,
        edge=max(yes_edge, no_edge),
        selection_score=selection_score,
        reasons=reasons,
        features=features,
        scoring_diagnostics=signal_diagnostics,
        captured_at=now_captured_at,
    )

    if suppression_reasons:
        return ScoredRecommendation(
            recommendation=None,
            signal=signal,
            metadata=metadata,
        )

    return ScoredRecommendation(
        recommendation=Recommendation(
            event_id=event.id,
            market_id=market.id,
            side=side,
            action="buy",
            status="active",
            suggested_price=round(suggested_price, 4),
            edge=round(edge, 4),
            confidence=round(confidence, 4),
            selection_score=selection_score,
            model_name=active_lineage.model_name,
            model_version=active_lineage.model_version,
            calibration_version=active_lineage.calibration_version,
            feature_set_version=active_lineage.feature_set_version,
            model_metadata=active_metadata,
            invalidation=invalidation,
            rationale="; ".join(reasons),
            scoring_diagnostics=signal_diagnostics,
            captured_at=now_captured_at,
        ),
        signal=signal,
        metadata=metadata,
    )


def score_event(
    db: Session,
    event: Event,
    market: Market | None,
    snapshot: MarketSnapshot | None,
    resolver: PropStatsResolver | None = None,
) -> Recommendation | None:
    scored = _build_scored_recommendation(db, event, market, snapshot, resolver=resolver)
    if not scored:
        return None
    db.add(scored.signal)
    return scored.recommendation


def _quality_tier_rank(value: str | None) -> int:
    return {"high": 2, "medium": 1, "low": 0}.get((value or "").lower(), -1)


def _enforce_prop_monotonicity(
    scored_recommendations: list[tuple[Market, ScoredRecommendation]],
    *,
    summary: WatchlistGenerationSummary | None = None,
) -> None:
    from collections import defaultdict

    settings = get_settings()
    grouped: dict[tuple[int, str, str], list[tuple[Market, ScoredRecommendation]]] = defaultdict(list)
    for market, scored in scored_recommendations:
        metadata = scored.metadata or {}
        if str(metadata.get("copilot_market_family") or "") != "player_prop":
            continue
        subject_name = str(metadata.get("copilot_subject_name") or "").strip()
        stat_key = str(metadata.get("copilot_stat_key") or "").strip()
        threshold = metadata.get("copilot_threshold")
        if not subject_name or not stat_key or threshold is None or market.event_id is None:
            continue
        grouped[(market.event_id, subject_name.lower(), stat_key)].append((market, scored))

    for group in grouped.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda item: float((item[1].metadata or {}).get("copilot_threshold") or 0.0))
        for index in range(1, len(group)):
            previous_scored = group[index - 1][1]
            current_scored = group[index][1]
            previous_probability = float(previous_scored.signal.fair_yes_price or 0.0)
            current_probability = float(current_scored.signal.fair_yes_price or 0.0)
            if current_probability <= previous_probability:
                continue

            clamped_probability = round(previous_probability, 4)
            current_scored.signal.fair_yes_price = clamped_probability
            current_scored.signal.fair_no_price = round(1 - clamped_probability, 4)

            signal_diagnostics = dict(current_scored.signal.scoring_diagnostics or {})
            signal_diagnostics["monotonicity_adjusted"] = True
            current_scored.signal.scoring_diagnostics = signal_diagnostics

            recommendation = current_scored.recommendation
            if recommendation is None:
                continue

            recommendation.edge = round(clamped_probability - recommendation.suggested_price, 4)
            # Codex PR #33 P2/P3: mirror the recomputed edge AND the clamped
            # selected_side_probability onto the signal so coverage captures
            # (which read signal fields when recommendation is None) persist
            # the post-clamp values, not the stale pre-clamp pair.
            current_scored.signal.edge = recommendation.edge
            signal_diagnostics = dict(current_scored.signal.scoring_diagnostics or {})
            signal_diagnostics["selected_side_probability"] = clamped_probability
            signal_diagnostics["monotonicity_adjusted"] = True
            current_scored.signal.scoring_diagnostics = signal_diagnostics
            recommendation.scoring_diagnostics = {
                **dict(recommendation.scoring_diagnostics or {}),
                "selected_side_probability": clamped_probability,
                "monotonicity_adjusted": True,
            }

            # Bug #9: when the clamp drops edge below the watchlist floor,
            # the user shouldn't see this pick — it would have been filtered
            # out had the lowered probability been the original. Record the
            # suppression reason on the signal (so ops can explain it) and
            # clear the recommendation so downstream surfaces drop it.
            if recommendation.edge < settings.watchlist_min_edge:
                signal_diagnostics = dict(current_scored.signal.scoring_diagnostics or {})
                signal_diagnostics["monotonicity_edge_below_min"] = True
                suppression_reasons = list(signal_diagnostics.get("suppression_reasons") or [])
                if "monotonicity_edge_below_min" not in suppression_reasons:
                    suppression_reasons.append("monotonicity_edge_below_min")
                signal_diagnostics["suppression_reasons"] = suppression_reasons
                current_scored.signal.scoring_diagnostics = signal_diagnostics
                current_scored.recommendation = None
                # Codex PR #33 round-3 P3: the market was counted as
                # ``recommended`` upstream before monotonicity ran. Reclassify
                # it so scorer-outcome metrics surface this suppression
                # instead of overreporting recommendations.
                if summary is not None:
                    counts = summary.outcome_reason_counts
                    if counts.get("recommended", 0) > 0:
                        counts["recommended"] = counts["recommended"] - 1
                        if counts["recommended"] == 0:
                            counts.pop("recommended", None)
                    counts["suppressed_monotonicity_edge_below_min"] = (
                        counts.get("suppressed_monotonicity_edge_below_min", 0) + 1
                    )


def _dedupe_winner_recommendations(
    scored_recommendations: list[tuple[Market, ScoredRecommendation]],
) -> tuple[list[tuple[Market, ScoredRecommendation]], int, int]:
    deduped: dict[str, tuple[Market, ScoredRecommendation]] = {}
    passthrough: list[tuple[Market, ScoredRecommendation]] = []
    collapsed_count = 0
    combo_suppressed = 0

    for market, scored in scored_recommendations:
        recommendation = scored.recommendation
        if recommendation is None:
            continue
        diagnostics = dict(recommendation.scoring_diagnostics or {})
        thesis_key = diagnostics.get("selected_thesis_key")
        if not thesis_key:
            passthrough.append((market, scored))
            continue

        current = deduped.get(str(thesis_key))
        if current is None:
            deduped[str(thesis_key)] = (market, scored)
            continue

        _, existing_scored = current
        existing_recommendation = existing_scored.recommendation
        assert existing_recommendation is not None

        candidate_tuple = (
            recommendation.selection_score or 0.0,
            recommendation.edge,
            -recommendation.suggested_price,
            _quality_tier_rank(diagnostics.get("quality_tier")),
        )
        existing_tuple = (
            existing_recommendation.selection_score or 0.0,
            existing_recommendation.edge,
            -existing_recommendation.suggested_price,
            _quality_tier_rank((existing_recommendation.scoring_diagnostics or {}).get("quality_tier")),
        )
        if candidate_tuple > existing_tuple:
            if str((existing_recommendation.scoring_diagnostics or {}).get("source_type") or "") == "combo_derived":
                combo_suppressed += 1
            deduped[str(thesis_key)] = (market, scored)
        else:
            if str(diagnostics.get("source_type") or "") == "combo_derived":
                combo_suppressed += 1
        collapsed_count += 1

    return [*passthrough, *deduped.values()], collapsed_count, combo_suppressed


def _prediction_recommendation_tuple(prediction: Prediction) -> tuple[float, float, float, int]:
    diagnostics = dict(prediction.scoring_diagnostics or {})
    return (
        prediction.selection_score or 0.0,
        prediction.edge,
        -prediction.suggested_price,
        _quality_tier_rank(diagnostics.get("quality_tier")),
    )


def _build_recommendation_from_prediction(prediction: Prediction) -> Recommendation:
    return Recommendation(
        event_id=prediction.event_id,
        market_id=prediction.market_id,
        side=prediction.side,
        action=prediction.action,
        status="active",
        suggested_price=prediction.suggested_price,
        edge=prediction.edge,
        confidence=prediction.confidence,
        selection_score=prediction.selection_score,
        model_name=prediction.model_name,
        model_version=prediction.model_version,
        calibration_version=prediction.calibration_version,
        feature_set_version=prediction.feature_set_version,
        model_metadata=dict(prediction.model_metadata or {}),
        invalidation=prediction.invalidation or "Pull if execution conditions materially change.",
        rationale=prediction.rationale,
        scoring_diagnostics=dict(prediction.scoring_diagnostics or {}),
        captured_at=prediction.captured_at,
    )


def _signal_snapshot_from_prediction(prediction: Prediction) -> SignalSnapshot:
    fair_yes_price = float(prediction.fair_yes_price or 0.0)
    fair_no_price = float(prediction.fair_no_price if prediction.fair_no_price is not None else (1 - fair_yes_price))
    return SignalSnapshot(
        event_id=prediction.event_id,
        market_id=prediction.market_id,
        captured_at=prediction.captured_at,
        model_name=prediction.model_name or MODEL_NAME,
        model_version=prediction.model_version,
        calibration_version=prediction.calibration_version,
        feature_set_version=prediction.feature_set_version,
        model_metadata=dict(prediction.model_metadata or {}),
        confidence=prediction.confidence,
        fair_yes_price=fair_yes_price,
        fair_no_price=fair_no_price,
        edge=prediction.edge,
        selection_score=prediction.selection_score,
        reasons=list(prediction.reasons or []),
        features=dict(prediction.features or {}),
        scoring_diagnostics=dict(prediction.scoring_diagnostics or {}),
    )


def _parlay_candidate_from_prediction(prediction: Prediction) -> ParlayCandidateInput | None:
    market = prediction.market
    event = market.event if market is not None else None
    if market is None or event is None:
        return None
    return ParlayCandidateInput(
        event=event,
        market=market,
        recommendation=_build_recommendation_from_prediction(prediction),
        signal=_signal_snapshot_from_prediction(prediction),
        prediction=prediction,
        metadata=dict(market.raw_data or {}),
    )


def _maintenance_watchlist_market_batch(
    db: Session,
    *,
    cursor_market_id: int | None = None,
    batch_size: int = 100,
) -> list[Market]:
    stmt = (
        select(Market)
        .options(joinedload(Market.event).selectinload(Event.participants).joinedload(EventParticipant.participant))
        .where(Market.event_id.is_not(None), Market.status.in_(tuple(OPEN_MARKET_STATUSES)))
        .order_by(Market.id.asc())
    )
    if cursor_market_id is not None:
        stmt = stmt.where(Market.id > cursor_market_id)
    return db.scalars(stmt.limit(batch_size)).all()


def _explicit_watchlist_market_batch(
    db: Session,
    *,
    market_ids: list[int],
    cursor_index: int = 0,
    batch_size: int = 100,
) -> tuple[list[Market], WatchlistGenerationSummary, int | None, bool]:
    summary = WatchlistGenerationSummary()
    batch_ids = market_ids[cursor_index : cursor_index + batch_size]
    next_index = cursor_index + len(batch_ids)
    complete = next_index >= len(market_ids)
    if not batch_ids:
        return [], summary, None, True

    current_event_ids = set(current_watchlist_event_ids(db))
    rows = db.scalars(
        select(Market)
        .options(joinedload(Market.event).selectinload(Event.participants).joinedload(EventParticipant.participant))
        .where(Market.id.in_(tuple(sorted(batch_ids))))
    ).all()
    by_id = {market.id: market for market in rows}
    loaded_by_id: dict[int, Market] = {}
    for market_id in batch_ids:
        market = by_id.get(market_id)
        if market is None:
            _record_candidate_filter(summary, "not_found")
            continue
        if market.event_id is None or market.event is None:
            _record_candidate_filter(summary, "event_missing")
            continue
        if (market.sport_key or "").upper() not in CURRENT_WATCHLIST_SPORTS:
            _record_candidate_filter(summary, "sport_not_supported")
            continue
        if (market.status or "").lower() not in OPEN_MARKET_STATUSES:
            _record_candidate_filter(summary, "status_not_open")
            continue
        if market.event_id not in current_event_ids:
            _record_candidate_filter(summary, "not_current_event")
            continue
        loaded_by_id[market_id] = market

    ordered = [loaded_by_id[market_id] for market_id in batch_ids if market_id in loaded_by_id]
    summary.loaded_candidate_market_count = len(ordered)
    return ordered, summary, (None if complete else next_index), complete


def _annotate_current_watchlist_flag(scored: ScoredRecommendation, *, current_watchlist_market: bool) -> None:
    signal_diagnostics = dict(scored.signal.scoring_diagnostics or {})
    signal_diagnostics["current_watchlist_market"] = current_watchlist_market
    scored.signal.scoring_diagnostics = signal_diagnostics
    if scored.recommendation is not None:
        recommendation_diagnostics = dict(scored.recommendation.scoring_diagnostics or {})
        recommendation_diagnostics["current_watchlist_market"] = current_watchlist_market
        scored.recommendation.scoring_diagnostics = recommendation_diagnostics


def _score_watchlist_markets_batch(
    db: Session,
    *,
    markets: list[Market],
    resolver: PropStatsResolver | None = None,
) -> tuple[WatchlistGenerationSummary, list[ScoredWatchlistCapture]]:
    """Score a batch of markets without persisting anything.

    Slice 6: this is the pure half of the watchlist batch pipeline. It
    consults the latest market snapshots, runs ``_build_scored_recommendation``
    over each market, and updates the summary's diagnostic counters based
    on the scored output. It does **not** call ``db.add`` or
    ``capture_prediction``; the caller is responsible for handing the
    returned captures to ``_persist_scored_watchlist_captures``.
    """
    summary = WatchlistGenerationSummary()
    captures: list[ScoredWatchlistCapture] = []
    if not markets:
        return summary, captures
    active_resolver = resolver or PropStatsResolver(db, allow_network=False)

    latest_snapshots = latest_snapshot_by_market_id(db, [market.id for market in markets])
    for market in markets:
        if not market.event:
            _record_scorer_outcome(summary, "mapping_failed")
            continue
        latest_snapshot = latest_snapshots.get(market.id)
        scored = _build_scored_recommendation(db, market.event, market, latest_snapshot, resolver=active_resolver)
        if scored is None:
            _record_scorer_outcome(summary, _scoring_none_reason(market))
            continue
        summary.scored_market_count += 1
        current_watchlist_market = is_current_watchlist_market(market)
        _annotate_current_watchlist_flag(scored, current_watchlist_market=current_watchlist_market)
        if scored.recommendation is not None:
            _record_scorer_outcome(summary, "recommended")
            captures.append(
                ScoredWatchlistCapture(
                    market=market,
                    scored=scored,
                    capture_scope="recommendation",
                )
            )
            summary.prediction_count += 1
            continue
        _record_scorer_outcome(
            summary,
            _suppression_outcome_reason(scored, current_watchlist_market=current_watchlist_market),
        )
        diagnostics = dict(scored.signal.scoring_diagnostics or {})
        suppression_reasons = {str(value) for value in list(diagnostics.get("suppression_reasons") or [])}
        if "winner_selected_probability_floor" in suppression_reasons:
            summary.heuristic_longshots_suppressed += 1
        if "critical_market_snapshot_missing" in suppression_reasons:
            summary.critical_context_suppressed += 1
        if current_watchlist_market:
            captures.append(
                ScoredWatchlistCapture(
                    market=market,
                    scored=scored,
                    capture_scope="coverage",
                )
            )
            summary.prediction_count += 1
            summary.coverage_prediction_count += 1
        else:
            # The scored signal still needs to be persisted even when no
            # prediction is captured — production behavior pre-Slice-6 always
            # called ``db.add(scored.signal)`` for every successfully-scored
            # market regardless of whether ``capture_prediction`` ran.
            captures.append(
                ScoredWatchlistCapture(
                    market=market,
                    scored=scored,
                    capture_scope=None,
                )
            )
    return summary, captures


def _persist_scored_watchlist_captures(
    db: Session,
    *,
    run_id: int,
    captures: list[ScoredWatchlistCapture],
) -> None:
    """Persist the side-effect tail of ``_score_watchlist_markets_batch``.

    Slice 6: split out so the scoring kernel above is unit-testable as a
    pure function. Iterates the captures, stages each ``SignalSnapshot``
    via ``db.add``, and routes ``capture_prediction`` calls to either the
    ``"recommendation"`` or ``"coverage"`` scope (or skips it for captures
    that were emitted purely for signal persistence).
    """
    if not captures:
        return
    for capture in captures:
        db.add(capture.scored.signal)
        if capture.capture_scope is None:
            continue
        capture_prediction(
            db,
            run_id=run_id,
            event=capture.market.event,
            market=capture.market,
            recommendation=capture.scored.recommendation,
            signal=capture.scored.signal,
            metadata=capture.scored.metadata,
            capture_scope=capture.capture_scope,
        )
    db.flush()


def stage_maintenance_watchlist_batch(
    db: Session,
    *,
    run_id: int,
    resolver: PropStatsResolver | None = None,
    cursor_market_id: int | None = None,
    batch_size: int = 100,
) -> tuple[WatchlistGenerationSummary, int | None, bool]:
    markets = _maintenance_watchlist_market_batch(
        db,
        cursor_market_id=cursor_market_id,
        batch_size=batch_size,
    )
    if not markets:
        return WatchlistGenerationSummary(), None, True

    summary, captures = _score_watchlist_markets_batch(
        db,
        markets=markets,
        resolver=resolver,
    )
    _persist_scored_watchlist_captures(db, run_id=run_id, captures=captures)
    return summary, markets[-1].id if markets else cursor_market_id, len(markets) < batch_size


def stage_current_slate_watchlist_batch(
    db: Session,
    *,
    run_id: int,
    market_ids: list[int],
    resolver: PropStatsResolver | None = None,
    cursor_index: int = 0,
    batch_size: int = 100,
) -> tuple[WatchlistGenerationSummary, int | None, bool]:
    markets, batch_summary, next_index, complete = _explicit_watchlist_market_batch(
        db,
        market_ids=market_ids,
        cursor_index=cursor_index,
        batch_size=batch_size,
    )
    if not markets:
        return batch_summary, next_index, complete
    scoring_summary, captures = _score_watchlist_markets_batch(
        db,
        markets=markets,
        resolver=resolver,
    )
    scoring_summary.loaded_candidate_market_count += batch_summary.loaded_candidate_market_count
    scoring_summary.filtered_candidate_market_count += batch_summary.filtered_candidate_market_count
    scoring_summary.candidate_filter_reason_counts = _merge_count_maps(
        scoring_summary.candidate_filter_reason_counts,
        batch_summary.candidate_filter_reason_counts,
    )
    _persist_scored_watchlist_captures(db, run_id=run_id, captures=captures)
    return scoring_summary, next_index, complete


def _apply_prediction_monotonicity(
    predictions: list[Prediction],
    *,
    summary: WatchlistGenerationSummary | None = None,
) -> None:
    from collections import defaultdict

    settings = get_settings()
    grouped: dict[tuple[int, str, str], list[Prediction]] = defaultdict(list)
    for prediction in predictions:
        if prediction.market_family != "player_prop":
            continue
        subject_name = str(prediction.subject_name or "").strip()
        stat_key = str(prediction.stat_key or "").strip()
        threshold = prediction.threshold
        if not subject_name or not stat_key or threshold is None or prediction.event_id is None:
            continue
        grouped[(prediction.event_id, subject_name.lower(), stat_key)].append(prediction)

    for group in grouped.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda prediction: float(prediction.threshold or 0.0))
        for index in range(1, len(group)):
            previous = group[index - 1]
            current = group[index]
            previous_probability = float(previous.fair_yes_price or 0.0)
            current_probability = float(current.fair_yes_price or 0.0)
            if current_probability <= previous_probability:
                continue
            clamped_probability = round(previous_probability, 4)
            current.fair_yes_price = clamped_probability
            current.fair_no_price = round(1 - clamped_probability, 4)
            current.edge = round(clamped_probability - current.suggested_price, 4)
            diagnostics = dict(current.scoring_diagnostics or {})
            diagnostics["selected_side_probability"] = clamped_probability
            diagnostics["monotonicity_adjusted"] = True
            if current.edge < settings.watchlist_min_edge:
                # Bug #9: the clamp dropped edge below the watchlist floor —
                # mark the prediction suppressed so finalize_staged_watchlist
                # excludes it from the dedup pass (and the operator never
                # sees it on the watchlist). capture_scope == "suppressed"
                # is the same filter the downstream pipeline already uses.
                diagnostics["monotonicity_edge_below_min"] = True
                suppression_reasons = list(diagnostics.get("suppression_reasons") or [])
                if "monotonicity_edge_below_min" not in suppression_reasons:
                    suppression_reasons.append("monotonicity_edge_below_min")
                diagnostics["suppression_reasons"] = suppression_reasons
                current.capture_scope = "suppressed"
                # Codex PR #33 round-4 P2: the staged path counted this
                # prediction as ``recommended`` during batch scoring; keep
                # the run summary aligned with the predictions finalize
                # actually emits.
                if summary is not None:
                    counts = summary.outcome_reason_counts
                    if counts.get("recommended", 0) > 0:
                        counts["recommended"] = counts["recommended"] - 1
                        if counts["recommended"] == 0:
                            counts.pop("recommended", None)
                    counts["suppressed_monotonicity_edge_below_min"] = (
                        counts.get("suppressed_monotonicity_edge_below_min", 0) + 1
                    )
            current.scoring_diagnostics = diagnostics


def _dedupe_prediction_recommendations(
    predictions: list[Prediction],
) -> tuple[list[Prediction], int, int]:
    deduped: dict[str, Prediction] = {}
    passthrough: list[Prediction] = []
    collapsed_count = 0
    combo_suppressed = 0

    for prediction in predictions:
        diagnostics = dict(prediction.scoring_diagnostics or {})
        thesis_key = diagnostics.get("selected_thesis_key")
        if not thesis_key:
            passthrough.append(prediction)
            continue

        current = deduped.get(str(thesis_key))
        if current is None:
            deduped[str(thesis_key)] = prediction
            continue

        if _prediction_recommendation_tuple(prediction) > _prediction_recommendation_tuple(current):
            if str((current.scoring_diagnostics or {}).get("source_type") or "") == "combo_derived":
                combo_suppressed += 1
            deduped[str(thesis_key)] = prediction
        else:
            if str(diagnostics.get("source_type") or "") == "combo_derived":
                combo_suppressed += 1
        collapsed_count += 1

    return [*passthrough, *deduped.values()], collapsed_count, combo_suppressed


def finalize_staged_watchlist(
    db: Session,
    *,
    run_id: int,
    capture_parlays: bool = True,
) -> WatchlistGenerationSummary:
    summary = WatchlistGenerationSummary()
    predictions = db.scalars(
        select(Prediction)
        .options(
            joinedload(Prediction.market)
            .joinedload(Market.event)
            .selectinload(Event.participants)
            .joinedload(EventParticipant.participant)
        )
        .where(Prediction.run_id == run_id)
        .order_by(Prediction.id.asc())
    ).all()

    candidate_predictions = [prediction for prediction in predictions if prediction.capture_scope != "coverage"]
    _apply_prediction_monotonicity(candidate_predictions, summary=summary)
    candidate_predictions = [prediction for prediction in predictions if prediction.capture_scope not in {"coverage", "suppressed"}]
    winners, collapsed_count, combo_suppressed = _dedupe_prediction_recommendations(candidate_predictions)
    summary.inverse_winner_duplicates_collapsed = collapsed_count
    summary.combo_prop_candidates_suppressed = combo_suppressed
    winner_ids = {prediction.id for prediction in winners}

    for prediction in predictions:
        if prediction.capture_scope == "coverage":
            continue
        if prediction.id in winner_ids:
            prediction.capture_scope = "recommendation"
            continue
        current_flag = bool((prediction.scoring_diagnostics or {}).get("current_watchlist_market"))
        if current_flag:
            prediction.capture_scope = "coverage"
            continue
        db.delete(prediction)

    db.flush()
    db.query(Recommendation).delete()
    if capture_parlays:
        clear_active_parlay_watchlist(db)
    db.flush()

    parlay_candidates: list[ParlayCandidateInput] = []
    for prediction in winners:
        diagnostics = dict(prediction.scoring_diagnostics or {})
        quality_tier = str(diagnostics.get("quality_tier") or "medium")
        summary.quality_tier_counts[quality_tier] = summary.quality_tier_counts.get(quality_tier, 0) + 1
        if str(diagnostics.get("source_type") or "") == "combo_derived":
            summary.combo_prop_candidates_emitted += 1
        db.add(_build_recommendation_from_prediction(prediction))
        summary.recommendation_count += 1
        candidate = _parlay_candidate_from_prediction(prediction)
        if candidate is not None:
            parlay_candidates.append(candidate)

    db.flush()
    if capture_parlays:
        parlay_recommendation_count, parlay_prediction_count = capture_parlay_artifacts(
            db,
            run_id=run_id,
            candidates=parlay_candidates,
        )
        summary.parlay_recommendation_count = parlay_recommendation_count
        summary.parlay_prediction_count = parlay_prediction_count
    db.flush()
    summary.prediction_count = int(
        db.scalar(select(func.count()).select_from(Prediction).where(Prediction.run_id == run_id))
        or 0
    )
    summary.scored_market_count = len(predictions)
    summary.coverage_prediction_count = int(
        db.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.run_id == run_id, Prediction.capture_scope == "coverage")
        )
        or 0
    )
    return summary


def finalize_current_slate_watchlist(
    db: Session,
    *,
    run_id: int,
    candidate_market_ids: set[int],
    staged_summary: WatchlistGenerationSummary | None = None,
) -> WatchlistGenerationSummary:
    summary = WatchlistGenerationSummary()
    predictions = db.scalars(
        select(Prediction)
        .options(
            joinedload(Prediction.market)
            .joinedload(Market.event)
            .selectinload(Event.participants)
            .joinedload(EventParticipant.participant)
        )
        .where(Prediction.run_id == run_id)
        .order_by(Prediction.id.asc())
    ).all()

    candidate_predictions = [prediction for prediction in predictions if prediction.capture_scope != "coverage"]
    # Bug #9 follow-up (codex PR #33 round-5 P2): in the staged path the
    # original ``recommended`` counts live in ``staged_summary``, not the
    # fresh summary this function builds. Apply the metric adjustment to
    # ``staged_summary`` so the merge in ingestion.py doesn't double-count
    # a market as both recommended *and* suppressed.
    monotonicity_metric_target = staged_summary if staged_summary is not None else summary
    _apply_prediction_monotonicity(candidate_predictions, summary=monotonicity_metric_target)
    candidate_predictions = [prediction for prediction in predictions if prediction.capture_scope not in {"coverage", "suppressed"}]
    winners, collapsed_count, combo_suppressed = _dedupe_prediction_recommendations(candidate_predictions)
    summary.inverse_winner_duplicates_collapsed = collapsed_count
    summary.combo_prop_candidates_suppressed = combo_suppressed
    winner_ids = {prediction.id for prediction in winners}

    for prediction in predictions:
        if prediction.capture_scope == "coverage":
            continue
        if prediction.id in winner_ids:
            prediction.capture_scope = "recommendation"
            continue
        current_flag = bool((prediction.scoring_diagnostics or {}).get("current_watchlist_market"))
        if current_flag:
            prediction.capture_scope = "coverage"
            continue
        db.delete(prediction)

    db.flush()
    if candidate_market_ids:
        db.query(Recommendation).filter(
            Recommendation.market_id.in_(tuple(sorted(candidate_market_ids)))
        ).delete(synchronize_session=False)
    db.flush()

    for prediction in winners:
        diagnostics = dict(prediction.scoring_diagnostics or {})
        quality_tier = str(diagnostics.get("quality_tier") or "medium")
        summary.quality_tier_counts[quality_tier] = summary.quality_tier_counts.get(quality_tier, 0) + 1
        if str(diagnostics.get("source_type") or "") == "combo_derived":
            summary.combo_prop_candidates_emitted += 1
        db.add(_build_recommendation_from_prediction(prediction))
        summary.recommendation_count += 1

    db.flush()
    summary.prediction_count = int(
        db.scalar(select(func.count()).select_from(Prediction).where(Prediction.run_id == run_id))
        or 0
    )
    return summary


def regenerate_watchlist(
    db: Session,
    *,
    run_id: int | None = None,
    resolver: PropStatsResolver | None = None,
    allowed_market_ids: set[int] | None = None,
    replace_all: bool = True,
    capture_parlays: bool = True,
    candidate_markets: list[Market] | None = None,
) -> WatchlistGenerationSummary:
    if replace_all:
        db.query(Recommendation).delete()
        if capture_parlays:
            clear_active_parlay_watchlist(db)
    elif allowed_market_ids:
        db.query(Recommendation).filter(Recommendation.market_id.in_(tuple(sorted(allowed_market_ids)))).delete(synchronize_session=False)
    summary = WatchlistGenerationSummary()
    active_resolver = resolver or PropStatsResolver(db, allow_network=False)
    parlay_candidates: list[ParlayCandidateInput] = []
    pending_recommendations: list[tuple[Market, ScoredRecommendation]] = []
    current_coverage_candidates: list[tuple[Market, ScoredRecommendation]] = []
    if candidate_markets is not None:
        markets = [
            market
            for market in candidate_markets
            if market.event_id is not None and (market.status or "").lower() in OPEN_MARKET_STATUSES
        ]
        if allowed_market_ids is not None:
            if not allowed_market_ids:
                return summary
            markets = [market for market in markets if market.id in allowed_market_ids]
    else:
        stmt = (
            select(Market)
            .options(joinedload(Market.event).selectinload(Event.participants).joinedload(EventParticipant.participant))
            .where(Market.event_id.is_not(None), Market.status.in_(tuple(OPEN_MARKET_STATUSES)))
        )
        if allowed_market_ids is not None:
            if not allowed_market_ids:
                return summary
            stmt = stmt.where(Market.id.in_(tuple(sorted(allowed_market_ids))))
        markets = db.scalars(stmt).all()
    latest_snapshots = latest_snapshot_by_market_id(db, [market.id for market in markets])
    for market in markets:
        if not market.event:
            continue
        latest_snapshot = latest_snapshots.get(market.id)
        scored = _build_scored_recommendation(db, market.event, market, latest_snapshot, resolver=active_resolver)
        if scored:
            summary.scored_market_count += 1
            db.add(scored.signal)
            current_watchlist_market = is_current_watchlist_market(market)
            if current_watchlist_market:
                current_coverage_candidates.append((market, scored))
            if scored.recommendation:
                _record_scorer_outcome(summary, "recommended")
                pending_recommendations.append((market, scored))
            else:
                _record_scorer_outcome(
                    summary,
                    _suppression_outcome_reason(scored, current_watchlist_market=current_watchlist_market),
                )
                diagnostics = dict(scored.signal.scoring_diagnostics or {})
                suppression_reasons = {str(value) for value in list(diagnostics.get("suppression_reasons") or [])}
                if "winner_selected_probability_floor" in suppression_reasons:
                    summary.heuristic_longshots_suppressed += 1
                if "critical_market_snapshot_missing" in suppression_reasons:
                    summary.critical_context_suppressed += 1
        else:
            _record_scorer_outcome(summary, _scoring_none_reason(market))

    _enforce_prop_monotonicity(pending_recommendations, summary=summary)
    deduped_recommendations, collapsed_count, combo_suppressed = _dedupe_winner_recommendations(pending_recommendations)
    summary.inverse_winner_duplicates_collapsed = collapsed_count
    summary.combo_prop_candidates_suppressed += combo_suppressed

    db.flush()
    recommendation_market_ids: set[int] = set()
    for market, scored in deduped_recommendations:
        assert scored.recommendation is not None
        recommendation_market_ids.add(market.id)
        diagnostics = dict(scored.recommendation.scoring_diagnostics or {})
        quality_tier = str(diagnostics.get("quality_tier") or "medium")
        summary.quality_tier_counts[quality_tier] = summary.quality_tier_counts.get(quality_tier, 0) + 1
        if str(diagnostics.get("source_type") or "") == "combo_derived":
            summary.combo_prop_candidates_emitted += 1
        db.add(scored.recommendation)
        prediction = capture_prediction(
            db,
            run_id=run_id,
            event=market.event,
            market=market,
            recommendation=scored.recommendation,
            signal=scored.signal,
            metadata=scored.metadata,
            capture_scope="recommendation",
        )
        summary.recommendation_count += 1
        summary.prediction_count += 1
        parlay_candidates.append(
            ParlayCandidateInput(
                event=market.event,
                market=market,
                recommendation=scored.recommendation,
                signal=scored.signal,
                prediction=prediction,
                metadata=scored.metadata,
            )
        )
    for market, scored in current_coverage_candidates:
        if market.id in recommendation_market_ids:
            continue
        capture_prediction(
            db,
            run_id=run_id,
            event=market.event,
            market=market,
            recommendation=None,
            signal=scored.signal,
            metadata=scored.metadata,
            capture_scope="coverage",
        )
        summary.prediction_count += 1
        summary.coverage_prediction_count += 1
    db.flush()
    if capture_parlays:
        parlay_recommendation_count, parlay_prediction_count = capture_parlay_artifacts(
            db,
            run_id=run_id,
            candidates=parlay_candidates,
        )
        summary.parlay_recommendation_count = parlay_recommendation_count
        summary.parlay_prediction_count = parlay_prediction_count
    db.flush()
    return summary
