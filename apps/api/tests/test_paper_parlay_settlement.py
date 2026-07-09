"""Settlement tests for paper parlays (PAPER_PARLAY_SCOPE.md step 4).

``settle_paper_parlays`` rolls up the outcomes of each leg's source
``Prediction`` once they've all settled. Covers:

- All-won → outcome=won, realized_pnl = stake * (1/combined_price - 1)
- Any-loss → outcome=lost, realized_pnl = -stake (decision #1 formula)
- Any push/cancelled (no loss) → outcome=cancelled, realized_pnl = 0
- Any leg still pending → parlay stays pending, no row updated
- Missing source prediction → unresolved (soft state)
- Idempotency: re-running settlement on a settled parlay does NOT
  re-bump the ``updated`` counter (codex pattern 5 / bug #27)
- Idempotency: re-running on an already-unresolved parlay does NOT
  re-bump the counter

The realized_pnl formulas are DIFFERENT from parlay-prediction
settlement (which uses fractional pricing): paper parlays are
dollar-denominated per locked decision #1. Pinning the exact dollar
amounts here is the cross-scope check that the formula doesn't
silently drift back to the parlay-prediction convention.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import (
    Event,
    EventParticipant,
    Market,
    PaperParlay,
    PaperParlayLeg,
    Participant,
    Prediction,
)
from app.services.paper_parlays import (
    OUTCOME_CANCELLED,
    OUTCOME_LOST,
    OUTCOME_UNRESOLVED,
    OUTCOME_WON,
    settle_paper_parlays,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _add_event(db: Session, prefix: str) -> Event:
    home = Participant(external_id=f"{prefix}-h", sport_key="NBA", display_name="Home", short_name="HM", participant_type="team")
    away = Participant(external_id=f"{prefix}-a", sport_key="NBA", display_name="Away", short_name="AW", participant_type="team")
    db.add_all([home, away])
    db.flush()
    event = Event(external_id=f"{prefix}-event", sport_key="NBA", name=f"{prefix} event", status="scheduled", starts_at=_utcnow() + timedelta(hours=2))
    db.add(event)
    db.flush()
    db.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
    ])
    db.flush()
    return event


def _add_prediction_for_market(
    db: Session,
    *,
    market: Market,
    outcome: str,
    side: str = "yes",
    settlement_status: str = "settled",
) -> Prediction:
    pred = Prediction(
        event_id=market.event_id,
        market_id=market.id,
        ticker=market.ticker,
        market_title=market.title or market.ticker,
        side=side,
        action="buy",
        suggested_price=0.55,
        edge=0.05,
        confidence=0.80,
        rationale="settled fixture",
        prediction_outcome=outcome,
        settlement_status=settlement_status,
        settled_at=_utcnow() if settlement_status == "settled" else None,
        captured_at=_utcnow(),
    )
    db.add(pred)
    db.flush()
    return pred


def _add_parlay_with_legs(
    db: Session,
    *,
    stake: float = 100.0,
    combined_market_price: float = 0.25,
    legs: list[tuple[Market, Prediction | None]],
) -> PaperParlay:
    parlay = PaperParlay(
        stake=stake,
        leg_count=len(legs),
        sport_scope="NBA",
        participating_sports=["NBA"],
        combined_market_price=combined_market_price,
        combined_model_probability=0.40,
        american_odds="+300",
        edge=0.15,
    )
    parlay.legs = [
        PaperParlayLeg(
            leg_index=i,
            source_prediction_id=(pred.id if pred is not None else None),
            market_id=market.id,
            ticker=market.ticker,
            market_title=market.title or market.ticker,
            side="yes",
            suggested_price=0.50,
        )
        for i, (market, pred) in enumerate(legs)
    ]
    db.add(parlay)
    db.flush()
    db.refresh(parlay)
    return parlay


def _add_market(db: Session, *, event: Event, ticker: str) -> Market:
    market = Market(
        ticker=ticker,
        sport_key="NBA",
        event_id=event.id,
        title=f"market {ticker}",
        status="settled",
        raw_data={},
    )
    db.add(market)
    db.flush()
    return market


# -----------------------------------------------------------------------------
# Happy-path outcomes


def test_settle_paper_parlays_marks_all_won_with_dollar_payout(db_session: Session) -> None:
    """Decision #1 (dollar stake): on a win, realized_pnl =
    stake * (1/combined_market_price - 1). For a $100 stake on a
    parlay priced at 0.25 (combined), payout = 100 * (1/0.25 - 1) = $300."""
    event = _add_event(db_session, "won")
    market_a = _add_market(db_session, event=event, ticker="WON-A")
    market_b = _add_market(db_session, event=event, ticker="WON-B")
    pred_a = _add_prediction_for_market(db_session, market=market_a, outcome="won")
    pred_b = _add_prediction_for_market(db_session, market=market_b, outcome="won")
    parlay = _add_parlay_with_legs(
        db_session,
        stake=100.0,
        combined_market_price=0.25,
        legs=[(market_a, pred_a), (market_b, pred_b)],
    )
    db_session.commit()

    summary = settle_paper_parlays(db_session)
    db_session.commit()
    db_session.refresh(parlay)

    assert summary["processed"] == 1
    assert summary["won"] == 1
    assert summary["updated"] == 1
    assert parlay.outcome == OUTCOME_WON
    assert parlay.settlement_status == "settled"
    assert parlay.settled_at is not None
    assert parlay.realized_pnl == 300.0


def test_settle_paper_parlays_marks_any_loss_with_neg_stake(db_session: Session) -> None:
    """Any-loss → outcome=lost, realized_pnl = -stake (decision #1)."""
    event = _add_event(db_session, "lost")
    market_a = _add_market(db_session, event=event, ticker="LOST-A")
    market_b = _add_market(db_session, event=event, ticker="LOST-B")
    pred_a = _add_prediction_for_market(db_session, market=market_a, outcome="won")
    pred_b = _add_prediction_for_market(db_session, market=market_b, outcome="lost")
    parlay = _add_parlay_with_legs(
        db_session,
        stake=75.0,
        legs=[(market_a, pred_a), (market_b, pred_b)],
    )
    db_session.commit()

    settle_paper_parlays(db_session)
    db_session.commit()
    db_session.refresh(parlay)

    assert parlay.outcome == OUTCOME_LOST
    assert parlay.settlement_status == "settled"
    assert parlay.realized_pnl == -75.0


def test_settle_paper_parlays_marks_cancelled_on_push_or_cancel(db_session: Session) -> None:
    """Any push/cancelled (and no loss) → cancelled, realized_pnl=0.
    Sportsbook convention varies (some refund stake at the parlay's
    original odds minus the pushed leg); the conservative paper
    behavior is to cancel the whole parlay."""
    event = _add_event(db_session, "cxl")
    market_a = _add_market(db_session, event=event, ticker="CXL-A")
    market_b = _add_market(db_session, event=event, ticker="CXL-B")
    pred_a = _add_prediction_for_market(db_session, market=market_a, outcome="won")
    pred_b = _add_prediction_for_market(db_session, market=market_b, outcome="push")
    parlay = _add_parlay_with_legs(
        db_session,
        stake=50.0,
        legs=[(market_a, pred_a), (market_b, pred_b)],
    )
    db_session.commit()

    settle_paper_parlays(db_session)
    db_session.commit()
    db_session.refresh(parlay)

    assert parlay.outcome == OUTCOME_CANCELLED
    assert parlay.realized_pnl == 0.0


def test_settle_paper_parlays_waits_on_push_while_a_leg_is_pending(db_session: Session) -> None:
    """Regression: a push/cancel must NOT finalize the parlay while another
    leg is still pending. Otherwise the row goes terminally 'cancelled' and a
    leg that later loses can never flip it to lost — silently inflating P&L."""
    event = _add_event(db_session, "cxlpend")
    market_a = _add_market(db_session, event=event, ticker="CXLP-A")
    market_b = _add_market(db_session, event=event, ticker="CXLP-B")
    pred_a = _add_prediction_for_market(db_session, market=market_a, outcome="push")
    pred_b = _add_prediction_for_market(
        db_session, market=market_b, outcome="pending", settlement_status="pending"
    )
    parlay = _add_parlay_with_legs(
        db_session,
        stake=50.0,
        legs=[(market_a, pred_a), (market_b, pred_b)],
    )
    db_session.commit()

    summary = settle_paper_parlays(db_session)
    db_session.commit()
    db_session.refresh(parlay)

    # Still pending — waiting on leg B, NOT finalized as cancelled.
    assert summary["pending"] == 1
    assert parlay.outcome == "pending"
    assert parlay.settlement_status == "pending"

    # Leg B now loses → the whole parlay must settle LOST, not cancelled.
    pred_b.prediction_outcome = "lost"
    pred_b.settlement_status = "settled"
    db_session.commit()
    settle_paper_parlays(db_session)
    db_session.commit()
    db_session.refresh(parlay)

    assert parlay.outcome == OUTCOME_LOST
    assert parlay.realized_pnl == -50.0


def test_settle_paper_parlays_leaves_pending_when_a_leg_is_unsettled(
    db_session: Session,
) -> None:
    """Codex pattern 5 (reset edge cases): if one leg is still pending,
    the parlay stays pending and ``updated`` is NOT bumped."""
    event = _add_event(db_session, "wait")
    market_a = _add_market(db_session, event=event, ticker="WAIT-A")
    market_b = _add_market(db_session, event=event, ticker="WAIT-B")
    pred_a = _add_prediction_for_market(db_session, market=market_a, outcome="won")
    # market_b's prediction still pending
    pred_b = _add_prediction_for_market(
        db_session, market=market_b, outcome="pending", settlement_status="pending"
    )
    parlay = _add_parlay_with_legs(
        db_session,
        stake=25.0,
        legs=[(market_a, pred_a), (market_b, pred_b)],
    )
    db_session.commit()

    summary = settle_paper_parlays(db_session)
    db_session.commit()
    db_session.refresh(parlay)

    assert summary["pending"] == 1
    assert summary["updated"] == 0
    assert parlay.outcome == "pending"
    assert parlay.settlement_status == "pending"
    assert parlay.settled_at is None
    assert parlay.realized_pnl is None


# -----------------------------------------------------------------------------
# Unresolved + idempotency


def test_settle_paper_parlays_marks_unresolved_when_source_prediction_is_missing(
    db_session: Session,
) -> None:
    """Codex pattern 5: pruned/missing source prediction → unresolved."""
    event = _add_event(db_session, "noprov")
    market_a = _add_market(db_session, event=event, ticker="NOPROV-A")
    market_b = _add_market(db_session, event=event, ticker="NOPROV-B")
    pred_a = _add_prediction_for_market(db_session, market=market_a, outcome="won")
    # Leg B has NO source prediction (None).
    parlay = _add_parlay_with_legs(
        db_session,
        stake=10.0,
        legs=[(market_a, pred_a), (market_b, None)],
    )
    db_session.commit()

    summary = settle_paper_parlays(db_session)
    db_session.commit()
    db_session.refresh(parlay)

    assert summary["unresolved"] == 1
    assert summary["updated"] == 1
    assert parlay.outcome == OUTCOME_UNRESOLVED
    # Unresolved is SOFT — settlement_status stays pending so the next
    # cron tick re-checks if the source row reappears.
    assert parlay.settlement_status == "pending"
    assert parlay.settlement_notes is not None
    assert "missing" in parlay.settlement_notes.lower()


def test_settle_paper_parlays_does_not_re_bump_updated_on_idempotent_unresolved(
    db_session: Session,
) -> None:
    """Codex pattern 5 / bug #27 framing: a parlay already in
    ``unresolved`` from a prior pass should NOT re-bump the
    ``updated`` counter on every subsequent cron tick. The auto-
    generator has the same guard in
    parlays._settle_parlay_rows."""
    event = _add_event(db_session, "idem")
    market_a = _add_market(db_session, event=event, ticker="IDEM-A")
    market_b = _add_market(db_session, event=event, ticker="IDEM-B")
    pred_a = _add_prediction_for_market(db_session, market=market_a, outcome="won")
    parlay = _add_parlay_with_legs(
        db_session,
        stake=10.0,
        legs=[(market_a, pred_a), (market_b, None)],
    )
    db_session.commit()

    # First pass: marks unresolved, updated +1.
    first = settle_paper_parlays(db_session)
    db_session.commit()
    assert first["updated"] == 1

    # Second pass: state already matches → updated stays 0.
    second = settle_paper_parlays(db_session)
    db_session.commit()
    assert second["updated"] == 0
    assert second["unresolved"] == 1


def test_settle_paper_parlays_skips_already_settled_parlays(db_session: Session) -> None:
    """Settled parlays are excluded from the query so the cron doesn't
    re-process them indefinitely."""
    event = _add_event(db_session, "skip")
    market_a = _add_market(db_session, event=event, ticker="SKIP-A")
    market_b = _add_market(db_session, event=event, ticker="SKIP-B")
    pred_a = _add_prediction_for_market(db_session, market=market_a, outcome="won")
    pred_b = _add_prediction_for_market(db_session, market=market_b, outcome="won")
    parlay = _add_parlay_with_legs(
        db_session,
        stake=10.0,
        legs=[(market_a, pred_a), (market_b, pred_b)],
    )
    db_session.commit()

    # First pass settles it.
    settle_paper_parlays(db_session)
    db_session.commit()
    first_settled_at = parlay.settled_at
    assert parlay.settlement_status == "settled"

    # Second pass should NOT touch it.
    summary = settle_paper_parlays(db_session)
    db_session.commit()
    db_session.refresh(parlay)

    assert summary["processed"] == 0
    assert parlay.settled_at == first_settled_at
