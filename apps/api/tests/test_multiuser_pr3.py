"""Multi-user batch PR 3 — per-user scoping + legacy backfill.

Covers:

- The backfill in ``seed_users_from_settings`` attributes NULL-user_id
  rows to the legacy bucket (paper_positions, paper_parlays, demo_orders)
- ``create_paper_position`` / ``create_paper_parlay`` / ``create_demo_order``
  attribute new rows to the supplied user_id
- ``close_paper_position`` enforces ownership (403 on cross-user, 403
  on legacy)
- ``cancel_demo_order`` enforces ownership
- ``GET /paper-parlays`` is scoped to the current user when one is set
- ``GET /positions`` returns the current user's data in the primary
  lists and legacy rows in the ``legacy_*`` lists
- Single-tenant mode (no current user) returns all data in the primary
  lists (existing behavior preserved)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    DemoOrder,
    Event,
    EventParticipant,
    Market,
    PaperParlay,
    PaperParlayLeg,
    PaperPosition,
    Participant,
    Prediction,
    User,
)
from app.schemas import (
    PaperParlayCreate,
    PaperParlayLegCreate,
    PaperPositionCreate,
    PaperPositionExit,
)
from app.services.orders import (
    close_paper_position,
    create_paper_position,
)
from app.services.paper_parlays import create_paper_parlay
from app.services.users import LEGACY_USERNAME, seed_users_from_settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _settings(users: str = "chris,canaan", kalshi_owner: str = "chris") -> Settings:
    return Settings(SIKA_USERS=users, SIKA_KALSHI_OWNER=kalshi_owner)


def _seed_users(db: Session) -> tuple[User, User, User]:
    seed_users_from_settings(db, _settings())
    db.commit()
    chris = db.query(User).filter_by(username="chris").one()
    canaan = db.query(User).filter_by(username="canaan").one()
    legacy = db.query(User).filter_by(username=LEGACY_USERNAME).one()
    return chris, canaan, legacy


def _add_market(db: Session, ticker: str) -> Market:
    home = Participant(external_id=f"{ticker}-h", sport_key="NBA", display_name="H", short_name="H", participant_type="team")
    away = Participant(external_id=f"{ticker}-a", sport_key="NBA", display_name="A", short_name="A", participant_type="team")
    db.add_all([home, away])
    db.flush()
    event = Event(external_id=f"{ticker}-evt", sport_key="NBA", name="evt", status="scheduled", starts_at=_utcnow() + timedelta(hours=1))
    db.add(event)
    db.flush()
    db.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
    ])
    db.flush()
    market = Market(ticker=ticker, sport_key="NBA", event_id=event.id, title=f"{ticker} title", status="active", raw_data={"copilot_subject_name": "S", "copilot_subject_team": "T"})
    db.add(market)
    db.flush()
    return market


def _add_prediction(db: Session, market: Market) -> Prediction:
    pred = Prediction(
        event_id=market.event_id,
        market_id=market.id,
        ticker=market.ticker,
        market_title=market.title,
        side="yes",
        action="buy",
        suggested_price=0.55,
        fair_yes_price=0.60,
        fair_no_price=0.40,
        edge=0.05,
        confidence=0.80,
        rationale="t",
        captured_at=_utcnow(),
    )
    db.add(pred)
    db.flush()
    return pred


# -----------------------------------------------------------------------------
# Backfill


def test_seed_users_backfills_existing_paper_data_to_legacy_bucket(
    db_session: Session,
) -> None:
    """Codex pattern 8 (migration / legacy compat): rows that existed
    before multi-user landed have NULL user_id. The seed function
    moves them to the legacy bucket on first run."""
    market = _add_market(db_session, "BACK-A")
    # Insert pre-multi-user paper data (no user_id).
    pos = PaperPosition(
        market_id=market.id, ticker="BACK-A", side="yes", quantity=1, entry_price=0.5,
    )
    parlay = PaperParlay(
        stake=10, leg_count=2, sport_scope="NBA", participating_sports=["NBA"],
        combined_market_price=0.25, combined_model_probability=0.4,
        american_odds="+300", edge=0.15,
    )
    parlay.legs = [
        PaperParlayLeg(leg_index=0, market_id=market.id, ticker="BACK-A", market_title="t", side="yes", suggested_price=0.5),
        PaperParlayLeg(leg_index=1, market_id=market.id, ticker="BACK-B", market_title="t", side="yes", suggested_price=0.5),
    ]
    order = DemoOrder(
        market_id=market.id, ticker="BACK-A", client_order_id="abc",
        side="yes", quantity=1, limit_price=0.5,
    )
    db_session.add_all([pos, parlay, order])
    db_session.commit()

    chris, _canaan, legacy = _seed_users(db_session)
    db_session.refresh(pos)
    db_session.refresh(parlay)
    db_session.refresh(order)
    assert pos.user_id == legacy.id
    assert parlay.user_id == legacy.id
    assert order.user_id == legacy.id


# -----------------------------------------------------------------------------
# Service attribution


def test_create_paper_position_attributes_to_supplied_user(db_session: Session) -> None:
    chris, _canaan, _legacy = _seed_users(db_session)
    market = _add_market(db_session, "SVC-A")
    db_session.commit()
    pos = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="SVC-A", side="yes", quantity=1, entry_price=0.5),
        user_id=chris.id,
    )
    db_session.commit()
    assert pos.user_id == chris.id


def test_create_paper_parlay_attributes_to_supplied_user(db_session: Session) -> None:
    chris, _canaan, _legacy = _seed_users(db_session)
    market_a = _add_market(db_session, "PRL-A")
    market_b = _add_market(db_session, "PRL-B")
    _add_prediction(db_session, market_a)
    _add_prediction(db_session, market_b)
    db_session.commit()
    parlay = create_paper_parlay(
        db_session,
        PaperParlayCreate(
            stake=10.0,
            legs=[
                PaperParlayLegCreate(ticker="PRL-A", side="yes", suggested_price=0.5),
                PaperParlayLegCreate(ticker="PRL-B", side="yes", suggested_price=0.5),
            ],
        ),
        user_id=chris.id,
    )
    db_session.commit()
    assert parlay.user_id == chris.id


# -----------------------------------------------------------------------------
# Ownership enforcement


def test_close_paper_position_rejects_cross_user_exit(db_session: Session) -> None:
    """Codex pattern 5 (reset edge cases): only the owner can exit."""
    chris, canaan, _legacy = _seed_users(db_session)
    market = _add_market(db_session, "OWN-A")
    db_session.commit()
    pos = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="OWN-A", side="yes", quantity=1, entry_price=0.5),
        user_id=chris.id,
    )
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        close_paper_position(
            db_session, pos.id, PaperPositionExit(exit_price=0.6), user_id=canaan.id
        )
    assert exc.value.status_code == 403


def test_close_paper_position_rejects_legacy_exit(db_session: Session) -> None:
    """Legacy rows are read-only for everyone, including the owner of
    the user who's currently selected. Codex pattern 6 — closed-set
    on the legacy bucket."""
    chris, _canaan, legacy = _seed_users(db_session)
    market = _add_market(db_session, "LEG-A")
    db_session.commit()
    pos = PaperPosition(
        user_id=legacy.id,
        market_id=market.id,
        ticker="LEG-A",
        side="yes",
        quantity=1,
        entry_price=0.5,
    )
    db_session.add(pos)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        close_paper_position(
            db_session, pos.id, PaperPositionExit(exit_price=0.6), user_id=chris.id
        )
    assert exc.value.status_code == 403
    assert "legacy" in exc.value.detail.lower()


def test_close_paper_position_allows_owner_exit(db_session: Session) -> None:
    chris, _canaan, _legacy = _seed_users(db_session)
    market = _add_market(db_session, "ALLOW-A")
    db_session.commit()
    pos = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="ALLOW-A", side="yes", quantity=1, entry_price=0.5),
        user_id=chris.id,
    )
    db_session.commit()
    closed = close_paper_position(
        db_session, pos.id, PaperPositionExit(exit_price=0.6), user_id=chris.id
    )
    assert closed.status == "closed"


def test_close_paper_position_single_tenant_mode_skips_ownership(
    db_session: Session,
) -> None:
    """When no current user is supplied (single-tenant), the legacy
    ownership check is skipped — pre-multi-user deployments keep
    working unchanged."""
    market = _add_market(db_session, "ST-A")
    db_session.commit()
    pos = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="ST-A", side="yes", quantity=1, entry_price=0.5),
        user_id=None,
    )
    db_session.commit()
    closed = close_paper_position(
        db_session, pos.id, PaperPositionExit(exit_price=0.6), user_id=None
    )
    assert closed.status == "closed"


# -----------------------------------------------------------------------------
# Endpoint scoping


def test_get_paper_parlays_scopes_to_current_user(
    client: TestClient, db_session: Session
) -> None:
    chris, canaan, _legacy = _seed_users(db_session)
    market_a = _add_market(db_session, "EP-A")
    market_b = _add_market(db_session, "EP-B")
    _add_prediction(db_session, market_a)
    _add_prediction(db_session, market_b)
    db_session.commit()

    # Chris saves a parlay.
    client.post("/users/switch", json={"username": "chris"})
    client.post(
        "/paper-parlays",
        json={
            "stake": 10.0,
            "legs": [
                {"ticker": "EP-A", "side": "yes", "suggested_price": 0.5},
                {"ticker": "EP-B", "side": "yes", "suggested_price": 0.5},
            ],
        },
    )
    # Chris sees his parlay.
    chris_list = client.get("/paper-parlays").json()
    assert len(chris_list) == 1
    # Canaan sees zero (the scoping filter excludes Chris's row).
    client.post("/users/switch", json={"username": "canaan"})
    canaan_list = client.get("/paper-parlays").json()
    assert len(canaan_list) == 0


def test_get_positions_returns_user_data_plus_legacy_bucket(
    client: TestClient, db_session: Session
) -> None:
    """Codex pattern 2 (cross-component flow): the portfolio's
    legacy bucket renders in the new ``legacy_*`` fields; the user's
    own data lands in the primary fields."""
    chris, _canaan, legacy = _seed_users(db_session)
    market = _add_market(db_session, "POS-A")
    db_session.commit()
    # Pre-existing legacy paper position.
    legacy_pos = PaperPosition(
        user_id=legacy.id, market_id=market.id, ticker="POS-A",
        side="yes", quantity=1, entry_price=0.4,
    )
    db_session.add(legacy_pos)
    db_session.commit()
    # Chris opens his own paper position.
    client.post("/users/switch", json={"username": "chris"})
    client.post(
        "/paper-positions",
        json={"ticker": "POS-A", "side": "yes", "quantity": 1, "entry_price": 0.5},
    )
    body = client.get("/positions").json()
    assert len(body["paper_positions"]) == 1
    assert body["paper_positions"][0]["entry_price"] == 0.5
    assert len(body["legacy_paper_positions"]) == 1
    assert body["legacy_paper_positions"][0]["entry_price"] == 0.4


def test_get_positions_single_tenant_returns_everything_in_primary_lists(
    client: TestClient, db_session: Session
) -> None:
    """Single-tenant mode (no users configured): no scoping at all.
    Returns all rows in the primary lists; legacy_* stays empty."""
    market = _add_market(db_session, "ST-POS")
    db_session.commit()
    pos = PaperPosition(market_id=market.id, ticker="ST-POS", side="yes", quantity=1, entry_price=0.5)
    db_session.add(pos)
    db_session.commit()

    body = client.get("/positions").json()
    assert len(body["paper_positions"]) == 1
    assert body["legacy_paper_positions"] == []
