from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.clients.kalshi import KalshiLiveClient
from app.config import get_settings
from app.models import AutoTradeDecision, CurrentSlateSnapshot, Event, LiveOrder, Market, MarketSnapshot, Recommendation
from app.services import analyst_chat
from app.services.live_trading import run_auto_trade_strategy


class FakeLiveClient:
    def __init__(self, balance=1000):
        self.balance = balance
        self.orders = []

    def get_balance(self):
        return {"balance": self.balance}

    def list_positions(self, **_params):
        return []

    def list_orders(self, **_params):
        return []

    def list_fills(self, **_params):
        return []

    def create_order(self, **kwargs):
        self.orders.append(kwargs)
        return {
            "request": kwargs,
            "order": {
                "order_id": f"live_{len(self.orders)}",
                "client_order_id": kwargs["client_order_id"],
                "status": "executed",
            },
        }


class FakeResponsesApiResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture()
def mutable_settings():
    settings = get_settings()
    saved = {
        "sika_owner_admin_token": settings.sika_owner_admin_token,
        "auto_trading_enabled": settings.auto_trading_enabled,
        "auto_trading_daily_budget_cents": settings.auto_trading_daily_budget_cents,
        "auto_trading_max_orders_per_day": settings.auto_trading_max_orders_per_day,
        "auto_trading_market_scope": settings.auto_trading_market_scope,
        "auto_trading_allow_parlays": settings.auto_trading_allow_parlays,
        "watchlist_min_edge": settings.watchlist_min_edge,
        "watchlist_min_confidence": settings.watchlist_min_confidence,
        "openai_api_key": settings.openai_api_key,
    }
    try:
        yield settings
    finally:
        for key, value in saved.items():
            setattr(settings, key, value)


def _seed_candidate(db_session, *, now: datetime, family: str = "game_line", source_type: str | None = None):
    event = Event(
        external_id=f"event-{family}-{source_type or 'standalone'}",
        sport_key="NBA",
        name="BOS at NYK",
        status="scheduled",
        starts_at=now + timedelta(hours=3),
    )
    db_session.add(event)
    db_session.flush()
    market = Market(
        ticker=f"NBA-{family}-{source_type or 'A'}",
        sport_key="NBA",
        event_id=event.id,
        title="Test market",
        status="open",
        close_time=now + timedelta(hours=2),
        raw_data={
            "copilot_market_family": family,
            "copilot_market_kind": "spread" if family == "game_line" else "points",
            "copilot_source_type": source_type,
        },
    )
    db_session.add(market)
    db_session.flush()
    db_session.add(
        MarketSnapshot(
            market_id=market.id,
            captured_at=now,
            yes_bid=0.49,
            yes_ask=0.5,
            last_price=0.49,
        )
    )
    recommendation = Recommendation(
        event_id=event.id,
        market_id=market.id,
        side="yes",
        action="buy",
        status="active",
        suggested_price=0.5,
        edge=0.12,
        confidence=0.8,
        selection_score=0.9,
        invalidation="Event starts",
        rationale="Positive edge",
        scoring_diagnostics={
            "quality_tier": "high",
            "selected_side_probability": 0.62,
            "source_type": source_type,
        },
        captured_at=now,
    )
    db_session.add(recommendation)
    db_session.flush()
    return recommendation


def _seed_slate_snapshot(db_session, *, now: datetime, status: str = "fresh"):
    db_session.add(
        CurrentSlateSnapshot(
            scope="all",
            generated_at=now,
            payload={
                "events": [],
                "research_sports": [],
                "generated_at": now.isoformat(),
                "freshness_status": status,
                "event_count": 1,
                "candidate_market_count": 1,
                "scored_market_count": 1,
                "recommendation_count": 1,
                "coverage_prediction_count": 1,
                "blocking_reason": None,
                "generated_from_run_id": None,
            },
        )
    )
    db_session.flush()


def test_live_signing_strips_query_params(monkeypatch, tmp_path: Path):
    key_path = tmp_path / "live.pem"
    key_path.write_text("unused")
    captured = {}

    class FakeKey:
        def sign(self, payload, *_args):
            captured["payload"] = payload
            return b"signature"

    client = KalshiLiveClient(key_id="abc", private_key_path=key_path, base_url="https://api.elections.kalshi.com/trade-api/v2")
    monkeypatch.setattr(client, "_load_private_key", lambda: FakeKey())

    signature = client.sign_request("GET", "/portfolio/orders?status=open", "1711814400000")

    assert signature
    assert captured["payload"] == b"1711814400000GET/portfolio/orders"


def test_live_create_order_payload_uses_no_resting_buy_max_cost(tmp_path: Path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "kalshi-live.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"order": {"order_id": "ord_1", "client_order_id": captured["json"]["client_order_id"], "status": "executed"}}

    class HttpClient:
        def request(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = kwargs["json"]
            return Response()

    client = KalshiLiveClient(
        key_id="abc",
        private_key_path=key_path,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
        http_client=HttpClient(),
    )

    response = client.create_order(
        ticker="NBA-TEST",
        side="yes",
        action="buy",
        quantity=3,
        limit_price=0.55,
        time_in_force="fill_or_kill",
        client_order_id="client-1",
        buy_max_cost=165,
        cancel_order_on_pause=True,
        no_resting=True,
        price_format="cents",
    )

    payload = captured["json"]
    assert captured["method"] == "POST"
    assert payload["type"] == "limit"
    assert payload["yes_price"] == 55
    assert payload["buy_max_cost"] == 165
    assert payload["time_in_force"] == "fill_or_kill"
    assert payload["cancel_order_on_pause"] is True
    assert payload["post_only"] is False
    assert response["request"] == payload


def test_admin_token_required_for_live_endpoints(client, mutable_settings):
    mutable_settings.sika_owner_admin_token = "secret-token"

    missing = client.get("/ops/auto-trading/status")
    wrong = client.get("/ops/auto-trading/status", headers={"X-Sika-Admin-Token": "wrong"})
    ok = client.get("/ops/auto-trading/status", headers={"X-Sika-Admin-Token": "secret-token"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["daily_budget_cents"] == mutable_settings.auto_trading_daily_budget_cents


def test_auto_trade_run_is_budget_capped_and_idempotent(db_session, mutable_settings):
    now = datetime.now(timezone.utc)
    mutable_settings.auto_trading_enabled = True
    mutable_settings.auto_trading_daily_budget_cents = 1000
    mutable_settings.auto_trading_max_orders_per_day = 5
    mutable_settings.auto_trading_market_scope = "nba_mlb_current_slate"
    mutable_settings.auto_trading_allow_parlays = False
    _seed_candidate(db_session, now=now)
    _seed_slate_snapshot(db_session, now=now)
    db_session.commit()
    fake = FakeLiveClient(balance=1000)

    first = run_auto_trade_strategy(db_session, requested_by="manual", client=fake, now=now)
    second = run_auto_trade_strategy(db_session, requested_by="manual", client=fake, now=now)

    assert first.id == second.id
    assert first.status == "completed"
    assert first.spent_cents == 500
    assert first.submitted_order_count == 1
    assert len(fake.orders) == 1
    assert db_session.query(LiveOrder).count() == 1
    assert db_session.query(AutoTradeDecision).filter_by(status="submitted").count() == 1


def test_auto_trade_skips_stale_slate_without_submitting(db_session, mutable_settings):
    now = datetime.now(timezone.utc)
    mutable_settings.auto_trading_enabled = True
    _seed_candidate(db_session, now=now)
    _seed_slate_snapshot(db_session, now=now, status="stale")
    db_session.commit()
    fake = FakeLiveClient(balance=1000)

    run = run_auto_trade_strategy(db_session, requested_by="manual", client=fake, now=now)

    assert run.status == "skipped"
    assert run.skipped_reason == "current_slate_stale"
    assert len(fake.orders) == 0
    assert db_session.query(LiveOrder).count() == 0


def test_auto_trade_excludes_combo_derived_candidates(db_session, mutable_settings):
    now = datetime.now(timezone.utc)
    mutable_settings.auto_trading_enabled = True
    mutable_settings.auto_trading_allow_parlays = False
    _seed_candidate(db_session, now=now, source_type="combo_derived")
    _seed_slate_snapshot(db_session, now=now)
    db_session.commit()
    fake = FakeLiveClient(balance=1000)

    run = run_auto_trade_strategy(db_session, requested_by="manual", client=fake, now=now)

    assert run.status == "skipped"
    assert run.skipped_reason == "no_eligible_candidates"
    assert len(fake.orders) == 0
    decision = db_session.query(AutoTradeDecision).one()
    assert decision.skip_reason == "combo_derived_excluded"


def test_site_analyst_chat_refuses_trade_placement(client, mutable_settings):
    mutable_settings.sika_owner_admin_token = "secret-token"

    response = client.post(
        "/ops/chat/analyst",
        headers={"X-Sika-Admin-Token": "secret-token"},
        json={"message": "place a trade for $10"},
    )

    assert response.status_code == 200
    assert "cannot create, cancel, or modify orders" in response.json()["message"]
    assert response.json()["citations"] == []
    assert response.json()["used_web_search"] is False
    assert response.json()["mode"] == "internal_only"


def test_site_research_requires_valid_admin_token(client, mutable_settings):
    mutable_settings.sika_owner_admin_token = "secret-token"

    missing = client.post("/ops/research/query", json={"message": "summarize my portfolio"})
    wrong = client.post(
        "/ops/research/query",
        headers={"X-Sika-Admin-Token": "wrong"},
        json={"message": "summarize my portfolio"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_site_research_requires_openai_api_key(client, mutable_settings):
    mutable_settings.sika_owner_admin_token = "secret-token"
    mutable_settings.openai_api_key = ""

    response = client.post(
        "/ops/research/query",
        headers={"X-Sika-Admin-Token": "secret-token"},
        json={"message": "summarize my portfolio"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "OPENAI_API_KEY is not configured"


def test_site_research_returns_internal_only_response(client, mutable_settings, monkeypatch):
    mutable_settings.sika_owner_admin_token = "secret-token"
    mutable_settings.openai_api_key = "test-key"
    calls: list[dict] = []

    def fake_post(url, *, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponsesApiResponse(
            {
                "output_text": "Portfolio risk is concentrated in one open position.",
                "output": [
                    {
                        "type": "message",
                        "content": [{"text": "Portfolio risk is concentrated in one open position."}],
                    }
                ],
            }
        )

    monkeypatch.setattr(analyst_chat.httpx, "post", fake_post)

    response = client.post(
        "/ops/research/query",
        headers={"X-Sika-Admin-Token": "secret-token"},
        json={"message": "summarize my portfolio risk", "include_web": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == "Portfolio risk is concentrated in one open position."
    assert payload["used_web_search"] is False
    assert payload["citations"] == []
    assert payload["mode"] == "internal_only"
    assert "portfolio" in payload["context"]
    assert "stats_query" not in payload["context"]
    assert "tools" not in calls[0]["json"]


def test_site_research_returns_web_citations(client, mutable_settings, monkeypatch):
    mutable_settings.sika_owner_admin_token = "secret-token"
    mutable_settings.openai_api_key = "test-key"

    def fake_post(_url, *, headers, json, timeout):
        assert headers["Authorization"] == "Bearer test-key"
        assert json["tools"] == [{"type": "web_search"}]
        return FakeResponsesApiResponse(
            {
                "output_text": "Latest injury reporting suggests the line moved on expected lineup news.",
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "sources": [
                                {
                                    "title": "ESPN injury report",
                                    "url": "https://example.com/injuries",
                                }
                            ]
                        },
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "text": "Latest injury reporting suggests the line moved on expected lineup news.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "ESPN injury report",
                                        "url": "https://example.com/injuries",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )

    monkeypatch.setattr(analyst_chat.httpx, "post", fake_post)

    response = client.post(
        "/ops/research/query",
        headers={"X-Sika-Admin-Token": "secret-token"},
        json={"message": "what injury news matters for tonight's board?"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["used_web_search"] is True
    assert payload["mode"] == "internal_plus_web"
    assert payload["citations"] == [
        {
            "title": "ESPN injury report",
            "url": "https://example.com/injuries",
        }
    ]


def test_site_research_retries_without_web_search(client, mutable_settings, monkeypatch):
    mutable_settings.sika_owner_admin_token = "secret-token"
    mutable_settings.openai_api_key = "test-key"
    calls: list[dict] = []

    def fake_post(_url, *, headers, json, timeout):
        calls.append({"headers": headers, "json": json, "timeout": timeout})
        if len(calls) == 1:
            raise httpx.ConnectError("web search unavailable")
        return FakeResponsesApiResponse(
            {
                "output_text": "Internal context fallback succeeded.",
                "output": [
                    {
                        "type": "message",
                        "content": [{"text": "Internal context fallback succeeded."}],
                    }
                ],
            }
        )

    monkeypatch.setattr(analyst_chat.httpx, "post", fake_post)

    response = client.post(
        "/ops/research/query",
        headers={"X-Sika-Admin-Token": "secret-token"},
        json={"message": "what changed on tonight's board?"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == "Internal context fallback succeeded."
    assert payload["used_web_search"] is False
    assert payload["mode"] == "internal_fallback"
    assert "tools" in calls[0]["json"]
    assert "tools" not in calls[1]["json"]


def test_site_research_includes_stats_query_context(client, mutable_settings, monkeypatch):
    mutable_settings.sika_owner_admin_token = "secret-token"
    mutable_settings.openai_api_key = "test-key"

    def fake_post(_url, *, headers, json, timeout):
        return FakeResponsesApiResponse(
            {
                "output_text": "Tatum has cleared that mark in four of his last five games.",
                "output": [
                    {
                        "type": "message",
                        "content": [{"text": "Tatum has cleared that mark in four of his last five games."}],
                    }
                ],
            }
        )

    def fake_stats_query(self, question, *, sport_key, season=None):
        return {
            "question": question,
            "sport_key": sport_key,
            "entity_name": "Jayson Tatum",
            "team_name": "BOS",
            "query_type": "player_recent",
            "season": season,
            "games_requested": 5,
            "games_analyzed": 5,
            "summary": {"games": 5, "metrics": {"points": 29.4}},
            "explanation": "Cleared in four of the last five.",
            "coverage_note": None,
            "source": "stats-service",
            "game_logs": [],
        }

    monkeypatch.setattr(analyst_chat.httpx, "post", fake_post)
    monkeypatch.setattr(analyst_chat.StatsQueryService, "query", fake_stats_query)

    response = client.post(
        "/ops/research/query",
        headers={"X-Sika-Admin-Token": "secret-token"},
        json={
            "message": "How has Jayson Tatum performed lately?",
            "sport_key": "NBA",
            "season": 2025,
            "include_web": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["context"]["stats_query"]["sport_key"] == "NBA"
    assert payload["context"]["stats_query"]["season"] == 2025
    assert payload["context"]["stats_query"]["entity_name"] == "Jayson Tatum"
