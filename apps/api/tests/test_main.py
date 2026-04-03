import asyncio
from contextlib import contextmanager

from fastapi import FastAPI

from app import main as app_main


class _SeedSession:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


@contextmanager
def _session_local():
    yield _SeedSession()


async def _run_lifespan():
    async with app_main.lifespan(FastAPI()):
        return None


def test_lifespan_starts_without_blocking_on_startup_refresh(monkeypatch):
    calls: list[str] = []
    original_scheduler_enabled = app_main.settings.scheduler_enabled

    monkeypatch.setattr(app_main, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(app_main, "SessionLocal", _session_local)
    monkeypatch.setattr(app_main, "seed_sports", lambda db: calls.append("seed_sports"))
    monkeypatch.setattr(app_main, "sync_refresh_runtime_state_from_db", lambda: calls.append("sync"))
    monkeypatch.setattr(app_main, "start_scheduler", lambda: calls.append("start_scheduler"))
    monkeypatch.setattr(app_main, "queue_startup_refresh_if_stale", lambda: calls.append("queue"))
    monkeypatch.setattr(app_main, "stop_scheduler", lambda: calls.append("stop_scheduler"))
    app_main.settings.scheduler_enabled = True

    try:
        asyncio.run(_run_lifespan())
    finally:
        app_main.settings.scheduler_enabled = original_scheduler_enabled

    assert calls == [
        "init_db",
        "seed_sports",
        "sync",
        "start_scheduler",
        "queue",
        "stop_scheduler",
    ]


def test_lifespan_swallows_startup_refresh_queue_errors(monkeypatch):
    calls: list[str] = []
    original_scheduler_enabled = app_main.settings.scheduler_enabled

    monkeypatch.setattr(app_main, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(app_main, "SessionLocal", _session_local)
    monkeypatch.setattr(app_main, "seed_sports", lambda db: calls.append("seed_sports"))
    monkeypatch.setattr(app_main, "sync_refresh_runtime_state_from_db", lambda: calls.append("sync"))
    monkeypatch.setattr(app_main, "start_scheduler", lambda: calls.append("start_scheduler"))
    monkeypatch.setattr(
        app_main,
        "queue_startup_refresh_if_stale",
        lambda: (_ for _ in ()).throw(RuntimeError("startup refresh failed")),
    )
    monkeypatch.setattr(app_main, "stop_scheduler", lambda: calls.append("stop_scheduler"))
    app_main.settings.scheduler_enabled = True

    try:
        asyncio.run(_run_lifespan())
    finally:
        app_main.settings.scheduler_enabled = original_scheduler_enabled

    assert calls == [
        "init_db",
        "seed_sports",
        "sync",
        "start_scheduler",
        "stop_scheduler",
    ]
