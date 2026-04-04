from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MarketSnapshot, Run, ShadowInference, ShadowParlayInference, SignalSnapshot


def prune_runtime_artifacts(db: Session) -> dict[str, int]:
    settings = get_settings()
    now = datetime.now(timezone.utc)

    market_snapshot_cutoff = now - timedelta(days=settings.market_snapshot_retention_days)
    signal_snapshot_cutoff = now - timedelta(days=settings.signal_snapshot_retention_days)
    shadow_cutoff = now - timedelta(days=settings.shadow_inference_retention_days)
    run_cutoff = now - timedelta(days=settings.run_retention_days)

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
    runs_deleted = (
        db.query(Run)
        .filter(
            Run.started_at < run_cutoff,
            Run.status.in_(("completed", "failed")),
        )
        .delete(synchronize_session=False)
    )

    return {
        "market_snapshots_deleted": int(market_snapshots_deleted or 0),
        "signal_snapshots_deleted": int(signal_snapshots_deleted or 0),
        "shadow_inferences_deleted": int(shadow_inferences_deleted or 0),
        "shadow_parlay_inferences_deleted": int(shadow_parlay_inferences_deleted or 0),
        "runs_deleted": int(runs_deleted or 0),
    }
