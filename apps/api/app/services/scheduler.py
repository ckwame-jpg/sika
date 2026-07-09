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
from app.services.orders import reconcile_demo_state
from app.services.refresh_jobs import (
    active_job_for_kind,
    enqueue_refresh_job,
    latest_job_for_kind,
    process_refresh_job_queue_once,
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
        active_settlement = active_job_for_kind(db, "settlement")
        latest_settlement = latest_job_for_kind(db, "settlement")

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
        "active_settlement_job": _serialize_job(active_settlement),
        "latest_settlement_job": _serialize_job(latest_settlement),
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


def _queue_settlement_job(reason: str = "interval") -> bool:
    return _queue_job(kind="settlement", scope="predictions", reason=reason)


def _queue_cleanup_job() -> bool:
    return _queue_job(kind="cleanup", scope="retention", reason="interval")


def _queue_advanced_stats_warm_job() -> bool:
    """Queue an advanced-stats warm-up job.

    PR 1: only refreshes NBA team rollups and league percentile breakpoints.
    Per-player NBA Stats fetches need an athlete_id mapping that is added in
    a follow-up PR — until then this job is a low-cost daily refresh.
    """
    return _queue_job(kind="advanced_stats_warm", scope="nba", reason="interval")


def _queue_market_discovery_job() -> bool:
    """Queue a Kalshi standalone-market discovery + event-mapping job.

    Runs ``refresh_kalshi_markets(include_standalone=True)`` to pull new
    market tickers (incl. KXMLBGAME / KXNBAGAME / KXMLBF5 game-winner
    tickers that get buried behind tens of thousands of prop tickers in
    Kalshi's default ordering) and maps them to existing events so the
    next slate refresh picks them up as candidates.
    """
    return _queue_job(kind="market_discovery", scope="standalone", reason="interval")


def _queue_lineup_refresh_job() -> bool:
    """Queue an MLB lineup-refresh job.

    Fetches today's schedule with ``hydrate=lineups,probablePitcher,…`` and
    persists per-event lineup payloads into ``mlb_lineup_cache`` so the
    scoring path's ``emit_lineup_features`` actually finds data to read.
    """
    return _queue_job(kind="lineup_refresh", scope="mlb", reason="interval")


def _queue_nba_injury_refresh_job() -> bool:
    """Smarter #17 phase 2-2 — queue an NBA injury-report refresh.

    Hits ESPN's ``/basketball/nba/injuries`` and persists the parsed
    payload into ``NbaInjuryReportCache``. The cache TTL self-tightens
    to 15 min when an NBA tip-off is inside the final hour
    (Smarter #29), so the scheduler's coarser cadence is enough —
    the near-tip near-zero-staleness happens via the
    ``_effective_injury_report_ttl_minutes`` helper on read.
    """
    return _queue_job(kind="nba_injury_refresh", scope="nba", reason="interval")


def _queue_wnba_injury_refresh_job() -> bool:
    """Smarter WNBA PR 7 — queue a WNBA injury-report refresh.

    WNBA counterpart of :func:`_queue_nba_injury_refresh_job`. Same
    near-tip TTL helper, separate cache table, separate scope so
    operator dashboards can audit per-sport refresh cadence.
    """
    return _queue_job(kind="wnba_injury_refresh", scope="wnba", reason="interval")


def _nfl_events_upcoming(window_days: int = 8) -> bool:
    """True when any NFL event sits inside the near window — the
    off-season gate for the nflverse bundle refresh. The bundle is a
    ~65 MB download; skipping it February–July costs nothing because
    the cached prior-season data doesn't move."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = db.query(Event.id).filter(
            Event.sport_key == "NFL",
            Event.starts_at >= now - timedelta(days=2),
            Event.starts_at <= now + timedelta(days=window_days),
        ).first()
        return row is not None


def _queue_nfl_data_refresh_job() -> bool:
    """Smarter NFL PR 3 — queue the daily nflverse bundle refresh.

    Gated on an NFL event existing inside the next 8 days so the
    off-season stays quiet. nflverse assets update nightly (US time)
    during the season; the 10:00 UTC daily tick lands after that
    publish, and a Sunday 16:00 UTC tick catches late-week injury /
    depth movement before the main slate kicks off at ~17:00 UTC.
    """
    if not _nfl_events_upcoming():
        return False
    return _queue_job(kind="nfl_data_refresh", scope="nfl", reason="interval")


def _queue_nfl_injury_refresh_job() -> bool:
    """Smarter NFL PR 6 — queue an ESPN NFL injury-feed refresh.

    Same off-season gate as the data bundle. NFL injury news moves
    Wed–Sat (practice reports) with a Sunday-morning inactives burst;
    hourly is the write floor and the near-kick TTL tighten handles
    game-day freshness on the read side.
    """
    if not _nfl_events_upcoming():
        return False
    return _queue_job(kind="nfl_injury_refresh", scope="nfl", reason="interval")


def _queue_nba_referee_refresh_job() -> bool:
    """Smarter #13 phase 2a-2 — queue an NBA referee-assignments refresh.

    Scrapes ``official.nba.com/referee-assignments`` and persists the
    parsed dataclass into ``NbaRefereeAssignmentCache``. NBA posts
    assignments the afternoon-of (typically around 5pm ET), so the
    scheduler ticks twice — once around 13:00 UTC (~9am ET, catches
    yesterday's stale data + an early publication) and once at
    21:30 UTC (~5:30pm ET, catches the same-day publication).
    """
    return _queue_job(kind="nba_referee_refresh", scope="nba", reason="interval")


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


def _event_in_burst_window(now: datetime) -> bool:
    """Smarter #14 — return True iff at least one non-terminal event
    has a tip-off inside the burst window.

    The contiguous time window is ``[now - live_game_window_hours,
    now + near_tip_off_window_minutes]`` — pre-tip events ramp burst
    up as a game approaches; events whose ``starts_at`` is in the
    look-back keep burst engaged while they're still live (the
    look-back is the "generous upper bound on game duration" knob;
    4h covers MLB extras).

    **Status filter:** Events with ``status in {completed, cancelled,
    postponed}`` are excluded. Without this filter, a game that
    finished at 22:00 would keep burst engaged until 02:00 next
    morning purely because ``starts_at`` sits in the look-back —
    sika never deletes ``Event`` rows. ``in_progress`` and
    ``scheduled`` are both kept (``scheduled`` covers the brief lag
    between game start and the ingestion cycle flipping the status).

    Single COUNT against an indexed ``starts_at`` column, so this
    runs cheaply on the every-30-seconds ``live_refresh_due_check``
    tick.
    """
    settings = get_settings()
    window_start = now - timedelta(hours=settings.live_game_window_hours)
    window_end = now + timedelta(minutes=settings.near_tip_off_window_minutes)
    with SessionLocal() as db:
        count = (
            db.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.starts_at >= window_start,
                    Event.starts_at <= window_end,
                    Event.status.notin_(("completed", "cancelled", "postponed")),
                )
            )
            or 0
        )
    return count > 0


def _effective_refresh_interval_minutes(now: datetime) -> float:
    """Smarter #14 — compute the effective refresh interval at ``now``.

    Defaults to ``settings.refresh_interval_minutes`` (5min) and
    shortens to ``settings.near_tip_off_refresh_interval_minutes``
    (1min) when an event is inside the burst window. ``min(base,
    burst)`` so an operator who sets the base shorter than the burst
    gets the base (already as fast or faster).
    """
    settings = get_settings()
    base_interval = float(settings.refresh_interval_minutes)
    if _event_in_burst_window(now):
        return min(base_interval, float(settings.near_tip_off_refresh_interval_minutes))
    return base_interval


def current_slate_refresh_due(now: datetime | None = None) -> bool:
    latest_finished_at = _latest_successful_refresh_finished_at()
    reference_now = _as_utc(now) or datetime.now(timezone.utc)
    if latest_finished_at is None:
        return True
    interval_minutes = _effective_refresh_interval_minutes(reference_now)
    due_at = latest_finished_at + timedelta(minutes=interval_minutes)
    return due_at <= reference_now


def _current_slate_due_within(seconds: int, *, now: datetime | None = None) -> bool:
    latest_finished_at = _latest_successful_refresh_finished_at()
    reference_now = _as_utc(now) or datetime.now(timezone.utc)
    if latest_finished_at is None:
        return True
    interval_minutes = _effective_refresh_interval_minutes(reference_now)
    due_at = latest_finished_at + timedelta(minutes=interval_minutes)
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
            sports=["NBA", "MLB", "WNBA"] if current_slate_only else None,
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


def _drain_outbox_job() -> None:
    """Bug #31 — drain pending outbox entries. Imported lazily so the
    handler-registration side effects in ``services/orders.py`` fire
    before the first drain (orders.py is the canonical home for the
    Kalshi-side handlers)."""
    # Import inside the function for two reasons: (1) avoid pulling
    # ``services.orders`` into module-import order at scheduler import,
    # which would create a circular path through routes.py; (2) ensure
    # the handler registrations happen when the scheduler kicks off,
    # not when ``services.scheduler`` is first imported (which can
    # happen during pytest collection).
    from app.services import orders as _orders  # noqa: F401 — registers handlers
    from app.services.outbox import drain_once

    with SessionLocal() as db:
        counts = drain_once(db)
        if counts.get("processed"):
            logger.info(
                "Outbox drain processed=%s succeeded=%s failed=%s dead_lettered=%s skipped=%s",
                counts["processed"],
                counts["succeeded"],
                counts["failed"],
                counts["dead_lettered"],
                counts["skipped"],
            )
        db.commit()


def _evaluate_model_promotions_job() -> None:
    from app.services.ml import kill_switch, promotion

    with SessionLocal() as db:
        promotion.evaluate_all_families(db)
        kill_switch.evaluate_all_families(db)
        db.commit()


# Bug #21: the weekly model retrain used to live HERE as an
# APScheduler job inside the API process. On any deploy that
# doesn't ship the ``apps/ml`` workspace + a writable artifact path
# (notably anything FastAPI-only), the job silently no-op'd. Moved
# to a GitHub Actions cron (``.github/workflows/ml-retrain.yml``)
# so the training step is independent of the API process and the
# API only SERVES the manifests it didn't produce.
#
# The API's ``load_model_manifest`` already reads
# ``apps/ml/manifests/current.json`` lazily on startup, so the API
# side needs no further change — the file lands via the workflow's
# PR commit and is picked up on the next deploy / restart.


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
        lambda: _queue_settlement_job("interval"),
        trigger=IntervalTrigger(minutes=5),
        id="settlement_due_check",
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
    # Bug #31 — outbox drain. Polls every 5s so a freshly-enqueued
    # demo-order submit reaches Kalshi with sub-10s latency in the
    # common case; per-entry exponential backoff still throttles
    # repeated failures.
    scheduler.add_job(
        _drain_outbox_job,
        trigger=IntervalTrigger(seconds=5),
        id="outbox_drain",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _evaluate_model_promotions_job,
        trigger=CronTrigger(hour=4, minute=0),
        id="model_promotion_evaluator",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # Bug #21: removed ``weekly_model_retrain`` scheduler entry. Retraining
    # now runs in GitHub Actions (.github/workflows/ml-retrain.yml). The
    # API only consumes the manifest produced by that workflow.
    if get_settings().advanced_stats_enabled:
        scheduler.add_job(
            _queue_advanced_stats_warm_job,
            trigger=CronTrigger(hour=5, minute=15),
            id="advanced_stats_warm_daily",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    # Kalshi standalone discovery — twice a day so newly-listed game-winner
    # markets land in the DB ahead of the slate refresh that scores them.
    # 09:00 catches morning slate (early MLB matinees + NBA load-in);
    # 16:00 catches the late evening US slate.
    scheduler.add_job(
        _queue_market_discovery_job,
        trigger=CronTrigger(hour="9,16", minute=0),
        id="market_discovery_twice_daily",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # MLB lineup refresh — confirmed lineups generally post 2-4h before first
    # pitch. Two daily ticks (15:00 ahead of evening slate, 11:00 ahead of
    # afternoon games) give us a sub-30-min staleness window without
    # hammering statsapi.mlb.com.
    scheduler.add_job(
        _queue_lineup_refresh_job,
        trigger=CronTrigger(hour="11,15", minute=0),
        id="mlb_lineup_refresh_twice_daily",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # Smarter #17 phase 2-2 — NBA injury refresh. ESPN publishes injury
    # updates throughout the day; we hit hourly while the slate is
    # potentially live (12:00–05:00 UTC ≈ 8am–1am ET) so a player ruled
    # out late afternoon is captured within an hour. The cache TTL
    # tightens to 15 min near tip-off (Smarter #29) so the on-read
    # path stays fresh; this scheduler cadence is the write-side floor.
    scheduler.add_job(
        _queue_nba_injury_refresh_job,
        trigger=CronTrigger(hour="12-23,0-5", minute=15),
        id="nba_injury_refresh_hourly",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # Smarter WNBA PR 7 — WNBA injury refresh. Offset 30 min from the
    # NBA tick so the two ESPN injury fetches don't pile up in the
    # same minute, and so a slow NBA fetch doesn't push the WNBA job
    # behind near-tip cache expiry. Same hour window as the NBA cron
    # (the WNBA season runs through fall, peak overlap with NBA in
    # May–June 2026); the cache TTL self-tightens near tip-off via
    # the shared ``_effective_injury_report_ttl_minutes`` helper.
    scheduler.add_job(
        _queue_wnba_injury_refresh_job,
        trigger=CronTrigger(hour="12-23,0-5", minute=45),
        id="wnba_injury_refresh_hourly",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # Smarter #13 phase 2a-2 — NBA referee refresh. official.nba.com
    # posts assignments the afternoon-of for that night's games,
    # typically around 5pm ET (= 21:00–22:00 UTC). Two daily ticks
    # at HH:30 — 13:30 catches the (rare) early publication; 21:30
    # sits right after the main publication window so the cache is
    # warm before evening tip-offs.
    scheduler.add_job(
        _queue_nba_referee_refresh_job,
        trigger=CronTrigger(hour="13,21", minute=30),
        id="nba_referee_refresh_twice_daily",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # Smarter NFL PR 3 — nflverse bundle refresh. Daily 10:00 UTC sits
    # after nflverse's nightly (US) asset rebuild; the Sunday 16:00 UTC
    # tick refreshes injuries/depth right before the main 17:00 UTC
    # kickoff slate. The queue function no-ops in the off-season (no
    # NFL event within 8 days).
    scheduler.add_job(
        _queue_nfl_data_refresh_job,
        trigger=CronTrigger(hour=10, minute=0),
        id="nfl_data_refresh_daily",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _queue_nfl_data_refresh_job,
        trigger=CronTrigger(day_of_week="sun", hour=16, minute=0),
        id="nfl_data_refresh_sunday",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # Smarter NFL PR 6 — ESPN NFL injury feed, hourly while a US slate
    # could be moving. Minute offset :05 keeps the three ESPN injury
    # fetches (NBA :15, WNBA :45, NFL :05) out of each other's way.
    scheduler.add_job(
        _queue_nfl_injury_refresh_job,
        trigger=CronTrigger(hour="12-23,0-5", minute=5),
        id="nfl_injury_refresh_hourly",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    schedule_event_refreshes()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
