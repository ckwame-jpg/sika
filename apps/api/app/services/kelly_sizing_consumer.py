"""Smarter #9 (phase 3) — consumer-side helper that composes the
phase 1 math + phase 2 DB inputs into a serialized ``kelly_sizing``
diagnostic block.

Phase 1 (PR #126) shipped the math (Kelly fraction, fractional
Kelly, clamps, drawdown brake). Phase 2 (PR #130) shipped DB inputs
(bankroll resolver + rolling PnL fraction). Phase 3 wires them
together for the scoring persistence layer so every recommendation
carries a persisted suggested size (no migration required — the
output lands in ``scoring_diagnostics`` JSON).

## What the diagnostic shape is

The returned dict contains everything an operator UI surface (or a
future trade-ticket size widget) needs to display the suggested
size with its provenance:

    {
        "fraction": float,            # bankroll-fraction stake
        "dollars": float,             # fraction * bankroll
        "raw_kelly": float,           # pre-clamp Kelly fraction
        "fractional_kelly": float,    # 0.25 * raw (pre-clamp)
        "brake_multiplier": float,    # drawdown brake (1.0 = no brake)
        "below_floor": bool,          # suppressed because too small
        "bankroll": float,            # the bankroll resolution used
    }

``None`` is returned when:
- Bankroll resolution returns ``None`` (operator hasn't configured
  ``kelly_sizing_bankroll_dollars`` and Kalshi opt-in is off or
  unavailable). The consumer should fall back to suppressing the
  size hint.
- ``probability`` or ``price`` is outside ``(0, 1)``. Defensive —
  invalid inputs return ``None`` rather than raising so the
  persistence layer doesn't crash on a malformed score.

## Side-aware math

The classifier's raw output is P(YES). Sika picks a side; the Kelly
math uses the selected-side probability + selected-side price. We
invert YES→NO when ``side == "no"``:

    side == "yes": probability = P(YES); price = YES-ask price
    side == "no":  probability = 1 - P(YES); price = 1 - YES-ask price

The fractional Kelly stays at the operator-set ``kelly_fraction``
default (25%).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy.orm import Session

from app.services.kelly_sizing import (
    drawdown_brake_multiplier,
    size_position,
)
from app.services.kelly_sizing_db import (
    compute_rolling_pnl_fraction,
    resolve_bankroll,
)

logger = logging.getLogger(__name__)

__all__ = [
    "compute_kelly_sizing_diagnostics",
]


def _selected_side_inputs(
    probability_yes: float, price_yes: float, side: str,
) -> tuple[float, float]:
    """Map (P(YES), YES price, side) → (selected probability,
    selected price) for the Kelly math. ``side == "yes"`` is the
    identity; ``side == "no"`` inverts both axes."""
    side_lower = (side or "").lower()
    if side_lower == "no":
        return 1.0 - probability_yes, 1.0 - price_yes
    return probability_yes, price_yes


def compute_kelly_sizing_diagnostics(
    db: Session,
    *,
    probability_yes: float,
    price_yes: float,
    side: str,
) -> dict[str, Any] | None:
    """Compose Phase 1 math + Phase 2 DB inputs into the diagnostic
    block. Returns ``None`` when sizing isn't applicable (no
    bankroll, invalid inputs).
    """
    # Validate inputs at the boundary — the Kelly math itself
    # raises on invalid ranges, which would surface as a 500 from
    # the persistence path. Defensive None lets the caller log
    # "sizing unavailable" instead.
    if not math.isfinite(probability_yes) or not (0.0 < probability_yes < 1.0):
        return None
    if not math.isfinite(price_yes) or not (0.0 < price_yes < 1.0):
        return None
    if side and side.lower() not in ("yes", "no"):
        # Unknown side — can't map to selected-side axis. Future
        # markets might introduce a third option; for now skip
        # rather than guess.
        return None

    selected_probability, selected_price = _selected_side_inputs(
        probability_yes, price_yes, side,
    )
    if not (0.0 < selected_price < 1.0):
        # Side inversion produced a degenerate price (YES price of
        # 1.0 inverts to NO price 0.0). Skip.
        return None

    bankroll = resolve_bankroll(db)
    if bankroll is None:
        return None

    try:
        rolling_pnl_fraction = compute_rolling_pnl_fraction(db, bankroll=bankroll)
    except Exception as exc:  # noqa: BLE001 — defensive at persistence boundary
        logger.warning("kelly_sizing: rolling_pnl fetch failed (%s) — skipping brake", exc)
        rolling_pnl_fraction = 0.0
    brake_multiplier = drawdown_brake_multiplier(rolling_pnl_fraction)
    sized = size_position(
        probability=selected_probability,
        price=selected_price,
        bankroll=bankroll,
        brake_multiplier=brake_multiplier,
    )
    return {
        "fraction": round(sized.fraction, 4),
        "dollars": round(sized.dollars, 2),
        "raw_kelly": round(sized.raw_kelly, 4),
        "fractional_kelly": round(sized.fractional_kelly, 4),
        "brake_multiplier": round(sized.brake_multiplier, 4),
        "below_floor": bool(sized.below_floor),
        "bankroll": round(bankroll, 2),
    }
