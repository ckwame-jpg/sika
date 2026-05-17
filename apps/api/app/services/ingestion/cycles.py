"""End-to-end refresh + shadow-capture cycles.

Extracted from ``ingestion/__init__.py`` as part of R2 phase 2.
These are the "run the whole pipeline once" entry points the
scheduler / refresh-job worker call. Each composes:

- ``refresh_sports_data`` (events / sports rosters from public
  providers)
- ``refresh_kalshi_markets`` (Kalshi market list + snapshots)
- ``map_markets_to_events`` (fuzzy mapping)
- ``warm_prop_context_cache`` / ``warm_current_watchlist_prop_context``
  (ESPN player + gamelog warming via ``PropStatsResolver``)
- ``regenerate_watchlist`` (scoring kernel via the scoring package)
- ``settle_predictions`` / ``settle_parlay_predictions`` (Kalshi
  settlement)
- ``persist_current_slate_snapshots`` (current-slate UI snapshot)

``refresh_sports_data`` and ``refresh_kalshi_markets`` still live
in ``ingestion/__init__.py``. ``cycles.py`` reaches them via lazy
imports inside the cycle functions — breaks the otherwise-circular
init graph (a future ingestion-refactor phase can move those into a
``ingestion/refresh.py`` module and switch to top-level imports).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Iterable

import httpx
from sqlalchemy.orm import Session

from app.clients.espn import EspnPublicClient
from app.clients.kalshi import KalshiPublicClient
from app.clients.sports_data import TheSportsDBClient
from app.config import get_settings
from app.models import RefreshJob, Run
from app.services.market_mapping import map_markets_to_events
from app.services.ml import capture_shadow_artifacts, sync_family_runtime_health
from app.services.parlays import settle_parlay_predictions
from app.services.predictions import settle_predictions
from app.services.scoring import (
    PropStatsResolver,
    regenerate_watchlist,
    warm_prop_context_cache,
)
from app.services.trade_desk import persist_current_slate_snapshots
from app.services.watchlist_coverage import (
    current_watchlist_markets,
    warm_current_watchlist_prop_context,
)

from app.services.ingestion.merge import _merge_numeric_detail_maps
from app.services.ingestion.summary import _build_watchlist_run_details

logger = logging.getLogger(__name__)

__all__ = [
    "run_refresh_cycle",
    "run_prop_refresh_cycle",
    "run_shadow_capture_cycle",
]


def run_refresh_cycle(
    db: Session,
    provider: TheSportsDBClient | None = None,
    major_provider: EspnPublicClient | None = None,
    niche_provider: TheSportsDBClient | None = None,
    public_client: KalshiPublicClient | None = None,
    sports: Iterable[str] | None = None,
    current_slate_only: bool = False,
    job: RefreshJob | None = None,
) -> Run:
    # R2 phase 2: ``refresh_sports_data`` and ``refresh_kalshi_markets``
    # still live in ``ingestion/__init__.py``. Lazy import here breaks
    # the otherwise-circular package graph.
    from app.services.ingestion import refresh_sports_data, refresh_kalshi_markets

    initial_sports = list(sports or (["NBA", "MLB", "WNBA"] if current_slate_only else get_settings().enabled_sports))
    run = Run(kind="refresh", status="running", details={"sports": initial_sports})
    db.add(run)
    db.flush()
    if job is not None:
        # Bug #10 P2: link the Run to the job AND commit immediately,
        # so the FK is durable BEFORE any stage work begins. Two
        # requirements that this commit satisfies together:
        #
        # 1. The worker-timeout watchdog snapshots ``job.run_id`` from
        #    a fresh session; the commit makes the link visible across
        #    sessions, so the watchdog can find and fail the Run.
        # 2. The commit releases the row-level write lock on
        #    ``refresh_jobs`` that the ``job.run_id = run.id`` UPDATE
        #    acquires. If we merely dirtied the attribute without
        #    committing, any inner ``db.flush()`` (e.g. inside
        #    ``seed_sports`` called by ``refresh_sports_data``) would
        #    flush the UPDATE and hold the lock until the first stage
        #    commit. A pre-commit hang would then block the watchdog's
        #    ``_guarded_fail_job`` UPDATE on the same row (codex
        #    round-2 P1 on PR #37).
        #
        # The caller has only just loaded ``job`` and added ``run``, so
        # this is a clean, narrow commit.
        job.run_id = run.id
        db.commit()
    try:
        with httpx.Client(follow_redirects=True, timeout=20) as shared_http_client:
            kalshi_client = public_client or KalshiPublicClient(http_client=shared_http_client)
            espn_client = major_provider or EspnPublicClient(http_client=shared_http_client)
            settings = get_settings()
            active_sports = list(sports or (["NBA", "MLB", "WNBA"] if current_slate_only else settings.enabled_sports))
            stage_details: dict[str, object] = {}

            stage_started = perf_counter()
            sports_summary = refresh_sports_data(
                db,
                provider=provider,
                major_provider=espn_client,
                niche_provider=niche_provider,
                sports=active_sports,
                lookback_days=settings.current_slate_lookback_days if current_slate_only else None,
                lookahead_days=settings.current_slate_lookahead_days if current_slate_only else None,
            )
            stage_details["sports_ingest_seconds"] = round(perf_counter() - stage_started, 3)
            db.commit()

            stage_started = perf_counter()
            kalshi_summary = refresh_kalshi_markets(
                db,
                client=kalshi_client,
                include_standalone=True,
                refresh_combo_prop_tickers=not current_slate_only,
                discover_combo_props=False,
            )
            stage_details["kalshi_ingest_seconds"] = round(perf_counter() - stage_started, 3)

            stage_started = perf_counter()
            touched_market_ids = set(kalshi_summary.get("touched_market_ids") or set())
            mapped_count = map_markets_to_events(db, candidate_market_ids=touched_market_ids if touched_market_ids else None)
            stage_details["market_mapping_seconds"] = round(perf_counter() - stage_started, 3)
            db.commit()

            current_markets = current_watchlist_markets(db) if current_slate_only else None
            current_watchlist_resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=True)
            stage_started = perf_counter()
            current_watchlist_summary = warm_current_watchlist_prop_context(
                db,
                resolver=current_watchlist_resolver,
                markets=current_markets,
            )
            stage_details["prop_warming_seconds"] = round(perf_counter() - stage_started, 3)
            target_market_ids = {market.id for market in current_markets or []} if current_slate_only else None
            resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=False)
            stage_started = perf_counter()
            watchlist_summary = regenerate_watchlist(
                db,
                run_id=run.id,
                resolver=resolver,
                allowed_market_ids=target_market_ids,
                replace_all=not current_slate_only,
                capture_parlays=not current_slate_only,
                candidate_markets=current_markets,
            )
            stage_details["watchlist_regeneration_seconds"] = round(perf_counter() - stage_started, 3)
            db.commit()
            if current_slate_only:
                stage_started = perf_counter()
                snapshots = persist_current_slate_snapshots(
                    db,
                    source_run_id=run.id,
                )
                stage_details["trade_snapshot_persist_seconds"] = round(perf_counter() - stage_started, 3)
                if snapshots.get("all") is not None:
                    stage_details["snapshot_generated_at"] = snapshots["all"].isoformat()
                db.commit()
                shadow_prediction_count, shadow_parlay_prediction_count = 0, 0
            else:
                shadow_prediction_count, shadow_parlay_prediction_count = capture_shadow_artifacts(
                    db,
                    run_id=run.id,
                )
            single_settlement_summary = {
                "processed": 0,
                "updated": 0,
                "won": 0,
                "lost": 0,
                "push": 0,
                "cancelled": 0,
                "pending": 0,
                "unresolved": 0,
                "errors": 0,
            }
            parlay_settlement_summary = {
                "updated": 0,
                "won": 0,
                "lost": 0,
                "push": 0,
                "cancelled": 0,
                "pending": 0,
                "unresolved": 0,
                "errors": 0,
            }
            run.details, records = _build_watchlist_run_details(
                db,
                sports=active_sports,
                sports_summary=sports_summary,
                kalshi_summary=kalshi_summary,
                mapped_count=mapped_count,
                watchlist_summary=watchlist_summary,
                shadow_prediction_count=shadow_prediction_count,
                shadow_parlay_prediction_count=shadow_parlay_prediction_count,
                single_settlement_summary=single_settlement_summary,
                parlay_settlement_summary=parlay_settlement_summary,
                extra_details={
                    **_merge_numeric_detail_maps(current_watchlist_summary, resolver.stats.as_dict()),
                    "refresh_scope": "current_slate" if current_slate_only else "full",
                    **stage_details,
                },
            )
        run.status = "completed"
        run.records_processed = records
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        return run
    except Exception as exc:
        run.status = "failed"
        run.error_message = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        raise


def run_prop_refresh_cycle(
    db: Session,
    major_provider: EspnPublicClient | None = None,
    public_client: KalshiPublicClient | None = None,
    sports: Iterable[str] | None = None,
) -> Run:
    from app.services.ingestion import refresh_kalshi_markets

    run = Run(kind="prop_refresh", status="running", details={"sports": list(sports or get_settings().enabled_sports)})
    db.add(run)
    db.flush()
    try:
        with httpx.Client(follow_redirects=True, timeout=20) as shared_http_client:
            kalshi_client = public_client or KalshiPublicClient(http_client=shared_http_client)
            espn_client = major_provider or EspnPublicClient(http_client=shared_http_client)
            stage_details: dict[str, float] = {}

            stage_started = perf_counter()
            kalshi_summary = refresh_kalshi_markets(
                db,
                client=kalshi_client,
                include_standalone=False,
                refresh_combo_prop_tickers=False,
                discover_combo_props=True,
            )
            stage_details["kalshi_ingest_seconds"] = round(perf_counter() - stage_started, 3)

            stage_started = perf_counter()
            touched_market_ids = set(kalshi_summary.get("touched_market_ids") or set())
            mapped_count = map_markets_to_events(db, candidate_market_ids=touched_market_ids if touched_market_ids else None)
            stage_details["market_mapping_seconds"] = round(perf_counter() - stage_started, 3)
            db.commit()
            resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=True)
            stage_started = perf_counter()
            warm_summary = warm_prop_context_cache(db, resolver=resolver)
            stage_details["prop_warming_seconds"] = round(perf_counter() - stage_started, 3)
            stage_started = perf_counter()
            watchlist_summary = regenerate_watchlist(
                db,
                run_id=run.id,
                resolver=resolver,
            )
            stage_details["watchlist_regeneration_seconds"] = round(perf_counter() - stage_started, 3)
            db.commit()
            stage_started = perf_counter()
            single_settlement_summary = settle_predictions(
                db,
                client=kalshi_client,
                open_market_tickers=set(kalshi_summary.get("open_market_tickers") or set()),
            )
            parlay_settlement_summary = settle_parlay_predictions(db)
            stage_details["maintenance_settlement_seconds"] = round(perf_counter() - stage_started, 3)
            run.details, records = _build_watchlist_run_details(
                db,
                sports=sports,
                sports_summary=None,
                kalshi_summary=kalshi_summary,
                mapped_count=mapped_count,
                watchlist_summary=watchlist_summary,
                single_settlement_summary=single_settlement_summary,
                parlay_settlement_summary=parlay_settlement_summary,
                extra_details={**warm_summary, **stage_details, "refresh_scope": "maintenance"},
            )
        run.status = "completed"
        run.records_processed = records
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        return run
    except Exception as exc:
        run.status = "failed"
        run.error_message = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        raise


def run_shadow_capture_cycle(
    db: Session,
    *,
    scope: str,
    source_run_id: int | None = None,
) -> Run:
    if scope == "current_slate" and (source_run_id or 0) <= 0:
        raise ValueError("A source refresh run is required for shadow capture.")
    if scope not in {"current_slate", "backfill"}:
        raise ValueError(f"Unsupported shadow capture scope: {scope}")

    run_details = {"shadow_capture_scope": scope}
    if source_run_id is not None:
        run_details["source_run_id"] = source_run_id
    run = Run(kind="shadow_capture", status="running", details=run_details)
    db.add(run)
    db.flush()
    try:
        sync_family_runtime_health(db)
        shadow_prediction_count, shadow_parlay_prediction_count = capture_shadow_artifacts(
            db,
            run_id=run.id,
            source_run_id=source_run_id,
            backfill=(scope == "backfill"),
        )
        run.details = {
            "shadow_capture_scope": scope,
            "source_run_id": source_run_id,
            "shadow_predictions_captured": shadow_prediction_count,
            "shadow_parlay_predictions_captured": shadow_parlay_prediction_count,
            "refresh_scope": "shadow_capture",
        }
        run.status = "completed"
        run.records_processed = shadow_prediction_count + shadow_parlay_prediction_count
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        return run
    except Exception as exc:
        run.status = "failed"
        run.error_message = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        raise
