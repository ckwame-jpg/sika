from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.clients.kalshi import KalshiDemoClient
from app.models import Market
from app.schemas import DemoOrderCreate
from app.services.orders import create_demo_order


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
