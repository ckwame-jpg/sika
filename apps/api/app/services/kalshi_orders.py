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
- Guardrails: ``approved`` must be true (400), quantity × limit_price
  must not exceed the operator's per-order cap (400), and the user
  must have credentials configured (409).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.kalshi import KalshiTradeClient
from app.config import get_settings
from app.models import KalshiOrder, KalshiOrderFill, Market
from app.schemas import KalshiOrderCreate
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
OPEN_STATUSES = ("pending_submission", "submitting", "resting", "cancelling")


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


def enforce_order_cost_cap(db: Session, *, quantity: int, limit_price: float) -> None:
    cap = effective_kalshi_max_order_cost(db)
    cost = quantity * limit_price
    if cost > cap:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Order cost ${cost:.2f} exceeds the ${cap:.2f} per-order cap. "
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
    enforce_order_cost_cap(db, quantity=payload.quantity, limit_price=payload.limit_price)

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
        limit_price=payload.limit_price,
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
    creds = get_user_credentials(db, order.user_id) if order.user_id is not None else None
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


def _kalshi_live_submit_handler(db: Session, entry) -> None:
    """Drain handler for ``kalshi_live_order_submit``.

    4xx from Kalshi (insufficient balance, market closed, bad price)
    is TERMINAL — retrying an invalid order forever would just spam the
    exchange, so mark ``submission_failed`` + surface the body in
    ``error_detail`` and let the entry complete. 5xx/network errors
    propagate so the outbox retries with backoff.
    """
    payload = dict(entry.payload or {})
    order = db.get(KalshiOrder, int(entry.target_id)) if entry.target_id is not None else None
    if order is None:
        raise RuntimeError(f"KalshiOrder id={entry.target_id} not found for outbox entry {entry.id}")

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
            order.error_detail = (
                f"Kalshi rejected the order ({exc.response.status_code}): "
                f"{exc.response.text[:500]}"
            )
            order.last_synced_at = datetime.now(timezone.utc)
            logger.warning("kalshi live order %s rejected: %s", order.id, order.error_detail)
            return
        raise

    remote_order = response.get("order", {})
    now = datetime.now(timezone.utc)
    order.kalshi_order_id = remote_order.get("order_id") or order.kalshi_order_id
    order.status = remote_order.get("status") or "submitted"
    order.request_body = response.get("request", {})
    order.response_body = response
    order.submitted_at = order.submitted_at or now
    order.last_synced_at = now


def _kalshi_live_cancel_handler(db: Session, entry) -> None:
    """Drain handler for ``kalshi_live_order_cancel``. Cancelling an
    already-cancelled order is a no-op on Kalshi, so this is naturally
    idempotent."""
    payload = dict(entry.payload or {})
    order = db.get(KalshiOrder, int(entry.target_id)) if entry.target_id is not None else None
    if order is None:
        raise RuntimeError(f"KalshiOrder id={entry.target_id} not found for outbox entry {entry.id}")

    client = client_for_order(db, order)
    response = client.cancel_order(payload["kalshi_order_id"])
    order.status = (response.get("order") or {}).get("status") or "cancelled"
    order.response_body = response
    order.last_synced_at = datetime.now(timezone.utc)


register_intent_handler(INTENT_KALSHI_LIVE_ORDER_SUBMIT, _kalshi_live_submit_handler)
register_intent_handler(INTENT_KALSHI_LIVE_ORDER_CANCEL, _kalshi_live_cancel_handler)


# ── Reconcile ───────────────────────────────────────────────────────


def _float_or_none(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def reconcile_kalshi_live_state(
    db: Session,
    *,
    client_factory=None,
) -> None:
    """Sync non-terminal real orders + fills from Kalshi.

    Orders are grouped by (user_id, base_url) so each group talks to
    the host it was placed on with that user's key material. Failures
    are per-group best-effort (same stance as ``reconcile_demo_state``):
    a dead sandbox must not block live syncs and vice versa.

    ``client_factory(db, order) -> client`` is injectable for tests;
    defaults to ``client_for_order``.
    """
    factory = client_factory or client_for_order
    pending = db.scalars(
        select(KalshiOrder).where(KalshiOrder.status.in_(OPEN_STATUSES))
    ).all()
    groups: dict[tuple[int | None, str], list[KalshiOrder]] = {}
    for order in pending:
        groups.setdefault((order.user_id, order.base_url), []).append(order)

    for (_user_id, _base_url), orders in groups.items():
        try:
            client = factory(db, orders[0])
            remote_orders = client.list_orders()
            remote_fills = client.list_fills()
        except Exception:
            logger.warning(
                "kalshi live reconcile skipped for user=%s host=%s",
                _user_id,
                _base_url,
                exc_info=True,
            )
            continue

        by_client_id = {
            item.get("client_order_id"): item
            for item in remote_orders
            if item.get("client_order_id")
        }
        now = datetime.now(timezone.utc)
        for local in orders:
            remote = by_client_id.get(local.client_order_id)
            if remote:
                local.kalshi_order_id = remote.get("order_id") or local.kalshi_order_id
                local.status = remote.get("status") or local.status
                local.response_body = remote
                local.last_synced_at = now

        known_fill_ids = {
            row[0]
            for row in db.execute(select(KalshiOrderFill.kalshi_fill_id)).all()
            if row[0]
        }
        by_kalshi_id = {o.kalshi_order_id: o for o in orders if o.kalshi_order_id}
        for remote_fill in remote_fills:
            fill_id = remote_fill.get("fill_id")
            order_id = remote_fill.get("order_id")
            if fill_id in known_fill_ids or order_id not in by_kalshi_id:
                continue
            db.add(
                KalshiOrderFill(
                    kalshi_order_id=by_kalshi_id[order_id].id,
                    kalshi_fill_id=fill_id,
                    count=float(remote_fill.get("count_fp") or remote_fill.get("count") or 0),
                    price=float(
                        remote_fill.get("yes_price_dollars")
                        or remote_fill.get("no_price_dollars")
                        or 0
                    ),
                    side=remote_fill.get("side") or by_kalshi_id[order_id].side,
                    fee_dollars=_float_or_none(
                        remote_fill.get("fee_cost") or remote_fill.get("fee_cost_dollars")
                    ),
                    raw_data=remote_fill,
                )
            )
    db.flush()
