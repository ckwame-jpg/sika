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

from datetime import datetime, timedelta, timezone

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


@pytest.fixture(autouse=True)
def _reset_reconcile_process_state():
    ko._clear_order_page_resume_cursors()
    ko._clear_fill_page_resume_cursors()
    ko._clear_terminal_candidate_rotation()
    yield
    ko._clear_order_page_resume_cursors()
    ko._clear_fill_page_resume_cursors()
    ko._clear_terminal_candidate_rotation()


def _seed(db: Session, *, base_url: str = PROD_URL) -> User:
    seed_users_from_settings(
        db, Settings(SIKA_USERS="chris", SIKA_KALSHI_OWNER="chris")
    )
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


def test_create_cost_cap_includes_worst_case_taker_fee(db_session):
    user = _seed(db_session)

    # Principal lands exactly on the default $25 cap, but the fee makes
    # the order's maximum cost $25.88.
    with pytest.raises(HTTPException) as err:
        ko.create_kalshi_order(
            db_session,
            _payload(quantity=50, limit_price=0.50),
            user_id=user.id,
        )
    assert err.value.status_code == 400
    assert "Order total $25.88" in err.value.detail
    assert "principal $25.00" in err.value.detail
    assert "worst-case taker fee $0.88" in err.value.detail
    assert "$25.00 per-order cap" in err.value.detail

    # The comparison is strict: a cap equal to principal + fee passes.
    set_kalshi_max_order_cost(db_session, 25.88)
    db_session.commit()
    order = ko.create_kalshi_order(
        db_session,
        _payload(quantity=50, limit_price=0.50),
        user_id=user.id,
    )
    assert order.quantity == 50


def test_create_cost_cap_exact_cent_boundary_passes(db_session):
    user = _seed(db_session)
    set_kalshi_max_order_cost(db_session, 0.42)
    db_session.commit()

    order = ko.create_kalshi_order(
        db_session,
        _payload(quantity=1, limit_price=0.40),
        user_id=user.id,
    )

    assert order.quantity == 1
    assert order.limit_price == 0.40


def test_create_cost_cap_never_rounds_subcent_cap_up(db_session):
    user = _seed(db_session)
    set_kalshi_max_order_cost(db_session, 0.426)
    db_session.commit()

    # One 41-cent contract plus its 2-cent fee costs 43 cents. The cap must
    # stay at the exact configured $0.426, not round upward to 43 cents.
    with pytest.raises(HTTPException) as err:
        ko.create_kalshi_order(
            db_session,
            _payload(quantity=1, limit_price=0.41),
            user_id=user.id,
        )

    assert err.value.status_code == 400
    assert "Order total $0.43" in err.value.detail
    assert "$0.426 per-order cap" in err.value.detail


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


def test_cancel_preserves_known_count_but_unknown_final_count_stays_incomplete(
    db_session,
    monkeypatch,
):
    user = _seed(db_session)

    class SparseCancelClient(FakeTradeClient):
        def create_order(self, **kwargs):
            return {
                "request": {"ticker": kwargs["ticker"]},
                "order": {
                    "order_id": "sparse-cancel",
                    "client_order_id": kwargs.get("client_order_id"),
                    "status": "resting",
                    "fill_count": 1.0,
                },
            }

        def list_orders(self, **kwargs):
            # The terminal order-list response is also sparse: it cannot turn
            # an empty fill page into proof that the final quantity was zero.
            return (
                [
                    {
                        "order_id": "sparse-cancel",
                        "client_order_id": order.client_order_id,
                        "status": "cancelled",
                    }
                ],
                None,
            )

        def list_fills(self, **kwargs):
            return [], None

    client = SparseCancelClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, local: client)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)
    assert ko._remote_fill_count(order.response_body) == 1.0
    assert ko._authoritative_fill_count(order) is None

    ko.cancel_kalshi_order(db_session, order.id, user_id=user.id)
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)
    assert order.response_body["_preserved_fill_count"] == 1.0
    assert ko._authoritative_fill_count(order) is None

    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: client,
    )
    db_session.commit()
    db_session.refresh(order)

    assert order.status == "cancelled"
    assert ko._remote_fill_count(order.response_body) == 1.0
    assert ko._authoritative_fill_count(order) is None
    assert order.fills_synced_at is None
    with pytest.raises(HTTPException) as err:
        ko.delete_kalshi_order(db_session, order.id, user_id=user.id)
    assert "zero-fill cancellation" in err.value.detail


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
                    "fill_count": 0.0,
                    "remaining_count": 0.0,
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
    assert order.fills_synced_at is not None


def test_sparse_terminal_submit_cannot_stamp_or_be_dismissed(
    db_session,
    monkeypatch,
):
    user = _seed(db_session)

    class SparseTerminalClient(FakeTradeClient):
        def create_order(self, **kwargs):
            return {
                "request": {"ticker": kwargs["ticker"]},
                "order": {
                    "order_id": "sparse-terminal",
                    "client_order_id": kwargs.get("client_order_id"),
                    "status": "cancelled",
                },
                "raw": {"order_id": "sparse-terminal"},
            }

    monkeypatch.setattr(
        ko,
        "client_for_order",
        lambda db, local: SparseTerminalClient(),
    )
    order = ko.create_kalshi_order(
        db_session,
        _payload(time_in_force="immediate_or_cancel"),
        user_id=user.id,
    )
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)

    assert order.status == "cancelled"
    assert ko._remote_fill_count(order.response_body) is None
    assert ko._authoritative_fill_count(order) is None
    assert order.fills_synced_at is None
    with pytest.raises(HTTPException):
        ko.delete_kalshi_order(db_session, order.id, user_id=user.id)


def test_executed_zero_count_contradiction_never_stamps(db_session, monkeypatch):
    user = _seed(db_session)

    class ContradictoryExecutedClient(FakeTradeClient):
        def create_order(self, **kwargs):
            return {
                "request": {"ticker": kwargs["ticker"]},
                "order": {
                    "order_id": "contradictory-executed",
                    "client_order_id": kwargs.get("client_order_id"),
                    "status": "executed",
                    "fill_count": 0.0,
                    "remaining_count": 0.0,
                },
            }

        def list_orders(self, **kwargs):
            return (
                [
                    {
                        "order_id": "contradictory-executed",
                        "client_order_id": order.client_order_id,
                        "status": "executed",
                        "fill_count_fp": "0.00",
                    }
                ],
                None,
            )

        def list_fills(self, **kwargs):
            return [], None

    client = ContradictoryExecutedClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, local: client)
    order = ko.create_kalshi_order(
        db_session,
        _payload(quantity=2, time_in_force="immediate_or_cancel"),
        user_id=user.id,
    )
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)

    assert order.status == "executed"
    assert ko._authoritative_fill_count(order) == 0.0
    assert order.fills_synced_at is None

    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: client,
    )
    db_session.commit()
    db_session.refresh(order)
    assert order.fills_synced_at is None


def test_drain_immediate_execution_imports_fills_fees_and_stamps(
    db_session,
    monkeypatch,
):
    user = _seed(db_session)

    class ImmediateFillClient(FakeTradeClient):
        def create_order(self, **kwargs):
            return {
                "request": {"ticker": kwargs["ticker"]},
                "order": {
                    "order_id": "ioc-full",
                    "client_order_id": kwargs.get("client_order_id"),
                    "status": "executed",
                    "fill_count": 10.0,
                    "remaining_count": 0.0,
                },
                "raw": {"fill_count": "10.00", "remaining_count": "0.00"},
            }

        def list_fills(self, *, order_id=None, limit=1000, cursor=None):
            assert order_id == "ioc-full"
            return (
                [
                    {
                        "fill_id": "fill-immediate",
                        "order_id": "ioc-full",
                        "count_fp": "10.00",
                        "yes_price_dollars": "0.39",
                        "side": "yes",
                        "fee_cost": "0.17",
                    }
                ],
                None,
            )

    fake = ImmediateFillClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)
    order = ko.create_kalshi_order(
        db_session,
        _payload(quantity=10, time_in_force="immediate_or_cancel"),
        user_id=user.id,
    )
    db_session.commit()

    drain_once(db_session)
    db_session.refresh(order)

    fills = db_session.scalars(
        select(KalshiOrderFill).where(KalshiOrderFill.kalshi_order_id == order.id)
    ).all()
    assert order.status == "executed"
    assert order.fills_synced_at is not None
    assert len(fills) == 1
    assert fills[0].count == 10.0
    assert fills[0].price == 0.39
    assert fills[0].fee_dollars == 0.17


def test_drain_partial_ioc_imports_fill_and_cancels_remainder(
    db_session,
    monkeypatch,
):
    user = _seed(db_session)

    class PartialFillClient(FakeTradeClient):
        def create_order(self, **kwargs):
            return {
                "request": {"ticker": kwargs["ticker"]},
                "order": {
                    "order_id": "ioc-partial",
                    "client_order_id": kwargs.get("client_order_id"),
                    "status": "cancelled",
                    "fill_count": 5.0,
                    "remaining_count": 0.0,
                },
                "raw": {"fill_count": "5.00", "remaining_count": "0.00"},
            }

        def list_fills(self, *, order_id=None, limit=1000, cursor=None):
            assert order_id == "ioc-partial"
            return (
                [
                    {
                        "fill_id": "fill-partial",
                        "order_id": "ioc-partial",
                        "count_fp": "5.00",
                        "yes_price_dollars": "0.40",
                        "side": "yes",
                        "fee_cost": "0.09",
                    }
                ],
                None,
            )

    fake = PartialFillClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)
    order = ko.create_kalshi_order(
        db_session,
        _payload(quantity=10, time_in_force="immediate_or_cancel"),
        user_id=user.id,
    )
    db_session.commit()

    drain_once(db_session)
    db_session.refresh(order)

    fill = db_session.scalars(
        select(KalshiOrderFill).where(KalshiOrderFill.kalshi_order_id == order.id)
    ).one()
    assert order.status == "cancelled"
    assert order.fills_synced_at is not None
    assert order.error_detail is None  # never claim a partial fill charged nothing
    assert fill.count == 5.0
    assert fill.fee_dollars == 0.09


def test_partial_resting_fill_stays_unstamped_until_terminal_sync(
    db_session,
    monkeypatch,
):
    user = _seed(db_session)

    class PartialThenCancelledClient(FakeTradeClient):
        terminal = False

        def create_order(self, **kwargs):
            return {
                "request": {"ticker": kwargs["ticker"]},
                "order": {
                    "order_id": "gtc-partial",
                    "client_order_id": kwargs.get("client_order_id"),
                    "status": "resting",
                    "fill_count": 1.0,
                    "remaining_count": 1.0,
                },
                "raw": {"fill_count": "1.00", "remaining_count": "1.00"},
            }

        def list_orders(self, *, limit=1000, cursor=None):
            return (
                [
                    {
                        "order_id": "gtc-partial",
                        "client_order_id": order.client_order_id,
                        "status": "cancelled",
                        "fill_count_fp": "2.00",
                    }
                ],
                None,
            )

        def list_fills(self, *, order_id=None, limit=1000, cursor=None):
            fills = [
                {
                    "fill_id": "gtc-fill-1",
                    "order_id": "gtc-partial",
                    "count_fp": "1.00",
                    "yes_price_dollars": "0.40",
                    "fee_cost": "0.01",
                }
            ]
            if self.terminal:
                fills.append(
                    {
                        "fill_id": "gtc-fill-2",
                        "order_id": "gtc-partial",
                        "count_fp": "1.00",
                        "yes_price_dollars": "0.39",
                        "fee_cost": "0.01",
                    }
                )
            return fills, None

    client = PartialThenCancelledClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, local: client)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()

    drain_once(db_session)
    db_session.refresh(order)
    assert order.status == "resting"
    assert order.fills_synced_at is None
    assert db_session.scalar(select(KalshiOrderFill.count)) == 1.0

    ko.cancel_kalshi_order(db_session, order.id, user_id=user.id)
    db_session.commit()
    drain_once(db_session)
    db_session.refresh(order)
    assert order.status == "cancelled"
    assert order.fills_synced_at is None

    client.terminal = True
    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: client,
    )
    db_session.commit()
    db_session.refresh(order)
    assert order.fills_synced_at is not None
    assert sum(db_session.scalars(select(KalshiOrderFill.count)).all()) == 2.0


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


def test_dismiss_cancelled_order_requires_synced_zero_fill_ledger(db_session):
    user = _seed(db_session)
    market = db_session.scalar(select(Market).where(Market.ticker == "KXLIVE-TEST"))
    assert market is not None
    now = datetime.now(timezone.utc)

    def cancelled_order(*, suffix: str, response_body: dict) -> KalshiOrder:
        row = KalshiOrder(
            user_id=user.id,
            market_id=market.id,
            kind="single",
            ticker=market.ticker,
            environment="live",
            base_url=PROD_URL,
            client_order_id=f"cancelled-client-{suffix}",
            kalshi_order_id=f"cancelled-order-{suffix}",
            side="yes",
            action="buy",
            quantity=2,
            limit_price=0.40,
            approved_by_user=True,
            status="cancelled",
            response_body=response_body,
            fills_synced_at=now,
        )
        db_session.add(row)
        db_session.flush()
        return row

    with_fill = cancelled_order(
        suffix="persisted",
        response_body={"status": "cancelled", "fill_count_fp": "0.00"},
    )
    persisted_fill = KalshiOrderFill(
        kalshi_order_id=with_fill.id,
        kalshi_fill_id="cancelled-fill",
        count=1.0,
        price=0.40,
        side="yes",
        fee_dollars=0.01,
    )
    db_session.add(persisted_fill)
    expected_fill = cancelled_order(
        suffix="expected",
        response_body={"status": "cancelled", "fill_count_fp": "1.00"},
    )
    unknown_fill_count = cancelled_order(suffix="unknown", response_body={})
    zero_fill = cancelled_order(
        suffix="zero",
        response_body={"status": "cancelled", "fill_count_fp": "0.00"},
    )
    unsynced_zero_fill = cancelled_order(
        suffix="unsynced-zero",
        response_body={"status": "cancelled", "fill_count_fp": "0.00"},
    )
    unsynced_zero_fill.fills_synced_at = None
    db_session.commit()

    for blocked in (
        with_fill,
        expected_fill,
        unknown_fill_count,
        unsynced_zero_fill,
    ):
        with pytest.raises(HTTPException) as err:
            ko.delete_kalshi_order(db_session, blocked.id, user_id=user.id)
        assert "zero-fill cancellation" in err.value.detail

    assert db_session.get(KalshiOrder, with_fill.id) is not None
    assert db_session.get(KalshiOrderFill, persisted_fill.id) is not None

    ko.delete_kalshi_order(db_session, zero_fill.id, user_id=user.id)
    db_session.commit()
    assert db_session.get(KalshiOrder, zero_fill.id) is None


def test_remote_fill_count_uses_max_across_direct_nested_and_raw_payloads():
    payload = {
        "fill_count": "0.00",
        "order": {"status": "executed", "fill_count_fp": "1.00"},
        "raw": {"fill_count": "2.00"},
    }

    assert ko._remote_fill_count(payload) == 2.0
    assert ko._authoritative_fill_count_from_payload(payload) == 2.0


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
                    "fill_count_fp": "2.00",
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
    assert order.fills_synced_at is not None

    # Force one more eligibility pass: the same remote fills remain
    # idempotent even when completion has to be re-proved.
    order.fills_synced_at = None
    db_session.commit()
    ko.reconcile_kalshi_live_state(
        db_session, client_factory=lambda db, o: SyncClient()
    )
    db_session.commit()
    assert len(db_session.scalars(select(KalshiOrderFill)).all()) == 1


def test_reconcile_imports_fill_from_cursor_page_two(db_session, monkeypatch):
    user = _seed(db_session)
    fake = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)

    class PagedSyncClient:
        def iter_order_pages(self, **kwargs):
            yield (
                [
                    {
                        "order_id": "live_ord_1",
                        "client_order_id": order.client_order_id,
                        "status": "executed",
                        "fill_count_fp": "2.00",
                    }
                ],
                None,
            )

        def iter_fill_pages(self, **kwargs):
            yield (
                [
                    {
                        "fill_id": "unrelated-fill",
                        "order_id": "another-order",
                        "count_fp": "1.00",
                        "yes_price_dollars": "0.50",
                    }
                ],
                "page-2",
            )
            yield (
                [
                    {
                        "fill_id": "page-two-fill",
                        "order_id": "live_ord_1",
                        "count_fp": "2.00",
                        "yes_price_dollars": "0.40",
                        "fee_cost": "0.03",
                    }
                ],
                None,
            )

    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: PagedSyncClient(),
    )
    db_session.commit()
    db_session.refresh(order)

    fill = db_session.scalars(
        select(KalshiOrderFill).where(KalshiOrderFill.kalshi_order_id == order.id)
    ).one()
    assert fill.kalshi_fill_id == "page-two-fill"
    assert order.fills_synced_at is not None


def test_reconcile_self_heals_terminal_order_from_historical_tier(db_session):
    user = _seed(db_session)
    market = db_session.scalar(
        select(Market).where(Market.ticker == "KXLIVE-TEST")
    )
    assert market is not None
    order = KalshiOrder(
        user_id=user.id,
        market_id=market.id,
        kind="single",
        ticker=market.ticker,
        environment="live",
        base_url=PROD_URL,
        client_order_id="archived-client-order",
        kalshi_order_id="archived-exchange-order",
        side="yes",
        action="buy",
        quantity=5,
        limit_price=0.40,
        approved_by_user=True,
        status="cancelled",
        # Legacy sparse V2 normalization wrote an authoritative-looking zero;
        # the historical order record must be allowed to correct it.
        response_body={
            "order": {
                "status": "cancelled",
                "fill_count": 0.0,
                "remaining_count": 0.0,
            },
            "raw": {"order_id": "archived-exchange-order"},
        },
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(order)
    db_session.commit()

    class HistoricalSyncClient:
        historical_order_tickers: list[str] = []
        historical_fill_tickers: list[str] = []
        historical_fill_cursors: list[str | None] = []

        def iter_order_pages(self, **kwargs):
            yield ([], None)

        def iter_fill_pages(self, **kwargs):
            yield ([], None)

        def iter_historical_order_pages(self, *, ticker, **kwargs):
            self.historical_order_tickers.append(ticker)
            yield (
                [
                    {
                        "order_id": "archived-exchange-order",
                        "client_order_id": "archived-client-order",
                        "status": "cancelled",
                        "fill_count_fp": "2.00",
                    }
                ],
                None,
            )

        def iter_historical_fill_pages(self, *, ticker, cursor=None, **kwargs):
            self.historical_fill_tickers.append(ticker)
            self.historical_fill_cursors.append(cursor)
            if cursor is None:
                yield (
                    [
                        {
                            "fill_id": "other-archived-fill",
                            "order_id": "another-order",
                            "count_fp": "9.00",
                            "yes_price_dollars": "0.50",
                            "fee_cost": "0.09",
                        },
                        {
                            "fill_id": "archived-fill-1",
                            "order_id": "archived-exchange-order",
                            "count_fp": "1.00",
                            "yes_price_dollars": "0.40",
                            "fee_cost": "0.01",
                        },
                    ],
                    "historical-page-two",
                )
                return
            assert cursor == "historical-page-two"
            yield (
                [
                    {
                        "fill_id": "archived-fill-2",
                        "order_id": "archived-exchange-order",
                        "count_fp": "1.00",
                        "yes_price_dollars": "0.40",
                        "fee_cost": "0.02",
                    }
                ],
                None,
            )

    client = HistoricalSyncClient()
    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: client,
    )
    db_session.commit()
    db_session.refresh(order)
    assert order.fills_synced_at is None

    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: client,
    )
    db_session.commit()
    db_session.refresh(order)

    fills = db_session.scalars(
        select(KalshiOrderFill).where(KalshiOrderFill.kalshi_order_id == order.id)
    ).all()
    assert client.historical_order_tickers == ["KXLIVE-TEST", "KXLIVE-TEST"]
    assert client.historical_fill_tickers == ["KXLIVE-TEST", "KXLIVE-TEST"]
    assert client.historical_fill_cursors == [None, "historical-page-two"]
    assert {fill.kalshi_fill_id for fill in fills} == {
        "archived-fill-1",
        "archived-fill-2",
    }
    assert sum(fill.fee_dollars or 0.0 for fill in fills) == 0.03
    assert order.fills_synced_at is not None


def test_legacy_nested_zero_retries_when_historical_order_lookup_fails(db_session):
    user = _seed(db_session)
    market = db_session.scalar(
        select(Market).where(Market.ticker == "KXLIVE-TEST")
    )
    assert market is not None
    order = KalshiOrder(
        user_id=user.id,
        market_id=market.id,
        kind="single",
        ticker=market.ticker,
        environment="live",
        base_url=PROD_URL,
        client_order_id="legacy-zero-client-order",
        kalshi_order_id="legacy-zero-exchange-order",
        side="yes",
        action="buy",
        quantity=5,
        limit_price=0.40,
        approved_by_user=True,
        status="cancelled",
        response_body={
            "order": {
                "status": "cancelled",
                "fill_count": 0.0,
                "remaining_count": 0.0,
            },
            "raw": {"order_id": "legacy-zero-exchange-order"},
        },
    )
    db_session.add(order)
    db_session.commit()

    class UnavailableHistoricalOrderClient:
        historical_order_calls = 0
        historical_fill_calls = 0

        def iter_order_pages(self, **kwargs):
            yield ([], None)

        def iter_fill_pages(self, **kwargs):
            yield ([], None)

        def iter_historical_order_pages(self, **kwargs):
            self.historical_order_calls += 1
            raise RuntimeError("scripted historical-order outage")
            yield ([], None)

        def iter_historical_fill_pages(self, **kwargs):
            self.historical_fill_calls += 1
            yield ([], None)

    client = UnavailableHistoricalOrderClient()
    assert ko._authoritative_fill_count(order) is None

    for expected_calls in (1, 2):
        ko.reconcile_kalshi_live_state(
            db_session,
            client_factory=lambda db, local: client,
        )
        db_session.commit()
        db_session.refresh(order)
        assert order.fills_synced_at is None
        assert client.historical_order_calls == expected_calls
        assert client.historical_fill_calls == expected_calls


def test_historical_ticker_scan_never_attributes_fill_without_order_id(db_session):
    user = _seed(db_session)
    order = ko.create_kalshi_order(db_session, _payload(quantity=1), user_id=user.id)
    order.kalshi_order_id = "historical-missing-id-order"
    order.status = "cancelled"
    order.response_body = {
        "status": "cancelled",
        "fill_count_fp": "1.00",
    }
    db_session.commit()

    class MissingOrderIdHistoricalClient:
        def iter_order_pages(self, **kwargs):
            yield (
                [
                    {
                        "order_id": "historical-missing-id-order",
                        "client_order_id": order.client_order_id,
                        "status": "cancelled",
                        "fill_count_fp": "1.00",
                    }
                ],
                None,
            )

        def iter_fill_pages(self, **kwargs):
            yield ([], None)

        def iter_historical_fill_pages(self, **kwargs):
            yield (
                [
                    {
                        "fill_id": "ambiguous-historical-fill",
                        "count_fp": "1.00",
                        "yes_price_dollars": "0.40",
                        "fee_cost": "0.01",
                    }
                ],
                None,
            )

    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: MissingOrderIdHistoricalClient(),
    )
    db_session.commit()
    db_session.refresh(order)

    assert db_session.scalars(select(KalshiOrderFill)).all() == []
    assert order.fills_synced_at is None


@pytest.mark.parametrize(
    "bad_fee",
    [None, "not-a-number", "nan", "inf", "-0.01"],
)
def test_invalid_fill_fee_stays_incomplete_then_heals_without_duplicate(
    db_session,
    monkeypatch,
    bad_fee,
):
    user = _seed(db_session)
    submit_client = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, local: submit_client)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)

    class FeeHealingClient:
        fee_value = bad_fee

        def list_orders(self):
            return [
                {
                    "order_id": "live_ord_1",
                    "client_order_id": order.client_order_id,
                    "status": "executed",
                    "fill_count_fp": "2.00",
                }
            ]

        def list_fills(self):
            fill = {
                "fill_id": "fee-heal-fill",
                "order_id": "live_ord_1",
                "count_fp": "2.00",
                "yes_price_dollars": "0.40",
                "side": "yes",
            }
            if self.fee_value is not None:
                fill["fee_cost"] = self.fee_value
            return [fill]

    client = FeeHealingClient()
    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: client,
    )
    db_session.commit()
    db_session.refresh(order)

    fills = db_session.scalars(
        select(KalshiOrderFill).where(KalshiOrderFill.kalshi_order_id == order.id)
    ).all()
    assert len(fills) == 1
    assert fills[0].fee_dollars is None
    assert order.fills_synced_at is None

    # Same fill id returns later with an explicit zero fee. Zero is valid,
    # updates the existing row, and permits completion without duplication.
    client.fee_value = "0.00"
    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: client,
    )
    db_session.commit()
    db_session.refresh(order)
    db_session.refresh(fills[0])

    assert len(
        db_session.scalars(
            select(KalshiOrderFill).where(
                KalshiOrderFill.kalshi_order_id == order.id
            )
        ).all()
    ) == 1
    assert fills[0].fee_dollars == 0.0
    assert order.fills_synced_at is not None


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("count_fp", "not-a-number"),
        ("count_fp", True),
        ("count_fp", "nan"),
        ("count_fp", "inf"),
        ("count_fp", "-0.01"),
        ("count_fp", "0.00"),
        ("yes_price_dollars", "nan"),
        ("yes_price_dollars", False),
        ("yes_price_dollars", "inf"),
        ("yes_price_dollars", "-0.01"),
        ("yes_price_dollars", "0.00"),
        ("yes_price_dollars", "1.00"),
        ("yes_price_dollars", "1.01"),
    ],
)
def test_invalid_incoming_fill_count_or_price_is_rejected_before_completion(
    db_session,
    monkeypatch,
    field,
    bad_value,
):
    user = _seed(db_session)
    submit_client = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, local: submit_client)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)

    class InvalidValueClient:
        def list_orders(self):
            return [
                {
                    "order_id": "live_ord_1",
                    "client_order_id": order.client_order_id,
                    "status": "executed",
                    "fill_count_fp": "2.00",
                }
            ]

        def list_fills(self):
            fill = {
                "fill_id": f"invalid-{field}-{bad_value}",
                "order_id": "live_ord_1",
                "count_fp": "2.00",
                "yes_price_dollars": "0.40",
                "fee_cost": "0.01",
            }
            fill[field] = bad_value
            return [fill]

    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: InvalidValueClient(),
    )
    db_session.commit()
    db_session.refresh(order)

    assert db_session.scalar(select(KalshiOrderFill.id)) is None
    assert order.fills_synced_at is None


@pytest.mark.parametrize(
    ("stored_count", "stored_price"),
    [
        (-1.0, 0.40),
        (0.0, 0.40),
        (float("inf"), 0.40),
        (1.0, -0.01),
        (1.0, 0.0),
        (1.0, 1.0),
        (1.0, float("inf")),
        (1.0, 1.01),
    ],
)
def test_invalid_existing_fill_values_block_completion(
    db_session,
    monkeypatch,
    stored_count,
    stored_price,
):
    user = _seed(db_session)
    submit_client = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, local: submit_client)
    order = ko.create_kalshi_order(db_session, _payload(quantity=1), user_id=user.id)
    db_session.commit()
    drain_once(db_session)
    order.status = "executed"
    order.response_body = {"status": "executed", "fill_count_fp": "1.00"}
    db_session.add(
        KalshiOrderFill(
            kalshi_order_id=order.id,
            kalshi_fill_id="stored-invalid-fill",
            count=stored_count,
            price=stored_price,
            side="yes",
            fee_dollars=0.01,
        )
    )
    db_session.commit()

    class EmptyFillClient:
        def list_fills(self, **kwargs):
            return [], None

    complete = ko._import_fills_for_order(
        db_session,
        EmptyFillClient(),
        order,
        ko._known_fill_ids(db_session),
    )
    db_session.commit()
    db_session.refresh(order)

    assert complete is False
    assert order.fills_synced_at is None


def test_valid_remote_fill_heals_legacy_zero_and_boundary_sentinels(db_session):
    user = _seed(db_session)
    market = db_session.scalar(select(Market).where(Market.ticker == "KXLIVE-TEST"))
    assert market is not None
    order = KalshiOrder(
        user_id=user.id,
        market_id=market.id,
        kind="single",
        ticker=market.ticker,
        environment="live",
        base_url=PROD_URL,
        client_order_id="heal-existing-client",
        kalshi_order_id="heal-existing-order",
        side="yes",
        action="buy",
        quantity=1,
        limit_price=0.40,
        approved_by_user=True,
        status="executed",
        response_body={"status": "executed", "fill_count_fp": "1.00"},
    )
    db_session.add(order)
    db_session.flush()
    fill = KalshiOrderFill(
        kalshi_order_id=order.id,
        kalshi_fill_id="heal-existing-fill",
        count=0.0,
        price=1.0,
        side="yes",
        fee_dollars=0.01,
    )
    db_session.add(fill)
    db_session.commit()

    class HealingFillClient:
        def list_fills(self, **kwargs):
            return (
                [
                    {
                        "fill_id": "heal-existing-fill",
                        "order_id": "heal-existing-order",
                        "count_fp": "1.00",
                        "yes_price_dollars": "0.40",
                        "fee_cost": "0.01",
                    }
                ],
                None,
            )

    assert ko._import_fills_for_order(
        db_session,
        HealingFillClient(),
        order,
        ko._known_fill_ids(db_session),
    )
    db_session.commit()
    db_session.refresh(order)
    db_session.refresh(fill)

    assert fill.count == 1.0
    assert fill.price == 0.40
    assert order.fills_synced_at is not None


@pytest.mark.parametrize(
    ("quantity", "remote_count", "remote_price", "remote_fee"),
    [
        (1, "1.00", "0.80", "0.09"),
        (2, "2.00", "0.20", "0.01"),
    ],
)
def test_valid_duplicate_fill_conflicts_block_without_rewriting_ledger(
    db_session,
    caplog,
    quantity,
    remote_count,
    remote_price,
    remote_fee,
):
    user = _seed(db_session)
    market = db_session.scalar(select(Market).where(Market.ticker == "KXLIVE-TEST"))
    assert market is not None
    order = KalshiOrder(
        user_id=user.id,
        market_id=market.id,
        kind="single",
        ticker=market.ticker,
        environment="live",
        base_url=PROD_URL,
        client_order_id=f"conflict-client-{quantity}",
        kalshi_order_id=f"conflict-order-{quantity}",
        side="yes",
        action="buy",
        quantity=quantity,
        limit_price=0.40,
        approved_by_user=True,
        status="executed",
        response_body={
            "status": "executed",
            "fill_count_fp": f"{quantity:.2f}",
        },
    )
    db_session.add(order)
    db_session.flush()
    fill = KalshiOrderFill(
        kalshi_order_id=order.id,
        kalshi_fill_id=f"conflict-fill-{quantity}",
        count=1.0,
        price=0.20,
        side="yes",
        fee_dollars=0.01,
    )
    db_session.add(fill)
    db_session.commit()

    class ConflictingFillClient:
        def list_fills(self, **kwargs):
            return (
                [
                    {
                        "fill_id": fill.kalshi_fill_id,
                        "order_id": order.kalshi_order_id,
                        "count_fp": remote_count,
                        "yes_price_dollars": remote_price,
                        "fee_cost": remote_fee,
                    }
                ],
                None,
            )

    with caplog.at_level("WARNING"):
        complete = ko._import_fills_for_order(
            db_session,
            ConflictingFillClient(),
            order,
            ko._known_fill_ids(db_session),
        )
    db_session.commit()
    db_session.refresh(order)
    db_session.refresh(fill)

    assert complete is False
    assert "conflicts with its stored ledger values" in caplog.text
    assert fill.count == 1.0
    assert fill.price == 0.20
    assert fill.fee_dollars == 0.01
    assert "_reconciliation_conflict" in fill.raw_data
    assert order.fills_synced_at is None

    class EmptyFillClient:
        def list_fills(self, **kwargs):
            return [], None

    # The warning is persisted in raw_data, so a later sparse/archived fill
    # page cannot silently bless the conflicting historical values.
    assert not ko._import_fills_for_order(
        db_session,
        EmptyFillClient(),
        order,
        ko._known_fill_ids(db_session),
    )
    assert order.fills_synced_at is None


def test_reconcile_resumes_order_cursor_after_page_cap(db_session):
    ko._clear_order_page_resume_cursors()
    user = _seed(db_session)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    order.status = "resting"
    order.kalshi_order_id = "local-before-resume"
    db_session.commit()

    class ResumingOrderClient:
        seen_cursors: list[str | None] = []

        def iter_order_pages(self, *, cursor=None, **kwargs):
            self.seen_cursors.append(cursor)
            if cursor is None:
                # Simulate max_pages stopping before the local order appears.
                yield ([{"client_order_id": "unrelated"}], "page-two")
                return
            assert cursor == "page-two"
            yield (
                [
                    {
                        "order_id": "remote-after-resume",
                        "client_order_id": order.client_order_id,
                        "status": "cancelled",
                        "fill_count_fp": "0.00",
                    }
                ],
                None,
            )

        def iter_fill_pages(self, **kwargs):
            yield ([], None)

    client = ResumingOrderClient()
    try:
        ko.reconcile_kalshi_live_state(
            db_session,
            client_factory=lambda db, local: client,
        )
        db_session.refresh(order)
        assert order.status == "resting"

        ko.reconcile_kalshi_live_state(
            db_session,
            client_factory=lambda db, local: client,
        )
        db_session.commit()
        db_session.refresh(order)

        assert client.seen_cursors == [None, "page-two"]
        assert order.kalshi_order_id == "remote-after-resume"
        assert order.status == "cancelled"
        assert order.fills_synced_at is not None
        assert ko._get_order_page_resume_cursor((user.id, PROD_URL)) is None
    finally:
        ko._clear_order_page_resume_cursors()


def test_targeted_fill_scan_resumes_after_page_cap(db_session):
    user = _seed(db_session)
    order = ko.create_kalshi_order(db_session, _payload(quantity=2), user_id=user.id)
    order.kalshi_order_id = "fill-resume-order"
    order.status = "executed"
    order.response_body = {"status": "executed", "fill_count_fp": "2.00"}
    db_session.commit()

    class ResumingFillClient:
        seen_cursors: list[str | None] = []

        def iter_fill_pages(self, *, cursor=None, order_id=None, **kwargs):
            assert order_id == "fill-resume-order"
            self.seen_cursors.append(cursor)
            if cursor is None:
                yield (
                    [
                        {
                            "fill_id": "fill-resume-one",
                            "order_id": order_id,
                            "count_fp": "1.00",
                            "yes_price_dollars": "0.40",
                            "fee_cost": "0.01",
                        }
                    ],
                    "page-two",
                )
                return
            assert cursor == "page-two"
            yield (
                [
                    {
                        "fill_id": "fill-resume-two",
                        "order_id": order_id,
                        "count_fp": "1.00",
                        "yes_price_dollars": "0.39",
                        "fee_cost": "0.01",
                    }
                ],
                None,
            )

    client = ResumingFillClient()
    known_fill_ids = ko._known_fill_ids(db_session)
    resume_key = ko._fill_page_resume_key(order)

    assert not ko._import_fills_for_order(
        db_session,
        client,
        order,
        known_fill_ids,
    )
    assert ko._get_fill_page_resume_cursor(resume_key) == "page-two"
    assert order.fills_synced_at is None

    assert ko._import_fills_for_order(
        db_session,
        client,
        order,
        known_fill_ids,
    )
    db_session.commit()
    db_session.refresh(order)

    assert client.seen_cursors == [None, "page-two"]
    assert ko._get_fill_page_resume_cursor(resume_key) is None
    assert sum(
        db_session.scalars(
            select(KalshiOrderFill.count).where(
                KalshiOrderFill.kalshi_order_id == order.id
            )
        ).all()
    ) == 2.0
    assert order.fills_synced_at is not None


def test_targeted_fill_scan_clears_repeated_resume_cursor(db_session):
    user = _seed(db_session)
    order = ko.create_kalshi_order(db_session, _payload(quantity=1), user_id=user.id)
    order.kalshi_order_id = "stale-fill-cursor-order"
    order.status = "cancelled"
    order.response_body = {"status": "cancelled", "fill_count_fp": "0.00"}
    db_session.commit()

    class RepeatingFillClient:
        def iter_fill_pages(self, *, cursor=None, **kwargs):
            yield [], "stale-cursor"

    client = RepeatingFillClient()
    resume_key = ko._fill_page_resume_key(order)
    known_fill_ids = ko._known_fill_ids(db_session)

    assert not ko._import_fills_for_order(
        db_session,
        client,
        order,
        known_fill_ids,
    )
    assert ko._get_fill_page_resume_cursor(resume_key) == "stale-cursor"

    assert not ko._import_fills_for_order(
        db_session,
        client,
        order,
        known_fill_ids,
    )
    assert ko._get_fill_page_resume_cursor(resume_key) is None
    assert order.fills_synced_at is None


def test_fill_resume_cursor_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(ko, "_FILL_PAGE_RESUME_MAX_ORDERS", 1)
    first_key = (1, PROD_URL, "first-order")
    second_key = (1, PROD_URL, "second-order")

    ko._set_fill_page_resume_cursor(first_key, "first-cursor")
    ko._set_fill_page_resume_cursor(second_key, "second-cursor")

    assert ko._get_fill_page_resume_cursor(first_key) is None
    assert ko._get_fill_page_resume_cursor(second_key) == "second-cursor"


def test_fill_fetch_failure_stays_eligible_and_second_pass_heals(
    db_session,
    monkeypatch,
):
    user = _seed(db_session)

    class RecoveringClient(FakeTradeClient):
        fail_fills = True

        def create_order(self, **kwargs):
            return {
                "request": {"ticker": kwargs["ticker"]},
                "order": {
                    "order_id": "recover-order",
                    "client_order_id": kwargs.get("client_order_id"),
                    "status": "executed",
                    "fill_count": 2.0,
                    "remaining_count": 0.0,
                },
                "raw": {"fill_count": "2.00", "remaining_count": "0.00"},
            }

        def list_orders(self, *, limit=1000, cursor=None):
            return (
                [
                    {
                        "order_id": "recover-order",
                        "client_order_id": order.client_order_id,
                        "status": "executed",
                        "fill_count_fp": "2.00",
                    }
                ],
                None,
            )

        def list_fills(self, *, limit=1000, cursor=None, order_id=None):
            if self.fail_fills:
                raise RuntimeError("scripted fill read outage")
            return (
                [
                    {
                        "fill_id": "recovered-fill",
                        "order_id": "recover-order",
                        "count_fp": "2.00",
                        "yes_price_dollars": "0.40",
                        "fee_cost": "0.03",
                    }
                ],
                None,
            )

    client = RecoveringClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, local: client)
    order = ko.create_kalshi_order(
        db_session,
        _payload(time_in_force="immediate_or_cancel"),
        user_id=user.id,
    )
    db_session.commit()

    counts = drain_once(db_session)
    db_session.refresh(order)
    assert counts["succeeded"] == 1
    assert order.status == "executed"
    assert order.fills_synced_at is None

    # A failed reconciliation still leaves the terminal row eligible.
    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: client,
    )
    db_session.refresh(order)
    assert order.fills_synced_at is None

    client.fail_fills = False
    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: client,
    )
    db_session.commit()
    db_session.refresh(order)
    assert order.fills_synced_at is not None
    assert db_session.scalar(select(KalshiOrderFill.count)) == 2.0


def test_reconcile_does_not_stamp_when_expected_fill_count_mismatches(
    db_session,
    monkeypatch,
):
    user = _seed(db_session)
    fake = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)

    class ShortFillClient:
        def list_orders(self):
            return [
                {
                    "order_id": "live_ord_1",
                    "client_order_id": order.client_order_id,
                    "status": "executed",
                    "fill_count_fp": "2.00",
                }
            ]

        def list_fills(self):
            return [
                {
                    "fill_id": "short-fill",
                    "order_id": "live_ord_1",
                    "count_fp": "1.00",
                    "yes_price_dollars": "0.40",
                }
            ]

    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: ShortFillClient(),
    )
    db_session.commit()
    db_session.refresh(order)

    assert db_session.scalar(select(KalshiOrderFill.count)) == 1.0
    assert order.fills_synced_at is None


def test_reconcile_page_cap_with_live_cursor_never_stamps_complete(
    db_session,
    monkeypatch,
):
    user = _seed(db_session)
    fake = FakeTradeClient()
    monkeypatch.setattr(ko, "client_for_order", lambda db, order: fake)
    order = ko.create_kalshi_order(db_session, _payload(), user_id=user.id)
    db_session.commit()
    drain_once(db_session)

    class CappedClient:
        def iter_order_pages(self, **kwargs):
            yield (
                [
                    {
                        "order_id": "live_ord_1",
                        "client_order_id": order.client_order_id,
                        "status": "cancelled",
                        "fill_count_fp": "1.00",
                    }
                ],
                None,
            )

        def iter_fill_pages(self, **kwargs):
            # Simulates an iterator stopping at max_pages while Kalshi still
            # advertises another page. The visible cursor must prevent stamp.
            yield (
                [
                    {
                        "fill_id": "capped-fill",
                        "order_id": "live_ord_1",
                        "count_fp": "1.00",
                        "yes_price_dollars": "0.40",
                    }
                ],
                "more-remains",
            )

    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: CappedClient(),
    )
    db_session.commit()
    db_session.refresh(order)

    assert db_session.scalar(select(KalshiOrderFill.count)) == 1.0
    assert order.fills_synced_at is None


def test_reconcile_terminal_backfill_is_oldest_first_and_capped(db_session):
    user = _seed(db_session)
    market = db_session.scalar(select(Market).where(Market.ticker == "KXLIVE-TEST"))
    assert market is not None
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    seeded: list[KalshiOrder] = []
    for index in range(25):
        seeded.append(
            KalshiOrder(
                user_id=user.id,
                market_id=market.id,
                kind="single",
                ticker=market.ticker,
                environment="live",
                base_url=PROD_URL,
                client_order_id=f"terminal-client-{index}",
                kalshi_order_id=f"terminal-order-{index}",
                side="yes",
                action="buy",
                quantity=1,
                limit_price=0.40,
                approved_by_user=True,
                status="cancelled",
                response_body={
                    "status": "cancelled",
                    "fill_count_fp": "0.00",
                },
                created_at=start + timedelta(seconds=index),
            )
        )
    db_session.add_all(seeded)
    db_session.commit()

    class EmptySyncClient:
        def list_orders(self):
            return []

        def list_fills(self):
            return []

    ko.reconcile_kalshi_live_state(
        db_session,
        client_factory=lambda db, local: EmptySyncClient(),
    )
    db_session.commit()

    synced_ids = {
        row.client_order_id
        for row in db_session.scalars(
            select(KalshiOrder).where(KalshiOrder.fills_synced_at.is_not(None))
        ).all()
    }
    assert len(synced_ids) == ko.TERMINAL_FILL_SYNC_BATCH
    assert synced_ids == {f"terminal-client-{index}" for index in range(20)}


def test_terminal_candidate_query_is_bounded_and_rotates_past_failed_group(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(ko, "TERMINAL_FILL_SYNC_CANDIDATE_LIMIT", 2)
    user = _seed(db_session)
    market = db_session.scalar(select(Market).where(Market.ticker == "KXLIVE-TEST"))
    assert market is not None
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    rows: list[KalshiOrder] = []
    for index in range(3):
        failed_group = index < 2
        rows.append(
            KalshiOrder(
                user_id=user.id,
                market_id=market.id,
                kind="single",
                ticker=market.ticker,
                environment="live",
                base_url="https://failed.example" if failed_group else PROD_URL,
                client_order_id=f"candidate-client-{index}",
                kalshi_order_id=f"candidate-order-{index}",
                side="yes",
                action="buy",
                quantity=1,
                limit_price=0.40,
                approved_by_user=True,
                status="cancelled",
                response_body={
                    "status": "cancelled",
                    "fill_count_fp": "0.00",
                },
                created_at=start + timedelta(seconds=index),
            )
        )
    db_session.add_all(rows)
    db_session.commit()

    class EmptySyncClient:
        def list_orders(self):
            return []

        def list_fills(self):
            return []

    def factory(db, local):
        if local.base_url == "https://failed.example":
            raise RuntimeError("scripted credential failure")
        return EmptySyncClient()

    # The first bounded window contains only the failing group.
    ko.reconcile_kalshi_live_state(db_session, client_factory=factory)
    db_session.commit()
    assert all(row.fills_synced_at is None for row in rows)

    # Rotation advances to the next window; the healthy group is not starved.
    ko.reconcile_kalshi_live_state(db_session, client_factory=factory)
    db_session.commit()
    for row in rows:
        db_session.refresh(row)
    assert rows[0].fills_synced_at is None
    assert rows[1].fills_synced_at is None
    assert rows[2].fills_synced_at is not None


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
        db_session,
        user_id=user.id,
        key_id="k1",
        private_key_pem=SAMPLE_PEM,
        base_url=demo_url,
    )
    db_session.commit()

    client = ko.client_for_order(db_session, order)
    assert client.base_url == PROD_URL.rstrip("/")
