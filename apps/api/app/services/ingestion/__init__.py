import logging
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)

from app.clients.espn import EspnPublicClient
from app.clients.kalshi import KalshiPublicClient, snapshot_from_market_payload
from app.clients.sports_data import TheSportsDBClient
from app.config import get_settings
from app.models import Event, EventParticipant, League, Market, MarketSnapshot, ParlayRecommendation, Participant, Recommendation, RefreshJob, Run, Sport
from app.services.market_mapping import map_markets_to_events
from app.services.market_support import (
    classify_market_payload,
    combo_leg_metadata_prefilter,
    infer_market_sport_key,
    infer_supported_market_kind,
)
from app.services.ml import capture_shadow_artifacts, sync_family_runtime_health
from app.services.parlays import settle_parlay_predictions, settle_parlay_predictions_batch
from app.services.predictions import OPEN_MARKET_STATUSES, settle_predictions, settle_predictions_batch
from app.services.scoring import (
    PropStatsResolver,
    WatchlistGenerationSummary,
    finalize_current_slate_watchlist,
    finalize_staged_watchlist,
    regenerate_watchlist,
    stage_current_slate_watchlist_batch,
    stage_maintenance_watchlist_batch,
    warm_prop_context_cache,
)
from app.services.trade_desk import persist_current_slate_snapshots
from app.services.watchlist_coverage import (
    CURRENT_WATCHLIST_SPORTS,
    current_watchlist_event_ids,
    current_watchlist_markets,
    latest_snapshot_by_market_id,
    load_current_watchlist_markets,
    recommendation_market_ids_for_sports,
    warm_current_watchlist_prop_context,
)
from app.sports.base import NormalizedEvent
from app.sports.registry import ADAPTERS


SPORT_LABELS = {
    "NBA": "NBA",
    "NFL": "NFL",
    "MLB": "MLB",
    "SOCCER": "Soccer",
    "TENNIS": "TENNIS",
}

PUBLIC_MAJOR_SPORTS = {"NBA", "NFL", "MLB"}
WATCHLIST_SCORE_BATCH_SIZE = 25
PREDICTION_SETTLEMENT_BATCH_SIZE = 100
PARLAY_SETTLEMENT_BATCH_SIZE = 50

# Batch-size constants + warming helpers live in ``warming.py``
# (R2 phase 3). Re-exported here for backward compat with tests +
# scheduler imports.
from app.services.ingestion.warming import (  # noqa: E402
    PROP_CONTEXT_BATCH_SIZE,
    PROP_REFRESH_COMBO_LEG_BATCH_SIZE,
    PROP_REFRESH_COMBO_PAGE_SIZE,
    _current_slate_blocking_reason,
    _current_slate_candidate_market_ids,
    _current_slate_processed_so_far,
    _ensure_current_slate_run,
    _ensure_prop_refresh_run,
    _normalize_subject_key,
    _open_market_batch,
    _prop_refresh_processed_so_far,
    _refresh_combo_prop_discovery_batch,
    _warm_current_slate_context_batch,
    _warm_prop_context_batch,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _snapshot_materially_changed(
    previous: MarketSnapshot | None,
    current: dict[str, float | None],
    *,
    payload: dict[str, Any],
) -> bool:
    if previous is None:
        return True
    for field in ("yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "volume", "open_interest"):
        if getattr(previous, field) != current.get(field):
            return True
    previous_status = str((previous.raw_data or {}).get("status") or "")
    return previous_status != str(payload.get("status") or "")


# Map-merge helpers live in ``ingestion.merge`` so the summary
# helpers can import them without creating a cycle through this
# module. Re-exported for backward compat with any external
# consumer that imports ``_merge_*`` from ``app.services.ingestion``.
from app.services.ingestion.merge import (  # noqa: E402
    _merge_count_maps,
    _merge_numeric_detail_maps,
)


def _persist_market_payload_records(
    db: Session,
    payload_records: list[dict[str, Any]],
) -> dict[str, object]:
    settings = get_settings()
    processed = 0
    supported_nba_props = 0
    supported_mlb_props = 0
    combo_prop_legs_discovered = 0
    combo_prop_legs_refreshed = 0
    market_snapshots_written = 0
    unsupported_prop_categories: dict[str, int] = {}
    open_market_tickers: set[str] = set()
    touched_market_ids: set[int] = set()
    heartbeat = timedelta(minutes=settings.market_snapshot_heartbeat_minutes)

    existing_markets: dict[str, Market] = {}
    tickers = sorted({str(record["payload"].get("ticker") or "") for record in payload_records if record["payload"].get("ticker")})
    if tickers:
        existing_markets = {
            market.ticker: market
            for market in db.scalars(select(Market).where(Market.ticker.in_(tuple(tickers)))).all()
        }
    latest_snapshots = latest_snapshot_by_market_id(db, [market.id for market in existing_markets.values()])

    def persist_market_payload(
        payload: dict[str, Any],
        *,
        source_type: str = "standalone",
        source_payload: dict[str, Any] | None = None,
        classification_override: dict[str, Any] | None = None,
    ) -> bool:
        nonlocal processed, supported_nba_props, supported_mlb_props, combo_prop_legs_discovered, combo_prop_legs_refreshed, market_snapshots_written
        classification = classification_override or classify_market_payload(payload)
        metadata = classification.get("metadata")
        if not classification.get("supported") or not metadata:
            if classification.get("reason") == "unsupported_prop_category" and classification.get("sport_key") in {"NBA", "MLB"}:
                prop_category = str(classification.get("prop_category") or "unknown")
                unsupported_prop_categories[prop_category] = unsupported_prop_categories.get(prop_category, 0) + 1
            return False
        market_kind = str(metadata.get("copilot_market_kind") or "")
        market_sport_key = str(classification.get("sport_key") or infer_market_sport_key(payload) or "")
        ticker = payload.get("ticker")
        if not ticker:
            return False
        if metadata.get("copilot_market_family") == "player_prop":
            if market_sport_key == "NBA":
                supported_nba_props += 1
            if market_sport_key == "MLB":
                supported_mlb_props += 1
        market = existing_markets.get(ticker)
        had_existing_market = market is not None
        existing_raw = dict(market.raw_data or {}) if market else {}
        existing_source_type = str(existing_raw.get("copilot_source_type") or "standalone")
        if not market:
            market = Market(ticker=ticker, title=payload.get("title") or ticker)
            db.add(market)
            db.flush()
            existing_markets[ticker] = market
        market.series_ticker = payload.get("series_ticker")
        market.event_ticker = payload.get("event_ticker")
        market.sport_key = market_sport_key
        market.title = payload.get("title") or market.title
        market.subtitle = payload.get("subtitle")
        market.status = payload.get("status") or "open"
        market.close_time = datetime.fromisoformat(payload["close_time"].replace("Z", "+00:00")) if payload.get("close_time") else None
        raw_data = {
            **payload,
            **metadata,
            "copilot_market_kind": market_kind,
            "copilot_display_market_title": payload.get("title") or market.title,
        }
        preserve_standalone_metadata = had_existing_market and existing_source_type == "standalone" and source_type == "combo_derived"
        if source_type == "combo_derived" and not preserve_standalone_metadata:
            source_market_ticker = source_payload.get("ticker") if source_payload else existing_raw.get("copilot_source_market_ticker")
            source_market_title = source_payload.get("title") if source_payload else existing_raw.get("copilot_source_market_title")
            raw_data.update(
                {
                    "copilot_source_type": "combo_derived",
                    "copilot_source_market_ticker": source_market_ticker,
                    "copilot_source_market_title": source_market_title,
                    "copilot_source_badge_label": "Combo-derived",
                }
            )
        elif source_type != "combo_derived":
            raw_data["copilot_source_type"] = "standalone"
        else:
            raw_data["copilot_source_type"] = existing_source_type
        market.raw_data = raw_data
        touched_market_ids.add(market.id)
        if market.status in OPEN_MARKET_STATUSES:
            open_market_tickers.add(ticker)

        snapshot = snapshot_from_market_payload(payload)
        latest_snapshot = latest_snapshots.get(market.id)
        latest_snapshot_at = _as_utc(latest_snapshot.captured_at) if latest_snapshot else None
        should_write_snapshot = _snapshot_materially_changed(latest_snapshot, snapshot, payload=payload)
        if not should_write_snapshot and latest_snapshot_at is not None:
            should_write_snapshot = datetime.now(timezone.utc) - latest_snapshot_at >= heartbeat
        if should_write_snapshot:
            snapshot_row = MarketSnapshot(
                market_id=market.id,
                captured_at=datetime.now(timezone.utc),
                raw_data=payload,
                **snapshot,
            )
            db.add(snapshot_row)
            latest_snapshots[market.id] = snapshot_row
            market_snapshots_written += 1
        processed += 1
        if source_type == "combo_derived" and source_payload is None:
            combo_prop_legs_refreshed += 1
        elif source_type == "combo_derived":
            combo_prop_legs_discovered += 1
        return True

    for record in payload_records:
        persist_market_payload(
            record["payload"],
            source_type=str(record["source_type"]),
            source_payload=record["source_payload"],
            classification_override=record["classification_override"],
        )

    db.flush()
    return {
        "processed": processed,
        "supported_nba_props_seen": supported_nba_props,
        "supported_mlb_props_seen": supported_mlb_props,
        "unsupported_prop_category_counts": unsupported_prop_categories,
        "combo_prop_legs_discovered": combo_prop_legs_discovered,
        "combo_prop_legs_refreshed": combo_prop_legs_refreshed,
        "market_snapshots_written": market_snapshots_written,
        "touched_market_ids": touched_market_ids,
        "open_market_tickers": open_market_tickers,
    }


# Watchlist composition counts + run-details builders live in
# ``ingestion.summary`` so the file the orchestration code sits in
# doesn't have to carry the (200-line) detail-payload structure.
# Re-exported here so external consumers / tests that import these
# from ``app.services.ingestion`` keep working.
from app.services.ingestion.summary import (  # noqa: E402
    _build_watchlist_run_details,
    _merge_settlement_summaries,
    _merge_watchlist_summaries,
    _parlay_watchlist_counts,
    _prop_market_summary_counts,
    _watchlist_summary_from_payload,
    _watchlist_summary_to_payload,
)


def is_supported_market_payload(payload: dict) -> bool:
    return infer_supported_market_kind(payload) is not None


def seed_sports(db: Session) -> None:
    enabled_sports = {sport.upper() for sport in get_settings().enabled_sports}
    for key, name in SPORT_LABELS.items():
        if key not in enabled_sports:
            continue
        existing = db.scalar(select(Sport).where(Sport.key == key))
        if not existing:
            db.add(Sport(key=key, name=name))
    db.flush()


def _get_or_create_league(db: Session, normalized: NormalizedEvent) -> League | None:
    if not normalized.league_name:
        return None
    league = None
    if normalized.league_external_id:
        league = db.scalar(
            select(League).where(League.sport_key == normalized.sport_key, League.external_id == normalized.league_external_id)
        )
    if not league:
        league = db.scalar(select(League).where(League.sport_key == normalized.sport_key, League.name == normalized.league_name))
    if not league:
        league = League(
            external_id=normalized.league_external_id or None,
            sport_key=normalized.sport_key,
            name=normalized.league_name,
            raw_data=normalized.raw_data,
        )
        db.add(league)
        db.flush()
    return league


def _upsert_event(db: Session, normalized: NormalizedEvent) -> Event:
    event = db.scalar(select(Event).where(Event.sport_key == normalized.sport_key, Event.external_id == normalized.external_id))
    league = _get_or_create_league(db, normalized)
    if not event:
        event = Event(external_id=normalized.external_id, sport_key=normalized.sport_key, name=normalized.name, starts_at=normalized.starts_at)
        db.add(event)
        db.flush()

    event.sport_key = normalized.sport_key
    event.league_id = league.id if league else None
    event.name = normalized.name
    event.status = normalized.status
    event.starts_at = normalized.starts_at
    event.completed_at = normalized.completed_at
    event.raw_data = normalized.raw_data

    existing_links = {link.participant.external_id: link for link in event.participants if link.participant}
    links_for_event = list(event.participants)
    for normalized_participant in normalized.participants:
        participant = db.scalar(
            select(Participant).where(
                Participant.sport_key == normalized.sport_key,
                Participant.external_id == normalized_participant.external_id,
            )
        )
        if not participant:
            participant = Participant(
                external_id=normalized_participant.external_id,
                sport_key=normalized.sport_key,
                display_name=normalized_participant.display_name,
                short_name=normalized_participant.short_name,
                participant_type="team" if normalized.sport_key in {"NBA", "NFL", "MLB", "SOCCER"} else "competitor",
                raw_data=normalized_participant.raw_data,
            )
            db.add(participant)
            db.flush()
        else:
            participant.display_name = normalized_participant.display_name
            participant.short_name = normalized_participant.short_name
            participant.raw_data = normalized_participant.raw_data

        link = existing_links.get(normalized_participant.external_id)
        if not link:
            link = EventParticipant(event_id=event.id, participant_id=participant.id, role=normalized_participant.role, is_home=normalized_participant.is_home)
            db.add(link)
            links_for_event.append(link)
        link.role = normalized_participant.role
        link.is_home = normalized_participant.is_home
        link.score = None
        link.result = None

    home_score = normalized.raw_data.get("intHomeScore")
    away_score = normalized.raw_data.get("intAwayScore")
    score_by_role: dict[str, float] = {}
    if home_score is not None:
        home_score_value = float(home_score)
        score_by_role["home"] = home_score_value
        score_by_role["competitor_1"] = home_score_value
    if away_score is not None:
        away_score_value = float(away_score)
        score_by_role["away"] = away_score_value
        score_by_role["competitor_2"] = away_score_value

    if score_by_role:
        for link in links_for_event:
            if link.role in score_by_role:
                link.score = score_by_role[link.role]

    if normalized.status == "completed" and home_score is not None and away_score is not None:
        home_wins = float(home_score) > float(away_score)
        for link in links_for_event:
            link.result = "win" if (link.role in {"home", "competitor_1"} and home_wins) or (link.role in {"away", "competitor_2"} and not home_wins) else "loss"

    db.flush()
    return event


def _fetch_events_window_with_diagnostics(
    client: object,
    sport_name: str,
    start_day: date,
    end_day: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    diagnostic_fetcher = getattr(client, "fetch_events_window_with_diagnostics", None)
    if callable(diagnostic_fetcher):
        return diagnostic_fetcher(sport_name, start_day, end_day)

    try:
        return client.fetch_events_window(sport_name, start_day, end_day), []
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        return [], [f"{start_day.isoformat()}..{end_day.isoformat()}: {exc.__class__.__name__}: {message}"]


def refresh_sports_data(
    db: Session,
    provider: TheSportsDBClient | None = None,
    major_provider: EspnPublicClient | None = None,
    niche_provider: TheSportsDBClient | None = None,
    sports: Iterable[str] | None = None,
    anchor_day: date | None = None,
    lookback_days: int | None = None,
    lookahead_days: int | None = None,
) -> dict[str, object]:
    settings = get_settings()
    seed_sports(db)
    major_provider = major_provider or EspnPublicClient()
    niche_provider = niche_provider or TheSportsDBClient()
    sports = list(sports or settings.enabled_sports)
    anchor_day = anchor_day or datetime.now(timezone.utc).date()
    effective_lookback = settings.lookback_days if lookback_days is None else lookback_days
    effective_lookahead = settings.lookahead_days if lookahead_days is None else lookahead_days
    major_start_day = anchor_day - timedelta(days=effective_lookback)
    major_end_day = anchor_day + timedelta(days=effective_lookahead)
    free_start_day = anchor_day - timedelta(days=settings.free_provider_lookback_days)
    free_end_day = anchor_day + timedelta(days=settings.free_provider_lookahead_days)

    processed = 0
    processed_by_sport: dict[str, int] = {sport_key: 0 for sport_key in sports}
    fetch_errors: dict[str, list[str]] = {}
    touched_event_ids: set[int] = set()
    # Smarter #23 phase 2 — track ESPN scoreboard reach across the
    # sports loop so the upstream-health board records one aggregate
    # entry per refresh tick instead of one-per-sport.
    espn_scoreboard_invoked = False
    espn_scoreboard_errors: list[str] = []
    for sport_key in sports:
        adapter = ADAPTERS[sport_key]
        if provider is not None:
            raw_events, sport_errors = _fetch_events_window_with_diagnostics(
                provider,
                adapter.provider_name,
                major_start_day,
                major_end_day,
            )
        elif sport_key in PUBLIC_MAJOR_SPORTS:
            espn_scoreboard_invoked = True
            raw_events, sport_errors = _fetch_events_window_with_diagnostics(
                major_provider,
                sport_key,
                major_start_day,
                major_end_day,
            )
            if sport_errors:
                espn_scoreboard_errors.extend(sport_errors)
        else:
            raw_events, sport_errors = _fetch_events_window_with_diagnostics(
                niche_provider,
                adapter.provider_name,
                free_start_day,
                free_end_day,
            )
        if sport_errors:
            fetch_errors[sport_key] = sport_errors
        for raw_event in raw_events:
            normalized = adapter.normalize_event(raw_event)
            if not normalized:
                continue
            event = _upsert_event(db, normalized)
            touched_event_ids.add(event.id)
            processed += 1
            processed_by_sport[sport_key] = processed_by_sport.get(sport_key, 0) + 1
    if espn_scoreboard_invoked:
        # Smarter #23 phase 2 — emit aggregate ESPN scoreboard health.
        # Partial failures (some sport_keys failed) record as FAILURE
        # because operators want to know about any error, not just
        # all-or-nothing outages. Truncate the message to keep the
        # operator surface readable.
        from app.services.upstream_health import (  # noqa: PLC0415 — avoid circular import
            record_upstream_failure,
            record_upstream_success,
        )
        if espn_scoreboard_errors:
            record_upstream_failure(
                db,
                "espn_scoreboard",
                "; ".join(espn_scoreboard_errors[:3]),
            )
        else:
            record_upstream_success(db, "espn_scoreboard")
    db.flush()
    return {
        "processed": processed,
        "sports_records_ingested": processed_by_sport,
        "sports_fetch_errors": fetch_errors,
        "touched_event_ids": touched_event_ids,
    }


def refresh_kalshi_markets(
    db: Session,
    client: KalshiPublicClient | None = None,
    *,
    include_standalone: bool = True,
    refresh_combo_prop_tickers: bool = True,
    discover_combo_props: bool = True,
) -> dict[str, object]:
    # Smarter #23 phase 2 — record kalshi_markets reachability on the
    # upstream-health board. Wrap the function body so any unhandled
    # exception is recorded as a failure (with the operator-visible
    # error message) AND re-raised to the caller so the refresh-job
    # row still surfaces the failure in its own details.
    try:
        result = _refresh_kalshi_markets_inner(
            db,
            client=client,
            include_standalone=include_standalone,
            refresh_combo_prop_tickers=refresh_combo_prop_tickers,
            discover_combo_props=discover_combo_props,
        )
    except Exception as exc:
        from app.services.upstream_health import record_upstream_failure  # noqa: PLC0415
        record_upstream_failure(db, "kalshi_markets", str(exc) or exc.__class__.__name__)
        raise
    else:
        from app.services.upstream_health import record_upstream_success  # noqa: PLC0415
        record_upstream_success(db, "kalshi_markets")
        return result


def _refresh_kalshi_markets_inner(
    db: Session,
    client: KalshiPublicClient | None = None,
    *,
    include_standalone: bool = True,
    refresh_combo_prop_tickers: bool = True,
    discover_combo_props: bool = True,
) -> dict[str, object]:
    client = client or KalshiPublicClient()
    processed = 0
    total_seen = 0
    supported_nba_props = 0
    supported_mlb_props = 0
    combo_prop_legs_discovered = 0
    combo_prop_legs_refreshed = 0
    market_snapshots_written = 0
    unsupported_prop_categories: dict[str, int] = {}
    payload_records: list[dict[str, Any]] = []

    if include_standalone:
        # Bug #18: 5K default cap missed game-winner tickers buried past
        # 5K in Kalshi's default ordering. Pass a 120 s wall-clock
        # budget — well inside the 300 s market-discovery worker
        # timeout — and let pagination drain the cursor instead.
        for payload in client.list_markets(
            status="open",
            limit=1000,
            mve_filter="exclude",
            wall_clock_budget_seconds=120.0,
        ):
            total_seen += 1
            payload_records.append(
                {
                    "payload": payload,
                    "source_type": "standalone",
                    "source_payload": None,
                    "classification_override": None,
                }
            )

    if refresh_combo_prop_tickers:
        # Bug #23: previously loaded every open market into Python and
        # filtered by ``raw_data["copilot_market_family"]`` /
        # ``raw_data["copilot_source_type"]`` on each row — O(all open
        # markets) per refresh tick. Push the JSON filter into SQL via
        # the dialect-portable ``.as_string()`` accessor (same shape
        # ``routes.py:1263`` already uses for the markets list filter).
        tracked_combo_prop_markets = db.scalars(
            select(Market).where(
                Market.status.in_(tuple(OPEN_MARKET_STATUSES)),
                Market.raw_data["copilot_market_family"].as_string() == "player_prop",
                Market.raw_data["copilot_source_type"].as_string() == "combo_derived",
            )
        ).all()
        for market in tracked_combo_prop_markets:
            try:
                payload = client.get_market(market.ticker)
            except Exception:
                continue
            if not payload:
                continue
            payload_records.append(
                {
                    "payload": payload,
                    "source_type": "combo_derived",
                    "source_payload": None,
                    "classification_override": None,
                }
            )

    if discover_combo_props:
        combo_leg_tickers_seen: set[str] = set()
        # Bug #18: same pagination budget as the standalone scan — combo
        # markets sit even deeper in Kalshi's ordering so the 5K cap
        # hurt this path more.
        for combo_payload in client.list_markets(
            status="open",
            limit=1000,
            mve_filter="include",
            wall_clock_budget_seconds=120.0,
        ):
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
                        "source_payload": combo_payload,
                        "classification_override": leg_classification,
                    }
                )
    persisted = _persist_market_payload_records(db, payload_records)
    processed = int(persisted.get("processed") or 0)
    supported_nba_props = int(persisted.get("supported_nba_props_seen") or 0)
    supported_mlb_props = int(persisted.get("supported_mlb_props_seen") or 0)
    unsupported_prop_categories = dict(persisted.get("unsupported_prop_category_counts") or {})
    combo_prop_legs_discovered = int(persisted.get("combo_prop_legs_discovered") or 0)
    combo_prop_legs_refreshed = int(persisted.get("combo_prop_legs_refreshed") or 0)
    market_snapshots_written = int(persisted.get("market_snapshots_written") or 0)
    return {
        "processed": processed,
        "total_kalshi_markets_seen": total_seen,
        "supported_nba_props_seen": supported_nba_props,
        "supported_mlb_props_seen": supported_mlb_props,
        "unsupported_prop_category_counts": unsupported_prop_categories,
        "combo_prop_legs_discovered": combo_prop_legs_discovered,
        "combo_prop_legs_refreshed": combo_prop_legs_refreshed,
        "market_snapshots_written": market_snapshots_written,
        "touched_market_ids": persisted.get("touched_market_ids") or set(),
        "open_market_tickers": persisted.get("open_market_tickers") or set(),
    }


def refresh_current_slate_kalshi_markets(
    db: Session,
    client: KalshiPublicClient | None = None,
) -> dict[str, object]:
    """Hydrate known current-slate markets directly before falling back to a broad scan."""
    client = client or KalshiPublicClient()
    target_markets = current_watchlist_markets(db)
    payload_records: list[dict[str, Any]] = []
    for market in target_markets:
        try:
            payload = client.get_market(market.ticker)
        except Exception:
            continue
        if not payload:
            continue
        source_type = str((market.raw_data or {}).get("copilot_source_type") or "standalone")
        payload_records.append(
            {
                "payload": payload,
                "source_type": "combo_derived" if source_type == "combo_derived" else "standalone",
                "source_payload": None,
                "classification_override": None,
            }
        )

    if not payload_records:
        fallback = refresh_kalshi_markets(
            db,
            client=client,
            include_standalone=True,
            refresh_combo_prop_tickers=False,
            discover_combo_props=False,
        )
        fallback["current_slate_targeted_market_count"] = len(target_markets)
        fallback["current_slate_targeted_markets_refreshed"] = 0
        fallback["broad_market_fallback_used"] = True
        return fallback

    # Smarter #23 phase 2 — the primary path reached `_persist_market_payload_records`
    # with non-empty payload_records, meaning at least one `client.get_market`
    # responded successfully. Record `kalshi_markets` health here too — the
    # fallback path is the only branch that goes through `refresh_kalshi_markets`'s
    # wrapper, so without this call the primary path never updates the board.
    from app.services.upstream_health import record_upstream_success  # noqa: PLC0415
    record_upstream_success(db, "kalshi_markets")

    persisted = _persist_market_payload_records(db, payload_records)
    return {
        "processed": int(persisted.get("processed") or 0),
        "total_kalshi_markets_seen": len(target_markets),
        "supported_nba_props_seen": int(persisted.get("supported_nba_props_seen") or 0),
        "supported_mlb_props_seen": int(persisted.get("supported_mlb_props_seen") or 0),
        "unsupported_prop_category_counts": dict(persisted.get("unsupported_prop_category_counts") or {}),
        "combo_prop_legs_discovered": int(persisted.get("combo_prop_legs_discovered") or 0),
        "combo_prop_legs_refreshed": int(persisted.get("combo_prop_legs_refreshed") or 0),
        "market_snapshots_written": int(persisted.get("market_snapshots_written") or 0),
        "touched_market_ids": persisted.get("touched_market_ids") or set(),
        "open_market_tickers": persisted.get("open_market_tickers") or set(),
        "current_slate_targeted_market_count": len(target_markets),
        "current_slate_targeted_markets_refreshed": len(payload_records),
        "broad_market_fallback_used": False,
    }





def advance_current_slate_refresh_job(
    db: Session,
    *,
    job: RefreshJob,
    provider: TheSportsDBClient | None = None,
    major_provider: EspnPublicClient | None = None,
    niche_provider: TheSportsDBClient | None = None,
    public_client: KalshiPublicClient | None = None,
    sports: Iterable[str] | None = None,
) -> tuple[Run, bool]:
    details = dict(job.details or {})
    active_sports = list(sports or ["NBA", "MLB"])
    run = _ensure_current_slate_run(db, job=job, sports=active_sports)
    phase = str(details.get("phase") or "sports_ingest")
    cursor_payload = dict(details.get("cursor") or {}) or None
    stage_details = {str(key): float(value or 0.0) for key, value in dict(details.get("stage_details") or {}).items()}
    sports_summary = {
        "processed": int((details.get("sports_summary") or {}).get("processed") or 0),
        "sports_records_ingested": dict((details.get("sports_summary") or {}).get("sports_records_ingested") or {}),
        "sports_fetch_errors": dict((details.get("sports_summary") or {}).get("sports_fetch_errors") or {}),
    }
    touched_event_ids = [int(value) for value in list(details.get("touched_event_ids") or [])]
    kalshi_summary = dict(details.get("kalshi_summary") or {})
    touched_market_ids = [int(value) for value in list(details.get("touched_market_ids") or [])]
    mapped_count = int(details.get("mapped_count") or 0)
    warm_summary = {str(key): int(value or 0) for key, value in dict(details.get("warm_summary") or {}).items()}
    staged_watchlist_summary = _watchlist_summary_from_payload(details.get("watchlist_summary"))
    candidate_market_ids = [int(value) for value in list(details.get("candidate_market_ids") or [])]
    candidate_market_count = int(details.get("candidate_market_count") or 0)
    affected_event_count = int(details.get("affected_event_count") or 0)
    snapshot_generated_at = str(details.get("snapshot_generated_at") or "").strip() or None

    with httpx.Client(follow_redirects=True, timeout=20) as shared_http_client:
        kalshi_client = public_client or KalshiPublicClient(http_client=shared_http_client)
        espn_client = major_provider or EspnPublicClient(http_client=shared_http_client)
        stage_started = perf_counter()
        batch_size = 0
        complete = False

        if phase == "sports_ingest":
            refreshed = refresh_sports_data(
                db,
                provider=provider,
                major_provider=espn_client,
                niche_provider=niche_provider,
                sports=active_sports,
                lookback_days=get_settings().current_slate_lookback_days,
                lookahead_days=get_settings().current_slate_lookahead_days,
            )
            sports_summary = {
                "processed": int(refreshed.get("processed") or 0),
                "sports_records_ingested": dict(refreshed.get("sports_records_ingested") or {}),
                "sports_fetch_errors": dict(refreshed.get("sports_fetch_errors") or {}),
            }
            touched_event_ids = [int(value) for value in sorted(refreshed.get("touched_event_ids") or set())]
            stage_details["sports_ingest_seconds"] = round(stage_details.get("sports_ingest_seconds", 0.0) + (perf_counter() - stage_started), 3)
            batch_size = int(sports_summary.get("processed") or 0)
            phase = "kalshi_ingest"
            cursor_payload = {}
        elif phase == "kalshi_ingest":
            refreshed = refresh_current_slate_kalshi_markets(
                db,
                client=kalshi_client,
            )
            touched_market_ids = [int(value) for value in sorted(refreshed.get("touched_market_ids") or set())]
            kalshi_summary = {
                "processed": int(refreshed.get("processed") or 0),
                "total_kalshi_markets_seen": int(refreshed.get("total_kalshi_markets_seen") or 0),
                "supported_nba_props_seen": int(refreshed.get("supported_nba_props_seen") or 0),
                "supported_mlb_props_seen": int(refreshed.get("supported_mlb_props_seen") or 0),
                "unsupported_prop_category_counts": dict(refreshed.get("unsupported_prop_category_counts") or {}),
                "combo_prop_legs_discovered": int(refreshed.get("combo_prop_legs_discovered") or 0),
                "combo_prop_legs_refreshed": int(refreshed.get("combo_prop_legs_refreshed") or 0),
                "market_snapshots_written": int(refreshed.get("market_snapshots_written") or 0),
                "current_slate_targeted_market_count": int(refreshed.get("current_slate_targeted_market_count") or 0),
                "current_slate_targeted_markets_refreshed": int(refreshed.get("current_slate_targeted_markets_refreshed") or 0),
                "broad_market_fallback_used": bool(refreshed.get("broad_market_fallback_used") or False),
            }
            stage_details["kalshi_ingest_seconds"] = round(stage_details.get("kalshi_ingest_seconds", 0.0) + (perf_counter() - stage_started), 3)
            batch_size = int(kalshi_summary.get("processed") or 0)
            phase = "market_mapping"
            cursor_payload = {}
        elif phase == "market_mapping":
            mapped_count = map_markets_to_events(
                db,
                candidate_market_ids=set(touched_market_ids) if touched_market_ids else None,
            )
            kalshi_summary["mapped_markets"] = mapped_count
            stage_details["market_mapping_seconds"] = round(stage_details.get("market_mapping_seconds", 0.0) + (perf_counter() - stage_started), 3)
            batch_size = mapped_count
            phase = "candidate_selection"
            cursor_payload = {}
        elif phase == "candidate_selection":
            candidate_market_ids, affected_event_count = _current_slate_candidate_market_ids(
                db,
                touched_market_ids=set(touched_market_ids),
                touched_event_ids=set(touched_event_ids),
            )
            candidate_market_count = len(candidate_market_ids)
            batch_size = candidate_market_count
            if candidate_market_ids:
                phase = "warm_prop_context_batch"
                cursor_payload = {"candidate_index": 0}
            else:
                phase = "watchlist_finalize"
                cursor_payload = {}
        elif phase == "warm_prop_context_batch":
            resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=True)
            batch_summary, next_index, phase_complete = _warm_current_slate_context_batch(
                db,
                resolver=resolver,
                market_ids=candidate_market_ids,
                cursor_index=int(cursor_payload.get("candidate_index") or 0) if cursor_payload else 0,
                batch_size=PROP_CONTEXT_BATCH_SIZE,
            )
            warm_summary = _merge_numeric_detail_maps(warm_summary, batch_summary)
            stage_details["prop_warming_seconds"] = round(stage_details.get("prop_warming_seconds", 0.0) + (perf_counter() - stage_started), 3)
            batch_size = PROP_CONTEXT_BATCH_SIZE
            if phase_complete:
                phase = "watchlist_score_batch"
                cursor_payload = {"candidate_index": 0}
            else:
                cursor_payload = {"candidate_index": next_index}
        elif phase == "watchlist_score_batch":
            resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=False)
            batch_summary, next_index, phase_complete = stage_current_slate_watchlist_batch(
                db,
                run_id=run.id,
                market_ids=candidate_market_ids,
                resolver=resolver,
                cursor_index=int(cursor_payload.get("candidate_index") or 0) if cursor_payload else 0,
                batch_size=WATCHLIST_SCORE_BATCH_SIZE,
            )
            staged_watchlist_summary = _merge_watchlist_summaries(staged_watchlist_summary, batch_summary)
            stage_details["watchlist_regeneration_seconds"] = round(stage_details.get("watchlist_regeneration_seconds", 0.0) + (perf_counter() - stage_started), 3)
            batch_size = WATCHLIST_SCORE_BATCH_SIZE
            if phase_complete:
                phase = "watchlist_finalize"
                cursor_payload = {}
            else:
                cursor_payload = {"candidate_index": next_index}
        elif phase == "watchlist_finalize":
            batch_summary = finalize_current_slate_watchlist(
                db,
                run_id=run.id,
                candidate_market_ids=set(candidate_market_ids),
                staged_summary=staged_watchlist_summary,
            )
            batch_summary.heuristic_longshots_suppressed += staged_watchlist_summary.heuristic_longshots_suppressed
            batch_summary.critical_context_suppressed += staged_watchlist_summary.critical_context_suppressed
            batch_summary.loaded_candidate_market_count += staged_watchlist_summary.loaded_candidate_market_count
            batch_summary.filtered_candidate_market_count += staged_watchlist_summary.filtered_candidate_market_count
            batch_summary.candidate_filter_reason_counts = _merge_count_maps(
                batch_summary.candidate_filter_reason_counts,
                staged_watchlist_summary.candidate_filter_reason_counts,
            )
            batch_summary.scored_market_count = max(
                batch_summary.scored_market_count,
                staged_watchlist_summary.scored_market_count,
            )
            batch_summary.coverage_prediction_count = max(
                batch_summary.coverage_prediction_count,
                staged_watchlist_summary.coverage_prediction_count,
            )
            batch_summary.outcome_reason_counts = _merge_count_maps(
                batch_summary.outcome_reason_counts,
                staged_watchlist_summary.outcome_reason_counts,
            )
            staged_watchlist_summary = batch_summary
            stage_details["watchlist_regeneration_seconds"] = round(stage_details.get("watchlist_regeneration_seconds", 0.0) + (perf_counter() - stage_started), 3)
            batch_size = max(staged_watchlist_summary.prediction_count, 1)
            phase = "trade_snapshot_persist"
            cursor_payload = {}
        elif phase == "trade_snapshot_persist":
            snapshots = persist_current_slate_snapshots(
                db,
                source_run_id=run.id,
            )
            snapshot_generated_at = snapshots.get("all").isoformat() if snapshots.get("all") else datetime.now(timezone.utc).isoformat()
            batch_size = len(snapshots)
            complete = True
        else:
            raise ValueError(f"Unsupported current-slate phase: {phase}")

    elapsed = round(perf_counter() - stage_started, 3)
    logger.info(
        "refresh_job_phase",
        extra={
            "job_id": job.id,
            "kind": "refresh",
            "scope": "current_slate",
            "phase": phase,
            "elapsed_seconds": elapsed,
            "complete": complete,
        },
    )

    details.update(
        {
            "phase": phase,
            "cursor": cursor_payload or {},
            "sports_summary": sports_summary,
            "touched_event_ids": touched_event_ids,
            "kalshi_summary": kalshi_summary,
            "touched_market_ids": touched_market_ids,
            "mapped_count": mapped_count,
            "warm_summary": warm_summary,
            "watchlist_summary": _watchlist_summary_to_payload(staged_watchlist_summary),
            "candidate_market_ids": candidate_market_ids,
            "candidate_market_count": candidate_market_count,
            "affected_event_count": affected_event_count,
            "current_slate_loaded_candidate_market_count": staged_watchlist_summary.loaded_candidate_market_count,
            "current_slate_filtered_candidate_market_count": staged_watchlist_summary.filtered_candidate_market_count,
            "current_slate_candidate_filter_reason_counts": dict(staged_watchlist_summary.candidate_filter_reason_counts or {}),
            "current_slate_blocking_reason": _current_slate_blocking_reason(
                event_count=affected_event_count,
                candidate_market_count=candidate_market_count,
                summary=staged_watchlist_summary,
            ),
            "processed_so_far": _current_slate_processed_so_far(
                {
                    "sports_summary": sports_summary,
                    "kalshi_summary": kalshi_summary,
                    "mapped_count": mapped_count,
                    "watchlist_summary": _watchlist_summary_to_payload(staged_watchlist_summary),
                }
            ),
            "batch_size": batch_size,
            "last_batch_seconds": round(perf_counter() - stage_started, 3),
            "remaining_estimate": None,
            "snapshot_generated_at": snapshot_generated_at,
        }
    )
    job.details = details
    run.records_processed = int(details.get("processed_so_far") or 0)
    run.details = {
        "sports": active_sports,
        "refresh_scope": "current_slate",
        "phase": phase,
        "cursor": cursor_payload or {},
        "candidate_market_count": candidate_market_count,
        "affected_event_count": affected_event_count,
        "current_slate_blocking_reason": details.get("current_slate_blocking_reason"),
        "current_slate_loaded_candidate_market_count": staged_watchlist_summary.loaded_candidate_market_count,
        "current_slate_filtered_candidate_market_count": staged_watchlist_summary.filtered_candidate_market_count,
        "current_slate_candidate_filter_reason_counts": dict(staged_watchlist_summary.candidate_filter_reason_counts or {}),
        "processed_so_far": run.records_processed,
        "batch_size": batch_size,
        "last_batch_seconds": details["last_batch_seconds"],
        "snapshot_generated_at": snapshot_generated_at,
        "stage_details": stage_details,
    }

    if not complete:
        db.flush()
        return run, False

    run.details, records = _build_watchlist_run_details(
        db,
        sports=active_sports,
        sports_summary=sports_summary,
        kalshi_summary=kalshi_summary,
        mapped_count=mapped_count,
        watchlist_summary=staged_watchlist_summary,
        shadow_prediction_count=0,
        shadow_parlay_prediction_count=0,
        single_settlement_summary=None,
        parlay_settlement_summary=None,
        extra_details={
            **warm_summary,
            **stage_details,
            "refresh_scope": "current_slate",
            "candidate_market_count": candidate_market_count,
            "affected_event_count": affected_event_count,
            "current_slate_event_count": affected_event_count,
            "current_slate_candidate_market_count": candidate_market_count,
            "current_slate_loaded_candidate_market_count": staged_watchlist_summary.loaded_candidate_market_count,
            "current_slate_filtered_candidate_market_count": staged_watchlist_summary.filtered_candidate_market_count,
            "current_slate_candidate_filter_reason_counts": dict(staged_watchlist_summary.candidate_filter_reason_counts or {}),
            "current_slate_blocking_reason": details.get("current_slate_blocking_reason"),
            "snapshot_generated_at": snapshot_generated_at,
        },
    )
    run.records_processed = records
    run.status = "completed"
    run.finished_at = datetime.now(timezone.utc)
    db.flush()
    return run, True


def advance_prop_refresh_job(
    db: Session,
    *,
    job: RefreshJob,
    major_provider: EspnPublicClient | None = None,
    public_client: KalshiPublicClient | None = None,
    sports: Iterable[str] | None = None,
) -> tuple[Run, bool]:
    details = dict(job.details or {})
    run = _ensure_prop_refresh_run(db, job=job, sports=sports)
    phase = str(details.get("phase") or "combo_discovery_page")
    cursor_payload = dict(details.get("cursor") or {}) or None
    stage_details = {str(key): float(value or 0.0) for key, value in dict(details.get("stage_details") or {}).items()}
    kalshi_summary = dict(details.get("kalshi_summary") or {})
    warm_summary = {str(key): int(value or 0) for key, value in dict(details.get("warm_summary") or {}).items()}
    staged_watchlist_summary = _watchlist_summary_from_payload(details.get("watchlist_summary"))
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
        **{str(key): int(value or 0) for key, value in dict(details.get("single_settlement_summary") or {}).items()},
    }
    parlay_settlement_summary = {
        "processed": 0,
        "updated": 0,
        "won": 0,
        "lost": 0,
        "push": 0,
        "cancelled": 0,
        "pending": 0,
        "unresolved": 0,
        "errors": 0,
        **{str(key): int(value or 0) for key, value in dict(details.get("parlay_settlement_summary") or {}).items()},
    }

    with httpx.Client(follow_redirects=True, timeout=20) as shared_http_client:
        kalshi_client = public_client or KalshiPublicClient(http_client=shared_http_client)
        espn_client = major_provider or EspnPublicClient(http_client=shared_http_client)
        batch_started = perf_counter()
        batch_size = 0
        complete = False

        if phase == "combo_discovery_page":
            batch_summary, next_cursor, phase_complete = _refresh_combo_prop_discovery_batch(
                db,
                client=kalshi_client,
                cursor_payload=cursor_payload,
                limit=PROP_REFRESH_COMBO_PAGE_SIZE,
                leg_batch_size=PROP_REFRESH_COMBO_LEG_BATCH_SIZE,
            )
            kalshi_summary = {
                "processed": int(kalshi_summary.get("processed") or 0) + int(batch_summary.get("processed") or 0),
                "total_kalshi_markets_seen": int(kalshi_summary.get("total_kalshi_markets_seen") or 0) + int(batch_summary.get("total_kalshi_markets_seen") or 0),
                "supported_nba_props_seen": int(kalshi_summary.get("supported_nba_props_seen") or 0) + int(batch_summary.get("supported_nba_props_seen") or 0),
                "supported_mlb_props_seen": int(kalshi_summary.get("supported_mlb_props_seen") or 0) + int(batch_summary.get("supported_mlb_props_seen") or 0),
                "unsupported_prop_category_counts": _merge_count_maps(
                    dict(kalshi_summary.get("unsupported_prop_category_counts") or {}),
                    dict(batch_summary.get("unsupported_prop_category_counts") or {}),
                ),
                "combo_prop_legs_discovered": int(kalshi_summary.get("combo_prop_legs_discovered") or 0) + int(batch_summary.get("combo_prop_legs_discovered") or 0),
                "combo_prop_legs_refreshed": int(kalshi_summary.get("combo_prop_legs_refreshed") or 0) + int(batch_summary.get("combo_prop_legs_refreshed") or 0),
                "market_snapshots_written": int(kalshi_summary.get("market_snapshots_written") or 0) + int(batch_summary.get("market_snapshots_written") or 0),
                "mapped_markets": int(kalshi_summary.get("mapped_markets") or 0) + int(batch_summary.get("mapped_markets") or 0),
            }
            stage_details["kalshi_ingest_seconds"] = round(stage_details.get("kalshi_ingest_seconds", 0.0) + (perf_counter() - batch_started), 3)
            batch_size = PROP_REFRESH_COMBO_LEG_BATCH_SIZE
            if phase_complete:
                phase = "warm_prop_context_batch"
                cursor_payload = None
            else:
                cursor_payload = next_cursor
        elif phase == "warm_prop_context_batch":
            resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=True)
            batch_summary, next_cursor, phase_complete = _warm_prop_context_batch(
                db,
                resolver=resolver,
                cursor_market_id=int(cursor_payload.get("market_id")) if cursor_payload and cursor_payload.get("market_id") is not None else None,
                batch_size=PROP_CONTEXT_BATCH_SIZE,
            )
            warm_summary = _merge_numeric_detail_maps(warm_summary, batch_summary)
            stage_details["prop_warming_seconds"] = round(stage_details.get("prop_warming_seconds", 0.0) + (perf_counter() - batch_started), 3)
            batch_size = PROP_CONTEXT_BATCH_SIZE
            if phase_complete:
                phase = "watchlist_score_batch"
                cursor_payload = None
            else:
                cursor_payload = {"market_id": next_cursor}
        elif phase == "watchlist_score_batch":
            resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=False)
            batch_summary, next_cursor, phase_complete = stage_maintenance_watchlist_batch(
                db,
                run_id=run.id,
                resolver=resolver,
                cursor_market_id=int(cursor_payload.get("market_id")) if cursor_payload and cursor_payload.get("market_id") is not None else None,
                batch_size=WATCHLIST_SCORE_BATCH_SIZE,
            )
            staged_watchlist_summary = _merge_watchlist_summaries(staged_watchlist_summary, batch_summary)
            stage_details["watchlist_regeneration_seconds"] = round(stage_details.get("watchlist_regeneration_seconds", 0.0) + (perf_counter() - batch_started), 3)
            batch_size = WATCHLIST_SCORE_BATCH_SIZE
            if phase_complete:
                phase = "watchlist_finalize"
                cursor_payload = None
            else:
                cursor_payload = {"market_id": next_cursor}
        elif phase == "watchlist_finalize":
            batch_summary = finalize_staged_watchlist(db, run_id=run.id, capture_parlays=True)
            batch_summary.heuristic_longshots_suppressed += staged_watchlist_summary.heuristic_longshots_suppressed
            batch_summary.critical_context_suppressed += staged_watchlist_summary.critical_context_suppressed
            staged_watchlist_summary = batch_summary
            stage_details["watchlist_regeneration_seconds"] = round(stage_details.get("watchlist_regeneration_seconds", 0.0) + (perf_counter() - batch_started), 3)
            batch_size = max(batch_summary.recommendation_count, 1)
            phase = "settle_predictions_batch"
            cursor_payload = None
        elif phase == "settle_predictions_batch":
            batch_summary, next_cursor = settle_predictions_batch(
                db,
                client=kalshi_client,
                limit=PREDICTION_SETTLEMENT_BATCH_SIZE,
                cursor=cursor_payload,
            )
            single_settlement_summary = _merge_numeric_detail_maps(single_settlement_summary, batch_summary)
            stage_details["maintenance_settlement_seconds"] = round(stage_details.get("maintenance_settlement_seconds", 0.0) + (perf_counter() - batch_started), 3)
            batch_size = PREDICTION_SETTLEMENT_BATCH_SIZE
            if next_cursor is None:
                phase = "settle_parlays_batch"
                cursor_payload = None
            else:
                cursor_payload = next_cursor
        elif phase == "settle_parlays_batch":
            batch_summary, next_cursor = settle_parlay_predictions_batch(
                db,
                limit=PARLAY_SETTLEMENT_BATCH_SIZE,
                cursor=cursor_payload,
            )
            parlay_settlement_summary = _merge_numeric_detail_maps(parlay_settlement_summary, batch_summary)
            stage_details["maintenance_settlement_seconds"] = round(stage_details.get("maintenance_settlement_seconds", 0.0) + (perf_counter() - batch_started), 3)
            batch_size = PARLAY_SETTLEMENT_BATCH_SIZE
            if next_cursor is None:
                complete = True
            else:
                cursor_payload = next_cursor
        else:
            raise ValueError(f"Unsupported prop refresh phase: {phase}")

    elapsed = round(perf_counter() - batch_started, 3)
    logger.info(
        "refresh_job_phase",
        extra={
            "job_id": job.id,
            "kind": "prop_refresh",
            "scope": "maintenance",
            "phase": phase,
            "elapsed_seconds": elapsed,
            "complete": complete,
        },
    )

    details.update(
        {
            "phase": phase,
            "cursor": cursor_payload or {},
            "kalshi_summary": kalshi_summary,
            "warm_summary": warm_summary,
            "watchlist_summary": _watchlist_summary_to_payload(staged_watchlist_summary),
            "single_settlement_summary": single_settlement_summary,
            "parlay_settlement_summary": parlay_settlement_summary,
            "stage_details": stage_details,
            "processed_so_far": _prop_refresh_processed_so_far(
                {
                    "kalshi_summary": kalshi_summary,
                    "watchlist_summary": _watchlist_summary_to_payload(staged_watchlist_summary),
                    "single_settlement_summary": single_settlement_summary,
                    "parlay_settlement_summary": parlay_settlement_summary,
                }
            ),
            "batch_size": batch_size,
            "last_batch_seconds": round(perf_counter() - batch_started, 3),
            "remaining_estimate": None,
        }
    )
    job.details = details
    run.records_processed = int(details.get("processed_so_far") or 0)
    run.details = {
        "sports": list(sports or get_settings().enabled_sports),
        "refresh_scope": "maintenance",
        "phase": phase,
        "cursor": cursor_payload or {},
        "processed_so_far": run.records_processed,
        "last_batch_seconds": details["last_batch_seconds"],
        "stage_details": stage_details,
        "kalshi_summary": kalshi_summary,
        "warm_summary": warm_summary,
        "watchlist_summary": _watchlist_summary_to_payload(staged_watchlist_summary),
        "single_settlement_summary": single_settlement_summary,
        "parlay_settlement_summary": parlay_settlement_summary,
    }

    if not complete:
        db.flush()
        return run, False

    final_watchlist_summary = staged_watchlist_summary
    run.details, records = _build_watchlist_run_details(
        db,
        sports=sports,
        sports_summary=None,
        kalshi_summary={
            **kalshi_summary,
            "processed": int(kalshi_summary.get("processed") or 0),
        },
        mapped_count=int(kalshi_summary.get("mapped_markets") or 0),
        watchlist_summary=final_watchlist_summary,
        single_settlement_summary=single_settlement_summary,
        parlay_settlement_summary=parlay_settlement_summary,
        extra_details={**warm_summary, **stage_details, "refresh_scope": "maintenance"},
    )
    run.records_processed = records
    run.status = "completed"
    run.finished_at = datetime.now(timezone.utc)
    db.flush()
    return run, True



# Refresh / prop-refresh / shadow-capture cycle runners moved to
# ``ingestion.cycles`` (R2 phase 2). Re-exported here so the
# scheduler / refresh-job worker that import ``run_*_cycle`` from
# ``app.services.ingestion`` keep working unchanged.
from app.services.ingestion.cycles import (  # noqa: E402
    run_prop_refresh_cycle,
    run_refresh_cycle,
    run_shadow_capture_cycle,
)
