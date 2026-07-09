"""Smarter NFL PR 3 — ``nfl_data_refresh`` refresh-job regression.

Mirrors ``test_wnba_cache_refresh_jobs.py``: kind registration, the
queue function's off-season gate, scheduler cron registration, and the
dedicated worker timeout.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.services.refresh_jobs import REFRESH_JOB_KINDS


def test_nfl_data_refresh_kind_is_registered() -> None:
    assert "nfl_data_refresh" in REFRESH_JOB_KINDS


def test_queue_nfl_data_refresh_skips_when_no_upcoming_events(monkeypatch) -> None:
    """Off-season gate: no NFL event inside the window → no enqueue, no
    65 MB nflverse download all spring."""
    from app.services import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "_nfl_events_upcoming", lambda: False)
    called: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scheduler_module, "_queue_job",
        lambda **kwargs: called.append(kwargs) or True,
    )
    assert scheduler_module._queue_nfl_data_refresh_job() is False
    assert called == []


def test_queue_nfl_data_refresh_enqueues_with_correct_kind(monkeypatch) -> None:
    from app.services import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "_nfl_events_upcoming", lambda: True)
    captured: dict[str, Any] = {}

    def fake_queue_job(*, kind, scope, reason):
        captured.update({"kind": kind, "scope": scope, "reason": reason})
        return True

    monkeypatch.setattr(scheduler_module, "_queue_job", fake_queue_job)
    assert scheduler_module._queue_nfl_data_refresh_job() is True
    assert captured == {"kind": "nfl_data_refresh", "scope": "nfl", "reason": "interval"}


def test_nfl_events_upcoming_reads_event_window(db_session, monkeypatch) -> None:
    from app.models import Event
    from app.services import scheduler as scheduler_module

    class _SessionFactory:
        def __call__(self):
            return self

        def __enter__(self):
            return db_session

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(scheduler_module, "SessionLocal", _SessionFactory())
    assert scheduler_module._nfl_events_upcoming() is False

    db_session.add(Event(
        external_id="espn:nfl:401999",
        sport_key="NFL",
        name="Dallas Cowboys at Philadelphia Eagles",
        status="scheduled",
        starts_at=datetime.now(timezone.utc) + timedelta(days=3),
    ))
    db_session.flush()
    assert scheduler_module._nfl_events_upcoming() is True


def test_start_scheduler_registers_nfl_data_refresh_crons(monkeypatch) -> None:
    from app.services import scheduler as scheduler_module

    add_job_calls: list[dict[str, Any]] = []

    class _SpyScheduler:
        running = False

        def add_job(self, fn, *, trigger, id, **kwargs):  # noqa: A002 — match signature
            add_job_calls.append({"id": id})

        def start(self) -> None:
            pass

    monkeypatch.setattr(scheduler_module, "scheduler", _SpyScheduler())
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(
            default_timezone="UTC",
            scheduler_enabled=True,
            queue_poll_interval_seconds=60,
            cleanup_interval_hours=12,
            advanced_stats_enabled=False,
        ),
    )
    monkeypatch.setattr(scheduler_module, "schedule_event_refreshes", lambda: None)

    scheduler_module.start_scheduler()

    job_ids = {call.get("id") for call in add_job_calls}
    assert "nfl_data_refresh_daily" in job_ids
    assert "nfl_data_refresh_sunday" in job_ids


def test_nfl_data_refresh_gets_dedicated_worker_timeout(monkeypatch) -> None:
    from app.models import RefreshJob
    from app.services import refresh_jobs

    monkeypatch.setattr(
        refresh_jobs,
        "get_settings",
        lambda: SimpleNamespace(
            maintenance_claim_budget_seconds=0.05, refresh_job_stale_minutes=30
        ),
    )
    monkeypatch.setattr(refresh_jobs, "WORKER_TIMEOUT_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(refresh_jobs, "NFL_DATA_REFRESH_WORKER_TIMEOUT_SECONDS", 0.9)

    job = RefreshJob(kind="nfl_data_refresh", scope="nfl", reason="interval", status="running")
    assert refresh_jobs._worker_timeout_seconds(job) == 0.9


def test_nfl_data_refresh_dispatch_records_summary(db_session, monkeypatch) -> None:
    """Mirror the elif-branch in ``process_refresh_job_queue_once``: the
    handler calls ``refresh_nfl_data(db)`` and writes the summary
    fields into ``job.details``."""
    from app.models import RefreshJob, Run

    def fake_refresh(db, **kwargs):
        return {
            "season": 2025, "weekly_stats_weeks": 10, "snap_count_weeks": 10,
            "depth_chart_teams": 32, "official_injury_weeks": 10,
            "rated_teams": 32, "schedule_games": 285, "weather_prewarmed": 3,
            "errors": [],
        }

    monkeypatch.setattr("app.services.nfl_advanced.refresh_nfl_data", fake_refresh)

    run = Run(kind="manual", status="running")
    db_session.add(run)
    db_session.flush()
    job = RefreshJob(
        kind="nfl_data_refresh", scope="nfl", reason="manual",
        status="running", run_id=run.id, details={},
    )
    db_session.add(job)
    db_session.flush()

    from app.services.nfl_advanced import refresh_nfl_data

    summary = refresh_nfl_data(db_session)
    job.details = {
        **(job.details or {}),
        "season": summary.get("season"),
        "rated_teams": summary.get("rated_teams"),
        "errors": summary.get("errors") or [],
    }
    db_session.flush()

    assert job.details["season"] == 2025
    assert job.details["rated_teams"] == 32
    assert job.details["errors"] == []
