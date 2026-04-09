from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api import routes
from app.models import RefreshJob, Run
from app.services import refresh_jobs, scheduler


class _DetachedRun:
    def __init__(self):
        self._detached = False
        self._id = 91
        self._status = "completed"
        self._records_processed = 128
        self._finished_at = datetime(2026, 4, 3, 18, 15, tzinfo=timezone.utc)

    def detach(self):
        self._detached = True

    def _read(self, value):
        if self._detached:
            raise RuntimeError("Run detached from session")
        return value

    @property
    def id(self):
        return self._read(self._id)

    @property
    def status(self):
        return self._read(self._status)

    @property
    def records_processed(self):
        return self._read(self._records_processed)

    @property
    def finished_at(self):
        return self._read(self._finished_at)


class _SessionContext:
    def __init__(self, run):
        self.run = run

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.run.detach()

    def commit(self):
        return None


class _DbSessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        return False


def test_run_refresh_cycle_now_returns_snapshot_before_session_detaches(monkeypatch):
    run = _DetachedRun()
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _SessionContext(run))
    monkeypatch.setattr(scheduler, "run_refresh_cycle", lambda db, **kwargs: run)
    monkeypatch.setattr(scheduler, "schedule_event_refreshes", lambda: None)

    snapshot = scheduler.run_refresh_cycle_now(reason="manual")

    assert snapshot is not None
    assert snapshot.run_id == 91
    assert snapshot.status == "completed"
    assert snapshot.records_processed == 128
    assert snapshot.finished_at == datetime(2026, 4, 3, 18, 15, tzinfo=timezone.utc)


def test_health_endpoint_uses_sanitized_refresh_error_message(client, monkeypatch):
    raw_error = (
        "Instance <Run at 0x10cdd5df0> is not bound to a Session; "
        "attribute refresh operation cannot proceed "
        "(Background on this error at: https://sqlalche.me/e/20/bhk3)"
    )
    monkeypatch_payload = {
        "refresh_status": "failed",
        "refresh_reason": "manual",
        "last_successful_refresh_at": None,
        "data_stale": True,
        "refresh_error_message": scheduler.summarize_refresh_error_message(raw_error),
        "prop_refresh_status": "running",
        "prop_refresh_reason": "interval",
        "last_prop_refresh_at": None,
        "prop_data_stale": True,
        "prop_refresh_error_message": None,
        "active_refresh_job": None,
        "latest_refresh_job": None,
        "active_prop_refresh_job": None,
        "latest_prop_refresh_job": None,
    }
    monkeypatch.setattr(routes, "get_refresh_runtime_state", lambda: monkeypatch_payload)
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["refresh_error_message"] == "The latest refresh hit a temporary database session issue."
    assert "sqlalche.me" not in payload["refresh_error_message"]
    assert payload["prop_refresh_status"] == "running"
    assert payload["prop_refresh_reason"] == "interval"


def test_reconcile_stale_jobs_marks_old_active_jobs_failed(db_session):
    stale_job = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason="manual",
        status="running",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    fresh_job = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason="interval",
        status="queued",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    db_session.add_all([stale_job, fresh_job])
    db_session.commit()

    reconciled = scheduler.reconcile_stale_jobs(db_session)
    db_session.commit()

    assert reconciled == 1
    db_session.refresh(stale_job)
    db_session.refresh(fresh_job)
    assert stale_job.status == "failed"
    assert stale_job.error_message == refresh_jobs.STALE_REFRESH_JOB_ERROR
    assert stale_job.finished_at is not None
    assert fresh_job.status == "queued"


def test_reconcile_stale_jobs_keeps_expected_long_running_refreshes_active(db_session):
    running_job = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason="interval",
        status="running",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=45),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )
    db_session.add(running_job)
    db_session.commit()

    reconciled = scheduler.reconcile_stale_jobs(db_session)
    db_session.commit()

    assert reconciled == 0
    db_session.refresh(running_job)
    assert running_job.status == "running"
    assert running_job.error_message is None


def test_enqueue_refresh_job_reconciles_stale_active_job_before_coalescing(db_session):
    stale_job = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason="interval",
        status="running",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    db_session.add(stale_job)
    db_session.commit()

    job, created = refresh_jobs.enqueue_refresh_job(
        db_session,
        kind="refresh",
        scope="current_slate",
        reason="manual",
    )
    db_session.commit()

    assert created is True
    db_session.refresh(stale_job)
    db_session.refresh(job)
    assert stale_job.status == "failed"
    assert stale_job.error_message == refresh_jobs.STALE_REFRESH_JOB_ERROR
    assert job.id != stale_job.id
    assert job.status == "queued"


def test_get_refresh_runtime_state_reconciles_stale_jobs_without_restart(db_session, monkeypatch):
    stale_job = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason="manual",
        status="running",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    db_session.add(stale_job)
    db_session.commit()

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DbSessionContext(db_session))

    runtime = scheduler.get_refresh_runtime_state()

    db_session.refresh(stale_job)
    assert stale_job.status == "failed"
    assert runtime["refresh_status"] == "failed"
    assert runtime["active_refresh_job"] is None
    assert runtime["latest_refresh_job"]["status"] == "failed"
    assert runtime["refresh_error_message"] == "The latest refresh stalled and was reset automatically."


def test_process_refresh_job_queue_once_enqueues_shadow_follow_up_for_current_slate_refresh(db_session, monkeypatch):
    job = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason="interval",
        status="queued",
    )
    db_session.add(job)
    db_session.commit()

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))
    monkeypatch.setattr(
        refresh_jobs,
        "advance_current_slate_refresh_job",
        lambda db, job, sports=None: (SimpleNamespace(id=101), True),
    )

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "refresh"
    assert result.status == "completed"
    shadow_job = db_session.query(RefreshJob).filter_by(kind="shadow_capture").one()
    assert shadow_job.scope == "current_slate"
    assert shadow_job.status == "queued"
    assert shadow_job.details["source_run_id"] == 101
    assert shadow_job.details["source_refresh_job_id"] == job.id


def test_claim_next_job_prioritizes_current_slate_over_maintenance_continuation(db_session):
    maintenance = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="queued",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        details={"phase": "watchlist_score_batch"},
    )
    current_slate = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason="interval",
        status="queued",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add_all([maintenance, current_slate])
    db_session.commit()

    claimed = refresh_jobs._claim_next_job(db_session)

    assert claimed is not None
    assert claimed.id == current_slate.id
    assert claimed.kind == "refresh"
    assert claimed.scope == "current_slate"


def test_process_refresh_job_queue_once_requeues_incomplete_prop_refresh(db_session, monkeypatch):
    run = Run(kind="prop_refresh", status="running")
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="queued",
    )
    db_session.add_all([run, job])
    db_session.commit()

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))

    def _advance(db, job):
        job.details = {"phase": "watchlist_score_batch", "cursor": {"market_id": 500}}
        return run, False

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "prop_refresh"
    assert result.status == "queued"
    db_session.refresh(job)
    assert job.run_id == run.id
    assert job.status == "queued"
    assert job.started_at is None
    assert job.details["phase"] == "watchlist_score_batch"


def test_enqueue_shadow_capture_job_coalesces_current_slate_follow_ups(db_session):
    first_job, first_created = refresh_jobs.enqueue_shadow_capture_job(
        db_session,
        scope="current_slate",
        source_run_id=101,
        source_refresh_job_id=11,
    )
    second_job, second_created = refresh_jobs.enqueue_shadow_capture_job(
        db_session,
        scope="current_slate",
        source_run_id=102,
        source_refresh_job_id=12,
    )
    db_session.commit()

    assert first_created is True
    assert second_created is False
    assert first_job.id == second_job.id
    assert db_session.query(RefreshJob).filter_by(kind="shadow_capture").count() == 1
    db_session.refresh(second_job)
    assert second_job.details["source_run_id"] == 102
    assert second_job.details["source_refresh_job_id"] == 12


def test_enqueue_shadow_capture_job_coalesces_backfill_independently_from_current_slate(db_session):
    current_job, current_created = refresh_jobs.enqueue_shadow_capture_job(
        db_session,
        scope="current_slate",
        source_run_id=101,
        source_refresh_job_id=11,
    )
    first_backfill, first_backfill_created = refresh_jobs.enqueue_shadow_capture_job(
        db_session,
        scope="backfill",
        source_prop_refresh_job_id=21,
    )
    second_backfill, second_backfill_created = refresh_jobs.enqueue_shadow_capture_job(
        db_session,
        scope="backfill",
        source_prop_refresh_job_id=22,
    )
    db_session.commit()

    assert current_created is True
    assert first_backfill_created is True
    assert second_backfill_created is False
    assert current_job.id != first_backfill.id
    assert first_backfill.id == second_backfill.id
    assert db_session.query(RefreshJob).filter_by(kind="shadow_capture").count() == 2
    db_session.refresh(second_backfill)
    assert second_backfill.scope == "backfill"
    assert second_backfill.details["source_prop_refresh_job_id"] == 22


def test_process_refresh_job_queue_once_enqueues_shadow_backfill_after_prop_refresh(db_session, monkeypatch):
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="queued",
    )
    db_session.add(job)
    db_session.commit()

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))
    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", lambda db, job: (SimpleNamespace(id=202), True))

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "prop_refresh"
    assert result.status == "completed"
    shadow_job = db_session.query(RefreshJob).filter_by(kind="shadow_capture", scope="backfill").one()
    assert shadow_job.status == "queued"
    assert shadow_job.details["source_prop_refresh_job_id"] == job.id


def test_process_refresh_job_queue_once_requeues_incomplete_shadow_capture(db_session, monkeypatch):
    job = RefreshJob(
        kind="shadow_capture",
        scope="backfill",
        reason="maintenance_follow_up",
        status="queued",
        details={"shadow_capture_scope": "backfill"},
    )
    db_session.add(job)
    db_session.commit()

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))
    monkeypatch.setattr(
        refresh_jobs,
        "capture_shadow_artifacts_batch",
        lambda db, **kwargs: SimpleNamespace(
            prediction_count=12,
            parlay_prediction_count=0,
            next_phase="predictions",
            next_cursor={"captured_at": datetime.now(timezone.utc).isoformat(), "item_id": 44},
            complete=False,
        ),
    )

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "shadow_capture"
    assert result.status == "queued"
    db_session.refresh(job)
    assert job.status == "queued"
    assert job.details["shadow_predictions_captured_total"] == 12
    assert job.details["phase"] == "predictions"
    assert job.started_at is None


def test_shadow_capture_failure_does_not_mark_completed_refresh_failed(db_session, monkeypatch):
    refresh_job = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason="manual",
        status="completed",
        finished_at=datetime.now(timezone.utc),
    )
    shadow_job = RefreshJob(
        kind="shadow_capture",
        scope="current_slate",
        reason="follow_up",
        status="queued",
        details={"source_run_id": 101},
    )
    db_session.add_all([refresh_job, shadow_job])
    db_session.commit()

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))

    def _raise_shadow_capture_batch(
        db,
        *,
        run_id: int,
        source_run_id: int | None = None,
        backfill: bool = False,
        phase: str = "predictions",
        cursor=None,
    ):
        raise RuntimeError(f"shadow capture failed for {source_run_id}")

    monkeypatch.setattr(refresh_jobs, "capture_shadow_artifacts_batch", _raise_shadow_capture_batch)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "shadow_capture"
    assert result.status == "failed"
    db_session.refresh(refresh_job)
    db_session.refresh(shadow_job)
    assert refresh_job.status == "completed"
    assert shadow_job.status == "failed"
    assert shadow_job.error_message == "shadow capture failed for 101"


def test_shadow_backfill_failure_does_not_mark_completed_prop_refresh_failed(db_session, monkeypatch):
    prop_refresh_job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="completed",
        finished_at=datetime.now(timezone.utc),
    )
    shadow_job = RefreshJob(
        kind="shadow_capture",
        scope="backfill",
        reason="maintenance_follow_up",
        status="queued",
        details={"shadow_capture_scope": "backfill"},
    )
    db_session.add_all([prop_refresh_job, shadow_job])
    db_session.commit()

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))

    def _raise_shadow_capture_batch(
        db,
        *,
        run_id: int,
        source_run_id: int | None = None,
        backfill: bool = False,
        phase: str = "predictions",
        cursor=None,
    ):
        raise RuntimeError(f"shadow backfill failed for {'backfill' if backfill else source_run_id}")

    monkeypatch.setattr(refresh_jobs, "capture_shadow_artifacts_batch", _raise_shadow_capture_batch)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "shadow_capture"
    assert result.status == "failed"
    db_session.refresh(prop_refresh_job)
    db_session.refresh(shadow_job)
    assert prop_refresh_job.status == "completed"
    assert shadow_job.status == "failed"
    assert shadow_job.error_message == "shadow backfill failed for backfill"
