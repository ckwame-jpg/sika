"""Service-layer tests for ``create_paper_parlay`` (PAPER_PARLAY_SCOPE.md step 2).

Covers:
- Happy-path persistence + relationship integrity
- Validation rejections (404 unknown ticker, 400 closed market,
  400 duplicate tickers, schema-level minimum leg count)
- Snapshot semantics: combined_market_price is the product of the
  OPERATOR-supplied entry prices, NOT current market state (decision #3)
- Joint probability: strict product when legs are uncorrelated;
  correlation lift when legs share a subject
- Sport-scope derivation (single-sport vs MIXED)
- Denormalized display fields (subject_name, market_title, etc.)

The correlation-math regression test below pins the lift behavior to a
specific numeric expectation — if either this service or the
auto-generator changes the formula, both implementations must update
together (the paper_parlays.py docstring calls this coupling out).
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import Event, EventParticipant, Market, Participant, Prediction
from app.schemas import PaperParlayCreate, PaperParlayLegCreate
from app.services.paper_parlays import create_paper_parlay


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _add_event(
    db: Session,
    *,
    prefix: str,
    sport_key: str = "NBA",
    home_name: str = "Cleveland Cavaliers",
    home_short: str = "CLE",
    away_name: str = "Detroit Pistons",
    away_short: str = "DET",
) -> Event:
    home = Participant(
        external_id=f"{prefix}-home",
        sport_key=sport_key,
        display_name=home_name,
        short_name=home_short,
        participant_type="team",
    )
    away = Participant(
        external_id=f"{prefix}-away",
        sport_key=sport_key,
        display_name=away_name,
        short_name=away_short,
        participant_type="team",
    )
    db.add_all([home, away])
    db.flush()
    event = Event(
        external_id=f"{prefix}-event",
        sport_key=sport_key,
        name=f"{away_name} at {home_name}",
        status="scheduled",
        starts_at=_utcnow() + timedelta(hours=2),
    )
    db.add(event)
    db.flush()
    db.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
        ]
    )
    db.flush()
    return event


def _add_market(
    db: Session,
    *,
    event: Event,
    ticker: str,
    subject_name: str,
    subject_team: str,
    status: str = "active",
    stat_key: str = "points",
    threshold: float = 25.0,
) -> Market:
    market = Market(
        ticker=ticker,
        sport_key=event.sport_key,
        event_id=event.id,
        title=f"{subject_name} {threshold}+ {stat_key}",
        status=status,
        raw_data={
            "copilot_subject_name": subject_name,
            "copilot_subject_team": subject_team,
            "copilot_stat_key": stat_key,
            "copilot_threshold": threshold,
            "copilot_market_kind": "player_prop",
        },
    )
    db.add(market)
    db.flush()
    return market


def _add_prediction(
    db: Session,
    *,
    market: Market,
    side: str = "yes",
    fair_yes_price: float | None = 0.62,
    fair_no_price: float | None = 0.38,
    suggested_price: float = 0.55,
) -> Prediction:
    prediction = Prediction(
        event_id=market.event_id,
        market_id=market.id,
        ticker=market.ticker,
        market_title=market.title or market.ticker,
        side=side,
        action="buy",
        suggested_price=suggested_price,
        fair_yes_price=fair_yes_price,
        fair_no_price=fair_no_price,
        edge=0.07,
        confidence=0.78,
        rationale="test",
        captured_at=_utcnow(),
    )
    db.add(prediction)
    db.flush()
    return prediction


def test_create_paper_parlay_persists_parlay_and_legs(db_session: Session) -> None:
    event = _add_event(db_session, prefix="happy")
    leg_a = _add_market(db_session, event=event, ticker="TEST-A", subject_name="Donovan Mitchell", subject_team="CLE")
    leg_b = _add_market(db_session, event=event, ticker="TEST-B", subject_name="Jalen Duren", subject_team="DET")
    _add_prediction(db_session, market=leg_a, fair_yes_price=0.60, fair_no_price=0.40)
    _add_prediction(db_session, market=leg_b, fair_yes_price=0.55, fair_no_price=0.45)

    payload = PaperParlayCreate(
        stake=100.0,
        notes="2-leg smoke",
        legs=[
            PaperParlayLegCreate(ticker="TEST-A", side="yes", suggested_price=0.50),
            PaperParlayLegCreate(ticker="TEST-B", side="yes", suggested_price=0.48),
        ],
    )
    parlay = create_paper_parlay(db_session, payload)
    db_session.commit()

    assert parlay.id is not None
    assert parlay.stake == 100.0
    assert parlay.leg_count == 2
    assert parlay.notes == "2-leg smoke"
    assert parlay.settlement_status == "pending"
    assert parlay.outcome == "pending"
    assert len(parlay.legs) == 2
    # Legs are returned sorted by leg_index per the model relationship.
    assert [leg.leg_index for leg in parlay.legs] == [0, 1]
    assert [leg.ticker for leg in parlay.legs] == ["TEST-A", "TEST-B"]


def test_create_paper_parlay_rejects_combined_price_underflow(db_session: Session) -> None:
    """The rounded product of long-shot legs can be exactly 0.0 (schema-legal
    input), which the win-settlement branch would divide by — a ZeroDivisionError
    that poisons the whole paper-parlay settlement pass. Reject it at creation."""
    event = _add_event(db_session, prefix="underflow")
    leg_a = _add_market(db_session, event=event, ticker="UF-A", subject_name="A", subject_team="X")
    leg_b = _add_market(db_session, event=event, ticker="UF-B", subject_name="B", subject_team="Y")
    _add_prediction(db_session, market=leg_a, fair_yes_price=0.5, fair_no_price=0.5)
    _add_prediction(db_session, market=leg_b, fair_yes_price=0.5, fair_no_price=0.5)

    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="UF-A", side="yes", suggested_price=0.0005),
            PaperParlayLegCreate(ticker="UF-B", side="yes", suggested_price=0.0005),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        create_paper_parlay(db_session, payload)
    assert exc.value.status_code == 400


def test_create_paper_parlay_rejects_unknown_ticker(db_session: Session) -> None:
    event = _add_event(db_session, prefix="unk")
    leg = _add_market(db_session, event=event, ticker="REAL", subject_name="A", subject_team="X")
    _add_prediction(db_session, market=leg)

    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="REAL", side="yes", suggested_price=0.5),
            PaperParlayLegCreate(ticker="DOES-NOT-EXIST", side="yes", suggested_price=0.5),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        create_paper_parlay(db_session, payload)
    assert exc.value.status_code == 404
    assert "DOES-NOT-EXIST" in exc.value.detail


def test_create_paper_parlay_rejects_closed_market(db_session: Session) -> None:
    """Codex pattern 6 (implicit data shape): can't wager on a market
    that's already closed; reject at save time rather than persist a
    parlay that will never settle correctly."""
    event = _add_event(db_session, prefix="closed")
    leg_open = _add_market(db_session, event=event, ticker="OPEN", subject_name="A", subject_team="X")
    leg_closed = _add_market(
        db_session, event=event, ticker="CLOSED", subject_name="B", subject_team="X", status="settled"
    )
    _add_prediction(db_session, market=leg_open)
    _add_prediction(db_session, market=leg_closed)

    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="OPEN", side="yes", suggested_price=0.5),
            PaperParlayLegCreate(ticker="CLOSED", side="yes", suggested_price=0.5),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        create_paper_parlay(db_session, payload)
    assert exc.value.status_code == 400
    assert "not open" in exc.value.detail.lower()


def test_create_paper_parlay_rejects_duplicate_tickers(db_session: Session) -> None:
    event = _add_event(db_session, prefix="dup")
    leg = _add_market(db_session, event=event, ticker="DUP", subject_name="A", subject_team="X")
    _add_prediction(db_session, market=leg)

    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="DUP", side="yes", suggested_price=0.5),
            PaperParlayLegCreate(ticker="DUP", side="no", suggested_price=0.5),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        create_paper_parlay(db_session, payload)
    assert exc.value.status_code == 400
    assert "distinct" in exc.value.detail.lower()


def test_create_paper_parlay_schema_rejects_fewer_than_two_legs() -> None:
    """Pydantic-level guard so the service layer's MIN_LEG_COUNT check
    is defense-in-depth, not the only line of validation."""
    with pytest.raises(ValidationError):
        PaperParlayCreate(
            stake=10.0,
            legs=[PaperParlayLegCreate(ticker="ONLY", side="yes", suggested_price=0.5)],
        )


def test_create_paper_parlay_locks_combined_market_price_to_operator_snapshots(
    db_session: Session,
) -> None:
    """Decision #3 (operator snapshot): combined_market_price is the
    product of the OPERATOR-supplied entry prices, NOT the latest
    prediction.suggested_price. Even if the model has repriced
    between tray-add and save, the saved snapshot reflects what the
    operator saw."""
    event = _add_event(db_session, prefix="snap")
    market_a = _add_market(db_session, event=event, ticker="SNAP-A", subject_name="A", subject_team="X")
    market_b = _add_market(db_session, event=event, ticker="SNAP-B", subject_name="B", subject_team="Y")
    # Live prediction prices have moved (0.70, 0.65) but the operator's
    # tray still shows their original snapshots (0.50, 0.40).
    _add_prediction(db_session, market=market_a, suggested_price=0.70)
    _add_prediction(db_session, market=market_b, suggested_price=0.65)

    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="SNAP-A", side="yes", suggested_price=0.50),
            PaperParlayLegCreate(ticker="SNAP-B", side="yes", suggested_price=0.40),
        ],
    )
    parlay = create_paper_parlay(db_session, payload)
    # Combined market price = 0.50 * 0.40 = 0.20 (operator's view), NOT
    # 0.70 * 0.65 = 0.455 (current).
    assert parlay.combined_market_price == pytest.approx(0.20, abs=1e-9)
    assert [leg.suggested_price for leg in parlay.legs] == [0.50, 0.40]


def test_create_paper_parlay_joint_prob_is_independent_product_for_uncorrelated_legs(
    db_session: Session,
) -> None:
    """Sanity: two legs with different subjects/teams/opponents should
    produce the strict independent product (no correlation lift)."""
    # Two separate events so no shared opponent either.
    event_a = _add_event(db_session, prefix="ind-a", home_name="A Home", home_short="AH", away_name="A Away", away_short="AA")
    event_b = _add_event(db_session, prefix="ind-b", home_name="B Home", home_short="BH", away_name="B Away", away_short="BA")
    market_a = _add_market(db_session, event=event_a, ticker="IND-A", subject_name="Alpha Player", subject_team="AH")
    market_b = _add_market(db_session, event=event_b, ticker="IND-B", subject_name="Beta Player", subject_team="BH")
    _add_prediction(db_session, market=market_a, fair_yes_price=0.60, fair_no_price=0.40)
    _add_prediction(db_session, market=market_b, fair_yes_price=0.50, fair_no_price=0.50)

    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="IND-A", side="yes", suggested_price=0.50),
            PaperParlayLegCreate(ticker="IND-B", side="yes", suggested_price=0.45),
        ],
    )
    parlay = create_paper_parlay(db_session, payload)
    # Independent: 0.60 * 0.50 = 0.30. No correlation pairs → joint = 0.30.
    assert parlay.combined_model_probability == pytest.approx(0.30, abs=1e-6)


def test_create_paper_parlay_joint_prob_lifts_for_shared_subject(db_session: Session) -> None:
    """Two legs on the same subject (e.g. Donovan Mitchell points AND
    Donovan Mitchell assists) get the correlation lift. Codex pattern
    4 / 9 (cross-scope): if the auto-generator's formula moves and
    this helper drifts, this test fails — forcing both to update
    together."""
    event = _add_event(db_session, prefix="shared")
    market_a = _add_market(db_session, event=event, ticker="SHARE-A", subject_name="Same Player", subject_team="CLE", stat_key="points", threshold=25.0)
    market_b = _add_market(db_session, event=event, ticker="SHARE-B", subject_name="Same Player", subject_team="CLE", stat_key="assists", threshold=6.0)
    _add_prediction(db_session, market=market_a, fair_yes_price=0.60, fair_no_price=0.40)
    _add_prediction(db_session, market=market_b, fair_yes_price=0.50, fair_no_price=0.50)

    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="SHARE-A", side="yes", suggested_price=0.50),
            PaperParlayLegCreate(ticker="SHARE-B", side="yes", suggested_price=0.45),
        ],
    )
    parlay = create_paper_parlay(db_session, payload)
    # Independent: 0.60 * 0.50 = 0.30. One shared_subject pair (weight
    # 0.7) over 1 total pair → correlation_factor = 0.70.
    # min_leg = 0.50.  joint = 0.30 + 0.70 * (0.50 - 0.30) = 0.44.
    assert parlay.combined_model_probability == pytest.approx(0.44, abs=1e-6)
    # Sanity: strictly greater than the independent product.
    assert parlay.combined_model_probability > 0.30


def test_create_paper_parlay_sport_scope_is_mixed_for_multi_sport_legs(
    db_session: Session,
) -> None:
    event_nba = _add_event(db_session, prefix="mx-nba", sport_key="NBA")
    event_mlb = _add_event(db_session, prefix="mx-mlb", sport_key="MLB", home_name="A Home", home_short="AH", away_name="A Away", away_short="AA")
    market_nba = _add_market(db_session, event=event_nba, ticker="MIX-NBA", subject_name="NBA Player", subject_team="CLE")
    market_mlb = _add_market(db_session, event=event_mlb, ticker="MIX-MLB", subject_name="MLB Player", subject_team="NYY")
    _add_prediction(db_session, market=market_nba)
    _add_prediction(db_session, market=market_mlb)

    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="MIX-NBA", side="yes", suggested_price=0.5),
            PaperParlayLegCreate(ticker="MIX-MLB", side="yes", suggested_price=0.5),
        ],
    )
    parlay = create_paper_parlay(db_session, payload)
    assert parlay.sport_scope == "MIXED"
    assert sorted(parlay.participating_sports) == ["MLB", "NBA"]


def test_create_paper_parlay_denormalizes_display_fields(db_session: Session) -> None:
    """Codex pattern 8 (migration / display robustness): even if the
    market row is later pruned or the event name changes, the leg row
    keeps enough denormalized state to render correctly in the
    portfolio table."""
    event = _add_event(db_session, prefix="denorm", home_name="Detroit Pistons", home_short="DET")
    market = _add_market(
        db_session,
        event=event,
        ticker="DENORM-1",
        subject_name="Jalen Duren",
        subject_team="DET",
        stat_key="rebounds",
        threshold=10.0,
    )
    market_b = _add_market(db_session, event=event, ticker="DENORM-2", subject_name="Other Player", subject_team="DET", stat_key="points", threshold=12.0)
    _add_prediction(db_session, market=market)
    _add_prediction(db_session, market=market_b)

    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="DENORM-1", side="yes", suggested_price=0.5),
            PaperParlayLegCreate(ticker="DENORM-2", side="yes", suggested_price=0.5),
        ],
    )
    parlay = create_paper_parlay(db_session, payload)
    leg0 = parlay.legs[0]
    assert leg0.subject_name == "Jalen Duren"
    assert leg0.subject_team == "DET"
    assert leg0.stat_key == "rebounds"
    assert leg0.threshold == 10.0
    assert leg0.event_name == event.name
    assert leg0.market_title == market.title
    assert leg0.sport_key == "NBA"


def test_create_paper_parlay_rejects_when_no_model_probability_available(
    db_session: Session,
) -> None:
    """Codex pattern 6 (implicit data shape): if no Prediction exists
    for the chosen (market, side) AND no complement exists either,
    the joint prob can't be computed. Fail loudly rather than
    silently producing a degenerate parlay."""
    event = _add_event(db_session, prefix="noprob")
    market_a = _add_market(db_session, event=event, ticker="NOPROB-A", subject_name="A", subject_team="X")
    market_b = _add_market(db_session, event=event, ticker="NOPROB-B", subject_name="B", subject_team="Y")
    _add_prediction(db_session, market=market_a)
    # No prediction at all for market_b.
    payload = PaperParlayCreate(
        stake=10.0,
        legs=[
            PaperParlayLegCreate(ticker="NOPROB-A", side="yes", suggested_price=0.5),
            PaperParlayLegCreate(ticker="NOPROB-B", side="yes", suggested_price=0.5),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        create_paper_parlay(db_session, payload)
    assert exc.value.status_code == 400
    assert "model probability" in exc.value.detail.lower()
