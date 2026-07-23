"""Kalshi fee estimates shared by live-order money guardrails.

Kalshi rounds each taker fee up to the next cent.  IOC orders can fill
below their limit price, so the largest possible fee is not always the
fee at the limit: ``p * (1 - p)`` peaks at 50 cents.
"""

from __future__ import annotations

import math

TAKER_FEE_RATE = 0.07


def estimate_taker_fee_dollars(quantity: float, price_dollars: float) -> float:
    """Estimate the taker fee, rounded up to the next whole cent."""
    if not math.isfinite(quantity) or not math.isfinite(price_dollars):
        return 0.0
    if quantity <= 0 or price_dollars <= 0 or price_dollars >= 1:
        return 0.0

    raw_fee = TAKER_FEE_RATE * quantity * price_dollars * (1 - price_dollars)
    # Guard exact-cent results from binary float noise.  For example,
    # 0.07 * 25 * 0.4 * 0.6 is represented just above 42 cents.
    return math.ceil(raw_fee * 100 - 1e-9) / 100


def worst_case_taker_fee_dollars(
    quantity: float, limit_price_dollars: float
) -> float:
    """Return the largest taker fee for a fill at or below the limit."""
    worst_price = min(limit_price_dollars, 0.5)
    return estimate_taker_fee_dollars(quantity, worst_price)
