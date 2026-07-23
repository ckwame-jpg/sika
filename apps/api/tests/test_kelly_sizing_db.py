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
from app.services.kelly_sizing import DEFAULT_DRAWDOWN_THRESHOLD
from app.services.kelly_sizing_db import (
    DEFAULT_PNL_LOOKBACK_DAYS,
    DrawdownBrakeSnapshot,
    compute_drawdown_brake_snapshot,
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
    bankroll = resolve_bankroll(
        db_session, settings=_settings(kelly_sizing_bankroll_dollars=5_000.0)
    )
    assert bankroll == 5_000.0


def test_resolve_bankroll_returns_none_when_setting_non_positive(db_session) -> None:
    """A non-positive bankroll is an obvious config error; resolver
    returns None rather than letting zero-sized positions ship."""
    bankroll = resolve_bankroll(
        db_session, settings=_settings(kelly_sizing_bankroll_dollars=0.0)
    )
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


def test_resolve_bankroll_falls_back_when_portfolio_value_non_positive(
    db_session,
) -> None:
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


def _seed_market(db_session, *, ticker: str = "NBA-T") -> Market:
    market = Market(
        ticker=ticker, sport_key="NBA", title="t", status="open", raw_data={}
    )
    db_session.add(market)
    db_session.flush()
    return market


def _seed_settled_prediction(
    db_session,
    *,
    market: Market,
    realized_pnl: float | None,
    settled_at: datetime,
    capture_scope: str = "recommendation",
    suggested_price: float = 0.5,
) -> Prediction:
    prediction = Prediction(
        market_id=market.id,
        ticker=market.ticker,
        sport_key="NBA",
        market_title="t",
        side="yes",
        action="buy",
        suggested_price=suggested_price,
        edge=0.05,
        confidence=0.6,
        rationale="x",
        prediction_outcome="won",
        settlement_status="settled",
        captured_at=settled_at - timedelta(hours=2),
        settled_at=settled_at,
        realized_pnl=realized_pnl,
        capture_scope=capture_scope,
    )
    db_session.add(prediction)
    db_session.flush()
    return prediction


def test_pnl_dollars_returns_zero_for_empty_window(db_session) -> None:
    assert compute_rolling_pnl_dollars(db_session, end_date=_NOW) == 0.0


def test_pnl_dollars_sums_realized_pnl(db_session) -> None:
    market_a = _seed_market(db_session, ticker="NBA-A")
    market_b = _seed_market(db_session, ticker="NBA-B")
    market_c = _seed_market(db_session, ticker="NBA-C")
    _seed_settled_prediction(
        db_session,
        market=market_a,
        realized_pnl=0.20,
        settled_at=_NOW - timedelta(days=2),
    )
    _seed_settled_prediction(
        db_session,
        market=market_b,
        realized_pnl=-0.10,
        settled_at=_NOW - timedelta(days=3),
    )
    _seed_settled_prediction(
        db_session,
        market=market_c,
        realized_pnl=0.05,
        settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    total = compute_rolling_pnl_dollars(db_session, end_date=_NOW)
    # At a $0.50 entry, a $100 cash notional buys 200 contracts:
    # (0.20 - 0.10 + 0.05) * 200 = $30.
    assert total == pytest.approx(30.0)


def test_pnl_dollars_respects_lookback_window(db_session) -> None:
    recent_market = _seed_market(db_session, ticker="NBA-RECENT")
    old_market = _seed_market(db_session, ticker="NBA-OLD")
    _seed_settled_prediction(
        db_session,
        market=recent_market,
        realized_pnl=0.50,
        settled_at=_NOW - timedelta(days=2),
    )
    _seed_settled_prediction(
        db_session,
        market=old_market,
        realized_pnl=1.00,
        settled_at=_NOW - timedelta(days=20),
    )
    db_session.commit()
    # Default 7-day window: only the recent row counts.
    short = compute_rolling_pnl_dollars(db_session, end_date=_NOW)
    long = compute_rolling_pnl_dollars(db_session, end_date=_NOW, lookback_days=30)
    assert short == pytest.approx(100.0)
    assert long == pytest.approx(300.0)


def test_pnl_dollars_skips_null_realized_pnl(db_session) -> None:
    """Rows with null realized_pnl (pending / not-yet-settled) are
    excluded — they have no PnL signal."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=None,
        settled_at=_NOW - timedelta(days=1),
    )
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=0.10,
        settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    total = compute_rolling_pnl_dollars(db_session, end_date=_NOW)
    assert total == pytest.approx(20.0)


def test_pnl_dollars_excludes_coverage_scope(db_session) -> None:
    """Coverage-scope rows (markets scored but never recommended, including
    force-YES-suppressed props booked at fair value) must not feed the drawdown
    brake that sizes real recommendations."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=0.30,
        settled_at=_NOW - timedelta(days=1),
        capture_scope="recommendation",
    )
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=-5.0,
        settled_at=_NOW - timedelta(days=1),
        capture_scope="coverage",
    )
    db_session.commit()
    total = compute_rolling_pnl_dollars(db_session, end_date=_NOW)
    assert total == pytest.approx(60.0)  # coverage row excluded


def test_pnl_dollars_equal_cash_losses_are_price_invariant(db_session) -> None:
    """Equal cash stakes lose equal dollars at different entry prices.

    A $100 stake is 1,000 contracts at $0.10 and 125 contracts at
    $0.80. Losing either position costs $100, not $10 and $80.
    """
    cheap_market = _seed_market(db_session, ticker="NBA-CHEAP")
    expensive_market = _seed_market(db_session, ticker="NBA-EXPENSIVE")
    _seed_settled_prediction(
        db_session,
        market=cheap_market,
        realized_pnl=-0.10,
        suggested_price=0.10,
        settled_at=_NOW - timedelta(days=1),
    )
    _seed_settled_prediction(
        db_session,
        market=expensive_market,
        realized_pnl=-0.80,
        suggested_price=0.80,
        settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()

    total = compute_rolling_pnl_dollars(db_session, end_date=_NOW)

    assert total == pytest.approx(-200.0)


def test_pnl_dollars_counts_latest_snapshot_per_market_once(db_session) -> None:
    """Refresh snapshots for one market represent one hypothetical stake."""
    market = _seed_market(db_session)
    for days_ago in (3, 2, 1):
        _seed_settled_prediction(
            db_session,
            market=market,
            realized_pnl=-0.10,
            suggested_price=0.10,
            settled_at=_NOW - timedelta(days=days_ago),
        )
    db_session.commit()

    total = compute_rolling_pnl_dollars(db_session, end_date=_NOW)

    assert total == pytest.approx(-100.0)


def test_pnl_dollars_skips_non_positive_entry_price(db_session) -> None:
    """Invalid entry prices cannot be converted to contract counts."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=-0.10,
        suggested_price=0.0,
        settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()

    assert compute_rolling_pnl_dollars(db_session, end_date=_NOW) == 0.0


def test_pnl_dollars_propagates_database_errors(db_session) -> None:
    """A failed source query must not masquerade as a flat PnL window."""
    with patch.object(
        db_session, "scalar", side_effect=RuntimeError("database unavailable")
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            compute_rolling_pnl_dollars(db_session, end_date=_NOW)


def test_pnl_dollars_rejects_non_positive_lookback(db_session) -> None:
    with pytest.raises(ValueError, match="lookback_days"):
        compute_rolling_pnl_dollars(db_session, lookback_days=0, end_date=_NOW)


@pytest.mark.parametrize("notional", [0.0, -1.0, float("nan"), float("inf")])
def test_pnl_dollars_rejects_invalid_notional(db_session, notional: float) -> None:
    with pytest.raises(ValueError, match="notional_per_pick_dollars"):
        compute_rolling_pnl_dollars(
            db_session,
            end_date=_NOW,
            notional_per_pick_dollars=notional,
        )


# -- Rolling PnL fraction ----------------------------------------------


def test_pnl_fraction_applies_notional_multiplier(db_session) -> None:
    """fraction = dollar PnL after price conversion / bankroll."""
    market_a = _seed_market(db_session, ticker="NBA-A")
    market_b = _seed_market(db_session, ticker="NBA-B")
    _seed_settled_prediction(
        db_session,
        market=market_a,
        realized_pnl=-0.20,
        settled_at=_NOW - timedelta(days=1),
    )
    _seed_settled_prediction(
        db_session,
        market=market_b,
        realized_pnl=-0.30,
        settled_at=_NOW - timedelta(days=2),
    )
    db_session.commit()
    fraction = compute_rolling_pnl_fraction(
        db_session,
        bankroll=1000.0,
        end_date=_NOW,
        notional_per_pick_dollars=100.0,
    )
    # At $0.50, the two $100 notionals buy 200 contracts each:
    # (-0.2 - 0.3) * 200 / $1,000 = -0.10.
    assert fraction == pytest.approx(-0.10)


def test_pnl_fraction_returns_zero_for_invalid_bankroll(db_session) -> None:
    """Zero / negative / NaN bankroll → fraction 0 (the brake
    helper treats 0 as 'no drawdown', which is the right behavior
    when bankroll is degenerate; sizing shouldn't be downscaled
    further by a bookkeeping error)."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=-0.50,
        settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    assert compute_rolling_pnl_fraction(db_session, bankroll=0.0, end_date=_NOW) == 0.0
    assert (
        compute_rolling_pnl_fraction(db_session, bankroll=-100.0, end_date=_NOW) == 0.0
    )
    assert (
        compute_rolling_pnl_fraction(db_session, bankroll=float("nan"), end_date=_NOW)
        == 0.0
    )


@pytest.mark.parametrize("notional", [0.0, -1.0, float("nan"), float("inf")])
def test_pnl_fraction_rejects_invalid_notional(db_session, notional: float) -> None:
    """A bad notional must not masquerade as a flat PnL window."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=-0.50,
        settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    with pytest.raises(ValueError, match="notional_per_pick_dollars"):
        compute_rolling_pnl_fraction(
            db_session,
            bankroll=1000.0,
            end_date=_NOW,
            notional_per_pick_dollars=notional,
        )


def test_pnl_fraction_defaults_to_settings_notional(db_session) -> None:
    """When ``notional_per_pick_dollars`` isn't passed, the helper
    reads ``Settings.kelly_sizing_assumed_notional_dollars``
    (default $100). Verifies the env-default flow."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=-0.50,
        settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    fraction = compute_rolling_pnl_fraction(db_session, bankroll=1000.0, end_date=_NOW)
    # -0.5 * (100 / 0.5) / 1000 = -0.10.
    assert fraction == pytest.approx(-0.10)


def test_pnl_fraction_returns_zero_for_empty_window(db_session) -> None:
    assert (
        compute_rolling_pnl_fraction(
            db_session,
            bankroll=1000.0,
            end_date=_NOW,
        )
        == 0.0
    )


def test_default_lookback_matches_drawdown_brake_horizon() -> None:
    """The drawdown brake in ``kelly_sizing.py`` is calibrated
    against a 7-day window — this helper's default must match or
    the brake fires on the wrong horizon."""
    assert DEFAULT_PNL_LOOKBACK_DAYS == 7


# -- Drawdown brake snapshot (Smarter #32) -----------------------------


def test_drawdown_brake_snapshot_inactive_with_no_pnl(db_session) -> None:
    """No settled rows → rolling PnL is zero → multiplier 1.0 →
    ``is_active`` False. The snapshot still surfaces so the UI can
    render the "no drawdown" baseline."""
    snapshot = compute_drawdown_brake_snapshot(
        db_session,
        end_date=_NOW,
        settings=_settings(),
    )
    assert isinstance(snapshot, DrawdownBrakeSnapshot)
    assert snapshot.bankroll == 1000.0
    assert snapshot.rolling_pnl_dollars == 0.0
    assert snapshot.rolling_pnl_fraction == 0.0
    assert snapshot.brake_multiplier == 1.0
    assert snapshot.threshold == pytest.approx(DEFAULT_DRAWDOWN_THRESHOLD)
    assert snapshot.is_active is False


def test_five_low_price_losses_trigger_deep_drawdown_floor(db_session) -> None:
    """Five $100 losses at $0.10 are a $500 / 50% drawdown.

    The old conversion treated each cash stake as 100 contracts and
    reported only a $50 / 5% drawdown, leaving the brake disengaged.
    """
    for index in range(5):
        market = _seed_market(db_session, ticker=f"NBA-LOSS-{index}")
        _seed_settled_prediction(
            db_session,
            market=market,
            realized_pnl=-0.10,
            suggested_price=0.10,
            settled_at=_NOW - timedelta(days=index + 1),
        )
    db_session.commit()

    snapshot = compute_drawdown_brake_snapshot(
        db_session,
        end_date=_NOW,
        settings=_settings(),
    )

    assert snapshot is not None
    assert snapshot.rolling_pnl_dollars == pytest.approx(-500.0)
    assert snapshot.rolling_pnl_fraction == pytest.approx(-0.50)
    assert snapshot.brake_multiplier == pytest.approx(0.25)
    assert snapshot.is_active is True


def test_drawdown_brake_snapshot_returns_none_when_no_bankroll(db_session) -> None:
    """Operator hasn't configured a bankroll → snapshot is None.
    The endpoint passes this through so the UI hides the brake panel
    rather than showing a brake-against-zero number."""
    snapshot = compute_drawdown_brake_snapshot(
        db_session,
        end_date=_NOW,
        settings=_settings(kelly_sizing_bankroll_dollars=0.0),
    )
    assert snapshot is None


@pytest.mark.parametrize("notional", [0.0, -1.0, float("nan"), float("inf")])
def test_drawdown_brake_snapshot_rejects_invalid_notional(
    db_session,
    notional: float,
) -> None:
    """Invalid operator config must surface instead of disabling the brake."""
    with pytest.raises(ValueError, match="notional_per_pick_dollars"):
        compute_drawdown_brake_snapshot(
            db_session,
            end_date=_NOW,
            settings=_settings(kelly_sizing_assumed_notional_dollars=notional),
        )


def test_drawdown_brake_snapshot_positive_pnl_keeps_brake_off(db_session) -> None:
    """A profitable week never engages the brake (multiplier 1.0)."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=0.40,
        settled_at=_NOW - timedelta(days=1),
    )
    db_session.commit()
    snapshot = compute_drawdown_brake_snapshot(
        db_session,
        end_date=_NOW,
        settings=_settings(),
    )
    assert snapshot is not None
    assert snapshot.rolling_pnl_dollars == pytest.approx(80.0)
    assert snapshot.rolling_pnl_fraction > 0.0
    assert snapshot.brake_multiplier == 1.0
    assert snapshot.is_active is False


def test_drawdown_brake_snapshot_respects_lookback_window(db_session) -> None:
    """Rows outside the lookback window aren't counted in the
    fraction. Defaults to 7 days."""
    market = _seed_market(db_session)
    _seed_settled_prediction(
        db_session,
        market=market,
        realized_pnl=-1.00,
        settled_at=_NOW - timedelta(days=20),
    )
    db_session.commit()
    snapshot = compute_drawdown_brake_snapshot(
        db_session,
        end_date=_NOW,
        settings=_settings(),
    )
    assert snapshot is not None
    assert snapshot.rolling_pnl_dollars == 0.0
    assert snapshot.is_active is False
