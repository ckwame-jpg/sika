from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import exp, tanh
from statistics import NormalDist, pstdev
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
    Prediction,
    Recommendation,
    SignalSnapshot,
)
from app.services.ml.lineage import HEURISTIC_SINGLE_MODEL
from app.services.ml.runtime import run_serving_inference
from app.services.market_support import infer_yes_label, market_metadata
from app.services.model_families import (
    quality_tier_thresholds_for,
    single_family_key,
    watchlist_min_edge_for,
)
from app.services.scoring.feature_groups import (
    FeatureGroupSeverity,
    FeatureGroupSnapshot,
    SuppressionContext,
    check_freshness,
    check_suppressions,
    emit_to_group,
    serialize_feature_groups,
)
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


logger = logging.getLogger(__name__)


def _events_ingestion_fresh_at(db: Session) -> datetime | None:
    """Return ``last_success_at`` for the ``espn_scoreboard`` upstream
    source — the freshness signal feeding the ``mlb_bullpen`` feature
    group's PENALIZE policy.

    The bullpen-rest helper queries Event + EventParticipant directly
    (no per-call cache), so the staleness of the bullpen rest index
    tracks the staleness of the events ingestion job. ``None`` means
    we've never recorded a successful run; the freshness layer treats
    that as opt-out (no penalty) rather than treating "no signal" as
    "infinitely stale" — matches the conservative default behavior.

    Computed once per batch at the top-level entrypoint
    (``score_event``, ``_score_watchlist_markets_batch``,
    ``regenerate_watchlist``) and threaded through
    ``_build_scored_recommendation`` → ``_score_player_prop`` via the
    ``events_fresh_at`` kwarg. The per-market read that this previously
    incurred is retired.
    """
    # Lazy import: upstream_health uses OperatorSetting which pulls
    # in the wider ORM surface. Local import keeps the scoring module
    # import-time small.
    from app.services.upstream_health import get_upstream_health  # noqa: PLC0415

    # ``get_upstream_health`` is documented to always return one row
    # per source (None-filled when never recorded). With a single
    # ``espn_scoreboard`` source the list is always length 1; access
    # ``[0]`` directly. ``last_success_at`` IS None when the row has
    # never been written, which is the conservative "no signal →
    # opt-out" path documented above.
    snapshots = get_upstream_health(db, sources=["espn_scoreboard"])
    return snapshots[0].last_success_at


# Dataclasses live in ``scoring.types`` so they can be imported
# without pulling in the rest of the scoring kernel (which drags
# the full DB / ML graph through the import). Re-exported below for
# backward compatibility with consumers that ``from app.services.scoring
# import ResolvedPropSubject``.
from app.services.scoring.types import (  # noqa: E402
    PropResolverStats,
    ResolvedPropSubject,
    ScoredRecommendation,
    ScoredWatchlistCapture,
    WatchlistGenerationSummary,
)
from app.services.scoring.interval_consumer import consume_prediction_interval  # noqa: E402


def _mlb_bullpen_total_factor(
    db: Session, *, event: Event, left: EventParticipant, right: EventParticipant,
) -> tuple[float, dict[str, float]]:
    """Smarter #6 phase 2 — combined bullpen rest factor for MLB
    game-line totals.

    Phase 1 (PR #78) shipped the bullpen rest infrastructure
    (``count_team_games_in_window`` + ``bullpen_rest_index_from_games``)
    and wired the per-team factor into batter offense props (runs,
    RBIs). This helper extends the same signal to game-line totals
    by averaging the two teams' opposing-bullpen factors.

    Each team's bullpen rest index → factor:
      - rest=1.0 (fully rested) → 0.95 (suppress opposing offense)
      - rest=0.0 (saturated)    → 1.05 (amplify opposing offense)
      - rest=0.5                → 1.0 (no-op)

    Combined factor for the total = mean of the two team factors.
    Both rested → ~0.95 (mild suppression). Both tired → ~1.05
    (mild amplification). Balanced → ~1.0 (no shift).

    Returns ``(combined_factor, features_dict)``. The features dict
    carries the per-side rest indices + the combined factor so
    operators can audit the multiplier in scoring diagnostics.
    """
    from app.services.mlb_advanced import (  # noqa: PLC0415 — local import
        bullpen_rest_index_from_games,
        count_team_games_in_window,
    )

    if event.starts_at is None:
        return 1.0, {}
    end_at = event.starts_at
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    home_ep = left if left.is_home else right
    away_ep = right if left.is_home else left
    home_games = count_team_games_in_window(
        db, participant_id=home_ep.participant_id, end_at=end_at,
    )
    away_games = count_team_games_in_window(
        db, participant_id=away_ep.participant_id, end_at=end_at,
    )
    home_rest_index = bullpen_rest_index_from_games(home_games)
    away_rest_index = bullpen_rest_index_from_games(away_games)
    # Per-team factor: linear from rest=1.0 → 0.95 to rest=0.0 → 1.05
    # (matches the batter-prop factor in heuristic_factors.py).
    home_factor = clamp(1.0 + (0.5 - home_rest_index) * 0.10, 0.95, 1.05)
    away_factor = clamp(1.0 + (0.5 - away_rest_index) * 0.10, 0.95, 1.05)
    combined = round((home_factor + away_factor) / 2, 4)
    return combined, {
        "home_bullpen_rest_index_3d": round(home_rest_index, 4),
        "away_bullpen_rest_index_3d": round(away_rest_index, 4),
        "bullpen_combined_factor": combined,
    }


def _unavailable_referee_fetcher(season: int) -> list[dict[str, Any]]:
    """Sentinel fetcher for ``load_nba_referee_tendencies`` from the
    scoring path. Smarter #13 phase 2b shipped the loader with a
    required ``fetcher`` callable; phase 2b-2 (deferred) wires the
    real basketball-reference scraper. Until then, scoring calls
    the loader with ``allow_network=False`` so the fetcher is never
    invoked — but the kwarg is required, so this raises if a code
    path ever drops the network gate. Fail loud rather than
    accidentally hammer BR from the scoring kernel."""
    raise RuntimeError(
        "Scoring path must not invoke the referee tendency fetcher. "
        "If allow_network=True is needed here, route through the "
        "refresh job instead (Smarter #13 phase 2b-2)."
    )


# Watchlist orchestration (counter helpers, classifiers, batch
# loaders, stage / finalize / regenerate entry points) moved to
# ``scoring.orchestration`` (R1 phase 4). Re-exported here for
# backward compat.
from app.services.scoring.orchestration import (  # noqa: E402
    _annotate_current_watchlist_flag,
    _explicit_watchlist_market_batch,
    _maintenance_watchlist_market_batch,
    _merge_count_maps,
    _record_candidate_filter,
    _record_scorer_outcome,
    _score_watchlist_markets_batch,
    _scoring_none_reason,
    _suppression_outcome_reason,
    finalize_current_slate_watchlist,
    finalize_staged_watchlist,
    regenerate_watchlist,
    stage_current_slate_watchlist_batch,
    stage_maintenance_watchlist_batch,
)


# Prop-subject resolution + per-family heuristic profiles live in
# ``scoring.resolver`` (R1 phase 2). Re-exported here so existing
# consumers (tests, the kernel below, ingestion) keep working
# unchanged.
from app.services.scoring.resolver import (  # noqa: E402
    HeuristicProfile,
    PropStatsResolver,
    SINGLE_HEURISTIC_PROFILES,
    _merge_cache_status,
    _profile_for_single_family,
    _team_abbreviation_from_player,
    warm_prop_context_cache,
)


# Per-family heuristic penalties + market/event context helpers live
# in ``scoring.heuristics`` (R1 phase 3). Re-exported here so existing
# consumers (the kernel functions below, ingestion, tests) keep
# working unchanged.
from app.services.scoring.heuristics import (  # noqa: E402
    _avg_first_five_diff,
    _avg_first_five_runs,
    _avg_score,
    _competition_from_event,
    _competitor_for_role,
    _days_since_latest_log,
    _days_since_participant_game,
    _event_venue_context,
    _fractional_win_rate,
    _games_in_recent_window,
    _latest_home_state,
    _market_disagreement_penalty,
    _market_implied_yes_price,
    _market_metadata,
    _market_payload,
    _market_yes_entry,
    _mean_abs_deviation,
    _parse_first_five_runs,
    _prop_volatility_penalty,
    _recent_first_five_results,
    _recent_participant_results,
    _sample_penalty,
    _schedule_context,
    _selected_side_probability,
    _spread_subject_entry,
    _staleness_penalty,
    _token_score,
    _win_rate,
    clamp,
)


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
    family_key: str,
    selected_side_probability: float,
    adjusted_confidence: float,
    context_coverage_score: float,
    total_penalty: float,
    served_mode: str,
) -> str:
    """Smarter #28 — per-family quality tier classification.

    Thresholds come from ``quality_tier_thresholds_for(family_key)``
    which falls back to shared defaults for any family without an
    explicit override in ``QUALITY_TIER_THRESHOLDS_BY_FAMILY``. The
    default values match the constants the kernel hardcoded before
    Smarter #28, so the registry-empty baseline preserves today's
    behavior exactly.
    """
    thresholds = quality_tier_thresholds_for(family_key)

    if served_mode == "ml":
        if (
            context_coverage_score >= thresholds.ml_high_context_coverage
            and adjusted_confidence >= thresholds.ml_high_adjusted_confidence
        ):
            return "high"
        if context_coverage_score >= thresholds.ml_medium_context_coverage:
            return "medium"
        return "low"

    if (
        selected_side_probability < thresholds.low_selected_side_probability
        or context_coverage_score < thresholds.low_context_coverage
        or adjusted_confidence < thresholds.low_adjusted_confidence
        or total_penalty >= thresholds.low_total_penalty
    ):
        return "low"
    if (
        selected_side_probability >= thresholds.high_selected_side_probability
        and context_coverage_score >= thresholds.high_context_coverage
        and adjusted_confidence >= thresholds.high_adjusted_confidence
        and total_penalty <= thresholds.high_total_penalty
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
    home_advantage = 0.03 if event.sport_key in {"NBA", "NFL", "MLB", "WNBA"} and left.is_home else 0.0
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
    if event.sport_key == "TENNIS":
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
        yes_entry_target = _spread_subject_entry(event, market)
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
        baseline_expected_total = (
            _avg_total_from_pairs(left_pairs) + _avg_total_from_pairs(right_pairs)
        ) / 2
        # Smarter #6 phase 2: combined bullpen rest factor on MLB
        # totals. Phase 1 wired the same signal into batter-offense
        # props (runs / RBIs); this extends it to game-line totals
        # by averaging both teams' opposing-bullpen factors. ±5%
        # envelope; non-MLB events skip the helper entirely.
        bullpen_features: dict[str, float] = {}
        bullpen_factor: float = 1.0
        if (event.sport_key or "").upper() == "MLB":
            bullpen_factor, bullpen_features = _mlb_bullpen_total_factor(
                db, event=event, left=left, right=right,
            )
        expected_total = baseline_expected_total * bullpen_factor
        sigma = max(7.5, 15.0 - min(sample_size, 10) * 0.35)
        over_probability = clamp(1 - NormalDist(mu=expected_total, sigma=sigma).cdf(threshold), 0.05, 0.95)
        probability_yes = over_probability if direction == "over" else round(1 - over_probability, 4)
        confidence = clamp(0.26 + (sample_size / 20.0) + abs(probability_yes - 0.5) * 0.35, 0.24, 0.88)
        reasons = [
            f"Projected combined total: {expected_total:.1f}",
            f"Market line: {direction.title()} {threshold:.1f}",
        ]
        if bullpen_features and bullpen_factor != 1.0:
            reasons.append(
                "Bullpen-rest factor: "
                f"{bullpen_factor:.3f} (baseline {baseline_expected_total:.1f} → "
                f"adjusted {expected_total:.1f})"
            )
        features = {
            "expected_total": round(expected_total, 4),
            "baseline_expected_total": round(baseline_expected_total, 4),
            "line_threshold": threshold,
            "distribution_sigma": round(sigma, 4),
            "left_average_total": round(_avg_total_from_pairs(left_pairs), 4),
            "right_average_total": round(_avg_total_from_pairs(right_pairs), 4),
            "sample_size": sample_size,
            "left_sample_size": len(left_pairs),
            "right_sample_size": len(right_pairs),
            **bullpen_features,
        }
        return probability_yes, confidence, reasons, features

    return None


def _prop_value_from_raw(sport_key: str, stat_key: str, raw: dict[str, float]) -> float:
    # WNBA shares NBA's basketball stat surface 1:1 (PR 3 wired
    # _build_game_logs to dispatch WNBA → _nba_raw_metrics_from_stat_map).
    # Without WNBA in this branch, codex caught that WNBA props like
    # made_threes (which alias to three_points_made) and combo props
    # like points_rebounds_assists would silently score expected = 0
    # — a HIGH-severity correctness bug for any WNBA 3PM / PRA market.
    if sport_key in {"NBA", "WNBA"}:
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
    if sport_key == "NFL":
        # Smarter NFL PR 7 — NFL component keys CONTAIN underscores
        # (passing_yards), so MLB's split("_") recombination above
        # would shred them into nonsense ("passing" + "yards"). Combos
        # are enumerated explicitly; simple keys match the gamelog
        # raw_metrics names 1:1 (Smarter NFL PR 1 parser).
        if stat_key == "rushing_yards_receiving_yards":
            return raw.get("rushing_yards", 0.0) + raw.get("receiving_yards", 0.0)
        if stat_key == "passing_yards_rushing_yards":
            return raw.get("passing_yards", 0.0) + raw.get("rushing_yards", 0.0)
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


# Smarter NFL PR 7 — distribution split. Poisson is fine for COUNT
# stats (TDs, receptions, completions: small integers, variance ≈
# mean) but mathematically wrong for YARDAGE: Poisson at λ=250 forces
# sd≈16 where real per-game passing-yard sd is ~65-75 — prices would
# be wildly overconfident. Yardage stats price with a Normal tail
# whose sd is the player's sample sd shrunk toward a league prior.
_NFL_NORMAL_PROP_STATS = frozenset({
    "passing_yards", "rushing_yards", "receiving_yards",
    "rushing_yards_receiving_yards", "passing_yards_rushing_yards",
})
# Sd priors + shrinkage live in ``ml_features.nfl_pricing`` (tuned by
# the Smarter NFL PR 9 replay; shared so serving and the backtest can
# never drift). Thin re-export keeps the kernel's call sites local.
def _nfl_prop_sd(stat_key: str, recent_values: list[float]) -> float:
    from ml_features.nfl_pricing import nfl_prop_sd  # noqa: PLC0415

    return nfl_prop_sd(stat_key, recent_values)


def _prop_yes_probability(
    sport_key: str,
    stat_key: str,
    expected_value: float,
    threshold: float,
    features: dict[str, Any],
) -> float:
    """Route a prop to its distribution model, annotating ``features``
    with the choice so operators / training rows can audit it."""
    if sport_key == "NFL" and stat_key in _NFL_NORMAL_PROP_STATS:
        from ml_features.nfl_pricing import normal_tail_yes_probability  # noqa: PLC0415

        sd = _nfl_prop_sd(stat_key, features.get("recent_values") or [])
        features["distribution_model"] = "normal"
        features["distribution_sd"] = round(sd, 3)
        return clamp(normal_tail_yes_probability(expected_value, sd, threshold), 0.01, 0.99)
    features["distribution_model"] = "poisson"
    return _poisson_yes_probability(expected_value, threshold)


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


def _player_prop_participation_gate(
    sport_key: str,
    recent_logs: list[dict[str, Any]],
    *,
    snap_shares: list[float] | None = None,
) -> tuple[bool, str | None]:
    # Smarter NFL PR 7 — 17-game seasons: a 5-log floor would keep every
    # player gated until week 6, so NFL uses 4. The role signal is snap
    # share (offense_pct from nflverse) instead of minutes/PAs: require
    # ≥40% in 3 of the last 4 games when snap data is available; weeks
    # 1-2 (no snap rows yet) fall through on the log floor alone.
    if sport_key == "NFL":
        if len(recent_logs) < 4:
            return False, "Not enough recent appearances to trust the player-prop sample."
        if snap_shares:
            recent_shares = snap_shares[:4]
            active = [share for share in recent_shares if share >= 40.0]
            if len(recent_shares) >= 3 and len(active) < min(3, len(recent_shares)):
                return False, "Player role looks unstable in recent NFL snap counts."
        return True, None

    if len(recent_logs) < 5:
        return False, "Not enough recent appearances to trust the player-prop sample."

    # WNBA shares NBA's basketball box-score shape (minutes-based
    # participation signal). Same thresholds — 5 active games with ≥10
    # minutes, recent-3 average ≥ 18 minutes. The WNBA-specific
    # message is reused so operators see which sport the gate fired
    # on.
    if sport_key in {"NBA", "WNBA"}:
        active_games = [item for item in recent_logs if item["raw_metrics"].get("minutes", 0.0) >= 10]
        recent_minutes = _log_average(recent_logs[:3], sport_key, "minutes")
        if len(active_games) < 5 or recent_minutes < 18:
            return False, f"Player role looks unstable in recent {sport_key} minutes."
        return True, None

    recent_pa = [_plate_appearances(item["raw_metrics"]) for item in recent_logs[:5]]
    if len([value for value in recent_pa if value >= 2.0]) < 3:
        return False, "Batter role looks unstable because recent plate appearances are too thin."
    return True, None


def _nfl_recent_snap_shares(db: Session, event: Event, player_name: str) -> list[float]:
    """Player's offense snap % per week, latest week first, from the
    nflverse snap-count cache. Name-matched (snap rows key on PFR ids,
    not ESPN athletes). Empty when the cache is cold or the player has
    no rows — the participation gate treats that as 'no snap signal'."""
    from app.services.nfl_advanced import (  # noqa: PLC0415 — avoid circular import
        _normalize_player_name,
        load_nfl_snap_counts,
    )

    ref_date = event.starts_at.date() if event.starts_at else None
    season = default_season_for_sport("NFL", ref_date)
    snaps = load_nfl_snap_counts(db, season)
    if not snaps.complete:
        return []
    target = _normalize_player_name(player_name)
    if not target:
        return []
    shares: list[float] = []
    weeks = sorted(
        ((int(week), rows) for week, rows in (snaps.payload.get("weeks") or {}).items()),
        key=lambda pair: pair[0],
        reverse=True,
    )
    for _week, rows in weeks:
        for row in rows:
            if _normalize_player_name(str(row.get("player") or "")) == target:
                try:
                    shares.append(float(row.get("offense_pct") or 0.0))
                except (TypeError, ValueError):
                    pass
                break
    return shares


def _emit_nfl_prop_context(
    db: Session,
    event: Event,
    opponent_entry: EventParticipant | None,
    snap_shares: list[float],
    features: dict[str, Any],
    feature_groups: dict[str, FeatureGroupSnapshot],
) -> None:
    """Smarter NFL PR 7 — opponent defense EPA, weather, and the snap-
    share volume proxy for the heuristic factor pass."""
    from app.services.nfl_advanced import (  # noqa: PLC0415 — avoid circular import
        load_nfl_team_ratings,
        load_nfl_weather,
        nfl_team_abbr_for_name,
    )

    ref_date = event.starts_at.date() if event.starts_at else None
    season = default_season_for_sport("NFL", ref_date)

    opponent_values: dict[str, Any] = {"nfl_opponent_data_complete": 0.0}
    ratings = load_nfl_team_ratings(db, season)
    opponent_code = nfl_team_abbr_for_name(
        opponent_entry.participant.display_name
        if opponent_entry is not None and opponent_entry.participant
        else None
    )
    opponent_rating = (ratings.payload.get("teams") or {}).get(opponent_code or "")
    if opponent_rating:
        def_epa = opponent_rating.get("def_epa_per_play_allowed")
        if isinstance(def_epa, (int, float)):
            opponent_values["nfl_opponent_data_complete"] = 1.0
            opponent_values["nfl_opp_def_epa_per_play"] = round(float(def_epa), 5)
    emit_to_group(
        feature_groups, features, "nfl_team_ratings", opponent_values,
        fresh_at=ratings.cached_at, source="NflTeamRatingCache",
    )

    home_entry = next((entry for entry in event.participants if entry.is_home), None)
    home_code = nfl_team_abbr_for_name(
        home_entry.participant.display_name if home_entry and home_entry.participant else None
    )
    weather = load_nfl_weather(
        db,
        event_id=str(event.id),
        home_team_abbr=home_code,
        game_time_utc=event.starts_at,
        allow_network=False,
    )
    weather_values: dict[str, Any] = {
        "nfl_weather_data_complete": 1.0 if weather.complete else 0.0,
        "nfl_is_dome": 1.0 if weather.payload.get("is_dome") else 0.0,
    }
    if weather.complete and not weather.payload.get("is_dome"):
        weather_values["nfl_wind_mph"] = float(weather.payload.get("wind_speed_mph") or 0.0)
    emit_to_group(
        feature_groups, features, "nfl_weather", weather_values,
        fresh_at=weather.cached_at if weather.cache_status != "dome" else None,
        source="NflWeatherCache",
    )

    snap_values: dict[str, Any] = {
        "nfl_snap_data_complete": 1.0 if snap_shares else 0.0,
    }
    if len(snap_shares) >= 4:
        recent_avg = sum(snap_shares[:3]) / 3.0
        season_avg = sum(snap_shares) / len(snap_shares)
        if season_avg > 0:
            snap_values["nfl_snap_share_factor_raw"] = round(
                clamp(recent_avg / season_avg, 0.88, 1.12), 4
            )
        snap_values["nfl_snap_share_recent"] = round(recent_avg, 2)
    emit_to_group(
        feature_groups, features, "nfl_snap_counts", snap_values,
        fresh_at=None, source="NflSnapCountsCache",
    )


def _score_player_prop(
    db: Session,
    event: Event,
    market: Market,
    snapshot: MarketSnapshot | None,
    resolver: PropStatsResolver,
    *,
    events_fresh_at: datetime | None = None,
) -> tuple[float, float, list[str], dict[str, Any], dict[str, FeatureGroupSnapshot]] | None:
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
    nfl_snap_shares = (
        _nfl_recent_snap_shares(db, event, resolved.display_name)
        if sport_key == "NFL"
        else None
    )
    is_eligible, _ = _player_prop_participation_gate(
        sport_key, recent_logs, snap_shares=nfl_snap_shares,
    )
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
    # Architecture #5 — feature_groups is the source of truth for
    # emitter-produced features; ``features`` becomes a derived view
    # that ``emit_to_group`` keeps in sync as each group is registered.
    # Kernel-direct writes (above + venue context below) stay in
    # ``features`` only — they're operational metadata, not externally-
    # refreshed cache data.
    feature_groups: dict[str, FeatureGroupSnapshot] = {}
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
            emit_nba_injury_features,
            emit_nba_interaction_term,
            emit_nba_opponent_team_features,
            emit_nba_player_features,
            emit_nba_workload_features,
            find_nba_team_id_by_name,
            load_nba_team_gamelog,
        )
        from app.services.nba_injury_report import load_nba_injury_report
        from app.services.nba_long_tail import (
            emit_nba_clutch_features,
            emit_nba_drives_features,
            emit_nba_hustle_features,
            load_nba_clutch_player,
            load_nba_hustle_player,
            load_nba_tracking,
        )
        from app.services.nba_referee_assignments import load_nba_referee_assignments
        from app.services.nba_referee_emit import emit_nba_referee_features
        from app.services.nba_referee_tendencies import load_nba_referee_tendencies

        if resolved.advanced_payload:
            emit_to_group(
                feature_groups,
                features,
                "nba_advanced",
                emit_nba_player_features(resolved.advanced_payload),
                # Advanced-stats loader doesn't expose cached_at yet;
                # default DEFAULT_POLICY (IGNORE, 365d) means no
                # penalty fires regardless. Follow-up plumbs this.
                source="load_nba_advanced",
            )

        # Smarter #17 phase 3 — wire the late-breaking-injury features
        # into the scoring path. Phase 1 shipped the emitter + the
        # ``_single_scoring_adjustments`` suppression gate; phase 2
        # shipped the cache loader + the daily refresh-job entry that
        # populates it. This call is the consumer-side wiring that
        # actually fires the suppression on real games.
        #
        # ``allow_network=False`` keeps scoring off the network: the
        # daily refresh-job populates the cache out-of-band, and a
        # cache miss here yields an empty payload (the emitter
        # returns ``{}`` when the player has no entry, so the
        # downstream suppression gate never fires on missing data).
        #
        # Architecture #5: nba_injury group is IGNORE policy — Smarter
        # #17's bespoke gate (OUT/DOUBTFUL + fresh report) stays
        # authoritative for this group. The freshness layer here is
        # passive; consolidating the bespoke gate into the registry is
        # a follow-up.
        injury_payload = load_nba_injury_report(db, allow_network=False)
        emit_to_group(
            feature_groups,
            features,
            "nba_injury",
            emit_nba_injury_features(injury_payload, player_name=resolved.display_name),
            source="NbaInjuryReportCache",
        )

        # Smarter #11: workload features from the ESPN game log. Pure
        # function read against ``resolved.game_logs`` (already in scope
        # and sorted reverse-chrono) — no network or cache reads.
        #
        # Architecture #5: nba_workload is PENALIZE (-3% / 24h TTL).
        # ``resolved.gamelog_cached_at`` is sourced from the
        # EspnPlayerGamelogCache row that backs ``game_logs`` — when
        # that row is past TTL, the freshness layer applies the penalty.
        emit_to_group(
            feature_groups,
            features,
            "nba_workload",
            emit_nba_workload_features(resolved.game_logs),
            fresh_at=resolved.gamelog_cached_at,
            source="EspnPlayerGamelogCache",
        )

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
                    emit_to_group(
                        feature_groups,
                        features,
                        "nba_opponent_team",
                        emit_nba_opponent_team_features(opponent_team_result.payload),
                        source="NbaTeamAdvancedCache",
                    )
                    features["opponent_team_cache_status"] = opponent_team_result.cache_status

        # Smarter #12: emit the usage × pace × (1 / opponent_DRtg) interaction
        # term once both source caches have contributed. The emitter returns
        # {} when any input is missing, so this is safe to run unconditionally
        # — the model handles absent keys via median imputation.
        #
        # ``is not None`` over ``or`` because ``recent_usage_pct == 0.0`` is a
        # legitimate edge case (DNP-adjacent player) and ``or`` would silently
        # skip to the season value. Pace is on the same pattern for
        # consistency, even though ``opponent_pace == 0.0`` shouldn't occur in
        # practice.
        _recent_usage = features.get("recent_usage_pct")
        _recent_pace = features.get("opponent_pace_recent_5")
        emit_to_group(
            feature_groups,
            features,
            "nba_interaction",
            emit_nba_interaction_term(
                usage_pct=(
                    _recent_usage if _recent_usage is not None
                    else features.get("season_usage_pct")
                ),
                opponent_pace=(
                    _recent_pace if _recent_pace is not None
                    else features.get("opponent_pace_season")
                ),
                opponent_drtg=features.get("opponent_def_rating_recent_5"),
            ),
            # Derived from upstream nba_advanced + nba_opponent_team
            # values; no independent cache, so fresh_at=None opts the
            # group out of the freshness check.
            source="emit_nba_interaction_term",
        )

        # Long-tail NBA features — hustle, drives, clutch — for the prop subject.
        # Cached only at scoring time (allow_network=False); the daily warm job
        # populates these league-wide leaderboards. ``resolved.nba_stats_id``
        # is set by ``_load_nba_advanced`` when resolution succeeds, so we
        # avoid re-scanning EspnPlayerSearchCache here.
        nba_stats_id = resolved.nba_stats_id

        if nba_stats_id:
            hustle_result = load_nba_hustle_player(db, season=resolved.season, allow_network=False)
            emit_to_group(
                feature_groups,
                features,
                "nba_hustle",
                emit_nba_hustle_features(hustle_result.payload, str(nba_stats_id)),
                source="NbaHustleStatsCache",
            )

            drives_result = load_nba_tracking(
                db, season=resolved.season, pt_measure_type="Drives", allow_network=False
            )
            emit_to_group(
                feature_groups,
                features,
                "nba_drives",
                emit_nba_drives_features(drives_result.payload, str(nba_stats_id)),
                source="NbaPlayerTrackingCache",
            )

            clutch_result = load_nba_clutch_player(db, season=resolved.season, allow_network=False)
            emit_to_group(
                feature_groups,
                features,
                "nba_clutch",
                emit_nba_clutch_features(clutch_result.payload, str(nba_stats_id)),
                source="NbaClutchStatsCache",
            )

        # Smarter #13 phase 2d — referee tendency factor.
        # Phases 2a/2b shipped the daily assignments + per-season
        # tendency caches; phase 2c shipped the emitter that joins
        # them for one event. This call is the consumer-side wiring
        # that surfaces ``referee_avg_fouls_per_game`` etc. into the
        # features dict so ``heuristic_factors._nba_referee_factor``
        # actually fires on real games.
        #
        # ``allow_network=False`` keeps scoring off the network: the
        # daily refresh job populates the assignments cache; the BR
        # tendency cache is populated by the (deferred) phase 2b-2
        # CLI / job. Either cache being empty yields an empty
        # emitter return → the factor's ``data_complete`` gate keeps
        # it at 1.0 (no-op, filtered out).
        if team_entry is not None and opponent_entry is not None:
            # Codex review P2: the assignments cache is keyed by the
            # NBA game date (US/Eastern), not UTC-today. A 10pm PT
            # game starts at 05:00 UTC the next day; without
            # ET-conversion, scoring would read the wrong daily
            # assignment row and the factor would silently no-op or
            # apply a different day's crew for the same matchup.
            from zoneinfo import ZoneInfo  # noqa: PLC0415 — local import
            assignment_date: date | None = None
            if event.starts_at is not None:
                # SQLite returns ``DateTime(timezone=True)`` as naive
                # UTC. ``astimezone`` on a naive datetime treats it as
                # the host's local time — wrong for SQLite deployments
                # near the UTC date boundary (codex review round 2 P2).
                # Coerce to UTC first.
                starts_at_utc = event.starts_at
                if starts_at_utc.tzinfo is None:
                    starts_at_utc = starts_at_utc.replace(tzinfo=timezone.utc)
                assignment_date = starts_at_utc.astimezone(
                    ZoneInfo("America/New_York")
                ).date()
            assignments_payload = load_nba_referee_assignments(
                db, allow_network=False, target_date=assignment_date,
            )
            tendencies_payload = load_nba_referee_tendencies(
                db, season=resolved.season, fetcher=_unavailable_referee_fetcher,
                allow_network=False,
            )
            home_entry = team_entry if team_entry.is_home else opponent_entry
            away_entry = opponent_entry if team_entry.is_home else team_entry
            emit_to_group(
                feature_groups,
                features,
                "nba_referee",
                emit_nba_referee_features(
                    assignments_payload=assignments_payload,
                    tendencies_payload=tendencies_payload,
                    away_team_name=away_entry.participant.display_name,
                    home_team_name=home_entry.participant.display_name,
                ),
                source="NbaRefereeAssignmentCache+NbaRefereeTendenciesCache",
            )

    elif resolved.sport_key.upper() == "WNBA":
        # WNBA branch. PR 4 shipped the workload signal; Smarter WNBA
        # PR 7 layered the injury suppression gate on top — ESPN serves
        # WNBA injuries at ``/basketball/wnba/injuries`` with the same
        # schema as NBA, so the loader + emitter + feature-group policy
        # all mirror NBA in lockstep. What still DOESN'T flow (separate
        # follow-ups):
        #
        # - NBA advanced stats client (stats.nba.com) → no
        #   ``nba_advanced`` / ``nba_opponent_team`` / ``nba_interaction``
        #   / hustle / drives / clutch groups for WNBA. These require a
        #   generalized stats client; documented as a separate WNBA
        #   advanced-stats follow-up.
        # - basketball-reference referee tendencies (Smarter #13) → no
        #   referee features for WNBA. RefMetrics has WNBA referee data
        #   but requires a separate scraper PR.
        #
        # See SMARTER_WNBA_PREP.md §6 for the MVP+1 sequence.
        from app.services.advanced_stats import (
            emit_nba_injury_features,
            emit_nba_workload_features,
        )
        from app.services.wnba_injury_report import load_wnba_injury_report

        # Smarter WNBA PR 7 — injury features for WNBA. ``emit_nba_injury_features``
        # is sport-agnostic (keys are ``player_injury_status_*`` /
        # ``injury_report_is_fresh`` / ``injury_data_complete``, no NBA
        # in the schema). ``allow_network=False`` keeps scoring off the
        # network: the daily ``wnba_injury_refresh`` job populates the
        # cache out-of-band, and a cache miss yields an empty payload.
        # The ``wnba_injury`` SUPPRESS-policy entry registered in
        # ``feature_groups.py`` consumes these features via
        # ``wnba_injury_suppress_when`` (family-key gated to
        # ``wnba_props``).
        wnba_injury_payload = load_wnba_injury_report(db, allow_network=False)
        emit_to_group(
            feature_groups,
            features,
            "wnba_injury",
            emit_nba_injury_features(
                wnba_injury_payload, player_name=resolved.display_name
            ),
            source="WnbaInjuryReportCache",
        )

        # ``wnba_workload`` is registered in feature_groups.py with the
        # same PENALIZE (-3% / 24h) policy as nba_workload. The emitter
        # is sport-agnostic (reads ``game_logs`` for ``minutes``); the
        # group key is sport-prefixed so operator diagnostics stay clear
        # about which sport the workload signal came from.
        emit_to_group(
            feature_groups,
            features,
            "wnba_workload",
            emit_nba_workload_features(resolved.game_logs),
            fresh_at=resolved.gamelog_cached_at,
            source="EspnPlayerGamelogCache",
        )

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
            mlb_park_coords,
            resolve_mlb_stats_player_id,
        )

        if resolved.advanced_payload:
            sabermetrics = resolved.advanced_payload.get("batter_sabermetrics")
            statcast = resolved.advanced_payload.get("batter_statcast")
            emit_to_group(
                feature_groups,
                features,
                "mlb_batter",
                emit_mlb_batter_features(sabermetrics, statcast),
                source="MlbBatterSabermetricsCache+MlbBatterStatcastCache",
            )

        # Bug #4: park factors are not keyed by ESPN's venue id; the
        # helper prefers venue-name match (disambiguates TBR Tropicana
        # vs. Steinbrenner), then home team abbreviation, then legacy
        # top-level venue_id for any non-ESPN rows.
        home_competitor = _competitor_for_role(event, "home")
        home_team_abbr = (home_competitor.get("team") or {}).get("abbreviation")
        park = load_park_factors_for_event(event.raw_data, home_team_abbr)
        emit_to_group(
            feature_groups,
            features,
            "mlb_park",
            emit_park_features(park),
            # Park factors are season-stable; IGNORE policy (default)
            # never penalizes. fresh_at=None reflects that.
            source="load_park_factors_for_event",
        )

        venue_indoor_flag = bool(features.get("venue_indoor"))
        # Bug #4 fix: weather lookup needs lat/lon to actually return
        # data for a specific game. ESPN's venue payload doesn't
        # include coordinates, but ``mlb_park_coords`` carries the
        # canonical (lat, lon, is_dome) tuple per home team — same
        # source of truth Smarter #15's weather pre-warm job will use,
        # so the read path and the warm path stay aligned. Falls back
        # to (None, None) for non-MLB or unmapped teams; in that case
        # ``load_weather`` continues to no-op as before.
        park_coords = mlb_park_coords(home_team_abbr)
        weather_lat = park_coords[0] if park_coords else None
        weather_lon = park_coords[1] if park_coords else None
        # Per-game ESPN ``venue.indoor`` is more authoritative than the
        # coords-table is_dome flag (catches retractable-roof openings/
        # closings) — keep ESPN's signal as the dome source of truth.
        starts_at = event.starts_at
        if starts_at is not None and starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=timezone.utc)
        game_time_utc = starts_at.astimezone(timezone.utc) if starts_at else None
        weather_result = load_weather(
            db,
            event_id=str(event.id),
            lat=weather_lat,
            lon=weather_lon,
            game_time_utc=game_time_utc,
            is_dome=venue_indoor_flag,
            allow_network=False,
        )
        if weather_result.payload:
            emit_to_group(
                feature_groups,
                features,
                "mlb_weather",
                emit_weather_features(weather_result.payload),
                # Architecture #5 — mlb_weather is PENALIZE (-5% /
                # 6h TTL). load_weather populates cached_at on the
                # AdvancedLoadResult for every return path; dome
                # games return cached_at=None (no refresh lifecycle).
                fresh_at=weather_result.cached_at,
                source="load_weather",
            )
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
                emit_to_group(
                    feature_groups,
                    features,
                    "mlb_starter",
                    emit_mlb_pitcher_features(
                        pitcher_result.payload,
                        pitcher_statcast_result.payload,
                    ),
                    source="MlbPitcherAdvancedCache+MlbPitcherStatcastCache",
                )

        # Lineup context — batting-order position drives the lineup_factor.
        # ``resolved.mlb_stats_id`` is set by ``_load_mlb_advanced``; no need
        # to re-scan the search cache here.
        lineup_result = load_lineup_for_event(db, event_id=str(event.id))
        if lineup_result.payload and resolved.mlb_stats_id:
            emit_to_group(
                feature_groups,
                features,
                "mlb_lineup",
                emit_lineup_features(lineup_result.payload, str(resolved.mlb_stats_id)),
                # IGNORE policy — Smarter #16's bespoke gate
                # (confirmed-and-scratched suppression) stays
                # authoritative for this group.
                source="MlbLineupCache",
            )

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
                emit_to_group(
                    feature_groups,
                    features,
                    "mlb_platoon",
                    emit_mlb_platoon_features(
                        starter_pitch_hand,
                        splits_result.payload,
                        features.get("season_ops"),
                    ),
                    source="MlbBatterSplitsCache",
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
            # The scoring kernel reads ``opposing_bullpen_rest_index_3d``
            # — alias the away_* emission so the feature name matches
            # the matchup framing (the batter's perspective).
            rest_index = (
                bullpen_features.get("away_bullpen_rest_index_3d")
                if bullpen_features else None
            )
            if rest_index is not None:
                # Architecture #5 — mlb_bullpen is PENALIZE (-5% / 4h
                # TTL). The bullpen helper queries Event +
                # EventParticipant directly (no per-call cache), so the
                # "freshness" of the result depends on when the events
                # ingestion last ran. Source that signal from
                # upstream_health for ``espn_scoreboard`` (the source
                # that populates MLB events). If the events ingestion
                # is stale, the bullpen rest index is computed against
                # an incomplete schedule window — PENALIZE fires.
                emit_to_group(
                    feature_groups,
                    features,
                    "mlb_bullpen",
                    {
                        "opposing_bullpen_rest_index_3d": rest_index,
                        "bullpen_rest_data_complete": 1.0,
                    },
                    fresh_at=events_fresh_at,
                    source="Event+EventParticipant (espn_scoreboard refresh)",
                )

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

    if sport_key in {"NBA", "WNBA"}:
        # WNBA shares NBA's basketball box-score stat surface
        # (field_goals_attempted, assists, turnovers, minutes), so the
        # minute_factor + usage_factor proxies work unchanged for WNBA
        # via _usage_proxy / _log_average. The advanced-data probes
        # (recent_usage_pct, opponent_pace_recent_5) will always read
        # None for WNBA today — the nba_advanced / nba_opponent_team
        # groups are NBA-only (no WNBA data source) — so
        # has_advanced_*_data resolves False and the gamelog-based
        # proxy path runs for every WNBA prop.
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
    # Smarter NFL PR 7 — opponent def EPA + weather + snap-share proxy
    # land in features before the advanced-factor pass reads them.
    if sport_key == "NFL":
        _emit_nfl_prop_context(
            db, event, opponent_entry, nfl_snap_shares or [], features, feature_groups,
        )

    expected_before_advanced = expected
    features["expected_before_advanced"] = round(expected_before_advanced, 3)

    probability_yes = _prop_yes_probability(sport_key, stat_key, expected, threshold, features)
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
        probability_yes = _prop_yes_probability(
            sport_key, stat_key, expected_after_advanced, threshold, features,
        )
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

    # Smarter #21 phase 2d — interval-model consumer. Gated on (1) a
    # trained sidecar existing for this stat key on the served
    # artifact and (2) ``coverage_status == "ok"`` (strict policy —
    # the 2026-05-16 demo proved 4/7 stat keys land in ``bad``
    # coverage where intervals would ship worse than Poisson). The
    # diagnostic is ALWAYS surfaced when the sidecar exists so the
    # operator can A/B inspect interval vs. Poisson per prop; the
    # probability is only swapped when coverage clears the gate.
    interval_diag = consume_prediction_interval(
        family_key=single_family_key(sport_key, "player_prop"),
        stat_key=stat_key,
        threshold=threshold,
        features=features,
        poisson_yes_probability=probability_yes,
    )
    if interval_diag is not None:
        features["prediction_interval"] = interval_diag
        if interval_diag["coverage_status"] == "ok":
            probability_yes = float(interval_diag["yes_probability_from_interval"])
            features["yes_probability"] = round(probability_yes, 4)

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

    return probability_yes, confidence, reasons, features, feature_groups


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
    # Architecture #5 follow-up 2 — Smarter #16 / #17 bespoke gates
    # consolidated into the unified policy registry. ``check_suppressions``
    # resolves SUPPRESS-policy groups (mlb_lineup, nba_injury,
    # wnba_injury) and returns ``{group_key: suppression_reason}``. The
    # branches below consume the result and translate to the existing
    # intermediate diagnostic keys (``lineup_suppression_reason``,
    # ``injury_suppression_reason``) so downstream readers see the same
    # shape they did pre-consolidation.
    suppressions = check_suppressions(
        SuppressionContext(
            features=features, metadata=metadata, family_key=family_key,
        )
    )
    # Smarter #16: flipped to True when ``copilot_requires_lineup`` is set
    # AND lineup data IS confirmed AND the player is NOT in the starting
    # lineup. Sourced from ``suppressions["mlb_lineup"]``.
    lineup_scratch_suppression = "mlb_lineup" in suppressions
    # Smarter #17 + WNBA PR 7: ``"player_injury_out"`` or
    # ``"player_injury_doubtful"`` when the unified gate fires for
    # either ``nba_injury`` or ``wnba_injury``. Per-sport callbacks
    # are family-key gated so at most one fires per scoring pass; the
    # ``or`` aggregator picks whichever did. The reason string itself
    # is sport-agnostic — the downstream translation in
    # ``_build_scored_recommendation`` only inspects the reason, not
    # which group keyed it.
    injury_suppression_reason: str | None = (
        suppressions.get("nba_injury")
        or suppressions.get("wnba_injury")
        # Smarter NFL PR 6 — NFL prop OUT/DOUBTFUL + the questionable-QB
        # game-line gate ride the same aggregation; the downstream
        # translation inspects the reason string, not the group key.
        or suppressions.get("nfl_injury")
        or suppressions.get("nfl_qb_status")
    )

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
        # Smarter #16 — lineup confirmation context. The suppression
        # decision itself flows through ``check_suppressions`` above;
        # this block keeps the surrounding ``feature_flags`` /
        # ``missing_context`` bookkeeping that the unified path doesn't
        # own (it's about reporting context coverage, not about
        # whether to drop the recommendation).
        if metadata.get("copilot_requires_lineup"):
            lineup_data_complete = float(features.get("lineup_data_complete") or 0.0) >= 1.0
            player_in_starting_lineup = (
                float(features.get("player_in_starting_lineup") or 0.0) >= 1.0
            )
            feature_flags["lineup_confirmation"] = (
                lineup_data_complete and player_in_starting_lineup
            )
            if not lineup_data_complete:
                missing_context.append("lineup_confirmation")
            elif not lineup_scratch_suppression and family_key == "nba_props":
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
    if injury_suppression_reason is not None:
        diagnostics["injury_suppression_reason"] = injury_suppression_reason
    # Smarter #18 phase 2c — surface the sportsbook H2H consensus as a
    # scoring diagnostic. Pure information for now; the suppression
    # rule (phase 2d, deferred) is what will gate on disagreement.
    # Reads from cache only — never invokes the network here. The
    # cache is empty by default (Odds API key required) so this is a
    # no-op until operators wire the key + refresh job.
    #
    # Reviewer HIGH catch: the emitter calls into the cache loader
    # (DB query) and the de-vig math (arithmetic on bookmaker prices).
    # An unexpected failure here would drop an unrelated recommendation
    # — a correctness regression for a feature billed as "diagnostic
    # only". Wrap defensively so any exception degrades to "no
    # sportsbook signal for this pick" with a log line.
    from app.services.sportsbook_consensus import emit_sportsbook_consensus_diagnostics  # noqa: PLC0415 — avoid module-import cycle through services/

    try:
        sportsbook_diagnostics = emit_sportsbook_consensus_diagnostics(db, event)
    except Exception as exc:  # noqa: BLE001 — diagnostic must never break scoring
        logger.warning("sportsbook consensus diagnostic failed: %s", exc)
        sportsbook_diagnostics = {}
    if sportsbook_diagnostics:
        diagnostics.update(sportsbook_diagnostics)
        # Smarter #18 phase 2d — when the consensus disagrees with
        # the model by more than ``threshold_pp`` AND the consensus
        # is averaged from at least ``min_book_count`` books, flag
        # the recommendation for suppression. ``_build_scored_recommendation``
        # reads ``sportsbook_disagreement_suppression`` from the
        # diagnostics dict and appends it to ``suppression_reasons``.
        # OFF by default — operators eyeball the diagnostic in phase 2c
        # first, then flip the toggle when confident.
        from app.services.operator_settings import (  # noqa: PLC0415 — avoid cycle
            effective_sportsbook_disagreement_min_book_count,
            effective_sportsbook_disagreement_suppression_enabled,
            effective_sportsbook_disagreement_threshold,
        )
        try:
            suppression_enabled = effective_sportsbook_disagreement_suppression_enabled(db)
        except Exception as exc:  # noqa: BLE001 — settings read must never break scoring
            logger.warning("sportsbook disagreement toggle read failed: %s", exc)
            suppression_enabled = False
        if suppression_enabled:
            try:
                threshold = effective_sportsbook_disagreement_threshold(db)
                min_book_count = effective_sportsbook_disagreement_min_book_count(db)
            except Exception as exc:  # noqa: BLE001
                logger.warning("sportsbook disagreement settings read failed: %s", exc)
                threshold = 0.15
                min_book_count = 3
            consensus_prob = sportsbook_diagnostics.get("sportsbook_consensus_prob")
            book_count = sportsbook_diagnostics.get("sportsbook_book_count")
            # Round the gap to 4 decimals (same precision the consensus
            # prob is reported at) so the boundary check isn't fooled
            # by float representation — ``0.60 - 0.45`` = 0.14999...97
            # in IEEE-754, which would silently miss the threshold.
            gap = (
                round(abs(float(probability_yes) - float(consensus_prob)), 4)
                if isinstance(consensus_prob, (int, float))
                else None
            )
            if (
                gap is not None
                and isinstance(book_count, int)
                and book_count >= min_book_count
                and gap >= threshold
            ):
                # Emit a structured diagnostic so the suppression-reason
                # mapper can produce the right outcome label and so
                # operators can see what the gap was on each pick.
                # ``sportsbook_disagreement_gap`` is SIGNED (probability_yes
                # − consensus_prob): positive means sika thinks the
                # selected side is MORE likely than the book consensus;
                # negative means sika thinks it's LESS likely. The
                # absolute-value threshold check above suppresses on
                # either direction, but operators see the sign to
                # quickly know "are we more bullish or more bearish
                # than the book?"
                diagnostics["sportsbook_disagreement_suppression"] = "model_book_disagreement"
                diagnostics["sportsbook_disagreement_gap"] = round(
                    float(probability_yes) - float(consensus_prob), 4
                )
                diagnostics["sportsbook_disagreement_threshold"] = round(float(threshold), 4)
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
    *,
    events_fresh_at: datetime | None = None,
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

    # Architecture #5 — non-prop scorers don't currently call emitters;
    # they return an empty feature_groups dict. The freshness check is a
    # no-op for those scopes (no groups → no assessments → no penalty).
    feature_groups: dict[str, FeatureGroupSnapshot] = {}
    if market and market_family == "player_prop":
        prop_score = _score_player_prop(
            db, event, market, snapshot, resolver or PropStatsResolver(db),
            events_fresh_at=events_fresh_at,
        )
        if prop_score is None:
            return None
        probability_yes, confidence, reasons, features, feature_groups = prop_score
        probability_subject = str(metadata.get("copilot_subject_name") or "Player")
    elif market and market_family == "game_line":
        if not left or not right:
            return None
        # Smarter NFL PR 5 — NFL routes to the consensus-anchored model
        # (empirical key-number margin grid). Non-NFL paths unchanged.
        if (event.sport_key or "").upper() == "NFL":
            from app.services.scoring.nfl_game_model import score_nfl_game_line

            nfl_line_score = score_nfl_game_line(db, event, market, left, right)
            if nfl_line_score is None:
                return None
            probability_yes, confidence, reasons, features, feature_groups = nfl_line_score
        else:
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
        elif (event.sport_key or "").upper() == "NFL":
            # Smarter NFL PR 5 — consensus-anchored winner model.
            from app.services.scoring.nfl_game_model import score_nfl_team_winner

            left_win_probability, confidence, reasons, features, feature_groups = (
                score_nfl_team_winner(db, event, left, right)
            )
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

    # Smarter #21 phase 2d (PR 4) — surface the prediction-interval
    # diagnostic on ``scoring_diagnostics`` so downstream surfaces
    # (the trade-desk endpoint, the trade-ticket UI band) can read
    # it from ``recommendation.scoring_diagnostics`` instead of
    # plucking it from the signal's raw ``features`` blob. The
    # consumer in PR 3 writes the same payload into ``features``
    # for ML-training continuity; this copy makes the operator-
    # facing diagnostic available without a second column lookup.
    #
    # The ML-inference branch below (around line 2200) rebuilds
    # ``scoring_diagnostics`` via ``{**scoring_diagnostics, ...}``
    # — the spread preserves the ``prediction_interval`` key
    # because the ML branch doesn't write its own. Verified manually
    # during PR review.
    prediction_interval = features.get("prediction_interval")
    if prediction_interval is not None:
        scoring_diagnostics["prediction_interval"] = prediction_interval

    # Architecture #5 — compute per-group freshness policy. Penalty
    # application is DEFERRED until after the ML branch below so it
    # applies to both the heuristic path AND the ML-served confidence.
    # Reviewer round 1 caught the original ordering as a correctness
    # bug: the ML model was trained on historical rows and doesn't
    # know the staleness state at inference time, so its calibrated
    # confidence doesn't encode the per-group freshness signal — the
    # penalty has to ride on top of whatever confidence the ML branch
    # produces, not be overwritten by it.
    total_freshness_delta = 0.0
    if feature_groups:
        freshness_now = datetime.now(timezone.utc)
        freshness_assessments = check_freshness(feature_groups, now=freshness_now)
        stale_groups: list[dict[str, Any]] = []
        for assessment in freshness_assessments:
            if assessment.is_stale:
                stale_groups.append(
                    {
                        "group_key": assessment.group_key,
                        "severity": assessment.severity.value,
                        "age_seconds": (
                            int(assessment.age.total_seconds())
                            if assessment.age is not None else None
                        ),
                        "confidence_delta": assessment.confidence_delta,
                    }
                )
                total_freshness_delta += assessment.confidence_delta
        # Always surface the serialized feature_groups + stale-group
        # diagnostics so operators can audit freshness even when no
        # penalty fires.
        scoring_diagnostics["feature_groups"] = serialize_feature_groups(feature_groups)
        if stale_groups:
            scoring_diagnostics["freshness_stale_groups"] = stale_groups
        if total_freshness_delta != 0.0:
            scoring_diagnostics["freshness_confidence_delta"] = round(total_freshness_delta, 4)

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
            # Smarter #20 phase 2c: persist the raw model output (pre-
            # recalibration) into scoring_diagnostics so the apps/ml
            # ``recalibrate`` CLI can fit the next rolling sidecar on
            # the model's actual distribution rather than on already-
            # recalibrated values. Without this, repeated CLI runs
            # would chain isotonic fits against the post-process output,
            # which is a different input scale (codex round 2 P1).
            recalibration_applied = bool(ml_result.metadata.get("recalibration_applied"))
            raw_probability = ml_result.metadata.get("raw_probability")
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
                "recalibration_applied": recalibration_applied,
                **(
                    {"raw_probability": round(float(raw_probability), 4)}
                    if recalibration_applied and raw_probability is not None
                    else {}
                ),
            }
            reasons = [*reasons, f"Served by {ml_result.lineage.model_name}."]
        else:
            if runtime_decision and runtime_decision.fallback_active and runtime_decision.last_error:
                scoring_diagnostics["fallback_reason"] = runtime_decision.last_error
                scoring_diagnostics["serving_mode"] = "heuristic_fallback"
                reasons = [*reasons, f"Served by heuristic fallback because ML was unavailable: {runtime_decision.last_error}"]
            else:
                scoring_diagnostics["serving_mode"] = "heuristic"

    # Architecture #5 — apply the per-group freshness penalty AFTER
    # the ML branch overrides confidence. Reviewer round 1 caught
    # the original ordering as a correctness bug: putting this
    # before the ML branch meant ``ml_result.confidence`` silently
    # discarded the penalty on the ML path, even though the model
    # was trained on historical rows and doesn't know the staleness
    # state of the current scoring call. Applying here means the
    # penalty rides on top of whatever confidence the kernel chose
    # (heuristic or ML), bounded so a deep penalty can't push below
    # zero.
    if total_freshness_delta != 0.0:
        confidence = max(0.0, round(confidence + total_freshness_delta, 4))

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
        family_key=family_key,
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
    # Smarter #30 — per-family edge floor with operator-setting
    # fallback. Empty override registry today means this resolves to
    # ``settings.watchlist_min_edge`` for every family; populating
    # ``WATCHLIST_MIN_EDGE_OVERRIDES`` tunes individual families
    # without touching this call site.
    family_min_edge = watchlist_min_edge_for(family_key, settings.watchlist_min_edge)
    if edge < family_min_edge:
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
    # Smarter #17: ESPN-style "player ruled out/doubtful" with a fresh
    # report. The lineup-confirmation pathway covers the pre-tip window
    # but doesn't cover late-breaking injury news that lands after
    # lineups are posted. Treat as a hard suppression — the prop is a
    # near-zero either way.
    injury_reason = str(scoring_diagnostics.get("injury_suppression_reason") or "")
    # Smarter NFL PR 6 adds ``starting_qb_questionable`` — an NFL
    # game-line pick with an unresolved QB1 is unpriceable, not
    # mispriced, so it suppresses like a hard injury signal.
    if injury_reason in {"player_injury_out", "player_injury_doubtful", "starting_qb_questionable"}:
        suppression_reasons.append(injury_reason)
    # Smarter #18 phase 2d: sportsbook consensus disagrees with the
    # model by more than the operator-configured pp threshold (and
    # the consensus has enough books to be authoritative). Flag set
    # inside ``_single_scoring_adjustments``; this just appends to
    # the suppression list so the recommendation gets dropped from
    # the watchlist with a clear outcome reason.
    if str(scoring_diagnostics.get("sportsbook_disagreement_suppression") or "") == "model_book_disagreement":
        suppression_reasons.append("model_book_disagreement")
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
    # Single-market entrypoint: events_fresh_at is computed once here
    # so the kernel's PENALIZE policy for mlb_bullpen has a freshness
    # signal without a redundant per-emitter upstream_health read.
    scored = _build_scored_recommendation(
        db, event, market, snapshot, resolver=resolver,
        events_fresh_at=_events_ingestion_fresh_at(db),
    )
    if not scored:
        return None
    db.add(scored.signal)
    return scored.recommendation


# Monotonicity clamps + thesis-key dedupe — see scoring/monotonicity.py
# for the full implementations. Re-exported here so existing
# consumers (tests, orchestration helpers in this module) keep
# working unchanged.
from app.services.scoring.monotonicity import (  # noqa: E402
    _dedupe_prediction_recommendations,
    _dedupe_winner_recommendations,
    _enforce_prop_monotonicity,
    _apply_prediction_monotonicity,
    _prediction_recommendation_tuple,
    _quality_tier_rank,
)


# Prediction → Recommendation rehydration helpers live in
# scoring.persistence (extracted as part of R1). Re-exported here
# for backward-compat with consumers / tests that import from
# ``app.services.scoring``.
from app.services.scoring.persistence import (  # noqa: E402
    _build_recommendation_from_prediction,
    _parlay_candidate_from_prediction,
    _persist_scored_watchlist_captures,
    _signal_snapshot_from_prediction,
)


