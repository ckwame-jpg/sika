from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.kalshi import KalshiDemoClient
from app.models import DemoFill, DemoOrder, Market, OutboxEntry, PaperPosition
from app.schemas import DemoOrderCreate, PaperPositionCreate, PaperPositionExit
from app.services.outbox import (
    INTENT_KALSHI_ORDER_CANCEL,
    INTENT_KALSHI_ORDER_SUBMIT,
    enqueue as enqueue_outbox,
    register_intent_handler,
)


def create_paper_position(
    db: Session,
    payload: PaperPositionCreate,
    *,
    user_id: int | None = None,
) -> PaperPosition:
    """Persist a new paper position, attributed to ``user_id``.

    Multi-user batch PR 3: ``user_id`` is keyword-only for safety —
    the endpoint passes it explicitly, and a future caller that
    forgets it will produce a NULL user_id row (visible only in the
    legacy bucket; not a silent leak into another user's view).
    """
    market = db.scalar(select(Market).where(Market.ticker == payload.ticker))
    if not market:
        raise HTTPException(status_code=404, detail="Market not found for ticker")

    position = PaperPosition(
        user_id=user_id,
        market_id=market.id,
        ticker=payload.ticker,
        side=payload.side.lower(),
        quantity=payload.quantity,
        entry_price=payload.entry_price,
        notes=payload.notes,
    )
    db.add(position)
    db.flush()
    return position


def close_paper_position(
    db: Session,
    position_id: int,
    payload: PaperPositionExit,
    *,
    user_id: int | None = None,
) -> PaperPosition:
    """Exit a paper position.

    Multi-user batch PR 3 — ownership check: a position can only be
    closed by the user who created it. Legacy-bucket positions
    (created before multi-user landed) are read-only for everyone;
    nobody can exit them. ``user_id=None`` skips the check for
    single-tenant deployments + the system itself.
    """
    position = db.get(PaperPosition, position_id)
    if not position:
        raise HTTPException(status_code=404, detail="Paper position not found")
    if position.status != "open":
        raise HTTPException(status_code=400, detail="Paper position already closed")
    if user_id is not None:
        if position.user_id is None or (
            position.user and position.user.is_legacy_bucket
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Legacy paper positions are read-only — they were created "
                    "before multi-user landed and have no clear owner."
                ),
            )
        if position.user_id != user_id:
            raise HTTPException(
                status_code=403,
                detail="You can only exit positions you opened.",
            )

    position.exit_price = payload.exit_price
    position.closed_at = datetime.now(timezone.utc)
    position.status = "closed"
    position.pnl = round((payload.exit_price - position.entry_price) * position.quantity, 4)
    db.flush()
    return position


def create_demo_order(
    db: Session,
    payload: DemoOrderCreate,
    *,
    user_id: int | None = None,
) -> DemoOrder:
    """Submit a demo order via the bug-#31 transactional outbox.

    The local ``DemoOrder`` row is written together with an
    ``OutboxEntry`` recording the intent to call Kalshi; both rows
    persist atomically when the request commits. The actual Kalshi
    submission happens out-of-band in the outbox drain worker
    (``_drain_outbox_job`` in scheduler.py), so a network glitch or
    crash between the local commit and the Kalshi call can no longer
    leave the two sides in disagreement.
    """
    if not payload.approved:
        raise HTTPException(status_code=400, detail="Manual approval is required before submitting demo orders")

    market = db.scalar(select(Market).where(Market.ticker == payload.ticker))
    if not market:
        raise HTTPException(status_code=404, detail="Market not found for ticker")

    client_order_id = str(uuid4())
    order = DemoOrder(
        user_id=user_id,
        market_id=market.id,
        ticker=payload.ticker,
        client_order_id=client_order_id,
        side=payload.side.lower(),
        action=payload.action.lower(),
        quantity=payload.quantity,
        limit_price=payload.limit_price,
        approved_by_user=True,
        # ``submitting`` matches the pre-#31 status the UI already
        # knows; the drain worker advances to ``resting`` /
        # ``submission_failed`` once Kalshi responds.
        status="submitting",
    )
    db.add(order)
    db.flush()

    enqueue_outbox(
        db,
        intent_kind=INTENT_KALSHI_ORDER_SUBMIT,
        target_kind="demo_order",
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


def cancel_demo_order(
    db: Session,
    order_id: int,
    *,
    user_id: int | None = None,
) -> DemoOrder:
    """Cancel a demo order via the outbox (bug #31).

    Multi-user batch PR 3 — ownership check: an order can only be
    cancelled by the user who submitted it. Legacy-bucket orders
    are read-only. ``user_id=None`` skips the check for single-tenant
    deployments + system callers.

    Cancel is enqueued for the worker so the local state shift is
    atomic with the recorded intent. The DemoOrder must already have a
    Kalshi-assigned ``kalshi_order_id`` (i.e., the submit-side outbox
    entry has already drained) — otherwise there's nothing on Kalshi
    to cancel and we return 400 the same as before.
    """
    order = db.get(DemoOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Demo order not found")
    if user_id is not None:
        if order.user_id is None or (order.user and order.user.is_legacy_bucket):
            raise HTTPException(
                status_code=403,
                detail="Legacy demo orders are read-only.",
            )
        if order.user_id != user_id:
            raise HTTPException(
                status_code=403,
                detail="You can only cancel orders you submitted.",
            )
    if not order.kalshi_order_id:
        raise HTTPException(status_code=400, detail="Demo order was not accepted by Kalshi")

    # ``cancelling`` is the new intermediate state — operator UI uses
    # this to suppress the cancel button while the drain processes the
    # request, similar to how ``submitting`` works on the create side.
    order.status = "cancelling"
    db.flush()
    enqueue_outbox(
        db,
        intent_kind=INTENT_KALSHI_ORDER_CANCEL,
        target_kind="demo_order",
        target_id=order.id,
        payload={"kalshi_order_id": order.kalshi_order_id},
    )
    return order


def _kalshi_submit_handler(db: Session, entry: OutboxEntry) -> None:
    """Outbox handler for ``kalshi_order_submit`` — calls
    ``KalshiDemoClient.create_order`` and reconciles the DemoOrder row.
    Idempotent: Kalshi's ``client_order_id`` ensures a re-submit of the
    same payload returns the same order.
    """
    payload = dict(entry.payload or {})
    order = db.get(DemoOrder, int(entry.target_id)) if entry.target_id is not None else None
    if order is None:
        raise RuntimeError(f"DemoOrder id={entry.target_id} not found for outbox entry {entry.id}")

    client = KalshiDemoClient()
    response = client.create_order(
        ticker=payload["ticker"],
        side=payload["side"],
        action=payload["action"],
        quantity=int(payload["quantity"]),
        limit_price=float(payload["limit_price"]),
        time_in_force=payload.get("time_in_force"),
    )

    remote_order = response.get("order", {})
    now = datetime.now(timezone.utc)
    order.kalshi_order_id = remote_order.get("order_id") or order.kalshi_order_id
    order.client_order_id = remote_order.get("client_order_id") or order.client_order_id
    order.status = remote_order.get("status") or "submitted"
    order.request_body = response.get("request", {})
    order.response_body = response
    order.submitted_at = order.submitted_at or now
    order.last_synced_at = now


def _kalshi_cancel_handler(db: Session, entry: OutboxEntry) -> None:
    """Outbox handler for ``kalshi_order_cancel`` — calls
    ``KalshiDemoClient.cancel_order``. Idempotent: cancelling an
    already-cancelled order is a no-op on Kalshi.
    """
    payload = dict(entry.payload or {})
    order = db.get(DemoOrder, int(entry.target_id)) if entry.target_id is not None else None
    if order is None:
        raise RuntimeError(f"DemoOrder id={entry.target_id} not found for outbox entry {entry.id}")

    client = KalshiDemoClient()
    response = client.cancel_order(payload["kalshi_order_id"])
    order.status = (response.get("order") or {}).get("status") or "cancelled"
    order.response_body = response
    order.last_synced_at = datetime.now(timezone.utc)


# Wire handlers at import time so the scheduler's drain job picks them
# up. ``register_intent_handler`` is idempotent on re-registration so
# importing this module twice (e.g., in test fixtures) is safe.
register_intent_handler(INTENT_KALSHI_ORDER_SUBMIT, _kalshi_submit_handler)
register_intent_handler(INTENT_KALSHI_ORDER_CANCEL, _kalshi_cancel_handler)


def reconcile_demo_state(db: Session, client: KalshiDemoClient | None = None) -> None:
    kalshi_client = client or KalshiDemoClient()
    try:
        orders = kalshi_client.list_orders()
        fills = kalshi_client.list_fills()
    except Exception:
        return

    orders_by_client_id = {item.get("client_order_id"): item for item in orders if item.get("client_order_id")}
    for local in db.scalars(select(DemoOrder)).all():
        remote = orders_by_client_id.get(local.client_order_id)
        if remote:
            local.kalshi_order_id = remote.get("order_id") or local.kalshi_order_id
            local.status = remote.get("status") or local.status
            local.response_body = remote
            local.last_synced_at = datetime.now(timezone.utc)

    known_fill_ids = {item[0] for item in db.execute(select(DemoFill.kalshi_fill_id)).all() if item[0]}
    local_orders = {item.kalshi_order_id: item for item in db.scalars(select(DemoOrder)).all() if item.kalshi_order_id}
    for remote_fill in fills:
        fill_id = remote_fill.get("fill_id")
        order_id = remote_fill.get("order_id")
        if fill_id in known_fill_ids or order_id not in local_orders:
            continue
        db.add(
            DemoFill(
                demo_order_id=local_orders[order_id].id,
                kalshi_fill_id=fill_id,
                count=float(remote_fill.get("count_fp") or remote_fill.get("count") or 0),
                price=float(remote_fill.get("yes_price_dollars") or remote_fill.get("no_price_dollars") or 0),
                side=remote_fill.get("side") or local_orders[order_id].side,
                raw_data=remote_fill,
            )
        )
    db.flush()
