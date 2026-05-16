"""Per-family heuristic penalties + market/event context helpers.

Extracted from ``scoring/__init__.py`` as part of R1 phase 3. These
are the leaf-level "compute a small number from one input" helpers
the kernel (``_score_*`` functions) and orchestration both consume:

- Penalties: ``_staleness_penalty``, ``_sample_penalty``,
  ``_market_disagreement_penalty``, ``_prop_volatility_penalty``,
  ``_mean_abs_deviation``.
- Market context: ``_market_implied_yes_price``, ``_market_payload``,
  ``_market_metadata``, ``_market_yes_entry``, ``_token_score``.
- Event context: ``_competition_from_event``, ``_event_venue_context``,
  ``_competitor_for_role``, ``_parse_first_five_runs``.
- Recent-history DB queries: ``_days_since_participant_game``,
  ``_days_since_latest_log``, ``_recent_participant_results``,
  ``_recent_first_five_results``, ``_games_in_recent_window``,
  ``_latest_home_state``, ``_schedule_context``.
- Win-rate / averaging primitives: ``_win_rate``, ``_avg_score``,
  ``_fractional_win_rate``, ``_avg_first_five_runs``,
  ``_avg_first_five_diff``.
- Side helpers: ``_selected_side_probability``, ``clamp``.

All pure (no kernel deps), so this is the lowest-risk extraction
that meaningfully shrinks the kernel. Re-exported from
``scoring/__init__.py`` so existing callers stay unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import (
    Event,
    EventParticipant,
    Market,
    MarketSnapshot,
)
from app.services.market_support import infer_yes_label, market_metadata
from app.services.scoring.resolver import HeuristicProfile
from app.sports.base import alias_tokens

__all__ = [
    "clamp",
    "_market_implied_yes_price",
    "_staleness_penalty",
    "_sample_penalty",
    "_market_disagreement_penalty",
    "_mean_abs_deviation",
    "_prop_volatility_penalty",
    "_days_since_participant_game",
    "_days_since_latest_log",
    "_recent_participant_results",
    "_win_rate",
    "_avg_score",
    "_market_payload",
    "_market_metadata",
    "_token_score",
    "_market_yes_entry",
    "_competition_from_event",
    "_event_venue_context",
    "_competitor_for_role",
    "_parse_first_five_runs",
    "_recent_first_five_results",
    "_fractional_win_rate",
    "_avg_first_five_runs",
    "_avg_first_five_diff",
    "_games_in_recent_window",
    "_latest_home_state",
    "_schedule_context",
    "_selected_side_probability",
]


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


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
    venue_id = venue.get("id")
    return {
        # Bug #4 fix: surface ESPN venue.id alongside name/city/state/
        # indoor. The park-factor lookup currently uses venue NAME for
        # disambiguation (Tropicana vs. Steinbrenner), but having the ID
        # available means non-name-keyed callers can fall back to it.
        "venue_id": str(venue_id) if venue_id is not None else None,
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
