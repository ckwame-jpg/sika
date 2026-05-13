import logging
import re
import threading
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.kalshi import KalshiAccountClient
from app.database import SessionLocal
from app.models import Market
from app.schemas import (
    KalshiAccountBalanceRead,
    KalshiAccountFillRead,
    KalshiAccountMarketPositionRead,
    KalshiAccountRead,
)


logger = logging.getLogger(__name__)


# Bug #6: cache the ``build_kalshi_account_snapshot`` response so
# /positions polling every ~15 s doesn't fan out to 3+ live Kalshi
# calls per request. The endpoint is single-tenant (one API key per
# process), so a single global cache key is sufficient.
#
# Two-tier TTL implementing stale-while-revalidate.
#
# Codex round-2 on PR #40: a flat 5 s TTL is shorter than the 15 s
# polling interval — every poll still refetches and the cache only
# coalesced concurrent requests.
#
# Codex round-4: a 5 s FRESH with 60 s STALE moved the Kalshi calls
# to a daemon thread but didn't reduce them — every poll still fired
# one upstream fetch (just async). To actually cut RPM, the fresh
# window must cover at least one polling interval, so 15 s polls
# inside FRESH never trigger a fetch.
#
# Window semantics:
# * within ``FRESH_SECONDS`` (30 s): serve cached, no refresh — covers
#   the 15 s portfolio poll twice. Roughly 50 % RPM reduction in the
#   common case.
# * between FRESH and ``STALE_SECONDS`` (120 s): serve cached AND
#   fire a single background refresh that updates the cache in place.
# * beyond STALE_SECONDS: cache is too stale to serve; the caller
#   blocks on a fresh fetch (still coalesced via the lock).
#
# Error-backoff: codex round-4 P2 — when a background refresh errors
# while we have a good cached snapshot, ``_build_..._uncached``
# returns ``KalshiAccountRead(status="error")`` rather than raising.
# Storing that over the connected snapshot would surface a transient
# Kalshi error to the user for the full TTL window. Instead, the
# background-refresh path preserves the good cache and extends the
# fresh marker by ``ERROR_BACKOFF_SECONDS`` so we don't immediately
# retry — the next stale-hit after the backoff fires a fresh attempt.
_ACCOUNT_SNAPSHOT_FRESH_SECONDS = 30.0
_ACCOUNT_SNAPSHOT_STALE_SECONDS = 120.0
_ACCOUNT_SNAPSHOT_ERROR_BACKOFF_SECONDS = 15.0
_account_snapshot_cache: dict[str, Any] = {
    "value": None,
    "fresh_until": 0.0,
    "stale_until": 0.0,
    # Codex round-6 P2: monotonically increasing generation token,
    # bumped each time the cache is invalidated. A background refresh
    # that started before the invalidation must NOT commit its
    # pre-invalidation result on top of fresh post-invalidation data
    # (or after a force-refresh sync path has populated the cache).
    # The worker captures the generation at start; if it has changed
    # when the worker tries to commit, the result is discarded.
    "generation": 0,
}
_account_snapshot_lock = threading.Lock()
# Codex round-3 P2 on PR #40: ``Event.is_set()`` + ``Event.set()`` is
# not an atomic test-and-set — two concurrent stale-hits can both pass
# the check and spawn a refresh, defeating the coalescing.
# ``Lock.acquire(blocking=False)`` IS atomic: exactly one caller wins
# the slot, the rest get False and bow out. The background thread
# releases the lock in its ``finally`` so subsequent stale windows can
# fire fresh refreshes. (Python's ``Lock`` permits release from a
# thread other than the acquirer.)
_background_refresh_slot = threading.Lock()


def invalidate_kalshi_account_cache() -> None:
    """Reset the cached account snapshot. Use in tests and ops paths
    that mutate Kalshi-visible state. Bumping the generation token
    ensures any in-flight background refresh discards its result
    rather than overwriting fresh post-invalidation data."""
    with _account_snapshot_lock:
        _account_snapshot_cache["value"] = None
        _account_snapshot_cache["fresh_until"] = 0.0
        _account_snapshot_cache["stale_until"] = 0.0
        _account_snapshot_cache["generation"] += 1


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cents_to_dollars(value: Any) -> float | None:
    parsed = _float_or_none(value)
    if parsed is None:
        return None
    return round(parsed / 100, 4)


def _account_error_message(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "Kalshi private key file is not available."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Kalshi account request failed with HTTP {exc.response.status_code}."
    if isinstance(exc, httpx.HTTPError):
        return "Kalshi account request failed."
    return "Kalshi account sync failed."


def _market_lookup(db: Session, tickers: set[str]) -> dict[str, Market]:
    if not tickers:
        return {}
    markets = db.scalars(select(Market).where(Market.ticker.in_(tickers))).all()
    return {market.ticker: market for market in markets}


def _remote_market_lookup(client: KalshiAccountClient, tickers: set[str]) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    sorted_tickers = sorted(tickers)
    for index in range(0, len(sorted_tickers), 100):
        chunk = sorted_tickers[index : index + 100]
        for market in client.list_markets_by_tickers(chunk):
            ticker = str(market.get("ticker") or "").strip()
            if ticker:
                lookup[ticker] = market
    return lookup


def _strip_side_prefix(value: str) -> str:
    return re.sub(r"^(?:yes|no)\s+", "", value.strip(), flags=re.IGNORECASE)


def _compact_multileg_label(value: str) -> str:
    legs = [_strip_side_prefix(part) for part in value.split(",")]
    legs = [leg for leg in legs if leg]
    return " + ".join(legs)


def _local_market_raw_data(local_market: Market | None) -> dict[str, Any]:
    raw_data = local_market.raw_data if local_market else None
    return raw_data if isinstance(raw_data, dict) else {}


def _market_metadata_value(
    *,
    key: str,
    local_market: Market | None,
    remote_market: dict[str, Any] | None,
) -> str | None:
    remote_value = str((remote_market or {}).get(key) or "").strip()
    if remote_value:
        return remote_value
    local_value = str(_local_market_raw_data(local_market).get(key) or "").strip()
    return local_value or None


def _is_ticker_like(value: str) -> bool:
    return bool(re.match(r"^KX[A-Z0-9-]+$", value.strip(), flags=re.IGNORECASE))


def _mve_leg_labels(market_payload: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for leg in list(market_payload.get("mve_selected_legs") or []):
        if not isinstance(leg, dict):
            continue
        side = str(leg.get("side") or "yes").lower()
        side_key = "no_sub_title" if side == "no" else "yes_sub_title"
        raw_label = (
            str(
                leg.get(side_key)
                or leg.get("sub_title")
                or leg.get("subtitle")
                or leg.get("market_title")
                or leg.get("title")
                or ""
            ).strip()
        )
        label = _strip_side_prefix(raw_label)
        if label and not _is_ticker_like(label):
            labels.append(label)
    return labels


def _selected_side_for_position(position: float) -> str:
    return "yes" if position >= 0 else "no"


def _bet_copy(
    *,
    ticker: str,
    side: str | None,
    local_market: Market | None,
    remote_market: dict[str, Any] | None,
) -> tuple[str, str | None, str | None, str | None]:
    payload = {**_local_market_raw_data(local_market), **(remote_market or {})}
    market_title = (
        str((remote_market or {}).get("title") or "").strip()
        or (local_market.title if local_market else None)
        or None
    )
    market_subtitle = (
        str((remote_market or {}).get("subtitle") or "").strip()
        or (local_market.subtitle if local_market else None)
        or None
    )
    normalized_side = (side or "yes").lower()
    side_label = "NO" if normalized_side == "no" else "YES"
    side_subtitle_key = "no_sub_title" if normalized_side == "no" else "yes_sub_title"
    selected_subtitle = (
        _market_metadata_value(key=side_subtitle_key, local_market=local_market, remote_market=remote_market)
        or ""
    )

    mve_labels = _mve_leg_labels(payload)
    if mve_labels:
        bet_subtitle = market_subtitle or market_title
        return " + ".join(mve_labels), bet_subtitle, market_title, market_subtitle

    source_label = selected_subtitle or market_title or ticker
    is_multileg = "," in source_label and re.search(r"(?:^|,)\s*(?:yes|no)\s+", source_label, re.IGNORECASE)
    if is_multileg:
        bet_label = _compact_multileg_label(source_label)
        bet_subtitle = market_subtitle or (market_title if market_title and market_title != source_label else None)
        return bet_label or ticker, bet_subtitle, market_title, market_subtitle

    if selected_subtitle and market_title and selected_subtitle.lower() != market_title.lower():
        return f"{side_label} {selected_subtitle}", market_title, market_title, market_subtitle

    if market_title:
        return f"{side_label} {market_title}", market_subtitle, market_title, market_subtitle

    return ticker, None, market_title, market_subtitle


def build_kalshi_account_snapshot(
    db: Session,
    *,
    client: KalshiAccountClient | None = None,
) -> KalshiAccountRead:
    # Bug #6: cache the production path (no explicit client) so the
    # portfolio page's ~15 s polling doesn't fan out 3+ Kalshi calls
    # per request. Tests that pass an explicit ``client`` bypass the
    # cache so they can drive specific scenarios.
    if client is not None:
        return _build_kalshi_account_snapshot_uncached(db, client=client)

    now = time.monotonic()
    cached = _account_snapshot_cache["value"]
    fresh_until = _account_snapshot_cache["fresh_until"]
    stale_until = _account_snapshot_cache["stale_until"]

    if cached is not None and now < fresh_until:
        # Fresh: return cached, no refresh.
        return cached

    if cached is not None and now < stale_until:
        # Stale-but-usable: serve cached immediately and fire a
        # single background refresh so the next poll lands fresh
        # data. Subsequent polls inside the stale window keep
        # landing the cached value out-of-band of the upstream call.
        _maybe_start_background_refresh()
        return cached

    # No cache (or beyond STALE_SECONDS): block on a synchronous
    # fetch. The lock coalesces concurrent callers — the second
    # caller observes the populated cache when it acquires the lock.
    with _account_snapshot_lock:
        now = time.monotonic()
        cached = _account_snapshot_cache["value"]
        if cached is not None and now < _account_snapshot_cache["fresh_until"]:
            return cached
        previous = cached
        result = _build_kalshi_account_snapshot_uncached(db, client=None)
        return _commit_refresh_result(result, previous=previous)


def _commit_refresh_result(
    snapshot: KalshiAccountRead,
    *,
    previous: KalshiAccountRead | None,
) -> KalshiAccountRead:
    """Store the new snapshot in the cache with the appropriate TTL,
    and return what should be served to the caller.

    Codex round-4 + round-5 P2: ``_build_..._uncached`` catches Kalshi
    exceptions and returns ``status="error"`` rather than raising.
    Storing that over a good cache with the normal TTL would surface
    a transient HTTP/429 to the portfolio UI for a full ``FRESH``
    window. Instead:

    * error + good prior cache → preserve the cached connected
      snapshot, extend ``fresh_until`` by ``ERROR_BACKOFF_SECONDS``,
      and return the cached snapshot (not the error).
    * error + no good prior → store the error with a short backoff so
      the next request retries soon, not the full ``FRESH`` window.
    * connected → store with the normal TTLs.
    """
    if snapshot.status == "error":
        if previous is not None and previous.status == "connected":
            _account_snapshot_cache["fresh_until"] = (
                time.monotonic() + _ACCOUNT_SNAPSHOT_ERROR_BACKOFF_SECONDS
            )
            logger.warning(
                "kalshi_account_refresh_error_preserved_cache",
                extra={"error_message": snapshot.error_message},
            )
            return previous
        # No good prior cache to preserve — store the error with a
        # short backoff so the next request retries soon.
        now = time.monotonic()
        _account_snapshot_cache["value"] = snapshot
        _account_snapshot_cache["fresh_until"] = now + _ACCOUNT_SNAPSHOT_ERROR_BACKOFF_SECONDS
        _account_snapshot_cache["stale_until"] = now + _ACCOUNT_SNAPSHOT_ERROR_BACKOFF_SECONDS
        return snapshot
    _store_account_snapshot(snapshot)
    return snapshot


def _store_account_snapshot(snapshot: KalshiAccountRead) -> None:
    now = time.monotonic()
    _account_snapshot_cache["value"] = snapshot
    _account_snapshot_cache["fresh_until"] = now + _ACCOUNT_SNAPSHOT_FRESH_SECONDS
    _account_snapshot_cache["stale_until"] = now + _ACCOUNT_SNAPSHOT_STALE_SECONDS


def _maybe_start_background_refresh() -> None:
    """Fire a daemon thread to refresh the cache in place. At most
    one refresh runs at a time — concurrent stale-hits race on
    ``_background_refresh_slot.acquire(blocking=False)`` and only
    one wins. The losers return immediately; the winner spawns the
    worker, which releases the slot in ``finally``."""
    if not _background_refresh_slot.acquire(blocking=False):
        return
    try:
        thread = threading.Thread(
            target=_run_background_refresh,
            daemon=True,
            name="kalshi-account-refresh",
        )
        thread.start()
    except BaseException:
        # If the thread couldn't start, release the slot so a future
        # stale-hit can retry.
        _background_refresh_slot.release()
        raise


def _run_background_refresh() -> None:
    try:
        start_generation = _account_snapshot_cache["generation"]
        with SessionLocal() as db:
            snapshot = _build_kalshi_account_snapshot_uncached(db, client=None)
        with _account_snapshot_lock:
            if start_generation != _account_snapshot_cache["generation"]:
                # Codex round-6 P2: cache was invalidated (or a force-
                # refresh ran) while we were fetching. Our snapshot is
                # pre-invalidation and may be stale relative to the
                # invalidation event — discard it rather than
                # overwriting whatever the invalidator (or the sync
                # path that followed it) wrote.
                logger.info("kalshi_account_background_refresh_discarded_after_invalidation")
                return
            _commit_refresh_result(snapshot, previous=_account_snapshot_cache["value"])
    except Exception:  # noqa: BLE001 — background refresh must not crash the app
        logger.exception("kalshi_account_background_refresh_failed")
    finally:
        _background_refresh_slot.release()


def _build_kalshi_account_snapshot_uncached(
    db: Session,
    *,
    client: KalshiAccountClient | None,
) -> KalshiAccountRead:
    kalshi_client = client or KalshiAccountClient()
    if not kalshi_client.is_configured():
        return KalshiAccountRead(
            configured=False,
            status="not_configured",
            error_message="Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH to connect your Kalshi account.",
        )

    try:
        balance_payload = kalshi_client.get_balance()
        positions_payload = kalshi_client.list_positions(count_filter="position", limit=100)
        fills_payload = kalshi_client.list_fills(limit=25)
    except Exception as exc:
        return KalshiAccountRead(
            configured=True,
            status="error",
            error_message=_account_error_message(exc),
        )

    raw_positions = list(positions_payload.get("market_positions") or [])
    raw_fills = list(fills_payload.get("fills") or [])
    tickers = {
        str(item.get("ticker") or item.get("market_ticker") or "").strip()
        for item in [*raw_positions, *raw_fills]
    }
    tickers.discard("")
    markets_by_ticker = _market_lookup(db, tickers)
    try:
        remote_markets_by_ticker = _remote_market_lookup(kalshi_client, tickers)
    except Exception:
        remote_markets_by_ticker = {}

    positions: list[KalshiAccountMarketPositionRead] = []
    for item in raw_positions:
        ticker = str(item.get("ticker") or item.get("market_ticker") or "").strip()
        if not ticker:
            continue
        market = markets_by_ticker.get(ticker)
        position_value = _float_or_none(item.get("position_fp") or item.get("position")) or 0.0
        bet_label, bet_subtitle, market_title, market_subtitle = _bet_copy(
            ticker=ticker,
            side=_selected_side_for_position(position_value),
            local_market=market,
            remote_market=remote_markets_by_ticker.get(ticker),
        )
        positions.append(
            KalshiAccountMarketPositionRead(
                ticker=ticker,
                bet_label=bet_label,
                bet_subtitle=bet_subtitle,
                market_title=market_title,
                market_subtitle=market_subtitle,
                sport_key=market.sport_key if market else None,
                position=position_value,
                total_traded_dollars=_float_or_none(item.get("total_traded_dollars")),
                market_exposure_dollars=_float_or_none(item.get("market_exposure_dollars")),
                realized_pnl_dollars=_float_or_none(item.get("realized_pnl_dollars")),
                fees_paid_dollars=_float_or_none(item.get("fees_paid_dollars")),
                resting_orders_count=int(_float_or_none(item.get("resting_orders_count")) or 0),
                last_updated_ts=item.get("last_updated_ts"),
            )
        )

    fills: list[KalshiAccountFillRead] = []
    for item in raw_fills:
        ticker = str(item.get("ticker") or item.get("market_ticker") or "").strip()
        if not ticker:
            continue
        market = markets_by_ticker.get(ticker)
        bet_label, bet_subtitle, market_title, market_subtitle = _bet_copy(
            ticker=ticker,
            side=item.get("side"),
            local_market=market,
            remote_market=remote_markets_by_ticker.get(ticker),
        )
        fills.append(
            KalshiAccountFillRead(
                fill_id=item.get("fill_id"),
                trade_id=item.get("trade_id"),
                order_id=item.get("order_id"),
                ticker=ticker,
                bet_label=bet_label,
                bet_subtitle=bet_subtitle,
                market_title=market_title,
                market_subtitle=market_subtitle,
                sport_key=market.sport_key if market else None,
                side=item.get("side"),
                action=item.get("action"),
                count=_float_or_none(item.get("count_fp") or item.get("count")) or 0.0,
                yes_price_dollars=_float_or_none(item.get("yes_price_dollars")),
                no_price_dollars=_float_or_none(item.get("no_price_dollars")),
                fee_dollars=_float_or_none(item.get("fee_cost") or item.get("fee_cost_dollars")),
                created_time=item.get("created_time"),
            )
        )

    return KalshiAccountRead(
        configured=True,
        status="connected",
        balance=KalshiAccountBalanceRead(
            cash_balance_dollars=_cents_to_dollars(balance_payload.get("balance")),
            portfolio_value_dollars=_cents_to_dollars(balance_payload.get("portfolio_value")),
            updated_ts=balance_payload.get("updated_ts"),
        ),
        market_positions=positions,
        recent_fills=fills,
    )
