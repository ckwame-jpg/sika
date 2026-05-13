from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

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
        "active_settlement_job": {
            "id": 7,
            "kind": "settlement",
            "scope": "predictions",
            "reason": "interval",
            "status": "running",
            "run_id": 17,
            "error_message": None,
            "details": {"processed_so_far": 100},
            "queued_at": datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
            "started_at": datetime(2026, 4, 7, 18, 1, tzinfo=timezone.utc),
            "finished_at": None,
        },
        "latest_settlement_job": None,
    }
    monkeypatch.setattr(routes, "get_refresh_runtime_state", lambda: monkeypatch_payload)
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["refresh_error_message"] == "The latest refresh hit a temporary database session issue."
    assert "sqlalche.me" not in payload["refresh_error_message"]
    assert payload["prop_refresh_status"] == "running"
    assert payload["prop_refresh_reason"] == "interval"
    assert payload["active_settlement_job"]["kind"] == "settlement"
    assert payload["active_settlement_job"]["scope"] == "predictions"


def test_manual_settle_predictions_endpoint_settles_all_unresolved(client, monkeypatch):
    """Bug #12: the manual ``/ops/jobs/settle-predictions`` endpoint
    must settle every unresolved prediction (single + parlay), not
    just the latest per ticker — older stacked predictions would
    otherwise stay ``pending`` forever."""
    calls = {}

    def _settle_predictions(db, **kwargs):
        calls["single_kwargs"] = kwargs
        return {"processed": 3, "updated": 2, "won": 1, "lost": 1}

    def _settle_parlay_predictions(db):
        calls["parlay_called"] = True
        return {"processed": 1, "updated": 1, "won": 1}

    monkeypatch.setattr(routes, "settle_predictions", _settle_predictions)
    monkeypatch.setattr(routes, "settle_parlay_predictions", _settle_parlay_predictions)

    response = client.post("/ops/jobs/settle-predictions")

    assert response.status_code == 200
    assert "latest_only_per_key" not in calls["single_kwargs"], (
        "endpoint must not ship a latest-only flag — bug #12 was caused by that filter"
    )
    assert calls["parlay_called"] is True
    payload = response.json()
    assert payload["processed"] == 4
    assert payload["updated"] == 3
    assert payload["won"] == 2
    assert payload["lost"] == 1


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
    now = datetime.now(timezone.utc)
    running_job = RefreshJob(
        kind="refresh",
        scope="current_slate",
        reason="interval",
        status="running",
        queued_at=now - timedelta(minutes=45),
        started_at=now - timedelta(minutes=4),
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


def test_enqueue_refresh_job_accepts_and_coalesces_settlement_jobs(db_session):
    first, first_created = refresh_jobs.enqueue_refresh_job(
        db_session,
        kind="settlement",
        scope="predictions",
        reason="interval",
    )
    second, second_created = refresh_jobs.enqueue_refresh_job(
        db_session,
        kind="settlement",
        scope="predictions",
        reason="manual",
    )
    db_session.commit()

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    db_session.refresh(second)
    assert second.kind == "settlement"
    assert second.scope == "predictions"
    assert second.reason == "interval"
    assert db_session.query(RefreshJob).filter_by(kind="settlement").count() == 1


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


def test_claim_next_job_prioritizes_settlement_after_current_shadow_before_prop_refresh(db_session):
    prop_refresh = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="queued",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )
    settlement = RefreshJob(
        kind="settlement",
        scope="predictions",
        reason="interval",
        status="queued",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    shadow_capture = RefreshJob(
        kind="shadow_capture",
        scope="current_slate",
        reason="follow_up",
        status="queued",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add_all([prop_refresh, settlement, shadow_capture])
    db_session.commit()

    first = refresh_jobs._claim_next_job(db_session)
    assert first is not None
    assert first.id == shadow_capture.id

    refresh_jobs._guarded_complete_job(db_session, first.id)
    db_session.commit()

    second = refresh_jobs._claim_next_job(db_session)
    assert second is not None
    assert second.id == settlement.id
    assert second.kind == "settlement"


def test_claim_next_job_prioritizes_shadow_backfill_before_prop_refresh(db_session):
    prop_refresh = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="queued",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )
    shadow_backfill = RefreshJob(
        kind="shadow_capture",
        scope="backfill",
        reason="maintenance_follow_up",
        status="queued",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add_all([prop_refresh, shadow_backfill])
    db_session.commit()

    claimed = refresh_jobs._claim_next_job(db_session)

    assert claimed is not None
    assert claimed.id == shadow_backfill.id
    assert claimed.kind == "shadow_capture"


def test_process_refresh_job_queue_once_requeues_and_completes_settlement_batches(db_session, monkeypatch):
    job = RefreshJob(
        kind="settlement",
        scope="predictions",
        reason="interval",
        status="queued",
    )
    db_session.add(job)
    db_session.commit()

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))
    single_cursors: list[dict | None] = []
    parlay_cursors: list[dict | None] = []

    def _settle_predictions_batch(db, *, limit, cursor=None, **kwargs):
        single_cursors.append(cursor)
        assert "latest_only_per_key" not in kwargs, (
            "settlement job must not pass latest_only_per_key — bug #12"
        )
        assert limit == refresh_jobs.PREDICTION_SETTLEMENT_BATCH_SIZE
        if cursor is None:
            return {"processed": 100, "updated": 8, "won": 5, "lost": 3}, {
                "captured_at": "2026-04-07T18:00:00+00:00",
                "prediction_id": 100,
            }
        return {"processed": 20, "updated": 2, "won": 1, "lost": 1}, None

    def _settle_parlay_predictions_batch(db, *, limit, cursor=None):
        parlay_cursors.append(cursor)
        assert limit == refresh_jobs.PARLAY_SETTLEMENT_BATCH_SIZE
        return {"processed": 4, "updated": 1, "won": 1}, None

    monkeypatch.setattr(refresh_jobs, "settle_predictions_batch", _settle_predictions_batch)
    monkeypatch.setattr(refresh_jobs, "settle_parlay_predictions_batch", _settle_parlay_predictions_batch)

    first = refresh_jobs.process_refresh_job_queue_once()
    assert first is not None
    assert first.kind == "settlement"
    assert first.status == "queued"
    db_session.refresh(job)
    assert job.run_id is not None
    assert job.details["phase"] == "single_predictions"
    assert job.details["cursor"]["prediction_id"] == 100
    assert job.details["single_settlement_summary"]["processed"] == 100

    second = refresh_jobs.process_refresh_job_queue_once()
    assert second is not None
    assert second.status == "queued"
    db_session.refresh(job)
    assert job.details["phase"] == "parlay_predictions"
    assert job.details["cursor"] == {}
    assert job.details["single_settlement_summary"]["processed"] == 120

    third = refresh_jobs.process_refresh_job_queue_once()
    assert third is not None
    assert third.status == "completed"
    db_session.refresh(job)
    run = db_session.get(Run, job.run_id)
    assert run is not None
    assert run.status == "completed"
    assert job.details["parlay_settlement_summary"]["processed"] == 4
    assert run.details["single_settlement_summary"]["updated"] == 10
    assert run.details["parlay_settlement_summary"]["updated"] == 1
    assert single_cursors == [None, {"captured_at": "2026-04-07T18:00:00+00:00", "prediction_id": 100}]
    assert parlay_cursors == [None]


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


def test_process_refresh_job_queue_once_runs_multiple_prop_refresh_batches_within_budget(db_session, monkeypatch):
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
    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(maintenance_claim_budget_seconds=25, refresh_job_stale_minutes=30),
    )
    perf_counter_values = iter([0.0, 5.0, 30.0])
    monkeypatch.setattr(refresh_jobs, "perf_counter", lambda: next(perf_counter_values))

    call_count = {"count": 0}

    def _advance(db, job):
        call_count["count"] += 1
        job.details = {"phase": "watchlist_score_batch", "cursor": {"market_id": 500 + call_count["count"]}}
        return run, False

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "prop_refresh"
    assert result.status == "queued"
    assert call_count["count"] == 2
    db_session.refresh(job)
    assert job.run_id == run.id
    assert job.status == "queued"
    assert job.details["cursor"]["market_id"] == 502


def test_process_refresh_job_queue_once_yields_prop_refresh_when_current_slate_arrives(db_session, monkeypatch):
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
    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(maintenance_claim_budget_seconds=25, refresh_job_stale_minutes=30),
    )
    perf_counter_values = iter([0.0, 1.0])
    monkeypatch.setattr(refresh_jobs, "perf_counter", lambda: next(perf_counter_values))

    call_count = {"count": 0}

    def _advance(db, job):
        call_count["count"] += 1
        if db.query(RefreshJob).filter_by(kind="refresh", scope="current_slate").count() == 0:
            db.add(
                RefreshJob(
                    kind="refresh",
                    scope="current_slate",
                    reason="manual",
                    status="queued",
                )
            )
            db.flush()
        job.details = {"phase": "watchlist_score_batch", "cursor": {"market_id": 700}}
        return run, False

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _advance)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "prop_refresh"
    assert result.status == "queued"
    assert call_count["count"] == 1
    db_session.refresh(job)
    assert job.status == "queued"
    assert db_session.query(RefreshJob).filter_by(kind="refresh", scope="current_slate", status="queued").count() == 1


def test_process_refresh_job_queue_once_requeues_prop_refresh_after_transient_http_error(db_session, monkeypatch):
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="queued",
        details={
            "phase": "combo_discovery_page",
            "cursor": {"kalshi_cursor": "abc"},
            "kalshi_summary": {"processed": 11},
        },
    )
    db_session.add(job)
    db_session.commit()

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))

    def _raise_transport_error(db, job):
        raise httpx.ReadError(
            "connection reset by peer",
            request=httpx.Request("GET", "https://example.test/markets"),
        )

    monkeypatch.setattr(refresh_jobs, "advance_prop_refresh_job", _raise_transport_error)

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "prop_refresh"
    assert result.status == "queued"
    db_session.refresh(job)
    assert job.status == "queued"
    assert job.started_at is None
    assert job.error_message is None
    assert job.details["phase"] == "combo_discovery_page"
    assert job.details["cursor"] == {"kalshi_cursor": "abc"}
    assert job.details["kalshi_summary"] == {"processed": 11}
    assert "connection reset by peer" in job.details["last_transient_error"]


def test_runtime_state_summarizes_pending_combo_legs_for_health_payloads(db_session, monkeypatch, client):
    job = RefreshJob(
        kind="prop_refresh",
        scope="maintenance",
        reason="interval",
        status="running",
        details={
            "phase": "combo_discovery_page",
            "cursor": {
                "kalshi_cursor": "cursor-123",
                "pending_combo_legs": [
                    {"market_ticker": "KXNBAPTS-1"},
                    {"market_ticker": "KXNBAPTS-2"},
                    {"market_ticker": "KXNBAPTS-3"},
                    {"market_ticker": "KXNBAPTS-4"},
                ],
            },
        },
    )
    db_session.add(job)
    db_session.commit()

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DbSessionContext(db_session))

    runtime = scheduler.get_refresh_runtime_state()
    cursor = runtime["active_prop_refresh_job"]["details"]["cursor"]
    assert cursor["kalshi_cursor"] == "cursor-123"
    assert cursor["pending_combo_leg_count"] == 4
    assert len(cursor["pending_combo_leg_preview"]) == 3
    assert "pending_combo_legs" not in cursor

    health = client.get("/health")
    assert health.status_code == 200
    health_cursor = health.json()["active_prop_refresh_job"]["details"]["cursor"]
    assert health_cursor["pending_combo_leg_count"] == 4
    assert "pending_combo_legs" not in health_cursor

    diagnostics = client.get("/ops/watchlist/diagnostics")
    assert diagnostics.status_code == 200
    diagnostics_cursor = diagnostics.json()["active_prop_refresh_job"]["details"]["cursor"]
    assert diagnostics_cursor["pending_combo_leg_count"] == 4
    assert "pending_combo_legs" not in diagnostics_cursor


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


# -----------------------------------------------------------------------------
# Bug #21: ``weekly_model_retrain`` is registered NEITHER as a function nor
# as a scheduler job — training lives in GitHub Actions now.
# -----------------------------------------------------------------------------


def test_scheduler_does_not_register_weekly_model_retrain_job(monkeypatch):
    """Bug #21: the old in-API retrain APScheduler entry silently no-op'd
    on any deploy that didn't ship ``apps/ml`` + writable artifact paths.
    Training moved to .github/workflows/ml-retrain.yml; this guard makes
    sure no one accidentally re-adds it to the scheduler. If you're
    here because this test failed, the right answer is to add the new
    job to the GH Actions workflow, not the scheduler."""
    # Capture every ``scheduler.add_job`` call ``start_scheduler`` makes.
    add_job_calls: list[dict[str, object]] = []

    class _SpyScheduler:
        running = False

        def add_job(self, _func, **kwargs):
            add_job_calls.append(kwargs)

        def start(self):  # pragma: no cover - never invoked here
            pass

    monkeypatch.setattr(scheduler, "scheduler", _SpyScheduler())
    # Force the scheduler-enabled gate open so add_job actually runs.
    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: SimpleNamespace(
            scheduler_enabled=True,
            queue_poll_interval_seconds=60,
            cleanup_interval_hours=12,
            advanced_stats_enabled=False,
        ),
    )
    # Codex round-1 P3 on PR #51: ``start_scheduler`` calls
    # ``schedule_event_refreshes`` at the end, which opens
    # ``SessionLocal`` and queries the live ``events`` table. Stub it so
    # this unit test stays off the DB — we only care about which
    # ``add_job`` ids land.
    monkeypatch.setattr(scheduler, "schedule_event_refreshes", lambda: None)

    scheduler.start_scheduler()

    job_ids = {call.get("id") for call in add_job_calls}
    assert "weekly_model_retrain" not in job_ids, (
        "Weekly retrain belongs in .github/workflows/ml-retrain.yml, "
        f"not the API scheduler. Registered ids: {sorted(filter(None, job_ids))}"
    )
    # Sanity check: the spy actually saw OTHER add_job calls. If this
    # collapses to 0 the spy is broken and the assertion above would
    # vacuously pass.
    assert len(add_job_calls) >= 5

    # The retrain function itself should also be gone from the module
    # so accidental imports fail loudly rather than silently no-op.
    assert not hasattr(scheduler, "_weekly_model_retrain_job"), (
        "Remove ``_weekly_model_retrain_job`` along with its scheduler entry."
    )
