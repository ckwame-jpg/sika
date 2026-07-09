"""Smarter NFL PR 10b — NFL parlay correlation: stack categories,
sport-aware pair weights, and the reliability penalties."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.parlays import (
    _correlation_adjusted_joint_probability,
    _count_correlation_pairs,
    _pair_weight,
    _parlay_diagnostics_for_combo,
)


def _candidate(
    *,
    event_id: int = 1,
    sport: str = "NFL",
    subject: str | None = None,
    team: str | None = None,
    stat_key: str | None = None,
    family: str = "player_prop",
    prob: float = 0.6,
    confidence: float = 0.7,
):
    metadata = {
        "copilot_market_family": family,
        "copilot_subject_name": subject,
        "copilot_subject_team": team,
        "copilot_stat_key": stat_key,
    }
    return SimpleNamespace(
        event=SimpleNamespace(id=event_id, sport_key=sport, participants=[]),
        market=SimpleNamespace(sport_key=sport, ticker=f"T{subject or family}{stat_key}"),
        recommendation=SimpleNamespace(side="yes", confidence=confidence),
        signal=SimpleNamespace(fair_yes_price=prob, fair_no_price=1 - prob),
        prediction=SimpleNamespace(),
        metadata=metadata,
    )


def _stack_combo():
    qb = _candidate(subject="jalen hurts", team="PHI", stat_key="passing_yards")
    wr = _candidate(subject="a.j. brown", team="PHI", stat_key="receiving_yards")
    return (qb, wr)


def test_qb_receiver_stack_detected() -> None:
    pairs = _count_correlation_pairs(_stack_combo())
    assert pairs["qb_receiver_stack"] == 1
    assert pairs["same_team"] == 1
    assert pairs["shared_subject"] == 0
    # Same-player pair is shared_subject, never a stack.
    qb = _candidate(subject="jalen hurts", team="PHI", stat_key="passing_yards")
    qb_rush = _candidate(subject="jalen hurts", team="PHI", stat_key="rushing_yards")
    self_pair = _count_correlation_pairs((qb, qb_rush))
    assert self_pair["qb_receiver_stack"] == 0
    assert self_pair["shared_subject"] == 1


def test_player_team_total_detected_same_event_only() -> None:
    prop = _candidate(subject="saquon barkley", team="PHI", stat_key="rushing_yards")
    line = _candidate(family="game_line", stat_key="total_points", subject=None, team=None)
    pairs = _count_correlation_pairs((prop, line))
    assert pairs["player_team_total"] == 1
    other_game = _candidate(
        family="game_line", stat_key="total_points", event_id=2, subject=None, team=None,
    )
    assert _count_correlation_pairs((prop, other_game))["player_team_total"] == 0


def test_nba_props_never_trip_nfl_stack_categories() -> None:
    a = _candidate(sport="NBA", subject="jayson tatum", team="BOS", stat_key="points")
    b = _candidate(sport="NBA", subject="jaylen brown", team="BOS", stat_key="rebounds")
    pairs = _count_correlation_pairs((a, b))
    assert pairs["qb_receiver_stack"] == 0
    assert pairs["player_team_total"] == 0
    assert pairs["same_team"] == 1


def test_sport_weight_overrides() -> None:
    assert _pair_weight("same_team", None, sport_scope="NFL") == 0.45
    assert _pair_weight("same_team", None) == 0.3  # default untouched
    assert _pair_weight("qb_receiver_stack", None, sport_scope="NFL") == 0.55
    assert _pair_weight("qb_receiver_stack", None, sport_scope="NBA") == 0.0


def test_stack_lifts_joint_probability_above_independence() -> None:
    combo = _stack_combo()
    pairs = _count_correlation_pairs(combo)
    joint = _correlation_adjusted_joint_probability(combo, pairs)
    independent = 0.6 * 0.6
    assert joint > independent
    # The NFL stack lifts harder than the same combo priced with the
    # default (non-NFL) weights would.
    nba_like = tuple(
        _candidate(sport="NBA", subject=s, team="PHI", stat_key=None)
        for s in ("player one", "player two")
    )
    nba_pairs = _count_correlation_pairs(nba_like)
    nba_joint = _correlation_adjusted_joint_probability(nba_like, nba_pairs)
    assert joint > nba_joint


def test_stack_penalty_in_diagnostics() -> None:
    confidence, diagnostics = _parlay_diagnostics_for_combo(
        _stack_combo(), leg_count=2, sport_scope="NFL",
    )
    assert diagnostics["qb_receiver_stack_pairs"] == 1
    assert diagnostics["penalties"]["stack"] == 0.05
    assert diagnostics["family_key"] == "nfl_parlay_2leg"
    assert confidence < 0.7  # penalties bit
