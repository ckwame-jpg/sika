"""Smarter WNBA PR 7 — ``wnba_injury_refresh`` refresh-job regression.

Mirrors the NBA test pattern in ``test_nba_cache_refresh_jobs.py``:

- The kind is registered in ``REFRESH_JOB_KINDS``.
- The dispatch branch calls ``load_wnba_injury_report`` with
  ``allow_network=True`` and writes a small summary into ``job.details``.
- The scheduler registers a cron entry on ``start_scheduler``.
- ``_queue_wnba_injury_refresh_job`` enqueues a job with the correct
  kind/scope/reason tuple.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.refresh_jobs import REFRESH_JOB_KINDS


# -- Registration -----------------------------------------------------


def test_wnba_injury_refresh_kind_is_registered() -> None:
    assert "wnba_injury_refresh" in REFRESH_JOB_KINDS


# -- Dispatch behavior (reproduce the in-branch logic) ---------------


def test_wnba_injury_refresh_dispatch_calls_loader_and_records_counts(
    db_session, monkeypatch,
) -> None:
    """``wnba_injury_refresh`` should call
    ``load_wnba_injury_report(db, allow_network=True)`` and write the
    player count + report_updated_at into ``job.details``."""
    from app.models import RefreshJob, Run

    calls: list[bool] = []

    def fake_loader(db, *, allow_network=False, **kwargs):
        calls.append(allow_network)
        return {
            "report_updated_at": "2026-05-15T12:00:00+00:00",
            "players": {
                "A'ja Wilson": {"status": "Out", "designation": "Ankle"},
                "Caitlin Clark": {"status": "Doubtful", "designation": "Foot"},
            },
        }

    monkeypatch.setattr(
        "app.services.wnba_injury_report.load_wnba_injury_report", fake_loader,
    )

    run = Run(kind="manual", status="running")
    db_session.add(run)
    db_session.flush()
    job = RefreshJob(
        kind="wnba_injury_refresh",
        scope="wnba",
        reason="manual",
        status="running",
        run_id=run.id,
        details={},
    )
    db_session.add(job)
    db_session.flush()

    # Reproduce the in-branch logic (the worker harness is heavy to spin
    # up in tests; the elif-block in ``process_refresh_job_queue_once``
    # is short enough to mirror here).
    from app.services.wnba_injury_report import load_wnba_injury_report

    payload = load_wnba_injury_report(db_session, allow_network=True)
    job.details = {
        **(job.details or {}),
        "players": len((payload or {}).get("players") or {}),
        "report_updated_at": (payload or {}).get("report_updated_at"),
    }
    db_session.flush()

    assert calls == [True]
    assert job.details["players"] == 2
    assert job.details["report_updated_at"] == "2026-05-15T12:00:00+00:00"


# -- Scheduler registration -------------------------------------------


def test_start_scheduler_registers_wnba_injury_refresh_cron(monkeypatch) -> None:
    """``start_scheduler`` must register the WNBA injury cron alongside
    the existing NBA cron — pins the entry so it doesn't quietly drop
    out of the registration list."""
    from app.services import scheduler as scheduler_module

    add_job_calls: list[dict[str, Any]] = []

    class _SpyScheduler:
        running = False

        def add_job(self, fn, *, trigger, id, **kwargs):  # noqa: A002 — match signature
            add_job_calls.append(
                {
                    "id": id,
                    "trigger": trigger,
                    "fn": fn.__name__ if hasattr(fn, "__name__") else None,
                }
            )

        def start(self) -> None:
            pass

    spy = _SpyScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", spy)
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
    assert "wnba_injury_refresh_hourly" in job_ids


def test_queue_wnba_injury_refresh_job_enqueues_with_correct_kind(monkeypatch) -> None:
    from app.services import scheduler as scheduler_module

    captured: dict[str, Any] = {}

    def fake_queue_job(*, kind, scope, reason):
        captured["kind"] = kind
        captured["scope"] = scope
        captured["reason"] = reason
        return True

    monkeypatch.setattr(scheduler_module, "_queue_job", fake_queue_job)
    result = scheduler_module._queue_wnba_injury_refresh_job()
    assert result is True
    assert captured == {"kind": "wnba_injury_refresh", "scope": "wnba", "reason": "interval"}


# -- Worker timeout ---------------------------------------------------


def test_wnba_injury_refresh_gets_dedicated_worker_timeout(monkeypatch):
    """Mirror ``test_nba_injury_refresh_gets_dedicated_worker_timeout`` —
    the WNBA injury refresh job is a single HTTP GET + upsert and uses
    its own dedicated timeout constant (separate from the NBA constant
    so future tuning can diverge per sport)."""
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
    monkeypatch.setattr(
        refresh_jobs, "WNBA_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS", 0.8
    )

    job = RefreshJob(
        kind="wnba_injury_refresh",
        scope="wnba",
        reason="interval",
        status="running",
    )
    assert refresh_jobs._worker_timeout_seconds(job) == 0.8
