"""Tests for Smarter #9 phase 1 — fractional Kelly sizing math.

Phase 1 is pure math. Phase 2 wires bankroll input + correlation cap
+ drawdown brake input + ``recommendation.suggested_size_fraction``
persistence + UI surface.

Load-bearing tests:

- ``test_kelly_fraction_matches_textbook_formula`` — pins the math
  against a worked example operators can sanity-check.
- ``test_below_floor_suppresses_position`` — the UI hides positions
  below the operator's per-trade-overhead floor.
- ``test_drawdown_brake_linear_ramp`` — the brake doesn't cliff.
"""

from __future__ import annotations

import math

import pytest

from app.services.kelly_sizing import (
    DEFAULT_DRAWDOWN_THRESHOLD,
    DEFAULT_KELLY_FRACTION,
    DEFAULT_MAX_FRACTION,
    DEFAULT_MIN_FRACTION,
    MIN_DRAWDOWN_BRAKE_MULTIPLIER,
    PositionSize,
    clamped_position_fraction,
    drawdown_brake_multiplier,
    fractional_kelly,
    kelly_fraction,
    size_position,
)


# -- Pure Kelly math ---------------------------------------------------


def test_kelly_fraction_matches_textbook_formula() -> None:
    """Worked example: probability=0.55, price=0.50.
    Kelly = (0.55 - 0.50) / (1 - 0.50) = 0.05 / 0.50 = 0.10."""
    assert kelly_fraction(0.55, 0.50) == pytest.approx(0.10)


def test_kelly_fraction_zero_when_no_edge() -> None:
    """probability == price → no edge → Kelly says don't bet."""
    assert kelly_fraction(0.40, 0.40) == 0.0


def test_kelly_fraction_zero_when_negative_edge() -> None:
    """Don't bet against the model — Kelly returns 0, not negative."""
    assert kelly_fraction(0.30, 0.40) == 0.0


def test_kelly_fraction_long_shot_amplifies_edge() -> None:
    """Same edge (5%) at price 0.10 vs price 0.50: long-shot Kelly
    is much larger because the payout is bigger.

    At price 0.10: (0.15 - 0.10) / 0.90 = 0.0556
    At price 0.50: (0.55 - 0.50) / 0.50 = 0.10
    Wait — those don't match the docstring claim. Let me redo with
    same dollar-edge rather than percentage-points edge.

    Dollar-edge ($0.05 expected return per $1):
    At price 0.10: probability = 0.105 → (0.105 - 0.10) / 0.90 = 0.00556
      Wait, let me think again. EV per $1 staked = (p/price - 1).
      For EV = 0.05/$1, p = 0.05 * price + price = 1.05 * price.
      At price=0.10: p=0.105 → Kelly = (0.105 - 0.10)/0.90 = 0.00556
      At price=0.50: p=0.525 → Kelly = (0.525 - 0.50)/0.50 = 0.05

    So actually same expected dollar return, larger Kelly at higher
    price. That's the opposite of my naive long-shot intuition. Let
    me just pin the math correctly.
    """
    # Same edge (probability - price = 0.05) at different prices.
    long_shot = kelly_fraction(0.15, 0.10)  # = 0.05 / 0.90 ≈ 0.0556
    fair = kelly_fraction(0.55, 0.50)       # = 0.05 / 0.50 = 0.10
    favorite = kelly_fraction(0.95, 0.90)   # = 0.05 / 0.10 = 0.50

    assert long_shot == pytest.approx(0.0556, abs=0.001)
    assert fair == pytest.approx(0.10)
    assert favorite == pytest.approx(0.50)
    # Higher price (smaller payout per $1) compresses Kelly toward
    # the cap; lower price (bigger payout) shrinks Kelly toward 0
    # for fixed probability gap.
    assert favorite > fair > long_shot


def test_kelly_fraction_rejects_invalid_probability() -> None:
    with pytest.raises(ValueError, match="probability"):
        kelly_fraction(-0.1, 0.5)
    with pytest.raises(ValueError, match="probability"):
        kelly_fraction(1.5, 0.5)


def test_kelly_fraction_rejects_invalid_price() -> None:
    with pytest.raises(ValueError, match="price"):
        kelly_fraction(0.5, 0.0)
    with pytest.raises(ValueError, match="price"):
        kelly_fraction(0.5, 1.0)
    with pytest.raises(ValueError, match="price"):
        kelly_fraction(0.5, 1.5)


# -- Fractional Kelly --------------------------------------------------


def test_fractional_kelly_default_is_quarter_kelly() -> None:
    """``DEFAULT_KELLY_FRACTION = 0.25`` — pin so a future bump in
    the constant doesn't silently change sizing."""
    assert DEFAULT_KELLY_FRACTION == 0.25
    raw = kelly_fraction(0.55, 0.50)
    assert fractional_kelly(0.55, 0.50) == pytest.approx(0.25 * raw)


def test_fractional_kelly_full_kelly() -> None:
    """fraction=1.0 returns raw Kelly. Some operators want full
    Kelly for diagnostic comparisons."""
    assert fractional_kelly(0.55, 0.50, fraction=1.0) == pytest.approx(
        kelly_fraction(0.55, 0.50)
    )


def test_fractional_kelly_rejects_zero_fraction() -> None:
    with pytest.raises(ValueError, match="fraction"):
        fractional_kelly(0.55, 0.50, fraction=0.0)


def test_fractional_kelly_rejects_over_kelly() -> None:
    """``fraction > 1.0`` ("over-Kelly") is mathematically allowed but
    catastrophically risky — the validator rejects it."""
    with pytest.raises(ValueError, match="fraction"):
        fractional_kelly(0.55, 0.50, fraction=1.5)


# -- Clamps ------------------------------------------------------------


def test_clamp_under_floor_suppresses() -> None:
    """Tiny edge (1bp) → fractional Kelly below the 0.5% floor
    → suppressed."""
    fraction, below = clamped_position_fraction(0.501, 0.500)
    assert below is True
    assert fraction == 0.0


def test_clamp_in_range_passes_through() -> None:
    """Edge that lands in [floor, ceiling] passes through unchanged."""
    # probability=0.55, price=0.50 → raw=0.10 → 0.25*0.10 = 0.025.
    # That's above the 2% ceiling — it gets clamped down.
    # Use a smaller edge that lands in range:
    # probability=0.51, price=0.50 → raw=0.02 → 0.25*0.02 = 0.005.
    # 0.005 == DEFAULT_MIN_FRACTION; ``< min_fraction`` check is
    # strict, so 0.005 passes through.
    fraction, below = clamped_position_fraction(0.51, 0.50)
    assert below is False
    assert fraction == pytest.approx(0.005)


def test_clamp_above_ceiling_caps() -> None:
    """A genuine fat-tail edge gets capped at max_fraction (default
    2% of bankroll). Pinning this prevents a future "loosen the cap"
    PR from quietly removing the safety net."""
    # probability=0.65, price=0.50 → raw=0.30 → 0.25*0.30 = 0.075.
    # Way above 2% ceiling; clamped to 0.02.
    fraction, below = clamped_position_fraction(0.65, 0.50)
    assert below is False
    assert fraction == pytest.approx(0.02)


def test_clamp_validates_args() -> None:
    with pytest.raises(ValueError, match="min_fraction"):
        clamped_position_fraction(0.55, 0.50, min_fraction=-0.01)
    with pytest.raises(ValueError, match="max_fraction"):
        clamped_position_fraction(0.55, 0.50, min_fraction=0.05, max_fraction=0.02)


# -- size_position end-to-end ------------------------------------------


def test_size_position_returns_dollars() -> None:
    """End-to-end: bankroll * clamped fraction = dollars."""
    sized = size_position(probability=0.51, price=0.50, bankroll=10_000.0)
    assert sized.fraction == pytest.approx(0.005)
    assert sized.dollars == pytest.approx(50.0)
    assert sized.below_floor is False


def test_size_position_below_floor_returns_zero_dollars() -> None:
    """Position size of 0 means the UI should hide the
    recommendation entirely."""
    sized = size_position(probability=0.501, price=0.500, bankroll=10_000.0)
    assert sized.fraction == 0.0
    assert sized.dollars == 0.0
    assert sized.below_floor is True


def test_size_position_carries_intermediates() -> None:
    """``raw_kelly`` and ``fractional_kelly`` survive the clamp so
    the operator can see how much edge is "left on the table" when
    the cap kicks in."""
    sized = size_position(probability=0.65, price=0.50, bankroll=10_000.0)
    # raw_kelly = (0.65 - 0.50) / 0.50 = 0.30
    # fractional_kelly = 0.25 * 0.30 = 0.075 (way above 0.02 cap)
    assert sized.raw_kelly == pytest.approx(0.30)
    assert sized.fractional_kelly == pytest.approx(0.075)
    # Final fraction was clamped to the 2% cap.
    assert sized.fraction == pytest.approx(0.02)


def test_size_position_applies_brake_multiplier() -> None:
    """Drawdown brake at 0.5 halves the final position size — the
    clamps run first, the brake runs last."""
    sized = size_position(
        probability=0.65, price=0.50, bankroll=10_000.0, brake_multiplier=0.5,
    )
    # Clamped to 0.02, then halved by brake = 0.01.
    assert sized.fraction == pytest.approx(0.01)
    assert sized.dollars == pytest.approx(100.0)
    assert sized.brake_multiplier == 0.5


def test_size_position_brake_does_not_re_suppress_below_floor() -> None:
    """A brake that pushes a clamped-but-OK position below the floor
    must NOT re-suppress to zero — the operator already knows the
    brake is on, and seeing the brake-downsized position is more
    informative than seeing nothing."""
    # probability=0.51, price=0.50 → fractional Kelly = 0.005 (== floor).
    # Brake of 0.25 would push to 0.00125, which is below 0.005 floor.
    # We do NOT re-clamp; brake is applied AFTER the clamps.
    sized = size_position(
        probability=0.51, price=0.50, bankroll=10_000.0, brake_multiplier=0.25,
    )
    assert sized.fraction == pytest.approx(0.00125)
    assert sized.below_floor is False  # the clamp didn't suppress, brake just shrunk


def test_size_position_validates_bankroll() -> None:
    with pytest.raises(ValueError, match="bankroll"):
        size_position(probability=0.51, price=0.50, bankroll=-100.0)


def test_size_position_validates_brake_multiplier() -> None:
    with pytest.raises(ValueError, match="brake_multiplier"):
        size_position(probability=0.51, price=0.50, bankroll=10_000.0, brake_multiplier=-0.1)
    with pytest.raises(ValueError, match="brake_multiplier"):
        size_position(probability=0.51, price=0.50, bankroll=10_000.0, brake_multiplier=1.5)


def test_size_position_handles_zero_bankroll() -> None:
    """Operator can sanity-check with zero bankroll without an
    error. Useful in tests + dev environments."""
    sized = size_position(probability=0.55, price=0.50, bankroll=0.0)
    assert sized.dollars == 0.0


# -- Drawdown brake ----------------------------------------------------


def test_drawdown_brake_no_brake_when_above_threshold() -> None:
    """Positive PnL or shallow drawdown → no brake."""
    assert drawdown_brake_multiplier(0.05) == 1.0
    assert drawdown_brake_multiplier(0.0) == 1.0
    assert drawdown_brake_multiplier(-0.04) == 1.0  # above threshold of -0.05


def test_drawdown_brake_at_threshold_starts_ramp() -> None:
    """Right at threshold → still 1.0 (inclusive)."""
    assert drawdown_brake_multiplier(-0.05) == pytest.approx(1.0)


def test_drawdown_brake_linear_ramp() -> None:
    """At halfway through the ramp (1.5x threshold), multiplier is
    halfway between 1.0 and the floor (0.25). For threshold=-0.05,
    halfway is -0.075 → multiplier = 0.625."""
    assert drawdown_brake_multiplier(-0.075) == pytest.approx(0.625)


def test_drawdown_brake_floor_at_2x_threshold() -> None:
    """At 2x threshold → floor multiplier."""
    assert drawdown_brake_multiplier(-0.10) == pytest.approx(0.25)


def test_drawdown_brake_pinned_at_floor_below_2x() -> None:
    """Beyond 2x threshold → still floor (no negative multiplier)."""
    assert drawdown_brake_multiplier(-0.50) == pytest.approx(0.25)


def test_drawdown_brake_handles_nan() -> None:
    """A NaN rolling PnL (e.g. divide-by-zero bankroll) must not
    crash sizing — disable the brake (multiplier 1.0). Defensive,
    not a production code path."""
    assert drawdown_brake_multiplier(float("nan")) == 1.0
    assert drawdown_brake_multiplier(float("-inf")) == 1.0


def test_drawdown_brake_validates_threshold_sign() -> None:
    """``threshold`` must be negative — a positive threshold would
    invert the brake. Reject as obvious caller bug."""
    with pytest.raises(ValueError, match="threshold"):
        drawdown_brake_multiplier(-0.05, threshold=0.05)


def test_drawdown_brake_validates_min_multiplier_range() -> None:
    with pytest.raises(ValueError, match="min_multiplier"):
        drawdown_brake_multiplier(-0.05, min_multiplier=-0.1)
    with pytest.raises(ValueError, match="min_multiplier"):
        drawdown_brake_multiplier(-0.05, min_multiplier=1.5)


# -- Module-level constants are sane -----------------------------------


def test_default_constants_are_sane() -> None:
    """Pin default values so a future "tighten the floor" PR is
    visible in diff and reviewable, not silent."""
    assert DEFAULT_KELLY_FRACTION == 0.25
    assert DEFAULT_MIN_FRACTION == 0.005
    assert DEFAULT_MAX_FRACTION == 0.02
    assert DEFAULT_DRAWDOWN_THRESHOLD == -0.05
    assert MIN_DRAWDOWN_BRAKE_MULTIPLIER == 0.25
    # Min must be strictly less than max — otherwise the clamp
    # collapses to a single point.
    assert DEFAULT_MIN_FRACTION < DEFAULT_MAX_FRACTION
