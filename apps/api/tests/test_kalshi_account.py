import threading
import time

import pytest

from app.models import Market
from app.services import kalshi_account as kalshi_account_module
from app.services.kalshi_account import build_kalshi_account_snapshot, invalidate_kalshi_account_cache


@pytest.fixture(autouse=True)
def _reset_kalshi_account_cache():
    invalidate_kalshi_account_cache()
    yield
    invalidate_kalshi_account_cache()


class FakeConfiguredKalshiAccountClient:
    def is_configured(self):
        return True

    def get_balance(self):
        return {"balance": 12550, "portfolio_value": 17125, "updated_ts": 1711814400}

    def list_positions(self, *, count_filter, limit):
        return {
            "market_positions": [
                {
                    "ticker": "NBA-TEST",
                    "position_fp": "3.00",
                    "total_traded_dollars": "1.6500",
                    "market_exposure_dollars": "1.3500",
                    "realized_pnl_dollars": "0.2400",
                    "fees_paid_dollars": "0.0100",
                    "resting_orders_count": 1,
                    "last_updated_ts": "2026-04-29T12:00:00Z",
                }
            ]
        }

    def list_markets_by_tickers(self, tickers):
        return [
            {
                "ticker": "NBA-TEST",
                "title": "Celtics to win?",
                "subtitle": "NBA regular season",
                "yes_sub_title": "Boston Celtics",
                "no_sub_title": "Brooklyn Nets",
            }
        ]

    def list_fills(self, *, limit):
        return {
            "fills": [
                {
                    "fill_id": "fill-1",
                    "trade_id": "trade-1",
                    "order_id": "order-1",
                    "ticker": "NBA-TEST",
                    "side": "yes",
                    "action": "buy",
                    "count_fp": "3.00",
                    "yes_price_dollars": "0.5500",
                    "fee_cost": "0.0100",
                    "created_time": "2026-04-29T12:01:00Z",
                }
            ]
        }


class FakeUnknownTickerKalshiAccountClient(FakeConfiguredKalshiAccountClient):
    def list_positions(self, *, count_filter, limit):
        return {
            "market_positions": [
                {
                    "ticker": "KXSWIFTKELCEWEDDINGLOCATION-30-PEN",
                    "position_fp": "117.00",
                    "market_exposure_dollars": "4.6800",
                    "realized_pnl_dollars": "0.0000",
                    "resting_orders_count": 0,
                }
            ]
        }

    def list_fills(self, *, limit):
        return {"fills": []}

    def list_markets_by_tickers(self, tickers):
        return [
            {
                "ticker": "KXSWIFTKELCEWEDDINGLOCATION-30-PEN",
                "title": "Where will Taylor Swift and Travis Kelce's Wedding occur?",
                "yes_sub_title": "Pennsylvania",
            }
        ]


class FakeMultilegKalshiAccountClient(FakeUnknownTickerKalshiAccountClient):
    def list_positions(self, *, count_filter, limit):
        return {
            "market_positions": [
                {
                    "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-TEST",
                    "position_fp": "5.00",
                    "market_exposure_dollars": "1.0000",
                    "realized_pnl_dollars": "0.0000",
                    "resting_orders_count": 0,
                }
            ]
        }

    def list_markets_by_tickers(self, tickers):
        return [
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-TEST",
                "title": "yes Boston,yes Bam Adebayo: 15+",
                "subtitle": "NBA combo",
                "mve_collection_ticker": "KXMVE-NBA-MIXED-COLLECTION",
            }
        ]


class FakeMetadataFailureKalshiAccountClient(FakeUnknownTickerKalshiAccountClient):
    def list_markets_by_tickers(self, tickers):
        raise RuntimeError("metadata unavailable")


class FakeMissingKalshiAccountClient:
    def is_configured(self):
        return False


def test_kalshi_account_snapshot_maps_live_positions_and_fills(db_session):
    db_session.add(
        Market(
            ticker="NBA-TEST",
            sport_key="NBA",
            title="Celtics to win?",
            status="open",
        )
    )
    db_session.commit()

    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeConfiguredKalshiAccountClient(),
    )

    assert snapshot.status == "connected"
    assert snapshot.balance.cash_balance_dollars == 125.5
    assert snapshot.balance.portfolio_value_dollars == 171.25
    assert snapshot.market_positions[0].market_title == "Celtics to win?"
    assert snapshot.market_positions[0].market_subtitle == "NBA regular season"
    assert snapshot.market_positions[0].bet_label == "YES Boston Celtics"
    assert snapshot.market_positions[0].bet_subtitle == "Celtics to win?"
    assert snapshot.market_positions[0].position == 3
    assert snapshot.market_positions[0].realized_pnl_dollars == 0.24
    assert snapshot.recent_fills[0].ticker == "NBA-TEST"
    assert snapshot.recent_fills[0].bet_label == "YES Boston Celtics"
    assert snapshot.recent_fills[0].yes_price_dollars == 0.55


def test_kalshi_account_snapshot_enriches_unknown_tickers_from_kalshi_metadata(db_session):
    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeUnknownTickerKalshiAccountClient(),
    )

    assert snapshot.status == "connected"
    assert snapshot.market_positions[0].bet_label == "YES Pennsylvania"
    assert (
        snapshot.market_positions[0].bet_subtitle
        == "Where will Taylor Swift and Travis Kelce's Wedding occur?"
    )


def test_kalshi_account_snapshot_uses_compact_multileg_labels(db_session):
    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeMultilegKalshiAccountClient(),
    )

    assert snapshot.market_positions[0].bet_label == "Boston + Bam Adebayo: 15+"
    assert snapshot.market_positions[0].bet_subtitle == "NBA combo"


def test_kalshi_account_snapshot_falls_back_to_ticker_when_metadata_lookup_fails(db_session):
    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeMetadataFailureKalshiAccountClient(),
    )

    assert snapshot.status == "connected"
    assert snapshot.market_positions[0].bet_label == "KXSWIFTKELCEWEDDINGLOCATION-30-PEN"
    assert snapshot.market_positions[0].bet_subtitle is None


def test_kalshi_account_snapshot_reports_missing_credentials(db_session):
    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeMissingKalshiAccountClient(),
    )

    assert snapshot.configured is False
    assert snapshot.status == "not_configured"
    assert snapshot.market_positions == []
    assert snapshot.recent_fills == []


class _CountingKalshiAccountClient(FakeConfiguredKalshiAccountClient):
    """Counts ``get_balance`` calls so the cache test can verify
    coalescing across consecutive callers."""

    def __init__(self) -> None:
        self.balance_calls = 0
        self.positions_calls = 0
        self.fills_calls = 0

    def get_balance(self):  # type: ignore[override]
        self.balance_calls += 1
        return super().get_balance()

    def list_positions(self, *, count_filter, limit):  # type: ignore[override]
        self.positions_calls += 1
        return super().list_positions(count_filter=count_filter, limit=limit)

    def list_fills(self, *, limit):  # type: ignore[override]
        self.fills_calls += 1
        return super().list_fills(limit=limit)


def test_kalshi_account_snapshot_caches_when_called_without_explicit_client(db_session, monkeypatch):
    """Bug #6: ``/positions`` polls every ~15 s and fans out 3+ Kalshi
    calls per request. The production path (``client=None``) must
    cache the snapshot inside the fresh window so consecutive callers
    don't all re-fetch from Kalshi."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    first = build_kalshi_account_snapshot(db_session)
    second = build_kalshi_account_snapshot(db_session)
    third = build_kalshi_account_snapshot(db_session)

    assert first.status == "connected"
    assert first == second == third
    assert counting_client.balance_calls == 1, (
        "consecutive /positions calls must reuse the cached snapshot"
    )
    assert counting_client.positions_calls == 1
    assert counting_client.fills_calls == 1


def test_kalshi_account_snapshot_serves_stale_while_revalidating(db_session, monkeypatch):
    """Bug #6, codex round-2 P2: the portfolio page polls every 15 s
    but the fresh TTL is 5 s. Without stale-while-revalidate, every
    poll re-fetches from Kalshi and the cache is useless for the
    actual polling cadence. The SWR path must (a) serve cached data
    immediately when the fresh window has elapsed but the stale
    window hasn't, and (b) fire a single background refresh that
    updates the cache out-of-band of the request."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )
    # ``SessionLocal`` is called by the background refresh thread —
    # return the test session so the in-memory DB is reused.

    class _SessionContext:
        def __enter__(self):
            return db_session

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(kalshi_account_module, "SessionLocal", lambda: _SessionContext())

    first = build_kalshi_account_snapshot(db_session)
    assert counting_client.balance_calls == 1

    # Advance time past the fresh window but inside the stale window.
    fake_now = {"value": time.monotonic() + 10.0}
    monkeypatch.setattr(kalshi_account_module.time, "monotonic", lambda: fake_now["value"])

    second = build_kalshi_account_snapshot(db_session)

    assert second == first, "stale-hit must return the cached value, not block on a fetch"
    # The background refresh fires; wait for it to finish then verify
    # it incremented the counter exactly once.
    deadline = time.monotonic() + 2
    while kalshi_account_module._background_refresh_in_progress.is_set():
        if time.monotonic() > deadline:
            raise AssertionError("background refresh did not complete")
        time.sleep(0.01)
    assert counting_client.balance_calls == 2, (
        "stale-hit must fire exactly one background refresh, not multiple"
    )


def test_kalshi_account_snapshot_blocks_on_fetch_when_beyond_stale_window(db_session, monkeypatch):
    """Past the stale window, the cache is no longer usable. The
    caller must block on a synchronous fetch so they don't get a
    minute-old snapshot."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    build_kalshi_account_snapshot(db_session)
    assert counting_client.balance_calls == 1

    # Advance time past the stale window entirely.
    fake_now = {"value": time.monotonic() + 120.0}
    monkeypatch.setattr(kalshi_account_module.time, "monotonic", lambda: fake_now["value"])

    second = build_kalshi_account_snapshot(db_session)

    assert second.status == "connected"
    assert counting_client.balance_calls == 2, (
        "callers past the stale window must block on a fresh synchronous fetch"
    )


def test_kalshi_account_snapshot_refetches_after_cache_invalidation(db_session, monkeypatch):
    """The cache must yield to a manual invalidation (used by ops paths
    that change Kalshi-visible state) and refetch from Kalshi."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    build_kalshi_account_snapshot(db_session)
    invalidate_kalshi_account_cache()
    build_kalshi_account_snapshot(db_session)

    assert counting_client.balance_calls == 2


def test_kalshi_account_snapshot_coalesces_concurrent_callers(db_session, monkeypatch):
    """Two concurrent threads hitting the production path must share a
    single Kalshi fetch — the second caller blocks on the cache lock,
    then observes the first's populated cache and returns without
    fanning out to Kalshi a second time."""
    counting_client = _CountingKalshiAccountClient()
    inside_fetch = threading.Event()
    release_fetch = threading.Event()

    original_get_balance = counting_client.get_balance

    def _slow_get_balance():
        inside_fetch.set()
        release_fetch.wait(timeout=2)
        return original_get_balance()

    counting_client.get_balance = _slow_get_balance
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    results: list[object] = []

    def _runner():
        results.append(build_kalshi_account_snapshot(db_session))

    t1 = threading.Thread(target=_runner)
    t1.start()
    # Wait until t1 is inside ``get_balance`` — at that point t1 holds
    # the cache lock, and t2's call below must block waiting for it.
    assert inside_fetch.wait(timeout=2)

    t2 = threading.Thread(target=_runner)
    t2.start()
    # Give t2 a moment to actually enter ``build_...`` and block on
    # the lock. We can't observe this directly, but a small sleep is
    # sufficient — the alternative is racing the lock acquisition.
    threading.Event().wait(timeout=0.1)

    release_fetch.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(results) == 2
    assert counting_client.balance_calls == 1, (
        "concurrent callers must coalesce on a single Kalshi fetch"
    )
