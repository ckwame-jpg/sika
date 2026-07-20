"""KalshiTradeClient — order idempotency, combo endpoints, env routing.

Money-safety focus:
- ``create_order`` must send the CALLER's ``client_order_id`` (the
  persisted one) so outbox retries re-submit the same order instead of
  minting a duplicate on the live host.
- ``build_trade_client_for_user`` honors the user's stored ``base_url``
  (the /settings/kalshi env choice) while ``build_demo_client_for_user``
  stays pinned to the sandbox.
- Combo endpoints: lookup is a free existence probe (404 → None, never
  creates); create-market sends the documented ``selected_markets``
  shape.

HTTP is faked by monkeypatching ``httpx.request`` (the authenticated
client calls it directly — there's no injectable http client like the
public client has).
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy.orm import Session

import app.clients.kalshi as kalshi_module
from app.clients.kalshi import KalshiDemoClient, KalshiTradeClient
from app.config import Settings, get_settings
from app.services.user_kalshi import (
    build_demo_client_for_user,
    build_trade_client_for_user,
    upsert_user_credentials,
)
from app.services.users import seed_users_from_settings
from app.models import User

SAMPLE_PEM = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_URL = get_settings().kalshi_demo_base_url


class _CapturingTransport:
    """Monkeypatch target for ``httpx.request`` — records each call and
    plays back scripted responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "json": json, "params": params}
        )
        response = self.responses.pop(0)
        # raise_for_status needs a request attached.
        response.request = httpx.Request(method, url)
        return response


@pytest.fixture()
def client(monkeypatch) -> KalshiTradeClient:
    # Signing is not under test — stub it out so no real key is needed.
    monkeypatch.setattr(
        KalshiTradeClient, "_headers", lambda self, method, url: {"Content-Type": "application/json"}
    )
    return KalshiTradeClient(key_id="test-key", private_key_pem=b"unused", base_url=PROD_URL)


def _patch_http(monkeypatch, responses: list[httpx.Response]) -> _CapturingTransport:
    transport = _CapturingTransport(responses)
    monkeypatch.setattr(kalshi_module.httpx, "request", transport)
    return transport


# ── create_order (V2 endpoint) ───────────────────────────────────────
# The legacy POST /portfolio/orders 410s on prod
# (deprecated_v1_order_endpoint) — everything below pins the V2
# contract: YES-book bid/ask sides, fixed-point strings, cent-snapped
# prices, and normalization back to the legacy {"order": ...} shape.

V2_RESTING = {"order_id": "o1", "client_order_id": "cid", "fill_count": "0.00", "remaining_count": "3.00", "ts_ms": 1}


def test_create_order_uses_v2_endpoint_and_contract(monkeypatch, client) -> None:
    transport = _patch_http(monkeypatch, [httpx.Response(200, json=V2_RESTING)])
    result = client.create_order(
        ticker="KXTEST",
        side="yes",
        action="buy",
        quantity=3,
        limit_price=0.42,
        time_in_force="good_till_canceled",
        client_order_id="persisted-id-123",
    )
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/portfolio/events/orders")
    body = call["json"]
    assert body == {
        "ticker": "KXTEST",
        "side": "bid",  # buy YES = bid on the yes book
        "count": "3.00",
        "price": "0.42",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": "persisted-id-123",
    }
    # Normalized for the outbox handlers.
    assert result["order"]["order_id"] == "o1"
    assert result["order"]["status"] == "resting"


def test_create_order_maps_no_side_to_yes_book_ask(monkeypatch, client) -> None:
    transport = _patch_http(monkeypatch, [httpx.Response(200, json=V2_RESTING)])
    client.create_order(
        ticker="KXTEST",
        side="no",
        action="buy",
        quantity=1,
        limit_price=0.35,
        time_in_force="good_till_canceled",
    )
    body = transport.calls[0]["json"]
    assert body["side"] == "ask"  # buy NO = ask on the yes book
    assert body["price"] == "0.65"  # 1 − 0.35
    assert body["client_order_id"]  # uuid fallback for ad-hoc calls


def test_create_order_quantizes_subcent_prices(monkeypatch, client) -> None:
    """+245 american odds parse to 0.2899… — the exchange 400s
    (invalid_price) on sub-cent limits, so the client snaps to the
    1¢ tick."""
    transport = _patch_http(monkeypatch, [httpx.Response(200, json=V2_RESTING)])
    client.create_order(
        ticker="KXTEST",
        side="yes",
        action="buy",
        quantity=2,
        limit_price=0.2899,
        time_in_force="good_till_canceled",
    )
    assert transport.calls[0]["json"]["price"] == "0.29"


def test_create_order_normalizes_immediate_fill_to_executed(monkeypatch, client) -> None:
    filled = {"order_id": "o2", "fill_count": "2.00", "remaining_count": "0.00", "ts_ms": 1}
    _patch_http(monkeypatch, [httpx.Response(200, json=filled)])
    result = client.create_order(
        ticker="KXTEST",
        side="yes",
        action="buy",
        quantity=2,
        limit_price=0.5,
        time_in_force="immediate_or_cancel",
    )
    assert result["order"]["status"] == "executed"


def test_create_order_normalizes_ioc_zero_fill_to_cancelled(monkeypatch, client) -> None:
    """An IOC that finds no liquidity fills nothing and rests nothing —
    the row must read ``cancelled`` (nothing happened), never
    ``resting``."""
    empty = {"order_id": "o3", "fill_count": "0.00", "remaining_count": "0.00", "ts_ms": 1}
    _patch_http(monkeypatch, [httpx.Response(200, json=empty)])
    result = client.create_order(
        ticker="KXTEST",
        side="yes",
        action="buy",
        quantity=2,
        limit_price=0.5,
        time_in_force="immediate_or_cancel",
    )
    assert result["order"]["status"] == "cancelled"


def test_cancel_order_uses_v2_path_and_normalizes(monkeypatch, client) -> None:
    transport = _patch_http(
        monkeypatch,
        [httpx.Response(200, json={"order_id": "o1", "reduced_by": "3.00", "ts_ms": 1})],
    )
    result = client.cancel_order("o1")
    call = transport.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/portfolio/events/orders/o1")
    assert result["order"] == {"order_id": "o1", "status": "cancelled"}


def test_quantize_price_clamps_to_tradable_band() -> None:
    assert KalshiTradeClient.quantize_price(0.2899) == 0.29
    assert KalshiTradeClient.quantize_price(0.005) == 0.01
    assert KalshiTradeClient.quantize_price(0.999) == 0.99


# ── combo endpoints ──────────────────────────────────────────────────

SELECTED = [
    {"market_ticker": "KXA", "event_ticker": "EVA", "side": "yes"},
    {"market_ticker": "KXB", "event_ticker": "EVB", "side": "yes"},
]


def test_lookup_combo_market_returns_none_on_404(monkeypatch, client) -> None:
    transport = _patch_http(
        monkeypatch, [httpx.Response(404, json={"error": "not found"})]
    )
    assert client.lookup_combo_market("KXCOMBO-COL", SELECTED) is None
    call = transport.calls[0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/multivariate_event_collections/KXCOMBO-COL/lookup")
    assert call["json"] == {"selected_markets": SELECTED}


def test_lookup_combo_market_returns_payload_when_minted(monkeypatch, client) -> None:
    _patch_http(
        monkeypatch,
        [httpx.Response(200, json={"event_ticker": "EVC", "market_ticker": "KXC"})],
    )
    result = client.lookup_combo_market("KXCOMBO-COL", SELECTED)
    assert result == {"event_ticker": "EVC", "market_ticker": "KXC"}


def test_lookup_combo_market_raises_on_server_error(monkeypatch, client) -> None:
    _patch_http(monkeypatch, [httpx.Response(500, json={})])
    with pytest.raises(httpx.HTTPStatusError):
        client.lookup_combo_market("KXCOMBO-COL", SELECTED)


def test_create_combo_market_sends_documented_shape(monkeypatch, client) -> None:
    transport = _patch_http(
        monkeypatch,
        [
            httpx.Response(
                200,
                json={"event_ticker": "EVC", "market_ticker": "KXC", "market": {"ticker": "KXC"}},
            )
        ],
    )
    result = client.create_combo_market("KXCOMBO-COL", SELECTED)
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/multivariate_event_collections/KXCOMBO-COL")
    assert call["json"] == {"selected_markets": SELECTED, "with_market_payload": True}
    assert result["market_ticker"] == "KXC"


def test_get_multivariate_event_collections_parses_and_filters(monkeypatch, client) -> None:
    transport = _patch_http(
        monkeypatch,
        [httpx.Response(200, json={"multivariate_contracts": [{"collection_ticker": "KXCOL"}]})],
    )
    rows = client.get_multivariate_event_collections(associated_event_ticker="EVA")
    assert rows == [{"collection_ticker": "KXCOL"}]
    params = transport.calls[0]["params"]
    assert params["status"] == "open"
    assert params["associated_event_ticker"] == "EVA"


# ── environment routing ──────────────────────────────────────────────


def _seed_user_with_creds(db: Session, *, base_url: str) -> User:
    seed_users_from_settings(
        db, Settings(SIKA_USERS="chris", SIKA_KALSHI_OWNER="chris")
    )
    db.commit()
    user = db.query(User).filter_by(username="chris").one()
    upsert_user_credentials(
        db, user_id=user.id, key_id="k1", private_key_pem=SAMPLE_PEM, base_url=base_url
    )
    db.commit()
    return user


def test_trade_client_honors_stored_prod_base_url(db_session: Session) -> None:
    user = _seed_user_with_creds(db_session, base_url=PROD_URL)
    client = build_trade_client_for_user(db_session, user.id)
    assert isinstance(client, KalshiTradeClient)
    assert client.base_url == PROD_URL.rstrip("/")


def test_trade_client_honors_stored_demo_base_url(db_session: Session) -> None:
    user = _seed_user_with_creds(db_session, base_url=DEMO_URL)
    client = build_trade_client_for_user(db_session, user.id)
    assert client is not None
    assert client.base_url == DEMO_URL.rstrip("/")


def test_demo_client_stays_pinned_to_sandbox_even_with_prod_row(db_session: Session) -> None:
    """The toy demo-order pipeline must never route live, no matter
    what environment the user picked for real trading."""
    user = _seed_user_with_creds(db_session, base_url=PROD_URL)
    demo = build_demo_client_for_user(db_session, user.id)
    assert isinstance(demo, KalshiDemoClient)
    assert demo.base_url == DEMO_URL.rstrip("/")


def test_trade_client_env_fallback_is_sandbox_or_nothing(db_session: Session) -> None:
    """Without a per-user row, the trade factory must never invent a
    live client from ambient env config. In tests KALSHI_KEY_ID is
    empty, so the conservative fallback is simply None."""
    assert build_trade_client_for_user(db_session, None) is None
