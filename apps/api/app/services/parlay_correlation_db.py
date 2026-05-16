"""Smarter #8 (phase 2) — DB query layer that feeds empirical
parlay-correlation estimates from settled history.

Phase 1 (PR #125) shipped the math (``parlay_correlation.py``).
Phase 2 wires the DB so an operator (or a future scoring consumer)
can ask:

    "What's the empirical correlation between same-subject parlay
    legs over the last 90 days, computed from settled history?"

and get back a ``dict[pair_type, PairCorrelation | None]`` keyed by
the same pair-type strings the live parlay combiner uses
(``shared_subject``, ``same_team``, ``shared_opponent``). Phase 3
(separate PR) will modify ``parlays._correlation_adjusted_joint_probability``
to read these empirical estimates and blend them with the
theoretical priors via ``blend_theoretical_with_empirical``.

## Why phi (2x2 contingency) instead of Pearson on outcome arrays

``ParlayPredictionLeg`` outcomes are binary (won / lost). Aggregating
across many parlays produces a 2x2 contingency table per pair-type
(both legs won, only A, only B, neither). Phi computed from the
aggregated table is the closed-form equivalent of Pearson on the
{0,1} encoding but doesn't require materializing one row per leg
pair into a Python list — important because the cross-product is
``O(legs²)`` per parlay and we want to scan months of history.

## Leg-outcome derivation

A parlay won iff every leg won. So if a parlay's
``prediction_outcome == "won"`` we know every leg won. If "lost",
at least one leg lost — but we don't know WHICH ones from the
parlay row alone. Hence the contingency table only counts pairs
where BOTH legs have a ``source_prediction`` with a settled
outcome ("won"/"lost"); side-aware mapping (matching the readiness
module's ``_did_yes_happen``) converts each leg's outcome to its
own "this leg's pick was correct" boolean.

## What's deferred to phase 3

- Caching layer (compute the correlation map ahead of time and
  store in ``OperatorSetting``; refresh on a schedule)
- ``parlays._correlation_adjusted_joint_probability`` consumer
  wiring that blends empirical with theoretical priors
- Per-sport / per-leg-count breakdowns (today's NBA same-team
  correlation may differ meaningfully from MLB's; phase 3 can
  introduce richer keying)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import (
    Event,
    EventParticipant,
    ParlayPrediction,
    ParlayPredictionLeg,
)
from app.services.parlay_correlation import (
    DEFAULT_MIN_SAMPLE,
    PairCorrelation,
    phi_coefficient_from_contingency,
)

__all__ = [
    "PAIR_SHARED_SUBJECT",
    "PAIR_SAME_TEAM",
    "PAIR_SHARED_OPPONENT",
    "PAIR_TYPES",
    "DEFAULT_LOOKBACK_DAYS",
    "iter_settled_parlay_leg_pairs",
    "aggregate_pair_contingency",
    "compute_empirical_pair_correlations",
]

PAIR_SHARED_SUBJECT = "shared_subject"
PAIR_SAME_TEAM = "same_team"
PAIR_SHARED_OPPONENT = "shared_opponent"
PAIR_TYPES: tuple[str, ...] = (PAIR_SHARED_SUBJECT, PAIR_SAME_TEAM, PAIR_SHARED_OPPONENT)

# 90 days is short enough to capture recent regime shifts (NBA
# season transitions, MLB pitcher rotations) but long enough to
# accumulate the row counts needed to clear ``DEFAULT_MIN_SAMPLE``.
DEFAULT_LOOKBACK_DAYS = 90

_CONTINGENCY_CELLS = ("both", "only_a", "only_b", "neither")


# -- Leg key helpers (mirror ``parlays._candidate_*_key``) -------------


def _leg_team_key(leg: ParlayPredictionLeg) -> str | None:
    raw = (leg.subject_team or "").upper()
    return raw or None


def _leg_subject_key(leg: ParlayPredictionLeg) -> str | None:
    raw = (leg.subject_name or "").strip().lower()
    return raw or None


def _leg_opponent_key(leg: ParlayPredictionLeg) -> str | None:
    """Return the OTHER team in the leg's event (best-effort).

    ``ParlayPredictionLeg.subject_team`` identifies the leg's own
    team; the opponent is whichever participant of ``leg.event``
    isn't that team. Returns ``None`` when the event/participants
    aren't loaded — the caller is responsible for eager-loading via
    ``selectinload``/``joinedload`` for the tight loop in
    ``iter_settled_parlay_leg_pairs``.
    """
    team_key = _leg_team_key(leg)
    if not team_key or leg.event is None:
        return None
    for participant in leg.event.participants:
        if participant.participant is None:
            continue
        display = str(participant.participant.display_name or "").upper()
        short = str(participant.participant.short_name or "").upper()
        if team_key and team_key in {display, short}:
            continue
        return short or display or None
    return None


def _classify_pair(
    leg_a: ParlayPredictionLeg, leg_b: ParlayPredictionLeg
) -> str | None:
    """Match the live ``parlays._count_correlation_pairs`` logic:
    a pair is classified as (in priority order) ``shared_subject``,
    ``same_team``, or ``shared_opponent``. ``None`` when no key
    matches.

    Priority order is important when one pair satisfies multiple
    relations (two same-team props on the same player are both
    ``shared_subject`` AND ``same_team`` — we want the strongest
    signal, which is shared subject). The live combiner sums weights
    independently rather than picking one; the contingency table
    can only assign each pair to one cell, so we pick the strongest
    here.
    """
    subject_a = _leg_subject_key(leg_a)
    subject_b = _leg_subject_key(leg_b)
    if subject_a and subject_a == subject_b:
        return PAIR_SHARED_SUBJECT
    team_a = _leg_team_key(leg_a)
    team_b = _leg_team_key(leg_b)
    if team_a and team_a == team_b:
        return PAIR_SAME_TEAM
    opponent_a = _leg_opponent_key(leg_a)
    opponent_b = _leg_opponent_key(leg_b)
    if opponent_a and opponent_a == opponent_b:
        return PAIR_SHARED_OPPONENT
    return None


# -- Per-leg outcome derivation ----------------------------------------


def _leg_won(leg: ParlayPredictionLeg) -> bool | None:
    """Return whether this leg's pick was correct.

    Source: the leg's ``source_prediction.prediction_outcome``
    mapped through the leg's own ``side``:

    YES side: 'won' → True (YES happened, pick was right)
              'lost' → False
    NO side:  'won' → True (NO happened, pick was right)
              'lost' → False

    push / cancelled / pending / unresolved / missing source →
    None (caller drops the pair).

    Note the symmetry: ``prediction_outcome == 'won'`` means "the
    pick was right" regardless of side. The side inversion lives in
    the readiness / walk-forward modules where the metric is over
    the YES axis (P(YES) → did YES happen?). For correlation between
    leg picks the right axis is "did each leg's pick land?" — which
    is just ``outcome == 'won'`` directly.
    """
    if leg.source_prediction is None:
        return None
    outcome = (leg.source_prediction.prediction_outcome or "").lower()
    if outcome == "won":
        return True
    if outcome == "lost":
        return False
    return None


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# -- Public iteration ---------------------------------------------------


def iter_settled_parlay_leg_pairs(
    db: Session,
    *,
    end_date: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Iterator[tuple[int, str, bool, bool]]:
    """Yield ``(parlay_id, pair_type, leg_a_won, leg_b_won)`` for
    every classifiable leg pair in every parlay whose
    ``captured_at`` falls in ``[end_date - lookback_days, end_date]``
    AND every leg has a ``source_prediction`` with a settled
    outcome.

    Pairs are classified by ``_classify_pair`` (shared_subject >
    same_team > shared_opponent priority); pairs that match no
    relation are silently skipped — they're already
    "approximately independent" by construction and don't inform
    any of the parlay-correlation pricing slots.
    """
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be > 0, got {lookback_days}")
    end_at = _coerce_utc(end_date) if end_date is not None else datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=lookback_days)

    stmt = (
        select(ParlayPrediction)
        .where(
            ParlayPrediction.captured_at >= start_at,
            ParlayPrediction.captured_at <= end_at,
        )
        .options(
            joinedload(ParlayPrediction.legs)
            .joinedload(ParlayPredictionLeg.source_prediction),
            joinedload(ParlayPrediction.legs)
            .joinedload(ParlayPredictionLeg.event)
            .selectinload(Event.participants)
            .joinedload(EventParticipant.participant),
        )
    )
    parlays = db.scalars(stmt).unique().all()

    for parlay in parlays:
        legs = list(parlay.legs)
        # Must have outcome for every leg — partial settlement (some
        # legs settled, others pending) corrupts the contingency
        # table by counting wins without their potential losses.
        leg_outcomes = [_leg_won(leg) for leg in legs]
        if any(outcome is None for outcome in leg_outcomes):
            continue
        for left_idx in range(len(legs)):
            for right_idx in range(left_idx + 1, len(legs)):
                pair_type = _classify_pair(legs[left_idx], legs[right_idx])
                if pair_type is None:
                    continue
                yield (
                    int(parlay.id),
                    pair_type,
                    bool(leg_outcomes[left_idx]),
                    bool(leg_outcomes[right_idx]),
                )


# -- Contingency aggregation -------------------------------------------


def aggregate_pair_contingency(
    db: Session,
    *,
    end_date: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, dict[str, int]]:
    """Aggregate ``iter_settled_parlay_leg_pairs`` into 2x2
    contingency tables per pair type.

    Returns a dict keyed by pair type; each value is a dict with
    keys ``both`` (both legs won), ``only_a`` (left won, right
    lost), ``only_b`` (left lost, right won), ``neither`` (both
    lost). Missing pair types are absent from the outer dict
    (callers should check ``.get(pair_type)`` rather than indexing
    blindly).

    Cell semantics: ``only_a`` / ``only_b`` are NOT direction-
    sensitive in the parlay-correlation context — phi treats them
    symmetrically (both contribute to the disagreement count).
    Keeping them separate in the dict makes audit easier (a
    radically asymmetric ``only_a`` vs ``only_b`` count for a
    pair-type that's supposed to be symmetric is a sign the
    classification logic has a bug).
    """
    contingency: dict[str, dict[str, int]] = {}
    for _parlay_id, pair_type, a_won, b_won in iter_settled_parlay_leg_pairs(
        db, end_date=end_date, lookback_days=lookback_days,
    ):
        bucket = contingency.setdefault(pair_type, {cell: 0 for cell in _CONTINGENCY_CELLS})
        if a_won and b_won:
            bucket["both"] += 1
        elif a_won and not b_won:
            bucket["only_a"] += 1
        elif not a_won and b_won:
            bucket["only_b"] += 1
        else:
            bucket["neither"] += 1
    return contingency


# -- Public correlation map --------------------------------------------


def compute_empirical_pair_correlations(
    db: Session,
    *,
    end_date: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> dict[str, PairCorrelation | None]:
    """Top-level helper: aggregate contingency + compute phi per
    pair type.

    Returns a dict keyed by every entry in ``PAIR_TYPES``. The
    value is ``None`` when the contingency table for that pair type
    has fewer than ``min_sample`` total observations — at that
    sample size the phi estimate is dominated by noise and the
    consumer should fall back to the theoretical prior. Above
    threshold, the value is ``PairCorrelation(coefficient,
    sample_size)``.

    Always returns a key for every pair type in ``PAIR_TYPES`` so
    callers can iterate without ``.get()`` defensive defaults.
    """
    if min_sample < 0:
        raise ValueError(f"min_sample must be >= 0, got {min_sample}")
    contingency = aggregate_pair_contingency(
        db, end_date=end_date, lookback_days=lookback_days,
    )
    out: dict[str, PairCorrelation | None] = {}
    for pair_type in PAIR_TYPES:
        cells = contingency.get(pair_type)
        if cells is None:
            out[pair_type] = None
            continue
        sample_size = sum(cells.values())
        if sample_size < min_sample:
            out[pair_type] = None
            continue
        coefficient = phi_coefficient_from_contingency(
            both=cells["both"],
            only_a=cells["only_a"],
            only_b=cells["only_b"],
            neither=cells["neither"],
        )
        out[pair_type] = PairCorrelation(
            coefficient=coefficient, sample_size=sample_size,
        )
    return out
