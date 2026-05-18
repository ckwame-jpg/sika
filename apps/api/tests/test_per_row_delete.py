"""Per-row delete for paper data (multi-user follow-up).

Tests:
- delete_paper_position / delete_demo_order / delete_paper_parlay
  enforce the same ownership rules as exit/cancel:
   * 404 unknown id
   * 403 cross-user
   * 403 legacy bucket
   * owner allowed (open + closed)
   * single-tenant skips the check
- Cascade delete for parlay legs + demo fills
- Endpoint shape: DELETE returns ``{deleted: true}`` on success
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
from app.schemas import PaperParlayCreate, PaperParlayLegCreate, PaperPositionCreate
from app.services.orders import (
    create_paper_position,
    delete_demo_order,
    delete_paper_position,
)
from app.services.paper_parlays import create_paper_parlay, delete_paper_parlay
from app.services.users import seed_users_from_settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seed(db: Session) -> tuple[User, User, User]:
    seed_users_from_settings(
        db, Settings(SIKA_USERS="chris,canaan", SIKA_KALSHI_OWNER="chris")
    )
    db.commit()
    chris = db.query(User).filter_by(username="chris").one()
    canaan = db.query(User).filter_by(username="canaan").one()
    legacy = db.query(User).filter_by(username="legacy").one()
    return chris, canaan, legacy


def _add_market(db: Session, ticker: str) -> Market:
    h = Participant(external_id=f"{ticker}h", sport_key="NBA", display_name="H", short_name="H", participant_type="team")
    a = Participant(external_id=f"{ticker}a", sport_key="NBA", display_name="A", short_name="A", participant_type="team")
    db.add_all([h, a])
    db.flush()
    event = Event(external_id=f"{ticker}-evt", sport_key="NBA", name="e", status="scheduled", starts_at=_utcnow() + timedelta(hours=1))
    db.add(event)
    db.flush()
    db.add_all([
        EventParticipant(event_id=event.id, participant_id=h.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=a.id, role="away", is_home=False),
    ])
    db.flush()
    m = Market(ticker=ticker, sport_key="NBA", event_id=event.id, title=f"{ticker} t", status="active", raw_data={"copilot_subject_name": "S", "copilot_subject_team": "T"})
    db.add(m)
    db.flush()
    return m


def _add_prediction(db: Session, market: Market) -> Prediction:
    p = Prediction(
        event_id=market.event_id, market_id=market.id, ticker=market.ticker,
        market_title=market.title, side="yes", action="buy", suggested_price=0.5,
        fair_yes_price=0.6, fair_no_price=0.4, edge=0.05, confidence=0.8,
        rationale="t", captured_at=_utcnow(),
    )
    db.add(p)
    db.flush()
    return p


# -----------------------------------------------------------------------------
# Service helpers


def test_delete_paper_position_404_on_missing(db_session: Session) -> None:
    chris, _c, _l = _seed(db_session)
    with pytest.raises(HTTPException) as exc:
        delete_paper_position(db_session, 99999, user_id=chris.id)
    assert exc.value.status_code == 404


def test_delete_paper_position_owner_succeeds_on_open(db_session: Session) -> None:
    chris, _c, _l = _seed(db_session)
    market = _add_market(db_session, "DEL-OPEN")
    db_session.commit()
    pos = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="DEL-OPEN", side="yes", quantity=1, entry_price=0.5),
        user_id=chris.id,
    )
    db_session.commit()
    pid = pos.id
    delete_paper_position(db_session, pid, user_id=chris.id)
    db_session.commit()
    assert db_session.get(PaperPosition, pid) is None


def test_delete_paper_position_owner_succeeds_on_closed(db_session: Session) -> None:
    """Codex pattern 9 (cross-scope): closed positions are deletable
    too — operator wants to clean up old test data, not just live."""
    chris, _c, _l = _seed(db_session)
    market = _add_market(db_session, "DEL-CLOSED")
    db_session.commit()
    pos = PaperPosition(
        user_id=chris.id, market_id=market.id, ticker="DEL-CLOSED",
        side="yes", quantity=1, entry_price=0.5, exit_price=0.55,
        status="closed", pnl=0.05,
    )
    db_session.add(pos)
    db_session.commit()
    pid = pos.id
    delete_paper_position(db_session, pid, user_id=chris.id)
    db_session.commit()
    assert db_session.get(PaperPosition, pid) is None


def test_delete_paper_position_403_on_cross_user(db_session: Session) -> None:
    chris, canaan, _l = _seed(db_session)
    market = _add_market(db_session, "DEL-X")
    db_session.commit()
    pos = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="DEL-X", side="yes", quantity=1, entry_price=0.5),
        user_id=chris.id,
    )
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        delete_paper_position(db_session, pos.id, user_id=canaan.id)
    assert exc.value.status_code == 403


def test_delete_paper_position_403_on_legacy(db_session: Session) -> None:
    chris, _c, legacy = _seed(db_session)
    market = _add_market(db_session, "DEL-LEG")
    db_session.commit()
    pos = PaperPosition(
        user_id=legacy.id, market_id=market.id, ticker="DEL-LEG",
        side="yes", quantity=1, entry_price=0.5,
    )
    db_session.add(pos)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        delete_paper_position(db_session, pos.id, user_id=chris.id)
    assert exc.value.status_code == 403
    assert "legacy" in exc.value.detail.lower()


def test_delete_paper_position_single_tenant_mode_skips_ownership(
    db_session: Session,
) -> None:
    market = _add_market(db_session, "DEL-ST")
    db_session.commit()
    pos = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="DEL-ST", side="yes", quantity=1, entry_price=0.5),
        user_id=None,
    )
    db_session.commit()
    delete_paper_position(db_session, pos.id, user_id=None)
    db_session.commit()
    assert db_session.get(PaperPosition, pos.id) is None


def test_delete_paper_parlay_cascades_legs(db_session: Session) -> None:
    """Codex pattern 9: the legs relationship has cascade='all,
    delete-orphan' — deleting the parlay should also drop the legs
    so we don't leave orphan rows."""
    chris, _c, _l = _seed(db_session)
    ma = _add_market(db_session, "PRL-DEL-A")
    mb = _add_market(db_session, "PRL-DEL-B")
    _add_prediction(db_session, ma)
    _add_prediction(db_session, mb)
    db_session.commit()
    parlay = create_paper_parlay(
        db_session,
        PaperParlayCreate(
            stake=10,
            legs=[
                PaperParlayLegCreate(ticker="PRL-DEL-A", side="yes", suggested_price=0.5),
                PaperParlayLegCreate(ticker="PRL-DEL-B", side="yes", suggested_price=0.5),
            ],
        ),
        user_id=chris.id,
    )
    db_session.commit()
    parlay_id = parlay.id
    delete_paper_parlay(db_session, parlay_id, user_id=chris.id)
    db_session.commit()
    assert db_session.get(PaperParlay, parlay_id) is None
    # Cascade — no leftover legs.
    leftover = db_session.query(PaperParlayLeg).filter_by(paper_parlay_id=parlay_id).count()
    assert leftover == 0


def test_delete_paper_parlay_403_on_legacy(db_session: Session) -> None:
    chris, _c, legacy = _seed(db_session)
    parlay = PaperParlay(
        user_id=legacy.id, stake=10, leg_count=2, sport_scope="NBA",
        participating_sports=["NBA"], combined_market_price=0.25,
        combined_model_probability=0.4, american_odds="+300", edge=0.15,
    )
    db_session.add(parlay)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        delete_paper_parlay(db_session, parlay.id, user_id=chris.id)
    assert exc.value.status_code == 403


def test_delete_demo_order_403_on_cross_user(db_session: Session) -> None:
    chris, canaan, _l = _seed(db_session)
    market = _add_market(db_session, "DEMO-DEL")
    db_session.commit()
    order = DemoOrder(
        user_id=chris.id, market_id=market.id, ticker="DEMO-DEL",
        client_order_id="xyz", side="yes", quantity=1, limit_price=0.5,
    )
    db_session.add(order)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        delete_demo_order(db_session, order.id, user_id=canaan.id)
    assert exc.value.status_code == 403


# -----------------------------------------------------------------------------
# Endpoints


def test_delete_paper_position_endpoint_returns_deleted_true(
    client: TestClient, db_session: Session
) -> None:
    _seed(db_session)
    market = _add_market(db_session, "EP-DEL-POS")
    db_session.commit()
    client.post("/users/switch", json={"username": "chris"})
    pos = client.post(
        "/paper-positions",
        json={"ticker": "EP-DEL-POS", "side": "yes", "quantity": 1, "entry_price": 0.5},
    ).json()
    response = client.delete(f"/paper-positions/{pos['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    # Next GET shouldn't include the row.
    positions = client.get("/positions").json()
    assert len(positions["paper_positions"]) == 0


def test_delete_paper_parlay_endpoint(
    client: TestClient, db_session: Session
) -> None:
    _seed(db_session)
    ma = _add_market(db_session, "EP-DEL-A")
    mb = _add_market(db_session, "EP-DEL-B")
    _add_prediction(db_session, ma)
    _add_prediction(db_session, mb)
    db_session.commit()
    client.post("/users/switch", json={"username": "chris"})
    parlay = client.post(
        "/paper-parlays",
        json={
            "stake": 10.0,
            "legs": [
                {"ticker": "EP-DEL-A", "side": "yes", "suggested_price": 0.5},
                {"ticker": "EP-DEL-B", "side": "yes", "suggested_price": 0.5},
            ],
        },
    ).json()
    response = client.delete(f"/paper-parlays/{parlay['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    after = client.get("/paper-parlays").json()
    assert len(after) == 0


def test_delete_endpoints_404_on_missing_id(
    client: TestClient, db_session: Session
) -> None:
    _seed(db_session)
    client.post("/users/switch", json={"username": "chris"})
    assert client.delete("/paper-positions/99999").status_code == 404
    assert client.delete("/paper-parlays/99999").status_code == 404
    assert client.delete("/demo-orders/99999").status_code == 404
