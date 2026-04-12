from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import engine, is_sqlite
from app.models import (
    CurrentSlateSnapshot,
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
from app.services.ml.study_progress import retained_study_cutoff


TERMINAL_RUN_STATUSES = ("completed", "failed")

# Slice 2: the snapshot store is append-only per scope. Retain the most
# recent N rows per scope so a new freshness regression is debuggable, but
# prune everything older than that to bound table growth. Small enough to
# keep the latest-row lookup cheap, large enough to see "yesterday's shape".
_CURRENT_SLATE_SNAPSHOT_KEEP_PER_SCOPE = 10


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
    shadow_cutoff = retained_study_cutoff(now=now, settings=settings)
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
    shadow_prediction_source_links_cleared = (
        db.query(ShadowInference)
        .filter(ShadowInference.source_prediction_id.in_(tuple(old_prediction_ids)))
        .update({ShadowInference.source_prediction_id: None}, synchronize_session=False)
        if old_prediction_ids
        else 0
    )
    shadow_parlay_source_links_cleared = (
        db.query(ShadowParlayInference)
        .filter(ShadowParlayInference.source_parlay_prediction_id.in_(tuple(old_parlay_prediction_ids)))
        .update({ShadowParlayInference.source_parlay_prediction_id: None}, synchronize_session=False)
        if old_parlay_prediction_ids
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

    current_slate_snapshots_deleted = _prune_current_slate_snapshots(db)

    return {
        "market_snapshots_deleted": int(market_snapshots_deleted or 0),
        "signal_snapshots_deleted": int(signal_snapshots_deleted or 0),
        "shadow_inferences_deleted": int(shadow_inferences_deleted or 0),
        "shadow_parlay_inferences_deleted": int(shadow_parlay_inferences_deleted or 0),
        "refresh_jobs_deleted": int(refresh_jobs_deleted or 0),
        "parlay_prediction_legs_deleted": int(parlay_prediction_legs_deleted or 0),
        "parlay_predictions_deleted": int(parlay_predictions_deleted or 0),
        "parlay_prediction_source_links_cleared": int(parlay_prediction_source_links_cleared or 0),
        "shadow_prediction_source_links_cleared": int(shadow_prediction_source_links_cleared or 0),
        "shadow_parlay_source_links_cleared": int(shadow_parlay_source_links_cleared or 0),
        "predictions_deleted": int(predictions_deleted or 0),
        "player_search_cache_deleted": int(player_search_cache_deleted or 0),
        "player_gamelog_cache_deleted": int(player_gamelog_cache_deleted or 0),
        "parlay_recommendation_run_links_cleared": int(parlay_recommendation_run_links_cleared or 0),
        "runs_deleted": int(runs_deleted or 0),
        "current_slate_snapshots_deleted": int(current_slate_snapshots_deleted or 0),
    }


def _prune_current_slate_snapshots(db: Session) -> int:
    """Keep the most recent ``_CURRENT_SLATE_SNAPSHOT_KEEP_PER_SCOPE`` rows
    per scope; delete everything older. Works on both Postgres and SQLite by
    computing the survivor set in Python rather than a windowed subquery —
    the table is tiny (one row per scope per refresh), so two round trips
    are cheaper than a dialect-specific CTE.
    """
    all_scopes = db.scalars(select(CurrentSlateSnapshot.scope).distinct()).all()
    survivor_ids: list[int] = []
    for scope in all_scopes:
        ids_to_keep = db.scalars(
            select(CurrentSlateSnapshot.id)
            .where(CurrentSlateSnapshot.scope == scope)
            .order_by(
                CurrentSlateSnapshot.generated_at.desc(),
                CurrentSlateSnapshot.id.desc(),
            )
            .limit(_CURRENT_SLATE_SNAPSHOT_KEEP_PER_SCOPE)
        ).all()
        survivor_ids.extend(int(x) for x in ids_to_keep)
    if not survivor_ids:
        return 0
    deleted = (
        db.query(CurrentSlateSnapshot)
        .filter(~CurrentSlateSnapshot.id.in_(tuple(survivor_ids)))
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


def vacuum_analyze_database() -> None:
    statements = ("VACUUM", "ANALYZE") if is_sqlite else ("VACUUM (ANALYZE)",)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)
