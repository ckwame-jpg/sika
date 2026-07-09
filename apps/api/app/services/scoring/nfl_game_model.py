"""Smarter NFL PR 5 — consensus-anchored NFL game-line scoring.

Market-anchored + situational design: the de-vigged sportsbook
consensus (Smarter NFL PR 4 anchor) is the location estimate the
internal EPA rating model blends toward; the kernel-conditional margin
grid (``ml_features.nfl_pricing``) turns the blended location into
probabilities that respect NFL key numbers.

Situational adjustments apply to the INTERNAL projection only — the
books already price QB news, rest and weather into their lines, so
adjusting the consensus side would double-count.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ml_features.nfl_pricing import (
    blend_line,
    blend_probability,
    nfl_margin_yes_probability,
    nfl_total_yes_probability,
    nfl_win_probability,
)

from app.config import get_settings
from app.models import Event, EventParticipant, Market
from app.services.nfl_advanced import (
    load_nfl_depth_chart,
    load_nfl_official_injuries,
    load_nfl_schedule,
    load_nfl_team_ratings,
    load_nfl_weather,
    nfl_stadium_info,
    nfl_team_abbr_for_name,
    normalize_nfl_team_code,
)
from app.services.nfl_market_anchor import NflConsensusAnchor, nfl_consensus_anchor
from app.services.scoring.feature_groups import FeatureGroupSnapshot, emit_to_group
from app.services.scoring.heuristics import _market_metadata, _token_score, clamp
from app.sports.base import alias_tokens


logger = logging.getLogger(__name__)

# 2020–2025 mean home margin is 1.74 (nfldata games.csv). Re-tuned by
# the Smarter NFL PR 9 backtest.
NFL_HOME_FIELD_ADVANTAGE_POINTS = 1.7
BYE_REST_BONUS_POINTS = 1.0
SHORT_WEEK_PENALTY_POINTS = 1.5
BYE_REST_DAYS = 13
SHORT_WEEK_REST_DAYS = 5
# League-average fallback when season-to-date scoring data is missing.
NFL_LEAGUE_AVERAGE_TOTAL = 44.0
# Wind starts biting passing/kicking ~15 mph; scale the internal total
# down 0.5%/mph beyond that, capped at -8%. Heavy rain/snow ≈ -2%.
WIND_TOTAL_SCALE_START_MPH = 15.0
WIND_TOTAL_SCALE_PER_MPH = 0.005
WIND_TOTAL_MAX_REDUCTION = 0.08
PRECIP_TOTAL_REDUCTION = 0.02
PRECIP_THRESHOLD_PCT = 60.0
# Week 1–4 shrink: a team's current-season rating earns full weight at
# 4 games played; the remainder comes from last season's rating.
RATING_SHRINK_GAMES = 4.0


@dataclass(slots=True)
class NflGameProjection:
    """One event's blended projection + everything scoring needs."""

    margin_home: float  # blended, home-positive
    total: float  # blended + weather-adjusted internal component
    internal_margin: float
    internal_total: float
    anchor: NflConsensusAnchor | None
    sample_games: int  # home+away current-season games behind ratings
    features: dict[str, Any]
    reasons: list[str]
    feature_groups: dict[str, FeatureGroupSnapshot]


def _home_away(
    left: EventParticipant, right: EventParticipant
) -> tuple[EventParticipant, EventParticipant]:
    if right.is_home and not left.is_home:
        return right, left
    return left, right  # left-first fallback matches the kernel's sort


def _team_rating_points(
    teams: dict[str, Any], code: str | None
) -> tuple[float | None, float, float, float]:
    """(net points/game, games, points_for/gm, points_against/gm)."""
    entry = (teams or {}).get(code or "") or {}
    net = entry.get("net_epa_per_play")
    plays = entry.get("plays_per_game")
    games = float(entry.get("games") or 0.0)
    if not isinstance(net, (int, float)) or not isinstance(plays, (int, float)):
        return None, games, 0.0, 0.0
    return (
        float(net) * float(plays),
        games,
        float(entry.get("points_for_per_game") or 0.0),
        float(entry.get("points_against_per_game") or 0.0),
    )


def _shrunk(current: float | None, games: float, prior: float | None) -> float | None:
    """Weeks 1–4 shrink toward last season's value."""
    if current is None and prior is None:
        return None
    if current is None:
        return prior
    if prior is None:
        return current
    weight = clamp(games / RATING_SHRINK_GAMES, 0.0, 1.0)
    return weight * current + (1.0 - weight) * prior


def _qb_out_adjustment(
    db: Session,
    season: int,
    team_code: str | None,
    features: dict[str, Any],
    prefix: str,
) -> float:
    """Margin penalty (positive number) when the depth-chart QB1 is
    Out / Doubtful on the latest OFFICIAL club report (nflverse; the
    ESPN intraday feed + questionable-SUPPRESS land in PR 6)."""
    if not team_code:
        return 0.0
    settings = get_settings()
    depth = load_nfl_depth_chart(db, season, team_code)
    qb1 = next(
        (
            row
            for row in (depth.payload.get("rows") or [])
            if str(row.get("pos_abb") or "").upper() == "QB"
            and str(row.get("pos_rank") or "") in {"1", "1.0"}
        ),
        None,
    )
    if qb1 is None:
        return 0.0
    features[f"{prefix}_qb1_name"] = qb1.get("player_name")
    injuries = load_nfl_official_injuries(db, season)
    qb_gsis = str(qb1.get("gsis_id") or "")
    report_row = next(
        (
            row
            for row in (injuries.payload.get("rows") or [])
            if qb_gsis and str(row.get("gsis_id") or "") == qb_gsis
        ),
        None,
    )
    status = str((report_row or {}).get("report_status") or "").strip().lower()
    features[f"{prefix}_qb1_report_status"] = status or None
    if status in {"out", "doubtful"}:
        features[f"{prefix}_qb1_out"] = 1.0
        return float(settings.nfl_qb_out_margin_penalty)
    return 0.0


def _rest_adjustment(
    db: Session,
    season: int,
    event: Event,
    home_code: str | None,
    away_code: str | None,
    features: dict[str, Any],
) -> float:
    """Home-margin adjustment from rest asymmetry (bye / short week),
    read off the nflverse schedule's ``home_rest`` / ``away_rest``."""
    if not home_code or not away_code or event.starts_at is None:
        return 0.0
    schedule = load_nfl_schedule(db, season)
    event_date = event.starts_at.date()
    game = next(
        (
            row
            for row in (schedule.payload.get("games") or [])
            if normalize_nfl_team_code(row.get("home_team")) == home_code
            and normalize_nfl_team_code(row.get("away_team")) == away_code
            and abs(
                (_parse_gameday(row.get("gameday")) - event_date).days
                if _parse_gameday(row.get("gameday"))
                else 99
            )
            <= 1
        ),
        None,
    )
    if game is None:
        return 0.0
    try:
        home_rest = float(game.get("home_rest") or 7)
        away_rest = float(game.get("away_rest") or 7)
    except (TypeError, ValueError):
        return 0.0
    features["nfl_home_rest_days"] = home_rest
    features["nfl_away_rest_days"] = away_rest
    adjustment = 0.0
    if home_rest >= BYE_REST_DAYS and away_rest < BYE_REST_DAYS:
        adjustment += BYE_REST_BONUS_POINTS
    if away_rest >= BYE_REST_DAYS and home_rest < BYE_REST_DAYS:
        adjustment -= BYE_REST_BONUS_POINTS
    if home_rest <= SHORT_WEEK_REST_DAYS and away_rest > SHORT_WEEK_REST_DAYS:
        adjustment -= SHORT_WEEK_PENALTY_POINTS
    if away_rest <= SHORT_WEEK_REST_DAYS and home_rest > SHORT_WEEK_REST_DAYS:
        adjustment += SHORT_WEEK_PENALTY_POINTS
    return adjustment


def _parse_gameday(raw: Any):
    from datetime import date

    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _weather_total_factor(
    db: Session,
    event: Event,
    home_code: str | None,
    features: dict[str, Any],
    feature_groups: dict[str, FeatureGroupSnapshot],
) -> float:
    """Multiplicative factor on the INTERNAL total from wind / precip.
    Domes and retractables return 1.0 (loader short-circuits)."""
    weather = load_nfl_weather(
        db,
        event_id=str(event.id),
        home_team_abbr=home_code,
        game_time_utc=event.starts_at,
        allow_network=False,  # warmed by the nfl_data_refresh job
    )
    stadium = nfl_stadium_info(home_code)
    weather_values: dict[str, Any] = {
        "nfl_weather_data_complete": 1.0 if weather.complete else 0.0,
        "nfl_is_dome": 1.0 if weather.payload.get("is_dome") else 0.0,
        "nfl_stadium_roof": (stadium or {}).get("roof"),
    }
    factor = 1.0
    if weather.complete and not weather.payload.get("is_dome"):
        wind = float(weather.payload.get("wind_speed_mph") or 0.0)
        precip = float(weather.payload.get("precip_pct") or 0.0)
        weather_values["nfl_wind_mph"] = wind
        weather_values["nfl_precip_pct"] = precip
        if wind > WIND_TOTAL_SCALE_START_MPH:
            factor -= min(
                (wind - WIND_TOTAL_SCALE_START_MPH) * WIND_TOTAL_SCALE_PER_MPH,
                WIND_TOTAL_MAX_REDUCTION,
            )
        if precip >= PRECIP_THRESHOLD_PCT:
            factor -= PRECIP_TOTAL_REDUCTION
    weather_values["nfl_weather_total_factor"] = round(factor, 4)
    emit_to_group(
        feature_groups,
        features,
        "nfl_weather",
        weather_values,
        # Dome payloads are constant by construction → no freshness cycle.
        fresh_at=weather.cached_at if weather.cache_status != "dome" else None,
        source="NflWeatherCache",
    )
    return factor


def build_nfl_game_projection(
    db: Session,
    event: Event,
    left: EventParticipant,
    right: EventParticipant,
    *,
    allow_network: bool = True,
    now: datetime | None = None,
) -> NflGameProjection:
    from app.services.stats_query import default_season_for_sport  # noqa: PLC0415 — avoid circular import

    features: dict[str, Any] = {}
    reasons: list[str] = []
    feature_groups: dict[str, FeatureGroupSnapshot] = {}

    home, away = _home_away(left, right)
    home_name = home.participant.display_name if home.participant else ""
    away_name = away.participant.display_name if away.participant else ""
    home_code = nfl_team_abbr_for_name(home_name)
    away_code = nfl_team_abbr_for_name(away_name)

    ref_date = event.starts_at.date() if event.starts_at else None
    season = default_season_for_sport("NFL", ref_date)

    ratings = load_nfl_team_ratings(db, season)
    prior_ratings = load_nfl_team_ratings(db, season - 1)
    teams = ratings.payload.get("teams") or {}
    prior_teams = prior_ratings.payload.get("teams") or {}

    home_rating, home_games, home_pf, home_pa = _team_rating_points(teams, home_code)
    away_rating, away_games, away_pf, away_pa = _team_rating_points(teams, away_code)
    prior_home_rating, _, prior_home_pf, prior_home_pa = _team_rating_points(prior_teams, home_code)
    prior_away_rating, _, prior_away_pf, prior_away_pa = _team_rating_points(prior_teams, away_code)

    home_strength = _shrunk(home_rating, home_games, prior_home_rating)
    away_strength = _shrunk(away_rating, away_games, prior_away_rating)
    ratings_complete = home_strength is not None and away_strength is not None
    rating_gap = (home_strength or 0.0) - (away_strength or 0.0)

    qb_home_penalty = _qb_out_adjustment(db, season, home_code, features, "nfl_home")
    qb_away_penalty = _qb_out_adjustment(db, season, away_code, features, "nfl_away")
    qb_adjustment = qb_away_penalty - qb_home_penalty
    rest_adjustment = _rest_adjustment(db, season, event, home_code, away_code, features)

    internal_margin = (
        rating_gap + NFL_HOME_FIELD_ADVANTAGE_POINTS + qb_adjustment + rest_adjustment
    )

    def _blend_pf_pa(current: float, games: float, prior: float) -> float:
        blended = _shrunk(current if games > 0 else None, games, prior if prior > 0 else None)
        return blended if blended is not None else 0.0

    home_pf_blend = _blend_pf_pa(home_pf, home_games, prior_home_pf)
    home_pa_blend = _blend_pf_pa(home_pa, home_games, prior_home_pa)
    away_pf_blend = _blend_pf_pa(away_pf, away_games, prior_away_pf)
    away_pa_blend = _blend_pf_pa(away_pa, away_games, prior_away_pa)
    if home_pf_blend > 0 and away_pf_blend > 0:
        internal_total = (
            (home_pf_blend + away_pa_blend) / 2.0 + (away_pf_blend + home_pa_blend) / 2.0
        )
    else:
        internal_total = NFL_LEAGUE_AVERAGE_TOTAL

    weather_factor = _weather_total_factor(db, event, home_code, features, feature_groups)
    internal_total *= weather_factor

    emit_to_group(
        feature_groups,
        features,
        "nfl_team_ratings",
        {
            "nfl_ratings_data_complete": 1.0 if ratings_complete else 0.0,
            "nfl_home_rating_points": round(home_strength, 3) if home_strength is not None else None,
            "nfl_away_rating_points": round(away_strength, 3) if away_strength is not None else None,
            "nfl_rating_gap": round(rating_gap, 3),
            "nfl_home_games_rated": home_games,
            "nfl_away_games_rated": away_games,
            "nfl_qb_adjustment": round(qb_adjustment, 2),
            "nfl_rest_adjustment": round(rest_adjustment, 2),
            "nfl_home_field_advantage": NFL_HOME_FIELD_ADVANTAGE_POINTS,
            "nfl_internal_margin": round(internal_margin, 3),
            "nfl_internal_total": round(internal_total, 3),
        },
        fresh_at=ratings.cached_at,
        source="NflTeamRatingCache",
    )

    anchor = nfl_consensus_anchor(db, event, allow_network=allow_network, now=now)
    consensus_margin = -anchor.spread_home if anchor and anchor.spread_home is not None else None
    spread_weight = _line_weight(anchor.spread_book_count if anchor else 0)
    total_weight = _line_weight(anchor.total_book_count if anchor else 0)
    margin_home = blend_line(internal_margin, consensus_margin, weight=spread_weight)
    total = blend_line(
        internal_total,
        anchor.total_line if anchor else None,
        weight=total_weight,
    )

    consensus_values: dict[str, Any] = {
        "nfl_consensus_data_complete": 1.0 if anchor else 0.0,
        "nfl_consensus_win_prob_home": anchor.win_prob_home if anchor else None,
        "nfl_consensus_spread_home": anchor.spread_home if anchor else None,
        "nfl_consensus_total_line": anchor.total_line if anchor else None,
        "nfl_consensus_book_count": anchor.book_count if anchor else 0,
        "nfl_blended_margin_home": round(margin_home, 3),
        "nfl_blended_total": round(total, 3),
    }
    emit_to_group(
        feature_groups,
        features,
        "nfl_consensus",
        consensus_values,
        fresh_at=anchor.fetched_at if anchor else None,
        source="odds_api_lines",
    )

    if anchor and anchor.spread_home is not None:
        reasons.append(
            f"Book consensus line: {home_name} {anchor.spread_home:+g} "
            f"({anchor.spread_book_count} books)"
        )
    if ratings_complete:
        stronger = home_name if rating_gap >= 0 else away_name
        reasons.append(f"EPA power ratings favor {stronger} by {abs(rating_gap):.1f} pts")
    else:
        reasons.append("Team EPA ratings unavailable — projection leans on the book anchor")
    if qb_adjustment:
        hurt = home_name if qb_home_penalty else away_name
        reasons.append(f"Starting QB for {hurt} is Out/Doubtful on the official report")
    if rest_adjustment:
        rested = home_name if rest_adjustment > 0 else away_name
        reasons.append(f"Rest edge favors {rested}")
    if weather_factor < 1.0:
        reasons.append(f"Weather trims the scoring projection ({weather_factor:.0%} of baseline)")

    sample_games = int(min(home_games, 8) + min(away_games, 8))
    return NflGameProjection(
        margin_home=margin_home,
        total=total,
        internal_margin=internal_margin,
        internal_total=internal_total,
        anchor=anchor,
        sample_games=sample_games,
        features=features,
        reasons=reasons,
        feature_groups=feature_groups,
    )


def _line_weight(book_count: int) -> float:
    if book_count >= 3:
        return 0.70
    if book_count >= 1:
        return 0.50
    return 0.0


def _confidence(
    base: float,
    projection: NflGameProjection,
    probability_yes: float,
) -> float:
    anchor = projection.anchor
    anchor_bonus = 0.0
    if anchor is not None:
        anchor_bonus = 0.12 if anchor.book_count >= 3 or anchor.spread_book_count >= 3 else 0.06
    return clamp(
        base
        + min(projection.sample_games, 16) / 50.0
        + anchor_bonus
        + abs(probability_yes - 0.5) * 0.3,
        0.2,
        0.92,
    )


def score_nfl_team_winner(
    db: Session,
    event: Event,
    left: EventParticipant,
    right: EventParticipant,
) -> tuple[float, float, list[str], dict[str, Any], dict[str, FeatureGroupSnapshot]]:
    """Return LEFT participant's win probability (kernel convention)."""
    projection = build_nfl_game_projection(db, event, left, right)
    p_internal_home = nfl_win_probability(projection.internal_margin)
    anchor = projection.anchor
    p_home, consensus_weight = blend_probability(
        p_internal_home,
        anchor.win_prob_home if anchor else None,
        anchor.book_count if anchor else 0,
    )
    # No h2h consensus but a spread anchor → derive the location from
    # the blended margin instead of running pure-internal.
    if consensus_weight == 0.0 and anchor is not None and anchor.spread_home is not None:
        p_home = nfl_win_probability(projection.margin_home)
        consensus_weight = _line_weight(anchor.spread_book_count)
    probability_home = clamp(p_home, 0.05, 0.95)

    home, _away = _home_away(left, right)
    probability_left = (
        probability_home if left.participant_id == home.participant_id else round(1 - probability_home, 4)
    )
    features = projection.features
    features["nfl_consensus_blend_weight"] = consensus_weight
    features["nfl_internal_win_prob_home"] = round(p_internal_home, 4)
    features["sample_size"] = projection.sample_games
    confidence = _confidence(0.30, projection, probability_left)
    if projection.anchor is None:
        confidence = clamp(confidence - 0.05, 0.2, 0.92)
    return probability_left, confidence, projection.reasons, features, projection.feature_groups


def _spread_subject_entry(
    event: Event, metadata: dict[str, Any]
) -> EventParticipant | None:
    """Resolve which participant a spread market's subject names.

    Token-matches ``copilot_subject_name`` ("Philadelphia wins by over
    3.5 points" → "Philadelphia") against the event participants —
    the same scoring ``_market_yes_entry`` uses, minus its
    winner-kind gate that spreads can't pass."""
    subject = str(metadata.get("copilot_subject_name") or "").strip()
    if not subject:
        return None
    subject_tokens = alias_tokens(subject)
    best_entry: EventParticipant | None = None
    best_score = 0.0
    for entry in event.participants:
        participant = entry.participant
        if participant is None:
            continue
        score = _token_score(
            subject_tokens, alias_tokens(participant.display_name, participant.short_name)
        )
        if score > best_score:
            best_score = score
            best_entry = entry
    if best_score < 0.15:
        return None
    return best_entry


def score_nfl_game_line(
    db: Session,
    event: Event,
    market: Market,
    left: EventParticipant,
    right: EventParticipant,
) -> tuple[float, float, list[str], dict[str, Any], dict[str, FeatureGroupSnapshot]] | None:
    metadata = _market_metadata(market)
    market_kind = str(metadata.get("copilot_market_kind") or "")
    threshold = float(metadata.get("copilot_threshold") or 0.0)
    direction = str(metadata.get("copilot_direction") or "over").lower()
    if market_kind not in {"spread", "total"}:
        return None

    projection = build_nfl_game_projection(db, event, left, right)
    home, _away = _home_away(left, right)
    features = projection.features
    reasons = list(projection.reasons)

    if market_kind == "spread":
        # NOTE: ``_market_yes_entry`` deliberately isn't used here — its
        # kind gate only admits winner markets (which leaves the legacy
        # ``_score_game_line`` spread branch dead for every sport; flagged
        # separately). Spread metadata carries the subject name directly.
        yes_entry = _spread_subject_entry(event, metadata)
        if yes_entry is None:
            return None
        subject_is_home = yes_entry.participant_id == home.participant_id
        # The symmetrized grid makes the away side exactly the mirrored
        # lookup: P(away margin > t | mu) == P(home margin > t | -mu).
        signed_mu = projection.margin_home if subject_is_home else -projection.margin_home
        probability_yes = clamp(
            nfl_margin_yes_probability(signed_mu, threshold), 0.05, 0.95
        )
        subject_name = (
            yes_entry.participant.display_name if yes_entry.participant else "Home"
        )
        reasons.append(
            f"Projected margin for {subject_name}: {signed_mu:+.1f} vs line {threshold:g} "
            "(key-number-aware pricing)"
        )
        features["nfl_spread_signed_mu"] = round(signed_mu, 3)
        features["line_threshold"] = threshold
    else:
        over_probability = nfl_total_yes_probability(
            projection.total, threshold, direction="over"
        )
        probability_yes = clamp(
            over_probability if direction == "over" else 1.0 - over_probability,
            0.05,
            0.95,
        )
        reasons.append(
            f"Projected total: {projection.total:.1f} vs line {direction.title()} {threshold:g}"
        )
        features["expected_total"] = round(projection.total, 3)
        features["line_threshold"] = threshold

    features["sample_size"] = projection.sample_games
    confidence = _confidence(0.28, projection, probability_yes)
    if projection.anchor is None:
        confidence = clamp(confidence - 0.05, 0.2, 0.92)
    return probability_yes, confidence, reasons, features, projection.feature_groups
