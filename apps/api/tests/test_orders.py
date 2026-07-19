from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.clients.kalshi import KalshiDemoClient
from app.models import Market, OutboxEntry, PaperPosition
from app.schemas import DemoOrderCreate, PaperPositionCreate, PaperPositionExit
from app.services.orders import cancel_demo_order, close_paper_position, create_demo_order, create_paper_position
from app.services.outbox import (
    INTENT_KALSHI_ORDER_SUBMIT,
    STATUS_DONE,
    STATUS_PENDING,
    drain_once,
)


class FakeDemoClient:
    """Stand-in for ``KalshiDemoClient`` used by the outbox drain in tests."""

    def __init__(self):
        self.seen_client_order_ids: list[str | None] = []

    def create_order(self, *, ticker, side, action, quantity, limit_price, time_in_force, client_order_id=None):
        # Recorded so tests can prove the handler passes the PERSISTED
        # id (idempotent retries) instead of letting the client mint a
        # fresh uuid per attempt.
        self.seen_client_order_ids.append(client_order_id)
        return {
            "request": {"ticker": ticker},
            "order": {
                "order_id": "ord_123",
                "client_order_id": client_order_id or "client_123",
                "status": "resting",
            },
        }

    def cancel_order(self, order_id):
        return {"order": {"order_id": order_id, "status": "cancelled"}}


def test_sign_request_produces_base64_signature(tmp_path: Path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "kalshi.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    client = KalshiDemoClient(key_id="abc", private_key_path=key_path, base_url="https://demo-api.kalshi.co/trade-api/v2")

    signature = client.sign_request("POST", "/trade-api/v2/portfolio/orders", "1711814400000")

    assert isinstance(signature, str)
    assert len(signature) > 20


def test_demo_orders_require_manual_approval(db_session):
    market = Market(ticker="NBA-TEST", title="Test market", status="open")
    db_session.add(market)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        create_demo_order(
            db_session,
            DemoOrderCreate(ticker="NBA-TEST", side="yes", quantity=1, limit_price=0.51, approved=False),
        )

    assert exc.value.status_code == 400


def test_demo_order_create_atomically_writes_outbox_entry(db_session):
    """Bug #31: ``create_demo_order`` now writes the local DemoOrder
    AND an outbox entry in the same transaction. The Kalshi call
    happens later in the drain worker."""
    market = Market(ticker="NBA-TEST", title="Test market", status="open")
    db_session.add(market)
    db_session.commit()

    order = create_demo_order(
        db_session,
        DemoOrderCreate(ticker="NBA-TEST", side="yes", quantity=2, limit_price=0.55, approved=True),
    )

    # Local row exists in ``submitting`` state — Kalshi has NOT been
    # called yet; the drain worker will advance to ``resting`` /
    # ``submission_failed`` once the handler runs.
    assert order.status == "submitting"
    assert order.kalshi_order_id is None

    # Outbox entry was enqueued in the same transaction, pointing at
    # this order.
    entries = db_session.scalars(
        select(OutboxEntry).where(OutboxEntry.target_id == order.id)
    ).all()
    assert len(entries) == 1
    assert entries[0].intent_kind == INTENT_KALSHI_ORDER_SUBMIT
    assert entries[0].status == STATUS_PENDING
    assert entries[0].payload["client_order_id"] == order.client_order_id
    assert entries[0].payload["ticker"] == "NBA-TEST"


def test_demo_order_drain_reconciles_remote_status(db_session, monkeypatch):
    """Submit + drain round-trip — after the outbox drains, the order
    reflects the Kalshi response and the outbox entry is marked done."""
    monkeypatch.setattr("app.services.orders.KalshiDemoClient", FakeDemoClient)
    market = Market(ticker="NBA-TEST", title="Test market", status="open")
    db_session.add(market)
    db_session.commit()

    order = create_demo_order(
        db_session,
        DemoOrderCreate(ticker="NBA-TEST", side="yes", quantity=2, limit_price=0.55, approved=True),
    )
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["succeeded"] == 1
    assert counts["failed"] == 0

    db_session.refresh(order)
    assert order.kalshi_order_id == "ord_123"
    assert order.status == "resting"
    assert order.submitted_at is not None

    entries = db_session.scalars(
        select(OutboxEntry).where(OutboxEntry.target_id == order.id)
    ).all()
    assert entries[0].status == STATUS_DONE
    assert entries[0].completed_at is not None


def test_demo_order_drain_passes_persisted_client_order_id(db_session, monkeypatch):
    """Money-safety: the submit handler must pass the client_order_id
    PERSISTED on the order row, so an outbox retry re-submits the same
    order (Kalshi dedupes on the id) instead of minting a duplicate
    under a fresh uuid. This was a real bug — the client used to
    generate its own uuid per call."""
    seen: list[str | None] = []

    class RecordingClient(FakeDemoClient):
        def create_order(self, **kwargs):
            seen.append(kwargs.get("client_order_id"))
            return super().create_order(**kwargs)

    monkeypatch.setattr("app.services.orders.KalshiDemoClient", RecordingClient)
    market = Market(ticker="NBA-IDEM", title="Idempotency market", status="open")
    db_session.add(market)
    db_session.commit()

    order = create_demo_order(
        db_session,
        DemoOrderCreate(ticker="NBA-IDEM", side="yes", quantity=1, limit_price=0.5, approved=True),
    )
    db_session.commit()
    assert order.client_order_id

    drain_once(db_session)
    assert seen == [order.client_order_id]


def test_demo_order_cancel_roundtrip_via_outbox(db_session, monkeypatch):
    """Cancel path mirrors submit: enqueue a cancel intent, drain
    flips the Kalshi side, local row reflects the cancelled status."""
    monkeypatch.setattr("app.services.orders.KalshiDemoClient", FakeDemoClient)
    market = Market(ticker="NBA-CANCEL", title="Cancel test", status="open")
    db_session.add(market)
    db_session.commit()

    order = create_demo_order(
        db_session,
        DemoOrderCreate(ticker="NBA-CANCEL", side="yes", quantity=1, limit_price=0.5, approved=True),
    )
    db_session.commit()
    drain_once(db_session)  # submit drains, order.kalshi_order_id set
    db_session.refresh(order)
    assert order.kalshi_order_id == "ord_123"

    cancel_demo_order(db_session, order.id)
    db_session.commit()
    db_session.refresh(order)
    # Immediate state shift to ``cancelling`` so the UI can hide the
    # cancel button while the drain processes.
    assert order.status == "cancelling"

    drain_once(db_session)
    db_session.refresh(order)
    assert order.status == "cancelled"

    cancel_entries = db_session.scalars(
        select(OutboxEntry)
        .where(OutboxEntry.target_id == order.id)
        .where(OutboxEntry.intent_kind == "kalshi_order_cancel")
    ).all()
    assert len(cancel_entries) == 1
    assert cancel_entries[0].status == STATUS_DONE


def test_paper_position_yes_round_trip_pnl(db_session):
    """Bug #15: YES position entry/exit with same-side prices yields
    correct PnL. Entry at 0.40, exit at 0.60 with 2 contracts → +0.40."""
    market = Market(ticker="NBA-YES-RT", title="YES round-trip", status="open")
    db_session.add(market)
    db_session.commit()

    position = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="NBA-YES-RT", side="yes", quantity=2, entry_price=0.40),
    )
    close_paper_position(db_session, position.id, PaperPositionExit(exit_price=0.60))
    db_session.commit()

    stored = db_session.get(PaperPosition, position.id)
    assert stored.side == "yes"
    assert stored.status == "closed"
    # (0.60 - 0.40) * 2 = 0.40
    assert stored.pnl == 0.40


def test_paper_position_no_round_trip_pnl(db_session):
    """Bug #15: NO position entry/exit with same-side prices yields
    correct PnL. The PnL formula ``(exit - entry) * qty`` works
    *only* when both prices are quoted on the same side as the
    position — same-side NO entry at 0.40, NO exit at 0.60 with 2
    contracts → +0.40."""
    market = Market(ticker="NBA-NO-RT", title="NO round-trip", status="open")
    db_session.add(market)
    db_session.commit()

    position = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="NBA-NO-RT", side="no", quantity=2, entry_price=0.40),
    )
    close_paper_position(db_session, position.id, PaperPositionExit(exit_price=0.60))
    db_session.commit()

    stored = db_session.get(PaperPosition, position.id)
    assert stored.side == "no"
    assert stored.status == "closed"
    assert stored.pnl == 0.40


def test_paper_position_create_rejects_invalid_side():
    """Bug #15: ``side`` is locked to ``Literal['yes', 'no']`` — any
    other value (typo, future enum) is rejected at the schema layer
    rather than silently lowercased and passed through."""
    with pytest.raises(ValidationError):
        PaperPositionCreate(ticker="NBA-TEST", side="invalid", quantity=1, entry_price=0.5)


def test_paper_position_create_accepts_case_insensitive_side():
    """Bug #15, codex round-2 P2: preserve the lenience of the prior
    ``.lower()`` normalization in services/orders.py — uppercase /
    mixed-case ``side`` is accepted at the boundary via a
    ``BeforeValidator`` and normalized to lowercase before the
    ``Literal`` validation runs."""
    for value in ("YES", "Yes", "yes"):
        payload = PaperPositionCreate(ticker="NBA-TEST", side=value, quantity=1, entry_price=0.5)
        assert payload.side == "yes"
    for value in ("NO", "No", "no"):
        payload = PaperPositionCreate(ticker="NBA-TEST", side=value, quantity=1, entry_price=0.5)
        assert payload.side == "no"


def test_demo_order_create_rejects_invalid_action_and_tif():
    """Bug #15: ``action`` is ``Literal['buy', 'sell']`` and
    ``time_in_force`` is the enum of Kalshi-supported values. Bad
    inputs are rejected at the schema layer."""
    with pytest.raises(ValidationError):
        DemoOrderCreate(
            ticker="NBA-TEST", side="yes", action="hold", quantity=1, limit_price=0.5, approved=True
        )
    with pytest.raises(ValidationError):
        DemoOrderCreate(
            ticker="NBA-TEST",
            side="yes",
            quantity=1,
            limit_price=0.5,
            approved=True,
            time_in_force="never",
        )
