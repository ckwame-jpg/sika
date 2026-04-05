from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import RefreshJob
from app.services.ingestion import run_prop_refresh_cycle, run_refresh_cycle
from app.services.maintenance import prune_runtime_artifacts
from app.services.research import run_research_cycle


REFRESH_JOB_KINDS = frozenset({"refresh", "prop_refresh", "cleanup", "research"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})


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
        .order_by(RefreshJob.queued_at.asc(), RefreshJob.id.asc())
        .limit(1)
    )
    if queued is None:
        return None

    queued.status = "running"
    queued.started_at = datetime.now(timezone.utc)
    db.flush()
    return queued


def process_refresh_job_queue_once() -> RefreshJobSnapshot | None:
    with SessionLocal() as db:
        claimed = _claim_next_job(db)
        if claimed is None:
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
            elif job.kind == "prop_refresh":
                run = run_prop_refresh_cycle(db)
                job.run_id = run.id
            elif job.kind == "cleanup":
                job.details = prune_runtime_artifacts(db)
            elif job.kind == "research":
                run = run_research_cycle(db)
                job.run_id = run.id
            else:  # pragma: no cover - guarded above
                raise ValueError(f"Unsupported refresh job kind: {job.kind}")

            job.status = "completed"
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(job)
            return _snapshot(job)
        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc).strip() or exc.__class__.__name__
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(job)
            return _snapshot(job)
