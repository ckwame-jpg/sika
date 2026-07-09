"""Smarter NFL PR 6 — ESPN NFL injury-report loader + job wiring.

Mirrors ``test_wnba_injury_report_loader.py``'s core cases: cache hit,
no-network stale serve, fetch + parse + upsert, HTTP-error fallback,
and the scheduler/job registration for ``nfl_injury_refresh``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx

from app.models import NflInjuryReportCache
from app.services.nfl_injury_report import load_nfl_injury_report
from app.services.refresh_jobs import REFRESH_JOB_KINDS


NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


class _StubHttpClient:
    def __init__(self, payload: Any = None, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(url)
        return httpx.Response(
            self.status_code, json=self.payload, request=httpx.Request("GET", url)
        )


def _espn_payload() -> dict[str, Any]:
    return {
        "injuries": [
            {
                "id": "22",
                "displayName": "Philadelphia Eagles",
                "injuries": [
                    {
                        "status": "Questionable",
                        "athlete": {"displayName": "Jalen Hurts"},
                        "type": {"description": "Ankle"},
                        "details": {"type": "Ankle"},
                    },
                ],
            },
        ],
    }


def test_loader_fetches_parses_and_caches(db_session) -> None:
    from app.clients.espn import EspnPublicClient

    client = EspnPublicClient(http_client=_StubHttpClient(payload=_espn_payload()))
    payload = load_nfl_injury_report(db_session, client=client, allow_network=True, now=NOW)
    assert "Jalen Hurts" in payload["players"]
    assert payload["players"]["Jalen Hurts"]["status"] == "Questionable"
    row = db_session.query(NflInjuryReportCache).one()
    assert row.fetched_date == "2026-09-10"
    # Second call inside the TTL serves from cache.
    stub2 = _StubHttpClient(payload={"injuries": []})
    again = load_nfl_injury_report(
        db_session, client=EspnPublicClient(http_client=stub2),
        allow_network=True, now=NOW + timedelta(minutes=5),
    )
    assert "Jalen Hurts" in again["players"]
    assert stub2.calls == []


def test_loader_serves_stale_without_network(db_session) -> None:
    db_session.add(NflInjuryReportCache(
        fetched_date="2026-09-10",
        payload={"report_updated_at": "2026-09-10T08:00:00+00:00",
                 "players": {"Old Guy": {"status": "Out", "designation": "Knee"}}},
        cached_at=NOW - timedelta(hours=6), expires_at=NOW - timedelta(hours=5),
    ))
    db_session.flush()
    payload = load_nfl_injury_report(db_session, allow_network=False, now=NOW)
    assert "Old Guy" in payload["players"]
    empty = load_nfl_injury_report(
        db_session, allow_network=False, now=NOW + timedelta(days=2),
    )
    assert empty == {"report_updated_at": None, "players": {}}


def test_loader_falls_back_to_cache_on_http_error(db_session) -> None:
    from app.clients.espn import EspnPublicClient

    db_session.add(NflInjuryReportCache(
        fetched_date="2026-09-10",
        payload={"report_updated_at": "old", "players": {"Old Guy": {"status": "Out"}}},
        cached_at=NOW - timedelta(hours=6), expires_at=NOW - timedelta(hours=5),
    ))
    db_session.flush()
    failing = EspnPublicClient(http_client=_StubHttpClient(status_code=503))
    payload = load_nfl_injury_report(db_session, client=failing, allow_network=True, now=NOW)
    assert "Old Guy" in payload["players"]


def test_nfl_injury_refresh_kind_and_timeout(monkeypatch) -> None:
    assert "nfl_injury_refresh" in REFRESH_JOB_KINDS
    from app.models import RefreshJob
    from app.services import refresh_jobs

    monkeypatch.setattr(
        refresh_jobs, "get_settings",
        lambda: SimpleNamespace(maintenance_claim_budget_seconds=0.05, refresh_job_stale_minutes=30),
    )
    monkeypatch.setattr(refresh_jobs, "WORKER_TIMEOUT_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(refresh_jobs, "NFL_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS", 0.7)
    job = RefreshJob(kind="nfl_injury_refresh", scope="nfl", reason="interval", status="running")
    assert refresh_jobs._worker_timeout_seconds(job) == 0.7


def test_queue_nfl_injury_refresh_gated_and_registered(monkeypatch) -> None:
    from app.services import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "_nfl_events_upcoming", lambda: False)
    assert scheduler_module._queue_nfl_injury_refresh_job() is False

    monkeypatch.setattr(scheduler_module, "_nfl_events_upcoming", lambda: True)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        scheduler_module, "_queue_job",
        lambda **kwargs: captured.update(kwargs) or True,
    )
    assert scheduler_module._queue_nfl_injury_refresh_job() is True
    assert captured == {"kind": "nfl_injury_refresh", "scope": "nfl", "reason": "interval"}


def test_start_scheduler_registers_nfl_injury_cron(monkeypatch) -> None:
    from app.services import scheduler as scheduler_module

    job_ids: list[str] = []

    class _SpyScheduler:
        running = False

        def add_job(self, fn, *, trigger, id, **kwargs):  # noqa: A002
            job_ids.append(id)

        def start(self) -> None:
            pass

    monkeypatch.setattr(scheduler_module, "scheduler", _SpyScheduler())
    monkeypatch.setattr(
        scheduler_module, "get_settings",
        lambda: SimpleNamespace(
            default_timezone="UTC", scheduler_enabled=True,
            queue_poll_interval_seconds=60, cleanup_interval_hours=12,
            advanced_stats_enabled=False,
        ),
    )
    monkeypatch.setattr(scheduler_module, "schedule_event_refreshes", lambda: None)
    scheduler_module.start_scheduler()
    assert "nfl_injury_refresh_hourly" in job_ids
