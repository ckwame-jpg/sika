"""Tests for Smarter #8 phase 2 — DB query layer for empirical
parlay-correlation estimation.

Phase 1 tests pinned the math (``parlay_correlation.py``); these
exercise the DB iteration / pair classification / contingency
aggregation. Load-bearing test:
``test_compute_returns_phi_matching_seeded_distribution`` — seed a
distribution with a known correlation, run the full pipeline, and
verify phi comes out within tolerance of the analytic value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Event,
    EventParticipant,
    Market,
    ParlayPrediction,
    ParlayPredictionLeg,
    Participant,
    Prediction,
)
from app.services.parlay_correlation import PairCorrelation
from app.services.parlay_correlation_db import (
    PAIR_SAME_TEAM,
    PAIR_SHARED_OPPONENT,
    PAIR_SHARED_SUBJECT,
    PAIR_TYPES,
    _classify_pair,
    _leg_won,
    aggregate_pair_contingency,
    compute_empirical_pair_correlations,
    iter_settled_parlay_leg_pairs,
)

_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


# -- Seed helpers ------------------------------------------------------


_event_counter = {"n": 0}


def _seed_event(db_session, *, home_name: str, away_name: str) -> Event:
    _event_counter["n"] += 1
    n = _event_counter["n"]
    home = Participant(
        external_id=f"home-{n}", sport_key="NBA",
        display_name=home_name, short_name=home_name, participant_type="team",
    )
    away = Participant(
        external_id=f"away-{n}", sport_key="NBA",
        display_name=away_name, short_name=away_name, participant_type="team",
    )
    db_session.add_all([home, away])
    db_session.flush()
    event = Event(
        external_id=f"evt-{n}",
        sport_key="NBA",
        name=f"{away_name} at {home_name}",
        starts_at=_NOW - timedelta(days=1),
        status="completed",
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
    ])
    db_session.flush()
    return event


_market_counter = {"n": 0}


def _seed_market(db_session, *, sport_key: str = "NBA") -> Market:
    _market_counter["n"] += 1
    market = Market(
        ticker=f"NBA-T{_market_counter['n']}",
        sport_key=sport_key,
        title="market",
        status="open",
        raw_data={},
    )
    db_session.add(market)
    db_session.flush()
    return market


def _seed_source_prediction(
    db_session,
    *,
    market: Market,
    side: str,
    outcome: str,
) -> Prediction:
    """Seed a Prediction with the given outcome, used as a leg's
    source_prediction."""
    pred = Prediction(
        market_id=market.id,
        ticker=market.ticker,
        sport_key=market.sport_key,
        market_title="leg",
        market_family=None,
        side=side,
        action="buy",
        suggested_price=0.5,
        edge=0.05,
        confidence=0.6,
        rationale="x",
        prediction_outcome=outcome,
        settlement_status="settled" if outcome in ("won", "lost") else "pending",
        captured_at=_NOW - timedelta(days=10),
    )
    db_session.add(pred)
    db_session.flush()
    return pred


def _seed_parlay(
    db_session,
    *,
    captured_at: datetime,
    legs_data: list[dict],
    parlay_outcome: str = "lost",
) -> ParlayPrediction:
    """Seed a parlay with N legs.

    ``legs_data`` is a list of dicts with keys:
    ``subject_name``, ``subject_team``, ``side``, ``outcome``,
    ``event``. The leg's source_prediction is created with the
    given outcome.
    """
    parlay = ParlayPrediction(
        leg_count=len(legs_data),
        sport_scope="NBA",
        participating_sports=["NBA"],
        combined_market_price=0.40,
        combined_model_probability=0.30,
        american_odds="+150",
        edge=0.05,
        confidence=0.60,
        invalidation="x",
        rationale="x",
        prediction_outcome=parlay_outcome,
        settlement_status="settled" if parlay_outcome in ("won", "lost") else "pending",
        captured_at=captured_at,
    )
    db_session.add(parlay)
    db_session.flush()
    for index, leg_spec in enumerate(legs_data):
        market = _seed_market(db_session)
        source = None
        if leg_spec.get("outcome") is not None:
            source = _seed_source_prediction(
                db_session, market=market, side=leg_spec["side"],
                outcome=leg_spec["outcome"],
            )
        leg = ParlayPredictionLeg(
            parlay_prediction_id=parlay.id,
            leg_index=index,
            source_prediction_id=source.id if source else None,
            event_id=leg_spec["event"].id if leg_spec.get("event") else None,
            market_id=market.id,
            ticker=market.ticker,
            sport_key="NBA",
            event_name=leg_spec["event"].name if leg_spec.get("event") else None,
            market_title="leg",
            subject_name=leg_spec.get("subject_name"),
            subject_team=leg_spec.get("subject_team"),
            side=leg_spec["side"],
            action="buy",
            suggested_price=0.5,
            edge=0.05,
            confidence=0.6,
        )
        db_session.add(leg)
    db_session.flush()
    db_session.commit()
    return parlay


# -- Pair classification -----------------------------------------------


def test_classify_pair_shared_subject_wins_priority(db_session) -> None:
    """A pair that shares both subject AND team should classify as
    shared_subject (the strongest signal). Without explicit
    priority we'd double-count or pick arbitrarily."""
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    parlay = _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
        ],
    )
    legs = list(parlay.legs)
    assert _classify_pair(legs[0], legs[1]) == PAIR_SHARED_SUBJECT


def test_classify_pair_same_team_when_no_shared_subject(db_session) -> None:
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    parlay = _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
            {
                "subject_name": "Anthony Davis", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
        ],
    )
    legs = list(parlay.legs)
    assert _classify_pair(legs[0], legs[1]) == PAIR_SAME_TEAM


def test_classify_pair_shared_opponent_when_different_teams(db_session) -> None:
    """Two different teams both facing the same opponent (different
    games but same opponent) → shared_opponent. Requires loaded
    event participants."""
    # Game 1: LAL vs DAL
    event_1 = _seed_event(db_session, home_name="LAL", away_name="DAL")
    # Game 2: BOS vs DAL (different game, same opponent)
    event_2 = _seed_event(db_session, home_name="BOS", away_name="DAL")
    parlay = _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event_1,
            },
            {
                "subject_name": "Jayson Tatum", "subject_team": "BOS",
                "side": "yes", "outcome": "won", "event": event_2,
            },
        ],
    )
    legs = list(parlay.legs)
    assert _classify_pair(legs[0], legs[1]) == PAIR_SHARED_OPPONENT


def test_classify_pair_returns_none_when_no_relation(db_session) -> None:
    """Two legs with different subjects, teams, and opponents →
    None (the pair is approximately independent and doesn't inform
    correlation pricing)."""
    event_1 = _seed_event(db_session, home_name="LAL", away_name="DAL")
    event_2 = _seed_event(db_session, home_name="BOS", away_name="MIA")
    parlay = _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event_1,
            },
            {
                "subject_name": "Jayson Tatum", "subject_team": "BOS",
                "side": "yes", "outcome": "won", "event": event_2,
            },
        ],
    )
    legs = list(parlay.legs)
    assert _classify_pair(legs[0], legs[1]) is None


# -- Per-leg outcome derivation ----------------------------------------


def test_leg_won_returns_true_when_source_won(db_session) -> None:
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    parlay = _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
        ],
    )
    leg = list(parlay.legs)[0]
    assert _leg_won(leg) is True


def test_leg_won_returns_false_when_source_lost(db_session) -> None:
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    parlay = _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "lost", "event": event,
            },
        ],
    )
    leg = list(parlay.legs)[0]
    assert _leg_won(leg) is False


def test_leg_won_returns_none_when_no_source(db_session) -> None:
    """Heuristic-only parlays don't have source_prediction set; the
    correlation pipeline drops these legs."""
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    parlay = _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": None, "event": event,
            },
        ],
    )
    leg = list(parlay.legs)[0]
    assert _leg_won(leg) is None


def test_leg_won_returns_none_for_undecided_source(db_session) -> None:
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    parlay = _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "push", "event": event,
            },
        ],
    )
    leg = list(parlay.legs)[0]
    assert _leg_won(leg) is None


# -- Iteration ---------------------------------------------------------


def test_iter_yields_classified_pairs(db_session) -> None:
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "lost", "event": event,
            },
        ],
    )
    pairs = list(iter_settled_parlay_leg_pairs(db_session, end_date=_NOW))
    assert len(pairs) == 1
    parlay_id, pair_type, a_won, b_won = pairs[0]
    assert pair_type == PAIR_SHARED_SUBJECT
    assert (a_won, b_won) == (True, False)


def test_iter_skips_parlays_with_unsettled_legs(db_session) -> None:
    """If even one leg lacks an outcome the entire parlay is
    skipped — partial settlement corrupts the contingency table."""
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
            {
                "subject_name": "Anthony Davis", "subject_team": "LAL",
                "side": "yes", "outcome": None, "event": event,  # no source pred
            },
        ],
    )
    pairs = list(iter_settled_parlay_leg_pairs(db_session, end_date=_NOW))
    assert pairs == []


def test_iter_respects_lookback_window(db_session) -> None:
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=200),  # outside default 90d
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
        ],
    )
    _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),  # inside
        legs_data=[
            {
                "subject_name": "Jayson Tatum", "subject_team": "BOS",
                "side": "yes", "outcome": "won", "event": event,
            },
            {
                "subject_name": "Jayson Tatum", "subject_team": "BOS",
                "side": "yes", "outcome": "won", "event": event,
            },
        ],
    )
    pairs = list(iter_settled_parlay_leg_pairs(db_session, end_date=_NOW))
    assert len(pairs) == 1


def test_iter_skips_unclassifiable_pairs(db_session) -> None:
    """A pair with no shared subject / team / opponent is still
    correlated weakly via game-state effects, but the live
    combiner doesn't price it — so the contingency table doesn't
    count it either."""
    event_1 = _seed_event(db_session, home_name="LAL", away_name="DAL")
    event_2 = _seed_event(db_session, home_name="BOS", away_name="MIA")
    _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=10),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event_1,
            },
            {
                "subject_name": "Jayson Tatum", "subject_team": "BOS",
                "side": "yes", "outcome": "won", "event": event_2,
            },
        ],
    )
    pairs = list(iter_settled_parlay_leg_pairs(db_session, end_date=_NOW))
    assert pairs == []


def test_iter_rejects_non_positive_lookback(db_session) -> None:
    with pytest.raises(ValueError, match="lookback_days"):
        list(iter_settled_parlay_leg_pairs(
            db_session, end_date=_NOW, lookback_days=0,
        ))


# -- Contingency aggregation -------------------------------------------


def test_aggregate_buckets_pairs_into_2x2_cells(db_session) -> None:
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    # 3 parlays with shared-subject pairs:
    #   (won, won) → both
    #   (won, lost) → only_a
    #   (lost, lost) → neither
    for left_outcome, right_outcome in [("won", "won"), ("won", "lost"), ("lost", "lost")]:
        _seed_parlay(
            db_session,
            captured_at=_NOW - timedelta(days=5),
            legs_data=[
                {
                    "subject_name": "LeBron James", "subject_team": "LAL",
                    "side": "yes", "outcome": left_outcome, "event": event,
                },
                {
                    "subject_name": "LeBron James", "subject_team": "LAL",
                    "side": "yes", "outcome": right_outcome, "event": event,
                },
            ],
        )
    contingency = aggregate_pair_contingency(db_session, end_date=_NOW)
    cells = contingency[PAIR_SHARED_SUBJECT]
    assert cells["both"] == 1
    assert cells["only_a"] == 1
    assert cells["only_b"] == 0
    assert cells["neither"] == 1


def test_aggregate_omits_empty_pair_types(db_session) -> None:
    """If no pairs of a type are seen, that pair type is absent
    from the dict (callers use .get to default safely)."""
    contingency = aggregate_pair_contingency(db_session, end_date=_NOW)
    assert contingency == {}


# -- compute_empirical_pair_correlations -------------------------------


def test_compute_returns_none_below_min_sample(db_session) -> None:
    """A pair type with only a few observations doesn't get a
    correlation estimate — caller should fall back to the
    theoretical prior. ``None`` is the explicit signal."""
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    _seed_parlay(
        db_session,
        captured_at=_NOW - timedelta(days=5),
        legs_data=[
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
            {
                "subject_name": "LeBron James", "subject_team": "LAL",
                "side": "yes", "outcome": "won", "event": event,
            },
        ],
    )
    out = compute_empirical_pair_correlations(db_session, end_date=_NOW)
    assert out[PAIR_SHARED_SUBJECT] is None  # only 1 pair, default min=30


def test_compute_returns_pair_correlation_above_threshold(db_session) -> None:
    """Above ``min_sample`` the result is a populated
    ``PairCorrelation``."""
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    # 30 perfectly-correlated shared-subject pairs (both won).
    for _ in range(30):
        _seed_parlay(
            db_session,
            captured_at=_NOW - timedelta(days=5),
            legs_data=[
                {
                    "subject_name": "LeBron James", "subject_team": "LAL",
                    "side": "yes", "outcome": "won", "event": event,
                },
                {
                    "subject_name": "LeBron James", "subject_team": "LAL",
                    "side": "yes", "outcome": "won", "event": event,
                },
            ],
        )
    out = compute_empirical_pair_correlations(db_session, end_date=_NOW)
    result = out[PAIR_SHARED_SUBJECT]
    assert isinstance(result, PairCorrelation)
    assert result.sample_size == 30
    # All-same outcome → zero variance → phi returns 0 ("no signal"),
    # not 1.0. This is the correct behavior — see
    # ``test_phi_constant_marginal_returns_zero`` in the phase-1
    # tests.
    assert result.coefficient == 0.0


def test_compute_returns_phi_matching_seeded_distribution(db_session) -> None:
    """Load-bearing: seed a 2x2 distribution with known phi and
    verify the pipeline recovers it.

    Setup: 100 shared-subject pairs with the following outcome
    distribution (perfectly positively correlated, but with one
    "off-diagonal" cell to give phi a non-degenerate value):

        both=40, only_a=10, only_b=10, neither=40

    Hand-computed phi = (40*40 - 10*10) / sqrt(50*50*50*50) = 1500/2500 = 0.6.
    """
    event = _seed_event(db_session, home_name="LAL", away_name="BOS")
    distribution = [("won", "won")] * 40 + [("won", "lost")] * 10 + \
                   [("lost", "won")] * 10 + [("lost", "lost")] * 40
    for left_outcome, right_outcome in distribution:
        _seed_parlay(
            db_session,
            captured_at=_NOW - timedelta(days=5),
            legs_data=[
                {
                    "subject_name": "LeBron James", "subject_team": "LAL",
                    "side": "yes", "outcome": left_outcome, "event": event,
                },
                {
                    "subject_name": "LeBron James", "subject_team": "LAL",
                    "side": "yes", "outcome": right_outcome, "event": event,
                },
            ],
        )
    out = compute_empirical_pair_correlations(db_session, end_date=_NOW)
    result = out[PAIR_SHARED_SUBJECT]
    assert isinstance(result, PairCorrelation)
    assert result.sample_size == 100
    assert result.coefficient == pytest.approx(0.6, abs=1e-9)


def test_compute_always_returns_key_for_every_pair_type(db_session) -> None:
    """Even with zero history the result is fully populated so
    callers don't need defensive ``.get(pair_type, None)``."""
    out = compute_empirical_pair_correlations(db_session, end_date=_NOW)
    assert set(out.keys()) == set(PAIR_TYPES)
    for pair_type in PAIR_TYPES:
        assert out[pair_type] is None


def test_compute_rejects_negative_min_sample(db_session) -> None:
    with pytest.raises(ValueError, match="min_sample"):
        compute_empirical_pair_correlations(
            db_session, end_date=_NOW, min_sample=-1,
        )
