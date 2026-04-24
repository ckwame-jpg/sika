import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.models import ModelFamilyRuntimeHealth, Prediction, ShadowInference
from app.services.ml.promotion import PromotionMetrics, evaluate_family, evaluate_promotion_gates
from app.services.ml.runtime import resolve_family_runtime


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_nba_shadow_pair(
    db_session,
    *,
    index: int,
    won: bool,
    shadow_probability: float,
    heuristic_probability: float = 0.52,
) -> None:
    captured_at = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=index)
    prediction = Prediction(
        run_id=1,
        event_id=None,
        market_id=index + 1,
        ticker=f"NBA-PROMO-{index}",
        sport_key="NBA",
        event_name="Promotion test game",
        market_title="Promotion test market",
        market_family="winner",
        market_kind="game_winner",
        capture_scope="recommendation",
        side="yes",
        action="buy",
        suggested_price=0.5,
        fair_yes_price=heuristic_probability,
        fair_no_price=round(1 - heuristic_probability, 4),
        edge=round(heuristic_probability - 0.5, 4),
        confidence=heuristic_probability,
        selection_score=0.1,
        model_name="heuristic-v1",
        rationale="Promotion test",
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
            model_version="v1",
            calibration_version="cal-v1",
            feature_set_version="features-v1",
            model_metadata={"family_key": "nba_singles"},
            rationale="Shadow test",
            reasons=["shadow"],
            features={},
            captured_at=captured_at,
        )
    )


def _seed_promotion_ready_family(db_session, *, total: int = 150) -> None:
    for index in range(total):
        won = index % 2 == 0
        _seed_nba_shadow_pair(
            db_session,
            index=index,
            won=won,
            shadow_probability=0.85 if won else 0.15,
        )
    db_session.flush()


def _write_static_manifest(tmp_path, *, mode: str = "shadow"):
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "family_key": "nba_singles",
                "scope": "single",
                "behavior": "static_probability",
                "probability": 0.61,
                "confidence": 0.61,
                "metadata": {"source": "promotion-test"},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "promotion-test",
                "serving_mode": "ml",
                "families": [
                    {
                        "family_key": "nba_singles",
                        "model_name": "nba_singles-model",
                        "model_version": "v1",
                        "calibration_version": "cal-v1",
                        "feature_set_version": "features-v1",
                        "artifact_path": str(artifact_path),
                        "mode": mode,
                        "metadata": {"source": "manifest"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_promotion_gates_require_volume_calibration_ranking_and_stability():
    passing = PromotionMetrics(
        sample_count=150,
        heuristic_brier=0.24,
        shadow_brier=0.2,
        heuristic_top_decile_roi=0.01,
        shadow_top_decile_roi=0.08,
    )

    assert not evaluate_promotion_gates(passing, previous_stability_days=1).promoted
    assert evaluate_promotion_gates(passing, previous_stability_days=2).promoted
    assert not evaluate_promotion_gates(
        PromotionMetrics(149, 0.24, 0.2, 0.01, 0.08),
        previous_stability_days=2,
    ).volume_passed
    assert not evaluate_promotion_gates(
        PromotionMetrics(150, 0.24, 0.26, 0.01, 0.08),
        previous_stability_days=2,
    ).calibration_passed
    assert not evaluate_promotion_gates(
        PromotionMetrics(150, 0.24, 0.2, 0.08, 0.01),
        previous_stability_days=2,
    ).ranking_passed


def test_evaluate_family_promotes_after_three_passing_daily_evaluations(db_session):
    _seed_promotion_ready_family(db_session)

    first = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 21, tzinfo=timezone.utc))
    second = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 22, tzinfo=timezone.utc))
    third = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 23, tzinfo=timezone.utc))

    assert first.gates.stability_days == 1
    assert second.gates.stability_days == 2
    assert third.gates.promoted is True
    runtime_row = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one()
    assert runtime_row.promotion_mode == "ml"
    assert runtime_row.promotion_baseline_brier == third.metrics.shadow_brier


def test_runtime_uses_promotion_mode_below_explicit_family_override(db_session, monkeypatch, tmp_path):
    _seed_promotion_ready_family(db_session)
    evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 21, tzinfo=timezone.utc))
    evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 22, tzinfo=timezone.utc))
    evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 23, tzinfo=timezone.utc))
    manifest_path = _write_static_manifest(tmp_path, mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.delenv("ML_FAMILY_MODES_JSON", raising=False)
    get_settings.cache_clear()

    promoted = resolve_family_runtime(db_session, "nba_singles", scope="single")
    assert promoted.desired_mode == "ml"
    assert promoted.effective_mode == "ml"

    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "shadow"}))
    get_settings.cache_clear()
    overridden = resolve_family_runtime(db_session, "nba_singles", scope="single")
    assert overridden.desired_mode == "shadow"
    assert overridden.effective_mode == "shadow"
