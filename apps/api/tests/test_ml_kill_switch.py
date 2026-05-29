from datetime import datetime, timedelta, timezone

from app.models import ModelFamilyRuntimeHealth, Prediction, ShadowInference
from app.services.ml.kill_switch import ROLLING_SAMPLE_SIZE, evaluate_family


def _seed_shadow_pair(
    db_session,
    *,
    index: int,
    won: bool = False,
    shadow_probability: float = 0.9,
    model_version: str = "v1",
    calibration_version: str = "cal-v1",
    feature_set_version: str = "features-v1",
) -> None:
    captured_at = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=index)
    prediction = Prediction(
        run_id=1,
        event_id=None,
        market_id=index + 1,
        ticker=f"NBA-KILL-{index}",
        sport_key="NBA",
        event_name="Kill switch test game",
        market_title="Kill switch test market",
        market_family="winner",
        market_kind="game_winner",
        capture_scope="recommendation",
        side="yes",
        action="buy",
        suggested_price=0.5,
        fair_yes_price=0.1,
        fair_no_price=0.9,
        edge=-0.4,
        confidence=0.1,
        selection_score=0.1,
        model_name="heuristic-v1",
        rationale="Kill switch test",
        reasons=["test"],
        features={"family_key": "nba_singles"},
        scoring_diagnostics={},
        market_status_at_capture="active",
        settlement_status="settled",
        prediction_outcome="won" if won else "lost",
        settled_at=captured_at + timedelta(hours=3),
        realized_pnl=0.5 if won else -0.5,
        captured_at=captured_at,
    )
    db_session.add(prediction)
    db_session.flush()
    db_session.add(
        ShadowInference(
            run_id=1,
            source_prediction_id=prediction.id,
            event_id=prediction.event_id,
            market_id=prediction.market_id,
            ticker=prediction.ticker,
            sport_key=prediction.sport_key,
            event_name=prediction.event_name,
            market_title=prediction.market_title,
            market_family=prediction.market_family,
            market_kind=prediction.market_kind,
            inference_scope="single",
            recommended_side="yes",
            suggested_price=prediction.suggested_price,
            fair_yes_price=shadow_probability,
            fair_no_price=round(1 - shadow_probability, 4),
            edge=round(shadow_probability - prediction.suggested_price, 4),
            confidence=shadow_probability,
            model_name="shadow-model",
            model_version=model_version,
            calibration_version=calibration_version,
            feature_set_version=feature_set_version,
            model_metadata={"family_key": "nba_singles"},
            rationale="Shadow test",
            reasons=["shadow"],
            features={},
            captured_at=captured_at,
        )
    )


def _seed_bad_shadow_pair(db_session, *, index: int) -> None:
    _seed_shadow_pair(db_session, index=index, won=False, shadow_probability=0.9)


def test_kill_switch_demotes_when_runtime_unavailable_for_15_minutes(db_session):
    now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
    row = ModelFamilyRuntimeHealth(
        family_key="nba_singles",
        promotion_mode="ml",
        desired_mode="ml",
        effective_mode="ml",
        runtime_health="unavailable",
        last_error="predict failed",
        last_error_at=now - timedelta(minutes=16),
        promotion_baseline_brier=0.1,
    )
    db_session.add(row)
    db_session.flush()

    result = evaluate_family(db_session, "nba_singles", now=now)

    assert result.demoted is True
    assert result.reason == "runtime_unavailable"
    assert row.promotion_mode == "shadow"
    assert row.desired_mode == "shadow"
    assert row.effective_mode == "heuristic"


def test_kill_switch_demotes_when_rolling_brier_regresses(db_session):
    for index in range(ROLLING_SAMPLE_SIZE):
        _seed_bad_shadow_pair(db_session, index=index)
    row = ModelFamilyRuntimeHealth(
        family_key="nba_singles",
        promotion_mode="ml",
        desired_mode="ml",
        effective_mode="ml",
        runtime_health="healthy",
        promotion_baseline_brier=0.1,
    )
    db_session.add(row)
    db_session.flush()

    result = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 24, tzinfo=timezone.utc))

    assert result.demoted is True
    assert result.reason == "rolling_brier_regression"
    assert result.rolling_sample_count == ROLLING_SAMPLE_SIZE
    assert result.rolling_shadow_brier is not None
    assert result.rolling_shadow_brier > result.baseline_brier
    assert row.promotion_mode == "shadow"
    assert row.effective_mode == "shadow"


def test_kill_switch_rolling_brier_uses_current_lineage_only(db_session):
    for index in range(ROLLING_SAMPLE_SIZE):
        _seed_shadow_pair(
            db_session,
            index=index,
            won=False,
            shadow_probability=0.9,
            model_version="old",
            calibration_version="cal-old",
            feature_set_version="features-old",
        )
    for index in range(ROLLING_SAMPLE_SIZE, ROLLING_SAMPLE_SIZE * 2):
        won = index % 2 == 0
        _seed_shadow_pair(
            db_session,
            index=index,
            won=won,
            shadow_probability=0.85 if won else 0.15,
            model_version="current",
            calibration_version="cal-current",
            feature_set_version="features-current",
        )
    row = ModelFamilyRuntimeHealth(
        family_key="nba_singles",
        promotion_mode="ml",
        desired_mode="ml",
        effective_mode="ml",
        runtime_health="healthy",
        promotion_baseline_brier=0.1,
        model_name="shadow-model",
        model_version="current",
        calibration_version="cal-current",
        feature_set_version="features-current",
    )
    db_session.add(row)
    db_session.flush()

    result = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 24, tzinfo=timezone.utc))

    assert result.demoted is False
    assert result.rolling_sample_count == ROLLING_SAMPLE_SIZE
    assert result.rolling_shadow_brier is not None
    assert result.rolling_shadow_brier < result.baseline_brier
    assert row.promotion_mode == "ml"
