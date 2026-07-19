"""Real Kalshi combos — combinability reasons, preview, and the
mint-then-order handler's idempotency guarantees.

The money-critical sequencing proofs:
- retry after "mint ok, order crashed" does NOT re-mint (lookup hits)
  and re-submits with the SAME client_order_id
- lookup hit → zero mint calls ever
- mint 4xx → terminal ``mint_failed`` with the body surfaced
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import KalshiOrder, Market, OutboxEntry, User
from app.schemas import (
    KalshiComboLegCreate,
    KalshiComboOrderCreate,
    KalshiComboPreviewRequest,
)
from app.services import kalshi_combos as kc
from app.services.outbox import STATUS_DONE, drain_once
from app.services.user_kalshi import upsert_user_credentials
from app.services.users import seed_users_from_settings

SAMPLE_PEM = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"

COLLECTION = {
    "collection_ticker": "KXMLBCOMBO",
    "size_min": 2,
    "size_max": 6,
    "associated_events": [
        {"ticker": "EV-NYY", "is_yes_only": False, "size_max": 1},
        {"ticker": "EV-BOS", "is_yes_only": True, "size_max": 1},
        {"ticker": "EV-LAD", "is_yes_only": False, "size_max": 2},
    ],
}


class FakeComboClient:
    """Scriptable trade-client stand-in for resolver/handler tests."""

    def __init__(
        self,
        *,
        collections=None,
        lookup_result=None,
        mint_result=None,
        base_url: str = PROD_URL,
    ):
        self.base_url = base_url
        self.collections = collections if collections is not None else [COLLECTION]
        self.lookup_result = lookup_result
        self.mint_result = mint_result or {
            "event_ticker": "EVCOMBO",
            "market_ticker": "KXCOMBO-MINTED",
        }
        self.lookup_calls = 0
        self.mint_calls = 0
        self.order_calls: list[dict] = []

    def get_multivariate_event_collections(self, **kwargs):
        return self.collections

    def lookup_combo_market(self, collection_ticker, selected_markets):
        self.lookup_calls += 1
        return self.lookup_result

    def create_combo_market(self, collection_ticker, selected_markets, **kwargs):
        self.mint_calls += 1
        return self.mint_result

    def create_order(self, **kwargs):
        self.order_calls.append(kwargs)
        return {
            "request": {"ticker": kwargs["ticker"]},
            "order": {
                "order_id": "combo_ord_1",
                "client_order_id": kwargs.get("client_order_id"),
                "status": "resting",
            },
        }

    def get_market(self, ticker):
        return {"yes_bid_dollars": "0.18", "yes_ask_dollars": "0.22"}


def _seed(db: Session, *, markets: list[tuple[str, str | None]]) -> User:
    seed_users_from_settings(db, Settings(SIKA_USERS="chris"))
    db.commit()
    user = db.query(User).filter_by(username="chris").one()
    upsert_user_credentials(
        db, user_id=user.id, key_id="k1", private_key_pem=SAMPLE_PEM, base_url=PROD_URL
    )
    for ticker, event in markets:
        db.add(Market(ticker=ticker, title=ticker, status="open", event_ticker=event))
    db.commit()
    return user


def _legs(*specs: tuple[str, str]) -> list[KalshiComboLegCreate]:
    return [
        KalshiComboLegCreate(ticker=ticker, side=side, entry_price=0.5)
        for ticker, side in specs
    ]


@pytest.fixture(autouse=True)
def _clear_cache():
    kc.clear_collections_cache()
    yield
    kc.clear_collections_cache()


# ── Resolver reason matrix ──────────────────────────────────────────


def test_resolver_rejects_untracked_ticker(db_session):
    _seed(db_session, markets=[("KXA", "EV-NYY")])
    collection, _, reason = kc.resolve_collection_for_legs(
        db_session, FakeComboClient(), _legs(("KXA", "yes"), ("KXNOPE", "yes"))
    )
    assert collection is None
    assert "not a tracked market" in reason


def test_resolver_rejects_missing_event_mapping(db_session):
    _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", None)])
    collection, _, reason = kc.resolve_collection_for_legs(
        db_session, FakeComboClient(), _legs(("KXA", "yes"), ("KXB", "yes"))
    )
    assert collection is None
    assert "no event mapping" in reason


def test_resolver_rejects_event_outside_collection(db_session):
    _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-UNKNOWN")])
    collection, _, reason = kc.resolve_collection_for_legs(
        db_session, FakeComboClient(), _legs(("KXA", "yes"), ("KXB", "yes"))
    )
    assert collection is None
    assert "EV-UNKNOWN" in reason


def test_resolver_rejects_too_many_legs_from_one_event(db_session):
    _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-NYY")])
    collection, _, reason = kc.resolve_collection_for_legs(
        db_session, FakeComboClient(), _legs(("KXA", "yes"), ("KXB", "yes"))
    )
    assert collection is None
    assert "only 1 leg(s) allowed" in reason


def test_resolver_allows_two_legs_when_event_size_permits(db_session):
    _seed(db_session, markets=[("KXA", "EV-LAD"), ("KXB", "EV-LAD")])
    collection, markets, reason = kc.resolve_collection_for_legs(
        db_session, FakeComboClient(), _legs(("KXA", "yes"), ("KXB", "yes"))
    )
    assert reason is None
    assert collection["collection_ticker"] == "KXMLBCOMBO"


def test_resolver_rejects_no_side_on_yes_only_event(db_session):
    _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])
    collection, _, reason = kc.resolve_collection_for_legs(
        db_session, FakeComboClient(), _legs(("KXA", "yes"), ("KXB", "no"))
    )
    assert collection is None
    assert "only combines as a YES pick" in reason


def test_resolver_rejects_leg_count_outside_collection_bounds(db_session):
    tight = dict(COLLECTION, size_min=3)
    _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])
    collection, _, reason = kc.resolve_collection_for_legs(
        db_session,
        FakeComboClient(collections=[tight]),
        _legs(("KXA", "yes"), ("KXB", "yes")),
    )
    assert collection is None
    assert "3–6 legs" in reason


# ── Preview ─────────────────────────────────────────────────────────


def test_preview_without_credentials_returns_reason(db_session):
    seed_users_from_settings(db_session, Settings(SIKA_USERS="chris"))
    db_session.commit()
    user = db_session.query(User).filter_by(username="chris").one()
    result = kc.preview_kalshi_combo(
        db_session,
        KalshiComboPreviewRequest(legs=_legs(("KXA", "yes"), ("KXB", "yes"))),
        user_id=user.id,
    )
    assert result.combinable is False
    assert "connect kalshi" in result.reason


def test_preview_combinable_with_existing_market_quote(db_session, monkeypatch):
    user = _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])
    fake = FakeComboClient(lookup_result={"market_ticker": "KXCOMBO-EXISTING"})
    monkeypatch.setattr(kc, "KalshiTradeClient", lambda **kwargs: fake)

    result = kc.preview_kalshi_combo(
        db_session,
        KalshiComboPreviewRequest(legs=_legs(("KXA", "yes"), ("KXB", "yes"))),
        user_id=user.id,
    )
    assert result.combinable is True
    assert result.collection_ticker == "KXMLBCOMBO"
    assert result.existing_market_ticker == "KXCOMBO-EXISTING"
    assert result.implied_price == 0.25  # 0.5 × 0.5
    assert result.quote_yes_ask == 0.22


def test_preview_never_mints(db_session, monkeypatch):
    user = _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])
    fake = FakeComboClient(lookup_result=None)  # market doesn't exist
    monkeypatch.setattr(kc, "KalshiTradeClient", lambda **kwargs: fake)

    result = kc.preview_kalshi_combo(
        db_session,
        KalshiComboPreviewRequest(legs=_legs(("KXA", "yes"), ("KXB", "yes"))),
        user_id=user.id,
    )
    assert result.combinable is True
    assert result.existing_market_ticker is None
    assert fake.mint_calls == 0


# ── Placement + handler sequencing ──────────────────────────────────


def _place(db_session, user, fake, monkeypatch, **overrides):
    monkeypatch.setattr(kc, "KalshiTradeClient", lambda **kwargs: fake)
    monkeypatch.setattr(kc, "client_for_order", lambda db, order: fake)
    payload = KalshiComboOrderCreate(
        legs=_legs(("KXA", "yes"), ("KXB", "yes")),
        quantity=overrides.get("quantity", 10),
        limit_price=overrides.get("limit_price", 0.25),
        approved=overrides.get("approved", True),
    )
    return kc.create_kalshi_combo_order(db_session, payload, user_id=user.id)


def test_combo_create_writes_order_legs_and_outbox(db_session, monkeypatch):
    user = _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])
    fake = FakeComboClient()
    order = _place(db_session, user, fake, monkeypatch)
    db_session.commit()

    assert order.kind == "combo"
    assert order.ticker is None
    assert order.collection_ticker == "KXMLBCOMBO"
    assert order.side == "yes"
    assert [leg.market_ticker for leg in order.legs] == ["KXA", "KXB"]
    assert [leg.event_ticker for leg in order.legs] == ["EV-NYY", "EV-BOS"]

    entries = db_session.scalars(
        select(OutboxEntry).where(OutboxEntry.target_id == order.id)
    ).all()
    assert len(entries) == 1
    assert entries[0].intent_kind == "kalshi_combo_submit"
    assert entries[0].payload["selected_markets"][0]["event_ticker"] == "EV-NYY"


def test_combo_create_rejects_uncombinable_legs(db_session, monkeypatch):
    user = _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-UNKNOWN")])
    fake = FakeComboClient()
    with pytest.raises(HTTPException) as err:
        _place(db_session, user, fake, monkeypatch)
    assert err.value.status_code == 400
    assert "Not combinable" in err.value.detail


def test_combo_create_enforces_cost_cap(db_session, monkeypatch):
    user = _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])
    fake = FakeComboClient()
    with pytest.raises(HTTPException) as err:
        _place(db_session, user, fake, monkeypatch, quantity=200)  # 200 × .25 = $50 > $25
    assert err.value.status_code == 400
    assert "cap" in err.value.detail.lower()


def test_combo_drain_mints_then_orders_with_persisted_id(db_session, monkeypatch):
    user = _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])
    fake = FakeComboClient(lookup_result=None)  # not minted yet
    order = _place(db_session, user, fake, monkeypatch)
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["succeeded"] == 1

    db_session.refresh(order)
    assert fake.lookup_calls == 1
    assert fake.mint_calls == 1
    assert order.ticker == "KXCOMBO-MINTED"
    assert order.combo_event_ticker == "EVCOMBO"
    assert order.status == "resting"
    assert order.kalshi_order_id == "combo_ord_1"
    assert fake.order_calls[0]["client_order_id"] == order.client_order_id
    assert fake.order_calls[0]["ticker"] == "KXCOMBO-MINTED"


def test_combo_drain_skips_mint_when_lookup_hits(db_session, monkeypatch):
    user = _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])
    fake = FakeComboClient(
        lookup_result={"event_ticker": "EVCOMBO", "market_ticker": "KXCOMBO-EXISTING"}
    )
    order = _place(db_session, user, fake, monkeypatch)
    db_session.commit()

    drain_once(db_session)
    db_session.refresh(order)
    assert fake.mint_calls == 0
    assert order.ticker == "KXCOMBO-EXISTING"
    assert order.status == "resting"


def test_combo_retry_after_order_failure_does_not_remint(db_session, monkeypatch):
    """Crash-between-mint-and-order: first drain mints then the order
    5xxs (retryable). Second drain must lookup (or reuse the persisted
    ticker), NOT mint again, and reuse the same client_order_id."""
    user = _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])

    class FlakyOrderClient(FakeComboClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.order_attempts = 0

        def create_order(self, **kwargs):
            self.order_attempts += 1
            if self.order_attempts == 1:
                request = httpx.Request("POST", "https://x/portfolio/orders")
                response = httpx.Response(502, text="bad gateway", request=request)
                raise httpx.HTTPStatusError("502", request=request, response=response)
            return super().create_order(**kwargs)

    fake = FlakyOrderClient(lookup_result=None)
    order = _place(db_session, user, fake, monkeypatch)
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["failed"] == 1  # order leg failed, retry pending
    db_session.refresh(order)
    assert order.ticker == "KXCOMBO-MINTED"  # checkpoint survived
    assert fake.mint_calls == 1

    # Backoff: force the entry eligible now.
    entry = db_session.scalars(select(OutboxEntry)).one()
    entry.next_attempt_at = None
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["succeeded"] == 1
    db_session.refresh(order)
    assert fake.mint_calls == 1  # NO second mint
    assert order.status == "resting"
    ids = {call["client_order_id"] for call in fake.order_calls}
    assert ids == {order.client_order_id}  # same id both attempts


def test_combo_mint_4xx_is_terminal(db_session, monkeypatch):
    user = _seed(db_session, markets=[("KXA", "EV-NYY"), ("KXB", "EV-BOS")])

    class RejectingMintClient(FakeComboClient):
        def create_combo_market(self, collection_ticker, selected_markets, **kwargs):
            self.mint_calls += 1
            request = httpx.Request("POST", "https://x/multivariate_event_collections/KXMLBCOMBO")
            response = httpx.Response(400, text="markets not combinable", request=request)
            raise httpx.HTTPStatusError("400", request=request, response=response)

    fake = RejectingMintClient(lookup_result=None)
    order = _place(db_session, user, fake, monkeypatch)
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["succeeded"] == 1  # handler completed terminally

    db_session.refresh(order)
    assert order.status == "mint_failed"
    assert "markets not combinable" in order.error_detail
    assert not fake.order_calls  # never tried to order

    entry = db_session.scalars(select(OutboxEntry)).one()
    assert entry.status == STATUS_DONE
