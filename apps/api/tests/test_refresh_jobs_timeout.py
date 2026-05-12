from __future__ import annotations

import threading
import unittest.mock
from datetime import datetime, timedelta, timezone
from time import monotonic
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import RefreshJob, Run
from app.services import refresh_jobs


def _install_threaded_session_factory(db_session, monkeypatch, seen_sessions=None):
    testing_session_local = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=db_session.get_bind(),
        future=True,
    )

    def _session_factory():
        session = testing_session_local()
        if seen_sessions is not None:
            seen_sessions.append((threading.get_ident(), id(session)))
        return session

    monkeypatch.setattr(refresh_jobs, "SessionLocal", _session_factory)


def _fast_timeout_settings(monkeypatch):
    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(maintenance_claim_budget_seconds=0.05, refresh_job_stale_minutes=30),
    )
    monkeypatch.setattr(refresh_jobs, "WORKER_TIMEOUT_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(refresh_jobs, "PROP_REFRESH_WORKER_TIMEOUT_SECONDS", 0.35)


def _queued_prop_job(db_session, *, reason="interval") -> RefreshJob:
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason=reason,
        status="queued",
    )
    db_session.add(job)
    db_session.commit()
    return job


def _queued_current_slate_job(db_session, *, reason="interval") -> RefreshJob:
    job = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason=reason,
        status="queued",
    )
    db_session.add(job)
    db_session.commit()
    return job


def _completed_run(db, *, kind="prop_refresh") -> Run:
    run = Run(kind=kind, status="completed")
    db.add(run)
    db.flush()
    return run


def _wait_for_run_status(db_session, run_id: int, status: str, *, timeout: float = 1.0) -> Run:
    deadline = monotonic() + timeout
    while True:
        db_session.expire_all()
        run = db_session.get(Run, run_id)
        if run is not None and run.status == status:
            return run
        if monotonic() >= deadline:
            assert run is not None
            assert run.status == status
        threading.Event().wait(timeout=0.01)


def test_worker_timeout_releases_claim(db_session, monkeypatch):
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)
    entered = threading.Event()
    release = threading.Event()

    def _advance(db, job):
        entered.set()
        release.wait(timeout=5)
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    started = monotonic()
    result = refresh_jobs.process_refresh_job_queue_once()
    elapsed = monotonic() - started

    assert entered.wait(timeout=1)
    assert elapsed < 1
    assert result is not None
    assert result.job_id == job.id
    assert result.status == "failed"
    assert result.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed"
    assert persisted.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR
    release.set()


def test_frozen_worker_does_not_block_next_tick(db_session, monkeypatch):
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    first = _queued_prop_job(db_session, reason="first")
    second = _queued_prop_job(db_session, reason="second")
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    worker_threads: list[int] = []

    def _advance(db, job):
        worker_threads.append(threading.get_ident())
        if job.id == first.id:
            first_entered.set()
            release_first.wait(timeout=5)
            return _completed_run(db), True
        second_entered.set()
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    first_result = refresh_jobs.process_refresh_job_queue_once()
    second_result = refresh_jobs.process_refresh_job_queue_once()

    assert first_entered.wait(timeout=1)
    assert second_entered.wait(timeout=1)
    assert first_result is not None
    assert first_result.job_id == first.id
    assert first_result.status == "failed"
    assert second_result is not None
    assert second_result.job_id == second.id
    assert second_result.status == "completed"
    assert len(set(worker_threads)) >= 2
    db_session.expire_all()
    assert db_session.get(RefreshJob, first.id).status == "failed"
    assert db_session.get(RefreshJob, second.id).status == "completed"
    release_first.set()


def test_late_worker_completion_does_not_resurrect(db_session, monkeypatch):
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)
    entered = threading.Event()
    release = threading.Event()
    complete_attempted = threading.Event()
    run_fail_attempted = threading.Event()
    complete_result = {"value": None}
    original_complete = refresh_jobs._guarded_complete_job
    original_fail_run = refresh_jobs._fail_run

    def _advance(db, job):
        entered.set()
        release.wait(timeout=5)
        return _completed_run(db), True

    def _guarded_complete(db, job_id):
        complete_result["value"] = original_complete(db, job_id)
        complete_attempted.set()
        return bool(complete_result["value"])

    def _fail_run(db, run_id, error_message, *, finished_at=None, only_running=True):
        result = original_fail_run(
            db,
            run_id,
            error_message,
            finished_at=finished_at,
            only_running=only_running,
        )
        run_fail_attempted.set()
        return result

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)
    monkeypatch.setattr(refresh_jobs, "_guarded_complete_job", _guarded_complete)
    monkeypatch.setattr(refresh_jobs, "_fail_run", _fail_run)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert entered.wait(timeout=1)
    assert result is not None
    assert result.status == "failed"
    release.set()
    assert complete_attempted.wait(timeout=1)
    assert run_fail_attempted.wait(timeout=1)
    assert complete_result["value"] is False
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed"
    assert persisted.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR
    persisted_run = db_session.query(Run).order_by(Run.id.desc()).first()
    persisted_run = _wait_for_run_status(db_session, persisted_run.id, "failed")
    assert persisted_run.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR


def test_late_worker_requeue_does_not_resurrect(db_session, monkeypatch):
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)
    entered = threading.Event()
    release = threading.Event()
    requeue_attempted = threading.Event()
    requeue_result = {"value": None}
    original_requeue = refresh_jobs._guarded_requeue_job

    def _advance(db, job):
        entered.set()
        release.wait(timeout=5)
        job.details = {"phase": "watchlist_score_batch", "cursor": {"market_id": 808}}
        return _completed_run(db), False

    def _guarded_requeue(db, job_id):
        requeue_result["value"] = original_requeue(db, job_id)
        requeue_attempted.set()
        return bool(requeue_result["value"])

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)
    monkeypatch.setattr(refresh_jobs, "_guarded_requeue_job", _guarded_requeue)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert entered.wait(timeout=1)
    assert result is not None
    assert result.status == "failed"
    release.set()
    assert requeue_attempted.wait(timeout=1)
    assert requeue_result["value"] is False
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed"
    assert persisted.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR


def test_sessions_are_not_shared_across_threads(db_session, monkeypatch):
    seen_sessions: list[tuple[int, int]] = []
    _install_threaded_session_factory(db_session, monkeypatch, seen_sessions=seen_sessions)
    _fast_timeout_settings(monkeypatch)
    _queued_prop_job(db_session)

    def _advance(db, job):
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.status == "completed"
    parent_thread = threading.get_ident()
    parent_sessions = {session_id for thread_id, session_id in seen_sessions if thread_id == parent_thread}
    worker_sessions = {session_id for thread_id, session_id in seen_sessions if thread_id != parent_thread}
    assert parent_sessions
    assert worker_sessions
    assert parent_sessions.isdisjoint(worker_sessions)


def test_normal_path_unchanged(db_session, monkeypatch):
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)

    def _advance(db, job):
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.job_id == job.id
    assert result.kind == "prop_refresh"
    assert result.status == "completed"
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "completed"
    assert persisted.finished_at is not None
    shadow_job = db_session.query(RefreshJob).filter_by(kind="shadow_capture", scope="backfill").one()
    assert shadow_job.status == "queued"
    assert shadow_job.details["source_prop_refresh_job_id"] == job.id


def test_current_slate_refresh_gets_longer_worker_timeout(db_session, monkeypatch):
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    monkeypatch.setattr(refresh_jobs, "CURRENT_SLATE_WORKER_TIMEOUT_SECONDS", 0.8)
    job = _queued_current_slate_job(db_session)
    entered = threading.Event()

    def _advance(db, *, job, sports):
        entered.set()
        threading.Event().wait(timeout=0.45)
        return _completed_run(db, kind="refresh"), True

    monkeypatch.setattr(refresh_jobs, "advance_current_slate_refresh_job", _advance)

    started = monotonic()
    result = refresh_jobs.process_refresh_job_queue_once()
    elapsed = monotonic() - started

    assert entered.wait(timeout=1)
    assert elapsed >= 0.4
    assert result is not None
    assert result.job_id == job.id
    assert result.kind == "refresh"
    assert result.status == "completed"
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "completed"


def test_settlement_gets_longer_worker_timeout(monkeypatch):
    _fast_timeout_settings(monkeypatch)
    monkeypatch.setattr(refresh_jobs, "SETTLEMENT_WORKER_TIMEOUT_SECONDS", 0.8)
    job = RefreshJob(
        kind="settlement",
        scope="predictions",
        reason="interval",
        status="running",
    )

    assert refresh_jobs._worker_timeout_seconds(job) == 0.8


def test_prop_refresh_gets_longer_worker_timeout(monkeypatch):
    _fast_timeout_settings(monkeypatch)
    monkeypatch.setattr(refresh_jobs, "PROP_REFRESH_WORKER_TIMEOUT_SECONDS", 0.8)
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="running",
    )

    assert refresh_jobs._worker_timeout_seconds(job) == 0.8


def test_advanced_stats_warm_gets_longer_worker_timeout(monkeypatch):
    _fast_timeout_settings(monkeypatch)
    monkeypatch.setattr(refresh_jobs, "ADVANCED_STATS_WARM_WORKER_TIMEOUT_SECONDS", 0.8)
    job = RefreshJob(
        kind="advanced_stats_warm",
        scope="maintenance",
        reason="interval",
        status="running",
    )

    assert refresh_jobs._worker_timeout_seconds(job) == 0.8


def test_market_discovery_gets_longer_worker_timeout(monkeypatch):
    _fast_timeout_settings(monkeypatch)
    monkeypatch.setattr(refresh_jobs, "MARKET_DISCOVERY_WORKER_TIMEOUT_SECONDS", 0.8)
    job = RefreshJob(
        kind="market_discovery",
        scope="maintenance",
        reason="interval",
        status="running",
    )

    assert refresh_jobs._worker_timeout_seconds(job) == 0.8


def test_lineup_refresh_gets_longer_worker_timeout(monkeypatch):
    _fast_timeout_settings(monkeypatch)
    monkeypatch.setattr(refresh_jobs, "LINEUP_REFRESH_WORKER_TIMEOUT_SECONDS", 0.8)
    job = RefreshJob(
        kind="lineup_refresh",
        scope="maintenance",
        reason="interval",
        status="running",
    )

    assert refresh_jobs._worker_timeout_seconds(job) == 0.8


def test_cleanup_gets_longer_worker_timeout(monkeypatch):
    _fast_timeout_settings(monkeypatch)
    monkeypatch.setattr(refresh_jobs, "CLEANUP_WORKER_TIMEOUT_SECONDS", 0.8)
    job = RefreshJob(
        kind="cleanup",
        scope="maintenance",
        reason="interval",
        status="running",
    )

    assert refresh_jobs._worker_timeout_seconds(job) == 0.8


def test_reconcile_marks_queued_age_with_wedged_reason(db_session, monkeypatch):
    """A queued job that aged past stale-minutes had no processor pick it up
    (queue processor is wedged). This is distinct from a worker that started
    but never finished — split the bucket so dashboards can grep."""
    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(maintenance_claim_budget_seconds=0.05, refresh_job_stale_minutes=30),
    )
    now = datetime.now(timezone.utc)
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="queued",
        queued_at=now - timedelta(minutes=31),
    )
    db_session.add(job)
    db_session.commit()

    reconciled = refresh_jobs.reconcile_stale_jobs(db_session, now=now)

    assert reconciled == 1
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed"
    assert persisted.error_message == refresh_jobs.QUEUE_PROCESSOR_WEDGED


def test_claim_next_job_takes_advisory_lock_on_postgres():
    """Bug #11: on Postgres, ``_claim_next_job`` must call
    ``pg_advisory_xact_lock`` BEFORE the running-state check so two
    concurrent processors can't both pass the check and claim distinct
    queued rows in parallel. ``with_for_update(skip_locked=True)`` below
    only protects against double-claim of the *same* row.

    Unit-level: mocks the dialect so the test runs under SQLite. The
    real concurrent-claim scenario on Postgres is covered by
    ``test_singleton_claim_under_concurrent_processors``.
    """
    mock_db = unittest.mock.MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.scalar.return_value = None  # exit at running-check

    refresh_jobs._claim_next_job(mock_db)

    executed_sql = [str(call.args[0]) for call in mock_db.execute.call_args_list]
    assert any("pg_advisory_xact_lock" in sql for sql in executed_sql), (
        f"expected pg_advisory_xact_lock in executed SQL, got: {executed_sql}"
    )


def test_claim_next_job_skips_advisory_lock_on_sqlite():
    """SQLite serializes writes at the database level, and
    ``pg_advisory_xact_lock`` doesn't exist there. The dialect guard in
    ``_claim_next_job`` must skip the lock for non-Postgres backends.
    """
    mock_db = unittest.mock.MagicMock()
    mock_db.bind.dialect.name = "sqlite"
    mock_db.scalar.return_value = None

    refresh_jobs._claim_next_job(mock_db)

    executed_sql = [str(call.args[0]) for call in mock_db.execute.call_args_list]
    assert not any("pg_advisory" in sql for sql in executed_sql)


def test_singleton_claim_under_concurrent_processors(db_session, monkeypatch):
    """Bug #11: two concurrent processors that both see ``running == 0``
    used to claim distinct queued rows in parallel, violating the
    singleton invariant (one running job at a time).
    ``with_for_update(skip_locked=True)`` only prevents double-claim of
    the same row; the advisory lock in ``_claim_next_job`` is what
    serializes the running-check + claim across rows.

    Postgres-only: SQLite serializes writes at the DB level so the race
    can't be reliably reproduced there.
    """
    pytest.importorskip("psycopg")
    bind = db_session.get_bind()
    if bind.dialect.name != "postgresql":
        pytest.skip("advisory lock is postgres-only")

    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job_a = _queued_prop_job(db_session, reason="a")
    job_b = _queued_prop_job(db_session, reason="b")
    release = threading.Event()

    def _advance(db, job):
        release.wait(timeout=5)
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    results: dict[str, refresh_jobs.RefreshJobSnapshot | None] = {}

    def _runner(key: str) -> None:
        results[key] = refresh_jobs.process_refresh_job_queue_once()

    t1 = threading.Thread(target=_runner, args=("a",))
    t2 = threading.Thread(target=_runner, args=("b",))
    t1.start()
    t2.start()
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    successful = [snapshot for snapshot in results.values() if snapshot is not None]
    assert len(successful) == 1, (
        f"singleton invariant violated: {len(successful)} processors claimed concurrently"
    )
    db_session.expire_all()
    persisted_a = db_session.get(RefreshJob, job_a.id)
    persisted_b = db_session.get(RefreshJob, job_b.id)
    statuses = sorted([persisted_a.status, persisted_b.status])
    assert statuses == ["completed", "queued"], (
        f"expected one completed + one queued, got {statuses}"
    )


def test_claim_skips_locked_under_concurrent_processors(db_session, monkeypatch):
    """SELECT ... FOR UPDATE SKIP LOCKED ensures two concurrent processors
    don't both claim the same queued row. SQLite ignores ``with_for_update``,
    so this test only enforces the postgres-level guarantee — under SQLite
    we just verify the two-threaded path completes without crashing."""
    pytest.importorskip("psycopg")
    bind = db_session.get_bind()
    if bind.dialect.name != "postgresql":
        pytest.skip("with_for_update / skip_locked is only enforced under postgres")

    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)
    seen_job_ids: list[int] = []
    barrier = threading.Barrier(2)
    release = threading.Event()

    def _advance(db, job):
        seen_job_ids.append(job.id)
        barrier.wait(timeout=2)
        release.wait(timeout=5)
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    results: dict[str, refresh_jobs.RefreshJobSnapshot | None] = {}

    def _runner(key: str) -> None:
        results[key] = refresh_jobs.process_refresh_job_queue_once()

    t1 = threading.Thread(target=_runner, args=("a",))
    t2 = threading.Thread(target=_runner, args=("b",))
    t1.start()
    t2.start()
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    claims = [snapshot for snapshot in results.values() if snapshot is not None]
    assert len(claims) == 1, "exactly one processor should have claimed the row"
    assert claims[0].job_id == job.id
    assert seen_job_ids == [job.id]


def test_reconcile_marks_stale_running_with_legacy_stale_reason(db_session, monkeypatch):
    """A running job stale by stale-minutes but inside per-kind timeout gets
    the legacy ``STALE_REFRESH_JOB_ERROR`` bucket (third case)."""
    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(maintenance_claim_budget_seconds=0.05, refresh_job_stale_minutes=30),
    )
    monkeypatch.setattr(refresh_jobs, "PROP_REFRESH_WORKER_TIMEOUT_SECONDS", 3600.0)
    now = datetime.now(timezone.utc)
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="running",
        queued_at=now - timedelta(minutes=32),
        started_at=now - timedelta(minutes=31),
    )
    db_session.add(job)
    db_session.commit()

    reconciled = refresh_jobs.reconcile_stale_jobs(db_session, now=now)

    assert reconciled == 1
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed"
    assert persisted.error_message == refresh_jobs.STALE_REFRESH_JOB_ERROR


def test_reconcile_marks_orphaned_running_job_worker_timeout(db_session, monkeypatch):
    _fast_timeout_settings(monkeypatch)
    now = datetime.now(timezone.utc)
    run = Run(
        kind="prop_refresh",
        status="running",
        started_at=now - timedelta(seconds=1),
    )
    db_session.add(run)
    db_session.flush()
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="running",
        run_id=run.id,
        queued_at=now - timedelta(seconds=5),
        started_at=now - timedelta(seconds=1),
    )
    db_session.add(job)
    db_session.commit()

    reconciled = refresh_jobs.reconcile_stale_jobs(db_session, now=now)

    assert reconciled == 1
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed"
    assert persisted.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR
    persisted_run = db_session.get(Run, run.id)
    assert persisted_run.status == "failed"
    assert persisted_run.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR


def test_worker_timeout_marks_visible_associated_run_failed(db_session, monkeypatch):
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)
    entered = threading.Event()
    release = threading.Event()
    complete_attempted = threading.Event()
    run_id = {"value": None}
    original_complete = refresh_jobs._guarded_complete_job

    def _advance(db, job):
        run = Run(kind="prop_refresh", status="running")
        db.add(run)
        db.flush()
        job.run_id = run.id
        run_id["value"] = run.id
        db.commit()
        entered.set()
        release.wait(timeout=5)
        return run, True

    def _guarded_complete(db, job_id):
        result = original_complete(db, job_id)
        complete_attempted.set()
        return result

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)
    monkeypatch.setattr(refresh_jobs, "_guarded_complete_job", _guarded_complete)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert entered.wait(timeout=1)
    assert result is not None
    assert result.status == "failed"
    db_session.expire_all()
    persisted_run = db_session.get(Run, run_id["value"])
    assert persisted_run.status == "failed"
    assert persisted_run.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR
    release.set()
    assert complete_attempted.wait(timeout=1)
    persisted_run = _wait_for_run_status(db_session, run_id["value"], "failed")
    assert persisted_run.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR
