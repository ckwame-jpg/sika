from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import RefreshJob, Run
from app.services.ingestion import advance_prop_refresh_job, run_refresh_cycle
from app.services.maintenance import prune_runtime_artifacts
from app.services.ml.shadow import capture_shadow_artifacts_batch


REFRESH_JOB_KINDS = frozenset({"refresh", "prop_refresh", "shadow_capture", "cleanup"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
STALE_REFRESH_JOB_ERROR = "stalled - reconciled automatically"


@dataclass(frozen=True, slots=True)
class RefreshJobSnapshot:
    job_id: int
    kind: str
    scope: str
    reason: str
    status: str
    run_id: int | None
    error_message: str | None
    details: dict[str, object]
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _snapshot(job: RefreshJob | None) -> RefreshJobSnapshot | None:
    if job is None:
        return None
    return RefreshJobSnapshot(
        job_id=job.id,
        kind=job.kind,
        scope=job.scope,
        reason=job.reason,
        status=job.status,
        run_id=job.run_id,
        error_message=job.error_message,
        details=dict(job.details or {}),
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_refresh_job(db: Session, job_id: int) -> RefreshJob | None:
    return db.get(RefreshJob, job_id)


def latest_job_for_kind(db: Session, kind: str) -> RefreshJob | None:
    return db.scalar(
        select(RefreshJob)
        .where(RefreshJob.kind == kind)
        .order_by(desc(RefreshJob.queued_at), desc(RefreshJob.id))
        .limit(1)
    )


def active_job_for_kind(db: Session, kind: str) -> RefreshJob | None:
    return db.scalar(
        select(RefreshJob)
        .where(RefreshJob.kind == kind, RefreshJob.status.in_(tuple(ACTIVE_JOB_STATUSES)))
        .order_by(RefreshJob.status.asc(), RefreshJob.queued_at.asc(), RefreshJob.id.asc())
        .limit(1)
    )


def reconcile_stale_jobs(db: Session, *, now: datetime | None = None) -> int:
    settings = get_settings()
    reference_now = now or datetime.now(timezone.utc)
    stale_jobs = []
    for job in db.scalars(
        select(RefreshJob).where(RefreshJob.status.in_(tuple(ACTIVE_JOB_STATUSES)))
    ).all():
        anchor = job.started_at if job.status == "running" and job.started_at is not None else job.queued_at
        age_minutes = (reference_now - _as_utc(anchor)).total_seconds() / 60
        if age_minutes > settings.refresh_job_stale_minutes:
            stale_jobs.append(job)
    for job in stale_jobs:
        job.status = "failed"
        job.error_message = STALE_REFRESH_JOB_ERROR
        job.finished_at = reference_now
    if stale_jobs:
        db.flush()
    return len(stale_jobs)


def enqueue_refresh_job(
    db: Session,
    *,
    kind: str,
    scope: str,
    reason: str,
    coalesce: bool = True,
) -> tuple[RefreshJob, bool]:
    if kind not in REFRESH_JOB_KINDS:
        raise ValueError(f"Unsupported refresh job kind: {kind}")

    reconcile_stale_jobs(db)

    if coalesce:
        existing = db.scalar(
            select(RefreshJob)
            .where(
                RefreshJob.kind == kind,
                RefreshJob.scope == scope,
                RefreshJob.status.in_(tuple(ACTIVE_JOB_STATUSES)),
            )
            .order_by(RefreshJob.queued_at.asc(), RefreshJob.id.asc())
            .limit(1)
        )
        if existing is not None:
            return existing, False

    job = RefreshJob(
        kind=kind,
        scope=scope,
        reason=reason,
        status="queued",
    )
    db.add(job)
    db.flush()
    return job, True


def enqueue_shadow_capture_job(
    db: Session,
    *,
    scope: str,
    source_run_id: int | None = None,
    source_refresh_job_id: int | None = None,
    source_prop_refresh_job_id: int | None = None,
) -> tuple[RefreshJob, bool]:
    if scope not in {"current_slate", "backfill"}:
        raise ValueError(f"Unsupported shadow capture scope: {scope}")
    if scope == "current_slate" and (source_run_id or 0) <= 0:
        raise ValueError("A source refresh run is required for current-slate shadow capture.")
    job, created = enqueue_refresh_job(
        db,
        kind="shadow_capture",
        scope=scope,
        reason="follow_up" if scope == "current_slate" else "maintenance_follow_up",
    )
    if created or job.status == "queued":
        details = dict(job.details or {})
        details["shadow_capture_scope"] = scope
        if source_run_id is not None:
            details["source_run_id"] = source_run_id
        else:
            details.pop("source_run_id", None)
        if source_refresh_job_id is not None:
            details["source_refresh_job_id"] = source_refresh_job_id
        elif scope == "backfill":
            details.pop("source_refresh_job_id", None)
        if source_prop_refresh_job_id is not None:
            details["source_prop_refresh_job_id"] = source_prop_refresh_job_id
        elif scope == "current_slate":
            details.pop("source_prop_refresh_job_id", None)
        job.details = details
        db.flush()
    return job, created


def _job_priority_order():
    return case(
        (
            (RefreshJob.kind == "refresh") & (RefreshJob.scope == "current_slate"),
            0,
        ),
        (
            RefreshJob.kind == "refresh",
            1,
        ),
        (
            RefreshJob.kind == "prop_refresh",
            2,
        ),
        (
            RefreshJob.kind == "shadow_capture",
            3,
        ),
        (
            RefreshJob.kind == "cleanup",
            4,
        ),
        else_=99,
    )


def _claim_next_job(db: Session) -> RefreshJob | None:
    running = db.scalar(
        select(RefreshJob)
        .where(RefreshJob.status == "running")
        .order_by(RefreshJob.started_at.asc().nullslast(), RefreshJob.id.asc())
        .limit(1)
    )
    if running is not None:
        return None

    queued = db.scalar(
        select(RefreshJob)
        .where(RefreshJob.status == "queued")
        .order_by(_job_priority_order(), RefreshJob.queued_at.asc(), RefreshJob.id.asc())
        .limit(1)
    )
    if queued is None:
        return None

    queued.status = "running"
    queued.started_at = datetime.now(timezone.utc)
    db.flush()
    return queued


def _requeue_job(job: RefreshJob) -> None:
    job.status = "queued"
    job.queued_at = datetime.now(timezone.utc)
    job.started_at = None
    job.finished_at = None
    job.error_message = None


def _ensure_shadow_capture_run(
    db: Session,
    *,
    job: RefreshJob,
) -> Run:
    if job.run_id is not None:
        existing = db.get(Run, job.run_id)
        if existing is not None:
            return existing
    source_run_id = int((job.details or {}).get("source_run_id") or 0) or None
    run_details = {"shadow_capture_scope": job.scope}
    if source_run_id is not None:
        run_details["source_run_id"] = source_run_id
    run = Run(kind="shadow_capture", status="running", details=run_details)
    db.add(run)
    db.flush()
    job.run_id = run.id
    return run


def process_refresh_job_queue_once() -> RefreshJobSnapshot | None:
    with SessionLocal() as db:
        reconciled = reconcile_stale_jobs(db)
        claimed = _claim_next_job(db)
        if claimed is None:
            if reconciled:
                db.commit()
            return None
        job_id = claimed.id
        db.commit()

    with SessionLocal() as db:
        job = db.get(RefreshJob, job_id)
        if job is None:
            return None
        try:
            if job.kind == "refresh":
                run = run_refresh_cycle(
                    db,
                    sports=["NBA", "MLB"] if job.scope == "current_slate" else None,
                    current_slate_only=(job.scope == "current_slate"),
                )
                job.run_id = run.id
                if job.scope == "current_slate":
                    shadow_job, _created = enqueue_shadow_capture_job(
                        db,
                        scope="current_slate",
                        source_run_id=run.id,
                        source_refresh_job_id=job.id,
                    )
                    details = dict(job.details or {})
                    details["shadow_follow_up_job_id"] = shadow_job.id
                    details["shadow_follow_up_scope"] = shadow_job.scope
                    job.details = details
            elif job.kind == "prop_refresh":
                run, completed = advance_prop_refresh_job(db, job=job)
                job.run_id = run.id
                if completed:
                    shadow_job, _created = enqueue_shadow_capture_job(
                        db,
                        scope="backfill",
                        source_prop_refresh_job_id=job.id,
                    )
                    details = dict(job.details or {})
                    details["shadow_backfill_job_id"] = shadow_job.id
                    details["shadow_backfill_scope"] = shadow_job.scope
                    job.details = details
                else:
                    _requeue_job(job)
                    db.commit()
                    db.refresh(job)
                    return _snapshot(job)
            elif job.kind == "shadow_capture":
                details = dict(job.details or {})
                run = _ensure_shadow_capture_run(db, job=job)
                source_run_id = int(details.get("source_run_id") or 0) or None
                phase = str(details.get("phase") or "predictions")
                cursor = dict(details.get("cursor") or {}) or None
                batch_started = datetime.now(timezone.utc)
                batch = capture_shadow_artifacts_batch(
                    db,
                    run_id=run.id,
                    source_run_id=source_run_id,
                    backfill=(job.scope == "backfill"),
                    phase=phase,
                    cursor=cursor,
                )
                job.run_id = run.id
                prediction_total = int(details.get("shadow_predictions_captured_total") or 0) + batch.prediction_count
                parlay_total = int(details.get("shadow_parlay_predictions_captured_total") or 0) + batch.parlay_prediction_count
                details.update(
                    {
                        "phase": batch.next_phase or phase,
                        "cursor": batch.next_cursor or {},
                        "shadow_capture_scope": job.scope,
                        "source_run_id": source_run_id,
                        "shadow_predictions_captured_total": prediction_total,
                        "shadow_parlay_predictions_captured_total": parlay_total,
                        "processed_so_far": prediction_total + parlay_total,
                        "batch_size": batch.prediction_count + batch.parlay_prediction_count,
                        "last_batch_seconds": max((datetime.now(timezone.utc) - batch_started).total_seconds(), 0.0),
                        "remaining_estimate": None,
                    }
                )
                job.details = details
                run.records_processed = prediction_total + parlay_total
                run.details = {
                    "shadow_capture_scope": job.scope,
                    "source_run_id": source_run_id,
                    "phase": batch.next_phase or phase,
                    "cursor": batch.next_cursor or {},
                    "shadow_predictions_captured": prediction_total,
                    "shadow_parlay_predictions_captured": parlay_total,
                    "refresh_scope": "shadow_capture",
                    "last_batch_seconds": details["last_batch_seconds"],
                }
                if batch.complete:
                    run.status = "completed"
                    run.finished_at = datetime.now(timezone.utc)
                    run.details = {
                        "shadow_capture_scope": job.scope,
                        "source_run_id": source_run_id,
                        "shadow_predictions_captured": prediction_total,
                        "shadow_parlay_predictions_captured": parlay_total,
                        "refresh_scope": "shadow_capture",
                    }
                else:
                    _requeue_job(job)
                    db.commit()
                    db.refresh(job)
                    return _snapshot(job)
            elif job.kind == "cleanup":
                job.details = prune_runtime_artifacts(db)
            else:  # pragma: no cover - guarded above
                raise ValueError(f"Unsupported refresh job kind: {job.kind}")

            job.status = "completed"
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(job)
            return _snapshot(job)
        except Exception as exc:
            if job.run_id is not None:
                run = db.get(Run, job.run_id)
                if run is not None and run.status == "running":
                    run.status = "failed"
                    run.error_message = str(exc).strip() or exc.__class__.__name__
                    run.finished_at = datetime.now(timezone.utc)
            job.status = "failed"
            job.error_message = str(exc).strip() or exc.__class__.__name__
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(job)
            return _snapshot(job)
