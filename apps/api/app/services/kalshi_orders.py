"""Real Kalshi order placement (singles) — live-money sibling of the
demo-order pipeline in ``services/orders.py``.

Design contract (see plan "kalshi live orders"):

- Orders route to the environment the user chose on /settings/kalshi:
  ``build_trade_client_for_user`` honors the stored ``base_url``. The
  row's ``environment``/``base_url`` are STAMPED AT CREATE TIME so
  cancel/reconcile always talk to the host the order lives on, even if
  the user flips settings while it rests.
- Submission is asynchronous via the transactional outbox with the
  DISTINCT ``kalshi_live_*`` intents — the sandbox demo handlers are
  untouched, and a dead-lettered live intent is unambiguously a
  real-money incident.
- Idempotency: the persisted ``client_order_id`` rides in the outbox
  payload and is passed to ``create_order`` verbatim, so a retry after
  a crash re-submits the SAME order (Kalshi dedupes) instead of
  minting a duplicate.
- Guardrails: ``approved`` must be true (400), principal plus the
  worst-case taker fee must not exceed the operator's per-order cap
  (400), and the user must have credentials configured (409).
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from inspect import Parameter, signature
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.kalshi import KalshiTradeClient, quantize_price_cents
from app.config import get_settings
from app.models import KalshiOrder, KalshiOrderFill, Market
from app.schemas import KalshiOrderCreate
from app.services.kalshi_fees import worst_case_taker_fee_dollars
from app.services.operator_settings import effective_kalshi_max_order_cost
from app.services.outbox import (
    INTENT_KALSHI_LIVE_ORDER_CANCEL,
    INTENT_KALSHI_LIVE_ORDER_SUBMIT,
    enqueue as enqueue_outbox,
    register_intent_handler,
)
from app.services.user_kalshi import get_user_credentials

logger = logging.getLogger(__name__)

# Statuses that still need reconcile attention / count as "open" in the
# UI. Terminal: cancelled, executed, submission_failed, mint_failed.
OPEN_STATUSES = (
    "pending_submission",
    "submitting",
    "submitted",
    "resting",
    "cancelling",
)
TERMINAL_STATUSES = (
    "cancelled",
    # Kalshi's current REST API spells this with one "l". Normalize new
    # reads, but include it so any rows written by older code still heal.
    "canceled",
    "executed",
    "submission_failed",
    "mint_failed",
)
TERMINAL_FILL_SYNC_BATCH = 20
TERMINAL_FILL_SYNC_CANDIDATE_LIMIT = TERMINAL_FILL_SYNC_BATCH * 5
RECONCILE_PAGE_LIMIT = 1000
RECONCILE_MAX_PAGES = 100

# Order history can exceed one reconcile scan's page guard. Keep a bounded,
# process-local continuation cursor per credential/host group so later passes
# resume instead of repeatedly starting at page one. Natural exhaustion,
# repeated/stale cursors, and cursor-start failures clear the entry safely.
_ORDER_PAGE_RESUME_MAX_GROUPS = 256
_order_page_resume_lock = threading.Lock()
_order_page_resume_cursors: dict[tuple[Any, ...], str] = {}

# A terminal order can itself own more fills than one guarded scan can read.
# Continue those scans per exchange order, with a bounded cache so hostile or
# stale identifiers cannot grow process memory without limit.
_FILL_PAGE_RESUME_MAX_ORDERS = 2048
_fill_page_resume_lock = threading.Lock()
_fill_page_resume_cursors: dict[tuple[int | None, str, str], str] = {}

# The terminal candidate query is deliberately bounded before materializing.
# Rotate its offset when a window is full so credential failures concentrated
# in the oldest window cannot permanently hide healthy groups behind it.
_terminal_candidate_lock = threading.Lock()
_terminal_candidate_offset = 0


def environment_for_base_url(base_url: str) -> str:
    """Classify a stored base_url as ``live`` or ``demo``. Anything
    that isn't the configured sandbox host counts as live — the
    conservative direction (real-money rails apply)."""
    demo = get_settings().kalshi_demo_base_url.rstrip("/")
    return "demo" if base_url.rstrip("/") == demo else "live"


def require_user_credentials(db: Session, user_id: int | None):
    """The credentials row is the authorization to trade for real —
    no row, no order. 409 (not 400) so the UI can distinguish
    "connect your account" from a bad payload."""
    row = get_user_credentials(db, user_id) if user_id is not None else None
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="Kalshi account not connected. Add credentials in settings/kalshi first.",
        )
    return row


def friendly_kalshi_rejection(prefix: str, exc: httpx.HTTPStatusError) -> str:
    """Exchange rejection → error_detail with the raw body plus, where
    we recognize the failure, the one-line fix the operator needs."""
    detail = f"{prefix} ({exc.response.status_code}): {exc.response.text[:500]}"
    if "insufficient_scope" in exc.response.text:
        detail += (
            " · fix: this api key can't trade — create a new kalshi api key with "
            "full trading permissions and re-save it in settings → kalshi."
        )
    return detail


def enforce_order_cost_cap(db: Session, *, quantity: int, limit_price: float) -> None:
    cap = effective_kalshi_max_order_cost(db)
    # Principal and fee are whole cents. Compare that integer total against
    # the *exact* configured cap: rounding a sub-cent cap upward would let the
    # server approve more exposure than the operator configured.
    principal_cents = round(quantity * limit_price * 100)
    fee_cents = round(worst_case_taker_fee_dollars(quantity, limit_price) * 100)
    total_cents = principal_cents + fee_cents
    cap_decimal = Decimal(str(cap))
    principal = principal_cents / 100
    worst_case_fee = fee_cents / 100
    total = total_cents / 100
    if Decimal(total_cents) / Decimal(100) > cap_decimal:
        cap_display = (
            f"{cap:.2f}"
            if cap_decimal == cap_decimal.quantize(Decimal("0.01"))
            else format(cap_decimal.normalize(), "f")
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Order total ${total:.2f} (principal ${principal:.2f} + "
                f"worst-case taker fee ${worst_case_fee:.2f}) exceeds the "
                f"${cap_display} per-order cap. "
                "Raise the cap in settings/kalshi if this was intentional."
            ),
        )


def create_kalshi_order(
    db: Session,
    payload: KalshiOrderCreate,
    *,
    user_id: int | None = None,
) -> KalshiOrder:
    """Persist a real single-market order + its outbox intent atomically.

    Mirrors ``create_demo_order`` (services/orders.py) with the live
    guardrails layered on. The actual Kalshi call happens in the outbox
    drain worker."""
    if not payload.approved:
        raise HTTPException(
            status_code=400,
            detail="Manual approval is required before submitting real orders",
        )
    creds = require_user_credentials(db, user_id)
    # Snap to Kalshi's 1¢ tick BEFORE the cap check and the persisted
    # row — the exchange rejects sub-cent prices (invalid_price), and
    # american-odds input naturally produces them (e.g. +245 → 0.2899).
    limit_price = quantize_price_cents(payload.limit_price)
    enforce_order_cost_cap(db, quantity=payload.quantity, limit_price=limit_price)

    market = db.scalar(select(Market).where(Market.ticker == payload.ticker))
    if not market:
        raise HTTPException(status_code=404, detail="Market not found for ticker")

    client_order_id = str(uuid4())
    base_url = creds.base_url.rstrip("/")
    order = KalshiOrder(
        user_id=user_id,
        market_id=market.id,
        kind="single",
        ticker=payload.ticker,
        environment=environment_for_base_url(base_url),
        base_url=base_url,
        client_order_id=client_order_id,
        side=payload.side.lower(),
        action=payload.action.lower(),
        quantity=payload.quantity,
        limit_price=limit_price,
        approved_by_user=True,
        status="submitting",
    )
    db.add(order)
    db.flush()

    enqueue_outbox(
        db,
        intent_kind=INTENT_KALSHI_LIVE_ORDER_SUBMIT,
        target_kind="kalshi_order",
        target_id=order.id,
        payload={
            "client_order_id": client_order_id,
            "ticker": payload.ticker,
            "side": order.side,
            "action": order.action,
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "time_in_force": payload.time_in_force,
        },
    )
    return order


def cancel_kalshi_order(
    db: Session,
    order_id: int,
    *,
    user_id: int | None = None,
) -> KalshiOrder:
    """Cancel via the outbox. Ownership: only the submitting user may
    cancel; there is deliberately NO hard-delete for real orders —
    the row is the audit trail."""
    order = db.get(KalshiOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Kalshi order not found")
    if user_id is not None and order.user_id != user_id:
        raise HTTPException(
            status_code=403, detail="You can only cancel orders you submitted."
        )
    if order.status in ("cancelled", "executed", "submission_failed", "mint_failed"):
        raise HTTPException(
            status_code=400, detail=f"Order is already terminal ({order.status})."
        )
    if not order.kalshi_order_id:
        raise HTTPException(
            status_code=409,
            detail="Order has no Kalshi id yet (still submitting) — retry in a moment.",
        )
    order.status = "cancelling"
    db.flush()
    enqueue_outbox(
        db,
        intent_kind=INTENT_KALSHI_LIVE_ORDER_CANCEL,
        target_kind="kalshi_order",
        target_id=order.id,
        payload={"kalshi_order_id": order.kalshi_order_id},
    )
    return order


# Rows the operator may clear from the panel. Failed submissions/mints
# never created anything on the exchange. A cancellation is dismissible
# only after a complete sync proves it has no fills; partial IOC/cancelled
# orders remain an immutable local audit ledger. Resting/submitting stay
# (cancel first), and executed orders always stay.
DISMISSIBLE_STATUSES = ("submission_failed", "mint_failed", "cancelled")


def delete_kalshi_order(
    db: Session,
    order_id: int,
    *,
    user_id: int | None = None,
) -> None:
    """Dismiss a terminal row (owner-only). See DISMISSIBLE_STATUSES
    for the rationale on what can go."""
    order = db.get(KalshiOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Kalshi order not found")
    if user_id is not None and order.user_id != user_id:
        raise HTTPException(
            status_code=403, detail="You can only dismiss orders you submitted."
        )
    if order.status not in DISMISSIBLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Only failed or cancelled orders can be dismissed (this one is "
                f"{order.status}). Cancel resting orders first."
            ),
        )
    if order.status == "cancelled":
        has_persisted_fill = (
            db.scalar(
                select(KalshiOrderFill.id)
                .where(KalshiOrderFill.kalshi_order_id == order.id)
                .limit(1)
            )
            is not None
        )
        authoritative_fill_count = _authoritative_fill_count(order)
        if (
            order.fills_synced_at is None
            or has_persisted_fill
            or authoritative_fill_count != 0
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Only a fully synced zero-fill cancellation can be dismissed; "
                    "orders with fills remain in the live-money audit ledger."
                ),
            )
    db.delete(order)
    db.flush()


def list_kalshi_orders(
    db: Session,
    *,
    user_id: int | None = None,
    open_only: bool = False,
    limit: int = 100,
) -> list[KalshiOrder]:
    stmt = select(KalshiOrder).order_by(KalshiOrder.created_at.desc()).limit(limit)
    if user_id is not None:
        stmt = stmt.where(KalshiOrder.user_id == user_id)
    if open_only:
        stmt = stmt.where(KalshiOrder.status.in_(OPEN_STATUSES))
    return list(db.scalars(stmt).all())


# ── Outbox handlers ─────────────────────────────────────────────────


def client_for_order(db: Session, order: KalshiOrder) -> KalshiTradeClient:
    """Build the trade client for an EXISTING order row.

    Key material comes from the user's current credentials row, but the
    ``base_url`` comes from the ORDER — flipping the settings
    environment must never re-route an already-placed order's cancel or
    reconcile traffic to a different host."""
    creds = (
        get_user_credentials(db, order.user_id) if order.user_id is not None else None
    )
    if creds is None:
        raise RuntimeError(
            f"KalshiOrder id={order.id}: no credentials row for user_id={order.user_id}; "
            "cannot talk to Kalshi (re-connect the account in settings/kalshi)"
        )
    return KalshiTradeClient(
        key_id=creds.key_id,
        private_key_pem=creds.private_key_pem.encode("utf-8"),
        base_url=order.base_url,
    )


# ── Fill import / pagination adapters ──────────────────────────────


def _float_or_none(value) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_kalshi_status(value: Any, fallback: str) -> str:
    status = str(value or fallback).lower()
    return "cancelled" if status == "canceled" else status


def _is_terminal_status(status: str | None) -> bool:
    return (status or "").lower() in TERMINAL_STATUSES


def _valid_order_fill_count_or_none(value: Any) -> float | None:
    """Validate an aggregate order fill count, where zero is meaningful."""
    count = _float_or_none(value)
    if count is None or not math.isfinite(count) or count < 0:
        return None
    return count


def _valid_fill_row_count_or_none(value: Any) -> float | None:
    """Validate a persisted fill quantity; a real fill must be positive."""
    count = _float_or_none(value)
    if count is None or not math.isfinite(count) or count <= 0:
        return None
    return count


def _valid_fill_row_price_or_none(value: Any) -> float | None:
    """Validate a persisted fill price in Kalshi's tradable open interval."""
    price = _float_or_none(value)
    if price is None or not math.isfinite(price) or not 0 < price < 1:
        return None
    return price


def _direct_remote_fill_count(payload: Any) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    counts: list[float] = []
    for key in (
        "fill_count_fp",
        "fill_count",
        "filled_count_fp",
        "filled_count",
        "_preserved_fill_count",
    ):
        value = _valid_order_fill_count_or_none(payload.get(key))
        if value is not None:
            counts.append(value)
    # Fill quantity is monotonic. If a later sparse/inconsistent response is
    # paired with a preserved prior count, never let it erase known fills.
    return max(counts) if counts else None


def _remote_fill_count(payload: Any) -> float | None:
    """Find an expected filled quantity in a create/list-order payload."""
    if not isinstance(payload, Mapping):
        return None
    counts: list[float] = []
    direct = _direct_remote_fill_count(payload)
    if direct is not None:
        counts.append(direct)
    # Submit responses are normalized as {order, raw}; combos wrap that
    # response once more under response_body["order"].
    for key in ("order", "raw"):
        value = _remote_fill_count(payload.get(key))
        if value is not None:
            counts.append(value)
    return max(counts) if counts else None


def _authoritative_fill_count_from_payload(payload: Any) -> float | None:
    """Return a finite final fill count, not merely a known interim count."""
    if not isinstance(payload, Mapping):
        return None
    marker = payload.get("_fill_count_authoritative")
    if marker is False:
        return None
    if marker is True:
        return _remote_fill_count(payload)

    direct = _direct_remote_fill_count(payload)
    if direct is not None and _is_terminal_status(str(payload.get("status") or "")):
        return _remote_fill_count(payload)

    nested_order = payload.get("order")
    nested_authoritative = _authoritative_fill_count_from_payload(nested_order)
    if nested_authoritative is not None and (
        nested_authoritative > 0
        or (
            isinstance(nested_order, Mapping)
            and nested_order.get("_fill_count_authoritative") is True
        )
    ):
        return _remote_fill_count(payload)
    if isinstance(nested_order, Mapping) and _is_terminal_status(
        str(nested_order.get("status") or "")
    ) and nested_order.get("_fill_count_authoritative") is not False:
        # Older normalized create responses kept the count only in ``raw``.
        # A positive value is useful evidence, but a marker-less zero is
        # ambiguous: sparse V2 responses used to be normalized to zero even
        # when an IOC partially filled. New writes carry an explicit marker,
        # while a current/historical flat order record can promote the count.
        legacy_count = _remote_fill_count(payload)
        if legacy_count is not None and legacy_count > 0:
            return legacy_count
    return None


def _authoritative_fill_count(order: KalshiOrder) -> float | None:
    return _authoritative_fill_count_from_payload(order.response_body)


def _fill_count_matches_order_lifecycle(
    order: KalshiOrder,
    fill_count: float | None,
) -> bool:
    """Reject aggregate counts that contradict the local lifecycle facts."""
    if fill_count is None:
        return False
    quantity = _valid_order_fill_count_or_none(order.quantity)
    if quantity is None:
        return False
    if fill_count > quantity and not math.isclose(
        fill_count,
        quantity,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        return False
    if _normalized_kalshi_status(order.status, "") == "executed":
        return math.isclose(
            fill_count,
            quantity,
            rel_tol=0.0,
            abs_tol=1e-6,
        )
    return True


def _response_with_fill_count_provenance(
    payload: Mapping[str, Any],
    *,
    status: str,
    previous_payload: Any = None,
) -> dict[str, Any]:
    """Copy a response while retaining whether its fill count is final.

    A successful cancel response often contains only an order id and status.
    Preserve any previously observed quantity for audit/reconciliation, but do
    not promote an interim resting-order count into terminal proof. A terminal
    response becomes authoritative only when it carries a valid count itself,
    or when an earlier response had already established an authoritative one.
    """
    response_body = dict(payload)
    incoming_count = _remote_fill_count(response_body)
    previous_count = _remote_fill_count(previous_payload)
    previous_authoritative = _authoritative_fill_count_from_payload(
        previous_payload
    )
    if previous_count is not None and (
        incoming_count is None or previous_count > incoming_count
    ):
        response_body["_preserved_fill_count"] = previous_count
    response_body["_fill_count_authoritative"] = bool(
        _is_terminal_status(status)
        and (incoming_count is not None or previous_authoritative is not None)
    )
    return response_body


def _accepted_kwargs(callable_obj, values: dict[str, Any]) -> dict[str, Any]:
    """Pass only keywords supported by a scripted/legacy test client.

    Production ``KalshiTradeClient`` exposes the cursor-aware iterators.
    Older tests intentionally use tiny fakes whose ``list_orders`` and
    ``list_fills`` methods take no arguments and return a flat list. This
    adapter preserves that explicit one-page-complete test contract without
    weakening the production client API or catching arbitrary TypeErrors.
    """
    try:
        params = signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return values
    if any(param.kind is Parameter.VAR_KEYWORD for param in params):
        return values
    names = {param.name for param in params}
    return {key: value for key, value in values.items() if key in names}


def _coerce_page_result(
    result: Any,
    *,
    item_key: str,
) -> tuple[list[dict[str, Any]], str | None]:
    if isinstance(result, tuple) and len(result) == 2:
        items, cursor = result
        return list(items or []), cursor
    if isinstance(result, Mapping):
        return list(result.get(item_key) or []), result.get("cursor")
    # Intentional legacy-fake adapter: a flat list is one fully drained page.
    return list(result or []), None


def _iter_client_pages(
    client,
    *,
    iterator_name: str,
    list_name: str,
    item_key: str,
    order_id: str | None = None,
    ticker: str | None = None,
    cursor: str | None = None,
    limit: int = RECONCILE_PAGE_LIMIT,
    max_pages: int = RECONCILE_MAX_PAGES,
) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
    iterator = getattr(client, iterator_name, None)
    base_kwargs: dict[str, Any] = {
        "limit": limit,
        "max_pages": max_pages,
        "cursor": cursor,
    }
    if order_id is not None:
        base_kwargs["order_id"] = order_id
    if ticker is not None:
        base_kwargs["ticker"] = ticker
    if callable(iterator):
        for result in iterator(**_accepted_kwargs(iterator, base_kwargs)):
            yield _coerce_page_result(result, item_key=item_key)
        return

    list_method = getattr(client, list_name)
    next_cursor = cursor
    pages_fetched = 0
    for _ in range(max(int(max_pages), 1)):
        values: dict[str, Any] = {"limit": limit, "cursor": next_cursor}
        if order_id is not None:
            values["order_id"] = order_id
        if ticker is not None:
            values["ticker"] = ticker
        accepted = _accepted_kwargs(list_method, values)
        result = list_method(**accepted)
        items, next_cursor = _coerce_page_result(result, item_key=item_key)
        pages_fetched += 1
        yield items, next_cursor
        if not next_cursor:
            break
        if "cursor" not in accepted:
            # A fake returned a continuation token but cannot accept it.
            # Stop with the live cursor visible so callers cannot mistake
            # the partial scan for completion.
            logger.warning(
                "%s returned a live cursor but does not accept cursor=; "
                "pagination is incomplete",
                list_name,
            )
            break
    else:
        if next_cursor:
            logger.warning(
                "%s adapter hit max_pages=%d with a live cursor after %d pages",
                list_name,
                max_pages,
                pages_fetched,
            )


def _iter_client_order_pages(
    client,
    *,
    cursor: str | None = None,
) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
    yield from _iter_client_pages(
        client,
        iterator_name="iter_order_pages",
        list_name="list_orders",
        item_key="orders",
        cursor=cursor,
    )


def _iter_client_fill_pages(
    client,
    *,
    order_id: str | None = None,
    cursor: str | None = None,
) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
    yield from _iter_client_pages(
        client,
        iterator_name="iter_fill_pages",
        list_name="list_fills",
        item_key="fills",
        order_id=order_id,
        cursor=cursor,
    )


def _iter_client_historical_order_pages(
    client,
    *,
    ticker: str,
    cursor: str | None = None,
) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
    yield from _iter_client_pages(
        client,
        iterator_name="iter_historical_order_pages",
        list_name="list_historical_orders",
        item_key="orders",
        ticker=ticker,
        cursor=cursor,
    )


def _iter_client_historical_fill_pages(
    client,
    *,
    ticker: str,
    cursor: str | None = None,
) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
    yield from _iter_client_pages(
        client,
        iterator_name="iter_historical_fill_pages",
        list_name="list_historical_fills",
        item_key="fills",
        ticker=ticker,
        cursor=cursor,
    )


def _supports_client_page_source(
    client,
    *,
    iterator_name: str,
    list_name: str,
) -> bool:
    return callable(getattr(client, iterator_name, None)) or callable(
        getattr(client, list_name, None)
    )


def _get_order_page_resume_cursor(group_key: tuple[Any, ...]) -> str | None:
    with _order_page_resume_lock:
        return _order_page_resume_cursors.get(group_key)


def _set_order_page_resume_cursor(
    group_key: tuple[Any, ...],
    cursor: str | None,
) -> None:
    with _order_page_resume_lock:
        if not cursor:
            _order_page_resume_cursors.pop(group_key, None)
            return
        if (
            group_key not in _order_page_resume_cursors
            and len(_order_page_resume_cursors) >= _ORDER_PAGE_RESUME_MAX_GROUPS
        ):
            evicted = next(iter(_order_page_resume_cursors))
            _order_page_resume_cursors.pop(evicted, None)
            logger.warning(
                "evicted Kalshi order-page resume cursor for group=%r; "
                "bounded cursor cache reached %d groups",
                evicted,
                _ORDER_PAGE_RESUME_MAX_GROUPS,
            )
        _order_page_resume_cursors[group_key] = cursor


def _clear_order_page_resume_cursors() -> None:
    """Test hook; production entries clear on natural cursor exhaustion."""
    with _order_page_resume_lock:
        _order_page_resume_cursors.clear()


def _fill_page_resume_key(
    order: KalshiOrder,
    *,
    historical: bool = False,
) -> tuple[int | None, str, str]:
    if not order.kalshi_order_id:
        raise ValueError("Kalshi fill-page resume key requires an exchange order id")
    exchange_order_key = (
        f"historical:{order.kalshi_order_id}"
        if historical
        else order.kalshi_order_id
    )
    return (order.user_id, order.base_url, exchange_order_key)


def _get_fill_page_resume_cursor(
    order_key: tuple[int | None, str, str],
) -> str | None:
    with _fill_page_resume_lock:
        return _fill_page_resume_cursors.get(order_key)


def _set_fill_page_resume_cursor(
    order_key: tuple[int | None, str, str],
    cursor: str | None,
) -> None:
    with _fill_page_resume_lock:
        if not cursor:
            _fill_page_resume_cursors.pop(order_key, None)
            return
        if (
            order_key not in _fill_page_resume_cursors
            and len(_fill_page_resume_cursors) >= _FILL_PAGE_RESUME_MAX_ORDERS
        ):
            evicted = next(iter(_fill_page_resume_cursors))
            _fill_page_resume_cursors.pop(evicted, None)
            logger.warning(
                "evicted Kalshi fill-page resume cursor for order=%r; "
                "bounded cursor cache reached %d orders",
                evicted,
                _FILL_PAGE_RESUME_MAX_ORDERS,
            )
        _fill_page_resume_cursors[order_key] = cursor


def _clear_fill_page_resume_cursors() -> None:
    """Test hook; production entries clear on natural cursor exhaustion."""
    with _fill_page_resume_lock:
        _fill_page_resume_cursors.clear()


def _get_terminal_candidate_offset() -> int:
    with _terminal_candidate_lock:
        return _terminal_candidate_offset


def _advance_terminal_candidate_offset(
    candidate_count: int,
    successful_attempts: int,
) -> None:
    global _terminal_candidate_offset
    with _terminal_candidate_lock:
        if (
            candidate_count < TERMINAL_FILL_SYNC_CANDIDATE_LIMIT
            or successful_attempts > 0
        ):
            _terminal_candidate_offset = 0
        else:
            # Every row in a full window belonged to groups that failed before
            # a remote scan could start. Move past that window once; any real
            # attempt resets to the oldest-first window on the next pass.
            _terminal_candidate_offset += candidate_count


def _clear_terminal_candidate_rotation() -> None:
    """Test hook for the bounded terminal candidate window."""
    global _terminal_candidate_offset
    with _terminal_candidate_lock:
        _terminal_candidate_offset = 0


def _known_fill_ids(db: Session) -> set[str]:
    return {
        row[0]
        for row in db.execute(select(KalshiOrderFill.kalshi_fill_id)).all()
        if row[0]
    }


def _first_present(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _valid_fee_or_none(value: Any) -> float | None:
    fee = _float_or_none(value)
    if fee is None or not math.isfinite(fee) or fee < 0:
        return None
    return fee


def _stored_fee_is_valid(value: Any) -> bool:
    return _valid_fee_or_none(value) is not None


def _fill_values_match(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9)


def _persist_remote_fill(
    db: Session,
    order: KalshiOrder,
    remote_fill: Mapping[str, Any],
    known_fill_ids: set[str],
) -> tuple[bool, float]:
    """Persist one fill; return (valid, newly-added quantity)."""
    fill_id = remote_fill.get("fill_id")
    if not fill_id:
        logger.warning(
            "Kalshi fill for order %s had no fill_id; not retiring sync", order.id
        )
        return False, 0.0
    fill_id = str(fill_id)
    count = _valid_fill_row_count_or_none(
        _first_present(remote_fill, "count_fp", "count")
    )
    price = _valid_fill_row_price_or_none(
        _first_present(remote_fill, "yes_price_dollars", "no_price_dollars")
    )
    fee_dollars = _valid_fee_or_none(
        _first_present(remote_fill, "fee_cost", "fee_cost_dollars")
    )
    incoming_values_valid = count is not None and price is not None
    if not incoming_values_valid:
        logger.warning(
            "Kalshi fill %s for order %s had a malformed/non-finite/"
            "out-of-range count or price; not retiring sync",
            fill_id,
            order.id,
        )
    if fill_id in known_fill_ids:
        existing = db.scalar(
            select(KalshiOrderFill).where(
                KalshiOrderFill.kalshi_fill_id == fill_id
            )
        )
        if existing is None or existing.kalshi_order_id != order.id:
            logger.warning(
                "Known Kalshi fill %s was missing or belonged to another order; "
                "not retiring sync",
                fill_id,
            )
            return False, 0.0
        if not incoming_values_valid:
            return False, 0.0

        stored_count = _valid_fill_row_count_or_none(existing.count)
        stored_price = _valid_fill_row_price_or_none(existing.price)
        stored_fee = _valid_fee_or_none(existing.fee_dollars)
        stored_raw = (
            dict(existing.raw_data)
            if isinstance(existing.raw_data, Mapping)
            else {}
        )
        had_persisted_conflict = "_reconciliation_conflict" in stored_raw
        conflicts: list[str] = []
        if stored_count is not None and not _fill_values_match(stored_count, count):
            conflicts.append(f"count stored={stored_count} remote={count}")
        if stored_price is not None and not _fill_values_match(stored_price, price):
            conflicts.append(f"price stored={stored_price} remote={price}")
        if (
            stored_fee is not None
            and fee_dollars is not None
            and not _fill_values_match(stored_fee, fee_dollars)
        ):
            conflicts.append(f"fee stored={stored_fee} remote={fee_dollars}")
        if conflicts:
            # A valid-looking historical money row that disagrees with the
            # exchange must never be silently rewritten or used as completion
            # proof. Keep it eligible for operator investigation/retry.
            logger.warning(
                "Kalshi fill %s for order %s conflicts with its stored ledger "
                "values (%s); not retiring sync",
                fill_id,
                order.id,
                "; ".join(conflicts),
            )
            stored_raw["_reconciliation_conflict"] = {
                "fields": conflicts,
                "remote_fill": dict(remote_fill),
            }
            existing.raw_data = stored_raw
            return False, 0.0

        if had_persisted_conflict and fee_dollars is None:
            logger.warning(
                "Kalshi fill %s for order %s has a persisted reconciliation "
                "conflict and the latest remote row lacks a valid fee; "
                "not retiring sync",
                fill_id,
                order.id,
            )
            return False, 0.0

        healed = False
        if stored_count is None:
            existing.count = count
            healed = True
        if stored_price is None:
            existing.price = price
            healed = True
        if stored_fee is None and fee_dollars is not None:
            existing.fee_dollars = fee_dollars
            healed = True
        if had_persisted_conflict:
            # A later full remote row now agrees with every stored money field;
            # that is explicit resolution evidence for the persisted conflict.
            existing.raw_data = dict(remote_fill)
            healed = True
        if healed:
            # Heal rows persisted by an earlier pass/API version. The fill id
            # remains the idempotency key.
            existing.raw_data = dict(remote_fill)

        stored_values_valid = (
            _valid_fill_row_count_or_none(existing.count) is not None
            and _valid_fill_row_price_or_none(existing.price) is not None
            and _stored_fee_is_valid(existing.fee_dollars)
        )
        if incoming_values_valid and stored_values_valid:
            return True, 0.0
        if not stored_values_valid:
            logger.warning(
                "Kalshi fill %s for order %s still has invalid stored "
                "count/price/fee values; "
                "not retiring sync",
                fill_id,
                order.id,
            )
        return False, 0.0

    if count is None or price is None:
        return False, 0.0

    db.add(
        KalshiOrderFill(
            kalshi_order_id=order.id,
            kalshi_fill_id=fill_id,
            count=count,
            price=price,
            side=remote_fill.get("side") or order.side,
            fee_dollars=fee_dollars,
            raw_data=dict(remote_fill),
        )
    )
    known_fill_ids.add(fill_id)
    if fee_dollars is None:
        logger.warning(
            "Kalshi fill %s for order %s had a missing/invalid fee; "
            "persisted fill but not retiring sync",
            fill_id,
            order.id,
        )
        return False, count
    return True, count


def _import_fills_for_order(
    db: Session,
    client,
    order: KalshiOrder,
    known_fill_ids: set[str],
    *,
    historical: bool = False,
) -> bool:
    """Drain and idempotently import all fills for one exchange order.

    Completion requires natural cursor exhaustion. If the order payload
    exposes Kalshi's fill count, the persisted fill quantities must also
    reconcile exactly; a page cap, malformed row, or count mismatch leaves
    ``fills_synced_at`` NULL so a later pass can heal it.
    """
    if not order.kalshi_order_id:
        return False

    normalized_status = _normalized_kalshi_status(order.status, "")
    terminal = _is_terminal_status(normalized_status)
    expected_count = (
        _authoritative_fill_count(order)
        if terminal
        else _remote_fill_count(order.response_body)
    )
    if expected_count is None and normalized_status == "executed":
        # Kalshi's executed status means the entire original order filled.
        # This fallback protects older local response bodies that predate
        # fill_count persistence (and avoids treating an archived-away empty
        # current-fills page as proof of completion).
        expected_count = _valid_order_fill_count_or_none(order.quantity)
    expected_count_known = expected_count is not None
    lifecycle_count_consistent = _fill_count_matches_order_lifecycle(
        order,
        expected_count,
    )

    resume_key = _fill_page_resume_key(order, historical=historical)
    start_cursor = _get_fill_page_resume_cursor(resume_key)
    saw_page = False
    last_cursor = start_cursor
    all_rows_valid = True
    try:
        page_iterator = (
            _iter_client_historical_fill_pages(
                client,
                ticker=order.ticker,
                cursor=start_cursor,
            )
            if historical
            else _iter_client_fill_pages(
                client,
                order_id=order.kalshi_order_id,
                cursor=start_cursor,
            )
        )
        for remote_fills, cursor in page_iterator:
            saw_page = True
            last_cursor = cursor
            for remote_fill in remote_fills:
                remote_order_id = remote_fill.get("order_id")
                if historical:
                    # Historical fills are ticker-wide: unlike the current
                    # order-filtered endpoint, a missing order id cannot be
                    # safely attributed to whichever local order is scanning.
                    if not str(remote_order_id or "").strip():
                        all_rows_valid = False
                        logger.warning(
                            "Historical Kalshi fill %s for ticker=%s had no "
                            "order_id; not attributing or retiring sync",
                            remote_fill.get("fill_id"),
                            order.ticker,
                        )
                        continue
                    if str(remote_order_id) != str(order.kalshi_order_id):
                        continue
                # Legacy flat-list current-endpoint fakes are account-wide;
                # filter locally while retaining their missing-id contract.
                elif remote_order_id and remote_order_id != order.kalshi_order_id:
                    continue
                valid, _added_count = _persist_remote_fill(
                    db,
                    order,
                    remote_fill,
                    known_fill_ids,
                )
                all_rows_valid = all_rows_valid and valid
    except Exception:
        if saw_page and last_cursor != start_cursor:
            _set_fill_page_resume_cursor(resume_key, last_cursor)
        elif start_cursor:
            # Saved cursors can expire. Make the next pass restart safely.
            _set_fill_page_resume_cursor(resume_key, None)
        raise

    repeated_resume_cursor = bool(
        start_cursor and last_cursor and last_cursor == start_cursor
    )
    if repeated_resume_cursor:
        logger.warning(
            "Kalshi %sfill pagination repeated resume cursor for local order %s; "
            "clearing it to avoid a permanent loop",
            "historical " if historical else "",
            order.id,
        )
        _set_fill_page_resume_cursor(resume_key, None)
    else:
        # A live cursor means the max-page guard stopped this scan; natural
        # exhaustion clears the entry and permits completion below.
        _set_fill_page_resume_cursor(resume_key, last_cursor)

    drained = saw_page and not last_cursor and not repeated_resume_cursor

    # Validate the entire stored ledger after flushing possible heals/adds.
    # Existing bad rows are just as unsafe as malformed rows received today.
    db.flush()
    stored_values = db.execute(
        select(
            KalshiOrderFill.count,
            KalshiOrderFill.price,
            KalshiOrderFill.fee_dollars,
            KalshiOrderFill.raw_data,
        ).where(KalshiOrderFill.kalshi_order_id == order.id)
    ).all()
    local_count = 0.0
    ledger_values_complete = True
    for stored_count, stored_price, stored_fee, stored_raw in stored_values:
        valid_count = _valid_fill_row_count_or_none(stored_count)
        if (
            valid_count is None
            or _valid_fill_row_price_or_none(stored_price) is None
            or not _stored_fee_is_valid(stored_fee)
            or (
                isinstance(stored_raw, Mapping)
                and "_reconciliation_conflict" in stored_raw
            )
        ):
            ledger_values_complete = False
            continue
        local_count += valid_count

    count_matches = expected_count_known and math.isclose(
        local_count,
        expected_count,
        rel_tol=0.0,
        abs_tol=1e-6,
    )
    # A resting order can acquire more fills later even when today's cursor
    # and current fill_count reconcile. Only a terminal lifecycle state makes
    # fill-ledger completion durable.
    complete = (
        terminal
        and drained
        and all_rows_valid
        and count_matches
        and lifecycle_count_consistent
        and ledger_values_complete
    )
    if complete:
        order.fills_synced_at = datetime.now(timezone.utc)
    elif terminal:
        logger.warning(
            "Kalshi %sfills incomplete for local order %s: drained=%s valid=%s "
            "ledger_values_complete=%s expected_count_known=%s local_count=%s "
            "expected_count=%s lifecycle_count_consistent=%s last_cursor=%r",
            "historical " if historical else "",
            order.id,
            drained,
            all_rows_valid,
            ledger_values_complete,
            expected_count_known,
            local_count,
            expected_count,
            lifecycle_count_consistent,
            last_cursor,
        )
    return complete


def _sync_fills_after_submit(
    db: Session,
    client,
    order: KalshiOrder,
    response: Mapping[str, Any],
) -> None:
    """Best-effort structured-fill sync immediately after submission."""
    fill_count = _remote_fill_count(response)
    if fill_count is not None and fill_count > 0:
        if not order.kalshi_order_id:
            logger.warning(
                "Kalshi submit for local order %s reported fills but no order_id",
                order.id,
            )
            return
        try:
            _import_fills_for_order(db, client, order, _known_fill_ids(db))
        except Exception:
            # The money-moving request already succeeded. Do not retry the
            # submit merely because its read-only fill fetch failed; leave
            # NULL so reconciliation can safely retry the import.
            logger.warning(
                "immediate Kalshi fill import failed for local order %s",
                order.id,
                exc_info=True,
            )
        return

    authoritative_count = _authoritative_fill_count(order)
    if (
        _is_terminal_status(order.status)
        and authoritative_count == 0
        and _fill_count_matches_order_lifecycle(order, authoritative_count)
    ):
        # Explicit matching-engine proof that this terminal order had no fills.
        order.fills_synced_at = datetime.now(timezone.utc)
    elif (
        _is_terminal_status(order.status)
        and authoritative_count is not None
        and not _fill_count_matches_order_lifecycle(order, authoritative_count)
    ):
        logger.warning(
            "Kalshi order %s reported terminal status=%s with contradictory "
            "fill_count=%s for quantity=%s; not retiring sync",
            order.id,
            order.status,
            authoritative_count,
            order.quantity,
        )


def _kalshi_live_submit_handler(db: Session, entry) -> None:
    """Drain handler for ``kalshi_live_order_submit``.

    4xx from Kalshi (insufficient balance, market closed, bad price)
    is TERMINAL — retrying an invalid order forever would just spam the
    exchange, so mark ``submission_failed`` + surface the body in
    ``error_detail`` and let the entry complete. 5xx/network errors
    propagate so the outbox retries with backoff.
    """
    payload = dict(entry.payload or {})
    order = (
        db.get(KalshiOrder, int(entry.target_id))
        if entry.target_id is not None
        else None
    )
    if order is None:
        raise RuntimeError(
            f"KalshiOrder id={entry.target_id} not found for outbox entry {entry.id}"
        )

    client = client_for_order(db, order)
    try:
        response = client.create_order(
            ticker=payload["ticker"],
            side=payload["side"],
            action=payload["action"],
            quantity=int(payload["quantity"]),
            limit_price=float(payload["limit_price"]),
            time_in_force=payload.get("time_in_force"),
            # The PERSISTED id — this is the double-submit guard.
            client_order_id=payload.get("client_order_id") or order.client_order_id,
        )
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            order.status = "submission_failed"
            order.error_detail = friendly_kalshi_rejection(
                "Kalshi rejected the order", exc
            )
            order.last_synced_at = datetime.now(timezone.utc)
            # The exchange rejected the submission, so no exchange order or
            # fill can exist for this local intent.
            order.fills_synced_at = order.last_synced_at
            logger.warning(
                "kalshi live order %s rejected: %s", order.id, order.error_detail
            )
            return
        raise

    remote_order = response.get("order", {})
    now = datetime.now(timezone.utc)
    order.kalshi_order_id = remote_order.get("order_id") or order.kalshi_order_id
    order.status = _normalized_kalshi_status(
        remote_order.get("status"),
        "submitted",
    )
    order.request_body = response.get("request", {})
    stored_response = _response_with_fill_count_provenance(
        response,
        status=order.status,
        previous_payload=order.response_body,
    )
    order.response_body = stored_response
    order.submitted_at = order.submitted_at or now
    order.last_synced_at = now
    _sync_fills_after_submit(db, client, order, stored_response)
    if (
        order.status == "cancelled"
        and payload.get("time_in_force") == "immediate_or_cancel"
        and _remote_fill_count(response) == 0
    ):
        # Fill-now that found no liquidity — make the outcome explicit
        # so the panel row reads as "nothing happened", not a mystery.
        order.error_detail = (
            "no fill available up to your limit — the book was empty. "
            "nothing was charged; try again or rest a bid."
        )


def _kalshi_live_cancel_handler(db: Session, entry) -> None:
    """Drain handler for ``kalshi_live_order_cancel``. Cancelling an
    already-cancelled order is a no-op on Kalshi, so this is naturally
    idempotent."""
    payload = dict(entry.payload or {})
    order = (
        db.get(KalshiOrder, int(entry.target_id))
        if entry.target_id is not None
        else None
    )
    if order is None:
        raise RuntimeError(
            f"KalshiOrder id={entry.target_id} not found for outbox entry {entry.id}"
        )

    client = client_for_order(db, order)
    response = client.cancel_order(payload["kalshi_order_id"])
    previous_response = order.response_body
    order.status = _normalized_kalshi_status(
        (response.get("order") or {}).get("status"),
        "cancelled",
    )
    order.response_body = _response_with_fill_count_provenance(
        response,
        status=order.status,
        previous_payload=previous_response,
    )
    order.last_synced_at = datetime.now(timezone.utc)
    # Cancelling a resting order creates the terminal boundary at which the
    # full fill ledger can be proven. Keep it eligible even if an older build
    # incorrectly stamped a partial resting snapshot.
    order.fills_synced_at = None


register_intent_handler(INTENT_KALSHI_LIVE_ORDER_SUBMIT, _kalshi_live_submit_handler)
register_intent_handler(INTENT_KALSHI_LIVE_ORDER_CANCEL, _kalshi_live_cancel_handler)


# ── Reconcile ───────────────────────────────────────────────────────


def _merge_historical_order_metadata(
    client,
    *,
    group_key: tuple[int | None, str],
    terminal_orders: list[KalshiOrder],
    by_client_id: dict[str, dict[str, Any]],
) -> None:
    """Best-effort lookup for terminal orders archived out of live results.

    Kalshi's historical order endpoint has no order-id filter. Narrow by the
    local ticker, page with a bounded process cursor, and match both client and
    exchange ids. Once every requested row is found, natural exhaustion is not
    required: the exact order metadata is already authoritative for its final
    fill count.
    """
    if not _supports_client_page_source(
        client,
        iterator_name="iter_historical_order_pages",
        list_name="list_historical_orders",
    ):
        return

    missing_by_ticker: dict[str, list[KalshiOrder]] = {}
    for order in terminal_orders:
        if order.client_order_id in by_client_id:
            continue
        # Every row here is terminal but explicitly *unsynced*. Do not trust a
        # legacy zero count enough to skip history: older sparse V2 create
        # responses were normalized to zero and can mask a real partial IOC.
        missing_by_ticker.setdefault(order.ticker, []).append(order)

    for ticker, missing_orders in missing_by_ticker.items():
        resume_key: tuple[Any, ...] = (*group_key, "historical_orders", ticker)
        start_cursor = _get_order_page_resume_cursor(resume_key)
        last_cursor = start_cursor
        saw_page = False
        remaining_by_client = {
            order.client_order_id: order for order in missing_orders
        }
        remaining_by_exchange = {
            order.kalshi_order_id: order
            for order in missing_orders
            if order.kalshi_order_id
        }
        try:
            for remote_orders, next_cursor in _iter_client_historical_order_pages(
                client,
                ticker=ticker,
                cursor=start_cursor,
            ):
                saw_page = True
                last_cursor = next_cursor
                for item in remote_orders:
                    local = remaining_by_client.get(
                        str(item.get("client_order_id") or "")
                    ) or remaining_by_exchange.get(str(item.get("order_id") or ""))
                    if local is None:
                        continue
                    by_client_id[local.client_order_id] = item
                    remaining_by_client.pop(local.client_order_id, None)
                    if local.kalshi_order_id:
                        remaining_by_exchange.pop(local.kalshi_order_id, None)
                if not remaining_by_client:
                    last_cursor = None
                    break
        except Exception:
            if saw_page and last_cursor != start_cursor:
                _set_order_page_resume_cursor(resume_key, last_cursor)
            elif start_cursor:
                _set_order_page_resume_cursor(resume_key, None)
            logger.warning(
                "Kalshi historical order reconcile failed for ticker=%s",
                ticker,
                exc_info=True,
            )
            continue

        repeated_resume_cursor = bool(
            start_cursor and last_cursor and last_cursor == start_cursor
        )
        if repeated_resume_cursor:
            logger.warning(
                "Kalshi historical order pagination repeated resume cursor "
                "for ticker=%s; clearing it",
                ticker,
            )
            _set_order_page_resume_cursor(resume_key, None)
        else:
            _set_order_page_resume_cursor(resume_key, last_cursor)


def reconcile_kalshi_live_state(
    db: Session,
    *,
    client_factory=None,
) -> None:
    """Sync open orders plus terminal orders whose fill ledger is unconfirmed.

    Orders are grouped by (user_id, base_url) so each group talks to
    the host it was placed on with that user's key material. Failures
    are per-group best-effort (same stance as ``reconcile_demo_state``):
    a dead sandbox must not block live syncs and vice versa.

    ``client_factory(db, order) -> client`` is injectable for tests;
    defaults to ``client_for_order``.
    """
    factory = client_factory or client_for_order
    open_orders = list(
        db.scalars(
            select(KalshiOrder).where(KalshiOrder.status.in_(OPEN_STATUSES))
        ).all()
    )
    terminal_candidate_offset = _get_terminal_candidate_offset()
    terminal_orders = list(
        db.scalars(
            select(KalshiOrder)
            .where(
                KalshiOrder.status.in_(TERMINAL_STATUSES),
                KalshiOrder.fills_synced_at.is_(None),
                KalshiOrder.kalshi_order_id.is_not(None),
            )
            # First pass is creation-oldest-first. Successfully contacted but
            # still-incomplete rows rotate behind never-attempted rows via
            # last_synced_at, so an irreconcilable record cannot starve the
            # rest of the historical backfill forever.
            .order_by(
                func.coalesce(
                    KalshiOrder.last_synced_at,
                    KalshiOrder.created_at,
                ).asc(),
                KalshiOrder.created_at.asc(),
                KalshiOrder.id.asc(),
            )
            .offset(terminal_candidate_offset)
            .limit(TERMINAL_FILL_SYNC_CANDIDATE_LIMIT)
        ).all()
    )
    eligible = terminal_orders + open_orders
    groups: dict[tuple[int | None, str], list[KalshiOrder]] = {}
    for order in eligible:
        groups.setdefault((order.user_id, order.base_url), []).append(order)

    known_fill_ids = _known_fill_ids(db)
    terminal_attempts = 0
    for group_key, grouped_orders in groups.items():
        _user_id, _base_url = group_key
        group_open = [order for order in grouped_orders if order.status in OPEN_STATUSES]
        terminal_slots = max(TERMINAL_FILL_SYNC_BATCH - terminal_attempts, 0)
        group_terminal = [
            order for order in grouped_orders if order.status not in OPEN_STATUSES
        ][:terminal_slots]
        orders = group_terminal + group_open
        if not orders:
            continue
        start_order_cursor = _get_order_page_resume_cursor(group_key)
        last_order_cursor = start_order_cursor
        saw_order_page = False
        try:
            client = factory(db, orders[0])
            by_client_id: dict[str, dict[str, Any]] = {}
            for remote_orders, next_cursor in _iter_client_order_pages(
                client,
                cursor=start_order_cursor,
            ):
                saw_order_page = True
                last_order_cursor = next_cursor
                for item in remote_orders:
                    client_order_id = item.get("client_order_id")
                    if client_order_id:
                        by_client_id[client_order_id] = item
        except Exception:
            if saw_order_page and last_order_cursor != start_order_cursor:
                _set_order_page_resume_cursor(group_key, last_order_cursor)
            elif start_order_cursor:
                # The saved cursor may have expired or been rejected. Clear it
                # so the next pass safely restarts at page one instead of
                # retrying the same bad token forever.
                _set_order_page_resume_cursor(group_key, None)
            logger.warning(
                "kalshi live order reconcile skipped for user=%s host=%s",
                _user_id,
                _base_url,
                exc_info=True,
            )
            continue

        if last_order_cursor and last_order_cursor == start_order_cursor:
            logger.warning(
                "Kalshi order pagination repeated resume cursor for user=%s host=%s; "
                "clearing it to avoid a permanent loop",
                _user_id,
                _base_url,
            )
            _set_order_page_resume_cursor(group_key, None)
        else:
            # Live cursor means the max-page guard stopped this scan; empty
            # means the cycle naturally drained and the next pass restarts.
            _set_order_page_resume_cursor(group_key, last_order_cursor)

        _merge_historical_order_metadata(
            client,
            group_key=group_key,
            terminal_orders=group_terminal,
            by_client_id=by_client_id,
        )

        # Count only groups whose client/order read succeeded. Missing or bad
        # credentials therefore cannot consume the whole terminal batch.
        terminal_attempts += len(group_terminal)
        now = datetime.now(timezone.utc)
        for local in group_terminal:
            # A successful remote order scan is a real sync attempt even when
            # its fill cursor later fails or the expected quantity mismatches;
            # recording it provides fair oldest-attempted-first rotation.
            local.last_synced_at = now
        for local in orders:
            remote = by_client_id.get(local.client_order_id)
            if remote:
                previous_response = local.response_body
                local.kalshi_order_id = remote.get("order_id") or local.kalshi_order_id
                local.status = _normalized_kalshi_status(
                    remote.get("status"),
                    local.status,
                )
                local.response_body = _response_with_fill_count_provenance(
                    remote,
                    status=local.status,
                    previous_payload=previous_response,
                )
                local.last_synced_at = now

        # Preserve the existing efficient account-wide pass so partial fills
        # on still-open orders appear in the ledger too. Bind against the
        # WHOLE eligible group (including pre-existing terminal rows).
        by_kalshi_id = {o.kalshi_order_id: o for o in orders if o.kalshi_order_id}
        try:
            for remote_fills, _cursor in _iter_client_fill_pages(client):
                for remote_fill in remote_fills:
                    local = by_kalshi_id.get(remote_fill.get("order_id"))
                    if local is None:
                        continue
                    _persist_remote_fill(
                        db,
                        local,
                        remote_fill,
                        known_fill_ids,
                    )
            # Targeted completion checks below query persisted quantities.
            db.flush()
        except Exception:
            logger.warning(
                "kalshi live account-wide fill reconcile failed for user=%s host=%s",
                _user_id,
                _base_url,
                exc_info=True,
            )

        # A terminal exchange status is not enough: drain the order-filtered
        # fills cursor and reconcile quantities before setting completion.
        for local in orders:
            if (
                not _is_terminal_status(local.status)
                or local.fills_synced_at is not None
                or not local.kalshi_order_id
            ):
                continue
            complete = False
            try:
                complete = _import_fills_for_order(
                    db,
                    client,
                    local,
                    known_fill_ids,
                )
            except Exception:
                logger.warning(
                    "kalshi live targeted fill reconcile failed for local order %s",
                    local.id,
                    exc_info=True,
                )
            if complete or not _supports_client_page_source(
                client,
                iterator_name="iter_historical_fill_pages",
                list_name="list_historical_fills",
            ):
                continue
            try:
                _import_fills_for_order(
                    db,
                    client,
                    local,
                    known_fill_ids,
                    historical=True,
                )
            except Exception:
                logger.warning(
                    "kalshi historical targeted fill reconcile failed for "
                    "local order %s",
                    local.id,
                    exc_info=True,
                )
    _advance_terminal_candidate_offset(len(terminal_orders), terminal_attempts)
    db.flush()
