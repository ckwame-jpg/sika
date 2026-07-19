"""Real Kalshi combos (parlays) — combinability resolution, preview,
and the two-phase mint-then-order submit handler.

A Kalshi combo is a REAL market minted from a "multivariate event
collection": pick legs (market + side), the collection mints a combo
market with its own order book, payout = all legs hit. Flow:

  preview  (tray)  → resolve collection + lookup existing market; NEVER mints
  place            → KalshiOrder(kind="combo") + legs + one outbox entry
  outbox handler   → lookup → mint if absent → CHECKPOINT COMMIT → order
                     on the minted ticker with the persisted client_order_id

Idempotency triple-guard (money-critical):
  1. lookup-before-mint on every attempt (a retry never re-mints),
  2. an explicit ``db.commit()`` right after the mint persists the
     ticker even if the process dies before the order call (the outbox
     drain loop only commits at the END of a handler),
  3. Kalshi dedupes the order itself on ``client_order_id``.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.kalshi import KalshiTradeClient, parse_price_dollars
from app.models import KalshiComboLeg, KalshiOrder, Market
from app.schemas import (
    KalshiComboLegCreate,
    KalshiComboOrderCreate,
    KalshiComboPreviewRead,
    KalshiComboPreviewRequest,
)
from app.services.kalshi_orders import (
    client_for_order,
    enforce_order_cost_cap,
    environment_for_base_url,
    require_user_credentials,
)
from app.services.outbox import (
    INTENT_KALSHI_COMBO_SUBMIT,
    enqueue as enqueue_outbox,
    register_intent_handler,
)
from app.services.user_kalshi import get_user_credentials

logger = logging.getLogger(__name__)

# ── Collections cache ───────────────────────────────────────────────
# The tray previews on every leg-set change; collections move slowly.
# 10-minute in-process cache per (base_url, associated_event_ticker)
# keeps keystroke-level previews off Kalshi's API.

_COLLECTIONS_TTL_SECONDS = 600.0
_collections_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_collections_lock = threading.Lock()


def _cached_collections(client: KalshiTradeClient, associated_event_ticker: str) -> list[dict]:
    key = (client.base_url, associated_event_ticker)
    now = time.monotonic()
    with _collections_lock:
        hit = _collections_cache.get(key)
        if hit and now - hit[0] < _COLLECTIONS_TTL_SECONDS:
            return hit[1]
    rows = client.get_multivariate_event_collections(
        status="open", associated_event_ticker=associated_event_ticker
    )
    with _collections_lock:
        _collections_cache[key] = (now, rows)
    return rows


def clear_collections_cache() -> None:
    """Test hook."""
    with _collections_lock:
        _collections_cache.clear()


# ── Combinability resolution ────────────────────────────────────────


def _load_leg_markets(
    db: Session, legs: list[KalshiComboLegCreate]
) -> tuple[dict[str, Market] | None, str | None]:
    markets: dict[str, Market] = {}
    for leg in legs:
        ticker = leg.ticker.upper()
        market = db.scalar(select(Market).where(Market.ticker == ticker))
        if market is None:
            return None, f"{ticker} is not a tracked market"
        if not market.event_ticker:
            return None, f"{ticker} has no event mapping yet"
        markets[ticker] = market
    return markets, None


def resolve_collection_for_legs(
    db: Session,
    client: KalshiTradeClient,
    legs: list[KalshiComboLegCreate],
) -> tuple[dict | None, dict[str, Market] | None, str | None]:
    """Find an open collection whose events cover EVERY leg.

    Returns (collection, leg_markets, reason). Exactly one of
    collection / reason is set. Checks, in order of most-specific
    failure: tracked markets, per-collection event coverage, leg-count
    bounds, per-event leg limits, yes-only events vs NO legs.
    """
    markets, reason = _load_leg_markets(db, legs)
    if markets is None:
        return None, None, reason

    leg_events = [markets[leg.ticker.upper()].event_ticker for leg in legs]
    collections = _cached_collections(client, leg_events[0])
    if not collections:
        return None, None, "no open kalshi combo collection covers these games"

    best_reason = "these picks aren't combinable on kalshi"
    for collection in collections:
        associated = {
            entry.get("ticker"): entry
            for entry in (collection.get("associated_events") or [])
            if entry.get("ticker")
        }
        missing = [event for event in leg_events if event not in associated]
        if missing:
            best_reason = f"{missing[0]} isn't in kalshi's combo collection"
            continue

        size_min = collection.get("size_min") or 2
        size_max = collection.get("size_max") or 6
        if not size_min <= len(legs) <= size_max:
            best_reason = (
                f"this collection takes {size_min}–{size_max} legs (you have {len(legs)})"
            )
            continue

        per_event_counts: dict[str, int] = {}
        for event in leg_events:
            per_event_counts[event] = per_event_counts.get(event, 0) + 1
        event_violation = None
        for event, count in per_event_counts.items():
            event_max = associated[event].get("size_max") or 1
            if count > event_max:
                event_violation = f"only {event_max} leg(s) allowed from {event}"
                break
        if event_violation:
            best_reason = event_violation
            continue

        yes_only_violation = None
        for leg in legs:
            event = markets[leg.ticker.upper()].event_ticker
            if associated[event].get("is_yes_only") and leg.side.lower() == "no":
                yes_only_violation = f"{leg.ticker.upper()} only combines as a YES pick"
                break
        if yes_only_violation:
            best_reason = yes_only_violation
            continue

        return collection, markets, None

    return None, None, best_reason


def _selected_markets(
    markets: dict[str, Market], legs: list[KalshiComboLegCreate]
) -> list[dict[str, str]]:
    return [
        {
            "market_ticker": leg.ticker.upper(),
            "event_ticker": markets[leg.ticker.upper()].event_ticker,
            "side": leg.side.lower(),
        }
        for leg in legs
    ]


# ── Preview (tray combinability) ────────────────────────────────────


def preview_kalshi_combo(
    db: Session,
    payload: KalshiComboPreviewRequest,
    *,
    user_id: int | None = None,
) -> KalshiComboPreviewRead:
    """Tray check — returns a REASON instead of raising, because the
    tray wants copy to show, not a toast of stack traces. Never mints."""
    creds = get_user_credentials(db, user_id) if user_id is not None else None
    if creds is None:
        return KalshiComboPreviewRead(
            combinable=False, reason="connect kalshi in settings first"
        )
    if len(payload.legs) < 2:
        return KalshiComboPreviewRead(combinable=False, reason="add at least 2 legs")

    client = KalshiTradeClient(
        key_id=creds.key_id,
        private_key_pem=creds.private_key_pem.encode("utf-8"),
        base_url=creds.base_url,
    )
    try:
        collection, markets, reason = resolve_collection_for_legs(db, client, payload.legs)
    except Exception:
        logger.warning("combo preview: collections fetch failed", exc_info=True)
        return KalshiComboPreviewRead(
            combinable=False, reason="couldn't reach kalshi to check — try again"
        )
    if collection is None or markets is None:
        return KalshiComboPreviewRead(combinable=False, reason=reason)

    implied: float | None = None
    if all(leg.entry_price is not None for leg in payload.legs):
        implied = 1.0
        for leg in payload.legs:
            implied *= float(leg.entry_price)  # type: ignore[arg-type]
        implied = round(implied, 4)

    existing_ticker: str | None = None
    quote_bid: float | None = None
    quote_ask: float | None = None
    try:
        existing = client.lookup_combo_market(
            collection["collection_ticker"], _selected_markets(markets, payload.legs)
        )
        if existing:
            existing_ticker = existing.get("market_ticker")
            if existing_ticker:
                market_payload = client.get_market(existing_ticker)
                quote_bid = parse_price_dollars(market_payload.get("yes_bid_dollars"))
                quote_ask = parse_price_dollars(market_payload.get("yes_ask_dollars"))
    except Exception:
        # Lookup/quote are nice-to-haves — combinability stands.
        logger.info("combo preview: lookup/quote skipped", exc_info=True)

    return KalshiComboPreviewRead(
        combinable=True,
        collection_ticker=collection.get("collection_ticker"),
        existing_market_ticker=existing_ticker,
        implied_price=implied,
        quote_yes_bid=quote_bid,
        quote_yes_ask=quote_ask,
    )


# ── Placement ───────────────────────────────────────────────────────


def create_kalshi_combo_order(
    db: Session,
    payload: KalshiComboOrderCreate,
    *,
    user_id: int | None = None,
) -> KalshiOrder:
    """Persist the combo order + legs + ONE outbox entry atomically.
    The mint + order happen out-of-band in the drain handler."""
    if not payload.approved:
        raise HTTPException(
            status_code=400,
            detail="Manual approval is required before submitting real orders",
        )
    creds = require_user_credentials(db, user_id)
    enforce_order_cost_cap(db, quantity=payload.quantity, limit_price=payload.limit_price)

    client = KalshiTradeClient(
        key_id=creds.key_id,
        private_key_pem=creds.private_key_pem.encode("utf-8"),
        base_url=creds.base_url,
    )
    # Server-side re-validation — tray state can be stale (localStorage
    # rehydration) and the schema alone can't prove combinability.
    collection, markets, reason = resolve_collection_for_legs(db, client, payload.legs)
    if collection is None or markets is None:
        raise HTTPException(status_code=400, detail=f"Not combinable: {reason}")

    base_url = creds.base_url.rstrip("/")
    client_order_id = str(uuid4())
    order = KalshiOrder(
        user_id=user_id,
        kind="combo",
        ticker=None,  # set by the handler after mint
        environment=environment_for_base_url(base_url),
        base_url=base_url,
        client_order_id=client_order_id,
        # Buying YES on the minted combo market = "all legs hit as picked".
        side="yes",
        action="buy",
        quantity=payload.quantity,
        limit_price=payload.limit_price,
        collection_ticker=collection.get("collection_ticker"),
        approved_by_user=True,
        status="submitting",
    )
    db.add(order)
    db.flush()

    for index, leg in enumerate(payload.legs):
        market = markets[leg.ticker.upper()]
        db.add(
            KalshiComboLeg(
                kalshi_order_id=order.id,
                leg_index=index,
                market_ticker=leg.ticker.upper(),
                event_ticker=market.event_ticker,
                side=leg.side.lower(),
                entry_price=leg.entry_price,
                market_title=leg.market_title,
                subject_name=leg.subject_name,
                stat_key=leg.stat_key,
                threshold=leg.threshold,
            )
        )
    db.flush()

    enqueue_outbox(
        db,
        intent_kind=INTENT_KALSHI_COMBO_SUBMIT,
        target_kind="kalshi_order",
        target_id=order.id,
        payload={
            "client_order_id": client_order_id,
            "collection_ticker": order.collection_ticker,
            "selected_markets": _selected_markets(markets, payload.legs),
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "time_in_force": payload.time_in_force,
        },
    )
    return order


# ── Outbox handler (mint → checkpoint → order) ──────────────────────


def _kalshi_combo_submit_handler(db: Session, entry) -> None:
    payload = dict(entry.payload or {})
    order = db.get(KalshiOrder, int(entry.target_id)) if entry.target_id is not None else None
    if order is None:
        raise RuntimeError(f"KalshiOrder id={entry.target_id} not found for outbox entry {entry.id}")

    client = client_for_order(db, order)
    now = datetime.now(timezone.utc)

    # Phase 1 — ensure the combo market exists. Lookup first so a
    # retry after "minted but crashed" NEVER mints twice.
    if not order.ticker:
        selected = payload["selected_markets"]
        collection_ticker = payload["collection_ticker"]
        try:
            existing = client.lookup_combo_market(collection_ticker, selected)
            if existing is None:
                minted = client.create_combo_market(collection_ticker, selected)
            else:
                minted = existing
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                order.status = "mint_failed"
                order.error_detail = (
                    f"Kalshi couldn't create the combo market "
                    f"({exc.response.status_code}): {exc.response.text[:500]}"
                )
                order.last_synced_at = now
                logger.warning("combo mint failed for order %s: %s", order.id, order.error_detail)
                return
            raise

        order.ticker = minted.get("market_ticker")
        order.combo_event_ticker = minted.get("event_ticker")
        response_body = dict(order.response_body or {})
        response_body["mint"] = minted
        order.response_body = response_body
        if not order.ticker:
            order.status = "mint_failed"
            order.error_detail = f"Mint response had no market_ticker: {minted}"
            return
        # CHECKPOINT: the drain loop only commits when the handler
        # returns. Persist the minted ticker NOW so a crash between
        # mint and order leaves a resumable row (retry → lookup hits).
        db.commit()

    # Phase 2 — normal limit order on the minted combo market.
    try:
        response = client.create_order(
            ticker=order.ticker,
            side="yes",
            action="buy",
            quantity=int(payload["quantity"]),
            limit_price=float(payload["limit_price"]),
            time_in_force=payload.get("time_in_force"),
            client_order_id=payload.get("client_order_id") or order.client_order_id,
        )
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            order.status = "submission_failed"
            order.error_detail = (
                f"Kalshi rejected the combo order ({exc.response.status_code}): "
                f"{exc.response.text[:500]}"
            )
            order.last_synced_at = datetime.now(timezone.utc)
            logger.warning("combo order %s rejected: %s", order.id, order.error_detail)
            return
        raise

    remote_order = response.get("order", {})
    now = datetime.now(timezone.utc)
    order.kalshi_order_id = remote_order.get("order_id") or order.kalshi_order_id
    order.status = remote_order.get("status") or "submitted"
    order.request_body = response.get("request", {})
    response_body = dict(order.response_body or {})
    response_body["order"] = response
    order.response_body = response_body
    order.submitted_at = order.submitted_at or now
    order.last_synced_at = now


register_intent_handler(INTENT_KALSHI_COMBO_SUBMIT, _kalshi_combo_submit_handler)
