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
    monkeypatch.setattr(app_main, "sync_family_runtime_health", lambda db: calls.append("sync_runtime"))
    monkeypatch.setattr(app_main, "reconcile_stale_jobs", lambda db: calls.append("reconcile_stale_jobs"))
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
        "sync_runtime",
        "reconcile_stale_jobs",
        "sync",
        "start_scheduler",
        "queue",
        "stop_scheduler",
    ]


def test_lifespan_logs_and_continues_when_startup_refresh_queue_errors(monkeypatch, caplog):
    """Bug #47 — startup-refresh enqueue must not block boot, but the
    pre-fix code silently swallowed the exception. The fix logs the
    traceback so operators can see what went wrong while the rest of
    startup proceeds."""
    import logging

    calls: list[str] = []
    original_scheduler_enabled = app_main.settings.scheduler_enabled

    monkeypatch.setattr(app_main, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(app_main, "SessionLocal", _session_local)
    monkeypatch.setattr(app_main, "seed_sports", lambda db: calls.append("seed_sports"))
    monkeypatch.setattr(app_main, "sync_family_runtime_health", lambda db: calls.append("sync_runtime"))
    monkeypatch.setattr(app_main, "reconcile_stale_jobs", lambda db: calls.append("reconcile_stale_jobs"))
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
        with caplog.at_level(logging.ERROR, logger=app_main.logger.name):
            asyncio.run(_run_lifespan())
    finally:
        app_main.settings.scheduler_enabled = original_scheduler_enabled

    # Boot still completes — no "queue" entry because the function
    # raised, but stop_scheduler ran on yield exit.
    assert calls == [
        "init_db",
        "seed_sports",
        "sync_runtime",
        "reconcile_stale_jobs",
        "sync",
        "start_scheduler",
        "stop_scheduler",
    ]
    # Bug #47 — the exception is logged, not swallowed silently. The
    # log record includes the original exception via ``logger.exception``.
    assert any(
        "Startup refresh enqueue failed" in record.getMessage()
        and record.exc_info is not None
        for record in caplog.records
    ), "Expected an ERROR log capturing the startup-refresh enqueue failure"


def test_lifespan_skips_scheduler_start_when_disabled(monkeypatch):
    calls: list[str] = []
    original_scheduler_enabled = app_main.settings.scheduler_enabled

    monkeypatch.setattr(app_main, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(app_main, "SessionLocal", _session_local)
    monkeypatch.setattr(app_main, "seed_sports", lambda db: calls.append("seed_sports"))
    monkeypatch.setattr(app_main, "sync_family_runtime_health", lambda db: calls.append("sync_runtime"))
    monkeypatch.setattr(app_main, "reconcile_stale_jobs", lambda db: calls.append("reconcile_stale_jobs"))
    monkeypatch.setattr(app_main, "sync_refresh_runtime_state_from_db", lambda: calls.append("sync"))
    monkeypatch.setattr(app_main, "start_scheduler", lambda: calls.append("start_scheduler"))
    monkeypatch.setattr(app_main, "queue_startup_refresh_if_stale", lambda: calls.append("queue"))
    monkeypatch.setattr(app_main, "stop_scheduler", lambda: calls.append("stop_scheduler"))
    app_main.settings.scheduler_enabled = False

    try:
        asyncio.run(_run_lifespan())
    finally:
        app_main.settings.scheduler_enabled = original_scheduler_enabled

    assert calls == [
        "init_db",
        "seed_sports",
        "sync_runtime",
        "reconcile_stale_jobs",
        "sync",
        "stop_scheduler",
    ]
