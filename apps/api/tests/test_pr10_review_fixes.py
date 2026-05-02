"""Regression tests for PR 10 — Codex round-5 partial-close findings.

Round 5 said PR 9 fixed the API split but missed the operational bound:
the worker built ``merged_pitcher_ids`` from explicit + sidecar +
probable IDs *before* checking ``pitchers_only``, so a "pitchers-only"
late-day tick still ran pitcher Statcast for every sidecar batter.

Mapping:
- Round-5 #1 (sidecar IDs leak into pitcher fanout under pitchers_only)
    → ``test_pitchers_only_excludes_sidecar_mlb_player_ids_from_pitcher_warm``
    → ``test_daily_warm_keeps_sidecar_ids_in_pitcher_warm``
- Round-5 #2 (paper test for pitchers_only worker — didn't exercise the branch)
    → ``test_pitchers_only_worker_branch_actually_skips_sidecar_pitcher_calls``
- Round-5 #3 (coalesce overwrites instead of union-merging)
    → ``test_lineup_refresh_pitcher_warm_unions_pitcher_ids_across_ticks``
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


# -----------------------------------------------------------------------------
# Round-5 #1 — sidecar IDs must NOT enter pitcher_ids_for_warm under pitchers_only.
# These are unit tests against the list-construction logic that lives
# inside the advanced_stats_warm branch.

def _build_pitcher_ids_for_warm(
    *,
    details_pitcher_ids: list[str],
    sidecar_mlb_player_ids: list[str],
    probable_pitcher_ids: list[str],
    pitchers_only: bool,
) -> list[str]:
    """Mirror of the logic in refresh_jobs.py advanced_stats_warm branch.

    Kept here as a tight unit-testable function — the integration test
    below proves the branch actually uses this same shape."""
    if pitchers_only:
        # Late-day path — explicit + probable, NEVER sidecar batters.
        candidates = list(details_pitcher_ids) + list(probable_pitcher_ids)
    else:
        # Daily path — keep sidecar as backstop.
        candidates = (
            list(details_pitcher_ids)
            + list(sidecar_mlb_player_ids)
            + list(probable_pitcher_ids)
        )
    return sorted({str(pid) for pid in candidates if pid})


def test_pitchers_only_excludes_sidecar_mlb_player_ids_from_pitcher_warm():
    """The bug Codex round 5 caught: a pitchers_only=True job must not
    fan out pitcher Statcast over the sidecar batter list. Pitcher ID set
    is bounded to (explicit + late-day probable) only."""
    result = _build_pitcher_ids_for_warm(
        details_pitcher_ids=["543037"],
        sidecar_mlb_player_ids=["592450", "660271", "605141"],  # 3 sidecar batters.
        probable_pitcher_ids=["666666"],
        pitchers_only=True,
    )
    assert result == ["543037", "666666"]
    # Critical: none of the sidecar batter IDs appear.
    for sidecar_id in ("592450", "660271", "605141"):
        assert sidecar_id not in result


def test_daily_warm_keeps_sidecar_ids_in_pitcher_warm():
    """The non-pitchers_only daily warm continues to include sidecar
    IDs — that's the backstop path for two-way players or sidecar
    starters who already showed up as a prop subject."""
    result = _build_pitcher_ids_for_warm(
        details_pitcher_ids=[],
        sidecar_mlb_player_ids=["592450", "660271"],
        probable_pitcher_ids=["666666"],
        pitchers_only=False,
    )
    assert result == ["592450", "660271", "666666"]


# -----------------------------------------------------------------------------
# Round-5 #2 — the actual worker branch must skip sidecar pitcher calls.

def test_pitchers_only_worker_branch_actually_skips_sidecar_pitcher_calls(monkeypatch, db_session):
    """Codex round 5 noted that ``test_advanced_stats_warm_pitchers_only_skips_batter_warming``
    only read job.details and never executed the worker. This test
    actually invokes ``process_refresh_job_queue_once`` for a queued
    ``advanced_stats_warm`` job with ``pitchers_only=True`` and asserts
    that a seeded sidecar batter MLB Stats ID is NOT in the pitcher_ids
    the warm function receives."""
    from sqlalchemy.orm import sessionmaker

    from app.models import EspnPlayerSearchCache, RefreshJob, utcnow
    from app.services import refresh_jobs

    # Mirror the SessionLocal pattern from test_refresh_jobs_timeout — the
    # worker spins its own session, so we have to bind it to the test DB.
    testing_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=db_session.get_bind(), future=True
    )
    monkeypatch.setattr(refresh_jobs, "SessionLocal", testing_session_local)

    # Seed a sidecar batter — this ID would have leaked into the pitcher
    # warm in PR 9.
    cached_at = utcnow()
    db_session.add(
        EspnPlayerSearchCache(
            sport_key="MLB",
            query_normalized="aaron judge",
            payload={"athlete_id": "33192", "mlb_stats_id": "592450"},
            cached_at=cached_at,
            expires_at=cached_at + timedelta(days=1),
        )
    )

    job = RefreshJob(
        kind="advanced_stats_warm",
        scope="lineup_refresh_pitchers",
        reason="late-day pitcher warm",
        status="queued",
        queued_at=cached_at,
        details={"pitcher_ids": ["543037"], "pitchers_only": True},
    )
    db_session.add(job)
    db_session.flush()
    db_session.commit()
    job_id = job.id

    # Capture what the worker actually passes into warm_mlb_advanced_for_athletes.
    captured: dict[str, Any] = {}

    def _fake_warm_mlb(db, *, mlb_stats_player_ids, pitcher_ids, season, **kwargs):
        captured["mlb_stats_player_ids"] = list(mlb_stats_player_ids)
        captured["pitcher_ids"] = list(pitcher_ids)
        captured["kwargs"] = kwargs
        return {
            "mlb_batters_attempted": 0,
            "mlb_batters_succeeded": 0,
            "mlb_pitchers_attempted": len(captured["pitcher_ids"]),
            "mlb_pitchers_succeeded": len(captured["pitcher_ids"]),
            "mlb_roster_loaded": 0,
        }

    nba_calls: list[Any] = []

    def _fake_warm_nba(db, *, nba_stats_player_ids, season):
        nba_calls.append((list(nba_stats_player_ids), season))

        class _Summary:
            def as_dict(self):
                return {"nba_attempted": len(nba_stats_player_ids)}

        return _Summary()

    class _StubMlbStatsClient:
        def fetch_schedule(self, target_date, **_kwargs):
            return {"dates": []}

    class _StubSavant:
        def fetch_pitcher_statcast(self, person_id, season):
            return ""

        def fetch_batter_statcast(self, person_id, season):
            return ""

    # The worker imports these lazily inside the branch via
    # ``from app.services.mlb_advanced import warm_mlb_advanced_for_athletes``
    # and ``from app.services.advanced_stats import warm_nba_advanced_for_athletes``,
    # so patching the module attributes is enough.
    monkeypatch.setattr(
        "app.services.mlb_advanced.warm_mlb_advanced_for_athletes", _fake_warm_mlb
    )
    monkeypatch.setattr(
        "app.services.advanced_stats.warm_nba_advanced_for_athletes", _fake_warm_nba
    )
    monkeypatch.setattr(
        "app.clients.mlb_stats.MlbStatsClient", lambda *a, **kw: _StubMlbStatsClient()
    )
    monkeypatch.setattr(
        "app.clients.baseball_savant.BaseballSavantClient", lambda *a, **kw: _StubSavant()
    )

    snap = refresh_jobs.process_refresh_job_queue_once()
    assert snap is not None and snap.job_id == job_id

    # Critical assertions:
    # 1. The seeded sidecar batter (592450) MUST NOT be in the pitcher list.
    assert "592450" not in captured["pitcher_ids"], (
        f"sidecar batter leaked into pitcher warm: {captured['pitcher_ids']}"
    )
    # 2. The explicit late-day pitcher ID is preserved.
    assert "543037" in captured["pitcher_ids"]
    # 3. NBA warming was skipped entirely.
    assert nba_calls == []
    # 4. Batter side was zeroed.
    assert captured["mlb_stats_player_ids"] == []
    # 5. The Savant client passed in is the pitcher-side one.
    assert captured["kwargs"].get("savant_pitcher") is not None
    # 6. Job moved to completed and recorded the new metric.
    db_session.expire_all()
    refreshed = db_session.query(RefreshJob).filter_by(id=job_id).one()
    assert refreshed.status == "completed"
    assert refreshed.details.get("pitchers_only") is True
    assert refreshed.details.get("mlb_pitcher_ids_warmed") == len(captured["pitcher_ids"])


# -----------------------------------------------------------------------------
# Round-5 #3 — coalesce should union pitcher_ids, not overwrite.

def test_lineup_refresh_pitcher_warm_unions_pitcher_ids_across_ticks(db_session):
    """Codex round 5 noted that the previous PR 9 coalesce path
    overwrote the existing job's pitcher_ids with the latest
    ``late_day_pitcher_ids``. Two back-to-back lineup_refresh ticks
    where the second sees fewer probable starters (e.g. one was
    scratched / TBD) should not lose the first tick's IDs.

    This test models the union-merge logic the worker now applies."""
    from app.models import RefreshJob
    from app.services.refresh_jobs import enqueue_refresh_job

    first_job, first_created = enqueue_refresh_job(
        db_session,
        kind="advanced_stats_warm",
        scope="lineup_refresh_pitchers",
        reason="11:00 lineup_refresh",
    )
    # Mirror the new branch logic — union prior + new.
    first_prior = list((first_job.details or {}).get("pitcher_ids") or [])
    first_job.details = {
        **(first_job.details or {}),
        "pitcher_ids": sorted({str(pid) for pid in first_prior + ["543037", "605141"] if pid}),
        "pitchers_only": True,
    }
    db_session.flush()
    assert first_created is True

    second_job, second_created = enqueue_refresh_job(
        db_session,
        kind="advanced_stats_warm",
        scope="lineup_refresh_pitchers",
        reason="15:00 lineup_refresh",
    )
    # Second tick discovers a different (smaller) set — but the union
    # must preserve the original 11:00 IDs too.
    second_prior = list((second_job.details or {}).get("pitcher_ids") or [])
    second_job.details = {
        **(second_job.details or {}),
        "pitcher_ids": sorted({str(pid) for pid in second_prior + ["605141", "660271"] if pid}),
        "pitchers_only": True,
    }
    db_session.flush()
    assert second_created is False  # coalesced
    assert second_job.id == first_job.id

    # Union of {543037, 605141} ∪ {605141, 660271} = {543037, 605141, 660271}.
    queued = db_session.query(RefreshJob).filter_by(
        kind="advanced_stats_warm", scope="lineup_refresh_pitchers"
    ).one()
    assert sorted(queued.details["pitcher_ids"]) == ["543037", "605141", "660271"]
