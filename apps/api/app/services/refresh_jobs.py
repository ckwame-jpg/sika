from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any

import httpx
from sqlalchemy import case, desc, event, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import RefreshJob, Run
from app.services.ingestion import advance_current_slate_refresh_job, advance_prop_refresh_job, run_refresh_cycle
from app.services.maintenance import prune_runtime_artifacts
from app.services.ml.shadow import capture_shadow_artifacts_batch
from app.services.ml.shadow_modes import DIAGNOSTIC_BACKFILL_CAPTURE_MODE
from app.services.paper_parlays import settle_paper_parlays
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
    # Smarter #17 phase 2-2 — populate ``NbaInjuryReportCache`` so the
    # consumer-side ``emit_nba_injury_features`` (Smarter #17 phase 1)
    # has data to read.
    "nba_injury_refresh",
    # Smarter WNBA PR 7 — WNBA counterpart of ``nba_injury_refresh``.
    # Populates ``WnbaInjuryReportCache`` so the scoring-side
    # ``wnba_injury`` SUPPRESS gate has data to suppress on. Separate
    # kind (not a shared ``injury_refresh`` with a sport param) keeps
    # operator visibility per-sport in the refresh-job table.
    "wnba_injury_refresh",
    # Smarter #13 phase 2a-2 — populate ``NbaRefereeAssignmentCache``
    # ahead of the same-day consumer-side wiring (phase 2b/c/d).
    # Phase 2b-2 (2026-05-16) bundles the per-season tendency cache
    # refresh into the same job — both are NBA-officiating data on
    # the same daily cadence, no need for a separate job kind.
    "nba_referee_refresh",
    # Smarter NFL PR 3 — daily nflverse bundle refresh: weekly player
    # stats, snap counts, depth charts, official injuries, team EPA
    # ratings, schedule (+ weather prewarm for events inside 36h).
    # One job for the whole bundle because nflverse distributes bulk
    # per-season files — there's no per-dataset incremental fetch to
    # split across kinds.
    "nfl_data_refresh",
    # Smarter NFL PR 6 — ESPN NFL injury feed (the intraday supplement
    # to the nightly official report). Same shape as the NBA / WNBA
    # injury refreshes.
    "nfl_injury_refresh",
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
# Smarter #17 / #13 phase 2 — both cache refreshes are single HTTP
# GETs (ESPN injuries / official.nba.com referees) plus a single
# upsert. Generous-enough budget for a slow upstream.
NBA_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS = 60.0
NBA_REFEREE_REFRESH_WORKER_TIMEOUT_SECONDS = 60.0
# Smarter WNBA PR 7 — same shape / cost profile as the NBA injury
# refresh (single ESPN GET + upsert into ``WnbaInjuryReportCache``).
WNBA_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS = 60.0
# Smarter NFL PR 3 — the nflverse bundle is ~6 CSV downloads, the
# largest ~50 MB (depth charts), plus per-week upserts. Generous
# budget: a cold GitHub CDN fetch of the full bundle takes ~1-2 min.
NFL_DATA_REFRESH_WORKER_TIMEOUT_SECONDS = 300.0
# Smarter NFL PR 6 — single ESPN GET + upsert, same cost profile as
# the NBA / WNBA injury refreshes.
NFL_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS = 60.0
PREDICTION_SETTLEMENT_BATCH_SIZE = 100
PARLAY_SETTLEMENT_BATCH_SIZE = 50
# Bug #22: cap the transient-error requeue loop so one persistent
# upstream outage can't churn the queue forever and drown log signal.
# Counter lives in ``job.details["transient_attempts"]``.
PROP_REFRESH_MAX_TRANSIENT_ATTEMPTS = 5
# Exponential backoff: 2s → 4s → 8s → 16s → 32s, capped at 10 min.
# The claim filter (``queued_at <= now``) actually enforces the wait —
# we just bump ``queued_at`` into the future on requeue.
PROP_REFRESH_BACKOFF_BASE_SECONDS = 2.0
PROP_REFRESH_BACKOFF_CAP_SECONDS = 600.0
PROP_REFRESH_DEAD_LETTER_ERROR = "prop_refresh_dead_letter_after_transient_errors"
# Codex round-3 P2 on PR #47 (bug #22): once a prop_refresh has
# dead-lettered, don't let the next scheduler tick replace it with
# a fresh job. The upstream outage is presumably still happening
# and the new job would walk the same back-off ladder and dead-
# letter again — just spam in the log. Cool the queue for this
# window so the next attempt has a chance of seeing recovery.
PROP_REFRESH_DEAD_LETTER_COOLDOWN_SECONDS = 300.0
# Bug #11: serialize concurrent ``_claim_next_job`` callers on Postgres so two
# workers can't both pass the "is anything running?" check and claim distinct
# queued rows in parallel (which would violate the singleton invariant — only
# one refresh job runs at a time). Derived from
# ``int.from_bytes(sha256(b"sika:refresh_job_claim").digest()[:8], "big",
# signed=True)``; stable across processes and DB restarts.
REFRESH_JOB_CLAIM_LOCK_KEY = -5064315184726640939
# Bug #52: anti-starvation. A long-running settlement (priority 3) that
# re-queues itself every ~40s for hours blocked every prop_refresh (priority
# 5) from being claimed, so the queued prop_refresh aged past
# ``refresh_job_stale_minutes`` (30 min) and was failed with
# ``queue_processor_wedged`` — even though the processor was never wedged;
# it was just busy with higher-priority work. Once a queued job ages past
# this threshold, ``_job_priority_order`` escalates it above all normal
# kind priorities so the next claim picks it. The escalation self-clears
# on requeue (fresh ``queued_at = now``), so settlement still dominates
# steady-state — this is a fairness floor, not a priority inversion.
STARVATION_PRIORITY_ESCALATION_SECONDS = 600.0
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
    for event_row in rows:
        starts_at = _as_utc(event_row.starts_at) if event_row.starts_at else None
        if starts_at is None:
            continue
        tokens = alias_tokens(event_row.name)
        for entry in event_row.participants:
            participant = entry.participant
            tokens.update(
                alias_tokens(participant.display_name, participant.short_name)
            )
        index.append((event_row, starts_at, tokens))
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
    for event_row, starts_at, event_tokens in event_index:
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
            best = (event_row, score, dt)
    return best[0] if best else None


def _extract_probable_pitcher_ids(schedule_payload: dict[str, Any]) -> list[str]:
    """Pull MLB Stats PERSON_IDs for both probable starters from a hydrated schedule.

    The `hydrate=probablePitcher` query string puts each starter under
    ``game.teams.{home,away}.probablePitcher``. We deduplicate across the
    whole slate and return them as strings ready for
    ``warm_mlb_advanced_for_athletes(pitcher_ids=...)``.

    Bug #44 — if MLB Stats stops honoring ``hydrate=probablePitcher``
    (param rename, deprecation, response-shape change), the function
    returns ``[]`` silently and downstream feature emitters get the
    "no signal" default for every pitcher. Counting games seen vs
    pitchers extracted gives us a breadcrumb to spot the regression
    in ops logs rather than discovering it via degraded model
    performance days later.
    """
    seen: set[str] = set()
    out: list[str] = []
    games_seen = 0
    for date_block in (schedule_payload or {}).get("dates") or []:
        for game in date_block.get("games") or []:
            games_seen += 1
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
    if games_seen > 0 and not out:
        logger.warning(
            "MLB schedule payload has %d game(s) but zero probable-pitcher IDs extracted. "
            "Likely the hydrate=probablePitcher contract regressed upstream; verify the "
            "schedule call and the response shape.",
            games_seen,
        )
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
    if job.kind == "nba_injury_refresh":
        return max(default_timeout, NBA_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS)
    if job.kind == "wnba_injury_refresh":
        return max(default_timeout, WNBA_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS)
    if job.kind == "nba_referee_refresh":
        return max(default_timeout, NBA_REFEREE_REFRESH_WORKER_TIMEOUT_SECONDS)
    if job.kind == "nfl_data_refresh":
        return max(default_timeout, NFL_DATA_REFRESH_WORKER_TIMEOUT_SECONDS)
    if job.kind == "nfl_injury_refresh":
        return max(default_timeout, NFL_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS)
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

    if kind == "prop_refresh":
        cooldown = _prop_refresh_dead_letter_cooldown(db)
        if cooldown is not None:
            return cooldown, False

    job = RefreshJob(
        kind=kind,
        scope=scope,
        reason=reason,
        status="queued",
    )
    db.add(job)
    db.flush()
    return job, True


def _prop_refresh_dead_letter_cooldown(
    db: Session, *, now: datetime | None = None
) -> RefreshJob | None:
    """Codex round-3 P2 on PR #47 (bug #22): if the most-recent
    prop_refresh failure carries the dead-letter marker AND finished
    within the cooldown window, refuse to enqueue a fresh
    prop_refresh — return that failed row so the caller treats
    ``created=False``. After the cooldown elapses, the next call
    falls through to a normal enqueue. Returns ``None`` if no recent
    dead-letter exists or the cooldown has elapsed."""
    reference_now = now or datetime.now(timezone.utc)
    cooldown_cutoff = reference_now - timedelta(
        seconds=PROP_REFRESH_DEAD_LETTER_COOLDOWN_SECONDS
    )
    candidate = db.scalar(
        select(RefreshJob)
        .where(
            RefreshJob.kind == "prop_refresh",
            RefreshJob.status == "failed",
            RefreshJob.finished_at >= cooldown_cutoff,
        )
        .order_by(RefreshJob.finished_at.desc().nullslast(), RefreshJob.id.desc())
        .limit(1)
    )
    if candidate is None:
        return None
    if PROP_REFRESH_DEAD_LETTER_ERROR not in (candidate.error_message or ""):
        return None
    return candidate


def enqueue_shadow_capture_job(
    db: Session,
    *,
    scope: str,
    source_run_id: int | None = None,
    source_refresh_job_id: int | None = None,
    source_prop_refresh_job_id: int | None = None,
) -> tuple[RefreshJob, bool]:
    if scope not in {"current_slate", "backfill", DIAGNOSTIC_BACKFILL_CAPTURE_MODE}:
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
        details["diagnostic_backfill"] = scope == DIAGNOSTIC_BACKFILL_CAPTURE_MODE
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


def _job_priority_order(starvation_cutoff: datetime | None = None):
    """Return the SQL priority expression for the claim ``order_by``.

    Smaller value = higher priority. The base ladder is by-kind so
    operator-facing pipelines (``refresh`` / ``shadow_capture`` current
    slate, then ``settlement``) lead background work (``prop_refresh``,
    ``cleanup``, etc.).

    Bug #52: an unbounded backlog on a single higher-priority kind (the
    observed case: 16k+ pending predictions feeding a settlement that
    re-queues itself every ~40s for hours) would otherwise starve every
    lower-priority queued job — eventually tripping ``reconcile_stale_jobs``
    to fail them with ``queue_processor_wedged``. When
    ``starvation_cutoff`` is supplied, any job whose ``queued_at`` is
    older than it is escalated to priority ``-1`` so a starved row gets
    one claim cycle. The escalation self-clears on requeue (workers
    stamp a fresh ``queued_at = now``), so the steady-state ladder is
    unchanged — this is a fairness floor, not a priority inversion.
    """
    branches: list[tuple[Any, int]] = []
    if starvation_cutoff is not None:
        branches.append((RefreshJob.queued_at < starvation_cutoff, -1))
    branches.extend([
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
    ])
    return case(*branches, else_=99)


def _claim_next_job(db: Session) -> RefreshJob | None:
    # Bug #11: take a transaction-scoped advisory lock on Postgres so two
    # workers can't both pass the "is anything running?" check and claim
    # distinct queued rows in parallel. ``with_for_update(skip_locked=True)``
    # below only prevents double-claim of the *same* row — it doesn't
    # enforce the singleton invariant across different queued rows. The
    # lock is released automatically when the transaction commits or
    # rolls back. No-op on SQLite (single-writer DB lock already serializes).
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": REFRESH_JOB_CLAIM_LOCK_KEY})

    running = db.scalar(
        select(RefreshJob)
        .where(RefreshJob.status == "running")
        .order_by(RefreshJob.started_at.asc().nullslast(), RefreshJob.id.asc())
        .limit(1)
    )
    if running is not None:
        return None

    # Bug #22: honor the back-off window stamped onto ``queued_at`` by
    # a transient-error requeue. A future ``queued_at`` means "not
    # eligible yet" — the same row will be picked up on a later tick
    # once the back-off has elapsed.
    now = datetime.now(timezone.utc)
    # Bug #52: any job queued for longer than this cutoff is escalated
    # above all kind priorities so a long-running higher-priority kind
    # (e.g. settlement chewing a 16k-prediction backlog at 40s/batch)
    # can't perpetually starve a lower-priority one. See
    # ``_job_priority_order`` for details.
    starvation_cutoff = now - timedelta(seconds=STARVATION_PRIORITY_ESCALATION_SECONDS)
    queued = db.scalar(
        select(RefreshJob)
        .where(
            RefreshJob.status == "queued",
            RefreshJob.queued_at <= now,
        )
        .order_by(
            _job_priority_order(starvation_cutoff=starvation_cutoff),
            RefreshJob.queued_at.asc(),
            RefreshJob.id.asc(),
        )
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


def _clear_prop_refresh_transient_state(job: RefreshJob) -> None:
    """Bug #22 round-2 P2: reset the counter + telemetry fields a
    transient-error requeue stamped onto ``job.details``. Called
    after every successful ``advance_prop_refresh_job`` batch so the
    dead-letter cap reflects CONSECUTIVE failures, not a running
    lifetime total."""
    details = dict(job.details or {})
    if not details.get("transient_attempts"):
        return
    for key in (
        "transient_attempts",
        "last_transient_error",
        "last_transient_backoff_seconds",
    ):
        details.pop(key, None)
    job.details = details


def _prop_refresh_backoff_seconds(transient_attempts: int) -> float:
    """Bug #22: exponential back-off after a transient HTTP error.
    Caller has just incremented ``transient_attempts``, so attempt N
    delays by ``2^N`` seconds (2, 4, 8, 16, 32, …), capped at the
    ``PROP_REFRESH_BACKOFF_CAP_SECONDS`` ceiling so a long upstream
    outage doesn't strand the job forever."""
    exponent = max(transient_attempts, 0)
    raw = PROP_REFRESH_BACKOFF_BASE_SECONDS * (2 ** max(exponent - 1, 0))
    return min(raw, PROP_REFRESH_BACKOFF_CAP_SECONDS)


def _guarded_requeue_with_backoff(
    db: Session, job_id: int, *, backoff_seconds: float
) -> bool:
    """Bug #22: like ``_guarded_requeue_job`` but pushes ``queued_at``
    into the future by ``backoff_seconds`` so the claim loop skips
    the row until the back-off elapses. Paired with the
    ``queued_at <= now`` filter in ``_claim_next_job``."""
    not_before = datetime.now(timezone.utc) + timedelta(seconds=max(backoff_seconds, 0.0))
    return _guarded_update_running_job(
        db,
        job_id,
        {
            "status": "queued",
            "queued_at": not_before,
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
    diagnostic_backfill = job.scope == DIAGNOSTIC_BACKFILL_CAPTURE_MODE or bool((job.details or {}).get("diagnostic_backfill"))
    run_details = {"shadow_capture_scope": job.scope, "diagnostic_backfill": diagnostic_backfill}
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


def _settlement_processed_so_far(
    *,
    single_summary: dict[str, int],
    parlay_summary: dict[str, int],
    paper_parlay_summary: dict[str, int] | None = None,
) -> int:
    # PAPER_PARLAY_SCOPE.md step 9: include paper-parlay rows in the
    # processed-so-far counter so the operator-facing progress metric
    # reflects all three phases. ``paper_parlay_summary`` defaults to
    # ``None`` so older job rows (queued before this PR) still
    # deserialize correctly via ``_coerce_settlement_summary``.
    base = (
        int(single_summary.get("processed") or 0)
        + int(parlay_summary.get("processed") or 0)
    )
    if paper_parlay_summary is None:
        return base
    return base + int(paper_parlay_summary.get("processed") or 0)


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
    paper_parlay_summary = _coerce_settlement_summary(
        details.get("paper_parlay_settlement_summary")
    )
    run = _ensure_settlement_run(db, job=job)
    batch_started = perf_counter()
    batch_size = 0
    completed = False

    if phase == "single_predictions":
        batch_summary, next_cursor = settle_predictions_batch(
            db,
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
            # PAPER_PARLAY_SCOPE.md step 9 — after auto-generated
            # parlays settle, sweep operator-built paper parlays.
            # Paper parlays don't have cursor batching (volume is
            # tens-per-day, not thousands), so one phase, one pass.
            phase = "paper_parlays"
            cursor = None
        else:
            cursor = next_cursor
    elif phase == "paper_parlays":
        # Single-pass: settle_paper_parlays processes every pending
        # row in one go. Codex pattern 6 (implicit data shape): an
        # operator-built parlay surface is small enough not to need
        # batching for the next few months; revisit when volume
        # justifies a cursor.
        paper_batch_summary = settle_paper_parlays(db)
        paper_parlay_summary = _merge_settlement_summaries(
            paper_parlay_summary, paper_batch_summary
        )
        batch_size = int(paper_batch_summary.get("processed", 0))
        completed = True
        cursor = None
    else:
        raise ValueError(f"Unsupported settlement phase: {phase}")

    processed_so_far = _settlement_processed_so_far(
        single_summary=single_summary,
        parlay_summary=parlay_summary,
        paper_parlay_summary=paper_parlay_summary,
    )
    details.update(
        {
            "phase": phase,
            "cursor": cursor or {},
            "single_settlement_summary": single_summary,
            "parlay_settlement_summary": parlay_summary,
            "paper_parlay_settlement_summary": paper_parlay_summary,
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
        "paper_parlay_settlement_summary": paper_parlay_summary,
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
            "paper_parlay_settlement_summary": paper_parlay_summary,
            "processed_so_far": processed_so_far,
        }
    db.flush()
    return run, completed


class WorkerCancelledError(BaseException):
    """Raised by ``_cancel_check`` when the worker thread's cancel event is
    set. Inherits from ``BaseException`` so ``except Exception`` blocks
    inside ``_execute_claimed_job`` don't swallow it — cancellation must
    propagate cleanly to the worker thread boundary.

    Bug #10: when ``process_refresh_job_queue_once`` times out on a
    worker, the main thread sets the cancel event. The next
    ``Session.commit()`` anywhere in the worker's call stack hits the
    ``before_commit`` hook below, raises this error, SQLAlchemy rolls
    back the transaction, and no stale state lands in the database.
    """


# Thread-local state — the cancel event for the current worker thread.
# ``before_commit`` checks this and only fires for threads that opted in.
_thread_state = threading.local()


@event.listens_for(Session, "before_commit", propagate=True)
def _cancel_check_before_commit(session: Session) -> None:
    cancel_event = getattr(_thread_state, "cancel_event", None)
    if cancel_event is not None and cancel_event.is_set():
        raise WorkerCancelledError(
            "Refresh worker cancelled after main-thread timeout; commit suppressed."
        )


def _apply_worker_statement_timeout(db: Session) -> None:
    """Bug #51 / connection-pool-leak: bound every individual DB statement
    issued by the refresh worker so a single slow query can't pin the
    worker thread (and its pooled connection) indefinitely.

    ``SET LOCAL`` only applies to the CURRENT transaction — the worker
    commits many times during a job (per stage / per batch), and each
    commit drops the LOCAL setting. Without the listener below the
    second-and-onward transactions ran with no timeout, so a slow query
    after the first commit could pin the worker indefinitely. When the
    main thread's timeout fired, the worker stayed alive (no Python-level
    interrupt is possible during a blocking DB call) and kept its pooled
    connection. Every subsequent tick spawned another worker, each one
    leaking another connection, until the pool was exhausted.

    Fix: apply the SET LOCAL on the current transaction AND register an
    ``after_begin`` listener on this specific session so every new
    transaction within the session re-applies the timeout. The listener
    is scoped to ``db`` (not the global ``Session`` class) so request
    sessions and tests are unaffected.

    No-op for non-Postgres dialects (SQLite has no equivalent; tests
    use SQLite and remain unaffected).
    """
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    timeout_seconds = int(get_settings().refresh_worker_statement_timeout_seconds)
    if timeout_seconds <= 0:
        return
    # SET LOCAL takes integer milliseconds. Use parameter binding on the
    # SET value via a literal — Postgres' SET syntax doesn't accept bind
    # params, but the value is operator-controlled config (not user input)
    # so f-string interpolation of an int is safe.
    timeout_ms = timeout_seconds * 1000
    set_local_sql = text(f"SET LOCAL statement_timeout = {timeout_ms}")
    db.execute(set_local_sql)

    def _reapply_on_begin(_session, _transaction, connection) -> None:
        # Re-apply for every new transaction in this session. Use the
        # provided ``connection`` (per SQLAlchemy docs — invoking SQL
        # via the session itself inside ``after_begin`` is unsafe).
        connection.execute(set_local_sql)

    event.listen(db, "after_begin", _reapply_on_begin)


def _execute_job_in_thread(
    job_id: int,
    done_event: threading.Event,
    result_holder: dict[str, RefreshJobSnapshot | BaseException | None],
    cancel_event: threading.Event,
) -> None:
    _thread_state.cancel_event = cancel_event
    try:
        result_holder["snapshot"] = _execute_claimed_job(job_id)
    except WorkerCancelledError:
        # Graceful cancellation — the main thread already failed the job
        # and won't read the snapshot. Don't pollute result_holder.
        pass
    except BaseException as exc:  # pragma: no cover - defensive thread boundary
        result_holder["exception"] = exc
    finally:
        _thread_state.cancel_event = None
        done_event.set()


def process_refresh_job_queue_once() -> RefreshJobSnapshot | None:
    with SessionLocal() as db:
        # Bug #51: bound the main-thread DB statements too. The advisory
        # lock in ``_claim_next_job`` and the iteration in
        # ``reconcile_stale_jobs`` both run on the scheduler's calling
        # thread; if either hangs (lock contention, slow scan) the
        # APScheduler tick never completes and ``max_instances=1``
        # silently skips every subsequent tick.
        _apply_worker_statement_timeout(db)
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
    cancel_event = threading.Event()
    result_holder: dict[str, RefreshJobSnapshot | BaseException | None] = {
        "snapshot": None,
        "exception": None,
    }
    worker = threading.Thread(
        target=_execute_job_in_thread,
        args=(job_id, done_event, result_holder, cancel_event),
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

    # Bug #10: timeout fired. Signal the worker to abort the next commit
    # via the before_commit hook — any in-flight work it tries to persist
    # after this point rolls back instead of silently leaking through.
    cancel_event.set()

    with SessionLocal() as db:
        # Bug #51: bound the cleanup-path statements too — see the
        # equivalent comment at the top of this function.
        _apply_worker_statement_timeout(db)
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
        # Bug #51: cap individual DB statements so a slow query can't
        # pin this worker thread (and its pooled connection)
        # indefinitely. Must be applied BEFORE any other DB work so the
        # very first query (``db.get`` below) is also bounded.
        _apply_worker_statement_timeout(db)
        job = db.get(RefreshJob, job_id)
        if job is None:
            return None
        try:
            if job.kind == "refresh":
                if job.scope == "current_slate":
                    run, completed = advance_current_slate_refresh_job(
                        db,
                        job=job,
                        sports=["NBA", "MLB", "WNBA"],
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
                    # Bug #22 round-2 P2: ``transient_attempts`` bounds
                    # CONSECUTIVE transient HTTP errors — not the
                    # lifetime total. A long-running prop_refresh that
                    # hits 5 intermittent blips spaced across many
                    # successful batches would otherwise dead-letter
                    # even though the upstream isn't persistently
                    # broken. Clear the counter (and its sibling
                    # telemetry fields) on any successful batch advance.
                    _clear_prop_refresh_transient_state(job)
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
                diagnostic_backfill = job.scope == DIAGNOSTIC_BACKFILL_CAPTURE_MODE or bool(details.get("diagnostic_backfill"))
                batch_started = datetime.now(timezone.utc)
                batch = capture_shadow_artifacts_batch(
                    db,
                    run_id=run.id,
                    source_run_id=source_run_id,
                    backfill=job.scope in {"backfill", DIAGNOSTIC_BACKFILL_CAPTURE_MODE} or diagnostic_backfill,
                    diagnostic_backfill=diagnostic_backfill,
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
                        "diagnostic_backfill": diagnostic_backfill,
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
                    "diagnostic_backfill": diagnostic_backfill,
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
                        "diagnostic_backfill": diagnostic_backfill,
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
                # Smarter #15 — game-day morning weather pre-warm.
                #
                # Walks today's MLB slate, matches each MLB Stats game to a
                # sika ``Event``, looks up the home park's coordinates +
                # dome flag, and calls ``load_weather(allow_network=True)``
                # to populate ``MlbWeatherCache``. The synchronous scoring
                # path (``load_weather(allow_network=False)``) then serves
                # the cached payload without paying the upstream latency
                # on the first scored prop of the day.
                from datetime import date as _date

                from app.clients.mlb_stats import MlbStatsClient
                from app.services.mlb_advanced import load_weather, mlb_park_coords

                target = _date.today()
                events_warmed = 0
                events_dome = 0
                events_missing_coords = 0
                games_unmatched = 0
                games_failed = 0
                schedule_failed = False
                try:
                    mlb_client = MlbStatsClient()
                    schedule = mlb_client.fetch_schedule(target)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("weather_refresh schedule fetch failed: %s", exc)
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
                                home_team = (
                                    ((game.get("teams") or {}).get("home") or {}).get("team")
                                    or {}
                                )
                                home_abbr = home_team.get("abbreviation")
                                coords = mlb_park_coords(home_abbr)
                                if coords is None:
                                    events_missing_coords += 1
                                    continue
                                lat, lon, is_dome = coords
                                starts_at = event.starts_at
                                if starts_at is not None and starts_at.tzinfo is None:
                                    starts_at = starts_at.replace(tzinfo=timezone.utc)
                                game_time_utc = (
                                    starts_at.astimezone(timezone.utc) if starts_at else None
                                )
                                result = load_weather(
                                    db,
                                    event_id=str(event.id),
                                    lat=lat,
                                    lon=lon,
                                    game_time_utc=game_time_utc,
                                    is_dome=is_dome,
                                    allow_network=True,
                                )
                                if is_dome and result.cache_status == "dome":
                                    events_dome += 1
                                elif result.complete:
                                    events_warmed += 1
                            except Exception as exc:  # noqa: BLE001
                                # One bad game must not poison the slate.
                                games_failed += 1
                                logger.warning(
                                    "weather_refresh per-game failure (gamePk=%s): %s",
                                    game_pk,
                                    exc,
                                )

                job.details = {
                    **(job.details or {}),
                    "events_warmed": events_warmed,
                    "events_dome": events_dome,
                    "events_missing_coords": events_missing_coords,
                    "games_unmatched": games_unmatched,
                    "games_failed": games_failed,
                    "schedule_failed": schedule_failed,
                }
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
            elif job.kind == "nba_injury_refresh":
                # Smarter #17 phase 2-2 — populates ``NbaInjuryReportCache``
                # so the scoring-side ``emit_nba_injury_features`` gate
                # (Smarter #17 phase 1) has data to suppress on. The
                # loader is its own cache-or-fetch shape (handles upstream-
                # health recording + the savepoint+IntegrityError race
                # retry) so the job-kind handler is a one-liner.
                from app.services.nba_injury_report import load_nba_injury_report

                payload = load_nba_injury_report(db, allow_network=True)
                job.details = {
                    **(job.details or {}),
                    "players": len((payload or {}).get("players") or {}),
                    "report_updated_at": (payload or {}).get("report_updated_at"),
                }
            elif job.kind == "wnba_injury_refresh":
                # Smarter WNBA PR 7 — populates ``WnbaInjuryReportCache``
                # so the scoring-side ``wnba_injury`` SUPPRESS gate has
                # data to suppress on. Mirrors the NBA shape exactly;
                # separate loader so per-sport cache races and upstream-
                # health buckets stay isolated.
                from app.services.wnba_injury_report import load_wnba_injury_report

                payload = load_wnba_injury_report(db, allow_network=True)
                job.details = {
                    **(job.details or {}),
                    "players": len((payload or {}).get("players") or {}),
                    "report_updated_at": (payload or {}).get("report_updated_at"),
                }
            elif job.kind == "nfl_injury_refresh":
                # Smarter NFL PR 6 — populates ``NflInjuryReportCache``
                # (the intraday ESPN supplement; the official weekly
                # report rides the nfl_data_refresh bundle). Mirrors
                # the NBA / WNBA shape.
                from app.services.nfl_injury_report import load_nfl_injury_report

                payload = load_nfl_injury_report(db, allow_network=True)
                job.details = {
                    **(job.details or {}),
                    "players": len((payload or {}).get("players") or {}),
                    "report_updated_at": (payload or {}).get("report_updated_at"),
                }
            elif job.kind == "nfl_data_refresh":
                # Smarter NFL PR 3 — refresh the whole nflverse bundle
                # (weekly stats / snap counts / depth charts / official
                # injuries / team ratings / schedule) + weather prewarm.
                # ``refresh_nfl_data`` degrades per-dataset: one failed
                # download doesn't abort the rest, and each failure is
                # recorded on the upstream-health board.
                from app.services.nfl_advanced import refresh_nfl_data

                summary = refresh_nfl_data(db)
                job.details = {
                    **(job.details or {}),
                    "season": summary.get("season"),
                    "weekly_stats_weeks": summary.get("weekly_stats_weeks"),
                    "snap_count_weeks": summary.get("snap_count_weeks"),
                    "depth_chart_teams": summary.get("depth_chart_teams"),
                    "official_injury_weeks": summary.get("official_injury_weeks"),
                    "rated_teams": summary.get("rated_teams"),
                    "schedule_games": summary.get("schedule_games"),
                    "weather_prewarmed": summary.get("weather_prewarmed"),
                    "errors": summary.get("errors") or [],
                }
            elif job.kind == "nba_referee_refresh":
                # Smarter #13 phase 2a-2 + 2b-2 — populates BOTH the
                # daily ``NbaRefereeAssignmentCache`` AND the per-season
                # referee tendency cache (the latter from BR's
                # ``/referees/{end_year}_register.html`` table). The
                # two are conceptually linked (both about NBA
                # officiating) and refresh on the same daily cadence,
                # so bundling avoids a separate job kind + scheduler
                # entry for what would be one extra call.
                from app.clients.basketball_reference import (
                    BasketballReferenceClient,
                )
                from app.services.nba_referee_assignments import (
                    load_nba_referee_assignments,
                )
                from app.services.nba_referee_tendencies import (
                    load_nba_referee_tendencies,
                )
                from app.services.stats_query import default_season_for_sport

                payload = load_nba_referee_assignments(db, allow_network=True)
                # Phase 2b-2: tendency refresh — fetcher is the new
                # ``fetch_referee_season_stats`` method on the BR
                # client (Smarter #13 phase 2b-2). Anonymous BR fetches
                # return 403 from fresh IPs; the operator's
                # ``basketball_reference_base_url`` config governs
                # whether this succeeds. On 403/404 the fetcher
                # returns ``[]`` and the loader stores an empty
                # tendency payload — graceful degradation.
                br_client = BasketballReferenceClient()
                season = default_season_for_sport("NBA")
                tendencies_payload = load_nba_referee_tendencies(
                    db,
                    season=season,
                    fetcher=br_client.fetch_referee_season_stats,
                    allow_network=True,
                )
                job.details = {
                    **(job.details or {}),
                    "assignments": len((payload or {}).get("assignments") or []),
                    "page_date": (payload or {}).get("page_date"),
                    "tendency_season": season,
                    "tendency_referees": len(
                        (tendencies_payload or {}).get("referees") or {}
                    ),
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
            # The work above may have failed mid-flush (an IntegrityError, a
            # SQLite lock, or the Postgres statement-timeout cancellation),
            # leaving the session in a failed transaction. Roll back first so
            # the failure bookkeeping below can issue SQL instead of raising
            # PendingRollbackError and wedging the job in 'running' (which then
            # blocks the singleton queue and masks the real error as a timeout).
            db.rollback()
            if job.kind == "prop_refresh" and isinstance(exc, httpx.HTTPError):
                # Bug #22: bound the transient-error requeue loop.
                # Previously every ``httpx.HTTPError`` requeued the job
                # immediately, so one persistent upstream outage churned
                # the queue forever and drowned out the rest of the
                # log signal. Cap the attempts and back off the requeue
                # window; dead-letter after the cap.
                details = dict(job.details or {})
                attempts = int(details.get("transient_attempts") or 0) + 1
                details["transient_attempts"] = attempts
                details["last_transient_error"] = str(exc).strip() or exc.__class__.__name__
                job.details = details

                if attempts >= PROP_REFRESH_MAX_TRANSIENT_ATTEMPTS:
                    error_message = (
                        f"{PROP_REFRESH_DEAD_LETTER_ERROR}: {details['last_transient_error']}"
                    )
                    if job.run_id is not None:
                        _fail_run(db, job.run_id, error_message)
                    db.flush()
                    failed = _guarded_fail_job(db, job.id, error_message)
                    if not failed:
                        logger.warning(
                            "refresh_job_late_failure_ignored", extra={"job_id": job.id}
                        )
                    else:
                        logger.warning(
                            "prop_refresh_dead_letter",
                            extra={
                                "job_id": job.id,
                                "attempts": attempts,
                                "last_error": details["last_transient_error"],
                            },
                        )
                    db.commit()
                    return _refresh_job_snapshot(db, job.id)

                backoff_seconds = _prop_refresh_backoff_seconds(attempts)
                details["last_transient_backoff_seconds"] = backoff_seconds
                job.details = details
                db.flush()
                requeued = _guarded_requeue_with_backoff(
                    db, job.id, backoff_seconds=backoff_seconds
                )
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
