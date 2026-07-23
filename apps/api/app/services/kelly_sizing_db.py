"""DB inputs for the fractional-Kelly sizing math in
``kelly_sizing.py``.

Phase 1 (PR #126) shipped the pure math (Kelly formula, fractional
multiplier, floor/ceiling clamps, drawdown brake). Phase 2 wires:

- **Bankroll resolution**: operator setting (``kelly_sizing_bankroll_dollars``)
  with opt-in toggle to read the live Kalshi account total
  (``kelly_sizing_use_kalshi_balance``). The toggle defaults off so
  an account-connection blip can't silently flip position sizes
  mid-session.
- **Rolling PnL fraction**: take the latest settled recommendation
  snapshot for each market in the rolling window and convert its
  per-contract ``realized_pnl`` into hypothetical dollar PnL using
  the configured cash notional and the recommendation's entry price.

## Why the assumed-notional approximation

``Prediction.realized_pnl`` today is the per-contract dollar PnL
(``payout - cost`` on a single contract). The product hasn't yet
persisted the operator's actual position size per prediction, so the
configured cash notional is converted to contracts at the row's
``suggested_price``::

    contracts = cash_notional / suggested_price
    dollar_pnl = realized_pnl * contracts

For example, losing a $100 stake bought at $0.10 is a $100 loss (1,000
contracts times -$0.10), not a $10 loss. The default notional ($100)
matches the typical demo-trading size. Operators who run larger sizes
can override it via settings.

Repeated model runs can persist several snapshots for one market, but
they do not represent separately approved positions. Only the latest
settled recommendation per market contributes to this advisory brake.
When actual per-prediction sizing or fills land, this approximation can
be retired in favor of realized position PnL (including fees).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Prediction
from app.services.kelly_sizing import (
    DEFAULT_DRAWDOWN_THRESHOLD,
    drawdown_brake_multiplier,
)

__all__ = [
    "DEFAULT_PNL_LOOKBACK_DAYS",
    "DrawdownBrakeSnapshot",
    "compute_drawdown_brake_snapshot",
    "compute_rolling_pnl_dollars",
    "compute_rolling_pnl_fraction",
    "resolve_bankroll",
]

# Mirrors ``kelly_sizing.DEFAULT_DROWDOWN_THRESHOLD``'s 7-day
# horizon — the brake is calibrated against a rolling-week PnL, so
# the query window has to match.
DEFAULT_PNL_LOOKBACK_DAYS = 7


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validated_notional(value: float) -> float:
    """Return a usable cash notional or fail without masking the brake."""
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(
            f"notional_per_pick_dollars must be finite and > 0, got {value!r}"
        )
    return float(value)


# -- Bankroll resolution ------------------------------------------------


def resolve_bankroll(
    db: Session,
    *,
    settings: Settings | None = None,
) -> float | None:
    """Return the operator's effective bankroll in dollars.

    Resolution order:

    1. If ``kelly_sizing_use_kalshi_balance`` is True AND the Kalshi
       account is connected AND the portfolio value is available,
       return ``portfolio_value_dollars`` (cash + open positions).
       This is what's actually at risk, so it's the most accurate
       bankroll metric.
    2. Else, return the operator-set ``kelly_sizing_bankroll_dollars``.
    3. Returns ``None`` only when the Kalshi-balance opt-in is on
       but the fetch failed AND the static setting is non-positive
       (defensive — non-positive bankroll is an obvious caller-
       config error that shouldn't silently fall through to
       zero-sized positions).

    The Kalshi snapshot lookup is wrapped in a try/except so a
    transient Kalshi error doesn't crash sizing — we fall through to
    the static setting and log the issue at the caller.
    """
    settings = settings or get_settings()

    if settings.kelly_sizing_use_kalshi_balance:
        try:
            # Lazy import: kalshi_account pulls in httpx + crypto
            # which the math-only path doesn't need.
            from app.services.kalshi_account import build_kalshi_account_snapshot  # noqa: PLC0415

            snapshot = build_kalshi_account_snapshot(db)
        except Exception:
            snapshot = None
        portfolio_value = None
        if snapshot is not None and snapshot.balance is not None:
            portfolio_value = snapshot.balance.portfolio_value_dollars
        if (
            portfolio_value is not None
            and portfolio_value > 0.0
            and math.isfinite(portfolio_value)
        ):
            return float(portfolio_value)

    static = settings.kelly_sizing_bankroll_dollars
    if static > 0.0 and math.isfinite(static):
        return float(static)
    return None


# -- Rolling PnL --------------------------------------------------------


def compute_rolling_pnl_dollars(
    db: Session,
    *,
    lookback_days: int = DEFAULT_PNL_LOOKBACK_DAYS,
    end_date: datetime | None = None,
    notional_per_pick_dollars: float | None = None,
) -> float:
    """Return hypothetical dollar PnL over the rolling window.

    Eligible rows have ``settled_at`` in
    ``[end_date - lookback_days, end_date]``, a non-null
    ``realized_pnl``, a positive entry price, and recommendation
    capture scope. A window function keeps the latest eligible row
    per market (``settled_at DESC, id DESC``), preventing refresh
    snapshots from fabricating multiple hypothetical positions.

    Each retained row is converted in SQL from per-contract PnL to
    dollars as ``realized_pnl * cash_notional / suggested_price``.
    Returns ``0.0`` (not ``None``) when no rows match — an empty
    window means no PnL signal, not missing data. Invalid notionals
    raise ``ValueError``; database errors also propagate to the caller.
    """
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be > 0, got {lookback_days}")
    notional = notional_per_pick_dollars
    if notional is None:
        notional = get_settings().kelly_sizing_assumed_notional_dollars
    notional = _validated_notional(notional)
    end_at = (
        _coerce_utc(end_date) if end_date is not None else datetime.now(timezone.utc)
    )
    start_at = end_at - timedelta(days=lookback_days)

    ranked = (
        select(
            Prediction.realized_pnl.label("realized_pnl"),
            Prediction.suggested_price.label("suggested_price"),
            func.row_number()
            .over(
                partition_by=Prediction.market_id,
                order_by=(Prediction.settled_at.desc(), Prediction.id.desc()),
            )
            .label("rn"),
        )
        .where(
            Prediction.settled_at >= start_at,
            Prediction.settled_at <= end_at,
            Prediction.realized_pnl.is_not(None),
            Prediction.suggested_price > 0.0,
            # Only operator-surfaced picks feed the drawdown brake. Coverage-scope
            # rows — markets the engine scored but never recommended, including the
            # force-YES-suppressed props booked at the model's own fair value —
            # must not drag the brake that sizes real recommendations.
            Prediction.capture_scope == "recommendation",
        )
        .subquery()
    )
    stmt = select(
        func.sum(ranked.c.realized_pnl * float(notional) / ranked.c.suggested_price)
    ).where(ranked.c.rn == 1)
    total = db.scalar(stmt)
    if total is None:
        return 0.0
    return float(total)


def compute_rolling_pnl_fraction(
    db: Session,
    *,
    bankroll: float,
    lookback_days: int = DEFAULT_PNL_LOOKBACK_DAYS,
    end_date: datetime | None = None,
    notional_per_pick_dollars: float | None = None,
) -> float:
    """Bankroll-relative rolling PnL — the input ``drawdown_brake_multiplier``
    expects in ``kelly_sizing.py``.

    Math: ``sum(realized_pnl * notional_per_pick / suggested_price)
    / bankroll``, after deduplicating to the latest settled row per
    market.

    ``realized_pnl`` is per-contract dollars. Dividing the cash
    notional by ``suggested_price`` first projects the number of
    contracts bought. Default notional comes from
    ``Settings.kelly_sizing_assumed_notional_dollars`` ($100) — see
    the module docstring for why this remains an approximation until
    actual position sizing or fills are persisted.

    Returns ``0.0`` when ``bankroll <= 0`` or invalid (defensive —
    the brake helper treats ``0.0`` as "no drawdown," which is the
    right behavior when bankroll is degenerate). An invalid cash
    notional raises ``ValueError`` because treating a configuration
    failure as flat PnL would silently disable the brake.
    """
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be > 0, got {lookback_days}")
    settings = get_settings()
    notional = notional_per_pick_dollars
    if notional is None:
        notional = settings.kelly_sizing_assumed_notional_dollars
    notional = _validated_notional(notional)
    if not math.isfinite(bankroll) or bankroll <= 0.0:
        return 0.0
    pnl_dollars = compute_rolling_pnl_dollars(
        db,
        lookback_days=lookback_days,
        end_date=end_date,
        notional_per_pick_dollars=notional,
    )
    return float(pnl_dollars / bankroll)


# -- Drawdown brake snapshot (Smarter #32) ------------------------------


@dataclass(frozen=True, slots=True)
class DrawdownBrakeSnapshot:
    """Operator-facing snapshot of the current drawdown brake state.

    Composes the three inputs the brake math reads (bankroll +
    rolling PnL fraction + threshold) with the resulting multiplier
    so an operator surface can render the state without re-deriving
    it. ``is_active`` is the binary "brake is dampening sizing right
    now" signal — true whenever ``brake_multiplier < 1.0``.

    Smarter #32 wires this into ``GET /positions`` so the portfolio
    panel can show a brake banner during a losing streak. The same
    snapshot also documents the math behind the
    ``brake_multiplier`` field already persisted on each
    recommendation's ``scoring_diagnostics.kelly_sizing`` block — a
    recommendation captured an hour ago can carry a stale brake
    value, but the snapshot is always current.
    """

    bankroll: float
    rolling_pnl_dollars: float
    rolling_pnl_fraction: float
    brake_multiplier: float
    threshold: float
    is_active: bool


def compute_drawdown_brake_snapshot(
    db: Session,
    *,
    lookback_days: int = DEFAULT_PNL_LOOKBACK_DAYS,
    end_date: datetime | None = None,
    settings: Settings | None = None,
) -> DrawdownBrakeSnapshot | None:
    """Compute the current drawdown brake state for operator display.

    Returns ``None`` when bankroll resolution fails — the brake is
    bankroll-relative, so without a bankroll there's nothing to
    surface. Operators get the same "configure bankroll first"
    affordance they'd see in the Kelly sizing block on a
    recommendation.

    ``threshold`` always reflects the brake's configured trigger
    point (default -5%); a positive rolling PnL still surfaces the
    snapshot with ``is_active=False`` so the UI can show the
    "no drawdown" baseline without ambiguity. An invalid assumed
    notional raises ``ValueError`` rather than appearing as flat PnL.
    """
    settings = settings or get_settings()
    notional = _validated_notional(settings.kelly_sizing_assumed_notional_dollars)
    bankroll = resolve_bankroll(db, settings=settings)
    if bankroll is None:
        return None
    # Derive the fraction from the dollars we already fetched instead
    # of calling ``compute_rolling_pnl_fraction`` — that would re-issue
    # the same aggregate query on every ``/positions`` poll.
    rolling_pnl_dollars = compute_rolling_pnl_dollars(
        db,
        lookback_days=lookback_days,
        end_date=end_date,
        notional_per_pick_dollars=notional,
    )
    rolling_pnl_fraction = float(rolling_pnl_dollars / bankroll)
    threshold = DEFAULT_DRAWDOWN_THRESHOLD
    multiplier = drawdown_brake_multiplier(
        rolling_pnl_fraction,
        threshold=threshold,
    )
    return DrawdownBrakeSnapshot(
        bankroll=round(bankroll, 2),
        rolling_pnl_dollars=round(rolling_pnl_dollars, 2),
        rolling_pnl_fraction=round(rolling_pnl_fraction, 4),
        brake_multiplier=round(multiplier, 4),
        threshold=round(threshold, 4),
        is_active=multiplier < 1.0,
    )
