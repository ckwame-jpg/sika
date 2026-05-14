"""Closing-line value (Smarter #3).

CLV is the signed delta between a prediction's entry price and the market's
closing price on the SAME side as the pick. Positive CLV = the line moved
toward sika's recommendation between capture and close, which is the
standard external sharpness signal: a model that consistently beats the
close is sharp; one that moves away from it is noise.

This module owns three concerns:

1. Reading the closing YES price for a market from its snapshot history.
2. Computing the signed CLV for a single prediction.
3. Aggregating average CLV across a list of settled predictions.

Backfill is intentionally out of scope here — the settlement path picks up
the new fields on the next pass, and historical rows can be filled lazily
by a one-off script if needed.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MarketSnapshot, Prediction


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return number


def _snapshot_yes_price(snapshot: MarketSnapshot) -> float | None:
    """Pick the best available YES price for a snapshot.

    Preference order: mid of ``yes_bid`` and ``yes_ask`` (most representative
    of fair value at close), then ``last_price`` (last traded price), then
    None. Mid-pricing prefers a real two-sided market over a stale last
    trade, but falls back gracefully when one side is missing or the snapshot
    only carries trade prices.
    """

    yes_bid = _safe_float(snapshot.yes_bid)
    yes_ask = _safe_float(snapshot.yes_ask)
    if yes_bid is not None and yes_ask is not None:
        mid = (yes_bid + yes_ask) / 2.0
        if 0.0 <= mid <= 1.0:
            return mid
    last_price = _safe_float(snapshot.last_price)
    if last_price is not None and 0.0 <= last_price <= 1.0:
        return last_price
    return None


def closing_yes_price_for_market(
    db: Session,
    market_id: int,
    *,
    before: datetime | None = None,
) -> float | None:
    """Latest YES price for ``market_id`` from a snapshot captured at or
    before ``before``.

    Falls back to the latest snapshot of any time if ``before`` is ``None``
    (settlement may run after market close, so this is the common case).
    Returns ``None`` if there is no usable snapshot at all.
    """

    stmt = select(MarketSnapshot).where(MarketSnapshot.market_id == market_id)
    if before is not None:
        stmt = stmt.where(MarketSnapshot.captured_at <= before)
    stmt = stmt.order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc()).limit(1)
    snapshot = db.scalar(stmt)
    if snapshot is None:
        return None
    return _snapshot_yes_price(snapshot)


def compute_clv(*, side: str, suggested_price: float | None, closing_yes_price: float | None) -> float | None:
    """Signed CLV from the pick's perspective.

    YES → ``closing_yes_price - suggested_price``
    NO  → ``(1 - closing_yes_price) - suggested_price``

    Positive means the closing line is more favorable to the pick than the
    capture price. Returns ``None`` when any input is missing, malformed, or
    out of the unit interval — we never report a meaningless CLV.
    """

    suggested = _safe_float(suggested_price)
    closing = _safe_float(closing_yes_price)
    if suggested is None or closing is None:
        return None
    if closing < 0.0 or closing > 1.0:
        return None
    if suggested < 0.0 or suggested > 1.0:
        return None
    normalized_side = str(side or "").strip().lower()
    if normalized_side == "yes":
        return round(closing - suggested, 4)
    if normalized_side == "no":
        return round((1.0 - closing) - suggested, 4)
    return None


def average_clv(predictions: Iterable[Prediction]) -> float | None:
    """Mean of ``closing_line_value`` over the settled subset of
    ``predictions``. Returns ``None`` when no row carries a CLV value
    (e.g. fresh deploy before any settlement has run with the new fields)."""

    values: list[float] = []
    for prediction in predictions:
        raw = _safe_float(getattr(prediction, "closing_line_value", None))
        if raw is not None:
            values.append(raw)
    if not values:
        return None
    return round(sum(values) / len(values), 4)
