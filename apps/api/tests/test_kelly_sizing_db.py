"""Tests for Smarter #9 phase 2 — DB inputs for fractional Kelly
sizing.

Covers bankroll resolution (static setting + Kalshi opt-in toggle)
and rolling PnL queries that feed
``kelly_sizing.drawdown_brake_multiplier``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import Settings
from app.models import Market, Prediction
from app.services.kelly_sizing_db import (
    DEFAULT_PNL_LOOKBACK_DAYS,
    compute_rolling_pnl_dollars,
    compute_rolling_pnl_fraction,
    resolve_bankroll,
)

_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


# -- Bankroll resolution -----------------------------------------------


def _settings(**overrides) -> Settings:
    """Build a Settings instance for resolution tests. Pydantic
    BaseSettings rejects unknown fields, so only pass the kelly_*
    knobs the resolver cares about."""
    defaults = {
        "kelly_sizing_bankroll_dollars": 1000.0,
        "kelly_sizing_use_kalshi_balance": False,
        "kelly_sizing_assumed_notional_dollars": 100.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_resolve_bankroll_returns_static_setting_by_default(db_session) -> None:
    """Kalshi opt-in off → resolver returns the operator setting."""
    bankroll = resolve_bankroll(db_session, settings=_settings(kelly_sizing_bankroll_dollars=5_000.0))
    assert bankroll == 5_000.0


def test_resolve_bankroll_returns_none_when_setting_non_positive(db_session) -> None:
    """A non-positive bankroll is an obvious config error; resolver
    returns None rather than letting zero-sized positions ship."""
    bankroll = resolve_bankroll(db_session, settings=_settings(kelly_sizing_bankroll_dollars=0.0))
    assert bankroll is None


def test_resolve_bankroll_reads_kalshi_when_opt_in_on(db_session) -> None:
    """When the opt-in is on, the resolver returns the Kalshi
    portfolio value instead of the static setting."""
    fake_snapshot = SimpleNamespace(
        balance=SimpleNamespace(portfolio_value_dollars=2_345.67),
    )
    with patch(
        "app.services.kalshi_account.build_kalshi_account_snapshot",
        return_value=fake_snapshot,
    ):
        bankroll = resolve_bankroll(
            db_session,
            settings=_settings(
                kelly_sizing_bankroll_dollars=1_000.0,
                kelly_sizing_use_kalshi_balance=True,
            ),
        )
    assert bankroll == pytest.approx(2_345.67)


def test_resolve_bankroll_falls_back_when_kalshi_unavailable(db_session) -> None:
    """Kalshi opt-in on but the snapshot fails → fall back to the
    static setting (the resolver must not crash sizing on a
    transient outage)."""
    with patch(
        "app.services.kalshi_account.build_kalshi_account_snapshot",
        side_effect=RuntimeError("kalshi 503"),
    ):
        bankroll = resolve_bankroll(
            db_session,
            settings=_settings(
                kelly_sizing_bankroll_dollars=750.0,
                kelly_sizing_use_kalshi_balance=True,
            ),
        )
    assert bankroll == 750.0


def test_resolve_bankroll_falls_back_when_portfolio_value_missing(db_session) -> None:
    """Snapshot returned but balance is None (account not yet
    syncing) → fall back."""
    fake_snapshot = SimpleNamespace(balance=None)
    with patch(
        "app.services.kalshi_account.build_kalshi_account_snapshot",
        return_value=fake_snapshot,
    ):
        bankroll = resolve_bankroll(
            db_session,
            settings=_settings(
                kelly_sizing_bankroll_dollars=750.0,
                kelly_sizing_use_kalshi_balance=True,
            ),
        )
    assert bankroll == 750.0


def test_resolve_bankroll_falls_back_when_portfolio_value_non_positive(db_session) -> None:
    """Portfolio value of zero or negative → fall back. A blown
    account shouldn't size everything down to nothing
    automatically; operator intervention is the right path."""
    fake_snapshot = SimpleNamespace(
        balance=SimpleNamespace(portfolio_value_dollars=0.0),
    )
    with patch(
        "app.services.kalshi_account.build_kalshi_account_snapshot",
        return_value=fake_snapshot,
    ):
        bankroll = resolve_bankroll(
            db_session,
            settings=_settings(
                kelly_sizing_bankroll_dollars=500.0,
                kelly_sizing_use_kalshi_balance=True,
            ),
        )
    assert bankroll == 500.0


# -- Rolling PnL dollars ------------------------------------------------


def _seed_market(db_session) -> Market:
    market = Market(ticker="NBA-T", sport_key="NBA", title="t", status="open", raw_data={})
    db_session.add(market)
    db_session.flush()
    return market


def _seed_settled_prediction(
    db_session,
    *,
    market: Market,
    realized_pnl: float | None,
    settled_at: datetime,
) -> None:
    db_session.add(
        Prediction(
            market_id=market.id, ticker=market.ticker, sport_key="NBA",
            market_title="t", side="yes", action="buy",
            suggested_price=0.5, edge=0.05, confidence=0.6, rationale="x",
            prediction_outcome="won", settlement_status="settled",
            captured_at=settled_at - timedelta(hours=2),
            settled_at=settled_at,
            realized_pnl=realized_pnl,
        )
    )
    db_session.flush()


def test_pnl_dollars_returns_zero_for_empty_window(db_session) -> None:
    assert compute_rolling_pnl_dollars(db_session, end_date=_NOW) == 0.0


def test_pnl_dollars_sums_realized_pnl(db_session) -> None:
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=0.20, settled_at=_NOW - timedelta(days=2),
    )
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=-0.10, settled_at=_NOW - timedelta(days=3),
    )
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=0.05, settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    total = compute_rolling_pnl_dollars(db_session, end_date=_NOW)
    assert total == pytest.approx(0.15)


def test_pnl_dollars_respects_lookback_window(db_session) -> None:
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=0.50, settled_at=_NOW - timedelta(days=2),
    )
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=1.00, settled_at=_NOW - timedelta(days=20),
    )
    db_session.commit()
    # Default 7-day window: only the recent row counts.
    short = compute_rolling_pnl_dollars(db_session, end_date=_NOW)
    long = compute_rolling_pnl_dollars(db_session, end_date=_NOW, lookback_days=30)
    assert short == pytest.approx(0.50)
    assert long == pytest.approx(1.50)


def test_pnl_dollars_skips_null_realized_pnl(db_session) -> None:
    """Rows with null realized_pnl (pending / not-yet-settled) are
    excluded — they have no PnL signal."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=None, settled_at=_NOW - timedelta(days=1),
    )
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=0.10, settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    total = compute_rolling_pnl_dollars(db_session, end_date=_NOW)
    assert total == pytest.approx(0.10)


def test_pnl_dollars_rejects_non_positive_lookback(db_session) -> None:
    with pytest.raises(ValueError, match="lookback_days"):
        compute_rolling_pnl_dollars(db_session, lookback_days=0, end_date=_NOW)


# -- Rolling PnL fraction ----------------------------------------------


def test_pnl_fraction_applies_notional_multiplier(db_session) -> None:
    """fraction = sum(realized_pnl) * notional / bankroll."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=-0.20, settled_at=_NOW - timedelta(days=1),
    )
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=-0.30, settled_at=_NOW - timedelta(days=2),
    )
    db_session.commit()
    fraction = compute_rolling_pnl_fraction(
        db_session,
        bankroll=1000.0,
        end_date=_NOW,
        notional_per_pick_dollars=100.0,
    )
    # sum = -0.5; -0.5 * 100 / 1000 = -0.05 (5% drawdown).
    assert fraction == pytest.approx(-0.05)


def test_pnl_fraction_returns_zero_for_invalid_bankroll(db_session) -> None:
    """Zero / negative / NaN bankroll → fraction 0 (the brake
    helper treats 0 as 'no drawdown', which is the right behavior
    when bankroll is degenerate; sizing shouldn't be downscaled
    further by a bookkeeping error)."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=-0.50, settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    assert compute_rolling_pnl_fraction(db_session, bankroll=0.0, end_date=_NOW) == 0.0
    assert compute_rolling_pnl_fraction(db_session, bankroll=-100.0, end_date=_NOW) == 0.0
    assert compute_rolling_pnl_fraction(db_session, bankroll=float("nan"), end_date=_NOW) == 0.0


def test_pnl_fraction_returns_zero_for_invalid_notional(db_session) -> None:
    """Non-positive / non-finite notional → 0 (same defensive
    behavior as bankroll)."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=-0.50, settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    fraction = compute_rolling_pnl_fraction(
        db_session, bankroll=1000.0, end_date=_NOW, notional_per_pick_dollars=0.0,
    )
    assert fraction == 0.0


def test_pnl_fraction_defaults_to_settings_notional(db_session) -> None:
    """When ``notional_per_pick_dollars`` isn't passed, the helper
    reads ``Settings.kelly_sizing_assumed_notional_dollars``
    (default $100). Verifies the env-default flow."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session, market=market, realized_pnl=-0.50, settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    fraction = compute_rolling_pnl_fraction(db_session, bankroll=1000.0, end_date=_NOW)
    # -0.5 * 100 / 1000 = -0.05.
    assert fraction == pytest.approx(-0.05)


def test_pnl_fraction_returns_zero_for_empty_window(db_session) -> None:
    assert compute_rolling_pnl_fraction(
        db_session, bankroll=1000.0, end_date=_NOW,
    ) == 0.0


def test_default_lookback_matches_drawdown_brake_horizon() -> None:
    """The drawdown brake in ``kelly_sizing.py`` is calibrated
    against a 7-day window — this helper's default must match or
    the brake fires on the wrong horizon."""
    assert DEFAULT_PNL_LOOKBACK_DAYS == 7
