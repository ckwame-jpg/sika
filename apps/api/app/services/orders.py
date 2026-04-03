from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.kalshi import KalshiDemoClient
from app.models import DemoFill, DemoOrder, Market, PaperPosition
from app.schemas import DemoOrderCreate, PaperPositionCreate, PaperPositionExit


def create_paper_position(db: Session, payload: PaperPositionCreate) -> PaperPosition:
    market = db.scalar(select(Market).where(Market.ticker == payload.ticker))
    if not market:
        raise HTTPException(status_code=404, detail="Market not found for ticker")

    position = PaperPosition(
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


def close_paper_position(db: Session, position_id: int, payload: PaperPositionExit) -> PaperPosition:
    position = db.get(PaperPosition, position_id)
    if not position:
        raise HTTPException(status_code=404, detail="Paper position not found")
    if position.status != "open":
        raise HTTPException(status_code=400, detail="Paper position already closed")

    position.exit_price = payload.exit_price
    position.closed_at = datetime.now(timezone.utc)
    position.status = "closed"
    position.pnl = round((payload.exit_price - position.entry_price) * position.quantity, 4)
    db.flush()
    return position


def create_demo_order(db: Session, payload: DemoOrderCreate, client: KalshiDemoClient | None = None) -> DemoOrder:
    if not payload.approved:
        raise HTTPException(status_code=400, detail="Manual approval is required before submitting demo orders")

    market = db.scalar(select(Market).where(Market.ticker == payload.ticker))
    if not market:
        raise HTTPException(status_code=404, detail="Market not found for ticker")

    order = DemoOrder(
        market_id=market.id,
        ticker=payload.ticker,
        client_order_id=str(uuid4()),
        side=payload.side.lower(),
        action=payload.action.lower(),
        quantity=payload.quantity,
        limit_price=payload.limit_price,
        approved_by_user=True,
        status="submitting",
    )
    db.add(order)
    db.flush()

    kalshi_client = client or KalshiDemoClient()
    try:
        response = kalshi_client.create_order(
            ticker=payload.ticker,
            side=order.side,
            action=order.action,
            quantity=order.quantity,
            limit_price=order.limit_price,
            time_in_force=payload.time_in_force,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        order.status = "submission_failed"
        db.flush()
        raise HTTPException(status_code=502, detail=f"Kalshi demo order submission failed: {exc}") from exc

    remote_order = response.get("order", {})
    order.kalshi_order_id = remote_order.get("order_id")
    order.client_order_id = remote_order.get("client_order_id") or order.client_order_id
    order.status = remote_order.get("status") or "submitted"
    order.request_body = response.get("request", {})
    order.response_body = response
    order.submitted_at = datetime.now(timezone.utc)
    order.last_synced_at = order.submitted_at
    db.flush()
    return order


def cancel_demo_order(db: Session, order_id: int, client: KalshiDemoClient | None = None) -> DemoOrder:
    order = db.get(DemoOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Demo order not found")
    if not order.kalshi_order_id:
        raise HTTPException(status_code=400, detail="Demo order was not accepted by Kalshi")

    kalshi_client = client or KalshiDemoClient()
    try:
        response = kalshi_client.cancel_order(order.kalshi_order_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Kalshi demo order cancel failed: {exc}") from exc

    order.status = (response.get("order") or {}).get("status") or "cancelled"
    order.response_body = response
    order.last_synced_at = datetime.now(timezone.utc)
    db.flush()
    return order


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
