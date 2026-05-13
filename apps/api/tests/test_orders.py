from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from pydantic import ValidationError

from app.clients.kalshi import KalshiDemoClient
from app.models import Market, PaperPosition
from app.schemas import DemoOrderCreate, PaperPositionCreate, PaperPositionExit
from app.services.orders import close_paper_position, create_demo_order, create_paper_position


class FakeDemoClient:
    def create_order(self, *, ticker, side, action, quantity, limit_price, time_in_force):
        return {
            "request": {"ticker": ticker},
            "order": {
                "order_id": "ord_123",
                "client_order_id": "client_123",
                "status": "resting",
            },
        }


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
            client=FakeDemoClient(),
        )

    assert exc.value.status_code == 400


def test_demo_order_submission_persists_remote_status(db_session):
    market = Market(ticker="NBA-TEST", title="Test market", status="open")
    db_session.add(market)
    db_session.commit()

    order = create_demo_order(
        db_session,
        DemoOrderCreate(ticker="NBA-TEST", side="yes", quantity=2, limit_price=0.55, approved=True),
        client=FakeDemoClient(),
    )

    assert order.kalshi_order_id == "ord_123"
    assert order.status == "resting"


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
