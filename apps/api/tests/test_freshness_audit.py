"""Tests for Smarter #22 PR B prep — freshness audit service.

The audit answers the question the tuning playbook
(`SMARTER_22_TUNING_PLAYBOOK.md`) asks: for each feature group,
when stale → how did calibration compare to when fresh?

Auto-captures the signal an operator would otherwise have to journal
by hand. Every recommendation already persists
``scoring_diagnostics["freshness_stale_groups"]`` (Smarter #22 PR A,
sika#186) and ``scoring_diagnostics["feature_groups"]``
(Architecture #5, sika#169). The settlement pipeline gives us
``prediction_outcome`` for each. This service joins them.

Output per group:
- count of stale-bucket predictions + fresh-bucket predictions
- avg predicted YES probability per bucket
- actual YES hit rate per bucket
- calibration miss per bucket (= |avg_predicted - hit_rate|)
- calibration delta = stale_miss - fresh_miss (positive ⇒ staleness hurts)

Operator reads the delta and decides whether to promote IGNORE → PENALIZE
in the policy registry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Event,
    EventParticipant,
    Market,
    Participant,
    Prediction,
    Run,
)
from app.schemas import FreshnessAuditRowRead
from app.services.ml.freshness_audit import compute_freshness_audit


def _seed_settled_prediction(
    db_session,
    *,
    side: str,
    outcome: str,
    fair_yes_price: float,
    settled_at: datetime,
    diagnostics: dict | None = None,
    market_index: int = 0,
):
    """Plant a settled Prediction with the given outcome + scoring
    diagnostics. Returns the Prediction row."""
    home = Participant(
        external_id=f"audit-home-{market_index}",
        sport_key="NBA",
        display_name="Home",
        short_name="Home",
        participant_type="team",
    )
    away = Participant(
        external_id=f"audit-away-{market_index}",
        sport_key="NBA",
        display_name="Away",
        short_name="Away",
        participant_type="team",
    )
    db_session.add_all([home, away])
    db_session.flush()

    event = Event(
        external_id=f"audit-event-{market_index}",
        sport_key="NBA",
        name="Audit test event",
        status="completed",
        starts_at=settled_at - timedelta(hours=3),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
    ])

    market = Market(
        ticker=f"KXAUDIT-{market_index}",
        sport_key="NBA",
        event_id=event.id,
        title="Audit market",
        status="settled",
        raw_data={"copilot_market_family": "winner"},
    )
    db_session.add(market)
    db_session.flush()

    run = Run(
        kind="scoring",
        status="completed",
        started_at=settled_at - timedelta(hours=4),
        finished_at=settled_at - timedelta(hours=3),
    )
    db_session.add(run)
    db_session.flush()

    prediction = Prediction(
        run_id=run.id,
        market_id=market.id,
        ticker=market.ticker,
        market_title=market.title,
        side=side,
        action="buy",
        suggested_price=fair_yes_price,
        fair_yes_price=fair_yes_price,
        fair_no_price=round(1.0 - fair_yes_price, 4),
        edge=0.05,
        confidence=0.7,
        invalidation="test",
        rationale="test",
        model_name="heuristic-v1",
        scoring_diagnostics=diagnostics or {},
        settlement_status="settled",
        prediction_outcome=outcome,
        settled_at=settled_at,
    )
    db_session.add(prediction)
    db_session.commit()
    return prediction


def _diagnostics(
    *,
    stale_groups: list[str] | None = None,
    fresh_groups: list[str] | None = None,
) -> dict:
    """Build a scoring_diagnostics dict matching the shape PR A
    persists (``freshness_stale_groups`` + ``feature_groups``)."""
    diag: dict = {}
    if stale_groups:
        diag["freshness_stale_groups"] = [
            {
                "group_key": key,
                "severity": "penalize",
                "age_seconds": 25200,
                "confidence_delta": -0.05,
            }
            for key in stale_groups
        ]
    feature_groups: dict = {}
    for key in stale_groups or []:
        feature_groups[key] = {
            "values": {"x": 1.0},
            "fresh_at": "2026-05-15T00:00:00+00:00",
            "source": f"load_{key}",
            "completeness": 1.0,
        }
    for key in fresh_groups or []:
        feature_groups[key] = {
            "values": {"x": 1.0},
            "fresh_at": "2026-05-16T20:00:00+00:00",
            "source": f"load_{key}",
            "completeness": 1.0,
        }
    if feature_groups:
        diag["feature_groups"] = feature_groups
    return diag


# -- compute_freshness_audit --------------------------------------------


def test_compute_freshness_audit_returns_empty_when_no_settled_predictions(db_session):
    """No settled predictions → no audit rows. The endpoint must
    return ``[]`` rather than raise so the readiness panel renders
    a clean empty state."""
    assert compute_freshness_audit(db_session) == []


def test_compute_freshness_audit_only_considers_settled_predictions(db_session):
    """A pending prediction with stale-group diagnostics must not
    contribute to the audit — we don't know its actual outcome yet."""
    settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    _seed_settled_prediction(
        db_session, side="yes", outcome="pending",
        fair_yes_price=0.7, settled_at=settled_at,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"]),
        market_index=0,
    )
    assert compute_freshness_audit(db_session) == []


def test_compute_freshness_audit_skips_push_outcomes(db_session):
    """Push outcomes can't inform calibration (no YES/NO winner)."""
    settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    _seed_settled_prediction(
        db_session, side="yes", outcome="push",
        fair_yes_price=0.6, settled_at=settled_at,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"]),
        market_index=0,
    )
    assert compute_freshness_audit(db_session) == []


def test_compute_freshness_audit_returns_row_per_group(db_session):
    """A group seen on at least one settled prediction (stale OR
    fresh) gets a row. The row carries both bucket counts even when
    one is zero."""
    settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    # Two settled predictions, both with mlb_weather emitted —
    # one stale, one fresh.
    _seed_settled_prediction(
        db_session, side="yes", outcome="won",
        fair_yes_price=0.7, settled_at=settled_at,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"]),
        market_index=0,
    )
    _seed_settled_prediction(
        db_session, side="yes", outcome="won",
        fair_yes_price=0.7, settled_at=settled_at,
        diagnostics=_diagnostics(fresh_groups=["mlb_weather"]),
        market_index=1,
    )
    rows = compute_freshness_audit(db_session)
    assert len(rows) == 1
    assert isinstance(rows[0], FreshnessAuditRowRead)
    assert rows[0].group_key == "mlb_weather"
    assert rows[0].stale_count == 1
    assert rows[0].fresh_count == 1


def test_compute_freshness_audit_calibration_delta_reflects_outcome_correlation(db_session):
    """The key signal: when stale-bucket calibration is worse than
    fresh-bucket calibration, calibration_delta is positive (staleness
    hurts). Two stale predictions (one won, one lost — 50% hit rate
    against a predicted 0.9 → miss of 0.40) vs two fresh predictions
    (both won — 100% hit rate against 0.9 → miss of 0.10). Delta =
    +0.30; staleness measurably degraded calibration."""
    settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    # Stale bucket: 1 won, 1 lost; predicted 0.9 both → hit rate 0.5,
    # avg predicted 0.9, miss = 0.40.
    _seed_settled_prediction(
        db_session, side="yes", outcome="won",
        fair_yes_price=0.9, settled_at=settled_at,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"]),
        market_index=0,
    )
    _seed_settled_prediction(
        db_session, side="yes", outcome="lost",
        fair_yes_price=0.9, settled_at=settled_at,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"]),
        market_index=1,
    )
    # Fresh bucket: 2 won; predicted 0.9 both → hit rate 1.0,
    # avg predicted 0.9, miss = 0.10.
    _seed_settled_prediction(
        db_session, side="yes", outcome="won",
        fair_yes_price=0.9, settled_at=settled_at,
        diagnostics=_diagnostics(fresh_groups=["mlb_weather"]),
        market_index=2,
    )
    _seed_settled_prediction(
        db_session, side="yes", outcome="won",
        fair_yes_price=0.9, settled_at=settled_at,
        diagnostics=_diagnostics(fresh_groups=["mlb_weather"]),
        market_index=3,
    )
    rows = compute_freshness_audit(db_session)
    weather = next(r for r in rows if r.group_key == "mlb_weather")
    assert weather.stale_count == 2
    assert weather.fresh_count == 2
    assert weather.stale_avg_predicted == pytest.approx(0.9, abs=1e-4)
    assert weather.fresh_avg_predicted == pytest.approx(0.9, abs=1e-4)
    assert weather.stale_hit_rate == pytest.approx(0.5, abs=1e-4)
    assert weather.fresh_hit_rate == pytest.approx(1.0, abs=1e-4)
    assert weather.stale_calibration_miss == pytest.approx(0.4, abs=1e-4)
    assert weather.fresh_calibration_miss == pytest.approx(0.1, abs=1e-4)
    # Positive delta → staleness hurt calibration; operator should
    # consider promoting this group to PENALIZE.
    assert weather.calibration_delta == pytest.approx(0.3, abs=1e-4)


def test_compute_freshness_audit_inverts_no_side_outcome_for_yes_calibration(db_session):
    """A NO-side prediction that wins means YES did NOT happen. The
    hit rate must be computed from the YES perspective so it matches
    the predicted probability (which is always P(YES))."""
    settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    # NO-side win → YES did not happen → outcome contributes 0 to YES hit rate.
    _seed_settled_prediction(
        db_session, side="no", outcome="won",
        fair_yes_price=0.7, settled_at=settled_at,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"]),
        market_index=0,
    )
    # NO-side loss → YES happened → outcome contributes 1 to YES hit rate.
    _seed_settled_prediction(
        db_session, side="no", outcome="lost",
        fair_yes_price=0.7, settled_at=settled_at,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"]),
        market_index=1,
    )
    rows = compute_freshness_audit(db_session)
    weather = next(r for r in rows if r.group_key == "mlb_weather")
    # 1 YES happened of 2 predictions → 0.5 hit rate.
    assert weather.stale_hit_rate == pytest.approx(0.5, abs=1e-4)


def test_compute_freshness_audit_window_filters_old_predictions(db_session):
    """The window arg limits the audit to recent settled predictions
    (default 30 days). Older predictions are excluded so a one-time
    pipeline change doesn't haunt the audit forever."""
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    ancient = datetime.now(timezone.utc) - timedelta(days=60)
    _seed_settled_prediction(
        db_session, side="yes", outcome="won",
        fair_yes_price=0.7, settled_at=recent,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"]),
        market_index=0,
    )
    _seed_settled_prediction(
        db_session, side="yes", outcome="lost",
        fair_yes_price=0.7, settled_at=ancient,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"]),
        market_index=1,
    )
    rows = compute_freshness_audit(db_session, window_days=30)
    weather = next(r for r in rows if r.group_key == "mlb_weather")
    # Only the recent (won) row counts.
    assert weather.stale_count == 1
    assert weather.stale_hit_rate == pytest.approx(1.0, abs=1e-4)


def test_compute_freshness_audit_emits_multiple_group_rows_sorted_by_delta(db_session):
    """When multiple groups appear in the audit, rows sort by
    calibration_delta descending so the most-actionable signals
    (biggest staleness penalty) are at the top of the operator's view."""
    settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    # Group A: big delta (stale bucket calibrates badly).
    _seed_settled_prediction(
        db_session, side="yes", outcome="lost",
        fair_yes_price=0.9, settled_at=settled_at,
        diagnostics=_diagnostics(stale_groups=["mlb_weather"], fresh_groups=["nba_workload"]),
        market_index=0,
    )
    _seed_settled_prediction(
        db_session, side="yes", outcome="won",
        fair_yes_price=0.9, settled_at=settled_at,
        diagnostics=_diagnostics(fresh_groups=["mlb_weather", "nba_workload"]),
        market_index=1,
    )
    rows = compute_freshness_audit(db_session)
    assert len(rows) >= 2
    deltas = [r.calibration_delta for r in rows]
    assert deltas == sorted(deltas, reverse=True), (
        f"Rows must be sorted by calibration_delta desc; got {deltas}"
    )


def test_compute_freshness_audit_skips_malformed_diagnostics_silently(db_session):
    """A row whose ``scoring_diagnostics`` is malformed (non-dict,
    or non-list ``freshness_stale_groups``) must not 500 the audit.
    The row is dropped from consideration; valid rows still count."""
    settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    # Malformed: freshness_stale_groups is a string, not a list.
    _seed_settled_prediction(
        db_session, side="yes", outcome="won",
        fair_yes_price=0.7, settled_at=settled_at,
        diagnostics={"freshness_stale_groups": "rofl"},
        market_index=0,
    )
    # Valid sibling.
    _seed_settled_prediction(
        db_session, side="yes", outcome="won",
        fair_yes_price=0.7, settled_at=settled_at,
        diagnostics=_diagnostics(fresh_groups=["mlb_weather"]),
        market_index=1,
    )
    rows = compute_freshness_audit(db_session)
    weather = next((r for r in rows if r.group_key == "mlb_weather"), None)
    assert weather is not None
    assert weather.fresh_count == 1
    assert weather.stale_count == 0
