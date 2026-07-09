from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.kalshi import KalshiPublicClient, parse_price_dollars
from app.models import Market, MarketSnapshot


RANGE_CONFIG = {
    "1D": {"window": timedelta(days=1), "period_minutes": 60},
    "7D": {"window": timedelta(days=7), "period_minutes": 1440},
}
SOURCE_ORDER = {
    "historical_candlestick": 0,
    "live_candlestick": 1,
    "local_snapshot": 2,
}


def build_market_history(
    db: Session,
    market: Market,
    *,
    range_key: str = "1D",
    client: KalshiPublicClient | None = None,
) -> dict[str, Any]:
    normalized_range = range_key.upper()
    if normalized_range not in RANGE_CONFIG:
        raise ValueError("Unsupported range. Try 1D or 7D.")

    config = RANGE_CONFIG[normalized_range]
    end_at = datetime.now(timezone.utc)
    start_at = end_at - config["window"]
    period_minutes = int(config["period_minutes"])
    client = client or KalshiPublicClient()

    points: list[dict[str, Any]] = []
    if market.series_ticker:
        try:
            payload = client.get_market_candlesticks(
                series_ticker=market.series_ticker,
                ticker=market.ticker,
                start_ts=int(start_at.timestamp()),
                end_ts=int(end_at.timestamp()),
                period_interval=period_minutes,
            )
            points.extend(_candlestick_points(payload, source="live_candlestick"))
        except httpx.HTTPError:
            pass

    if not points or (market.close_time and market.close_time < start_at):
        try:
            payload = client.get_historical_market_candlesticks(
                ticker=market.ticker,
                start_ts=int(start_at.timestamp()),
                end_ts=int(end_at.timestamp()),
                period_interval=period_minutes,
            )
            points.extend(_candlestick_points(payload, source="historical_candlestick"))
        except httpx.HTTPError:
            pass

    points.extend(_local_snapshot_points(db, market.id, start_at, end_at, period_minutes))
    merged_points = _merge_points(points)
    return {
        "ticker": market.ticker,
        "range": normalized_range,
        "points": merged_points,
    }


def _candlestick_points(payload: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    raw_points = payload.get("candlesticks") or payload.get("candles") or []
    points: list[dict[str, Any]] = []
    for item in raw_points:
        timestamp = _timestamp_from_candlestick(item)
        if not timestamp:
            continue
        # Live Kalshi candlesticks nest OHLC under a ``price`` object
        # (close_dollars / mean_dollars); the old top-level keys never existed,
        # so every point was price-less and the history chart rendered empty.
        # Read the nested dollar fields first, then the *_dollars fallbacks, and
        # drop the legacy bare cent keys (close_price / last_price) — those were
        # a latent 100x unit hazard on the same 0-1 axis.
        price = item.get("price") or {}
        last_price = parse_price_dollars(
            price.get("close_dollars")
            or price.get("mean_dollars")
            or item.get("close_price_dollars")
            or item.get("last_price_dollars")
        )
        mean_price = parse_price_dollars(
            price.get("mean_dollars") or item.get("mean_price_dollars")
        ) or last_price
        volume = parse_price_dollars(item.get("volume_fp") or item.get("volume_dollars") or item.get("volume"))
        points.append(
            {
                "timestamp": timestamp,
                "yes_bid": None,
                "yes_ask": None,
                "no_bid": None,
                "no_ask": None,
                "last_price": last_price,
                "mean_price": mean_price,
                "volume": volume,
                "source": source,
            }
        )
    return points


def _timestamp_from_candlestick(item: dict[str, Any]) -> datetime | None:
    for key in ("end_period_time", "period_end_time", "time", "timestamp"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    for key in ("end_period_ts", "period_end_ts", "ts"):
        value = item.get(key)
        if value is None:
            continue
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    return None


def _local_snapshot_points(
    db: Session,
    market_id: int,
    start_at: datetime,
    end_at: datetime,
    period_minutes: int,
) -> list[dict[str, Any]]:
    snapshots = db.scalars(
        select(MarketSnapshot)
        .where(MarketSnapshot.market_id == market_id, MarketSnapshot.captured_at >= start_at, MarketSnapshot.captured_at <= end_at)
        .order_by(MarketSnapshot.captured_at.asc())
    ).all()
    buckets: dict[datetime, list[MarketSnapshot]] = {}
    for snapshot in snapshots:
        bucket_time = _bucket_timestamp(snapshot.captured_at, period_minutes)
        buckets.setdefault(bucket_time, []).append(snapshot)

    points: list[dict[str, Any]] = []
    for bucket_time, bucket_rows in sorted(buckets.items()):
        latest = bucket_rows[-1]
        last_prices = [row.last_price for row in bucket_rows if row.last_price is not None]
        mean_price = round(sum(last_prices) / len(last_prices), 4) if last_prices else latest.last_price
        volume = latest.volume
        points.append(
            {
                "timestamp": bucket_time,
                "yes_bid": latest.yes_bid,
                "yes_ask": latest.yes_ask,
                "no_bid": latest.no_bid,
                "no_ask": latest.no_ask,
                "last_price": latest.last_price,
                "mean_price": mean_price,
                "volume": volume,
                "source": "local_snapshot",
            }
        )
    return points


def _bucket_timestamp(timestamp: datetime, period_minutes: int) -> datetime:
    normalized = timestamp.astimezone(timezone.utc)
    minute_bucket = (normalized.minute // period_minutes) * period_minutes if period_minutes < 60 else 0
    if period_minutes >= 1440:
        return normalized.replace(hour=0, minute=0, second=0, microsecond=0)
    return normalized.replace(minute=minute_bucket, second=0, microsecond=0)


def _merge_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[datetime, dict[str, Any]] = {}
    for point in sorted(points, key=lambda item: (item["timestamp"], SOURCE_ORDER.get(item["source"], 0))):
        bucket = point["timestamp"]
        existing = merged.get(bucket)
        if not existing:
            merged[bucket] = dict(point)
            continue
        for key, value in point.items():
            if key == "timestamp" or value is None:
                continue
            existing[key] = value
        if SOURCE_ORDER.get(point["source"], 0) >= SOURCE_ORDER.get(existing.get("source", ""), 0):
            existing["source"] = point["source"]
    return [
        {
            "timestamp": timestamp,
            "yes_bid": item.get("yes_bid"),
            "yes_ask": item.get("yes_ask"),
            "no_bid": item.get("no_bid"),
            "no_ask": item.get("no_ask"),
            "last_price": item.get("last_price"),
            "mean_price": item.get("mean_price"),
            "volume": item.get("volume"),
            "source": item.get("source"),
        }
        for timestamp, item in sorted(merged.items())
    ]
