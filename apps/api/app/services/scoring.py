from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import exp, tanh
from statistics import pstdev
from typing import Any

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
from app.services.watchlist_coverage import is_current_watchlist_market
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


@dataclass(slots=True)
class WatchlistGenerationSummary:
    recommendation_count: int = 0
    prediction_count: int = 0
    parlay_recommendation_count: int = 0
    parlay_prediction_count: int = 0
    heuristic_longshots_suppressed: int = 0
    inverse_winner_duplicates_collapsed: int = 0
    combo_prop_candidates_emitted: int = 0
    combo_prop_candidates_suppressed: int = 0
    critical_context_suppressed: int = 0
    quality_tier_counts: dict[str, int] = field(default_factory=dict)


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

    def _load_player_search(self, sport_key: str, query: str) -> tuple[dict[str, Any], str]:
        normalized_query = self._normalize_query(query)
        now = self._now()
        row = self.db.scalar(
            select(EspnPlayerSearchCache).where(
                EspnPlayerSearchCache.sport_key == sport_key,
                EspnPlayerSearchCache.query_normalized == normalized_query,
            )
        )
        expires_at = self._coerce_utc(row.expires_at) if row else None
        if row and expires_at and expires_at > now:
            self.stats.player_search_cache_hits += 1
            return dict(row.payload or {}), "hit"
        if row and not self.allow_network:
            self.stats.player_search_cache_hits += 1
            return dict(row.payload or {}), "hit"
        if not self.allow_network:
            self.stats.player_search_cache_misses += 1
            raise LookupError(f"No cached ESPN player search found for {sport_key}:{query}")

        self.stats.player_search_cache_misses += 1
        try:
            payload = self.espn_client.search_player(query, sport_key=sport_key)
        except Exception:
            if row:
                self.stats.player_search_cache_hits += 1
                return dict(row.payload or {}), "hit"
            raise

        if row is None:
            row = EspnPlayerSearchCache(
                sport_key=sport_key,
                query_normalized=normalized_query,
            )
            self.db.add(row)
        row.payload = dict(payload)
        row.cached_at = now
        row.expires_at = now + self._search_ttl()
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

        player, player_cache_status = self._load_player_search(sport_key, subject_name)
        season = default_season_for_sport(sport_key)
        gamelog_payload, gamelog_cache_status = self._load_player_gamelog(sport_key, player["athlete_id"], season)
        game_logs = _build_game_logs(sport_key, gamelog_payload)
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
        )
        self._cache[key] = resolved
        self.stats.prop_subjects_warmed += 1
        return resolved


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


def _days_since_participant_game(db: Session, participant_id: int, before: datetime) -> float | None:
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


def _days_since_latest_log(game_logs: list[dict[str, Any]], before: datetime) -> float | None:
    if not game_logs:
        return None
    game_date = game_logs[0].get("game_date")
    if not isinstance(game_date, datetime):
        return None
    if before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)
    if game_date.tzinfo is None:
        game_date = game_date.replace(tzinfo=timezone.utc)
    return max((before - game_date).total_seconds() / 86400.0, 0.0)


def _recent_participant_results(db: Session, participant_id: int, before: datetime, limit: int = 10) -> list[tuple[float, str | None]]:
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


def _recent_first_five_results(db: Session, participant_id: int, before: datetime, limit: int = 10) -> list[tuple[float, float, str]]:
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


def _games_in_recent_window(db: Session, participant_id: int, before: datetime, *, days: int) -> int:
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


def _latest_home_state(db: Session, participant_id: int, before: datetime) -> bool | None:
    return db.scalar(
        select(EventParticipant.is_home)
        .join(Event, Event.id == EventParticipant.event_id)
        .where(EventParticipant.participant_id == participant_id, Event.starts_at < before)
        .order_by(desc(Event.starts_at))
        .limit(1)
    )


def _schedule_context(db: Session, participant_id: int, before: datetime) -> dict[str, Any]:
    days_rest = _days_since_participant_game(db, participant_id, before)
    games_last_4 = _games_in_recent_window(db, participant_id, before, days=4)
    games_last_7 = _games_in_recent_window(db, participant_id, before, days=7)
    last_home_state = _latest_home_state(db, participant_id, before)
    return {
        "days_rest": round(days_rest, 3) if days_rest is not None else None,
        "games_last_4": games_last_4,
        "games_last_7": games_last_7,
        "back_to_back": bool(days_rest is not None and days_rest < 1.5),
        "last_home_state": last_home_state,
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
    features = {
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
    return left_win_probability, confidence, reasons, features


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
    is_eligible, gate_reason = _player_prop_participation_gate(sport_key, recent_logs)
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
        features["team_games_last_4"] = team_schedule.get("games_last_4")
        features["team_back_to_back"] = team_schedule.get("back_to_back")
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

    if sport_key == "NBA":
        recent_minutes = _log_average(short_term_logs, sport_key, "minutes")
        season_minutes = _log_average(season_logs, sport_key, "minutes")
        minute_factor = 1.0
        if season_minutes > 0:
            minute_factor = clamp(1 + ((recent_minutes - season_minutes) / season_minutes) * 0.25, 0.88, 1.12)
            expected *= minute_factor
        recent_usage = sum(_usage_proxy(item["raw_metrics"]) for item in short_term_logs) / max(len(short_term_logs), 1)
        season_usage = sum(_usage_proxy(item["raw_metrics"]) for item in season_logs) / max(len(season_logs), 1)
        usage_factor = 1.0
        if season_usage > 0:
            usage_factor = clamp(1 + ((recent_usage - season_usage) / season_usage) * 0.15, 0.90, 1.10)
            expected *= usage_factor
        features["recent_minutes"] = round(recent_minutes, 2)
        features["season_minutes"] = round(season_minutes, 2)
        features["minute_factor"] = round(minute_factor, 3)
        features["usage_factor"] = round(usage_factor, 3)
        reasons.append(f"Recent minutes trend factor: {minute_factor:.2f}x")
        if opponent_entry:
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
        recent_pa = sum(_plate_appearances(item["raw_metrics"]) for item in short_term_logs) / max(len(short_term_logs), 1)
        season_pa = sum(_plate_appearances(item["raw_metrics"]) for item in season_logs) / max(len(season_logs), 1)
        pa_factor = 1.0
        if season_pa > 0:
            pa_factor = clamp(1 + ((recent_pa - season_pa) / season_pa) * 0.18, 0.88, 1.12)
            expected *= pa_factor
        starter_era = _probable_pitcher_era(event, opponent_entry.role) if opponent_entry else None
        era_factor = 1.0
        if starter_era is not None:
            era_factor = clamp(1 + ((starter_era - 4.00) * 0.03), 0.90, 1.10)
            expected *= era_factor
        features["recent_plate_appearances"] = round(recent_pa, 2)
        features["season_plate_appearances"] = round(season_pa, 2)
        features["plate_appearance_factor"] = round(pa_factor, 3)
        features["opposing_probable_era"] = starter_era
        features["starter_era_factor"] = round(era_factor, 3)
        reasons.append(f"Recent plate appearance factor: {pa_factor:.2f}x")
        if starter_era is not None:
            reasons.append(f"Opposing probable starter ERA context: {starter_era:.2f}")
        venue_context = _event_venue_context(event)
        features["venue_indoor"] = venue_context.get("venue_indoor")
        features["venue_city"] = venue_context.get("venue_city")
        features["venue_state"] = venue_context.get("venue_state")

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
    reasons.append(f"Model probability of clearing {threshold:.1f}: {probability_yes:.0%}")
    if resolved.context_stale:
        reasons.append("Using stale cached prop context while live ESPN refresh catches up.")
    if metadata.get("copilot_requires_lineup"):
        reasons.append("Recommendation is only valid if the player is confirmed active / in the starting lineup.")

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
            feature_flags["lineup_confirmation"] = False
            missing_context.append("lineup_confirmation")

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
    diagnostics = {
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
        ml_result, runtime_decision = run_serving_inference(db, family_key=family_key, scope="single")
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

    probability_label = "Model win probability" if market_family != "player_prop" else "Model YES probability"
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
            scoring_diagnostics["suppression_reasons"] = [
                *list(scoring_diagnostics.get("suppression_reasons") or []),
                "yes_side_negative_edge",
            ]
            selection_score, signal_diagnostics = _finalize_single_scoring_diagnostics(
                diagnostics=scoring_diagnostics,
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

    if market_family == "player_prop" and metadata.get("copilot_requires_lineup"):
        invalidation = f"{invalidation}. Cancel if the player is not confirmed active / in the starting lineup."

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
    signal_diagnostics = {
        **signal_diagnostics,
        "suppression_reasons": suppression_reasons,
    }
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
            captured_at=datetime.now(timezone.utc),
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
) -> None:
    """Enforce that probability_yes decreases as threshold increases for same player/stat.

    For the same (subject_name, stat_key) group of player props, it's mathematically
    impossible for a higher threshold to have higher probability than a lower threshold.
    E.g., P(25+ points) cannot exceed P(20+ points).

    Uses isotonic regression: walk thresholds ascending, enforce monotonic decreasing.
    Mutates the ScoredRecommendation objects in place.
    """
    from collections import defaultdict

    # Group by (subject_name, stat_key) for player props only
    groups: dict[tuple[str, str], list[tuple[Market, ScoredRecommendation]]] = defaultdict(list)
    for market, sr in scored_recommendations:
        md = sr.metadata or {}
        family = md.get("copilot_market_family") or ""
        subject = md.get("copilot_subject_name") or ""
        stat = md.get("copilot_stat_key") or ""
        threshold = md.get("copilot_threshold")

        if family != "player_prop" or not subject or not stat or threshold is None:
            continue
        groups[(subject, stat)].append((market, sr))

    for key, group in groups.items():
        if len(group) < 2:
            continue

        # Sort by threshold ascending
        group.sort(key=lambda pair: float(pair[1].metadata.get("copilot_threshold", 0) if pair[1].metadata else 0))

        # Enforce monotonic decreasing: walk forward from lowest to highest threshold.
        # If fair_yes_price[i] (higher threshold) > fair_yes_price[i-1] (lower threshold),
        # clamp [i] DOWN to [i-1] because P(higher) must be <= P(lower).
        # This preserves the lower threshold estimate (usually more accurate) and
        # prevents inflated probabilities on harder thresholds.
        for i in range(1, len(group)):
            prev_prob = group[i - 1][1].signal.fair_yes_price
            curr_prob = group[i][1].signal.fair_yes_price
            if curr_prob > prev_prob:
                # Current (higher threshold) must have <= probability than previous (lower threshold)
                clamped = round(prev_prob, 4)
                group[i][1].signal.fair_yes_price = clamped
                group[i][1].signal.fair_no_price = round(1 - prev_prob, 4)
                # Recompute edge on recommendation if present
                rec = group[i][1].recommendation
                if rec is not None:
                    if rec.side == "yes":
                        rec.edge = round(clamped - rec.suggested_price, 4)
                    else:
                        rec.edge = round((1 - clamped) - rec.suggested_price, 4)
                    # Update selected_side_probability in scoring_diagnostics
                    diag = dict(rec.scoring_diagnostics or {})
                    diag["selected_side_probability"] = round(
                        clamped if rec.side == "yes" else 1 - clamped, 4
                    )
                    diag["monotonicity_adjusted"] = True
                    rec.scoring_diagnostics = diag
                # Also update signal scoring_diagnostics
                sig_diag = dict(group[i][1].signal.scoring_diagnostics or {})
                sig_diag["monotonicity_adjusted"] = True
                group[i][1].signal.scoring_diagnostics = sig_diag


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

        existing_market, existing_scored = current
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


def regenerate_watchlist(
    db: Session,
    *,
    run_id: int | None = None,
    resolver: PropStatsResolver | None = None,
    allowed_market_ids: set[int] | None = None,
    replace_all: bool = True,
    capture_parlays: bool = True,
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
    for market in markets:
        if not market.event:
            continue
        latest_snapshot = db.scalars(
            select(MarketSnapshot).where(MarketSnapshot.market_id == market.id).order_by(MarketSnapshot.captured_at.desc()).limit(1)
        ).first()
        scored = _build_scored_recommendation(db, market.event, market, latest_snapshot, resolver=active_resolver)
        if scored:
            db.add(scored.signal)
            if is_current_watchlist_market(market):
                current_coverage_candidates.append((market, scored))
            if scored.recommendation:
                pending_recommendations.append((market, scored))
            else:
                diagnostics = dict(scored.signal.scoring_diagnostics or {})
                suppression_reasons = {str(value) for value in list(diagnostics.get("suppression_reasons") or [])}
                if "winner_selected_probability_floor" in suppression_reasons:
                    summary.heuristic_longshots_suppressed += 1
                if "critical_market_snapshot_missing" in suppression_reasons:
                    summary.critical_context_suppressed += 1

    _enforce_prop_monotonicity(pending_recommendations)
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
