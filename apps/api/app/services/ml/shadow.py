from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import ParlayPrediction, Prediction, ShadowInference, ShadowParlayInference
from app.services.ml.runtime import run_shadow_inference
from app.services.ml.study_progress import is_active_study_family, retained_study_cutoff
from app.services.model_families import parlay_family_key, single_family_key

SHADOW_BACKFILL_MAX_PREDICTIONS = 250
SHADOW_BACKFILL_MAX_PARLAYS = 50


def _single_prediction_family_key(prediction: Prediction) -> str:
    return single_family_key(prediction.sport_key, prediction.market_family)


def _parlay_prediction_family_key(prediction: ParlayPrediction) -> str:
    return parlay_family_key(prediction.leg_count, prediction.participating_sports or [prediction.sport_scope])


def _shadow_single_exists(db: Session, prediction: Prediction) -> bool:
    linked = db.scalar(
        select(ShadowInference.id)
        .where(ShadowInference.source_prediction_id == prediction.id)
        .limit(1)
    )
    if linked is not None:
        return True
    if prediction.run_id is None:
        return False
    return db.scalar(
        select(ShadowInference.id)
        .where(
            ShadowInference.source_prediction_id.is_(None),
            ShadowInference.run_id == prediction.run_id,
            ShadowInference.market_id == prediction.market_id,
            ShadowInference.ticker == prediction.ticker,
            ShadowInference.inference_scope == "single",
        )
        .limit(1)
    ) is not None


def _shadow_parlay_exists(db: Session, parlay: ParlayPrediction) -> bool:
    linked = db.scalar(
        select(ShadowParlayInference.id)
        .where(ShadowParlayInference.source_parlay_prediction_id == parlay.id)
        .limit(1)
    )
    if linked is not None:
        return True
    if parlay.run_id is None:
        return False
    return db.scalar(
        select(ShadowParlayInference.id)
        .where(
            ShadowParlayInference.source_parlay_prediction_id.is_(None),
            ShadowParlayInference.run_id == parlay.run_id,
            ShadowParlayInference.leg_count == parlay.leg_count,
            ShadowParlayInference.leg_tickers == [leg.ticker for leg in parlay.legs],
        )
        .limit(1)
    ) is not None


def _shadow_metadata(result, decision, *, family_key: str) -> dict[str, object]:
    model_metadata = dict(result.lineage.model_metadata or {})
    model_metadata.update(
        {
            "family_key": family_key,
            "desired_mode": decision.desired_mode,
            "effective_mode": decision.effective_mode,
            "runtime_health": decision.runtime_health,
        }
    )
    return model_metadata


def prediction_requires_shadow_capture(
    db: Session,
    prediction: Prediction,
    *,
    active_study_only: bool = False,
) -> bool:
    if (prediction.capture_scope or "recommendation") == "coverage":
        return False
    family_key = _single_prediction_family_key(prediction)
    if active_study_only and not is_active_study_family(family_key):
        return False
    return not _shadow_single_exists(db, prediction)


def parlay_requires_shadow_capture(
    db: Session,
    parlay: ParlayPrediction,
    *,
    active_study_only: bool = False,
) -> bool:
    family_key = _parlay_prediction_family_key(parlay)
    if active_study_only and not is_active_study_family(family_key):
        return False
    return not _shadow_parlay_exists(db, parlay)


def _capture_prediction_shadow(db: Session, prediction: Prediction) -> bool:
    if not prediction_requires_shadow_capture(db, prediction):
        return False

    family_key = _single_prediction_family_key(prediction)
    result, decision = run_shadow_inference(db, family_key=family_key, scope="single")
    if result is None:
        return False

    recommended_side = prediction.side
    confidence = result.probability if recommended_side == "yes" else round(1 - result.probability, 4)
    fair_yes_price = round(result.probability, 4)
    fair_no_price = round(1 - result.probability, 4)
    edge = (fair_yes_price if recommended_side == "yes" else fair_no_price) - prediction.suggested_price
    db.add(
        ShadowInference(
            run_id=prediction.run_id,
            source_prediction_id=prediction.id,
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
            model_metadata=_shadow_metadata(result, decision, family_key=family_key),
            rationale=f"Shadow inference for {prediction.ticker}",
            reasons=list(prediction.reasons or []),
            features=dict(prediction.features or {}),
            captured_at=prediction.captured_at,
        )
    )
    return True


def _capture_parlay_shadow(db: Session, parlay: ParlayPrediction) -> bool:
    if not parlay_requires_shadow_capture(db, parlay):
        return False

    family_key = _parlay_prediction_family_key(parlay)
    result, decision = run_shadow_inference(db, family_key=family_key, scope="parlay")
    if result is None:
        return False

    db.add(
        ShadowParlayInference(
            run_id=parlay.run_id,
            source_parlay_prediction_id=parlay.id,
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
            model_metadata=_shadow_metadata(result, decision, family_key=family_key),
            rationale=f"Shadow inference for parlay {parlay.id}",
            features=dict(parlay.scoring_diagnostics or {}),
            captured_at=parlay.captured_at,
        )
    )
    return True


def _source_run_predictions(db: Session, *, source_run_id: int) -> list[Prediction]:
    return db.scalars(
        select(Prediction)
        .where(
            Prediction.run_id == source_run_id,
            or_(Prediction.capture_scope.is_(None), Prediction.capture_scope != "coverage"),
        )
        .order_by(Prediction.captured_at.asc(), Prediction.id.asc())
    ).all()


def _source_run_parlays(db: Session, *, source_run_id: int) -> list[ParlayPrediction]:
    return db.scalars(
        select(ParlayPrediction)
        .options(selectinload(ParlayPrediction.legs))
        .where(ParlayPrediction.run_id == source_run_id)
        .order_by(ParlayPrediction.captured_at.asc(), ParlayPrediction.id.asc())
    ).all()


def _current_run_predictions(db: Session, *, run_id: int) -> list[Prediction]:
    return _source_run_predictions(db, source_run_id=run_id)


def _current_run_parlays(db: Session, *, run_id: int) -> list[ParlayPrediction]:
    return _source_run_parlays(db, source_run_id=run_id)


def _backfill_predictions(db: Session) -> list[Prediction]:
    cutoff = retained_study_cutoff()
    selected_predictions: list[Prediction] = []
    rows = db.scalars(
        select(Prediction)
        .where(
            Prediction.captured_at >= cutoff,
            or_(Prediction.capture_scope.is_(None), Prediction.capture_scope != "coverage"),
        )
        .order_by(Prediction.captured_at.asc(), Prediction.id.asc())
    ).all()
    for prediction in rows:
        if not prediction_requires_shadow_capture(db, prediction, active_study_only=True):
            continue
        selected_predictions.append(prediction)
        if len(selected_predictions) >= SHADOW_BACKFILL_MAX_PREDICTIONS:
            break
    return selected_predictions


def _backfill_parlays(db: Session) -> list[ParlayPrediction]:
    cutoff = retained_study_cutoff()
    selected_parlays: list[ParlayPrediction] = []
    rows = db.scalars(
        select(ParlayPrediction)
        .options(selectinload(ParlayPrediction.legs))
        .where(ParlayPrediction.captured_at >= cutoff)
        .order_by(ParlayPrediction.captured_at.asc(), ParlayPrediction.id.asc())
    ).all()
    for parlay in rows:
        if not parlay_requires_shadow_capture(db, parlay, active_study_only=True):
            continue
        selected_parlays.append(parlay)
        if len(selected_parlays) >= SHADOW_BACKFILL_MAX_PARLAYS:
            break
    return selected_parlays


def capture_shadow_artifacts(
    db: Session,
    *,
    run_id: int,
    source_run_id: int | None = None,
    backfill: bool = False,
) -> tuple[int, int]:
    settings = get_settings()
    if settings.ml_serving_mode == "heuristic" or run_id is None:
        return 0, 0
    if source_run_id is not None and backfill:
        raise ValueError("Shadow capture cannot target a source run and backfill in the same pass.")

    if backfill:
        predictions = _backfill_predictions(db)
        parlays = _backfill_parlays(db)
    elif source_run_id is not None:
        predictions = _source_run_predictions(db, source_run_id=source_run_id)
        parlays = _source_run_parlays(db, source_run_id=source_run_id)
    else:
        predictions = _current_run_predictions(db, run_id=run_id)
        parlays = _current_run_parlays(db, run_id=run_id)

    prediction_count = 0
    for prediction in predictions:
        if _capture_prediction_shadow(db, prediction):
            prediction_count += 1

    parlay_prediction_count = 0
    for parlay in parlays:
        if _capture_parlay_shadow(db, parlay):
            parlay_prediction_count += 1

    db.flush()
    return prediction_count, parlay_prediction_count
