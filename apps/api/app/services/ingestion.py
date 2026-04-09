from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.espn import EspnPublicClient
from app.clients.kalshi import KalshiPublicClient, snapshot_from_market_payload
from app.clients.sports_data import TheSportsDBClient
from app.config import get_settings
from app.models import Event, EventParticipant, League, Market, MarketSnapshot, ParlayRecommendation, Participant, Recommendation, RefreshJob, Run, Sport
from app.services.market_mapping import map_markets_to_events
from app.services.market_support import classify_market_payload, infer_market_sport_key, infer_supported_market_kind
from app.services.ml import capture_shadow_artifacts, sync_family_runtime_health
from app.services.parlays import settle_parlay_predictions, settle_parlay_predictions_batch
from app.services.predictions import OPEN_MARKET_STATUSES, settle_predictions, settle_predictions_batch
from app.services.scoring import (
    PropStatsResolver,
    WatchlistGenerationSummary,
    finalize_staged_watchlist,
    regenerate_watchlist,
    stage_maintenance_watchlist_batch,
    warm_prop_context_cache,
)
from app.services.watchlist_coverage import current_watchlist_markets, latest_snapshot_by_market_id, warm_current_watchlist_prop_context
from app.sports.base import NormalizedEvent
from app.sports.registry import ADAPTERS


SPORT_LABELS = {
    "NBA": "NBA",
    "NFL": "NFL",
    "MLB": "MLB",
    "SOCCER": "Soccer",
    "TENNIS": "TENNIS",
}

FREE_PROVIDER_SPORTS = {"SOCCER", "TENNIS"}
PUBLIC_MAJOR_SPORTS = {"NBA", "NFL", "MLB"}
PROP_REFRESH_COMBO_PAGE_SIZE = 100
PROP_CONTEXT_BATCH_SIZE = 50
WATCHLIST_SCORE_BATCH_SIZE = 100
PREDICTION_SETTLEMENT_BATCH_SIZE = 250
PARLAY_SETTLEMENT_BATCH_SIZE = 100


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


def _merge_numeric_detail_maps(*payloads: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for payload in payloads:
        for key, value in payload.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


def _merge_count_maps(*payloads: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for payload in payloads:
        for key, value in payload.items():
            merged[str(key)] = merged.get(str(key), 0) + int(value or 0)
    return merged


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


def _prop_market_summary_counts(db: Session) -> tuple[int, dict[str, int], dict[str, int]]:
    watchlist_by_sport: dict[str, int] = {}
    watchlist_by_prop_category: dict[str, int] = {}

    recommendations = db.scalars(select(Recommendation).join(Market, Recommendation.market_id == Market.id)).all()
    for recommendation in recommendations:
        market = db.scalar(select(Market).where(Market.id == recommendation.market_id))
        if not market:
            continue
        sport_key = market.sport_key or "UNKNOWN"
        watchlist_by_sport[sport_key] = watchlist_by_sport.get(sport_key, 0) + 1
        raw_data = market.raw_data or {}
        if raw_data.get("copilot_market_family") == "player_prop":
            stat_key = str(raw_data.get("copilot_stat_key") or "unknown")
            watchlist_by_prop_category[stat_key] = watchlist_by_prop_category.get(stat_key, 0) + 1

    mapped_prop_markets = 0
    prop_markets = db.scalars(select(Market).where(Market.raw_data.is_not(None))).all()
    for market in prop_markets:
        raw_data = market.raw_data or {}
        if raw_data.get("copilot_market_family") != "player_prop":
            continue
        if market.event_id:
            mapped_prop_markets += 1

    return mapped_prop_markets, watchlist_by_sport, watchlist_by_prop_category


def _parlay_watchlist_counts(db: Session) -> tuple[dict[str, int], dict[str, int]]:
    parlay_watchlist_by_scope: dict[str, int] = {}
    parlay_watchlist_by_leg_count: dict[str, int] = {}
    parlay_recommendations = db.scalars(select(ParlayRecommendation)).all()
    for parlay in parlay_recommendations:
        parlay_watchlist_by_scope[parlay.sport_scope] = parlay_watchlist_by_scope.get(parlay.sport_scope, 0) + 1
        leg_key = str(parlay.leg_count)
        parlay_watchlist_by_leg_count[leg_key] = parlay_watchlist_by_leg_count.get(leg_key, 0) + 1
    return parlay_watchlist_by_scope, parlay_watchlist_by_leg_count


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
            raw_events, sport_errors = _fetch_events_window_with_diagnostics(
                major_provider,
                sport_key,
                major_start_day,
                major_end_day,
            )
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
            _upsert_event(db, normalized)
            processed += 1
            processed_by_sport[sport_key] = processed_by_sport.get(sport_key, 0) + 1
    db.flush()
    return {
        "processed": processed,
        "sports_records_ingested": processed_by_sport,
        "sports_fetch_errors": fetch_errors,
    }


def refresh_kalshi_markets(
    db: Session,
    client: KalshiPublicClient | None = None,
    *,
    include_standalone: bool = True,
    refresh_combo_prop_tickers: bool = True,
    discover_combo_props: bool = True,
) -> dict[str, object]:
    client = client or KalshiPublicClient()
    settings = get_settings()
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
        for payload in client.list_markets(status="open", limit=1000, mve_filter="exclude"):
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
        tracked_combo_prop_markets = [
            market
            for market in db.scalars(select(Market).where(Market.status.in_(tuple(OPEN_MARKET_STATUSES)))).all()
            if (market.raw_data or {}).get("copilot_market_family") == "player_prop"
            and (market.raw_data or {}).get("copilot_source_type") == "combo_derived"
        ]
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
        for combo_payload in client.list_markets(status="open", limit=1000, mve_filter="include"):
            if not (combo_payload.get("mve_collection_ticker") or combo_payload.get("mve_selected_legs")):
                continue
            for leg in combo_payload.get("mve_selected_legs") or []:
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



def _merge_settlement_summaries(*summaries: dict[str, int]) -> dict[str, int]:
    merged = {
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
    for summary in summaries:
        for key in merged:
            merged[key] += int(summary.get(key) or 0)
    return merged


def _build_watchlist_run_details(
    db: Session,
    *,
    sports: Iterable[str] | None,
    sports_summary: dict[str, object] | None,
    kalshi_summary: dict[str, object],
    mapped_count: int,
    watchlist_summary,
    shadow_prediction_count: int = 0,
    shadow_parlay_prediction_count: int = 0,
    single_settlement_summary: dict[str, int] | None = None,
    parlay_settlement_summary: dict[str, int] | None = None,
    extra_details: dict[str, object] | None = None,
) -> tuple[dict[str, object], int]:
    single_settlement_summary = single_settlement_summary or {
        "updated": 0,
        "won": 0,
        "lost": 0,
        "push": 0,
        "cancelled": 0,
        "pending": 0,
        "unresolved": 0,
        "errors": 0,
    }
    parlay_settlement_summary = parlay_settlement_summary or {
        "updated": 0,
        "won": 0,
        "lost": 0,
        "push": 0,
        "cancelled": 0,
        "pending": 0,
        "unresolved": 0,
        "errors": 0,
    }
    mapped_prop_markets, watchlist_by_sport, watchlist_by_prop_category = _prop_market_summary_counts(db)
    parlay_watchlist_by_scope, parlay_watchlist_by_leg_count = _parlay_watchlist_counts(db)
    records = (
        int((sports_summary or {}).get("processed") or 0)
        + int(kalshi_summary.get("processed") or 0)
        + mapped_count
        + watchlist_summary.recommendation_count
        + watchlist_summary.prediction_count
        + watchlist_summary.parlay_recommendation_count
        + watchlist_summary.parlay_prediction_count
        + shadow_prediction_count
        + shadow_parlay_prediction_count
        + int(single_settlement_summary.get("updated") or 0)
        + int(parlay_settlement_summary.get("updated") or 0)
    )
    details: dict[str, object] = {
        "sports_requested": list(sports or get_settings().enabled_sports),
        "sports_records_ingested": (sports_summary or {}).get("sports_records_ingested") or {},
        "sports_fetch_errors": (sports_summary or {}).get("sports_fetch_errors") or {},
        "total_kalshi_markets_seen": kalshi_summary.get("total_kalshi_markets_seen") or 0,
        "supported_markets_kept": kalshi_summary.get("processed") or 0,
        "market_snapshots_written": kalshi_summary.get("market_snapshots_written") or 0,
        "supported_nba_props_seen": kalshi_summary.get("supported_nba_props_seen") or 0,
        "supported_mlb_props_seen": kalshi_summary.get("supported_mlb_props_seen") or 0,
        "unsupported_prop_category_counts": kalshi_summary.get("unsupported_prop_category_counts") or {},
        "combo_prop_legs_discovered": kalshi_summary.get("combo_prop_legs_discovered") or 0,
        "combo_prop_legs_refreshed": kalshi_summary.get("combo_prop_legs_refreshed") or 0,
        "mapped_markets": mapped_count,
        "mapped_prop_markets": mapped_prop_markets,
        "recommendations_emitted": watchlist_summary.recommendation_count,
        "predictions_captured": watchlist_summary.prediction_count,
        "parlay_recommendations_emitted": watchlist_summary.parlay_recommendation_count,
        "parlay_predictions_captured": watchlist_summary.parlay_prediction_count,
        "heuristic_longshots_suppressed": watchlist_summary.heuristic_longshots_suppressed,
        "inverse_winner_duplicates_collapsed": watchlist_summary.inverse_winner_duplicates_collapsed,
        "combo_prop_candidates_emitted": watchlist_summary.combo_prop_candidates_emitted,
        "combo_prop_candidates_suppressed": watchlist_summary.combo_prop_candidates_suppressed,
        "critical_context_suppressed": watchlist_summary.critical_context_suppressed,
        "quality_tier_counts": watchlist_summary.quality_tier_counts,
        "shadow_predictions_captured": shadow_prediction_count,
        "shadow_parlay_predictions_captured": shadow_parlay_prediction_count,
        "prediction_settlement_updated": int(single_settlement_summary.get("updated") or 0),
        "parlay_prediction_settlement_updated": int(parlay_settlement_summary.get("updated") or 0),
        "prediction_outcomes": {
            "won": int(single_settlement_summary.get("won") or 0),
            "lost": int(single_settlement_summary.get("lost") or 0),
            "push": int(single_settlement_summary.get("push") or 0),
            "cancelled": int(single_settlement_summary.get("cancelled") or 0),
            "pending": int(single_settlement_summary.get("pending") or 0),
            "unresolved": int(single_settlement_summary.get("unresolved") or 0),
            "errors": int(single_settlement_summary.get("errors") or 0),
        },
        "parlay_prediction_outcomes": {
            "won": int(parlay_settlement_summary.get("won") or 0),
            "lost": int(parlay_settlement_summary.get("lost") or 0),
            "push": int(parlay_settlement_summary.get("push") or 0),
            "cancelled": int(parlay_settlement_summary.get("cancelled") or 0),
            "pending": int(parlay_settlement_summary.get("pending") or 0),
            "unresolved": int(parlay_settlement_summary.get("unresolved") or 0),
            "errors": int(parlay_settlement_summary.get("errors") or 0),
        },
        "watchlist_counts_by_sport": watchlist_by_sport,
        "watchlist_counts_by_prop_category": watchlist_by_prop_category,
        "parlay_watchlist_counts_by_scope": parlay_watchlist_by_scope,
        "parlay_watchlist_counts_by_leg_count": parlay_watchlist_by_leg_count,
    }
    if extra_details:
        details.update(extra_details)
    return details, records


def _watchlist_summary_to_payload(summary: WatchlistGenerationSummary) -> dict[str, object]:
    return {
        "recommendation_count": summary.recommendation_count,
        "prediction_count": summary.prediction_count,
        "parlay_recommendation_count": summary.parlay_recommendation_count,
        "parlay_prediction_count": summary.parlay_prediction_count,
        "heuristic_longshots_suppressed": summary.heuristic_longshots_suppressed,
        "inverse_winner_duplicates_collapsed": summary.inverse_winner_duplicates_collapsed,
        "combo_prop_candidates_emitted": summary.combo_prop_candidates_emitted,
        "combo_prop_candidates_suppressed": summary.combo_prop_candidates_suppressed,
        "critical_context_suppressed": summary.critical_context_suppressed,
        "quality_tier_counts": dict(summary.quality_tier_counts or {}),
    }


def _watchlist_summary_from_payload(payload: dict[str, object] | None) -> WatchlistGenerationSummary:
    payload = dict(payload or {})
    return WatchlistGenerationSummary(
        recommendation_count=int(payload.get("recommendation_count") or 0),
        prediction_count=int(payload.get("prediction_count") or 0),
        parlay_recommendation_count=int(payload.get("parlay_recommendation_count") or 0),
        parlay_prediction_count=int(payload.get("parlay_prediction_count") or 0),
        heuristic_longshots_suppressed=int(payload.get("heuristic_longshots_suppressed") or 0),
        inverse_winner_duplicates_collapsed=int(payload.get("inverse_winner_duplicates_collapsed") or 0),
        combo_prop_candidates_emitted=int(payload.get("combo_prop_candidates_emitted") or 0),
        combo_prop_candidates_suppressed=int(payload.get("combo_prop_candidates_suppressed") or 0),
        critical_context_suppressed=int(payload.get("critical_context_suppressed") or 0),
        quality_tier_counts={str(key): int(value or 0) for key, value in dict(payload.get("quality_tier_counts") or {}).items()},
    )


def _merge_watchlist_summaries(
    left: WatchlistGenerationSummary,
    right: WatchlistGenerationSummary,
) -> WatchlistGenerationSummary:
    merged = WatchlistGenerationSummary(
        recommendation_count=left.recommendation_count + right.recommendation_count,
        prediction_count=left.prediction_count + right.prediction_count,
        parlay_recommendation_count=left.parlay_recommendation_count + right.parlay_recommendation_count,
        parlay_prediction_count=left.parlay_prediction_count + right.parlay_prediction_count,
        heuristic_longshots_suppressed=left.heuristic_longshots_suppressed + right.heuristic_longshots_suppressed,
        inverse_winner_duplicates_collapsed=left.inverse_winner_duplicates_collapsed + right.inverse_winner_duplicates_collapsed,
        combo_prop_candidates_emitted=left.combo_prop_candidates_emitted + right.combo_prop_candidates_emitted,
        combo_prop_candidates_suppressed=left.combo_prop_candidates_suppressed + right.combo_prop_candidates_suppressed,
        critical_context_suppressed=left.critical_context_suppressed + right.critical_context_suppressed,
        quality_tier_counts=_merge_count_maps(left.quality_tier_counts, right.quality_tier_counts),
    )
    return merged


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


def _refresh_combo_prop_discovery_batch(
    db: Session,
    *,
    client: KalshiPublicClient,
    cursor: str | None,
    limit: int = PROP_REFRESH_COMBO_PAGE_SIZE,
) -> tuple[dict[str, object], str | None, bool]:
    combo_page, next_cursor = client.list_markets_page(
        status="open",
        limit=limit,
        mve_filter="include",
        cursor=cursor,
    )
    if not combo_page:
        return {
            "processed": 0,
            "total_kalshi_markets_seen": 0,
            "supported_nba_props_seen": 0,
            "supported_mlb_props_seen": 0,
            "unsupported_prop_category_counts": {},
            "combo_prop_legs_discovered": 0,
            "combo_prop_legs_refreshed": 0,
            "market_snapshots_written": 0,
            "mapped_markets": 0,
        }, None, True

    payload_records: list[dict[str, Any]] = []
    combo_leg_tickers_seen: set[str] = set()
    total_seen = 0
    for combo_payload in combo_page:
        total_seen += 1
        if not (combo_payload.get("mve_collection_ticker") or combo_payload.get("mve_selected_legs")):
            continue
        for leg in combo_payload.get("mve_selected_legs") or []:
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
    touched_market_ids = set(persisted.get("touched_market_ids") or set())
    mapped_count = map_markets_to_events(db, candidate_market_ids=touched_market_ids if touched_market_ids else None)
    summary = {
        "processed": int(persisted.get("processed") or 0),
        "total_kalshi_markets_seen": total_seen,
        "supported_nba_props_seen": int(persisted.get("supported_nba_props_seen") or 0),
        "supported_mlb_props_seen": int(persisted.get("supported_mlb_props_seen") or 0),
        "unsupported_prop_category_counts": dict(persisted.get("unsupported_prop_category_counts") or {}),
        "combo_prop_legs_discovered": int(persisted.get("combo_prop_legs_discovered") or 0),
        "combo_prop_legs_refreshed": int(persisted.get("combo_prop_legs_refreshed") or 0),
        "market_snapshots_written": int(persisted.get("market_snapshots_written") or 0),
        "mapped_markets": mapped_count,
    }
    return summary, next_cursor, next_cursor is None


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
                cursor=str(cursor_payload.get("kalshi_cursor")) if cursor_payload and cursor_payload.get("kalshi_cursor") else None,
                limit=PROP_REFRESH_COMBO_PAGE_SIZE,
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
            batch_size = PROP_REFRESH_COMBO_PAGE_SIZE
            if phase_complete:
                phase = "warm_prop_context_batch"
                cursor_payload = None
            else:
                cursor_payload = {"kalshi_cursor": next_cursor}
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
                latest_only_per_key=True,
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


def run_refresh_cycle(
    db: Session,
    provider: TheSportsDBClient | None = None,
    major_provider: EspnPublicClient | None = None,
    niche_provider: TheSportsDBClient | None = None,
    public_client: KalshiPublicClient | None = None,
    sports: Iterable[str] | None = None,
    current_slate_only: bool = False,
) -> Run:
    initial_sports = list(sports or (["NBA", "MLB"] if current_slate_only else get_settings().enabled_sports))
    run = Run(kind="refresh", status="running", details={"sports": initial_sports})
    db.add(run)
    db.flush()
    try:
        with httpx.Client(follow_redirects=True, timeout=20) as shared_http_client:
            kalshi_client = public_client or KalshiPublicClient(http_client=shared_http_client)
            espn_client = major_provider or EspnPublicClient(http_client=shared_http_client)
            settings = get_settings()
            active_sports = list(sports or (["NBA", "MLB"] if current_slate_only else settings.enabled_sports))
            stage_details: dict[str, float] = {}

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
                latest_only_per_key=True,
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
