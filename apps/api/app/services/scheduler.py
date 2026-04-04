import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import desc, func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import EspnPlayerGamelogCache, Event, Run
from app.services.ingestion import run_prop_refresh_cycle, run_refresh_cycle
from app.services.orders import reconcile_demo_state


scheduler = BackgroundScheduler(timezone=get_settings().default_timezone)
_refresh_lock = Lock()
_prop_refresh_lock = Lock()
_refresh_state_lock = Lock()
_prop_refresh_state_lock = Lock()
_refresh_runtime_state: dict[str, object | None] = {
    "refresh_status": "idle",
    "refresh_reason": "none",
    "last_successful_refresh_at": None,
    "refresh_error_message": None,
}
_prop_refresh_runtime_state: dict[str, object | None] = {
    "prop_refresh_status": "idle",
    "prop_refresh_reason": "none",
    "last_prop_refresh_at": None,
    "prop_refresh_error_message": None,
}


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


def summarize_refresh_error_message(error_message: str | None) -> str | None:
    raw = " ".join((error_message or "").split()).strip()
    if not raw:
        return None

    lowered = raw.lower()
    if "not bound to a session" in lowered or "refresh operation cannot proceed" in lowered:
        return "The latest refresh hit a temporary database session issue."

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


def _set_refresh_runtime_state(
    *,
    refresh_status: str | None = None,
    refresh_reason: str | None = None,
    last_successful_refresh_at: datetime | None | object = ...,
    refresh_error_message: str | None | object = ...,
) -> None:
    with _refresh_state_lock:
        if refresh_status is not None:
            _refresh_runtime_state["refresh_status"] = refresh_status
        if refresh_reason is not None:
            _refresh_runtime_state["refresh_reason"] = refresh_reason
        if last_successful_refresh_at is not ...:
            _refresh_runtime_state["last_successful_refresh_at"] = _as_utc(last_successful_refresh_at)  # type: ignore[arg-type]
        if refresh_error_message is not ...:
            _refresh_runtime_state["refresh_error_message"] = refresh_error_message


def _set_prop_refresh_runtime_state(
    *,
    prop_refresh_status: str | None = None,
    prop_refresh_reason: str | None = None,
    last_prop_refresh_at: datetime | None | object = ...,
    prop_refresh_error_message: str | None | object = ...,
) -> None:
    with _prop_refresh_state_lock:
        if prop_refresh_status is not None:
            _prop_refresh_runtime_state["prop_refresh_status"] = prop_refresh_status
        if prop_refresh_reason is not None:
            _prop_refresh_runtime_state["prop_refresh_reason"] = prop_refresh_reason
        if last_prop_refresh_at is not ...:
            _prop_refresh_runtime_state["last_prop_refresh_at"] = _as_utc(last_prop_refresh_at)  # type: ignore[arg-type]
        if prop_refresh_error_message is not ...:
            _prop_refresh_runtime_state["prop_refresh_error_message"] = prop_refresh_error_message


def sync_refresh_runtime_state_from_db() -> None:
    _set_refresh_runtime_state(
        refresh_status="idle",
        refresh_reason="none",
        last_successful_refresh_at=_latest_successful_refresh_finished_at(),
        refresh_error_message=None,
    )
    _set_prop_refresh_runtime_state(
        prop_refresh_status="idle",
        prop_refresh_reason="none",
        last_prop_refresh_at=_latest_successful_prop_refresh_finished_at(),
        prop_refresh_error_message=None,
    )


def get_refresh_runtime_state() -> dict[str, object | None]:
    with _refresh_state_lock:
        refresh_snapshot = dict(_refresh_runtime_state)
    with _prop_refresh_state_lock:
        prop_snapshot = dict(_prop_refresh_runtime_state)

    last_successful_refresh_at = _as_utc(refresh_snapshot["last_successful_refresh_at"])  # type: ignore[arg-type]
    last_prop_refresh_at = _as_utc(prop_snapshot["last_prop_refresh_at"])  # type: ignore[arg-type]
    return {
        "refresh_status": refresh_snapshot["refresh_status"],
        "refresh_reason": refresh_snapshot["refresh_reason"],
        "last_successful_refresh_at": last_successful_refresh_at,
        "data_stale": _refresh_data_stale(last_successful_refresh_at),
        "refresh_error_message": refresh_snapshot["refresh_error_message"],
        "prop_refresh_status": prop_snapshot["prop_refresh_status"],
        "prop_refresh_reason": prop_snapshot["prop_refresh_reason"],
        "last_prop_refresh_at": last_prop_refresh_at,
        "prop_data_stale": _prop_data_stale(last_prop_refresh_at),
        "prop_refresh_error_message": prop_snapshot["prop_refresh_error_message"],
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


def _run_refresh_cycle_guarded(
    *,
    reason: str,
    only_if_stale: bool = False,
    raise_on_error: bool = False,
) -> RefreshRunSnapshot | None:
    if not _refresh_lock.acquire(blocking=False):
        return None
    try:
        _set_refresh_runtime_state(
            refresh_status="running",
            refresh_reason=reason,
            refresh_error_message=None,
        )
        if only_if_stale and not startup_refresh_needed():
            _set_refresh_runtime_state(refresh_status="idle", refresh_reason="none", refresh_error_message=None)
            return None
        with SessionLocal() as db:
            run = run_refresh_cycle(db)
            refresh_snapshot = RefreshRunSnapshot(
                run_id=run.id,
                status=run.status,
                records_processed=run.records_processed,
                finished_at=run.finished_at,
            )
            db.commit()
        _set_refresh_runtime_state(
            refresh_status="idle",
            refresh_reason="none",
            last_successful_refresh_at=refresh_snapshot.finished_at or datetime.now(timezone.utc),
            refresh_error_message=None,
        )
        schedule_event_refreshes()
        return refresh_snapshot
    except Exception as exc:
        _set_refresh_runtime_state(
            refresh_status="failed",
            refresh_reason=reason,
            refresh_error_message=summarize_refresh_error_message(str(exc).strip() or exc.__class__.__name__),
        )
        if raise_on_error:
            raise
        return None
    finally:
        _refresh_lock.release()


def _run_prop_refresh_cycle_guarded(
    *,
    reason: str,
    only_if_due: bool = False,
    raise_on_error: bool = False,
) -> RefreshRunSnapshot | None:
    if not _prop_refresh_lock.acquire(blocking=False):
        return None
    try:
        _set_prop_refresh_runtime_state(
            prop_refresh_status="running",
            prop_refresh_reason=reason,
            prop_refresh_error_message=None,
        )
        if only_if_due and not prop_refresh_needed():
            _set_prop_refresh_runtime_state(prop_refresh_status="idle", prop_refresh_reason="none", prop_refresh_error_message=None)
            return None
        with SessionLocal() as db:
            run = run_prop_refresh_cycle(db)
            refresh_snapshot = RefreshRunSnapshot(
                run_id=run.id,
                status=run.status,
                records_processed=run.records_processed,
                finished_at=run.finished_at,
            )
            db.commit()
        _set_prop_refresh_runtime_state(
            prop_refresh_status="idle",
            prop_refresh_reason="none",
            last_prop_refresh_at=refresh_snapshot.finished_at or datetime.now(timezone.utc),
            prop_refresh_error_message=None,
        )
        return refresh_snapshot
    except Exception as exc:
        _set_prop_refresh_runtime_state(
            prop_refresh_status="failed",
            prop_refresh_reason=reason,
            prop_refresh_error_message=summarize_refresh_error_message(str(exc).strip() or exc.__class__.__name__),
        )
        if raise_on_error:
            raise
        return None
    finally:
        _prop_refresh_lock.release()


def _run_refresh_job() -> None:
    _run_refresh_cycle_guarded(reason="interval")


def _run_prop_refresh_job() -> None:
    _run_prop_refresh_cycle_guarded(reason="interval", only_if_due=True)


def _run_startup_refresh_job() -> None:
    _run_refresh_cycle_guarded(reason="startup", only_if_stale=True)


def _run_pregame_refresh_job() -> None:
    _run_refresh_cycle_guarded(reason="pregame")


def queue_startup_refresh_if_stale() -> bool:
    if not startup_refresh_needed():
        _set_refresh_runtime_state(refresh_status="idle", refresh_reason="none", refresh_error_message=None)
        return False
    _set_refresh_runtime_state(
        refresh_status="queued",
        refresh_reason="startup",
        refresh_error_message=None,
    )
    try:
        scheduler.add_job(
            _run_startup_refresh_job,
            trigger=DateTrigger(run_date=datetime.now(timezone.utc)),
            id="startup_refresh",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    except Exception as exc:
        _set_refresh_runtime_state(
            refresh_status="failed",
            refresh_reason="startup",
            refresh_error_message=str(exc).strip() or exc.__class__.__name__,
        )
        return False
    return True


def _queue_prop_refresh_thread(*, reason: str) -> None:
    Thread(
        target=_run_prop_refresh_cycle_guarded,
        kwargs={"reason": reason, "raise_on_error": False},
        daemon=True,
    ).start()


def queue_prop_refresh_if_due(*, reason: str = "manual") -> bool:
    if not prop_refresh_needed():
        return False
    _set_prop_refresh_runtime_state(
        prop_refresh_status="queued",
        prop_refresh_reason=reason,
        prop_refresh_error_message=None,
    )
    try:
        if scheduler.running:
            scheduler.add_job(
                _run_prop_refresh_job if reason == "interval" else lambda: _run_prop_refresh_cycle_guarded(reason=reason),
                trigger=DateTrigger(run_date=datetime.now(timezone.utc)),
                id=f"{reason}_prop_refresh",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        else:
            _queue_prop_refresh_thread(reason=reason)
    except Exception as exc:
        _set_prop_refresh_runtime_state(
            prop_refresh_status="failed",
            prop_refresh_reason=reason,
            prop_refresh_error_message=str(exc).strip() or exc.__class__.__name__,
        )
        return False
    return True


def run_refresh_cycle_now(*, reason: str = "manual") -> RefreshRunSnapshot | None:
    return _run_refresh_cycle_guarded(reason=reason, raise_on_error=True)


def _reconcile_job() -> None:
    with SessionLocal() as db:
        reconcile_demo_state(db)
        db.commit()


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
            job_id = f"event_refresh_{event.id}"
            scheduler.add_job(
                _run_pregame_refresh_job,
                trigger=DateTrigger(run_date=run_at),
                id=job_id,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )


def start_scheduler() -> None:
    settings = get_settings()
    if not settings.scheduler_enabled or scheduler.running:
        return
    scheduler.add_job(
        _run_refresh_job,
        trigger=IntervalTrigger(minutes=settings.refresh_interval_minutes),
        id="live_refresh",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _run_prop_refresh_job,
        trigger=IntervalTrigger(minutes=settings.prop_refresh_interval_minutes),
        id="prop_refresh",
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
    scheduler.start()
    schedule_event_refreshes()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
