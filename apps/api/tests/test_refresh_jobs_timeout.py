from __future__ import annotations

import threading
import unittest.mock
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from time import monotonic
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import RefreshJob, Run
from app.services import refresh_jobs


@pytest.fixture()
def db_session(tmp_path) -> Generator[Session, None, None]:
    """Override the global ``db_session`` fixture for this file.

    Bug #10's cancellation hook only correctly rolls back a worker's flushed
    writes when the worker's commit attempt runs against its OWN connection
    — otherwise a parallel main-thread commit on a *shared* connection will
    have already absorbed the worker's uncommitted writes into the
    connection's transaction and committed them, so the worker's later
    ``before_commit`` cancellation has nothing to roll back.

    The global ``db_session`` fixture uses an in-memory SQLite with
    ``StaticPool`` — every session shares one connection. That's fine for
    99% of the suite but invalid for these threaded bug-#10 scenarios.
    Here we use a per-test temp-file SQLite so each SQLAlchemy session (one
    per thread, since the threaded factory creates its own) checks out its
    own real connection — matching production semantics where the
    cancellation hook actually works.
    """

    db_path = tmp_path / "bug10_refresh_jobs.sqlite"
    url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine, future=True
    )
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


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


def test_worker_commits_after_timeout_are_suppressed(db_session, monkeypatch):
    """Bug #10: when the main thread times out, any in-flight db.commit()
    inside the worker thread must roll back rather than silently
    persisting stale state. A before-commit hook scoped to the worker
    thread enforces this; an attempt to commit after the cancel flag is
    set raises WorkerCancelledError instead of writing through."""
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)

    timeout_observed = threading.Event()
    commit_attempted = threading.Event()
    commit_succeeded = threading.Event()

    def _advance(db, job):
        # Block until the main thread has fired its timeout. By the time
        # we wake up, cancel_event should be set on this thread.
        timeout_observed.wait(timeout=2)
        try:
            stray_run = Run(kind="prop_refresh", status="completed")
            db.add(stray_run)
            db.commit()
            commit_succeeded.set()
        except refresh_jobs.WorkerCancelledError:
            pass
        finally:
            commit_attempted.set()
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.status == "failed"
    timeout_observed.set()
    assert commit_attempted.wait(timeout=2)
    assert not commit_succeeded.is_set(), (
        "Worker commit attempted after main thread timed out — band-aid must intercept"
    )


def test_main_thread_commits_are_not_affected_by_worker_cancel(db_session, monkeypatch):
    """The before-commit hook must be scoped to the worker thread only.
    Main-thread commits (and concurrent tests' commits) must not be
    intercepted by a stray cancel event from a finished worker."""
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)

    def _advance(db, job):
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    refresh_jobs.process_refresh_job_queue_once()

    # After the worker has finished, the main thread must be able to
    # commit freely — the cancel hook must have cleaned up its
    # thread-local state.
    db_session.add(Run(kind="probe", status="running"))
    db_session.commit()  # would raise WorkerCancelledError if the hook leaked


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
    # The guarded helpers (belt) still run inside the worker — _guarded_complete_job
    # returns False because the job status is no longer "running", and _fail_run
    # tries to mark the Run failed. Then db.commit() hits the bug-#10 cancellation
    # hook and rolls back everything the worker did this session, including the
    # Run that ``_advance`` created via ``_completed_run``. End state from the
    # user's perspective is unchanged: no resurrection.
    assert complete_attempted.wait(timeout=1)
    assert run_fail_attempted.wait(timeout=1)
    assert complete_result["value"] is False
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed"
    assert persisted.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR
    # The Run the worker tried to create never lands in the DB — the cancellation
    # hook rolled back the worker's transaction before commit.
    assert db_session.query(Run).count() == 0


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


def test_non_current_slate_refresh_links_job_run_id_before_stage_commit(db_session, monkeypatch):
    """Bug #10 P2 follow-up: non-current_slate refresh used to commit
    stage work via ``run_refresh_cycle`` BEFORE ``_execute_claimed_job``
    set ``job.run_id``. A worker timeout in that window fails the job
    from a snapshot with ``run_id=None``, so the already-committed
    ``runs`` row stays in ``running`` forever.

    Fix: ``_execute_claimed_job`` passes ``job`` into
    ``run_refresh_cycle``, which links ``job.run_id = run.id`` right
    after creating the Run — so any subsequent stage commit also
    persists the FK and the timeout path can find and fail the Run.
    """
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)

    job = RefreshJob(kind="refresh", scope="maintenance", reason="interval", status="queued")
    db_session.add(job)
    db_session.commit()
    job_id = job.id

    received_job = {"value": None}
    entered = threading.Event()
    release = threading.Event()

    def _run_refresh_cycle(db, *, sports=None, current_slate_only=False, job=None):
        received_job["value"] = job
        run = Run(kind="refresh", status="running")
        db.add(run)
        db.flush()
        # The real ``run_refresh_cycle`` links ``job.run_id = run.id``
        # immediately after the flush (see ingestion.py). Mirror that
        # so the stage commit below persists the FK.
        if job is not None:
            job.run_id = run.id
            db.flush()
        # Simulate a successful stage commit — Run + job.run_id are now
        # durable in the DB before we block until the parent times out.
        db.commit()
        entered.set()
        release.wait(timeout=5)
        return run

    monkeypatch.setattr(refresh_jobs, "run_refresh_cycle", _run_refresh_cycle)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert entered.wait(timeout=1)
    assert received_job["value"] is not None, (
        "non-current_slate refresh must pass ``job`` to run_refresh_cycle "
        "so the Run-to-job FK is set before any stage commit"
    )
    assert received_job["value"].id == job_id
    assert result is not None
    assert result.status == "failed"
    release.set()
    db_session.expire_all()
    persisted_job = db_session.get(RefreshJob, job_id)
    assert persisted_job.status == "failed"
    assert persisted_job.run_id is not None, (
        "job.run_id must have been persisted by the early link + stage commit"
    )
    persisted_run = _wait_for_run_status(db_session, persisted_job.run_id, "failed")
    assert persisted_run.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR


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


def test_nba_injury_refresh_gets_dedicated_worker_timeout(monkeypatch):
    _fast_timeout_settings(monkeypatch)
    monkeypatch.setattr(refresh_jobs, "NBA_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS", 0.8)
    job = RefreshJob(
        kind="nba_injury_refresh",
        scope="nba",
        reason="interval",
        status="running",
    )

    assert refresh_jobs._worker_timeout_seconds(job) == 0.8


def test_nba_referee_refresh_gets_dedicated_worker_timeout(monkeypatch):
    _fast_timeout_settings(monkeypatch)
    monkeypatch.setattr(refresh_jobs, "NBA_REFEREE_REFRESH_WORKER_TIMEOUT_SECONDS", 0.8)
    job = RefreshJob(
        kind="nba_referee_refresh",
        scope="nba",
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
    worker_entered = threading.Event()
    release = threading.Event()

    def _advance(db, job):
        # Block here so the first claimer's job stays ``running`` until
        # we've verified the second processor bows out. Without this,
        # the first claimer could complete, status="completed", and the
        # second processor would legitimately claim the other queued
        # row — a valid sequential interleaving that doesn't violate
        # the singleton invariant we're testing (codex P2 on PR #38).
        worker_entered.set()
        release.wait(timeout=5)
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    results: dict[str, refresh_jobs.RefreshJobSnapshot | None] = {}

    def _runner(key: str) -> None:
        results[key] = refresh_jobs.process_refresh_job_queue_once()

    t1 = threading.Thread(target=_runner, args=("first",))
    t1.start()
    # Wait until the first claimer's worker has entered ``_advance``.
    # By this point its main thread has committed ``status="running"``
    # and released the advisory lock, so the second processor can
    # proceed past the lock — and the singleton check is what must
    # prevent it from claiming.
    assert worker_entered.wait(timeout=3), "first worker must enter _advance"

    t2 = threading.Thread(target=_runner, args=("second",))
    t2.start()
    t2.join(timeout=5)

    assert "second" in results, "second processor must complete"
    assert results["second"] is None, (
        "second processor must observe the first's claim and bow out"
    )

    release.set()
    t1.join(timeout=5)

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


# -----------------------------------------------------------------------------
# Bug #22: prop_refresh transient errors cap + back-off + dead-letter
# -----------------------------------------------------------------------------


def test_prop_refresh_transient_error_backs_off_with_attempt_counter(db_session, monkeypatch):
    """Bug #22: an ``httpx.HTTPError`` during ``advance_prop_refresh_job``
    must not requeue the job for an immediate replay. The fix bumps a
    per-job ``transient_attempts`` counter and pushes ``queued_at``
    into the future by ``_prop_refresh_backoff_seconds(attempts)``."""
    import httpx

    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)

    def _advance(db, job):
        raise httpx.ConnectError("simulated upstream connectivity blip")

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()
    assert result is not None
    assert result.status == "queued", "transient error before the cap must requeue, not fail"

    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "queued"
    details = persisted.details or {}
    assert details["transient_attempts"] == 1
    assert "last_transient_error" in details
    assert details["last_transient_backoff_seconds"] >= refresh_jobs.PROP_REFRESH_BACKOFF_BASE_SECONDS

    # The back-off must push ``queued_at`` into the future so the claim
    # loop skips it until the back-off elapses. SQLite stores naive
    # datetimes, so normalize before comparing.
    queued_at = persisted.queued_at
    if queued_at.tzinfo is None:
        queued_at = queued_at.replace(tzinfo=timezone.utc)
    assert queued_at > datetime.now(timezone.utc)


def test_prop_refresh_claim_skips_jobs_whose_backoff_has_not_elapsed(db_session, monkeypatch):
    """Bug #22: a row whose ``queued_at`` is still in the future is
    not eligible for claim — the back-off window enforces itself.
    A second queued job whose window HAS elapsed should be picked
    up instead."""
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    now = datetime.now(timezone.utc)

    backing_off = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="transient-retry",
        status="queued",
        queued_at=now + timedelta(seconds=300),  # not eligible yet
        details={"transient_attempts": 2},
    )
    eligible = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="fresh",
        status="queued",
        queued_at=now - timedelta(seconds=1),
    )
    db_session.add_all([backing_off, eligible])
    db_session.commit()

    advanced_for: list[int] = []

    def _advance(db, job):
        advanced_for.append(job.id)
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()
    assert result is not None
    assert result.job_id == eligible.id, "backed-off job must NOT be claimed before its window elapses"
    assert advanced_for == [eligible.id]

    db_session.expire_all()
    backing_off_persisted = db_session.get(RefreshJob, backing_off.id)
    assert backing_off_persisted.status == "queued", "untouched"


def test_prop_refresh_dead_letters_after_cap_of_attempts(db_session, monkeypatch):
    """Bug #22: once ``transient_attempts`` reaches
    ``PROP_REFRESH_MAX_TRANSIENT_ATTEMPTS``, the next transient error
    must fail the job instead of requeuing it forever. The error
    message identifies the dead-letter path so ops can grep for it."""
    import httpx

    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    now = datetime.now(timezone.utc)

    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="cap-test",
        status="queued",
        queued_at=now - timedelta(seconds=1),
        details={"transient_attempts": refresh_jobs.PROP_REFRESH_MAX_TRANSIENT_ATTEMPTS - 1},
    )
    db_session.add(job)
    db_session.commit()

    def _advance(db, job):
        raise httpx.ReadTimeout("simulated persistent upstream timeout")

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()
    assert result is not None
    assert result.status == "failed"
    assert refresh_jobs.PROP_REFRESH_DEAD_LETTER_ERROR in (result.error_message or "")

    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed"
    assert (persisted.details or {}).get("transient_attempts") == refresh_jobs.PROP_REFRESH_MAX_TRANSIENT_ATTEMPTS


def test_prop_refresh_backoff_is_exponential_and_capped():
    """Bug #22: the back-off doubles each attempt (2s → 4s → 8s → 16s
    → 32s) and caps at ``PROP_REFRESH_BACKOFF_CAP_SECONDS`` so a long
    outage doesn't strand a job for hours."""
    assert refresh_jobs._prop_refresh_backoff_seconds(1) == 2.0
    assert refresh_jobs._prop_refresh_backoff_seconds(2) == 4.0
    assert refresh_jobs._prop_refresh_backoff_seconds(3) == 8.0
    assert refresh_jobs._prop_refresh_backoff_seconds(4) == 16.0
    assert refresh_jobs._prop_refresh_backoff_seconds(5) == 32.0
    # Way past the cap — clamped to PROP_REFRESH_BACKOFF_CAP_SECONDS.
    assert refresh_jobs._prop_refresh_backoff_seconds(50) == refresh_jobs.PROP_REFRESH_BACKOFF_CAP_SECONDS


def test_enqueue_prop_refresh_skips_during_dead_letter_cooldown(db_session, monkeypatch):
    """Codex round-3 P2 on PR #47 (bug #22): a dead-lettered prop_refresh
    cools off cross-job enqueueing for ``PROP_REFRESH_DEAD_LETTER_COOLDOWN_SECONDS``.
    Otherwise the next scheduler tick would just create a fresh job
    with a reset counter and walk through the same dead-letter
    sequence, producing one new log line per tick during a
    persistent outage."""
    now = datetime.now(timezone.utc)
    dead_letter = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="cooldown-test",
        status="failed",
        queued_at=now - timedelta(seconds=60),
        finished_at=now - timedelta(seconds=30),  # within the cooldown window
        error_message=(
            f"{refresh_jobs.PROP_REFRESH_DEAD_LETTER_ERROR}: persistent upstream timeout"
        ),
        details={"transient_attempts": refresh_jobs.PROP_REFRESH_MAX_TRANSIENT_ATTEMPTS},
    )
    db_session.add(dead_letter)
    db_session.commit()
    dead_letter_id = dead_letter.id

    # Attempt to enqueue a new prop_refresh — must be suppressed.
    job, created = refresh_jobs.enqueue_refresh_job(
        db_session,
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
    )
    db_session.commit()

    assert created is False, (
        "while a recent dead-letter is in the cooldown window, "
        "no new prop_refresh should be enqueued"
    )
    assert job.id == dead_letter_id, (
        "the cooldown helper returns the failed row so the scheduler "
        "treats the call as a no-op"
    )
    # Schema invariant: no new queued prop_refresh was created.
    queued_count = db_session.scalar(
        select(func.count(RefreshJob.id)).where(
            RefreshJob.kind == "prop_refresh",
            RefreshJob.status == "queued",
        )
    )
    assert queued_count == 0


def test_enqueue_prop_refresh_resumes_after_cooldown_elapses(db_session, monkeypatch):
    """Codex round-3 P2 on PR #47 (bug #22): once the cooldown
    window expires, the next enqueue must fall through to a fresh
    queued job. The cooldown is meant to suppress spam, not strand
    prop_refresh forever."""
    now = datetime.now(timezone.utc)
    stale_dead_letter = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="stale-cooldown",
        status="failed",
        queued_at=now - timedelta(hours=2),
        finished_at=now - timedelta(seconds=refresh_jobs.PROP_REFRESH_DEAD_LETTER_COOLDOWN_SECONDS + 60),
        error_message=(
            f"{refresh_jobs.PROP_REFRESH_DEAD_LETTER_ERROR}: long-past timeout"
        ),
    )
    db_session.add(stale_dead_letter)
    db_session.commit()

    job, created = refresh_jobs.enqueue_refresh_job(
        db_session,
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
    )
    db_session.commit()

    assert created is True
    assert job.status == "queued"
    assert job.id != stale_dead_letter.id


def test_enqueue_prop_refresh_not_blocked_by_non_dead_letter_failure(db_session):
    """A failed prop_refresh that ISN'T the dead-letter signal
    (e.g. a one-off worker timeout) must NOT suppress fresh
    enqueues — only the explicit ``PROP_REFRESH_DEAD_LETTER_ERROR``
    marker triggers cooldown."""
    now = datetime.now(timezone.utc)
    plain_failure = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="plain-failure",
        status="failed",
        queued_at=now - timedelta(seconds=60),
        finished_at=now - timedelta(seconds=10),
        error_message=refresh_jobs.WORKER_TIMEOUT_ERROR,
    )
    db_session.add(plain_failure)
    db_session.commit()

    job, created = refresh_jobs.enqueue_refresh_job(
        db_session,
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
    )
    db_session.commit()
    assert created is True
    assert job.status == "queued"


def test_prop_refresh_successful_batch_resets_transient_attempts(db_session, monkeypatch):
    """Bug #22 round-2 P2: a long-running prop_refresh that hits 5
    intermittent blips spaced across many successful batches must NOT
    dead-letter — the cap is for CONSECUTIVE failures, not lifetime
    total. A successful ``advance_prop_refresh_job`` clears the
    ``transient_attempts`` counter so the next blip starts at 1."""
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    now = datetime.now(timezone.utc)

    # Pre-stamp the job with 3 prior transient attempts (e.g. from
    # earlier blips during the same multi-batch run).
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="reset-test",
        status="queued",
        queued_at=now - timedelta(seconds=1),
        details={
            "transient_attempts": 3,
            "last_transient_error": "stale upstream blip",
            "last_transient_backoff_seconds": 8.0,
        },
    )
    db_session.add(job)
    db_session.commit()

    def _advance(db, job):
        # A successful batch advance — no exception, returns ``True``
        # so the worker exits cleanly.
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()
    assert result is not None
    assert result.status == "completed"

    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    details = persisted.details or {}
    assert "transient_attempts" not in details, (
        "successful advance must clear the consecutive-failure counter"
    )
    assert "last_transient_error" not in details
    assert "last_transient_backoff_seconds" not in details


# -----------------------------------------------------------------------------
# Bug #51: refresh-worker statement_timeout bounds slow DB queries so a
# wedged query can't pin the worker thread (and its pooled connection)
# indefinitely. Symptom in prod: the first ``_process_refresh_queue_job``
# tick never returned, every subsequent tick was skipped with
# ``maximum number of running instances reached (1)``, and the
# SQLAlchemy pool eventually exhausted after ~90min of zombie workers.
# -----------------------------------------------------------------------------


def _fake_pg_session(monkeypatch, db):
    """Helper: mark ``db`` as a postgres session by patching its bind's
    dialect name and intercepting ``db.execute`` so SET LOCAL is
    captured without hitting the underlying engine (which is SQLite
    in tests and would raise).

    Note: ``get_bind`` is called BOTH by ``_apply_worker_statement_timeout``
    (which we want to take the postgres branch) AND by the session's
    own internal execute machinery (which needs the REAL bind for
    actual queries). So the lambda inspects the call site via a small
    state flag: when called with no kwargs (from our helper), return
    the fake; otherwise (from session internals, e.g. with
    ``clause=...``), return the real bind.
    """
    real_get_bind = db.get_bind
    fake_pg_bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def _patched_get_bind(*args, **kwargs):
        # Only the SET LOCAL dialect check calls ``get_bind()`` with no
        # args from our helper. Session internals call it with
        # ``clause=...`` / ``mapper=...`` / etc. Forward those to the
        # real engine.
        if not args and not kwargs:
            return fake_pg_bind
        return real_get_bind(*args, **kwargs)

    monkeypatch.setattr(db, "get_bind", _patched_get_bind)
    real_execute = db.execute
    captured: list[str] = []

    def _execute(stmt, *args, **kwargs):
        sql_text = str(stmt)
        if "SET LOCAL statement_timeout" in sql_text:
            captured.append(sql_text)
            return None
        return real_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(db, "execute", _execute)
    return captured


def test_apply_worker_statement_timeout_emits_set_local_on_postgres(
    tmp_path, monkeypatch
):
    """On Postgres, the helper issues ``SET LOCAL statement_timeout``
    with the configured millisecond value so any individual query is
    bounded."""
    db_path = tmp_path / "set_local_pg.sqlite"
    url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine(
        url, connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)()

    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(refresh_worker_statement_timeout_seconds=30),
    )
    captured = _fake_pg_session(monkeypatch, db)

    try:
        refresh_jobs._apply_worker_statement_timeout(db)
        assert any(
            "SET LOCAL statement_timeout = 30000" in sql for sql in captured
        ), f"expected SET LOCAL statement_timeout=30000, got: {captured}"
    finally:
        db.close()
        engine.dispose()


def test_apply_worker_statement_timeout_is_noop_on_sqlite(monkeypatch):
    """SQLite has no ``statement_timeout`` GUC; the helper must
    short-circuit so the SQLite test suite (and dev mode) stays
    unaffected."""
    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(refresh_worker_statement_timeout_seconds=30),
    )
    mock_db = unittest.mock.MagicMock()
    mock_db.get_bind.return_value.dialect.name = "sqlite"

    refresh_jobs._apply_worker_statement_timeout(mock_db)

    assert mock_db.execute.call_count == 0


def test_apply_worker_statement_timeout_is_noop_when_setting_disabled(monkeypatch):
    """Operators can disable the cap by setting the value to 0; the
    helper must respect that without issuing a no-op ``SET LOCAL`` that
    Postgres would otherwise interpret as "no limit"."""
    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(refresh_worker_statement_timeout_seconds=0),
    )
    mock_db = unittest.mock.MagicMock()
    mock_db.get_bind.return_value.dialect.name = "postgresql"

    refresh_jobs._apply_worker_statement_timeout(mock_db)

    assert mock_db.execute.call_count == 0


def test_slow_worker_query_raising_does_not_leak_session(db_session, monkeypatch):
    """Bug #51 end-to-end: simulate a long DB query hitting
    ``statement_timeout`` mid-worker by having ``advance_prop_refresh_job``
    raise a generic exception (psycopg surfaces this as ``OperationalError``
    on a real timeout). The worker's ``except Exception`` path must fail
    the job, commit the failure, and close the session — i.e. the
    function must return within bounded time AND no orphan ``running``
    job is left behind.

    Before the fix, a slow query had nothing to bound it, the worker
    sat in psycopg waiting forever, and ``done_event.wait`` returned
    False after the worker-level timeout (300s for current_slate) —
    but the worker thread kept holding its pooled connection. Every
    subsequent tick spawned another zombie. After 15 cycles the
    SQLAlchemy pool was exhausted (5 base + 10 overflow) and every API
    request 30s-timed-out at session checkout.
    """
    _install_threaded_session_factory(db_session, monkeypatch)
    _fast_timeout_settings(monkeypatch)
    job = _queued_prop_job(db_session)

    advance_invocations: list[int] = []

    def _advance(db, job):
        advance_invocations.append(job.id)
        # Simulate the exception psycopg raises when the Postgres
        # statement_timeout GUC fires mid-query. The worker's
        # ``except Exception`` path catches this, fails the job, and
        # commits. The ``with SessionLocal()`` block then closes the
        # session and returns the connection to the pool.
        raise RuntimeError("simulated statement_timeout firing mid-query")

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    started = monotonic()
    result = refresh_jobs.process_refresh_job_queue_once()
    elapsed = monotonic() - started

    # Function must return promptly — the worker's exception path
    # closes its session synchronously, no timeout-wait dance involved.
    assert elapsed < 1.0
    assert result is not None
    assert result.job_id == job.id
    assert result.status == "failed"
    assert advance_invocations == [job.id]

    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed", (
        "the job row must NOT remain in ``running`` after the worker "
        "raises — otherwise reconcile_stale_jobs would have to clean "
        "it up and the next scheduler tick is blocked until then"
    )
    assert persisted.error_message  # any non-empty error message

    # The follow-up tick must be able to claim a fresh job — no
    # zombie worker is hogging the singleton invariant.
    second_job = _queued_prop_job(db_session, reason="post-error")

    def _advance_ok(db, job):
        return _completed_run(db), True

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance_ok)

    second_result = refresh_jobs.process_refresh_job_queue_once()
    assert second_result is not None
    assert second_result.job_id == second_job.id
    assert second_result.status == "completed"


# -----------------------------------------------------------------------------
# Connection-pool-leak fix: ``SET LOCAL statement_timeout`` is scoped to the
# CURRENT transaction. The worker commits many times per job (per stage / per
# batch), and every commit drops the LOCAL setting. Without an
# ``after_begin`` listener that re-applies the timeout, the second-and-onward
# transactions ran with no cap — so a slow query after the first commit
# could pin the worker indefinitely. When the main thread's timeout fired,
# the worker stayed alive (no Python-level interrupt during a blocking DB
# call), kept its pooled connection, and every subsequent tick spawned
# another worker. After ~170 of these the pool was exhausted at 5 base + 10
# overflow = 15 connections and every API request 30s-timed-out at session
# checkout. The wedge log captured this exact pattern.
# -----------------------------------------------------------------------------


def test_set_local_statement_timeout_reapplied_after_commit(tmp_path, monkeypatch):
    """The ``after_begin`` listener must fire on every new transaction
    in the worker session, re-emitting ``SET LOCAL statement_timeout``
    so the cap survives mid-worker commits.

    Before the fix the helper only ran ``SET LOCAL`` once. SET LOCAL is
    transaction-scoped, so after the first ``db.commit()`` the timeout
    was gone — the next slow query had no cap and could pin the worker
    indefinitely, leaking its pooled connection.
    """
    db_path = tmp_path / "conn_pool_leak.sqlite"
    url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine(
        url, connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)()

    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(refresh_worker_statement_timeout_seconds=30),
    )

    captured_db_set_local = _fake_pg_session(monkeypatch, db)

    # The fix re-emits SET LOCAL via the after_begin listener using the
    # ``connection`` provided by SQLAlchemy. Capture the statement at
    # the engine layer AND short-circuit before SQLite executes it
    # (SQLite would raise on unknown SET syntax).
    from sqlalchemy import event as _event

    connection_set_local_records: list[str] = []

    @_event.listens_for(engine, "before_cursor_execute", retval=True)
    def _capture_and_skip_set_local(
        _conn, _cursor, statement, parameters, _context, _executemany
    ):
        if "SET LOCAL statement_timeout" in statement:
            connection_set_local_records.append(statement)
            # Replace with a harmless statement SQLite accepts. We're
            # only verifying the listener fires, not the actual DDL.
            return ("SELECT 1 -- SET LOCAL placeholder", parameters)
        return (statement, parameters)

    after_begin_calls = {"count": 0}

    @_event.listens_for(db, "after_begin")
    def _count_after_begin(_session, _trans, _conn):
        after_begin_calls["count"] += 1

    real_execute = type(db).execute.__get__(db, type(db))

    try:
        # Apply the timeout — this both emits SET LOCAL on the current
        # transaction (via db.execute) AND registers the after_begin
        # listener.
        refresh_jobs._apply_worker_statement_timeout(db)
        assert len(captured_db_set_local) == 1, (
            "_apply_worker_statement_timeout must emit SET LOCAL once on the "
            f"current transaction; got: {captured_db_set_local}"
        )

        # Commit closes the current transaction; the next statement
        # opens a new one and ``after_begin`` fires the listener.
        db.commit()
        real_execute(text("SELECT 1"))
        assert after_begin_calls["count"] >= 1, (
            "after_begin listener must fire when a new transaction begins"
        )
        # The fix's listener must have emitted SET LOCAL again on the
        # new transaction via the connection. Before the fix, this list
        # would be empty — every transaction past the first ran with
        # no cap.
        assert any(
            "SET LOCAL statement_timeout" in s
            for s in connection_set_local_records
        ), (
            "the after_begin listener must re-emit SET LOCAL on every new "
            "transaction in the worker session — otherwise post-commit "
            "queries run uncapped and a slow one pins the connection "
            f"forever. connection_set_local_records={connection_set_local_records}"
        )

        # Do one more commit cycle — every NEW transaction's begin
        # must re-arm SET LOCAL, not just the first post-init one.
        previous_count = len(connection_set_local_records)
        db.commit()
        real_execute(text("SELECT 1"))
        assert len(connection_set_local_records) > previous_count, (
            "SET LOCAL must be re-emitted on EVERY new transaction, not "
            "just the second one. Got "
            f"{len(connection_set_local_records)} SET LOCAL calls after 2 "
            "commit cycles."
        )
    finally:
        db.close()
        engine.dispose()


def test_set_local_statement_timeout_listener_scoped_to_session(tmp_path, monkeypatch):
    """The ``after_begin`` listener must be attached to the SPECIFIC
    session that ``_apply_worker_statement_timeout`` was called on, not
    the global ``Session`` class. Otherwise every request session would
    have its statements bounded by ``refresh_worker_statement_timeout_seconds``
    — silently capping API queries far below their natural budget.
    """
    db_path = tmp_path / "listener_scope.sqlite"
    url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine(
        url, connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine, future=True
    )
    worker_db = TestingSessionLocal()
    request_db = TestingSessionLocal()

    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(refresh_worker_statement_timeout_seconds=30),
    )
    _fake_pg_session(monkeypatch, worker_db)

    try:
        refresh_jobs._apply_worker_statement_timeout(worker_db)

        # Now exercise a transaction on the OTHER session. If the
        # listener leaked to the global ``Session`` class, the
        # request_db's begin would also trigger SET LOCAL — and SQLite
        # would raise because we haven't intercepted request_db.execute.
        # No exception = listener is correctly session-scoped.
        request_db.execute(text("SELECT 1"))
        request_db.commit()
    finally:
        worker_db.close()
        request_db.close()
        engine.dispose()
