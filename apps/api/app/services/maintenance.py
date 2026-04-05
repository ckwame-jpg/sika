from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import engine, is_sqlite
from app.models import (
    EspnPlayerGamelogCache,
    EspnPlayerSearchCache,
    MarketSnapshot,
    ParlayPrediction,
    ParlayPredictionLeg,
    ParlayRecommendation,
    Prediction,
    RefreshJob,
    Run,
    ShadowInference,
    ShadowParlayInference,
    SignalSnapshot,
)


TERMINAL_RUN_STATUSES = ("completed", "failed")


def _ids_for(session: Session, stmt) -> list[int]:
    return list(session.scalars(stmt))


def _delete_rows(session: Session, model, ids: list[int]) -> int:
    if not ids:
        return 0
    return int(
        session.query(model)
        .filter(model.id.in_(tuple(ids)))
        .delete(synchronize_session=False)
        or 0
    )


def prune_runtime_artifacts(db: Session) -> dict[str, int]:
    settings = get_settings()
    now = datetime.now(timezone.utc)

    market_snapshot_cutoff = now - timedelta(days=settings.market_snapshot_retention_days)
    signal_snapshot_cutoff = now - timedelta(days=settings.signal_snapshot_retention_days)
    shadow_cutoff = now - timedelta(days=settings.shadow_inference_retention_days)
    run_cutoff = now - timedelta(days=settings.run_retention_days)
    refresh_job_cutoff = now - timedelta(days=settings.refresh_job_retention_days)
    prediction_cutoff = now - timedelta(days=settings.prediction_retention_days)

    old_refresh_job_ids = _ids_for(
        db,
        select(RefreshJob.id).where(
            RefreshJob.finished_at.is_not(None),
            RefreshJob.finished_at < refresh_job_cutoff,
        ),
    )
    old_parlay_prediction_ids = _ids_for(
        db,
        select(ParlayPrediction.id).where(ParlayPrediction.captured_at < prediction_cutoff),
    )
    old_prediction_ids = _ids_for(
        db,
        select(Prediction.id).where(Prediction.captured_at < prediction_cutoff),
    )
    old_run_ids = _ids_for(
        db,
        select(Run.id).where(
            Run.started_at < run_cutoff,
            Run.status.in_(TERMINAL_RUN_STATUSES),
        ),
    )

    market_snapshots_deleted = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.captured_at < market_snapshot_cutoff)
        .delete(synchronize_session=False)
    )
    signal_snapshots_deleted = (
        db.query(SignalSnapshot)
        .filter(SignalSnapshot.captured_at < signal_snapshot_cutoff)
        .delete(synchronize_session=False)
    )
    shadow_inferences_deleted = (
        db.query(ShadowInference)
        .filter(ShadowInference.captured_at < shadow_cutoff)
        .delete(synchronize_session=False)
    )
    shadow_parlay_inferences_deleted = (
        db.query(ShadowParlayInference)
        .filter(ShadowParlayInference.captured_at < shadow_cutoff)
        .delete(synchronize_session=False)
    )
    refresh_jobs_deleted = _delete_rows(db, RefreshJob, old_refresh_job_ids)
    parlay_prediction_legs_deleted = (
        db.query(ParlayPredictionLeg)
        .filter(ParlayPredictionLeg.parlay_prediction_id.in_(tuple(old_parlay_prediction_ids)))
        .delete(synchronize_session=False)
        if old_parlay_prediction_ids
        else 0
    )
    parlay_predictions_deleted = _delete_rows(db, ParlayPrediction, old_parlay_prediction_ids)
    parlay_prediction_source_links_cleared = (
        db.query(ParlayPredictionLeg)
        .filter(ParlayPredictionLeg.source_prediction_id.in_(tuple(old_prediction_ids)))
        .update({ParlayPredictionLeg.source_prediction_id: None}, synchronize_session=False)
        if old_prediction_ids
        else 0
    )
    predictions_deleted = _delete_rows(db, Prediction, old_prediction_ids)
    player_search_cache_deleted = (
        db.query(EspnPlayerSearchCache)
        .filter(EspnPlayerSearchCache.expires_at < now)
        .delete(synchronize_session=False)
    )
    player_gamelog_cache_deleted = (
        db.query(EspnPlayerGamelogCache)
        .filter(EspnPlayerGamelogCache.expires_at < now)
        .delete(synchronize_session=False)
    )
    parlay_recommendation_run_links_cleared = (
        db.query(ParlayRecommendation)
        .filter(ParlayRecommendation.run_id.in_(tuple(old_run_ids)))
        .update({ParlayRecommendation.run_id: None}, synchronize_session=False)
        if old_run_ids
        else 0
    )
    runs_deleted = (
        db.query(Run)
        .filter(Run.id.in_(tuple(old_run_ids)))
        .delete(synchronize_session=False)
        if old_run_ids
        else 0
    )

    return {
        "market_snapshots_deleted": int(market_snapshots_deleted or 0),
        "signal_snapshots_deleted": int(signal_snapshots_deleted or 0),
        "shadow_inferences_deleted": int(shadow_inferences_deleted or 0),
        "shadow_parlay_inferences_deleted": int(shadow_parlay_inferences_deleted or 0),
        "refresh_jobs_deleted": int(refresh_jobs_deleted or 0),
        "parlay_prediction_legs_deleted": int(parlay_prediction_legs_deleted or 0),
        "parlay_predictions_deleted": int(parlay_predictions_deleted or 0),
        "parlay_prediction_source_links_cleared": int(parlay_prediction_source_links_cleared or 0),
        "predictions_deleted": int(predictions_deleted or 0),
        "player_search_cache_deleted": int(player_search_cache_deleted or 0),
        "player_gamelog_cache_deleted": int(player_gamelog_cache_deleted or 0),
        "parlay_recommendation_run_links_cleared": int(parlay_recommendation_run_links_cleared or 0),
        "runs_deleted": int(runs_deleted or 0),
    }


def vacuum_analyze_database() -> None:
    statements = ("VACUUM", "ANALYZE") if is_sqlite else ("VACUUM (ANALYZE)",)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)
