"""Smarter #9 (phase 1) — fractional Kelly position sizing math.

Better probabilities don't translate to better PnL if stake sizing
overexposes correlated edges. Kelly maximizes long-run growth rate,
fractional Kelly trades a small expected-growth penalty for a large
variance reduction (the standard advice for real-money trading), and
the floor/ceiling clamps protect against both noise (don't size up
on a 4-row "edge") and ruin (don't size down to dust on a real one).

Phase 1 ships the math only. Phase 2 wires:

- Bankroll input (operator setting + opt-in toggle to Kalshi live balance)
- Per-event cap aggregating correlated legs (Smarter #8 estimator)
- Drawdown brake checking rolling 7-day PnL
- ``recommendation.suggested_size_fraction`` persistence
- ``apps/web/components/trade/trade-ticket.tsx`` surface

## Kelly derivation for binary YES contracts

Standard Kelly on a binary bet at decimal odds ``D``:

    f* = (b * p - q) / b
    where b = D - 1 is the net decimal odds, p is the true win
    probability, q = 1 - p

Kalshi YES contracts pay ``$1`` for every winning contract bought at
``price``, so:

    decimal odds D = 1 / price
    net odds b = D - 1 = (1 - price) / price

Substituting and simplifying:

    f* = (p - price) / (1 - price)

Cleaner: **Kelly fraction equals edge over remaining payout.** Edge
is ``p - price``, remaining payout is ``1 - price`` (what each $1
stake actually pays out as profit on a win).

This form makes the operator intuition obvious: a 5% edge at
price 0.50 is a 10% Kelly stake (5% / 50%); the same 5% edge at
price 0.90 is a 50% Kelly stake (5% / 10%) — the long-shot is
proportionally a much bigger bet because the payout is larger.

## Why fractional Kelly

Full Kelly maximizes long-run growth but bets aggressively. A 25%
Kelly is the standard "real-money" choice — gives up about 6% of
expected log-growth in exchange for ~94% lower variance. The class
that matters is "robust to estimation error in p" — at full Kelly,
a 5% over-estimate of p doubles the bet size; at quarter Kelly the
same error has 1/4 the impact.

## Floor / ceiling clamps

The bare Kelly fraction can recommend tiny stakes (e.g. 0.3% of
bankroll on a marginal edge). Below some floor the operator's time
to enter the trade isn't worth the EV; we'd rather not surface it
at all. Above some ceiling the position concentration becomes a
correlation-risk problem (one bad slate eats meaningful capital).
The clamps are bankroll-relative, not absolute, so they scale with
the operator's balance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

__all__ = [
    "PositionSize",
    "kelly_fraction",
    "fractional_kelly",
    "clamped_position_fraction",
    "size_position",
    "drawdown_brake_multiplier",
]

# Default Kelly multiplier (fractional Kelly) — 25% is the standard
# real-money trading choice. 100% (full Kelly) maximizes log-growth
# but is fragile to estimation error in p; 25% gives up ~6% of
# expected growth for ~94% variance reduction.
DEFAULT_KELLY_FRACTION = 0.25

# Default min / max bankroll fraction. Anything below the floor is
# below the operator's per-trade transaction overhead; anything above
# the ceiling concentrates too much capital in one correlated bet.
DEFAULT_MIN_FRACTION = 0.005  # 0.5% of bankroll
DEFAULT_MAX_FRACTION = 0.02  # 2% of bankroll

# Default drawdown brake threshold. Rolling 7-day PnL below this
# fraction of bankroll triggers the brake — losing 5% in a week is
# a strong signal that something's miscalibrated, and downsizing
# while we figure out what gives the operator runway to investigate.
DEFAULT_DRAWDOWN_THRESHOLD = -0.05  # -5% of bankroll over 7d

# Floor for the brake multiplier. Even a deep drawdown shouldn't
# size positions to literal zero — operators sometimes need to take
# a small position to keep market-making spreads tight or test a
# fix. 25% is the floor.
MIN_DRAWDOWN_BRAKE_MULTIPLIER = 0.25


@dataclass(frozen=True, slots=True)
class PositionSize:
    """Output of ``size_position``. ``fraction`` is the bankroll
    fraction after all clamps; ``dollars`` is ``fraction * bankroll``;
    ``raw_kelly`` and ``fractional_kelly`` are the pre-clamp values
    so the operator can see how aggressive the unclamped sizing
    would have been (useful when a position size hits the ceiling
    and the operator wants to know how much edge they're "leaving
    on the table").

    ``brake_multiplier`` is what the drawdown brake applied (1.0 =
    no brake, < 1.0 = downsized, 0.0 only if the caller passed a
    floor of 0). ``below_floor`` is True when the unclamped
    fractional Kelly was below ``min_fraction`` — the position is
    suppressed entirely (returned as ``fraction = 0.0``) so the
    UI can hide it rather than surface a position that the operator
    won't bother to take.
    """

    fraction: float
    dollars: float
    raw_kelly: float
    fractional_kelly: float
    brake_multiplier: float
    below_floor: bool


# -- Pure Kelly math ---------------------------------------------------


def kelly_fraction(probability: float, price: float) -> float:
    """Raw Kelly fraction for a YES contract bought at ``price`` with
    true win probability ``probability``. Returns the fraction of
    *bankroll* to stake; positive values indicate a positive-EV bet.

    Formula: ``(probability - price) / (1 - price)``. See module
    docstring for the derivation from the standard binary-Kelly
    form.

    Returns ``0.0`` when:
    - ``probability <= price`` (no edge — Kelly says don't bet)
    - ``price >= 1.0`` (degenerate — no payout)

    Raises ``ValueError`` for invalid inputs (probability or price
    outside ``[0, 1]``).
    """
    if not 0.0 <= probability <= 1.0:
        raise ValueError(f"probability must be in [0, 1], got {probability}")
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price}")
    if probability <= price:
        return 0.0
    return float((probability - price) / (1.0 - price))


def fractional_kelly(
    probability: float,
    price: float,
    *,
    fraction: float = DEFAULT_KELLY_FRACTION,
) -> float:
    """``fraction * kelly_fraction(probability, price)``. The standard
    real-money trading move — full Kelly maximizes growth but is
    fragile to estimation error; 25% Kelly gives up a small slice of
    expected growth for a large variance reduction.

    ``fraction`` must be in ``(0, 1]``. ``1.0`` is full Kelly;
    values above 1.0 ("over-Kelly") are mathematically allowed but
    catastrophically risky and not what the parlay sizer wants —
    the validator rejects them.
    """
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction}")
    return float(fraction * kelly_fraction(probability, price))


def clamped_position_fraction(
    probability: float,
    price: float,
    *,
    kelly_fraction_value: float = DEFAULT_KELLY_FRACTION,
    min_fraction: float = DEFAULT_MIN_FRACTION,
    max_fraction: float = DEFAULT_MAX_FRACTION,
) -> tuple[float, bool]:
    """Apply the floor/ceiling clamps to a fractional-Kelly value.

    Returns ``(clamped_fraction, below_floor)``:

    - ``below_floor=True`` when the raw fractional Kelly was strictly
      below ``min_fraction`` — the position is suppressed (returns
      ``0.0``) so the UI hides it rather than surface a stake too
      small to be worth the operator's time.
    - Otherwise ``clamped_fraction`` is the fractional Kelly bounded
      by ``[min_fraction, max_fraction]``.
    """
    if min_fraction < 0.0:
        raise ValueError(f"min_fraction must be >= 0, got {min_fraction}")
    if max_fraction < min_fraction:
        raise ValueError(
            f"max_fraction ({max_fraction}) must be >= min_fraction ({min_fraction})"
        )
    raw = fractional_kelly(probability, price, fraction=kelly_fraction_value)
    if raw < min_fraction:
        return 0.0, True
    if raw > max_fraction:
        return max_fraction, False
    return raw, False


def size_position(
    probability: float,
    price: float,
    bankroll: float,
    *,
    kelly_fraction_value: float = DEFAULT_KELLY_FRACTION,
    min_fraction: float = DEFAULT_MIN_FRACTION,
    max_fraction: float = DEFAULT_MAX_FRACTION,
    brake_multiplier: float = 1.0,
) -> PositionSize:
    """End-to-end sizing: probability + price + bankroll → dollars.

    ``brake_multiplier`` is the drawdown brake's output (1.0 = no
    brake). It's applied AFTER the clamps — a brake that pushes the
    position below ``min_fraction`` doesn't suppress the position
    further; the operator already knows the brake is on, and a
    full-suppress would hide the position from the UI when the
    operator most needs to see "downsized due to drawdown" rather
    than nothing.

    All inputs validated. Returns the full ``PositionSize`` with the
    raw / fractional-Kelly intermediates so the caller can show
    "you're leaving X bps on the table because the cap kicked in"
    in the operator UI.
    """
    if bankroll < 0.0:
        raise ValueError(f"bankroll must be >= 0, got {bankroll}")
    if not 0.0 <= brake_multiplier <= 1.0:
        raise ValueError(
            f"brake_multiplier must be in [0, 1], got {brake_multiplier}"
        )
    raw = kelly_fraction(probability, price)
    frac_kelly = float(kelly_fraction_value * raw)
    clamped, below_floor = clamped_position_fraction(
        probability,
        price,
        kelly_fraction_value=kelly_fraction_value,
        min_fraction=min_fraction,
        max_fraction=max_fraction,
    )
    if below_floor:
        return PositionSize(
            fraction=0.0,
            dollars=0.0,
            raw_kelly=raw,
            fractional_kelly=frac_kelly,
            brake_multiplier=brake_multiplier,
            below_floor=True,
        )
    final_fraction = clamped * brake_multiplier
    return PositionSize(
        fraction=final_fraction,
        dollars=final_fraction * bankroll,
        raw_kelly=raw,
        fractional_kelly=frac_kelly,
        brake_multiplier=brake_multiplier,
        below_floor=False,
    )


# -- Drawdown brake ----------------------------------------------------


def drawdown_brake_multiplier(
    rolling_pnl_fraction: float,
    *,
    threshold: float = DEFAULT_DRAWDOWN_THRESHOLD,
    min_multiplier: float = MIN_DRAWDOWN_BRAKE_MULTIPLIER,
) -> float:
    """Compute a position-size multiplier from rolling PnL as a
    fraction of bankroll.

    ``rolling_pnl_fraction`` is e.g. -0.05 for a 5% drawdown.
    ``threshold`` is the brake-on point (default -0.05).
    ``min_multiplier`` is the floor (default 0.25 — even a deep
    drawdown leaves the operator able to take small positions).

    - At or above threshold: returns 1.0 (no brake).
    - Below threshold: linear scaling. The multiplier hits
      ``min_multiplier`` at ``2 * threshold`` (so a 10% drawdown
      with a -5% threshold gives the floor multiplier).
    - Pinned to ``[min_multiplier, 1.0]``.

    Linear (not e.g. exponential) because operators reason about
    "how much am I downsizing" — a smooth linear ramp from 1.0 to
    0.25 over a 5% PnL band is easier to predict than a curve.
    """
    if threshold >= 0.0:
        raise ValueError(f"threshold must be < 0 for drawdown, got {threshold}")
    if not 0.0 <= min_multiplier <= 1.0:
        raise ValueError(
            f"min_multiplier must be in [0, 1], got {min_multiplier}"
        )
    if not math.isfinite(rolling_pnl_fraction):
        # Defensive: a NaN/inf rolling PnL (e.g. divide-by-zero
        # bankroll) shouldn't crash sizing — disable the brake.
        return 1.0
    if rolling_pnl_fraction >= threshold:
        return 1.0
    # Ramp range: [threshold, 2*threshold]. At threshold → 1.0;
    # at 2*threshold (or beyond) → min_multiplier.
    ramp_span = abs(threshold)
    overshoot = abs(rolling_pnl_fraction) - abs(threshold)
    fraction_into_ramp = min(overshoot / ramp_span, 1.0)
    return float(1.0 - fraction_into_ramp * (1.0 - min_multiplier))
