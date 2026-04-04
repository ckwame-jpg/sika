from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ParlayPrediction, Prediction, ShadowInference, ShadowParlayInference
from app.services.ml.runtime import run_shadow_inference
from app.services.model_families import parlay_family_key, single_family_key


def capture_shadow_artifacts(
    db: Session,
    *,
    run_id: int | None,
    candidates: list[object],
) -> tuple[int, int]:
    del candidates
    settings = get_settings()
    if settings.ml_serving_mode == "heuristic" or run_id is None:
        return 0, 0

    prediction_count = 0
    parlay_prediction_count = 0

    predictions = db.scalars(select(Prediction).where(Prediction.run_id == run_id)).all()
    for prediction in predictions:
        family_key = single_family_key(prediction.sport_key, prediction.market_family)
        result, decision = run_shadow_inference(db, family_key=family_key, scope="single")
        if result is None:
            continue

        recommended_side = prediction.side
        confidence = result.probability if recommended_side == "yes" else round(1 - result.probability, 4)
        fair_yes_price = round(result.probability, 4)
        fair_no_price = round(1 - result.probability, 4)
        edge = (fair_yes_price if recommended_side == "yes" else fair_no_price) - prediction.suggested_price
        model_metadata = dict(result.lineage.model_metadata or {})
        model_metadata.update(
            {
                "family_key": family_key,
                "desired_mode": decision.desired_mode,
                "effective_mode": decision.effective_mode,
                "runtime_health": decision.runtime_health,
            }
        )
        db.add(
            ShadowInference(
                run_id=run_id,
                event_id=prediction.event_id,
                market_id=prediction.market_id,
                ticker=prediction.ticker,
                sport_key=prediction.sport_key,
                event_name=prediction.event_name,
                market_title=prediction.market_title,
                market_family=prediction.market_family,
                market_kind=prediction.market_kind,
                stat_key=prediction.stat_key,
                threshold=prediction.threshold,
                subject_name=prediction.subject_name,
                subject_team=prediction.subject_team,
                inference_scope="single",
                recommended_side=recommended_side,
                suggested_price=prediction.suggested_price,
                fair_yes_price=fair_yes_price,
                fair_no_price=fair_no_price,
                edge=round(edge, 4),
                confidence=round(confidence, 4),
                model_name=result.lineage.model_name,
                model_version=result.lineage.model_version,
                calibration_version=result.lineage.calibration_version,
                feature_set_version=result.lineage.feature_set_version,
                model_metadata=model_metadata,
                rationale=f"Shadow inference for {prediction.ticker}",
                reasons=list(prediction.reasons or []),
                features=dict(prediction.features or {}),
                captured_at=prediction.captured_at,
            )
        )
        prediction_count += 1

    parlays = db.scalars(select(ParlayPrediction).where(ParlayPrediction.run_id == run_id)).all()
    for parlay in parlays:
        family_key = parlay_family_key(parlay.leg_count, parlay.participating_sports or [parlay.sport_scope])
        result, decision = run_shadow_inference(db, family_key=family_key, scope="parlay")
        if result is None:
            continue

        model_metadata = dict(result.lineage.model_metadata or {})
        model_metadata.update(
            {
                "family_key": family_key,
                "desired_mode": decision.desired_mode,
                "effective_mode": decision.effective_mode,
                "runtime_health": decision.runtime_health,
            }
        )
        db.add(
            ShadowParlayInference(
                run_id=run_id,
                leg_count=parlay.leg_count,
                sport_scope=parlay.sport_scope,
                participating_sports=list(parlay.participating_sports or []),
                leg_tickers=[leg.ticker for leg in parlay.legs],
                combined_market_price=parlay.combined_market_price,
                combined_model_probability=round(result.probability, 4),
                edge=round(result.probability - parlay.combined_market_price, 4),
                confidence=round(result.confidence, 4),
                model_name=result.lineage.model_name,
                model_version=result.lineage.model_version,
                calibration_version=result.lineage.calibration_version,
                feature_set_version=result.lineage.feature_set_version,
                model_metadata=model_metadata,
                rationale=f"Shadow inference for parlay {parlay.id}",
                features=dict(parlay.scoring_diagnostics or {}),
                captured_at=parlay.captured_at,
            )
        )
        parlay_prediction_count += 1

    db.flush()
    return prediction_count, parlay_prediction_count
