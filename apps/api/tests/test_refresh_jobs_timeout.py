from __future__ import annotations

import threading
from time import monotonic
from types import SimpleNamespace

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


def _completed_run(db, *, kind="prop_refresh") -> Run:
    run = Run(kind=kind, status="completed")
    db.add(run)
    db.flush()
    return run


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
    complete_result = {"value": None}
    original_complete = refresh_jobs._guarded_complete_job

    def _advance(db, job):
        entered.set()
        release.wait(timeout=5)
        return _completed_run(db), True

    def _guarded_complete(db, job_id):
        complete_result["value"] = original_complete(db, job_id)
        complete_attempted.set()
        return bool(complete_result["value"])

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)
    monkeypatch.setattr(refresh_jobs, "_guarded_complete_job", _guarded_complete)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert entered.wait(timeout=1)
    assert result is not None
    assert result.status == "failed"
    release.set()
    assert complete_attempted.wait(timeout=1)
    assert complete_result["value"] is False
    db_session.expire_all()
    persisted = db_session.get(RefreshJob, job.id)
    assert persisted.status == "failed"
    assert persisted.error_message == refresh_jobs.WORKER_TIMEOUT_ERROR


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
