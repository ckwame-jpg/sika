"""Regression tests for candlestick price parsing.

Live Kalshi candlesticks nest OHLC under a ``price`` object
(``close_dollars`` / ``mean_dollars``). The parser previously read only
non-existent top-level keys, so every history point was price-less.
"""

from app.services.market_history import _candlestick_points


def test_candlestick_points_reads_nested_price_object():
    payload = {
        "candlesticks": [
            {
                "end_period_time": "2026-07-09T00:00:00Z",
                "price": {"close_dollars": 0.67, "mean_dollars": 0.66},
            }
        ]
    }
    points = _candlestick_points(payload, source="candlesticks")
    assert len(points) == 1
    assert points[0]["last_price"] == 0.67
    assert points[0]["mean_price"] == 0.66


def test_candlestick_points_falls_back_to_top_level_dollars():
    payload = {
        "candlesticks": [
            {"end_period_time": "2026-07-09T00:00:00Z", "close_price_dollars": 0.42}
        ]
    }
    points = _candlestick_points(payload, source="candlesticks")
    assert points[0]["last_price"] == 0.42


def test_candlestick_points_priceless_payload_is_none_not_crash():
    payload = {"candlesticks": [{"end_period_time": "2026-07-09T00:00:00Z", "price": {}}]}
    points = _candlestick_points(payload, source="candlesticks")
    assert len(points) == 1
    assert points[0]["last_price"] is None
    assert points[0]["mean_price"] is None
