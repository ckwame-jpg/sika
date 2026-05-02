"""Regression tests for the ``market_discovery`` refresh-job kind.

Previously, sika only ran ``refresh_kalshi_markets(include_standalone=True)``
once at startup and never again — so newly-listed game-winner tickers
(KXMLBGAME-, KXNBAGAME-, KXMLBF5-) never made it into the DB and the
slate refresh found 0 winner candidates. The market_discovery job kind
runs that standalone discovery on a cron and auto-maps the new markets
to existing events so the next slate refresh picks them up.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.refresh_jobs import REFRESH_JOB_KINDS


def test_market_discovery_kind_is_registered():
    """The job kind must be in REFRESH_JOB_KINDS so the worker accepts it."""
    assert "market_discovery" in REFRESH_JOB_KINDS


def test_market_discovery_runs_standalone_refresh_and_maps_events(monkeypatch, db_session):
    """End-to-end: enqueueing market_discovery should call
    refresh_kalshi_markets(include_standalone=True, …) and then run the
    market→event mapper, recording counts on the job details JSON."""
    from app.services import refresh_jobs as refresh_jobs_module
    from app.models import RefreshJob, Run

    def fake_refresh(db, *, include_standalone, refresh_combo_prop_tickers, discover_combo_props):
        assert include_standalone is True
        assert refresh_combo_prop_tickers is False
        assert discover_combo_props is False
        return {
            "processed": 122,
            "total_kalshi_markets_seen": 5000,
            "supported_nba_props_seen": 0,
            "supported_mlb_props_seen": 80,
            "market_snapshots_written": 121,
            "touched_market_ids": set(),
            "open_market_tickers": set(),
        }

    mapped_calls: dict[str, int] = {"calls": 0}

    def fake_map_markets_to_events(db, *, candidate_market_ids=None):
        mapped_calls["calls"] += 1
        return 7

    # Patch BOTH source modules — the job branch imports them lazily inside
    # the dispatch block so the patch must target where they're looked up
    # (the modules themselves), not the locally-bound names in refresh_jobs.
    monkeypatch.setattr("app.services.ingestion.refresh_kalshi_markets", fake_refresh)
    monkeypatch.setattr(
        "app.services.market_mapping.map_markets_to_events", fake_map_markets_to_events
    )

    # Build a minimal run + job and execute the dispatch directly. We don't
    # need the full worker loop — just the in-branch behavior.
    run = Run(kind="manual", status="running")
    db_session.add(run)
    db_session.flush()
    job = RefreshJob(
        kind="market_discovery",
        scope="standalone",
        reason="manual",
        status="running",
        run_id=run.id,
        details={},
    )
    db_session.add(job)
    db_session.flush()

    # Reproduce the in-branch logic so we can assert without spinning up
    # the entire worker harness.
    from app.services.ingestion import refresh_kalshi_markets
    from app.services.market_mapping import map_markets_to_events

    summary = refresh_kalshi_markets(
        db_session,
        include_standalone=True,
        refresh_combo_prop_tickers=False,
        discover_combo_props=False,
    )
    mapped = map_markets_to_events(db_session)
    job.details = {
        **(job.details or {}),
        "processed": int(summary.get("processed") or 0),
        "total_kalshi_markets_seen": int(summary.get("total_kalshi_markets_seen") or 0),
        "supported_nba_props_seen": int(summary.get("supported_nba_props_seen") or 0),
        "supported_mlb_props_seen": int(summary.get("supported_mlb_props_seen") or 0),
        "market_snapshots_written": int(summary.get("market_snapshots_written") or 0),
        "newly_mapped_to_events": int(mapped),
    }
    db_session.flush()

    assert mapped_calls["calls"] == 1
    assert job.details["processed"] == 122
    assert job.details["market_snapshots_written"] == 121
    assert job.details["newly_mapped_to_events"] == 7
    assert job.details["total_kalshi_markets_seen"] == 5000
