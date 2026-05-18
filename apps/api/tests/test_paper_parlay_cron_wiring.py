"""PAPER_PARLAY_SCOPE.md step 9 — settlement cron wiring tests.

Covers the new ``paper_parlays`` phase added to
``advance_settlement_job``:

- An empty queue still progresses through the phases cleanly
  (single_predictions → parlay_predictions → paper_parlays → completed)
- A pending PaperParlay with all legs WON gets settled inside the
  paper_parlays phase
- The completed run.details includes a ``paper_parlay_settlement_summary``
  field alongside the existing single/parlay summaries
- processed_so_far counts paper-parlay rows alongside the others
- The cron is backward-compatible with job rows queued before this PR
  (no ``paper_parlay_settlement_summary`` field) — they migrate
  through the new phase without error
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
    RefreshJob,
)
from app.services.refresh_jobs import advance_settlement_job


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_won_paper_parlay(db: Session) -> PaperParlay:
    home = Participant(external_id="cron-h", sport_key="NBA", display_name="Home", short_name="HM", participant_type="team")
    away = Participant(external_id="cron-a", sport_key="NBA", display_name="Away", short_name="AW", participant_type="team")
    db.add_all([home, away])
    db.flush()
    event = Event(external_id="cron-evt", sport_key="NBA", name="cron event", status="scheduled", starts_at=_utcnow() + timedelta(hours=1))
    db.add(event)
    db.flush()
    db.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
    ])
    db.flush()

    legs_with_predictions = []
    for index, ticker in enumerate(["CRON-A", "CRON-B"]):
        market = Market(
            ticker=ticker,
            sport_key="NBA",
            event_id=event.id,
            title=f"cron market {ticker}",
            status="settled",
            raw_data={},
        )
        db.add(market)
        db.flush()
        pred = Prediction(
            event_id=event.id,
            market_id=market.id,
            ticker=ticker,
            market_title=market.title,
            side="yes",
            action="buy",
            suggested_price=0.55,
            edge=0.05,
            confidence=0.8,
            rationale="settled fixture",
            prediction_outcome="won",
            settlement_status="settled",
            settled_at=_utcnow(),
            captured_at=_utcnow(),
        )
        db.add(pred)
        db.flush()
        legs_with_predictions.append((market, pred, index))

    parlay = PaperParlay(
        stake=50.0,
        leg_count=len(legs_with_predictions),
        sport_scope="NBA",
        participating_sports=["NBA"],
        combined_market_price=0.25,
        combined_model_probability=0.45,
        american_odds="+300",
        edge=0.20,
    )
    parlay.legs = [
        PaperParlayLeg(
            leg_index=index,
            source_prediction_id=pred.id,
            market_id=market.id,
            ticker=market.ticker,
            market_title=market.title,
            side="yes",
            suggested_price=0.5,
        )
        for market, pred, index in legs_with_predictions
    ]
    db.add(parlay)
    db.flush()
    return parlay


def _run_to_completion(db: Session, job: RefreshJob) -> RefreshJob:
    """Crank ``advance_settlement_job`` until the helper reports
    completed (mirrors how the worker thread drives a settlement
    job to completion in production)."""
    for _ in range(10):  # 10 iterations is a safety net; real flow takes 3.
        _, completed = advance_settlement_job(db, job=job)
        db.flush()
        if completed:
            break
    return job


def test_settlement_advances_through_paper_parlays_phase_on_empty_queue(
    db_session: Session,
) -> None:
    """Empty DB → single → parlay → paper-parlay → completed without
    error. Verifies the new phase is reachable end-to-end."""
    job = RefreshJob(kind="settlement", scope="predictions", reason="interval", status="queued")
    db_session.add(job)
    db_session.flush()
    _run_to_completion(db_session, job)
    assert job.details.get("phase") == "paper_parlays"
    assert "paper_parlay_settlement_summary" in job.details


def test_settlement_settles_pending_paper_parlay_inside_paper_parlays_phase(
    db_session: Session,
) -> None:
    """Codex pattern 2 (cross-component flow): a pending PaperParlay
    settled via the cron flow — same outcome as calling
    settle_paper_parlays directly, but exercised through the
    advance_settlement_job state machine."""
    parlay = _make_won_paper_parlay(db_session)
    job = RefreshJob(kind="settlement", scope="predictions", reason="interval", status="queued")
    db_session.add(job)
    db_session.flush()

    _run_to_completion(db_session, job)
    db_session.refresh(parlay)

    assert parlay.outcome == "won"
    assert parlay.settlement_status == "settled"
    # Payout = stake * (1/combined_market_price - 1) = 50 * (1/0.25 - 1) = 150
    assert parlay.realized_pnl == 150.0
    # The job's summary records the settled parlay.
    summary = job.details["paper_parlay_settlement_summary"]
    assert summary["won"] == 1
    assert summary["processed"] == 1


def test_settlement_completed_details_carries_paper_parlay_summary(
    db_session: Session,
) -> None:
    """Codex pattern 6 (implicit data shape): the final Run.details
    payload — what the ops/runs UI reads — must include the
    paper_parlay_settlement_summary alongside the existing
    single/parlay summaries."""
    _make_won_paper_parlay(db_session)
    job = RefreshJob(kind="settlement", scope="predictions", reason="interval", status="queued")
    db_session.add(job)
    db_session.flush()
    _run_to_completion(db_session, job)
    db_session.refresh(job)
    assert job.run is not None
    assert job.run.status == "completed"
    assert "paper_parlay_settlement_summary" in job.run.details
    # processed_so_far now includes the paper-parlay row.
    assert job.run.records_processed >= 1


def test_settlement_backward_compatible_with_legacy_job_missing_paper_parlay_summary(
    db_session: Session,
) -> None:
    """Codex pattern 8 (migration / legacy data compat): a job row
    queued BEFORE this PR landed won't have the new field in its
    details JSON. The phase must still progress through the new
    paper_parlays stage without raising."""
    job = RefreshJob(
        kind="settlement",
        scope="predictions",
        reason="interval",
        status="running",
        # Legacy: only single + parlay summaries, no paper_parlay.
        details={
            "phase": "parlay_predictions",
            "cursor": {},
            "single_settlement_summary": {
                "processed": 5,
                "updated": 5,
                "pending": 0,
                "won": 3,
                "lost": 2,
                "cancelled": 0,
                "unresolved": 0,
            },
            "parlay_settlement_summary": {
                "processed": 1,
                "updated": 1,
                "pending": 0,
                "won": 1,
                "lost": 0,
                "cancelled": 0,
                "unresolved": 0,
            },
        },
    )
    db_session.add(job)
    db_session.flush()
    _run_to_completion(db_session, job)
    # The legacy run-up still completes cleanly with the new field
    # appended.
    assert "paper_parlay_settlement_summary" in job.details
