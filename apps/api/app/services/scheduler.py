import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import desc, func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import EspnPlayerGamelogCache, Event, RefreshJob, Run
from app.services.ingestion import run_refresh_cycle
from app.services.live_trading import parse_auto_trade_local_time
from app.services.orders import reconcile_demo_state
from app.services.refresh_jobs import (
    active_job_for_kind,
    enqueue_refresh_job,
    latest_job_for_kind,
    process_refresh_job_queue_once,
    requeue_interrupted_jobs,
    reconcile_stale_jobs as reconcile_stale_refresh_jobs,
)


scheduler = BackgroundScheduler(timezone=get_settings().default_timezone)
logger = logging.getLogger(__name__)
ACTIVE_JOB_STATUSES = ("queued", "running")


@dataclass(frozen=True, slots=True)
class RefreshRunSnapshot:
    run_id: int
    status: str
    records_processed: int
    finished_at: datetime | None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def reconcile_stale_jobs(db) -> int:
    reconciled = reconcile_stale_refresh_jobs(db)
    if reconciled:
        logger.warning("Reconciled %s stale refresh job(s)", reconciled)
    return reconciled


def summarize_refresh_error_message(error_message: str | None) -> str | None:
    raw = " ".join((error_message or "").split()).strip()
    if not raw:
        return None

    lowered = raw.lower()
    if "not bound to a session" in lowered or "refresh operation cannot proceed" in lowered:
        return "The latest refresh hit a temporary database session issue."
    if "stalled - reconciled automatically" in lowered:
        return "The latest refresh stalled and was reset automatically."

    cleaned = re.sub(r"\(background on this error:.*$", "", raw, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"https?://\S+", "", cleaned).strip()
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        return "The latest refresh failed."
    if len(cleaned) > 160:
        cleaned = f"{cleaned[:157].rstrip()}..."
    if cleaned[-1] not in ".!?":
        cleaned = f"{cleaned}."
    return cleaned


def _latest_successful_run_finished_at(kind: str) -> datetime | None:
    with SessionLocal() as db:
        run = db.scalar(
            select(Run)
            .where(Run.kind == kind, Run.status == "completed", Run.finished_at.is_not(None))
            .order_by(desc(Run.finished_at))
            .limit(1)
        )
        return _as_utc(run.finished_at) if run else None


def _latest_successful_refresh_finished_at() -> datetime | None:
    return _latest_successful_run_finished_at("refresh")


def _latest_successful_prop_refresh_finished_at() -> datetime | None:
    return _latest_successful_run_finished_at("prop_refresh")


def _data_stale(latest_finished_at: datetime | None, *, threshold_minutes: int, now: datetime | None = None) -> bool:
    if latest_finished_at is None:
        return True
    reference_now = _as_utc(now) or datetime.now(timezone.utc)
    stale_before = reference_now - timedelta(minutes=threshold_minutes)
    return latest_finished_at < stale_before


def _refresh_data_stale(latest_finished_at: datetime | None, now: datetime | None = None) -> bool:
    settings = get_settings()
    return _data_stale(
        latest_finished_at,
        threshold_minutes=settings.startup_refresh_stale_after_minutes,
        now=now,
    )


def _prop_data_stale(latest_finished_at: datetime | None, now: datetime | None = None) -> bool:
    settings = get_settings()
    return _data_stale(
        latest_finished_at,
        threshold_minutes=settings.prop_refresh_interval_minutes,
        now=now,
    )


def sync_refresh_runtime_state_from_db() -> None:
    return None


def _serialize_job(job) -> dict[str, object] | None:
    if job is None:
        return None
    return {
        "id": job.id,
        "kind": job.kind,
        "scope": job.scope,
        "reason": job.reason,
        "status": job.status,
        "run_id": job.run_id,
        "error_message": job.error_message,
        "details": _serialize_job_details(job),
        "queued_at": _as_utc(job.queued_at),
        "started_at": _as_utc(job.started_at),
        "finished_at": _as_utc(job.finished_at),
    }


def _serialize_job_details(job) -> dict[str, object]:
    details = dict(job.details or {})
    if job.kind != "prop_refresh":
        return details

    cursor = details.get("cursor")
    if not isinstance(cursor, dict):
        return details

    pending_combo_legs = cursor.get("pending_combo_legs")
    if not isinstance(pending_combo_legs, list):
        return details

    summarized_cursor = dict(cursor)
    summarized_cursor["pending_combo_leg_count"] = len(pending_combo_legs)
    summarized_cursor["pending_combo_leg_preview"] = [
        dict(item) if isinstance(item, dict) else item for item in pending_combo_legs[:3]
    ]
    summarized_cursor.pop("pending_combo_legs", None)
    details["cursor"] = summarized_cursor
    return details


def get_refresh_runtime_state() -> dict[str, object | None]:
    with SessionLocal() as db:
        reconciled = reconcile_stale_jobs(db)
        if reconciled:
            db.commit()
        active_refresh = active_job_for_kind(db, "refresh")
        latest_refresh = latest_job_for_kind(db, "refresh")
        active_prop_refresh = active_job_for_kind(db, "prop_refresh")
        latest_prop_refresh = latest_job_for_kind(db, "prop_refresh")

    last_successful_refresh_at = _latest_successful_refresh_finished_at()
    last_prop_refresh_at = _latest_successful_prop_refresh_finished_at()

    refresh_status = active_refresh.status if active_refresh else ("failed" if latest_refresh and latest_refresh.status == "failed" else "idle")
    refresh_reason = active_refresh.reason if active_refresh else (latest_refresh.reason if latest_refresh and latest_refresh.status == "failed" else "none")
    refresh_error_message = active_refresh.error_message if active_refresh else (latest_refresh.error_message if latest_refresh and latest_refresh.status == "failed" else None)

    prop_refresh_status = active_prop_refresh.status if active_prop_refresh else ("failed" if latest_prop_refresh and latest_prop_refresh.status == "failed" else "idle")
    prop_refresh_reason = active_prop_refresh.reason if active_prop_refresh else (latest_prop_refresh.reason if latest_prop_refresh and latest_prop_refresh.status == "failed" else "none")
    prop_refresh_error_message = active_prop_refresh.error_message if active_prop_refresh else (latest_prop_refresh.error_message if latest_prop_refresh and latest_prop_refresh.status == "failed" else None)

    return {
        "refresh_status": refresh_status,
        "refresh_reason": refresh_reason,
        "last_successful_refresh_at": last_successful_refresh_at,
        "data_stale": _refresh_data_stale(last_successful_refresh_at),
        "refresh_error_message": summarize_refresh_error_message(
            str(refresh_error_message).strip() if refresh_error_message else None
        ),
        "prop_refresh_status": prop_refresh_status,
        "prop_refresh_reason": prop_refresh_reason,
        "last_prop_refresh_at": last_prop_refresh_at,
        "prop_data_stale": _prop_data_stale(last_prop_refresh_at),
        "prop_refresh_error_message": summarize_refresh_error_message(
            str(prop_refresh_error_message).strip() if prop_refresh_error_message else None
        ),
        "active_refresh_job": _serialize_job(active_refresh),
        "latest_refresh_job": _serialize_job(latest_refresh),
        "active_prop_refresh_job": _serialize_job(active_prop_refresh),
        "latest_prop_refresh_job": _serialize_job(latest_prop_refresh),
    }


def startup_refresh_needed(now: datetime | None = None) -> bool:
    latest_finished_at = _latest_successful_refresh_finished_at()
    return _refresh_data_stale(latest_finished_at, now)


def _prop_cache_empty() -> bool:
    with SessionLocal() as db:
        return (db.scalar(select(func.count()).select_from(EspnPlayerGamelogCache)) or 0) == 0


def prop_refresh_needed(now: datetime | None = None) -> bool:
    latest_finished_at = _latest_successful_prop_refresh_finished_at()
    if latest_finished_at is None or _prop_data_stale(latest_finished_at, now):
        return True
    return _prop_cache_empty()


def _queue_job(*, kind: str, scope: str, reason: str) -> bool:
    with SessionLocal() as db:
        _job, created = enqueue_refresh_job(db, kind=kind, scope=scope, reason=reason)
        db.commit()
        return created


def _queue_current_slate_refresh(reason: str) -> bool:
    return _queue_job(kind="refresh", scope="current_slate", reason=reason)


def _queue_maintenance_refresh(reason: str) -> bool:
    return _queue_job(kind="prop_refresh", scope="maintenance", reason=reason)


def _queue_cleanup_job() -> bool:
    return _queue_job(kind="cleanup", scope="retention", reason="interval")


def _queue_auto_trade_job(reason: str = "scheduled") -> bool:
    return _queue_job(kind="auto_trade", scope="daily", reason=reason)


def queue_startup_refresh_if_stale() -> bool:
    if not startup_refresh_needed():
        return False
    return _queue_current_slate_refresh("startup")


def _current_slate_job_pending() -> bool:
    with SessionLocal() as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(RefreshJob)
                .where(
                    RefreshJob.kind == "refresh",
                    RefreshJob.scope == "current_slate",
                    RefreshJob.status.in_(ACTIVE_JOB_STATUSES),
                )
            )
            or 0
        ) > 0


def current_slate_refresh_due(now: datetime | None = None) -> bool:
    settings = get_settings()
    latest_finished_at = _latest_successful_refresh_finished_at()
    reference_now = _as_utc(now) or datetime.now(timezone.utc)
    if latest_finished_at is None:
        return True
    due_at = latest_finished_at + timedelta(minutes=settings.refresh_interval_minutes)
    return due_at <= reference_now


def _current_slate_due_within(seconds: int, *, now: datetime | None = None) -> bool:
    settings = get_settings()
    latest_finished_at = _latest_successful_refresh_finished_at()
    reference_now = _as_utc(now) or datetime.now(timezone.utc)
    if latest_finished_at is None:
        return True
    due_at = latest_finished_at + timedelta(minutes=settings.refresh_interval_minutes)
    return due_at <= reference_now + timedelta(seconds=seconds)


def queue_current_slate_refresh_if_due(*, reason: str = "interval") -> bool:
    if _current_slate_job_pending():
        return False
    if not current_slate_refresh_due():
        return False
    return _queue_current_slate_refresh(reason)


def queue_maintenance_refresh_if_due(*, reason: str = "interval") -> bool:
    if _current_slate_job_pending():
        return False
    if _current_slate_due_within(60):
        return False
    if not prop_refresh_needed():
        return False
    return _queue_maintenance_refresh(reason)


def run_refresh_cycle_now(*, reason: str = "manual", current_slate_only: bool = False) -> RefreshRunSnapshot | None:
    with SessionLocal() as db:
        run = run_refresh_cycle(
            db,
            current_slate_only=current_slate_only,
            sports=["NBA", "MLB"] if current_slate_only else None,
        )
        snapshot = RefreshRunSnapshot(
            run_id=run.id,
            status=run.status,
            records_processed=run.records_processed,
            finished_at=run.finished_at,
        )
        db.commit()
        return snapshot


def _reconcile_job() -> None:
    with SessionLocal() as db:
        reconcile_demo_state(db)
        db.commit()


def _process_refresh_queue_job() -> None:
    result = process_refresh_job_queue_once()
    if result and result.kind == "refresh" and result.status == "completed":
        schedule_event_refreshes()


def schedule_event_refreshes() -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        future_events = db.scalars(select(Event).where(Event.starts_at > now)).all()
        for event in future_events:
            starts_at = _as_utc(event.starts_at)
            if starts_at is None:
                continue
            run_at = starts_at - timedelta(minutes=45)
            if run_at <= now:
                continue
            scheduler.add_job(
                lambda: _queue_current_slate_refresh("pregame"),
                trigger=DateTrigger(run_date=run_at),
                id=f"event_refresh_{event.id}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )


def start_scheduler() -> None:
    settings = get_settings()
    if not settings.scheduler_enabled or scheduler.running:
        return
    with SessionLocal() as db:
        recovered = requeue_interrupted_jobs(db)
        if recovered:
            logger.warning("Requeued %s interrupted refresh job(s) after API startup", recovered)
        db.commit()
    enqueue_check_seconds = max(settings.queue_poll_interval_seconds, 30)
    scheduler.add_job(
        _process_refresh_queue_job,
        trigger=IntervalTrigger(seconds=settings.queue_poll_interval_seconds),
        id="refresh_queue_processor",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        lambda: queue_current_slate_refresh_if_due(reason="interval"),
        trigger=IntervalTrigger(seconds=enqueue_check_seconds),
        id="live_refresh_due_check",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        lambda: queue_maintenance_refresh_if_due(reason="interval"),
        trigger=IntervalTrigger(seconds=enqueue_check_seconds),
        id="maintenance_refresh_due_check",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _queue_cleanup_job,
        trigger=IntervalTrigger(hours=settings.cleanup_interval_hours),
        id="cleanup_enqueue",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _reconcile_job,
        trigger=CronTrigger(minute="*/15"),
        id="demo_reconcile",
        replace_existing=True,
    )
    auto_trade_time = parse_auto_trade_local_time(settings)
    scheduler.add_job(
        lambda: _queue_auto_trade_job("scheduled"),
        trigger=CronTrigger(hour=auto_trade_time.hour, minute=auto_trade_time.minute),
        id="auto_trade_daily_enqueue",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    schedule_event_refreshes()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
