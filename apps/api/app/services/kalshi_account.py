import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
# Codex round-13 P2: how long the sync path waits for an in-flight
# background refresh before falling through and doing its own fetch.
# Tuned to be longer than a typical Kalshi response (~1 s) but short
# enough that a hung worker doesn't make /positions hang.
_ACCOUNT_SNAPSHOT_SYNC_COALESCE_TIMEOUT_SECONDS = 5.0
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
    # Codex round-9 P2: monotonic timestamp of the last connected
    # commit. The preserve-cache-on-error path consults this to
    # avoid serving an arbitrarily-old connected snapshot during
    # persistent failures (e.g. credential revocation or a long
    # outage). Once ``now - last_successful_at`` exceeds
    # ``STALE_SECONDS``, errors are surfaced instead of preserved.
    "last_successful_at": 0.0,
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
    """Hard-reset the cached account snapshot — clears the value and
    timestamps. Use in tests and any path where the cached value must
    not be served (e.g. credential change). Bumping the generation
    ensures any in-flight background refresh discards its result
    rather than overwriting post-invalidation data."""
    with _account_snapshot_lock:
        _account_snapshot_cache["value"] = None
        _account_snapshot_cache["fresh_until"] = 0.0
        _account_snapshot_cache["stale_until"] = 0.0
        _account_snapshot_cache["generation"] += 1
        _account_snapshot_cache["last_successful_at"] = 0.0


def expire_kalshi_account_cache() -> None:
    """Mark the cached snapshot as expired (past STALE) WITHOUT
    clearing the stored value. The next ``build_kalshi_account_snapshot``
    call will go to the sync fetch path with ``previous`` set to the
    old value — so if the fresh Kalshi fetch errors, the connected
    snapshot is preserved (codex round-8 P2 on PR #40). Use this for
    user-initiated force-refreshes; use ``invalidate`` only when the
    stored value itself must not be served (e.g. tests, credential
    rotation)."""
    with _account_snapshot_lock:
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


# Bug #25: previously these chunks ran sequentially inside a request
# handler, so an account with N×100 distinct tickers paid an N-call
# latency tax serially. The chunks are independent reads, so a
# bounded ``ThreadPoolExecutor`` fans them out in parallel while
# capping peak concurrent calls to Kalshi (default 4). Single-chunk
# accounts skip the pool entirely and stay synchronous.
_REMOTE_MARKET_LOOKUP_CHUNK_SIZE = 100
_REMOTE_MARKET_LOOKUP_MAX_WORKERS = 4


def _remote_market_lookup(client: KalshiAccountClient, tickers: set[str]) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    sorted_tickers = sorted(tickers)
    chunks = [
        sorted_tickers[index : index + _REMOTE_MARKET_LOOKUP_CHUNK_SIZE]
        for index in range(0, len(sorted_tickers), _REMOTE_MARKET_LOOKUP_CHUNK_SIZE)
    ]
    if not chunks:
        return {}

    if len(chunks) == 1:
        chunk_results: list[list[dict[str, Any]]] = [client.list_markets_by_tickers(chunks[0])]
    else:
        max_workers = min(_REMOTE_MARKET_LOOKUP_MAX_WORKERS, len(chunks))
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="kalshi-market-lookup",
        ) as executor:
            chunk_results = list(executor.map(client.list_markets_by_tickers, chunks))

    lookup: dict[str, dict[str, Any]] = {}
    for chunk_payload in chunk_results:
        for market in chunk_payload or []:
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

    # Codex round-7 P2: read the cache state and generation token
    # atomically under the lock. Without the lock, an invalidate
    # racing this read could be visible to the generation check but
    # not to the cache-state check (or vice versa), letting a
    # background refresh slip past discard.
    with _account_snapshot_lock:
        now = time.monotonic()
        cached = _account_snapshot_cache["value"]
        fresh_until = _account_snapshot_cache["fresh_until"]
        stale_until = _account_snapshot_cache["stale_until"]
        observed_generation = _account_snapshot_cache["generation"]

    if cached is not None and now < fresh_until:
        # Fresh: return cached, no refresh.
        return cached

    if cached is not None and now < stale_until:
        # Stale-but-usable: serve cached immediately and fire a
        # single background refresh so the next poll lands fresh
        # data. Pass ``observed_generation`` so the worker can
        # discard its result if the cache is invalidated (or another
        # commit lands) before its Kalshi fetch finishes.
        _maybe_start_background_refresh(observed_generation)
        return cached

    # No cache (or beyond STALE_SECONDS): block on a synchronous
    # fetch. Codex round-13 P2: also coalesce with any in-flight
    # background refresh by acquiring the bg slot (with a timeout
    # so we don't hang behind a stuck worker). If the bg landed a
    # fresh commit while we were waiting, return that cached value
    # — saves an upstream call in the auto-poll-races-bg case.
    acquired_slot = _background_refresh_slot.acquire(
        timeout=_ACCOUNT_SNAPSHOT_SYNC_COALESCE_TIMEOUT_SECONDS
    )
    try:
        with _account_snapshot_lock:
            now = time.monotonic()
            cached = _account_snapshot_cache["value"]
            if cached is not None and now < _account_snapshot_cache["fresh_until"]:
                return cached
            previous = cached
            result = _build_kalshi_account_snapshot_uncached(db, client=None)
            return _commit_refresh_result(result, previous=previous)
    finally:
        if acquired_slot:
            _background_refresh_slot.release()


def _commit_refresh_result(
    snapshot: KalshiAccountRead,
    *,
    previous: KalshiAccountRead | None,
) -> KalshiAccountRead:
    """Store the new snapshot in the cache with the appropriate TTL,
    and return what should be served to the caller. Bumps the
    generation token on every write so any in-flight background
    refresh checking against an older generation discards its result.

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
            # Codex round-9 P2: only preserve the connected cache if
            # its last successful refresh is within the stale horizon.
            # Past that, the snapshot is too old to keep serving as
            # ``connected`` — surface the error so credential
            # revocations / extended outages aren't silently masked.
            now = time.monotonic()
            last_successful = _account_snapshot_cache["last_successful_at"]
            stale_horizon = last_successful + _ACCOUNT_SNAPSHOT_STALE_SECONDS
            if now < stale_horizon:
                # Extend fresh_until by ERROR_BACKOFF so we don't
                # immediately retry. Codex round-10 P2: ALSO restore
                # ``stale_until`` to the cached snapshot's original
                # stale horizon — without this, ``expire`` had reset
                # ``stale_until`` to 0 and subsequent polls would
                # skip SWR and block on a sync fetch.
                #
                # Codex round-11 P2: cap ``fresh_until`` at the
                # stale horizon. Without this cap, an error near the
                # horizon (e.g. at 119s with STALE=120s, ERROR_BACKOFF=15s)
                # could push fresh_until to 134s — serving a
                # ``connected`` snapshot past the configured maximum
                # stale age. Cap ensures the next poll after the
                # horizon falls through to the sync path, where the
                # round-9 check surfaces the error.
                _account_snapshot_cache["fresh_until"] = min(
                    now + _ACCOUNT_SNAPSHOT_ERROR_BACKOFF_SECONDS,
                    stale_horizon,
                )
                _account_snapshot_cache["stale_until"] = stale_horizon
                _account_snapshot_cache["generation"] += 1
                logger.warning(
                    "kalshi_account_refresh_error_preserved_cache",
                    extra={"error_message": snapshot.error_message},
                )
                return previous
            logger.warning(
                "kalshi_account_refresh_error_cache_too_stale_to_preserve",
                extra={
                    "error_message": snapshot.error_message,
                    "cache_age_seconds": now - last_successful,
                },
            )
        # No good prior cache to preserve — store the error with a
        # short backoff so the next request retries soon.
        now = time.monotonic()
        _account_snapshot_cache["value"] = snapshot
        _account_snapshot_cache["fresh_until"] = now + _ACCOUNT_SNAPSHOT_ERROR_BACKOFF_SECONDS
        _account_snapshot_cache["stale_until"] = now + _ACCOUNT_SNAPSHOT_ERROR_BACKOFF_SECONDS
        _account_snapshot_cache["generation"] += 1
        return snapshot
    _store_account_snapshot(snapshot)
    return snapshot


def _store_account_snapshot(snapshot: KalshiAccountRead) -> None:
    now = time.monotonic()
    _account_snapshot_cache["value"] = snapshot
    _account_snapshot_cache["fresh_until"] = now + _ACCOUNT_SNAPSHOT_FRESH_SECONDS
    _account_snapshot_cache["stale_until"] = now + _ACCOUNT_SNAPSHOT_STALE_SECONDS
    # Codex round-7 P2: bump the generation on every commit so a
    # background refresh whose Kalshi fetch started before this write
    # will see a generation mismatch and discard its result instead
    # of overwriting a fresher cache.
    _account_snapshot_cache["generation"] += 1
    # Codex round-9 P2: track the last successful connected commit
    # so the preserve-cache-on-error path can bound how long stale
    # data is served during persistent failures.
    if snapshot.status == "connected":
        _account_snapshot_cache["last_successful_at"] = now


def _maybe_start_background_refresh(observed_generation: int) -> None:
    """Fire a daemon thread to refresh the cache in place. At most
    one refresh runs at a time — concurrent stale-hits race on
    ``_background_refresh_slot.acquire(blocking=False)`` and only
    one wins. The losers return immediately; the winner spawns the
    worker, which releases the slot in ``finally``."""
    if not _background_refresh_slot.acquire(blocking=False):
        return
    try:
        # Codex round-7 P2: pass the caller's observed generation in,
        # rather than re-reading it inside the worker. If we read it
        # in the worker, an invalidate that fires between the caller
        # observing the cache state and the worker starting would
        # already have bumped the generation — so the worker's
        # captured value would match the post-invalidate state and
        # the commit guard would not fire.
        thread = threading.Thread(
            target=_run_background_refresh,
            args=(observed_generation,),
            daemon=True,
            name="kalshi-account-refresh",
        )
        thread.start()
    except BaseException:
        # If the thread couldn't start, release the slot so a future
        # stale-hit can retry.
        _background_refresh_slot.release()
        raise


def _run_background_refresh(start_generation: int) -> None:
    try:
        with SessionLocal() as db:
            snapshot = _build_kalshi_account_snapshot_uncached(db, client=None)
        with _account_snapshot_lock:
            if start_generation != _account_snapshot_cache["generation"]:
                # Cache was invalidated or a force-refresh / sync
                # commit landed while we were fetching. Our snapshot
                # is pre-event and may be stale relative to whatever
                # bumped the generation — discard rather than
                # overwriting the current cache value.
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
