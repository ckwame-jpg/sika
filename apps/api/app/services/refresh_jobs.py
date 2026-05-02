from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

import httpx
from sqlalchemy import case, desc, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import RefreshJob, Run
from app.services.ingestion import advance_current_slate_refresh_job, advance_prop_refresh_job, run_refresh_cycle
from app.services.maintenance import prune_runtime_artifacts
from app.services.ml.shadow import capture_shadow_artifacts_batch
from app.services.parlays import settle_parlay_predictions_batch
from app.services.predictions import settle_predictions_batch


REFRESH_JOB_KINDS = frozenset({"refresh", "prop_refresh", "shadow_capture", "settlement", "cleanup", "advanced_stats_warm"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
STALE_REFRESH_JOB_ERROR = "stalled - reconciled automatically"
WORKER_TIMEOUT_ERROR = "worker_timeout"
WORKER_TIMEOUT_GRACE_SECONDS = 10.0
CURRENT_SLATE_WORKER_TIMEOUT_SECONDS = 300.0
PROP_REFRESH_WORKER_TIMEOUT_SECONDS = 300.0
SETTLEMENT_WORKER_TIMEOUT_SECONDS = 120.0
PREDICTION_SETTLEMENT_BATCH_SIZE = 100
PARLAY_SETTLEMENT_BATCH_SIZE = 50
logger = logging.getLogger(__name__)
SETTLEMENT_SUMMARY_KEYS = ("processed", "updated", "won", "lost", "push", "cancelled", "pending", "unresolved", "errors")


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


def _worker_timeout_seconds(job: RefreshJob) -> float:
    settings = get_settings()
    default_timeout = max(float(settings.maintenance_claim_budget_seconds), 0.0) + WORKER_TIMEOUT_GRACE_SECONDS
    if job.kind == "refresh" and job.scope == "current_slate":
        return max(default_timeout, CURRENT_SLATE_WORKER_TIMEOUT_SECONDS)
    if job.kind == "prop_refresh":
        return max(default_timeout, PROP_REFRESH_WORKER_TIMEOUT_SECONDS)
    if job.kind == "settlement":
        return max(default_timeout, SETTLEMENT_WORKER_TIMEOUT_SECONDS)
    return default_timeout


def _fail_run(
    db: Session,
    run_id: int | None,
    error_message: str,
    *,
    finished_at: datetime | None = None,
    only_running: bool = True,
) -> bool:
    if run_id is None:
        return False
    criteria = [Run.id == run_id]
    if only_running:
        criteria.append(Run.status == "running")
    result = db.execute(
        update(Run)
        .where(*criteria)
        .values(
            status="failed",
            error_message=error_message,
            finished_at=finished_at or datetime.now(timezone.utc),
        )
    )
    return bool(result.rowcount)


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
        age_seconds = (reference_now - _as_utc(anchor)).total_seconds()
        timed_out = job.status == "running" and job.started_at is not None and age_seconds > _worker_timeout_seconds(job)
        stale = (age_seconds / 60) > settings.refresh_job_stale_minutes
        if timed_out or stale:
            job.error_message = WORKER_TIMEOUT_ERROR if timed_out else STALE_REFRESH_JOB_ERROR
            stale_jobs.append(job)
    for job in stale_jobs:
        job.status = "failed"
        job.finished_at = reference_now
        _fail_run(
            db,
            job.run_id,
            job.error_message or STALE_REFRESH_JOB_ERROR,
            finished_at=reference_now,
        )
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
            (RefreshJob.kind == "shadow_capture") & (RefreshJob.scope == "current_slate"),
            1,
        ),
        (
            RefreshJob.kind == "refresh",
            2,
        ),
        (
            RefreshJob.kind == "settlement",
            3,
        ),
        (
            RefreshJob.kind == "shadow_capture",
            4,
        ),
        (
            RefreshJob.kind == "prop_refresh",
            5,
        ),
        (
            RefreshJob.kind == "cleanup",
            6,
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


def _guarded_update_running_job(
    db: Session,
    job_id: int,
    values: dict[str, Any],
) -> bool:
    result = db.execute(
        update(RefreshJob)
        .where(RefreshJob.id == job_id, RefreshJob.status == "running")
        .values(**values)
    )
    return bool(result.rowcount)


def _guarded_requeue_job(db: Session, job_id: int) -> bool:
    now = datetime.now(timezone.utc)
    return _guarded_update_running_job(
        db,
        job_id,
        {
            "status": "queued",
            "queued_at": now,
            "started_at": None,
            "finished_at": None,
            "error_message": None,
        },
    )


def _guarded_complete_job(db: Session, job_id: int) -> bool:
    return _guarded_update_running_job(
        db,
        job_id,
        {
            "status": "completed",
            "finished_at": datetime.now(timezone.utc),
        },
    )


def _guarded_fail_job(db: Session, job_id: int, error_message: str) -> bool:
    return _guarded_update_running_job(
        db,
        job_id,
        {
            "status": "failed",
            "error_message": error_message,
            "finished_at": datetime.now(timezone.utc),
        },
    )


def _refresh_job_snapshot(db: Session, job_id: int) -> RefreshJobSnapshot | None:
    job = db.get(RefreshJob, job_id)
    if job is not None:
        db.refresh(job)
    return _snapshot(job)


def _current_slate_refresh_pending(db: Session) -> bool:
    return (
        db.scalar(
            select(RefreshJob.id)
            .where(
                RefreshJob.kind == "refresh",
                RefreshJob.scope == "current_slate",
                RefreshJob.status.in_(tuple(ACTIVE_JOB_STATUSES)),
            )
            .limit(1)
        )
        is not None
    )


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


def _empty_settlement_summary() -> dict[str, int]:
    return {key: 0 for key in SETTLEMENT_SUMMARY_KEYS}


def _coerce_settlement_summary(value: object) -> dict[str, int]:
    payload = dict(value or {}) if isinstance(value, dict) else {}
    summary = _empty_settlement_summary()
    for key in summary:
        summary[key] = int(payload.get(key) or 0)
    return summary


def _merge_settlement_summaries(*summaries: dict[str, int]) -> dict[str, int]:
    merged = _empty_settlement_summary()
    for summary in summaries:
        for key in merged:
            merged[key] += int(summary.get(key) or 0)
    return merged


def _settlement_processed_so_far(*, single_summary: dict[str, int], parlay_summary: dict[str, int]) -> int:
    return int(single_summary.get("processed") or 0) + int(parlay_summary.get("processed") or 0)


def _ensure_settlement_run(
    db: Session,
    *,
    job: RefreshJob,
) -> Run:
    if job.run_id is not None:
        existing = db.get(Run, job.run_id)
        if existing is not None:
            return existing
    run = Run(
        kind="settlement",
        status="running",
        details={"refresh_scope": "settlement", "phase": "single_predictions"},
    )
    db.add(run)
    db.flush()
    job.run_id = run.id
    return run


def advance_settlement_job(db: Session, *, job: RefreshJob) -> tuple[Run, bool]:
    details = dict(job.details or {})
    phase = str(details.get("phase") or "single_predictions")
    cursor = dict(details.get("cursor") or {}) or None
    single_summary = _coerce_settlement_summary(details.get("single_settlement_summary"))
    parlay_summary = _coerce_settlement_summary(details.get("parlay_settlement_summary"))
    run = _ensure_settlement_run(db, job=job)
    batch_started = perf_counter()
    batch_size = 0
    completed = False

    if phase == "single_predictions":
        batch_summary, next_cursor = settle_predictions_batch(
            db,
            latest_only_per_key=True,
            limit=PREDICTION_SETTLEMENT_BATCH_SIZE,
            cursor=cursor,
        )
        single_summary = _merge_settlement_summaries(single_summary, batch_summary)
        batch_size = PREDICTION_SETTLEMENT_BATCH_SIZE
        if next_cursor is None:
            phase = "parlay_predictions"
            cursor = None
        else:
            cursor = next_cursor
    elif phase == "parlay_predictions":
        batch_summary, next_cursor = settle_parlay_predictions_batch(
            db,
            limit=PARLAY_SETTLEMENT_BATCH_SIZE,
            cursor=cursor,
        )
        parlay_summary = _merge_settlement_summaries(parlay_summary, batch_summary)
        batch_size = PARLAY_SETTLEMENT_BATCH_SIZE
        if next_cursor is None:
            completed = True
            cursor = None
        else:
            cursor = next_cursor
    else:
        raise ValueError(f"Unsupported settlement phase: {phase}")

    processed_so_far = _settlement_processed_so_far(
        single_summary=single_summary,
        parlay_summary=parlay_summary,
    )
    details.update(
        {
            "phase": phase,
            "cursor": cursor or {},
            "single_settlement_summary": single_summary,
            "parlay_settlement_summary": parlay_summary,
            "processed_so_far": processed_so_far,
            "batch_size": batch_size,
            "last_batch_seconds": round(perf_counter() - batch_started, 3),
            "remaining_estimate": None,
        }
    )
    job.details = details
    run.records_processed = processed_so_far
    run.details = {
        "refresh_scope": "settlement",
        "phase": phase,
        "cursor": cursor or {},
        "single_settlement_summary": single_summary,
        "parlay_settlement_summary": parlay_summary,
        "processed_so_far": processed_so_far,
        "batch_size": batch_size,
        "last_batch_seconds": details["last_batch_seconds"],
    }
    if completed:
        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        run.details = {
            "refresh_scope": "settlement",
            "single_settlement_summary": single_summary,
            "parlay_settlement_summary": parlay_summary,
            "processed_so_far": processed_so_far,
        }
    db.flush()
    return run, completed


def _execute_job_in_thread(
    job_id: int,
    done_event: threading.Event,
    result_holder: dict[str, RefreshJobSnapshot | BaseException | None],
) -> None:
    try:
        result_holder["snapshot"] = _execute_claimed_job(job_id)
    except BaseException as exc:  # pragma: no cover - defensive thread boundary
        result_holder["exception"] = exc
    finally:
        done_event.set()


def process_refresh_job_queue_once() -> RefreshJobSnapshot | None:
    with SessionLocal() as db:
        reconciled = reconcile_stale_jobs(db)
        claimed = _claim_next_job(db)
        if claimed is None:
            if reconciled:
                db.commit()
            return None
        job_id = claimed.id
        timeout_seconds = _worker_timeout_seconds(claimed)
        db.commit()

    done_event = threading.Event()
    result_holder: dict[str, RefreshJobSnapshot | BaseException | None] = {
        "snapshot": None,
        "exception": None,
    }
    worker = threading.Thread(
        target=_execute_job_in_thread,
        args=(job_id, done_event, result_holder),
        daemon=True,
        name=f"refresh-worker-{job_id}",
    )
    worker.start()
    if done_event.wait(timeout=timeout_seconds):
        exception = result_holder.get("exception")
        if exception is not None:
            raise exception
        snapshot = result_holder.get("snapshot")
        return snapshot if isinstance(snapshot, RefreshJobSnapshot) else None

    with SessionLocal() as db:
        timed_out = _guarded_fail_job(db, job_id, WORKER_TIMEOUT_ERROR)
        snapshot = _refresh_job_snapshot(db, job_id)
        if timed_out and snapshot is not None:
            _fail_run(db, snapshot.run_id, WORKER_TIMEOUT_ERROR)
        db.commit()
    if timed_out:
        logger.warning(
            "refresh_job_worker_timeout",
            extra={"job_id": job_id, "worker_name": worker.name, "timeout_seconds": timeout_seconds},
        )
    return snapshot


def _execute_claimed_job(job_id: int) -> RefreshJobSnapshot | None:
    with SessionLocal() as db:
        job = db.get(RefreshJob, job_id)
        if job is None:
            return None
        try:
            if job.kind == "refresh":
                if job.scope == "current_slate":
                    run, completed = advance_current_slate_refresh_job(
                        db,
                        job=job,
                        sports=["NBA", "MLB"],
                    )
                    job.run_id = run.id
                    if not completed:
                        db.flush()
                        requeued = _guarded_requeue_job(db, job.id)
                        if not requeued:
                            logger.warning("refresh_job_late_requeue_ignored", extra={"job_id": job.id})
                            _fail_run(db, job.run_id, WORKER_TIMEOUT_ERROR, only_running=False)
                        db.commit()
                        return _refresh_job_snapshot(db, job.id)
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
                else:
                    run = run_refresh_cycle(
                        db,
                        sports=None,
                        current_slate_only=False,
                    )
                    job.run_id = run.id
            elif job.kind == "prop_refresh":
                settings = get_settings()
                claim_started = perf_counter()
                budget_seconds = max(float(settings.maintenance_claim_budget_seconds), 0.0)
                completed = False
                run = None
                while True:
                    run, completed = advance_prop_refresh_job(db, job=job)
                    job.run_id = run.id
                    if completed:
                        break
                    if _current_slate_refresh_pending(db):
                        break
                    if perf_counter() - claim_started >= budget_seconds:
                        break

                assert run is not None
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
                    db.flush()
                    requeued = _guarded_requeue_job(db, job.id)
                    if not requeued:
                        logger.warning("refresh_job_late_requeue_ignored", extra={"job_id": job.id})
                        _fail_run(db, job.run_id, WORKER_TIMEOUT_ERROR, only_running=False)
                    db.commit()
                    return _refresh_job_snapshot(db, job.id)
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
                    db.flush()
                    requeued = _guarded_requeue_job(db, job.id)
                    if not requeued:
                        logger.warning("refresh_job_late_requeue_ignored", extra={"job_id": job.id})
                        _fail_run(db, job.run_id, WORKER_TIMEOUT_ERROR, only_running=False)
                    db.commit()
                    return _refresh_job_snapshot(db, job.id)
            elif job.kind == "settlement":
                run, completed = advance_settlement_job(db, job=job)
                job.run_id = run.id
                if not completed:
                    db.flush()
                    requeued = _guarded_requeue_job(db, job.id)
                    if not requeued:
                        logger.warning("refresh_job_late_requeue_ignored", extra={"job_id": job.id})
                        _fail_run(db, job.run_id, WORKER_TIMEOUT_ERROR, only_running=False)
                    db.commit()
                    return _refresh_job_snapshot(db, job.id)
            elif job.kind == "cleanup":
                job.details = prune_runtime_artifacts(db)
            elif job.kind == "advanced_stats_warm":
                from app.services.advanced_stats import (
                    warm_nba_advanced_for_athletes,
                )
                from app.services.stats_query import default_season_for_sport

                player_ids = list((job.details or {}).get("nba_stats_player_ids") or [])
                season = int((job.details or {}).get("season") or default_season_for_sport("NBA"))
                summary = warm_nba_advanced_for_athletes(
                    db,
                    nba_stats_player_ids=player_ids,
                    season=season,
                )
                job.details = {**(job.details or {}), **summary.as_dict(), "season": season}
            else:  # pragma: no cover - guarded above
                raise ValueError(f"Unsupported refresh job kind: {job.kind}")

            db.flush()
            completed = _guarded_complete_job(db, job.id)
            if not completed:
                logger.warning("refresh_job_late_completion_ignored", extra={"job_id": job.id})
                _fail_run(db, job.run_id, WORKER_TIMEOUT_ERROR, only_running=False)
            db.commit()
            return _refresh_job_snapshot(db, job.id)
        except Exception as exc:
            if job.kind == "prop_refresh" and isinstance(exc, httpx.HTTPError):
                details = dict(job.details or {})
                details["last_transient_error"] = str(exc).strip() or exc.__class__.__name__
                job.details = details
                db.flush()
                requeued = _guarded_requeue_job(db, job.id)
                if not requeued:
                    logger.warning("refresh_job_late_requeue_ignored", extra={"job_id": job.id})
                db.commit()
                return _refresh_job_snapshot(db, job.id)
            if job.run_id is not None:
                _fail_run(db, job.run_id, str(exc).strip() or exc.__class__.__name__)
            error_message = str(exc).strip() or exc.__class__.__name__
            db.flush()
            failed = _guarded_fail_job(db, job.id, error_message)
            if not failed:
                logger.warning("refresh_job_late_failure_ignored", extra={"job_id": job.id})
            db.commit()
            return _refresh_job_snapshot(db, job.id)
