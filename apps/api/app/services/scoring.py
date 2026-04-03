from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp, tanh
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, joinedload

from app.clients.espn import EspnPublicClient
from app.config import get_settings
from app.models import Event, EventParticipant, Market, MarketSnapshot, Recommendation, SignalSnapshot
from app.services.market_support import infer_yes_label, market_metadata
from app.services.parlays import ParlayCandidateInput, capture_parlay_artifacts, clear_active_parlay_watchlist
from app.services.predictions import MODEL_NAME, OPEN_MARKET_STATUSES, capture_prediction
from app.services.stats_query import _build_game_logs, default_season_for_sport
from app.sports.base import alias_tokens


@dataclass(slots=True)
class ResolvedPropSubject:
    sport_key: str
    athlete_id: str
    display_name: str
    team_name: str | None
    season: int
    game_logs: list[dict[str, Any]]


@dataclass(slots=True)
class ScoredRecommendation:
    recommendation: Recommendation | None
    signal: SignalSnapshot
    metadata: dict[str, Any]


class PropStatsResolver:
    def __init__(self, espn_client: EspnPublicClient | None = None) -> None:
        self.espn_client = espn_client or EspnPublicClient()
        self._cache: dict[tuple[str, str, str], ResolvedPropSubject] = {}

    def resolve(self, sport_key: str, subject_name: str, team_hint: str | None = None) -> ResolvedPropSubject:
        key = (sport_key, subject_name.lower(), (team_hint or "").upper())
        cached = self._cache.get(key)
        if cached:
            return cached

        player = self.espn_client.search_player(subject_name, sport_key=sport_key)
        season = default_season_for_sport(sport_key)
        gamelog_payload = self.espn_client.fetch_player_gamelog(sport_key, player["athlete_id"], season)
        game_logs = _build_game_logs(sport_key, gamelog_payload)
        resolved = ResolvedPropSubject(
            sport_key=sport_key,
            athlete_id=player["athlete_id"],
            display_name=player["display_name"],
            team_name=player.get("team_name"),
            season=season,
            game_logs=game_logs,
        )
        self._cache[key] = resolved
        return resolved


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


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

    left_win_rate = _win_rate(left_results)
    right_win_rate = _win_rate(right_results)
    left_avg_score = _avg_score(left_results)
    right_avg_score = _avg_score(right_results)
    score_gap = left_avg_score - right_avg_score
    home_advantage = 0.03 if event.sport_key in {"NBA", "NFL", "MLB", "SOCCER"} and left.is_home else 0.0

    raw_probability = 0.5 + ((left_win_rate - right_win_rate) * 0.25) + tanh(score_gap / 10.0) * 0.08 + home_advantage
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
    features = {
        "left_win_rate": left_win_rate,
        "right_win_rate": right_win_rate,
        "left_avg_score": left_avg_score,
        "right_avg_score": right_avg_score,
        "home_advantage": home_advantage,
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

    resolved = resolver.resolve(sport_key, subject_name, team_hint=team_hint if isinstance(team_hint, str) else None)
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

    probability_yes = _poisson_yes_probability(expected, threshold)
    sample_size = min(len(recent_logs), 10)
    confidence = clamp(0.32 + (sample_size / 18.0) + abs(probability_yes - 0.5) * 0.45, 0.25, 0.93)
    if len(short_term_logs) < 3:
        confidence = clamp(confidence - 0.08, 0.25, 0.93)

    features["expected_stat_output"] = round(expected, 3)
    features["yes_probability"] = round(probability_yes, 4)
    reasons.append(f"Model probability of clearing {threshold:.1f}: {probability_yes:.0%}")
    if metadata.get("copilot_requires_lineup"):
        reasons.append("Recommendation is only valid if the player is confirmed active / in the starting lineup.")

    return probability_yes, confidence, reasons, features


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
        prop_score = _score_player_prop(event, market, snapshot, resolver or PropStatsResolver())
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

    fair_yes_price = round(probability_yes, 4)
    fair_no_price = round(1 - probability_yes, 4)

    yes_entry = snapshot.yes_ask if snapshot and snapshot.yes_ask is not None else snapshot.last_price if snapshot else None
    no_entry = snapshot.no_ask if snapshot and snapshot.no_ask is not None else (1 - snapshot.last_price) if snapshot and snapshot.last_price is not None else None
    yes_edge = fair_yes_price - yes_entry if yes_entry is not None else 0.0
    no_edge = fair_no_price - no_entry if no_entry is not None else 0.0

    probability_label = "Model win probability" if market_family != "player_prop" else "Model YES probability"
    reasons = [*reasons, f"{probability_label} for {probability_subject}: {probability_yes:.0%}"]

    signal = SignalSnapshot(
        event_id=event.id,
        market_id=market.id if market else None,
        model_name=MODEL_NAME,
        confidence=confidence,
        fair_yes_price=fair_yes_price,
        fair_no_price=fair_no_price,
        edge=max(yes_edge, no_edge),
        reasons=reasons,
        features=features,
    )

    if not market:
        return ScoredRecommendation(
            recommendation=None,
            signal=signal,
            metadata=metadata,
        )

    if yes_edge >= no_edge:
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

    if edge < settings.watchlist_min_edge or confidence < settings.watchlist_min_confidence:
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
            invalidation=invalidation,
            rationale="; ".join(reasons),
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



def regenerate_watchlist(db: Session, *, run_id: int | None = None) -> tuple[int, int, int, int]:
    db.query(Recommendation).delete()
    clear_active_parlay_watchlist(db)
    recommendation_count = 0
    prediction_count = 0
    parlay_recommendation_count = 0
    parlay_prediction_count = 0
    resolver = PropStatsResolver()
    parlay_candidates: list[ParlayCandidateInput] = []
    markets = db.scalars(
        select(Market)
        .options(joinedload(Market.event).selectinload(Event.participants).joinedload(EventParticipant.participant))
        .where(Market.event_id.is_not(None), Market.status.in_(tuple(OPEN_MARKET_STATUSES)))
    ).all()
    for market in markets:
        if not market.event:
            continue
        latest_snapshot = db.scalars(
            select(MarketSnapshot).where(MarketSnapshot.market_id == market.id).order_by(MarketSnapshot.captured_at.desc()).limit(1)
        ).first()
        scored = _build_scored_recommendation(db, market.event, market, latest_snapshot, resolver=resolver)
        if scored:
            db.add(scored.signal)
            if scored.recommendation:
                db.add(scored.recommendation)
                prediction = capture_prediction(
                    db,
                    run_id=run_id,
                    event=market.event,
                    market=market,
                    recommendation=scored.recommendation,
                    signal=scored.signal,
                    metadata=scored.metadata,
                )
                recommendation_count += 1
                prediction_count += 1
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
    db.flush()
    parlay_recommendation_count, parlay_prediction_count = capture_parlay_artifacts(
        db,
        run_id=run_id,
        candidates=parlay_candidates,
    )
    db.flush()
    return recommendation_count, prediction_count, parlay_recommendation_count, parlay_prediction_count
