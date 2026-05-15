"""Regression tests for the ``nba_injury_refresh`` and
``nba_referee_refresh`` refresh-job kinds.

Phase 2-2 follow-ups for:
- Smarter #17 (NBA injury report cache loader, PR #98)
- Smarter #13 (NBA referee assignments cache loader, PR #101)

Both kinds wrap their respective ``load_*`` cache-or-fetch functions
and write a small summary into ``job.details`` so operators can see
what landed on the last tick.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.refresh_jobs import REFRESH_JOB_KINDS


# -- Registration -----------------------------------------------------


def test_nba_injury_refresh_kind_is_registered() -> None:
    assert "nba_injury_refresh" in REFRESH_JOB_KINDS


def test_nba_referee_refresh_kind_is_registered() -> None:
    assert "nba_referee_refresh" in REFRESH_JOB_KINDS


# -- Dispatch behavior (reproduce the in-branch logic) ---------------


def test_nba_injury_refresh_dispatch_calls_loader_and_records_counts(
    db_session, monkeypatch,
) -> None:
    """``nba_injury_refresh`` should call
    ``load_nba_injury_report(db, allow_network=True)`` and write the
    player count + report_updated_at into ``job.details``."""
    from app.models import RefreshJob, Run

    calls: list[bool] = []

    def fake_loader(db, *, allow_network=False, **kwargs):
        calls.append(allow_network)
        return {
            "report_updated_at": "2026-05-15T12:00:00+00:00",
            "players": {
                "Tatum": {"status": "Out", "designation": "Knee"},
                "Booker": {"status": "Doubtful", "designation": "Ankle"},
            },
        }

    # Patch at the import site (the dispatch block uses
    # ``from app.services.nba_injury_report import load_nba_injury_report``
    # inside the elif branch, so we patch the module-attribute that
    # gets looked up at call time).
    monkeypatch.setattr(
        "app.services.nba_injury_report.load_nba_injury_report", fake_loader,
    )

    run = Run(kind="manual", status="running")
    db_session.add(run)
    db_session.flush()
    job = RefreshJob(
        kind="nba_injury_refresh",
        scope="nba",
        reason="manual",
        status="running",
        run_id=run.id,
        details={},
    )
    db_session.add(job)
    db_session.flush()

    # Reproduce the in-branch logic so we can assert without spinning
    # up the entire worker harness.
    from app.services.nba_injury_report import load_nba_injury_report

    payload = load_nba_injury_report(db_session, allow_network=True)
    job.details = {
        **(job.details or {}),
        "players": len((payload or {}).get("players") or {}),
        "report_updated_at": (payload or {}).get("report_updated_at"),
    }
    db_session.flush()

    assert calls == [True]
    assert job.details["players"] == 2
    assert job.details["report_updated_at"] == "2026-05-15T12:00:00+00:00"


def test_nba_referee_refresh_dispatch_calls_loader_and_records_counts(
    db_session, monkeypatch,
) -> None:
    """``nba_referee_refresh`` should call
    ``load_nba_referee_assignments(db, allow_network=True)`` and write
    the assignment count + page_date into ``job.details``."""
    from app.models import RefreshJob, Run

    calls: list[bool] = []

    def fake_loader(db, *, allow_network=False, **kwargs):
        calls.append(allow_network)
        return {
            "page_date": "May 15, 2026",
            "assignments": [
                {"matchup": "A @ B", "away_team": "A", "home_team": "B",
                 "crew_chief": None, "referee": None, "umpire": None,
                 "alternate": None},
                {"matchup": "C @ D", "away_team": "C", "home_team": "D",
                 "crew_chief": None, "referee": None, "umpire": None,
                 "alternate": None},
                {"matchup": "E @ F", "away_team": "E", "home_team": "F",
                 "crew_chief": None, "referee": None, "umpire": None,
                 "alternate": None},
            ],
        }

    monkeypatch.setattr(
        "app.services.nba_referee_assignments.load_nba_referee_assignments",
        fake_loader,
    )

    run = Run(kind="manual", status="running")
    db_session.add(run)
    db_session.flush()
    job = RefreshJob(
        kind="nba_referee_refresh",
        scope="nba",
        reason="manual",
        status="running",
        run_id=run.id,
        details={},
    )
    db_session.add(job)
    db_session.flush()

    from app.services.nba_referee_assignments import load_nba_referee_assignments

    payload = load_nba_referee_assignments(db_session, allow_network=True)
    job.details = {
        **(job.details or {}),
        "assignments": len((payload or {}).get("assignments") or []),
        "page_date": (payload or {}).get("page_date"),
    }
    db_session.flush()

    assert calls == [True]
    assert job.details["assignments"] == 3
    assert job.details["page_date"] == "May 15, 2026"


def test_nba_injury_refresh_dispatch_tolerates_loader_returning_empty(
    db_session, monkeypatch,
) -> None:
    """When the loader falls back to the empty payload (no cache + no
    network), the dispatch should record ``players=0`` and
    ``report_updated_at=None`` rather than crashing."""
    from app.models import RefreshJob, Run

    monkeypatch.setattr(
        "app.services.nba_injury_report.load_nba_injury_report",
        lambda db, **kwargs: {"report_updated_at": None, "players": {}},
    )

    run = Run(kind="manual", status="running")
    db_session.add(run)
    db_session.flush()
    job = RefreshJob(
        kind="nba_injury_refresh",
        scope="nba",
        reason="manual",
        status="running",
        run_id=run.id,
        details={},
    )
    db_session.add(job)
    db_session.flush()

    from app.services.nba_injury_report import load_nba_injury_report

    payload = load_nba_injury_report(db_session, allow_network=True)
    job.details = {
        **(job.details or {}),
        "players": len((payload or {}).get("players") or {}),
        "report_updated_at": (payload or {}).get("report_updated_at"),
    }
    db_session.flush()

    assert job.details["players"] == 0
    assert job.details["report_updated_at"] is None


def test_nba_referee_refresh_dispatch_tolerates_loader_returning_empty(
    db_session, monkeypatch,
) -> None:
    from app.models import RefreshJob, Run

    monkeypatch.setattr(
        "app.services.nba_referee_assignments.load_nba_referee_assignments",
        lambda db, **kwargs: {"page_date": None, "assignments": []},
    )

    run = Run(kind="manual", status="running")
    db_session.add(run)
    db_session.flush()
    job = RefreshJob(
        kind="nba_referee_refresh",
        scope="nba",
        reason="manual",
        status="running",
        run_id=run.id,
        details={},
    )
    db_session.add(job)
    db_session.flush()

    from app.services.nba_referee_assignments import load_nba_referee_assignments

    payload = load_nba_referee_assignments(db_session, allow_network=True)
    job.details = {
        **(job.details or {}),
        "assignments": len((payload or {}).get("assignments") or []),
        "page_date": (payload or {}).get("page_date"),
    }
    db_session.flush()

    assert job.details["assignments"] == 0
    assert job.details["page_date"] is None


# -- Scheduler entries pinned -----------------------------------------


def test_scheduler_registers_nba_injury_and_referee_refresh_jobs(monkeypatch) -> None:
    """The two new CronTrigger entries should be added when
    ``start_scheduler`` runs. Pins them so they don't quietly drop
    out of the registration list."""
    from app.services import scheduler as scheduler_module
    from types import SimpleNamespace

    add_job_calls: list[dict[str, Any]] = []

    class _SpyScheduler:
        running = False

        def add_job(self, fn, *, trigger, id, **kwargs):  # noqa: A002 — match signature
            add_job_calls.append(
                {"id": id, "trigger": trigger, "fn": fn.__name__ if hasattr(fn, "__name__") else None}
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
    assert "nba_injury_refresh_hourly" in job_ids
    assert "nba_referee_refresh_twice_daily" in job_ids


def test_queue_nba_injury_refresh_job_enqueues_with_correct_kind(monkeypatch) -> None:
    from app.services import scheduler as scheduler_module

    captured: dict[str, Any] = {}

    def fake_queue_job(*, kind, scope, reason):
        captured["kind"] = kind
        captured["scope"] = scope
        captured["reason"] = reason
        return True

    monkeypatch.setattr(scheduler_module, "_queue_job", fake_queue_job)
    result = scheduler_module._queue_nba_injury_refresh_job()
    assert result is True
    assert captured == {"kind": "nba_injury_refresh", "scope": "nba", "reason": "interval"}


def test_queue_nba_referee_refresh_job_enqueues_with_correct_kind(monkeypatch) -> None:
    from app.services import scheduler as scheduler_module

    captured: dict[str, Any] = {}

    def fake_queue_job(*, kind, scope, reason):
        captured["kind"] = kind
        captured["scope"] = scope
        captured["reason"] = reason
        return True

    monkeypatch.setattr(scheduler_module, "_queue_job", fake_queue_job)
    result = scheduler_module._queue_nba_referee_refresh_job()
    assert result is True
    assert captured == {"kind": "nba_referee_refresh", "scope": "nba", "reason": "interval"}


# -- End-to-end dispatch through process_refresh_job_queue_once -------
#
# The reproduce-the-logic-inline tests above pin the contract of the
# elif branches but wouldn't catch a misnamed kind in the elif chain
# (e.g. ``elif job.kind == "nba_injury_refresh:"`` with a trailing
# colon). These tests actually run ``process_refresh_job_queue_once``
# against a queued ``RefreshJob`` row so the dispatch chain is
# exercised end-to-end.


class _DbSessionContext:
    """Mirror the helper from test_scheduler.py for monkeypatching
    ``SessionLocal``. Wrapping the in-memory session in this context
    manager lets ``process_refresh_job_queue_once`` open and close
    "sessions" without losing the seeded state."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def __enter__(self) -> Any:
        return self.session

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


def test_nba_injury_refresh_runs_through_process_queue(db_session, monkeypatch) -> None:
    """Verify the ``elif job.kind == "nba_injury_refresh"`` branch
    inside ``_execute_claimed_job`` actually runs the loader and
    writes details — catches the failure mode where the elif typo
    silently routes to the ``else: raise`` branch."""
    from app.models import RefreshJob
    from app.services import refresh_jobs

    loader_calls: list[bool] = []

    def fake_loader(db, *, allow_network=False, **kwargs):
        loader_calls.append(allow_network)
        return {
            "report_updated_at": "2026-05-15T18:00:00+00:00",
            "players": {"P1": {"status": "Out", "designation": "Knee"}},
        }

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))
    monkeypatch.setattr(
        "app.services.nba_injury_report.load_nba_injury_report", fake_loader,
    )

    job = RefreshJob(
        kind="nba_injury_refresh",
        scope="nba",
        reason="interval",
        status="queued",
    )
    db_session.add(job)
    db_session.commit()

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "nba_injury_refresh"
    assert result.status == "completed"
    assert loader_calls == [True]

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.details.get("players") == 1
    assert job.details.get("report_updated_at") == "2026-05-15T18:00:00+00:00"


def test_nba_referee_refresh_runs_through_process_queue(db_session, monkeypatch) -> None:
    from app.models import RefreshJob
    from app.services import refresh_jobs

    loader_calls: list[bool] = []

    def fake_loader(db, *, allow_network=False, **kwargs):
        loader_calls.append(allow_network)
        return {
            "page_date": "May 15, 2026",
            "assignments": [
                {"matchup": "A @ B", "away_team": "A", "home_team": "B",
                 "crew_chief": None, "referee": None, "umpire": None,
                 "alternate": None},
                {"matchup": "C @ D", "away_team": "C", "home_team": "D",
                 "crew_chief": None, "referee": None, "umpire": None,
                 "alternate": None},
            ],
        }

    monkeypatch.setattr(refresh_jobs, "SessionLocal", lambda: _DbSessionContext(db_session))
    monkeypatch.setattr(
        "app.services.nba_referee_assignments.load_nba_referee_assignments",
        fake_loader,
    )

    job = RefreshJob(
        kind="nba_referee_refresh",
        scope="nba",
        reason="interval",
        status="queued",
    )
    db_session.add(job)
    db_session.commit()

    result = refresh_jobs.process_refresh_job_queue_once()

    assert result is not None
    assert result.kind == "nba_referee_refresh"
    assert result.status == "completed"
    assert loader_calls == [True]

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.details.get("assignments") == 2
    assert job.details.get("page_date") == "May 15, 2026"
