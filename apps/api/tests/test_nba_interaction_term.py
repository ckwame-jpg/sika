"""Tests for Smarter #12 — NBA usage × pace × defense interaction term.

Per the handoff, this PR explicitly does NOT add a heuristic factor.
The interaction term is a single uncapped feature emitted into the
features dict so the ML training pipeline picks it up via its existing
dynamic flatten path.
"""

import pytest

from app.services.advanced_stats import emit_nba_interaction_term
from app.services.heuristic_factors import _NBA_FACTOR_FNS, _NBA_FACTORS_BY_STAT


# -- happy paths ---------------------------------------------------------


def test_league_average_inputs_produce_unity():
    out = emit_nba_interaction_term(
        usage_pct=0.25,
        opponent_pace=100.0,
        opponent_drtg=110.0,
    )
    assert out["nba_offense_interaction_term"] == 1.0


def test_high_usage_slow_pace_strong_defense_partial_cancel():
    # Hand-computed under the corrected (drtg / 110) direction:
    #   (0.30 / 0.25) * (95 / 100) * (102 / 110) = 1.2 * 0.95 * 0.9273 ≈ 1.0571
    # Usage boost partially cancels against slow pace and strong defense
    # — net still slightly above league average because high usage
    # dominates a mildly strong defense.
    out = emit_nba_interaction_term(
        usage_pct=0.30,
        opponent_pace=95.0,
        opponent_drtg=102.0,
    )
    assert out["nba_offense_interaction_term"] == pytest.approx(1.0571, abs=1e-3)


def test_high_usage_fast_pace_weak_defense_compounds_positively():
    # All three factors lift: (0.32 / 0.25) * (105 / 100) * (118 / 110)
    #   = 1.28 * 1.05 * 1.0727 ≈ 1.4419
    out = emit_nba_interaction_term(
        usage_pct=0.32,
        opponent_pace=105.0,
        opponent_drtg=118.0,
    )
    assert out["nba_offense_interaction_term"] == pytest.approx(1.4419, abs=1e-3)


def test_low_usage_slow_pace_strong_defense_compounds_negatively():
    # All three factors suppress: (0.18 / 0.25) * (94 / 100) * (100 / 110)
    #   = 0.72 * 0.94 * 0.9091 ≈ 0.6153
    out = emit_nba_interaction_term(
        usage_pct=0.18,
        opponent_pace=94.0,
        opponent_drtg=100.0,
    )
    assert out["nba_offense_interaction_term"] == pytest.approx(0.6153, abs=1e-3)


def test_direction_matches_nba_opp_def_factor_convention():
    """Smarter #12 corrects the original handoff pseudocode (which had
    ``110 / drtg``, inverting the established convention). Under the
    corrected formula ``drtg / 110``, an elite defense suppresses the
    term and a weak defense boosts it — matching ``_nba_opp_def_factor``.
    """
    base = dict(usage_pct=0.25, opponent_pace=100.0)
    weak_defense = emit_nba_interaction_term(**base, opponent_drtg=120.0)
    elite_defense = emit_nba_interaction_term(**base, opponent_drtg=100.0)
    assert weak_defense["nba_offense_interaction_term"] > 1.0
    assert elite_defense["nba_offense_interaction_term"] < 1.0


# -- missing / defensive inputs -----------------------------------------


def test_missing_usage_returns_empty():
    assert emit_nba_interaction_term(
        usage_pct=None,
        opponent_pace=100.0,
        opponent_drtg=110.0,
    ) == {}


def test_missing_pace_returns_empty():
    assert emit_nba_interaction_term(
        usage_pct=0.25,
        opponent_pace=None,
        opponent_drtg=110.0,
    ) == {}


def test_missing_drtg_returns_empty():
    assert emit_nba_interaction_term(
        usage_pct=0.25,
        opponent_pace=100.0,
        opponent_drtg=None,
    ) == {}


def test_zero_drtg_returns_empty():
    # Defensive: divide-by-zero would crash the heuristic chain.
    assert emit_nba_interaction_term(
        usage_pct=0.25,
        opponent_pace=100.0,
        opponent_drtg=0.0,
    ) == {}


def test_negative_drtg_returns_empty():
    # Defensive: a negative DRtg from corrupted data would produce a
    # nonsensical sign-flipped product.
    assert emit_nba_interaction_term(
        usage_pct=0.25,
        opponent_pace=100.0,
        opponent_drtg=-10.0,
    ) == {}


def test_non_numeric_inputs_return_empty():
    # Type contract — callers shouldn't pass strings, but defending here
    # keeps the function safe to invoke without sentinel handling.
    assert emit_nba_interaction_term(
        usage_pct="0.25",  # type: ignore[arg-type]
        opponent_pace=100.0,
        opponent_drtg=110.0,
    ) == {}


def test_zero_usage_emits_zero_interaction_term():
    # A player with effectively zero usage produces a zero-valued
    # interaction; this is NOT a missing-data case so we still emit.
    out = emit_nba_interaction_term(
        usage_pct=0.0,
        opponent_pace=100.0,
        opponent_drtg=110.0,
    )
    assert out["nba_offense_interaction_term"] == 0.0


# -- intentional non-integration with heuristic factors -----------------


def test_interaction_term_is_not_a_heuristic_factor():
    """Smarter #12 explicitly does NOT add a heuristic factor that
    consumes this feature. The point is to let the ML model learn the
    multiplicative shape — the heuristic continues to use independent
    capped factors."""
    # Drift guard in the OPPOSITE direction from the usual pattern.
    gated = {name for tup in _NBA_FACTORS_BY_STAT.values() for name in tup}
    assert "nba_offense_interaction_term" not in gated, (
        "Smarter #12 explicitly defers the interaction shape to the ML "
        "model — adding a heuristic factor here would double-count."
    )
    assert "nba_offense_interaction_term" not in _NBA_FACTOR_FNS


# -- output key contract --------------------------------------------------


def test_emitted_key_matches_consumer_expectation():
    """Codex Pattern 2 — the training pipeline (``apps/ml/ml/dataset.py``)
    picks up new keys via its dynamic flatten path (no allowlist). This
    test pins the key name so a rename here would break feature continuity
    across retrains."""
    out = emit_nba_interaction_term(
        usage_pct=0.25,
        opponent_pace=100.0,
        opponent_drtg=110.0,
    )
    assert set(out.keys()) == {"nba_offense_interaction_term"}


def test_output_is_rounded_to_four_decimals():
    out = emit_nba_interaction_term(
        usage_pct=0.273,
        opponent_pace=99.7,
        opponent_drtg=109.3,
    )
    value = out["nba_offense_interaction_term"]
    # Result should be a 4-decimal-rounded float.
    assert abs(value - round(value, 4)) < 1e-9
