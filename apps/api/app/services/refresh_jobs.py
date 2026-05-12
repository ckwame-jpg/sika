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


REFRESH_JOB_KINDS = frozenset({
    "refresh",
    "prop_refresh",
    "shadow_capture",
    "settlement",
    "cleanup",
    "advanced_stats_warm",
    "weather_refresh",
    "lineup_refresh",
    "advanced_stats_audit",
    "market_discovery",
})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
STALE_REFRESH_JOB_ERROR = "stalled - reconciled automatically"
QUEUE_PROCESSOR_WEDGED = "queue_processor_wedged"
WORKER_TIMEOUT_ERROR = "worker_timeout"
WORKER_TIMEOUT_GRACE_SECONDS = 10.0
CURRENT_SLATE_WORKER_TIMEOUT_SECONDS = 300.0
PROP_REFRESH_WORKER_TIMEOUT_SECONDS = 300.0
SETTLEMENT_WORKER_TIMEOUT_SECONDS = 120.0
ADVANCED_STATS_WARM_WORKER_TIMEOUT_SECONDS = 600.0
MARKET_DISCOVERY_WORKER_TIMEOUT_SECONDS = 300.0
LINEUP_REFRESH_WORKER_TIMEOUT_SECONDS = 180.0
WEATHER_REFRESH_WORKER_TIMEOUT_SECONDS = 120.0
CLEANUP_WORKER_TIMEOUT_SECONDS = 120.0
SHADOW_CAPTURE_WORKER_TIMEOUT_SECONDS = 180.0
ADVANCED_STATS_AUDIT_WORKER_TIMEOUT_SECONDS = 120.0
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


_MLB_GAME_TIME_WINDOW_SECONDS = 3 * 60 * 60  # ±3h match window — covers rain delays + TZ drift.


def _build_mlb_event_index(
    db: Session,
) -> list[tuple[Any, datetime, set[str]]]:
    """Return a list of (event, starts_at_utc, team_tokens) for active MLB events.

    Cached once per ``lineup_refresh`` invocation so we don't re-scan the
    Event table per scheduled game. Token comparison mirrors
    ``map_markets_to_events`` so behavior stays consistent across pipelines.
    """
    from sqlalchemy import select as _select
    from sqlalchemy.orm import selectinload

    from app.models import Event, EventParticipant
    from app.sports.base import alias_tokens

    rows = db.scalars(
        _select(Event)
        .options(
            selectinload(Event.participants).selectinload(
                EventParticipant.participant
            )
        )
        .where(Event.sport_key == "MLB")
        .where(Event.status != "completed")
    ).all()
    index: list[tuple[Any, datetime, set[str]]] = []
    for event in rows:
        starts_at = _as_utc(event.starts_at) if event.starts_at else None
        if starts_at is None:
            continue
        tokens = alias_tokens(event.name)
        for entry in event.participants:
            participant = entry.participant
            tokens.update(
                alias_tokens(participant.display_name, participant.short_name)
            )
        index.append((event, starts_at, tokens))
    return index


def _match_mlb_event(
    event_index: list[tuple[Any, datetime, set[str]]],
    game: dict[str, Any],
) -> Any | None:
    """Find the sika ``Event`` matching an MLB Stats schedule game.

    Match key is ``sport_key == "MLB"`` (already filtered by the index) plus
    a start-time window (±3h) plus a team-name token overlap. Returns the
    closest-in-time best-token-overlap event, or ``None`` if no event meets
    the threshold (Codex round 3: this replaces the old `event_id=str(gamePk)`
    write that never met scoring's `event_id=str(event.id)` read).
    """
    from app.sports.base import alias_tokens

    game_iso = game.get("gameDate") or game.get("game_date")
    game_time: datetime | None = None
    if isinstance(game_iso, str):
        try:
            game_time = datetime.fromisoformat(game_iso.replace("Z", "+00:00"))
        except ValueError:
            game_time = None
    if game_time is not None and game_time.tzinfo is None:
        game_time = game_time.replace(tzinfo=timezone.utc)
    elif game_time is not None:
        game_time = game_time.astimezone(timezone.utc)

    teams = game.get("teams") or {}
    home_name = ((teams.get("home") or {}).get("team") or {}).get("name")
    away_name = ((teams.get("away") or {}).get("team") or {}).get("name")
    game_tokens = alias_tokens(home_name, away_name)
    if not game_tokens:
        return None

    best: tuple[Any, float, float] | None = None  # (event, -overlap, |dt|)
    for event, starts_at, event_tokens in event_index:
        shared = game_tokens & event_tokens
        if not shared:
            continue
        # Both teams must overlap by at least one strong token each. We can't
        # cheaply prove "one token per team" without participant-level
        # tokenisation, so require >=2 shared tokens of length >= 4 as a
        # conservative proxy (e.g. {"yankees", "redsox"}).
        strong = [t for t in shared if len(t) >= 4]
        if len(strong) < 2:
            continue
        if game_time is not None:
            dt = abs((starts_at - game_time).total_seconds())
            if dt > _MLB_GAME_TIME_WINDOW_SECONDS:
                continue
        else:
            dt = 0.0
        score = -float(len(shared))
        if best is None or (score, dt) < (best[1], best[2]):
            best = (event, score, dt)
    return best[0] if best else None


def _extract_probable_pitcher_ids(schedule_payload: dict[str, Any]) -> list[str]:
    """Pull MLB Stats PERSON_IDs for both probable starters from a hydrated schedule.

    The `hydrate=probablePitcher` query string puts each starter under
    ``game.teams.{home,away}.probablePitcher``. We deduplicate across the
    whole slate and return them as strings ready for
    ``warm_mlb_advanced_for_athletes(pitcher_ids=...)``.
    """
    seen: set[str] = set()
    out: list[str] = []
    for date_block in (schedule_payload or {}).get("dates") or []:
        for game in date_block.get("games") or []:
            teams = game.get("teams") or {}
            for side_key in ("home", "away"):
                side = teams.get(side_key) or {}
                pitcher = side.get("probablePitcher") or {}
                pid = pitcher.get("id")
                if pid is None:
                    continue
                pid_str = str(pid)
                if pid_str in seen:
                    continue
                seen.add(pid_str)
                out.append(pid_str)
    return out


def _worker_timeout_seconds(job: RefreshJob) -> float:
    settings = get_settings()
    default_timeout = max(float(settings.maintenance_claim_budget_seconds), 0.0) + WORKER_TIMEOUT_GRACE_SECONDS
    if job.kind == "refresh" and job.scope == "current_slate":
        return max(default_timeout, CURRENT_SLATE_WORKER_TIMEOUT_SECONDS)
    if job.kind == "prop_refresh":
        return max(default_timeout, PROP_REFRESH_WORKER_TIMEOUT_SECONDS)
    if job.kind == "settlement":
        return max(default_timeout, SETTLEMENT_WORKER_TIMEOUT_SECONDS)
    if job.kind == "advanced_stats_warm":
        return max(default_timeout, ADVANCED_STATS_WARM_WORKER_TIMEOUT_SECONDS)
    if job.kind == "market_discovery":
        return max(default_timeout, MARKET_DISCOVERY_WORKER_TIMEOUT_SECONDS)
    if job.kind == "lineup_refresh":
        return max(default_timeout, LINEUP_REFRESH_WORKER_TIMEOUT_SECONDS)
    if job.kind == "weather_refresh":
        return max(default_timeout, WEATHER_REFRESH_WORKER_TIMEOUT_SECONDS)
    if job.kind == "cleanup":
        return max(default_timeout, CLEANUP_WORKER_TIMEOUT_SECONDS)
    if job.kind == "shadow_capture":
        return max(default_timeout, SHADOW_CAPTURE_WORKER_TIMEOUT_SECONDS)
    if job.kind == "advanced_stats_audit":
        return max(default_timeout, ADVANCED_STATS_AUDIT_WORKER_TIMEOUT_SECONDS)
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
            if timed_out:
                job.error_message = WORKER_TIMEOUT_ERROR
            elif job.status == "queued":
                # Queued past stale-minutes means no processor ever picked it
                # up — distinct from a worker that started but timed out.
                job.error_message = QUEUE_PROCESSOR_WEDGED
            else:
                job.error_message = STALE_REFRESH_JOB_ERROR
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
        .with_for_update(skip_locked=True)
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
                        job=job,
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
                from datetime import date as _date

                from app.clients.baseball_savant import BaseballSavantClient
                from app.clients.mlb_stats import MlbStatsClient
                from app.models import EspnPlayerSearchCache
                from app.services.advanced_stats import (
                    warm_nba_advanced_for_athletes,
                )
                from app.services.mlb_advanced import (
                    warm_mlb_advanced_for_athletes,
                )
                from app.services.stats_query import default_season_for_sport

                details = dict(job.details or {})
                # Allow callers to pre-pin a list (tests, manual runs); otherwise
                # derive from EspnPlayerSearchCache rows that already have a
                # resolved nba_stats_id / mlb_stats_id sidecar. This is the set
                # the resolver has touched on real prop scoring, so warming
                # them keeps the cache fresh for tomorrow's slate without
                # needing the full active roster.
                nba_player_ids = list(details.get("nba_stats_player_ids") or [])
                mlb_player_ids = list(details.get("mlb_stats_player_ids") or [])

                if not nba_player_ids:
                    nba_player_ids = sorted({
                        str(payload.get("nba_stats_id"))
                        for entry in db.query(EspnPlayerSearchCache).filter(EspnPlayerSearchCache.sport_key == "NBA").all()
                        for payload in [entry.payload or {}]
                        if payload.get("nba_stats_id")
                    })
                if not mlb_player_ids:
                    mlb_player_ids = sorted({
                        str(payload.get("mlb_stats_id"))
                        for entry in db.query(EspnPlayerSearchCache).filter(EspnPlayerSearchCache.sport_key == "MLB").all()
                        for payload in [entry.payload or {}]
                        if payload.get("mlb_stats_id")
                    })

                # Probable starters from today's MLB schedule — Codex round 3
                # called out that the EspnPlayerSearchCache sidecar list only
                # contains players the resolver has previously touched as a
                # prop subject, so a starter-only pitcher would never get
                # warmed. Pull the schedule with probablePitcher hydration and
                # extract every starter ID directly so the cron actually
                # warms the slate's mlb_pitcher_advanced_cache /
                # mlb_statcast_pitcher_cache rows.
                probable_pitcher_ids: list[str] = []
                schedule_probable_failed = False
                try:
                    schedule_payload = MlbStatsClient().fetch_schedule(_date.today())
                    probable_pitcher_ids = _extract_probable_pitcher_ids(schedule_payload)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("advanced_stats_warm probable-pitcher fetch failed: %s", exc)
                    schedule_probable_failed = True

                # Codex round 4 #1 / round 5: the warm cron is invoked on
                # two paths now — the daily 05:15 cron with no flag, and the
                # late-day `lineup_refresh` enqueue with
                # ``details.pitchers_only=True``. The flag short-circuits
                # batter + NBA warming AND scopes pitcher warming to the
                # discovered late-day starter set so the tick stays cheap.
                #
                # Round 5 caught a real bug here: PR 9 built
                # ``merged_pitcher_ids`` from
                # ``explicit + sidecar_mlb_player_ids + probable_pitcher_ids``
                # *before* checking the flag, so even a "pitchers only"
                # job fanned out pitcher Statcast over every sidecar batter
                # ID. The fix is to build the pitcher list per mode.
                pitchers_only = bool(details.get("pitchers_only"))
                nba_season = int(details.get("nba_season") or default_season_for_sport("NBA"))
                mlb_season = int(details.get("mlb_season") or default_season_for_sport("MLB"))
                if pitchers_only:
                    nba_summary_dict: dict[str, int] = {}
                    effective_mlb_player_ids: list[str] = []
                    # Late-day path — only explicit IDs supplied by
                    # ``lineup_refresh`` plus the schedule's probable
                    # starters. NEVER include the sidecar batter list.
                    pitcher_ids_for_warm = sorted({
                        str(pid)
                        for pid in (
                            list(details.get("pitcher_ids") or [])
                            + list(probable_pitcher_ids)
                        )
                        if pid
                    })
                else:
                    nba_summary = warm_nba_advanced_for_athletes(
                        db, nba_stats_player_ids=nba_player_ids, season=nba_season
                    )
                    nba_summary_dict = nba_summary.as_dict()
                    effective_mlb_player_ids = mlb_player_ids
                    # Daily path — keep the sidecar IDs as a backstop so
                    # any two-way player or starter who has shown up as a
                    # prop subject still gets pitcher caches refreshed.
                    pitcher_ids_for_warm = sorted({
                        str(pid)
                        for pid in (
                            list(details.get("pitcher_ids") or [])
                            + list(mlb_player_ids)
                            + list(probable_pitcher_ids)
                        )
                        if pid
                    })
                # Codex round 4 #2: the cron used to pass a single ``savant``
                # client which fanned out batter Statcast for every
                # ``mlb_stats_player_ids`` sidecar, not just probable
                # starters. Pass ``savant_pitcher`` only so the operational
                # cost stays bounded by the probable-starter set.
                mlb_summary = warm_mlb_advanced_for_athletes(
                    db,
                    mlb_stats_player_ids=effective_mlb_player_ids,
                    pitcher_ids=pitcher_ids_for_warm,
                    season=mlb_season,
                    savant_pitcher=BaseballSavantClient(),
                )
                job.details = {
                    **details,
                    **nba_summary_dict,
                    **mlb_summary,
                    "nba_season": nba_season,
                    "mlb_season": mlb_season,
                    "nba_stats_player_ids_warmed": len(nba_player_ids) if not pitchers_only else 0,
                    "mlb_stats_player_ids_warmed": len(effective_mlb_player_ids),
                    "mlb_pitcher_ids_warmed": len(pitcher_ids_for_warm),
                    "mlb_probable_pitcher_ids_warmed": len(probable_pitcher_ids),
                    "schedule_probable_fetch_failed": schedule_probable_failed,
                    "pitchers_only": pitchers_only,
                }
            elif job.kind == "weather_refresh":
                # Placeholder: per-event weather is loaded lazily via the resolver
                # path. This kind exists so a scheduled cron can pre-warm caches
                # for upcoming events; the implementation walks the current slate
                # in a follow-up PR.
                job.details = {**(job.details or {}), "events_warmed": 0}
            elif job.kind == "lineup_refresh":
                # Fetch today's MLB schedule with lineups + probablePitcher
                # hydration and persist the per-event payload via
                # ``load_lineup_for_event``. Each event's lineup row is what
                # ``emit_lineup_features`` reads at scoring time, so this
                # is the producer that PR 6's lineup-feature consumer was
                # missing.
                #
                # Codex round 3 flagged the key mismatch: scoring reads
                # ``event_id=str(event.id)`` (sika DB primary key) but the
                # producer was writing ``event_id=str(gamePk)`` (MLB Stats
                # API key), so cache rows never met. We now match each
                # MLB game to its sika ``Event`` by sport_key + start time
                # window + team-name token overlap and persist under the
                # app event id.
                from datetime import date as _date

                from app.clients.mlb_stats import MlbStatsClient
                from app.services.mlb_advanced import load_lineup_for_event

                target = _date.today()
                events_warmed = 0
                games_unmatched = 0
                games_failed = 0
                schedule_failed = False
                late_day_pitcher_ids: list[str] = []
                try:
                    mlb_client = MlbStatsClient()
                    schedule = mlb_client.fetch_schedule(target)
                except Exception as exc:  # noqa: BLE001 — upstream MLB API is unpredictable
                    logger.warning("lineup_refresh schedule fetch failed: %s", exc)
                    schedule = None
                    schedule_failed = True

                if schedule is not None:
                    event_index = _build_mlb_event_index(db)
                    for date_block in schedule.get("dates") or []:
                        for game in date_block.get("games") or []:
                            game_pk = game.get("gamePk") or game.get("game_pk")
                            if not game_pk:
                                continue
                            try:
                                event = _match_mlb_event(event_index, game)
                                if event is None:
                                    games_unmatched += 1
                                    continue
                                # Wrap each per-game payload in the same
                                # envelope ``emit_lineup_features`` expects:
                                # payload["raw"] is a schedule-shaped dict so
                                # the parser walks the same path it does for
                                # the live schedule.
                                single_game_envelope = {"dates": [{"games": [game]}]}
                                result = load_lineup_for_event(
                                    db,
                                    event_id=str(event.id),
                                    schedule_payload=single_game_envelope,
                                )
                                if result.complete:
                                    events_warmed += 1
                            except Exception as exc:  # noqa: BLE001
                                # Codex round 3 hardening: one bad game payload
                                # must not poison the whole slate's warm pass.
                                games_failed += 1
                                logger.warning(
                                    "lineup_refresh per-game failure (gamePk=%s): %s",
                                    game_pk,
                                    exc,
                                )
                    late_day_pitcher_ids = _extract_probable_pitcher_ids(schedule)

                # Codex round 4 #2: the 05:15 advanced_stats_warm tick can
                # miss TBD starters / late scratches. ``lineup_refresh``
                # already has the schedule in hand, so enqueue a
                # pitchers-only warm pass with the discovered probable IDs.
                # This is the cheap "second tick" Codex asked for without
                # adding a new cron.
                pitcher_warm_enqueued = False
                if late_day_pitcher_ids:
                    try:
                        warm_job, created = enqueue_refresh_job(
                            db,
                            kind="advanced_stats_warm",
                            scope="lineup_refresh_pitchers",
                            reason=f"lineup_refresh discovered {len(late_day_pitcher_ids)} probable starters",
                        )
                        # ``coalesce`` may have returned an existing queued
                        # job from an earlier lineup_refresh tick. Codex
                        # round 5: union the existing + newly-discovered
                        # pitcher IDs rather than overwriting, so a partial
                        # earlier schedule fetch (e.g. 11:00) doesn't drop
                        # starters the second tick still sees.
                        prior_details = dict(warm_job.details or {})
                        prior_ids = list(prior_details.get("pitcher_ids") or [])
                        merged_late_ids = sorted({
                            str(pid)
                            for pid in prior_ids + late_day_pitcher_ids
                            if pid
                        })
                        warm_job.details = {
                            **prior_details,
                            "pitcher_ids": merged_late_ids,
                            "pitchers_only": True,
                        }
                        db.flush()
                        pitcher_warm_enqueued = bool(created)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "lineup_refresh pitcher-warm enqueue failed: %s", exc
                        )

                job.details = {
                    **(job.details or {}),
                    "lineups_warmed": events_warmed,
                    "lineups_unmatched": games_unmatched,
                    "lineups_failed": games_failed,
                    "target_date": target.isoformat(),
                    "schedule_fetch_failed": schedule_failed,
                    "pitcher_warm_enqueued": pitcher_warm_enqueued,
                    "late_day_pitcher_ids_seen": len(late_day_pitcher_ids),
                }
            elif job.kind == "advanced_stats_audit":
                # Placeholder reconciliation: counts unmapped athlete IDs.
                # Will be implemented in PR 2c.
                job.details = {**(job.details or {}), "unmapped_count": 0}
            elif job.kind == "market_discovery":
                # Pulls a deep page of Kalshi standalone markets and maps any
                # new ones to existing events. Targets game-winner / first-five
                # tickers (KXMLBGAME-, KXNBAGAME-, KXMLBF5-) which otherwise
                # get buried behind tens of thousands of prop tickers.
                from app.services.ingestion import refresh_kalshi_markets
                from app.services.market_mapping import map_markets_to_events

                summary = refresh_kalshi_markets(
                    db,
                    include_standalone=True,
                    refresh_combo_prop_tickers=False,
                    discover_combo_props=False,
                )
                mapped = map_markets_to_events(db)
                job.details = {
                    **(job.details or {}),
                    "processed": int(summary.get("processed") or 0),
                    "total_kalshi_markets_seen": int(summary.get("total_kalshi_markets_seen") or 0),
                    "supported_nba_props_seen": int(summary.get("supported_nba_props_seen") or 0),
                    "supported_mlb_props_seen": int(summary.get("supported_mlb_props_seen") or 0),
                    "market_snapshots_written": int(summary.get("market_snapshots_written") or 0),
                    "newly_mapped_to_events": int(mapped),
                }
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
