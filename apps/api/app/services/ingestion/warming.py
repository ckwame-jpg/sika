"""Warming + batch-selection helpers for the staged refresh pipeline.

Extracted from ``ingestion/__init__.py`` as part of R2 phase 3 to
break up the 1,600-line file. These are the leaf-level helpers the
``advance_*_refresh_job`` functions call between phases — they
select candidate markets, warm the prop-context cache in batches,
fan out combo-prop discovery, and report progress for operator UI.

Module contents:
- Run-ensuring helpers: ``_ensure_prop_refresh_run`` /
  ``_ensure_current_slate_run``.
- Current-slate selection: ``_current_slate_blocking_reason``,
  ``_normalize_subject_key``, ``_current_slate_candidate_market_ids``.
- Warming: ``_warm_current_slate_context_batch`` /
  ``_warm_prop_context_batch`` /
  ``_refresh_combo_prop_discovery_batch``.
- Batch readers: ``_open_market_batch``.
- Progress reporters: ``_prop_refresh_processed_so_far`` /
  ``_current_slate_processed_so_far``.

``_persist_market_payload_records`` still lives in
``ingestion/__init__.py``; the combo-prop discovery helper reaches
it via a lazy import to break the otherwise-circular package init
graph.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.kalshi import KalshiPublicClient
from app.config import get_settings
from app.models import Market, RefreshJob, Run
from app.services.market_mapping import map_markets_to_events
from app.services.market_support import (
    classify_market_payload,
    combo_leg_metadata_prefilter,
)
from app.services.predictions import OPEN_MARKET_STATUSES
from app.services.scoring import (
    PropStatsResolver,
    WatchlistGenerationSummary,
)
from app.services.watchlist_coverage import (
    CURRENT_WATCHLIST_SPORTS,
    current_watchlist_event_ids,
    load_current_watchlist_markets,
    recommendation_market_ids_for_sports,
)

__all__ = [
    "PROP_REFRESH_COMBO_PAGE_SIZE",
    "PROP_REFRESH_COMBO_LEG_BATCH_SIZE",
    "PROP_CONTEXT_BATCH_SIZE",
    "_current_slate_blocking_reason",
    "_ensure_prop_refresh_run",
    "_ensure_current_slate_run",
    "_normalize_subject_key",
    "_current_slate_candidate_market_ids",
    "_warm_current_slate_context_batch",
    "_refresh_combo_prop_discovery_batch",
    "_open_market_batch",
    "_warm_prop_context_batch",
    "_prop_refresh_processed_so_far",
    "_current_slate_processed_so_far",
]


# Module-level batch-size constants. Kept here so the advance
# functions (extracted in phase 3) can import them from a single
# location alongside the helpers they drive.
PROP_REFRESH_COMBO_PAGE_SIZE = 50
PROP_REFRESH_COMBO_LEG_BATCH_SIZE = 100
PROP_CONTEXT_BATCH_SIZE = 25


def _current_slate_blocking_reason(
    *,
    event_count: int,
    candidate_market_count: int,
    summary: WatchlistGenerationSummary,
) -> str | None:
    if event_count <= 0:
        return None
    if candidate_market_count <= 0:
        return "Current NBA/MLB/WNBA events exist, but no current Kalshi markets are mapped to them."
    if summary.loaded_candidate_market_count <= 0 and summary.filtered_candidate_market_count > 0:
        return "Current slate candidate markets were filtered before scoring; no current open supported markets reached the scorer."
    if summary.scored_market_count <= 0:
        return "Current slate markets exist, but none were scored successfully."
    if summary.recommendation_count <= 0 and summary.coverage_prediction_count > 0:
        return "Current slate scored successfully, but no markets cleared recommendation thresholds."
    if summary.recommendation_count <= 0:
        return "Current slate markets exist, but none were scored successfully."
    return None


def _ensure_prop_refresh_run(
    db: Session,
    *,
    job: RefreshJob,
    sports: Iterable[str] | None = None,
) -> Run:
    if job.run_id is not None:
        existing = db.get(Run, job.run_id)
        if existing is not None:
            return existing
    run = Run(
        kind="prop_refresh",
        status="running",
        details={
            "sports": list(sports or get_settings().enabled_sports),
            "refresh_scope": "maintenance",
        },
    )
    db.add(run)
    db.flush()
    job.run_id = run.id
    return run


def _ensure_current_slate_run(
    db: Session,
    *,
    job: RefreshJob,
    sports: Iterable[str] | None = None,
) -> Run:
    if job.run_id is not None:
        existing = db.get(Run, job.run_id)
        if existing is not None:
            return existing
    run = Run(
        kind="refresh",
        status="running",
        details={
            "sports": list(sports or ["NBA", "MLB", "WNBA"]),
            "refresh_scope": "current_slate",
        },
    )
    db.add(run)
    db.flush()
    job.run_id = run.id
    return run


def _normalize_subject_key(raw_data: dict[str, Any]) -> tuple[str, str] | None:
    subject_name = str(raw_data.get("copilot_subject_name") or "").strip().lower()
    stat_key = str(raw_data.get("copilot_stat_key") or "").strip().lower()
    if not subject_name or not stat_key:
        return None
    return subject_name, stat_key


def _current_slate_candidate_market_ids(
    db: Session,
    *,
    touched_market_ids: set[int],
    touched_event_ids: set[int],
) -> tuple[list[int], int]:
    current_event_ids = set(current_watchlist_event_ids(db))
    tracked_sports = set(CURRENT_WATCHLIST_SPORTS)
    existing_recommendation_market_ids = set(
        recommendation_market_ids_for_sports(db, sports=tracked_sports)
    )
    candidate_market_ids: set[int] = set(existing_recommendation_market_ids)
    affected_event_ids: set[int] = set()

    touched_markets = db.scalars(
        select(Market).where(Market.id.in_(tuple(sorted(touched_market_ids))))
    ).all() if touched_market_ids else []
    touched_current_prop_keys: set[tuple[int, str, str]] = set()
    for market in touched_markets:
        if market.event_id is None:
            continue
        if (market.sport_key or "").upper() not in tracked_sports:
            continue
        affected_event_ids.add(market.event_id)
        candidate_market_ids.add(market.id)
        raw_data = market.raw_data or {}
        if str(raw_data.get("copilot_market_family") or "") != "player_prop":
            continue
        subject_key = _normalize_subject_key(raw_data)
        if subject_key is None:
            continue
        touched_current_prop_keys.add((market.event_id, *subject_key))

    affected_event_ids.update(event_id for event_id in touched_event_ids if event_id in current_event_ids)
    if affected_event_ids:
        event_markets = db.scalars(
            select(Market).where(Market.event_id.in_(tuple(sorted(affected_event_ids))))
        ).all()
        for market in event_markets:
            if (market.sport_key or "").upper() not in tracked_sports:
                continue
            candidate_market_ids.add(market.id)
            raw_data = market.raw_data or {}
            if str(raw_data.get("copilot_market_family") or "") != "player_prop":
                continue
            subject_key = _normalize_subject_key(raw_data)
            if subject_key is None:
                continue
            if (market.event_id or 0, *subject_key) in touched_current_prop_keys:
                candidate_market_ids.add(market.id)

    current_candidate_markets = load_current_watchlist_markets(
        db,
        market_ids=candidate_market_ids,
        event_ids=current_event_ids,
    )
    expanded_ids = {market.id for market in current_candidate_markets}
    for market in current_candidate_markets:
        raw_data = market.raw_data or {}
        if str(raw_data.get("copilot_market_family") or "") != "player_prop":
            continue
        subject_key = _normalize_subject_key(raw_data)
        if subject_key is None:
            continue
        if (market.event_id or 0, *subject_key) in touched_current_prop_keys:
            expanded_ids.add(market.id)

    candidate_market_ids.update(expanded_ids)
    return sorted(candidate_market_ids), len(affected_event_ids)


def _warm_current_slate_context_batch(
    db: Session,
    *,
    resolver: PropStatsResolver,
    market_ids: list[int],
    cursor_index: int = 0,
    batch_size: int = PROP_CONTEXT_BATCH_SIZE,
) -> tuple[dict[str, int], int | None, bool]:
    batch_ids = market_ids[cursor_index : cursor_index + batch_size]
    if not batch_ids:
        return resolver.stats.as_dict(), None, True
    markets = load_current_watchlist_markets(
        db,
        market_ids=set(batch_ids),
    )
    unique_subjects: dict[tuple[str, str, str], tuple[str, str, str | None]] = {}
    for market in markets:
        raw_data = market.raw_data or {}
        if raw_data.get("copilot_market_family") != "player_prop":
            continue
        sport_key = str(market.sport_key or "")
        subject_name = str(raw_data.get("copilot_subject_name") or "").strip()
        team_hint = str(raw_data.get("copilot_subject_team") or "").strip() or None
        if not sport_key or not subject_name:
            continue
        key = (sport_key, subject_name.lower(), (team_hint or "").upper())
        unique_subjects[key] = (sport_key, subject_name, team_hint)

    for sport_key, subject_name, team_hint in unique_subjects.values():
        try:
            resolver.resolve(sport_key, subject_name, team_hint=team_hint)
        except Exception:
            continue

    next_index = cursor_index + len(batch_ids)
    return resolver.stats.as_dict(), (None if next_index >= len(market_ids) else next_index), next_index >= len(market_ids)


def _refresh_combo_prop_discovery_batch(
    db: Session,
    *,
    client: KalshiPublicClient,
    cursor_payload: dict[str, object] | None,
    limit: int = PROP_REFRESH_COMBO_PAGE_SIZE,
    leg_batch_size: int = PROP_REFRESH_COMBO_LEG_BATCH_SIZE,
) -> tuple[dict[str, object], dict[str, object] | None, bool]:
    # ``_persist_market_payload_records`` still lives in
    # ``ingestion/__init__.py``. Lazy-import to break the cycle.
    from app.services.ingestion import _persist_market_payload_records

    summary = {
        "processed": 0,
        "total_kalshi_markets_seen": 0,
        "supported_nba_props_seen": 0,
        "supported_mlb_props_seen": 0,
        "unsupported_prop_category_counts": {},
        "combo_prop_legs_discovered": 0,
        "combo_prop_legs_refreshed": 0,
        "market_snapshots_written": 0,
        "mapped_markets": 0,
    }
    cursor_data = dict(cursor_payload or {})
    kalshi_cursor = str(cursor_data.get("kalshi_cursor") or "").strip() or None
    pending_combo_legs = [dict(item) for item in list(cursor_data.get("pending_combo_legs") or []) if isinstance(item, dict)]

    if not pending_combo_legs:
        combo_page, next_cursor = client.list_markets_page(
            status="open",
            limit=limit,
            mve_filter="include",
            cursor=kalshi_cursor,
        )
        if not combo_page:
            return summary, None, True
        summary["total_kalshi_markets_seen"] = len(combo_page)
        kalshi_cursor = next_cursor
        combo_leg_tickers_seen: set[str] = set()
        for combo_payload in combo_page:
            if not (combo_payload.get("mve_collection_ticker") or combo_payload.get("mve_selected_legs")):
                continue
            for leg in combo_payload.get("mve_selected_legs") or []:
                leg_prefilter = combo_leg_metadata_prefilter(leg)
                if not leg_prefilter.get("supported"):
                    continue
                leg_ticker = str(leg.get("market_ticker") or "").strip()
                if not leg_ticker or leg_ticker in combo_leg_tickers_seen:
                    continue
                combo_leg_tickers_seen.add(leg_ticker)
                pending_combo_legs.append(
                    {
                        "event_ticker": leg.get("event_ticker"),
                        "market_ticker": leg_ticker,
                        "source_market_ticker": combo_payload.get("ticker"),
                        "source_market_title": combo_payload.get("title"),
                    }
                )

    payload_records: list[dict[str, Any]] = []
    active_combo_legs = pending_combo_legs[:leg_batch_size]
    for combo_leg in active_combo_legs:
        leg_prefilter = combo_leg_metadata_prefilter(combo_leg)
        if not leg_prefilter.get("supported"):
            continue
        leg_ticker = str(combo_leg.get("market_ticker") or "").strip()
        if not leg_ticker:
            continue
        try:
            leg_payload = client.get_market(leg_ticker)
        except Exception:
            continue
        if not leg_payload:
            continue
        leg_classification = classify_market_payload(leg_payload)
        leg_metadata = leg_classification.get("metadata") or {}
        if (
            not leg_classification.get("supported")
            or leg_metadata.get("copilot_market_family") != "player_prop"
        ):
            continue
        payload_records.append(
            {
                "payload": {
                    **leg_payload,
                    "mve_collection_ticker": None,
                    "mve_selected_legs": None,
                },
                "source_type": "combo_derived",
                "source_payload": {
                    "ticker": combo_leg.get("source_market_ticker"),
                    "title": combo_leg.get("source_market_title"),
                },
                "classification_override": leg_classification,
            }
        )

    persisted = _persist_market_payload_records(db, payload_records)
    touched_market_ids = set(persisted.get("touched_market_ids") or set())
    mapped_count = map_markets_to_events(db, candidate_market_ids=touched_market_ids if touched_market_ids else None)
    summary.update(
        {
            "processed": int(persisted.get("processed") or 0),
            "supported_nba_props_seen": int(persisted.get("supported_nba_props_seen") or 0),
            "supported_mlb_props_seen": int(persisted.get("supported_mlb_props_seen") or 0),
            "unsupported_prop_category_counts": dict(persisted.get("unsupported_prop_category_counts") or {}),
            "combo_prop_legs_discovered": int(persisted.get("combo_prop_legs_discovered") or 0),
            "combo_prop_legs_refreshed": int(persisted.get("combo_prop_legs_refreshed") or 0),
            "market_snapshots_written": int(persisted.get("market_snapshots_written") or 0),
            "mapped_markets": mapped_count,
        }
    )

    remaining_combo_legs = pending_combo_legs[leg_batch_size:]
    if remaining_combo_legs:
        return summary, {"kalshi_cursor": kalshi_cursor, "pending_combo_legs": remaining_combo_legs}, False
    if kalshi_cursor is None:
        return summary, None, True
    return summary, {"kalshi_cursor": kalshi_cursor, "pending_combo_legs": []}, False


def _open_market_batch(
    db: Session,
    *,
    cursor_market_id: int | None,
    batch_size: int,
) -> list[Market]:
    stmt = (
        select(Market)
        .where(Market.status.in_(tuple(OPEN_MARKET_STATUSES)))
        .order_by(Market.id.asc())
    )
    if cursor_market_id is not None:
        stmt = stmt.where(Market.id > cursor_market_id)
    return db.scalars(stmt.limit(batch_size)).all()


def _warm_prop_context_batch(
    db: Session,
    *,
    resolver: PropStatsResolver,
    cursor_market_id: int | None,
    batch_size: int = PROP_CONTEXT_BATCH_SIZE,
) -> tuple[dict[str, int], int | None, bool]:
    markets = _open_market_batch(
        db,
        cursor_market_id=cursor_market_id,
        batch_size=batch_size,
    )
    if not markets:
        return resolver.stats.as_dict(), None, True

    unique_subjects: dict[tuple[str, str, str], tuple[str, str, str | None]] = {}
    for market in markets:
        raw_data = market.raw_data or {}
        if raw_data.get("copilot_market_family") != "player_prop":
            continue
        sport_key = str(market.sport_key or "")
        subject_name = str(raw_data.get("copilot_subject_name") or "").strip()
        team_hint = str(raw_data.get("copilot_subject_team") or "").strip() or None
        if not sport_key or not subject_name:
            continue
        key = (sport_key, subject_name.lower(), (team_hint or "").upper())
        unique_subjects[key] = (sport_key, subject_name, team_hint)

    for sport_key, subject_name, team_hint in unique_subjects.values():
        try:
            resolver.resolve(sport_key, subject_name, team_hint=team_hint)
        except Exception:
            continue
    return resolver.stats.as_dict(), markets[-1].id if markets else cursor_market_id, len(markets) < batch_size


def _prop_refresh_processed_so_far(details: dict[str, object]) -> int:
    return (
        int((details.get("kalshi_summary") or {}).get("processed") or 0)
        + int((details.get("watchlist_summary") or {}).get("prediction_count") or 0)
        + int((details.get("single_settlement_summary") or {}).get("updated") or 0)
        + int((details.get("parlay_settlement_summary") or {}).get("updated") or 0)
    )


def _current_slate_processed_so_far(details: dict[str, object]) -> int:
    return (
        int((details.get("sports_summary") or {}).get("processed") or 0)
        + int((details.get("kalshi_summary") or {}).get("processed") or 0)
        + int(details.get("mapped_count") or 0)
        + int((details.get("watchlist_summary") or {}).get("recommendation_count") or 0)
        + int((details.get("watchlist_summary") or {}).get("prediction_count") or 0)
    )
