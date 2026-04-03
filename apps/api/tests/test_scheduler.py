from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import scheduler


class _DummySession:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


@contextmanager
def _session_local():
    yield _DummySession()


class _FakeScheduler:
    def __init__(self):
        self.jobs: list[dict[str, object]] = []

    def add_job(self, func, **kwargs):
        self.jobs.append({"func": func, **kwargs})


def _reset_runtime_state():
    scheduler._set_refresh_runtime_state(  # type: ignore[attr-defined]
        refresh_status="idle",
        refresh_reason="none",
        last_successful_refresh_at=None,
        refresh_error_message=None,
    )


def test_startup_refresh_needed_when_no_successful_refresh(monkeypatch):
    monkeypatch.setattr(scheduler, "_latest_successful_refresh_finished_at", lambda: None)

    assert scheduler.startup_refresh_needed(now=datetime(2026, 4, 2, 23, 0, tzinfo=timezone.utc)) is True


def test_startup_refresh_needed_respects_stale_threshold(monkeypatch):
    now = datetime(2026, 4, 2, 23, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_latest_successful_refresh_finished_at", lambda: now - timedelta(minutes=5))
    assert scheduler.startup_refresh_needed(now=now) is False

    monkeypatch.setattr(scheduler, "_latest_successful_refresh_finished_at", lambda: now - timedelta(minutes=25))
    assert scheduler.startup_refresh_needed(now=now) is True


def test_queue_startup_refresh_if_stale_enqueues_background_job(monkeypatch):
    _reset_runtime_state()
    fake_scheduler = _FakeScheduler()

    monkeypatch.setattr(scheduler, "startup_refresh_needed", lambda now=None: True)
    monkeypatch.setattr(scheduler, "scheduler", fake_scheduler)

    assert scheduler.queue_startup_refresh_if_stale() is True
    assert len(fake_scheduler.jobs) == 1
    assert fake_scheduler.jobs[0]["id"] == "startup_refresh"

    runtime = scheduler.get_refresh_runtime_state()
    assert runtime["refresh_status"] == "queued"
    assert runtime["refresh_reason"] == "startup"
    assert runtime["data_stale"] is True


def test_queue_startup_refresh_if_stale_skips_when_fresh(monkeypatch):
    _reset_runtime_state()
    fake_scheduler = _FakeScheduler()

    monkeypatch.setattr(scheduler, "startup_refresh_needed", lambda now=None: False)
    monkeypatch.setattr(scheduler, "scheduler", fake_scheduler)

    assert scheduler.queue_startup_refresh_if_stale() is False
    assert fake_scheduler.jobs == []

    runtime = scheduler.get_refresh_runtime_state()
    assert runtime["refresh_status"] == "idle"
    assert runtime["refresh_reason"] == "none"


def test_run_refresh_cycle_now_updates_runtime_state(monkeypatch):
    _reset_runtime_state()
    calls = {"refresh": 0, "schedule": 0}
    finished_at = datetime(2026, 4, 3, 2, 5, tzinfo=timezone.utc)

    monkeypatch.setattr(scheduler, "SessionLocal", _session_local)
    monkeypatch.setattr(
        scheduler,
        "run_refresh_cycle",
        lambda db: calls.__setitem__("refresh", calls["refresh"] + 1) or SimpleNamespace(
            id=7,
            status="completed",
            records_processed=12,
            finished_at=finished_at,
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "schedule_event_refreshes",
        lambda: calls.__setitem__("schedule", calls["schedule"] + 1),
    )

    run = scheduler.run_refresh_cycle_now(reason="manual")

    assert run is not None
    assert run.id == 7
    assert calls == {"refresh": 1, "schedule": 1}

    runtime = scheduler.get_refresh_runtime_state()
    assert runtime["refresh_status"] == "idle"
    assert runtime["refresh_reason"] == "none"
    assert runtime["last_successful_refresh_at"] == finished_at
    assert runtime["refresh_error_message"] is None


def test_run_refresh_cycle_now_marks_failure_and_raises(monkeypatch):
    _reset_runtime_state()

    monkeypatch.setattr(scheduler, "SessionLocal", _session_local)

    def _boom(_db):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(scheduler, "run_refresh_cycle", _boom)
    monkeypatch.setattr(scheduler, "schedule_event_refreshes", lambda: None)

    with pytest.raises(RuntimeError, match="upstream unavailable"):
        scheduler.run_refresh_cycle_now(reason="manual")

    runtime = scheduler.get_refresh_runtime_state()
    assert runtime["refresh_status"] == "failed"
    assert runtime["refresh_reason"] == "manual"
    assert runtime["refresh_error_message"] == "upstream unavailable"


def test_health_route_returns_refresh_runtime_fields(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.get_refresh_runtime_state",
        lambda: {
            "refresh_status": "running",
            "refresh_reason": "startup",
            "last_successful_refresh_at": datetime(2026, 4, 3, 1, 55, tzinfo=timezone.utc),
            "data_stale": True,
            "refresh_error_message": None,
        },
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "environment": "development",
        "scheduler_enabled": False,
        "refresh_status": "running",
        "refresh_reason": "startup",
        "last_successful_refresh_at": "2026-04-03T01:55:00Z",
        "data_stale": True,
        "refresh_error_message": None,
    }


def test_refresh_route_returns_conflict_when_refresh_already_running(client, monkeypatch):
    monkeypatch.setattr("app.api.routes.run_refresh_cycle_now", lambda reason="manual": None)

    response = client.post("/jobs/refresh")

    assert response.status_code == 409
    assert response.json() == {"detail": "Refresh already in progress"}
