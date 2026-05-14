"""Unit tests for Smarter #3 — closing-line value (``apps/api/app/services/clv.py``).

Covers:
- ``_snapshot_yes_price`` mid-vs-last preference + invalid-range guards.
- ``closing_yes_price_for_market`` DB-backed lookup with ``before`` window.
- ``compute_clv`` sign convention for YES vs NO + None-on-bad-input.
- ``average_clv`` aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Market, MarketSnapshot
from app.services import clv


@pytest.fixture()
def sample_market_factory(db_session):
    """Create test ``Market`` rows with unique tickers."""

    counter = {"value": 0}

    def _factory(**overrides) -> Market:
        counter["value"] += 1
        defaults = {
            "ticker": f"TEST-CLV-{counter['value']}",
            "title": f"Test CLV market {counter['value']}",
            "status": "open",
        }
        defaults.update(overrides)
        market = Market(**defaults)
        db_session.add(market)
        db_session.flush()
        return market

    return _factory


# -- _snapshot_yes_price ------------------------------------------------------


@dataclass
class _Snap:
    yes_bid: float | None = None
    yes_ask: float | None = None
    last_price: float | None = None


def test_snapshot_yes_price_prefers_mid_when_both_sides_present() -> None:
    snap = _Snap(yes_bid=0.40, yes_ask=0.44, last_price=0.30)
    assert clv._snapshot_yes_price(snap) == pytest.approx(0.42)


def test_snapshot_yes_price_falls_back_to_last_when_bid_missing() -> None:
    snap = _Snap(yes_bid=None, yes_ask=0.50, last_price=0.55)
    assert clv._snapshot_yes_price(snap) == pytest.approx(0.55)


def test_snapshot_yes_price_falls_back_to_last_when_ask_missing() -> None:
    snap = _Snap(yes_bid=0.50, yes_ask=None, last_price=0.55)
    assert clv._snapshot_yes_price(snap) == pytest.approx(0.55)


def test_snapshot_yes_price_returns_none_when_all_fields_missing() -> None:
    assert clv._snapshot_yes_price(_Snap()) is None


def test_snapshot_yes_price_rejects_out_of_range_mid() -> None:
    # A pathological snapshot can have widely divergent bid/ask; if their
    # mid lands outside [0,1], reject and try the next signal.
    snap = _Snap(yes_bid=1.0, yes_ask=2.0, last_price=0.50)
    assert clv._snapshot_yes_price(snap) == pytest.approx(0.50)


def test_snapshot_yes_price_rejects_out_of_range_last_price() -> None:
    snap = _Snap(yes_bid=None, yes_ask=None, last_price=1.5)
    assert clv._snapshot_yes_price(snap) is None


def test_snapshot_yes_price_rejects_non_finite_inputs() -> None:
    snap = _Snap(yes_bid=float("nan"), yes_ask=0.5, last_price=float("inf"))
    assert clv._snapshot_yes_price(snap) is None


# -- closing_yes_price_for_market --------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_closing_yes_price_returns_latest_snapshot_value(db_session, sample_market_factory) -> None:
    market = sample_market_factory()
    now = _now()
    db_session.add_all(
        [
            MarketSnapshot(
                market_id=market.id,
                captured_at=now - timedelta(hours=2),
                yes_bid=0.30,
                yes_ask=0.34,
            ),
            MarketSnapshot(
                market_id=market.id,
                captured_at=now - timedelta(hours=1),
                yes_bid=0.40,
                yes_ask=0.44,
            ),
        ]
    )
    db_session.flush()
    assert clv.closing_yes_price_for_market(db_session, market.id) == pytest.approx(0.42)


def test_closing_yes_price_honors_before_window(db_session, sample_market_factory) -> None:
    market = sample_market_factory()
    now = _now()
    cutoff = now - timedelta(minutes=30)
    db_session.add_all(
        [
            MarketSnapshot(
                market_id=market.id,
                captured_at=now - timedelta(hours=2),
                yes_bid=0.30,
                yes_ask=0.34,
            ),
            # After the cutoff — must be excluded.
            MarketSnapshot(
                market_id=market.id,
                captured_at=now,
                yes_bid=0.80,
                yes_ask=0.82,
            ),
        ]
    )
    db_session.flush()
    assert clv.closing_yes_price_for_market(db_session, market.id, before=cutoff) == pytest.approx(0.32)


def test_closing_yes_price_returns_none_when_no_snapshots(db_session, sample_market_factory) -> None:
    market = sample_market_factory()
    assert clv.closing_yes_price_for_market(db_session, market.id) is None


def test_closing_yes_price_other_markets_do_not_leak(db_session, sample_market_factory) -> None:
    target = sample_market_factory()
    other = sample_market_factory()
    db_session.add(
        MarketSnapshot(
            market_id=other.id,
            captured_at=_now(),
            yes_bid=0.99,
            yes_ask=0.99,
        )
    )
    db_session.flush()
    assert clv.closing_yes_price_for_market(db_session, target.id) is None


# -- compute_clv --------------------------------------------------------------


@pytest.mark.parametrize(
    "side,suggested,closing,expected",
    [
        # YES pick: closing 0.55 vs entry 0.40 → +0.15 (line moved toward YES, sika beat the close)
        ("yes", 0.40, 0.55, 0.15),
        # YES pick: closing 0.30 vs entry 0.40 → -0.10 (line moved away from YES)
        ("yes", 0.40, 0.30, -0.10),
        # NO pick: closing 0.30 vs entry NO=0.60. Closing NO = 1 - 0.30 = 0.70. CLV = 0.70 - 0.60 = +0.10.
        ("no", 0.60, 0.30, 0.10),
        # NO pick: closing 0.80 vs entry NO=0.60. Closing NO = 0.20. CLV = 0.20 - 0.60 = -0.40.
        ("no", 0.60, 0.80, -0.40),
        # Tied: no movement.
        ("yes", 0.50, 0.50, 0.0),
        ("no", 0.50, 0.50, 0.0),
    ],
)
def test_compute_clv_sign_and_magnitude(
    side: str, suggested: float, closing: float, expected: float
) -> None:
    result = clv.compute_clv(side=side, suggested_price=suggested, closing_yes_price=closing)
    assert result == pytest.approx(expected)


def test_compute_clv_returns_none_when_suggested_missing() -> None:
    assert clv.compute_clv(side="yes", suggested_price=None, closing_yes_price=0.5) is None


def test_compute_clv_returns_none_when_closing_missing() -> None:
    assert clv.compute_clv(side="yes", suggested_price=0.4, closing_yes_price=None) is None


@pytest.mark.parametrize("bad_closing", [-0.01, 1.01, 1.5, -1.0])
def test_compute_clv_rejects_out_of_range_closing(bad_closing: float) -> None:
    assert clv.compute_clv(side="yes", suggested_price=0.4, closing_yes_price=bad_closing) is None


@pytest.mark.parametrize("bad_suggested", [-0.01, 1.01, 1.5])
def test_compute_clv_rejects_out_of_range_suggested(bad_suggested: float) -> None:
    assert clv.compute_clv(side="yes", suggested_price=bad_suggested, closing_yes_price=0.5) is None


@pytest.mark.parametrize("bad_side", ["", "buy", "sell", "YES", " yes "])
def test_compute_clv_rejects_or_normalizes_side(bad_side: str) -> None:
    result = clv.compute_clv(side=bad_side, suggested_price=0.4, closing_yes_price=0.5)
    if bad_side.strip().lower() in ("yes", "no"):
        assert result is not None
    else:
        assert result is None


# -- average_clv -------------------------------------------------------------


@dataclass
class _Row:
    closing_line_value: float | None = None
    extras: dict = field(default_factory=dict)


def test_average_clv_skips_rows_without_value() -> None:
    rows = [_Row(closing_line_value=0.10), _Row(closing_line_value=None), _Row(closing_line_value=0.20)]
    assert clv.average_clv(rows) == pytest.approx(0.15)


def test_average_clv_returns_none_when_all_missing() -> None:
    rows = [_Row(closing_line_value=None), _Row(closing_line_value=None)]
    assert clv.average_clv(rows) is None


def test_average_clv_returns_none_on_empty_input() -> None:
    assert clv.average_clv([]) is None


def test_average_clv_skips_non_finite() -> None:
    rows = [_Row(closing_line_value=0.10), _Row(closing_line_value=float("nan")), _Row(closing_line_value=0.20)]
    assert clv.average_clv(rows) == pytest.approx(0.15)


# -- settlement integration ---------------------------------------------------


def test_settlement_writes_closing_line_value(db_session) -> None:
    """End-to-end: settling a prediction should snapshot the closing YES
    price and the signed CLV from the latest pre-close ``MarketSnapshot``."""

    from tests.test_predictions import FakeSettlementClient, _create_prediction
    from app.services.predictions import settle_predictions

    prediction = _create_prediction(
        db_session,
        ticker="KXCLV-INTEG-1",
        side="yes",
        suggested_price=0.40,
    )
    # Add a market snapshot that lands BEFORE close_time so the closing-line
    # lookup picks it up.
    close_time = datetime(2026, 4, 2, 3, 0, tzinfo=timezone.utc)
    prediction.market.close_time = close_time
    db_session.add(
        MarketSnapshot(
            market_id=prediction.market_id,
            captured_at=close_time - timedelta(minutes=5),
            yes_bid=0.54,
            yes_ask=0.56,  # mid = 0.55
        )
    )
    db_session.commit()

    summary = settle_predictions(
        db_session,
        client=FakeSettlementClient(
            {
                prediction.ticker: {
                    "ticker": prediction.ticker,
                    "status": "settled",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                    "settlement_ts": "2026-04-02T03:00:00Z",
                }
            }
        ),
    )
    db_session.commit()

    assert summary["won"] == 1
    db_session.refresh(prediction)
    assert prediction.closing_yes_price == pytest.approx(0.55)
    # YES pick, entry 0.40, close 0.55 → +0.15 CLV (sika beat the close)
    assert prediction.closing_line_value == pytest.approx(0.15)


def test_settlement_leaves_clv_alone_when_no_snapshot_available(db_session) -> None:
    """Markets with no captured history just keep ``closing_*`` as None;
    settlement does not crash or write garbage."""

    from tests.test_predictions import FakeSettlementClient, _create_prediction
    from app.services.predictions import settle_predictions

    prediction = _create_prediction(
        db_session,
        ticker="KXCLV-NO-HISTORY-1",
        side="yes",
        suggested_price=0.40,
    )
    db_session.commit()

    settle_predictions(
        db_session,
        client=FakeSettlementClient(
            {
                prediction.ticker: {
                    "ticker": prediction.ticker,
                    "status": "settled",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                    "settlement_ts": "2026-04-02T03:00:00Z",
                }
            }
        ),
    )
    db_session.commit()
    db_session.refresh(prediction)
    assert prediction.closing_yes_price is None
    assert prediction.closing_line_value is None


def test_settlement_does_not_overwrite_existing_clv(db_session) -> None:
    """Codex pattern-1 catch: re-settling a row that already carries a CLV
    should leave the original close-time value intact, not snap to a fresh
    (post-close) snapshot. The first close captured is the authoritative one."""

    from tests.test_predictions import FakeSettlementClient, _create_prediction
    from app.services.predictions import settle_predictions

    prediction = _create_prediction(
        db_session,
        ticker="KXCLV-IDEMPOTENT-1",
        side="yes",
        suggested_price=0.40,
    )
    prediction.closing_yes_price = 0.55
    prediction.closing_line_value = 0.15
    db_session.commit()

    # Add a NEW snapshot post-close — if the code overwrites, this would
    # replace 0.55 with 0.80.
    close_time = datetime(2026, 4, 2, 3, 0, tzinfo=timezone.utc)
    prediction.market.close_time = close_time
    db_session.add(
        MarketSnapshot(
            market_id=prediction.market_id,
            captured_at=close_time - timedelta(minutes=1),
            yes_bid=0.79,
            yes_ask=0.81,
        )
    )
    db_session.commit()

    settle_predictions(
        db_session,
        client=FakeSettlementClient(
            {
                prediction.ticker: {
                    "ticker": prediction.ticker,
                    "status": "settled",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                    "settlement_ts": "2026-04-02T03:00:00Z",
                }
            }
        ),
    )
    db_session.commit()
    db_session.refresh(prediction)
    # Authoritative original close preserved.
    assert prediction.closing_yes_price == pytest.approx(0.55)
    assert prediction.closing_line_value == pytest.approx(0.15)
