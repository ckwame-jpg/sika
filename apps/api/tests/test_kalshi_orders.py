"""Real Kalshi order pipeline (singles) — gates, atomicity, drain,
cancel, reconcile.

Money-safety proofs live here:
- approved / cap / creds gates reject before anything persists
- environment + base_url are stamped at create from the user's row
- the drain handler submits with the PERSISTED client_order_id
- Kalshi 4xx is terminal (submission_failed + error_detail), 5xx retries
- cancel is owner-only; no hard-delete exists for real orders
- reconcile groups by the ORDER's stored host, not current settings
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import KalshiOrder, KalshiOrderFill, Market, OutboxEntry, User
from app.schemas import KalshiOrderCreate
from app.services import kalshi_orders as ko
from app.services.operator_settings import set_kalshi_max_order_cost
from app.services.outbox import STATUS_DONE, drain_once
from app.services.user_kalshi import upsert_user_credentials
from app.services.users import seed_users_from_settings

SAMPLE_PEM = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _seed(db: Session, *, base_url: str = PROD_URL) -> User:
    seed_users_from_settings(db, Settings(SIKA_USERS="chris", SIKA_KALSHI_OWNER="chris"))
    db.commit()
    user = db.query(User).filter_by(username="chris").one()
    upsert_user_credentials(
        db, user_id=user.id, key_id="k1", private_key_pem=SAMPLE_PEM, base_url=base_url
    )
    market = Market(ticker="KXLIVE-TEST", title="Live test market", status="open")
    db.add(market)
    db.commit()
    return user


def _payload(**overrides) -> KalshiOrderCreate:
    base = dict(
        ticker="KXLIVE-TEST", side="yes", quantity=2, limit_price=0.4, approved=True
    )
    base.update(overrides)
    return KalshiOrderCreate(**base)


class FakeTradeClient:
    """Scriptable stand-in for KalshiTradeClient in handler tests."""

    def __init__(self):
        self.create_calls: list[dict] = []
        self.cancel_calls: list[str] = []

    def create_order(self, **kwargs):
        self.create_calls.append(kwargs)
        return {
            "request": {"ticker": kwargs["ticker"]},
            "order": {
                "order_id": "live_ord_1",
                "client_order_id": kwargs.get("client_order_id"),
                "status": "resting",
            },
        }

    def cancel_order(self, kalshi_order_id):
        self.cancel_calls.append(kalshi_order_id)
        return {"order": {"order_id": kalshi_order_id, "status": "cancelled"}}


# ── create gates ─────────────────────────────────────────────────────


def test_create_requires_approval(db_session):
    user = _seed(db_session)
    with pytest.raises(HTTPException) as err:
        ko.create_kalshi_order(db_session, _payload(approved=False), user_id=user.id)
    assert err.value.status_code == 400
    assert "approval" in err.value.detail.lower()


def test_create_requires_connected_credentials(db_session):
    seed_users_from_settings(db_session, Settings(SIKA_USERS="chris"))
    db_session.commit()
    user = db_session.query(User).filter_by(username="chris").one()
    db_session.add(Market(ticker="KXLIVE-TEST", title="m", status="open"))
    db_session.commit()
    with pytest.raises(HTTPException) as err:
        ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    assert err.value.status_code == 409
    assert "not connected" in err.value.detail.lower()


def test_create_enforces_cost_cap(db_session):
    user = _seed(db_session)
    # default cap $25 — 100 × $0.40 = $40 exceeds it
    with pytest.raises(HTTPException) as err:
        ko.create_kalshi_order(db_session, _payload(quantity=100), user_id=user.id)
    assert err.value.status_code == 400
    assert "cap" in err.value.detail.lower()

    # operator raises the cap → same order passes
    set_kalshi_max_order_cost(db_session, 100.0)
    db_session.commit()
    order = ko.create_kalshi_order(db_session, _payload(quantity=100), user_id=user.id)
    assert order.quantity == 100


def test_create_quantizes_subcent_limit_price(db_session):
    """American-odds input produces sub-cent prices (+245 → 0.2899…);
    the row, cap check, and outbox payload all use the cent-snapped
    value so the exchange never sees an invalid_price."""
    user = _seed(db_session)
    order = ko.create_kalshi_order(
        db_session, _payload(limit_price=0.2899), user_id=user.id
    )
    db_session.commit()
    assert order.limit_price == 0.29

    entry = db_session.scalars(
        select(OutboxEntry).where(OutboxEntry.target_id == order.id)
    ).one()
    assert entry.payload["limit_price"] == 0.29


def test_create_unknown_ticker_404(db_session):
    user = _seed(db_session)
    with pytest.raises(HTTPException) as err:
        ko.create_kalshi_order(db_session, _payload(ticker="KXNOPE"), user_id=user.id)
    assert err.value.status_code == 404


# ── create semantics ────────────────────────────────────────────────


def test_create_stamps_environment_and_writes_outbox_atomically(db_session):
    user = _seed(db_session, base_url=PROD_URL)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()

    assert order.kind == "single"
    assert order.environment == "live"
    assert order.base_url == PROD_URL.rstrip("/")
    assert order.client_order_id
    assert order.status == "submitting"
    assert order.approved_by_user is True

    entries = db_session.scalars(
        select(OutboxEntry).where(OutboxEntry.target_id == order.id)
    ).all()
    assert len(entries) == 1
    assert entries[0].intent_kind == "kalshi_live_order_submit"
    assert entries[0].payload["client_order_id"] == order.client_order_id


def test_create_classifies_demo_environment(db_session):
    demo_url = get_settings().kalshi_demo_base_url
    user = _seed(db_session, base_url=demo_url)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    assert order.environment == "demo"
    assert order.base_url == demo_url.rstrip("/")


# ── drain round-trips ───────────────────────────────────────────────


def test_drain_submits_with_persisted_client_order_id(db_session, monkeypatch):
    user = _seed(db_session)
    fake = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)

    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["succeeded"] == 1

    db_session.refresh(order)
    assert order.kalshi_order_id == "live_ord_1"
    assert order.status == "resting"
    assert fake.create_calls[0]["client_order_id"] == order.client_order_id


def test_drain_marks_kalshi_4xx_terminal(db_session, monkeypatch):
    """A rejected order (insufficient funds, closed market) must not
    retry forever — it lands as submission_failed with the body
    surfaced, and the outbox entry completes."""
    user = _seed(db_session)

    class RejectingClient(FakeTradeClient):
        def create_order(self, **kwargs):
            request = httpx.Request("POST", "https://x/portfolio/orders")
            response = httpx.Response(400, text="insufficient balance", request=request)
            raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(ko, "client_for_order", lambda db, order: RejectingClient())
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["succeeded"] == 1  # handler completed (terminal), no retry

    db_session.refresh(order)
    assert order.status == "submission_failed"
    assert "insufficient balance" in (order.error_detail or "")

    entries = db_session.scalars(
        select(OutboxEntry).where(OutboxEntry.target_id == order.id)
    ).all()
    assert entries[0].status == STATUS_DONE


def test_drain_retries_on_5xx(db_session, monkeypatch):
    user = _seed(db_session)

    class FlakyClient(FakeTradeClient):
        def create_order(self, **kwargs):
            request = httpx.Request("POST", "https://x/portfolio/orders")
            response = httpx.Response(502, text="bad gateway", request=request)
            raise httpx.HTTPStatusError("502", request=request, response=response)

    monkeypatch.setattr(ko, "client_for_order", lambda db, order: FlakyClient())
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["failed"] == 1  # left for retry, not dead yet

    db_session.refresh(order)
    assert order.status == "submitting"  # unchanged — retry pending


# ── cancel ──────────────────────────────────────────────────────────


def test_cancel_roundtrip_and_ownership(db_session, monkeypatch):
    user = _seed(db_session)
    fake = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)

    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)

    # wrong user → 403
    with pytest.raises(HTTPException) as err:
        ko.cancel_kalshi_order(db_session, order.id, user_id=user.id + 999)
    assert err.value.status_code == 403

    ko.cancel_kalshi_order(db_session, order.id, user_id=user.id)
    db_session.commit()
    db_session.refresh(order)
    assert order.status == "cancelling"

    drain_once(db_session)
    db_session.refresh(order)
    assert order.status == "cancelled"
    assert fake.cancel_calls == ["live_ord_1"]


def test_cancel_before_submission_conflicts(db_session):
    user = _seed(db_session)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    # no kalshi_order_id yet (outbox not drained)
    with pytest.raises(HTTPException) as err:
        ko.cancel_kalshi_order(db_session, order.id, user_id=user.id)
    assert err.value.status_code == 409


def test_drain_ioc_no_fill_sets_friendly_detail(db_session, monkeypatch):
    """Fill-now (IOC) that found an empty book lands as cancelled with
    a plain-language explanation — never a silent mystery row."""
    user = _seed(db_session)

    class NoLiquidityClient(FakeTradeClient):
        def create_order(self, **kwargs):
            return {
                "request": {"ticker": kwargs["ticker"]},
                "order": {
                    "order_id": "ioc_1",
                    "client_order_id": kwargs.get("client_order_id"),
                    "status": "cancelled",
                },
            }

    monkeypatch.setattr(ko, "client_for_order", lambda db, order: NoLiquidityClient())
    order = ko.create_kalshi_order(
        db_session, _payload(time_in_force="immediate_or_cancel"), user_id=user.id
    )
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)

    assert order.status == "cancelled"
    assert "nothing was charged" in order.error_detail


# ── dismiss ─────────────────────────────────────────────────────────


def test_dismiss_failed_row_deletes_it(db_session, monkeypatch):
    user = _seed(db_session)

    class RejectingClient(FakeTradeClient):
        def create_order(self, **kwargs):
            request = httpx.Request("POST", "https://x/portfolio/events/orders")
            response = httpx.Response(400, text="invalid price", request=request)
            raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(ko, "client_for_order", lambda db, order: RejectingClient())
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)
    assert order.status == "submission_failed"

    ko.delete_kalshi_order(db_session, order.id, user_id=user.id)
    db_session.commit()
    assert db_session.get(KalshiOrder, order.id) is None


def test_dismiss_blocks_resting_and_wrong_owner(db_session, monkeypatch):
    user = _seed(db_session)
    fake = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)
    assert order.status == "resting"

    with pytest.raises(HTTPException) as err:
        ko.delete_kalshi_order(db_session, order.id, user_id=user.id)
    assert err.value.status_code == 400
    assert "Cancel resting orders first" in err.value.detail

    order.status = "submission_failed"
    db_session.commit()
    with pytest.raises(HTTPException) as err:
        ko.delete_kalshi_order(db_session, order.id, user_id=user.id + 999)
    assert err.value.status_code == 403


# ── reconcile ───────────────────────────────────────────────────────


def test_reconcile_syncs_status_and_fills_with_fees(db_session, monkeypatch):
    user = _seed(db_session)
    fake = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)

    class SyncClient:
        def list_orders(self):
            return [
                {
                    "order_id": "live_ord_1",
                    "client_order_id": order.client_order_id,
                    "status": "executed",
                }
            ]

        def list_fills(self):
            return [
                {
                    "fill_id": "fill_1",
                    "order_id": "live_ord_1",
                    "count": 2,
                    "yes_price_dollars": "0.40",
                    "side": "yes",
                    "fee_cost": "0.03",
                }
            ]

    ko.reconcile_kalshi_live_state(
        db_session, client_factory=lambda db, o: SyncClient()
    )
    db_session.commit()
    db_session.refresh(order)

    assert order.status == "executed"
    fills = db_session.scalars(select(KalshiOrderFill)).all()
    assert len(fills) == 1
    assert fills[0].fee_dollars == 0.03
    assert fills[0].price == 0.40

    # idempotent: same remote fills don't duplicate
    ko.reconcile_kalshi_live_state(
        db_session, client_factory=lambda db, o: SyncClient()
    )
    db_session.commit()
    assert len(db_session.scalars(select(KalshiOrderFill)).all()) == 1


def test_reconcile_skips_groups_whose_client_fails(db_session, monkeypatch):
    user = _seed(db_session)
    fake = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)

    def exploding_factory(db, o):
        raise RuntimeError("no creds")

    # must not raise — best-effort per group
    ko.reconcile_kalshi_live_state(db_session, client_factory=exploding_factory)
    db_session.refresh(order)
    assert order.status == "resting"  # untouched


def test_client_for_order_uses_order_host_not_current_settings(db_session):
    """Flipping the settings environment must not re-route an existing
    order's traffic: the client is built from the ORDER's stored
    base_url."""
    user = _seed(db_session, base_url=PROD_URL)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()

    # user flips settings to demo AFTER placing
    demo_url = get_settings().kalshi_demo_base_url
    upsert_user_credentials(
        db_session, user_id=user.id, key_id="k1", private_key_pem=SAMPLE_PEM, base_url=demo_url
    )
    db_session.commit()

    client = ko.client_for_order(db_session, order)
    assert client.base_url == PROD_URL.rstrip("/")
