"""Canonical correlation-aware parlay quote math.

Every caller first adapts its own leg representation to :class:`QuoteLeg`,
then uses :func:`quote_joint_probability`.  Pair classification is exclusive:
each unordered pair contributes to the first matching category in
``PAIR_CATEGORY_PRECEDENCE`` and can therefore never receive two correlation
lifts.

The auto generator intentionally still rejects same-event combinations.  The
same-event NFL categories in this module are reachable through operator-built
paper parlays and the tray quote endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import isfinite, prod
from typing import Mapping

from sqlalchemy.orm import Session

from app.services.parlay_correlation import (
    PairCorrelation,
    blend_theoretical_with_empirical,
)

PAIR_CATEGORY_PRECEDENCE: tuple[str, ...] = (
    "shared_subject",
    "qb_receiver_stack",
    "player_team_total",
    "same_team",
    "shared_opponent",
)

_THEORETICAL_PAIR_WEIGHTS: Mapping[str, float] = {
    "shared_subject": 0.7,
    "same_team": 0.3,
    "shared_opponent": 0.2,
}

_SPORT_PAIR_WEIGHT_OVERRIDES: Mapping[str, Mapping[str, float]] = {
    "NFL": {
        "shared_subject": 0.75,
        "qb_receiver_stack": 0.55,
        "player_team_total": 0.35,
        "same_team": 0.45,
        "shared_opponent": 0.25,
    },
}

_NFL_PASSING_PROP_STATS = frozenset(
    {"passing_yards", "passing_touchdowns", "completions"}
)
_NFL_RECEIVING_PROP_STATS = frozenset(
    {"receiving_yards", "receptions", "receiving_touchdowns"}
)
_NFL_YARDAGE_PROP_STATS = _NFL_PASSING_PROP_STATS | _NFL_RECEIVING_PROP_STATS | {
    "rushing_yards",
    "rushing_touchdowns",
    "rushing_yards_receiving_yards",
}
_CORRELATION_CAP = 0.85
PARLAY_QUOTE_DECIMAL_PLACES = 6
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QuoteLeg:
    """The normalized fields used by the quote engine.

    ``event_id`` is deliberately part of the contract: QB/receiver and
    player/team-total lifts describe same-game relationships and must not leak
    across two games involving the same team.
    """

    model_probability: float
    sport_key: str | None = None
    event_id: int | None = None
    subject_name: str | None = None
    subject_team: str | None = None
    opponent: str | None = None
    stat_key: str | None = None
    market_family: str | None = None


@dataclass(frozen=True, slots=True)
class JointQuote:
    joint_probability: float
    independent_probability: float
    pair_counts: dict[str, int]
    correlation_factor: float


@dataclass(frozen=True, slots=True)
class ParlayQuote:
    """Canonical persisted/wire quote shared by every parlay workflow."""

    combined_market_price: float
    joint_probability: float
    edge: float
    pair_counts: dict[str, int]
    correlation_factor: float


def classify_quote_pair(left: QuoteLeg, right: QuoteLeg) -> str | None:
    """Return the one correlation category assigned to a leg pair."""

    # Generic names and team abbreviations are only meaningful within one
    # sport.  Missing sport metadata is deliberately conservative: a quote
    # must not infer correlation from an ambiguous ``CLE`` or player name.
    if _shared_pair_sport(left, right) is None:
        return None

    left_subject = _normalized_subject(left.subject_name)
    right_subject = _normalized_subject(right.subject_name)
    if left_subject and left_subject == right_subject:
        return "shared_subject"
    if _is_qb_receiver_stack(left, right):
        return "qb_receiver_stack"
    if _is_player_team_total_pair(left, right):
        return "player_team_total"

    left_team = _normalized_team(left.subject_team)
    right_team = _normalized_team(right.subject_team)
    if left_team and left_team == right_team:
        return "same_team"

    left_opponent = _normalized_team(left.opponent)
    right_opponent = _normalized_team(right.opponent)
    if left_opponent and left_opponent == right_opponent:
        return "shared_opponent"
    return None


def count_correlation_pairs(legs: tuple[QuoteLeg, ...]) -> dict[str, int]:
    """Count mutually exclusive pair categories for a quote."""

    counts = {category: 0 for category in PAIR_CATEGORY_PRECEDENCE}
    for left_index, left in enumerate(legs):
        for right in legs[left_index + 1 :]:
            category = classify_quote_pair(left, right)
            if category is not None:
                counts[category] += 1
    return counts


def pair_weight(
    pair_type: str,
    empirical_correlations: Mapping[str, PairCorrelation | None] | None,
    *,
    sport_scope: str | None = None,
) -> float:
    """Blend a category's theoretical prior with settled-history evidence."""

    sport_overrides = _SPORT_PAIR_WEIGHT_OVERRIDES.get(
        (sport_scope or "").upper(), {}
    )
    theoretical = sport_overrides.get(
        pair_type, _THEORETICAL_PAIR_WEIGHTS.get(pair_type, 0.0)
    )
    if empirical_correlations is None:
        return theoretical
    empirical = empirical_correlations.get(pair_type)
    if empirical is None:
        return theoretical

    # Negative empirical correlation must not turn a positive-correlation
    # quote into a negative weight.  A saturated negative estimate instead
    # removes the lift for that category.
    blendable = PairCorrelation(
        coefficient=max(empirical.coefficient, 0.0),
        sample_size=empirical.sample_size,
    )
    return blend_theoretical_with_empirical(theoretical, blendable)


def load_quote_empirical_correlations(
    db: Session,
) -> dict[str, PairCorrelation | None] | None:
    """Read the stored settled-history input without refreshing or writing.

    Quote endpoints are read-only and can be called on every tray keystroke,
    so this path must never trigger the expensive history scan or mutate an
    ``OperatorSetting`` row.  Refresh belongs to the mutating auto-capture
    lifecycle below.
    """

    try:
        from app.services.parlay_correlation_cache import (
            read_stored_empirical_pair_correlations,
        )

        return read_stored_empirical_pair_correlations(db)
    except Exception:  # noqa: BLE001 - quote availability beats cache reads
        logger.warning(
            "Parlay empirical correlation cache unavailable; using priors.",
            exc_info=True,
        )
        return None


def refresh_quote_empirical_correlations(
    db: Session,
) -> dict[str, PairCorrelation | None] | None:
    """Refresh the empirical snapshot during a mutating capture workflow."""

    try:
        from app.services.parlay_correlation_cache import (
            cached_empirical_pair_correlations,
        )

        return cached_empirical_pair_correlations(db)
    except Exception:  # noqa: BLE001 - priors keep artifact capture available
        logger.warning(
            "Parlay empirical correlation refresh failed; using stored priors.",
            exc_info=True,
        )
        return load_quote_empirical_correlations(db)


def quote_joint_probability(
    legs: tuple[QuoteLeg, ...],
    *,
    empirical_correlations: Mapping[str, PairCorrelation | None] | None = None,
) -> JointQuote:
    """Return the canonical correlation-adjusted joint probability."""

    if not legs:
        raise ValueError("A parlay quote requires at least one leg.")
    probabilities = [leg.model_probability for leg in legs]
    for probability in probabilities:
        if not isfinite(probability) or probability < 0.0 or probability > 1.0:
            raise ValueError(
                "Parlay leg model probabilities must be finite values in [0, 1]."
            )

    independent = float(prod(probabilities))
    pair_counts = {category: 0 for category in PAIR_CATEGORY_PRECEDENCE}
    if len(legs) == 1:
        return JointQuote(
            joint_probability=independent,
            independent_probability=independent,
            pair_counts=pair_counts,
            correlation_factor=0.0,
        )

    total_pairs = len(legs) * (len(legs) - 1) // 2
    weighted_total = 0.0
    for left_index, left in enumerate(legs):
        for right in legs[left_index + 1 :]:
            category = classify_quote_pair(left, right)
            if category is None:
                continue
            pair_counts[category] += 1
            weighted_total += pair_weight(
                category,
                empirical_correlations,
                sport_scope=_shared_pair_sport(left, right),
            )
    weighted = weighted_total / total_pairs
    correlation_factor = min(max(weighted, 0.0), _CORRELATION_CAP)
    min_leg = min(probabilities)
    joint_probability = float(
        independent + correlation_factor * (min_leg - independent)
    )
    return JointQuote(
        joint_probability=joint_probability,
        independent_probability=independent,
        pair_counts=pair_counts,
        correlation_factor=correlation_factor,
    )


def quote_parlay(
    legs: tuple[QuoteLeg, ...],
    market_prices: tuple[float, ...],
    *,
    empirical_correlations: Mapping[str, PairCorrelation | None] | None = None,
) -> ParlayQuote:
    """Return the canonical persisted/wire quote at one shared precision."""

    if len(market_prices) != len(legs):
        raise ValueError("Parlay quote market prices must match the leg count.")
    for market_price in market_prices:
        if (
            not isfinite(market_price)
            or market_price <= 0.0
            or market_price > 1.0
        ):
            raise ValueError(
                "Parlay leg market prices must be finite values in (0, 1]."
            )

    joint_quote = quote_joint_probability(
        legs,
        empirical_correlations=empirical_correlations,
    )
    combined_market_price = round(
        prod(market_prices), PARLAY_QUOTE_DECIMAL_PLACES
    )
    if combined_market_price <= 0.0:
        raise ValueError(
            "Combined parlay price is too small to price; the legs are too long-shot."
        )
    joint_probability = round(
        joint_quote.joint_probability, PARLAY_QUOTE_DECIMAL_PLACES
    )
    return ParlayQuote(
        combined_market_price=combined_market_price,
        joint_probability=joint_probability,
        edge=round(
            joint_probability - combined_market_price,
            PARLAY_QUOTE_DECIMAL_PLACES,
        ),
        pair_counts=joint_quote.pair_counts,
        correlation_factor=round(
            joint_quote.correlation_factor, PARLAY_QUOTE_DECIMAL_PLACES
        ),
    )


def _is_qb_receiver_stack(left: QuoteLeg, right: QuoteLeg) -> bool:
    if not _same_event(left, right) or not _both_nfl(left, right):
        return False
    left_team = _normalized_team(left.subject_team)
    right_team = _normalized_team(right.subject_team)
    if not left_team or left_team != right_team:
        return False
    left_subject = _normalized_subject(left.subject_name)
    right_subject = _normalized_subject(right.subject_name)
    if not left_subject or not right_subject or left_subject == right_subject:
        return False
    left_stat = _normalized_stat(left.stat_key)
    right_stat = _normalized_stat(right.stat_key)
    return (
        left_stat in _NFL_PASSING_PROP_STATS
        and right_stat in _NFL_RECEIVING_PROP_STATS
    ) or (
        right_stat in _NFL_PASSING_PROP_STATS
        and left_stat in _NFL_RECEIVING_PROP_STATS
    )


def _is_player_team_total_pair(left: QuoteLeg, right: QuoteLeg) -> bool:
    if not _same_event(left, right) or not _both_nfl(left, right):
        return False
    left_family = _normalized_family(left.market_family)
    right_family = _normalized_family(right.market_family)
    if left_family == "player_prop" and right_family in {"winner", "game_line"}:
        prop_leg = left
    elif right_family == "player_prop" and left_family in {"winner", "game_line"}:
        prop_leg = right
    else:
        return False
    return _normalized_stat(prop_leg.stat_key) in _NFL_YARDAGE_PROP_STATS


def _same_event(left: QuoteLeg, right: QuoteLeg) -> bool:
    return left.event_id is not None and left.event_id == right.event_id


def _both_nfl(left: QuoteLeg, right: QuoteLeg) -> bool:
    return _normalized_sport(left.sport_key) == "NFL" and _normalized_sport(
        right.sport_key
    ) == "NFL"


def _shared_pair_sport(left: QuoteLeg, right: QuoteLeg) -> str | None:
    left_sport = _normalized_sport(left.sport_key)
    right_sport = _normalized_sport(right.sport_key)
    if not left_sport or left_sport != right_sport:
        return None
    return left_sport


def _normalized_subject(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalized_team(value: str | None) -> str:
    return (value or "").strip().upper()


def _normalized_sport(value: str | None) -> str:
    return (value or "").strip().upper()


def _normalized_stat(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalized_family(value: str | None) -> str:
    return (value or "").strip().lower()


__all__ = [
    "JointQuote",
    "PAIR_CATEGORY_PRECEDENCE",
    "PARLAY_QUOTE_DECIMAL_PLACES",
    "ParlayQuote",
    "QuoteLeg",
    "classify_quote_pair",
    "count_correlation_pairs",
    "load_quote_empirical_correlations",
    "pair_weight",
    "quote_parlay",
    "quote_joint_probability",
    "refresh_quote_empirical_correlations",
]
