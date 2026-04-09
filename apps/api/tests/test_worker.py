from contextlib import contextmanager

import pytest

from app import worker as app_worker


class _SeedSession:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


class _StopLoop(Exception):
    pass


@contextmanager
def _session_local():
    yield _SeedSession()


def test_worker_main_starts_scheduler_and_queues_startup_refresh(monkeypatch):
    calls: list[str] = []

    class _Settings:
        scheduler_enabled = True

    monkeypatch.setattr(app_worker, "get_settings", lambda: _Settings())
    monkeypatch.setattr(app_worker, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(app_worker, "SessionLocal", _session_local)
    monkeypatch.setattr(app_worker, "seed_sports", lambda db: calls.append("seed_sports"))
    monkeypatch.setattr(app_worker, "sync_family_runtime_health", lambda db: calls.append("sync_runtime"))
    monkeypatch.setattr(app_worker, "reconcile_stale_jobs", lambda db: calls.append("reconcile_stale_jobs"))
    monkeypatch.setattr(app_worker, "start_scheduler", lambda: calls.append("start_scheduler"))
    monkeypatch.setattr(app_worker, "queue_startup_refresh_if_stale", lambda: calls.append("queue"))
    monkeypatch.setattr(app_worker, "stop_scheduler", lambda: calls.append("stop_scheduler"))
    monkeypatch.setattr(app_worker.time, "sleep", lambda _seconds: (_ for _ in ()).throw(_StopLoop()))

    with pytest.raises(_StopLoop):
        app_worker.main()

    assert calls == [
        "init_db",
        "seed_sports",
        "sync_runtime",
        "reconcile_stale_jobs",
        "start_scheduler",
        "queue",
        "stop_scheduler",
    ]


def test_worker_main_skips_scheduler_when_disabled(monkeypatch):
    calls: list[str] = []

    class _Settings:
        scheduler_enabled = False

    monkeypatch.setattr(app_worker, "get_settings", lambda: _Settings())
    monkeypatch.setattr(app_worker, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(app_worker, "SessionLocal", _session_local)
    monkeypatch.setattr(app_worker, "seed_sports", lambda db: calls.append("seed_sports"))
    monkeypatch.setattr(app_worker, "sync_family_runtime_health", lambda db: calls.append("sync_runtime"))
    monkeypatch.setattr(app_worker, "reconcile_stale_jobs", lambda db: calls.append("reconcile_stale_jobs"))
    monkeypatch.setattr(app_worker, "start_scheduler", lambda: calls.append("start_scheduler"))
    monkeypatch.setattr(app_worker, "queue_startup_refresh_if_stale", lambda: calls.append("queue"))
    monkeypatch.setattr(app_worker, "stop_scheduler", lambda: calls.append("stop_scheduler"))
    monkeypatch.setattr(app_worker.time, "sleep", lambda _seconds: (_ for _ in ()).throw(_StopLoop()))

    with pytest.raises(_StopLoop):
        app_worker.main()

    assert calls == [
        "init_db",
        "seed_sports",
        "sync_runtime",
        "reconcile_stale_jobs",
        "stop_scheduler",
    ]
