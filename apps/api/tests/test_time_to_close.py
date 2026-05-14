"""Unit tests for Smarter #24 — ``time_to_close_minutes`` helpers.

Covers the helper in both ``apps/api/app/api/routes.py`` and
``apps/api/app/services/trade_desk.py`` — they're intentionally duplicated
(small, hot-path) but must agree on semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.api.routes import _time_to_close_minutes as routes_helper
from app.services.trade_desk import _time_to_close_minutes as trade_desk_helper


@dataclass
class _Market:
    close_time: datetime | None = None


_NOW = datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("helper", [routes_helper, trade_desk_helper])
class TestTimeToCloseMinutes:
    def test_returns_none_when_close_time_missing(self, helper) -> None:
        assert helper(_Market(close_time=None), now=_NOW) is None

    def test_returns_minutes_for_future_close(self, helper) -> None:
        market = _Market(close_time=_NOW + timedelta(minutes=45))
        assert helper(market, now=_NOW) == 45

    def test_floors_seconds_to_minutes(self, helper) -> None:
        market = _Market(close_time=_NOW + timedelta(minutes=45, seconds=58))
        # 45 min 58 sec → 45 (floor), not rounded to 46.
        assert helper(market, now=_NOW) == 45

    def test_clamps_past_close_to_zero(self, helper) -> None:
        market = _Market(close_time=_NOW - timedelta(minutes=10))
        assert helper(market, now=_NOW) == 0

    def test_zero_seconds_to_close_returns_zero(self, helper) -> None:
        market = _Market(close_time=_NOW)
        assert helper(market, now=_NOW) == 0

    def test_far_future_close_in_minutes(self, helper) -> None:
        market = _Market(close_time=_NOW + timedelta(days=2, hours=3))
        # 2d 3h = 48*60 + 3*60 = 2880 + 180 = 3060
        assert helper(market, now=_NOW) == 3060

    def test_handles_none_market(self, helper) -> None:
        assert helper(None, now=_NOW) is None


def test_routes_and_trade_desk_helpers_agree() -> None:
    """Both helpers exist on hot paths — they must produce identical
    results for the same input or we have a divergence bug waiting."""

    samples = [
        _Market(close_time=None),
        _Market(close_time=_NOW + timedelta(minutes=15)),
        _Market(close_time=_NOW - timedelta(minutes=5)),
        _Market(close_time=_NOW + timedelta(hours=12)),
    ]
    for sample in samples:
        assert routes_helper(sample, now=_NOW) == trade_desk_helper(sample, now=_NOW)
