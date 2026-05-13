import threading
import time

import pytest

from app.models import Market
from app.services import kalshi_account as kalshi_account_module
from app.services.kalshi_account import (
    build_kalshi_account_snapshot,
    expire_kalshi_account_cache,
    invalidate_kalshi_account_cache,
)


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
    fake_now = {"value": time.monotonic() + kalshi_account_module._ACCOUNT_SNAPSHOT_FRESH_SECONDS + 5.0}
    monkeypatch.setattr(kalshi_account_module.time, "monotonic", lambda: fake_now["value"])

    second = build_kalshi_account_snapshot(db_session)

    assert second == first, "stale-hit must return the cached value, not block on a fetch"
    # The background refresh fires; wait for it to finish then verify
    # it incremented the counter exactly once. The slot lock is the
    # atomic gate — acquiring it (blocking) means the worker has
    # already released it in its ``finally``.
    acquired = kalshi_account_module._background_refresh_slot.acquire(timeout=2)
    assert acquired, "background refresh did not complete in time"
    kalshi_account_module._background_refresh_slot.release()
    assert counting_client.balance_calls == 2, (
        "stale-hit must fire exactly one background refresh, not multiple"
    )


def test_kalshi_account_snapshot_stale_refresh_fires_only_once_under_concurrency(db_session, monkeypatch):
    """Bug #6, codex round-3 P2 on PR #40: two concurrent stale-hits
    must NOT both spawn a background refresh — the test-and-set on the
    refresh slot is atomic via ``Lock.acquire(blocking=False)``, so
    exactly one caller wins the slot."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    class _SessionContext:
        def __enter__(self):
            return db_session

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(kalshi_account_module, "SessionLocal", lambda: _SessionContext())

    # Block the background fetch so we can verify exactly one worker
    # is launched even when many threads race the stale-hit branch.
    inside_fetch = threading.Event()
    release_fetch = threading.Event()
    original_get_balance = counting_client.get_balance

    def _slow_get_balance():
        inside_fetch.set()
        release_fetch.wait(timeout=2)
        return original_get_balance()

    counting_client.get_balance = _slow_get_balance

    # Seed the cache so subsequent calls hit the stale branch.
    build_kalshi_account_snapshot(db_session)
    counting_client.balance_calls = 0  # reset after seeding
    inside_fetch.clear()

    # Advance time into the stale window.
    fake_now = {"value": time.monotonic() + kalshi_account_module._ACCOUNT_SNAPSHOT_FRESH_SECONDS + 5.0}
    monkeypatch.setattr(kalshi_account_module.time, "monotonic", lambda: fake_now["value"])

    # Fire many concurrent stale-hits.
    threads = [
        threading.Thread(target=build_kalshi_account_snapshot, args=(db_session,))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    # Wait for the (single) background worker to enter ``get_balance``,
    # then release it.
    assert inside_fetch.wait(timeout=2), "background refresh did not start"
    release_fetch.set()

    acquired = kalshi_account_module._background_refresh_slot.acquire(timeout=2)
    assert acquired, "background refresh did not release the slot"
    kalshi_account_module._background_refresh_slot.release()

    assert counting_client.balance_calls == 1, (
        f"expected exactly one background refresh under 8 concurrent stale-hits, "
        f"got {counting_client.balance_calls}"
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
    fake_now = {"value": time.monotonic() + kalshi_account_module._ACCOUNT_SNAPSHOT_STALE_SECONDS + 5.0}
    monkeypatch.setattr(kalshi_account_module.time, "monotonic", lambda: fake_now["value"])

    second = build_kalshi_account_snapshot(db_session)

    assert second.status == "connected"
    assert counting_client.balance_calls == 2, (
        "callers past the stale window must block on a fresh synchronous fetch"
    )


def test_kalshi_account_snapshot_background_error_preserves_connected_cache(db_session, monkeypatch):
    """Bug #6, codex round-4 P2: when the background refresh hits a
    transient Kalshi error, ``_build_..._uncached`` returns a
    ``KalshiAccountRead(status="error")`` rather than raising. Storing
    that over the good cached snapshot would surface the error to the
    portfolio UI for a full TTL window. Instead, the background path
    must preserve the connected snapshot and only extend the fresh
    marker by the error backoff."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    class _SessionContext:
        def __enter__(self):
            return db_session

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(kalshi_account_module, "SessionLocal", lambda: _SessionContext())

    # Seed the cache with a good "connected" snapshot.
    seeded = build_kalshi_account_snapshot(db_session)
    assert seeded.status == "connected"

    # Make the next Kalshi call fail. ``_build_..._uncached`` catches
    # the exception and returns ``status="error"`` — the background
    # refresh path must NOT cache that response.
    def _failing_get_balance():
        raise RuntimeError("upstream 429")

    counting_client.get_balance = _failing_get_balance

    # Advance into the stale window so the next call fires the
    # background refresh.
    fake_now = {"value": time.monotonic() + kalshi_account_module._ACCOUNT_SNAPSHOT_FRESH_SECONDS + 5.0}
    monkeypatch.setattr(kalshi_account_module.time, "monotonic", lambda: fake_now["value"])

    served = build_kalshi_account_snapshot(db_session)
    assert served.status == "connected", "stale-hit must serve the cached connected snapshot"

    # Wait for the background refresh worker to finish.
    acquired = kalshi_account_module._background_refresh_slot.acquire(timeout=2)
    assert acquired
    kalshi_account_module._background_refresh_slot.release()

    # The cached value must still be the good one — NOT the error
    # response from the failing background refresh.
    cached = kalshi_account_module._account_snapshot_cache["value"]
    assert cached is not None
    assert cached.status == "connected", (
        "transient background-refresh errors must not overwrite a connected cache"
    )


def test_kalshi_account_snapshot_does_not_cache_error_for_full_ttl_when_no_prior(db_session, monkeypatch):
    """Codex round-5 P2: when there's no good prior cache and the
    Kalshi fetch errors, the sync path used to store the error
    response with the full FRESH TTL — so /positions would skip
    retries and surface the error for 30 s. The error must use a
    short ``ERROR_BACKOFF_SECONDS`` window so the next request
    retries soon."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    def _failing_get_balance():
        raise RuntimeError("upstream 429")

    counting_client.get_balance = _failing_get_balance

    first = build_kalshi_account_snapshot(db_session)
    assert first.status == "error"

    cached_fresh_until = kalshi_account_module._account_snapshot_cache["fresh_until"]
    now = time.monotonic()
    error_window = cached_fresh_until - now
    assert 0.0 < error_window < kalshi_account_module._ACCOUNT_SNAPSHOT_FRESH_SECONDS, (
        f"error response must use a short backoff window, got {error_window:.1f}s"
    )


def test_kalshi_account_snapshot_background_refresh_discarded_after_invalidation(db_session, monkeypatch):
    """Codex round-6 P2 on PR #40: a background refresh started before
    a force-invalidation must NOT overwrite the post-invalidation cache
    when it eventually completes. The generation token captured at
    background-refresh start is re-checked at commit time; if it has
    changed, the result is discarded."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    class _SessionContext:
        def __enter__(self):
            return db_session

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(kalshi_account_module, "SessionLocal", lambda: _SessionContext())

    # Seed a connected cache so we hit the stale branch on the next call.
    build_kalshi_account_snapshot(db_session)
    assert counting_client.balance_calls == 1

    # Block the background fetch mid-flight.
    bg_inside = threading.Event()
    bg_release = threading.Event()
    original_get_balance = counting_client.get_balance

    def _bg_blocked_get_balance():
        bg_inside.set()
        bg_release.wait(timeout=2)
        # Mutate to a recognizably-different value so we can prove a
        # discarded write doesn't leak through.
        result = original_get_balance()
        return {**result, "balance": 99999}

    counting_client.get_balance = _bg_blocked_get_balance

    # Advance into the stale window so the next call fires the bg refresh.
    fake_now = {"value": time.monotonic() + kalshi_account_module._ACCOUNT_SNAPSHOT_FRESH_SECONDS + 5.0}
    monkeypatch.setattr(kalshi_account_module.time, "monotonic", lambda: fake_now["value"])

    build_kalshi_account_snapshot(db_session)
    assert bg_inside.wait(timeout=2), "background refresh did not start"

    # Force-invalidate the cache while the background is still in flight.
    invalidate_kalshi_account_cache()
    assert kalshi_account_module._account_snapshot_cache["value"] is None

    # Release the background fetch; its commit must see a generation
    # mismatch and discard the result.
    bg_release.set()
    acquired = kalshi_account_module._background_refresh_slot.acquire(timeout=2)
    assert acquired
    kalshi_account_module._background_refresh_slot.release()

    assert kalshi_account_module._account_snapshot_cache["value"] is None, (
        "background-refresh result must be discarded after invalidation"
    )


def test_kalshi_account_snapshot_surfaces_error_when_cache_too_stale_to_preserve(db_session, monkeypatch):
    """Codex round-9 P2 on PR #40: the preserve-cache-on-error
    fallback used to extend ``fresh_until`` by ERROR_BACKOFF every
    time the upstream errored — with no upper bound. During a long
    Kalshi outage (or after credential revocation), /positions would
    serve a ``connected`` snapshot with arbitrarily old balances
    forever. Fix: track ``last_successful_at`` and bypass the
    preserve path once the cached value is older than
    ``STALE_SECONDS``."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    # Seed a connected cache.
    seeded = build_kalshi_account_snapshot(db_session)
    assert seeded.status == "connected"

    # Make all subsequent Kalshi calls error.
    def _failing_get_balance():
        raise RuntimeError("upstream 429")

    counting_client.get_balance = _failing_get_balance

    # Advance time WELL past the stale horizon — the cached snapshot
    # is now older than STALE_SECONDS since its last successful
    # commit. ``expire`` doesn't reset ``last_successful_at`` so the
    # horizon check operates on the original commit time.
    fake_now = {"value": time.monotonic() + kalshi_account_module._ACCOUNT_SNAPSHOT_STALE_SECONDS + 10.0}
    monkeypatch.setattr(kalshi_account_module.time, "monotonic", lambda: fake_now["value"])

    # Trigger a sync fetch (force ensures we hit the sync path).
    expire_kalshi_account_cache()
    served = build_kalshi_account_snapshot(db_session)

    assert served.status == "error", (
        "after STALE_SECONDS with persistent errors, /positions must surface "
        f"the error rather than serve an arbitrarily-old connected snapshot; got {served.status}"
    )


def test_kalshi_account_snapshot_force_refresh_falls_back_to_cache_when_fetch_errors(client, monkeypatch):
    """Codex round-8 P2 on PR #40: a user-initiated force-refresh
    that hits a transient Kalshi error must NOT replace a good
    connected snapshot with an error response. The /positions
    endpoint uses ``expire_kalshi_account_cache`` (not ``invalidate``)
    so the previous value is preserved as ``previous`` in the sync
    fetch — and ``_commit_refresh_result`` falls back to it when the
    new snapshot is ``status="error"``."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    # Seed a connected cache.
    response_seed = client.get("/positions")
    assert response_seed.status_code == 200
    body_seed = response_seed.json()
    assert body_seed["kalshi_account"]["status"] == "connected"
    assert counting_client.balance_calls == 1

    # Make the next Kalshi call fail (still increment the counter so
    # the test can verify the fetch attempt happened).
    def _failing_get_balance():
        counting_client.balance_calls += 1
        raise RuntimeError("upstream 429")

    counting_client.get_balance = _failing_get_balance

    # Force-refresh — the fetch errors, but the user must still see
    # the connected snapshot (not the error response).
    response_force = client.get("/positions?force=true")
    assert response_force.status_code == 200
    body_force = response_force.json()
    assert body_force["kalshi_account"]["status"] == "connected", (
        "force-refresh + error must fall back to the cached connected snapshot, "
        f"got status={body_force['kalshi_account']['status']}"
    )
    assert counting_client.balance_calls == 2, "force-refresh must have attempted a fetch"


def test_kalshi_account_snapshot_force_refresh_bypasses_cache(client, monkeypatch):
    """Codex round-5 P2: the in-app Refresh button must be able to
    bypass the cache. ``/positions?force=true`` invalidates the cache
    before serving so the next call lands a fresh Kalshi fetch."""
    counting_client = _CountingKalshiAccountClient()
    monkeypatch.setattr(
        kalshi_account_module, "KalshiAccountClient", lambda: counting_client
    )

    response_a = client.get("/positions")
    assert response_a.status_code == 200
    assert counting_client.balance_calls == 1

    response_b = client.get("/positions")
    assert response_b.status_code == 200
    assert counting_client.balance_calls == 1  # cached

    response_c = client.get("/positions?force=true")
    assert response_c.status_code == 200
    assert counting_client.balance_calls == 2, (
        "force=true must bypass the cache and re-fetch from Kalshi"
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
