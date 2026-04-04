from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.espn import EspnPublicClient
from app.clients.kalshi import KalshiPublicClient, snapshot_from_market_payload
from app.clients.sports_data import TheSportsDBClient
from app.config import get_settings
from app.models import Event, EventParticipant, League, Market, MarketSnapshot, ParlayRecommendation, Participant, Recommendation, Run, Sport
from app.services.market_mapping import map_markets_to_events
from app.services.market_support import classify_market_payload, infer_market_sport_key, infer_supported_market_kind
from app.services.ml import capture_shadow_artifacts
from app.services.parlays import settle_parlay_predictions
from app.services.predictions import OPEN_MARKET_STATUSES, settle_predictions
from app.services.scoring import PropStatsResolver, regenerate_watchlist, warm_prop_context_cache
from app.services.watchlist_coverage import current_watchlist_markets, warm_current_watchlist_prop_context
from app.sports.base import NormalizedEvent
from app.sports.registry import ADAPTERS


SPORT_LABELS = {
    "NBA": "NBA",
    "NFL": "NFL",
    "MLB": "MLB",
    "SOCCER": "Soccer",
    "TENNIS": "TENNIS",
    "UFC": "UFC",
}

FREE_PROVIDER_SPORTS = {"SOCCER", "TENNIS", "UFC"}
PUBLIC_MAJOR_SPORTS = {"NBA", "NFL", "MLB"}


def _merge_numeric_detail_maps(*payloads: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for payload in payloads:
        for key, value in payload.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


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
    for key, name in SPORT_LABELS.items():
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
    processed = 0
    total_seen = 0
    supported_nba_props = 0
    supported_mlb_props = 0
    combo_prop_legs_discovered = 0
    combo_prop_legs_refreshed = 0
    unsupported_prop_categories: dict[str, int] = {}
    open_market_tickers: set[str] = set()

    def persist_market_payload(
        payload: dict[str, Any],
        *,
        source_type: str = "standalone",
        source_payload: dict[str, Any] | None = None,
        classification_override: dict[str, Any] | None = None,
    ) -> bool:
        nonlocal processed, supported_nba_props, supported_mlb_props
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
        market = db.scalar(select(Market).where(Market.ticker == ticker))
        had_existing_market = market is not None
        existing_raw = dict(market.raw_data or {}) if market else {}
        existing_source_type = str(existing_raw.get("copilot_source_type") or "standalone")
        if not market:
            market = Market(ticker=ticker, title=payload.get("title") or ticker)
            db.add(market)
            db.flush()
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
        if market.status in OPEN_MARKET_STATUSES:
            open_market_tickers.add(ticker)

        snapshot = snapshot_from_market_payload(payload)
        db.add(MarketSnapshot(market_id=market.id, raw_data=payload, **snapshot))
        processed += 1
        return True

    if include_standalone:
        for payload in client.list_markets(status="open", limit=1000, mve_filter="exclude"):
            total_seen += 1
            persist_market_payload(payload, source_type="standalone")

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
            if persist_market_payload(payload, source_type="combo_derived"):
                combo_prop_legs_refreshed += 1

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
                leg_payload = {
                    **leg_payload,
                    "mve_collection_ticker": None,
                    "mve_selected_legs": None,
                }
                if persist_market_payload(
                    leg_payload,
                    source_type="combo_derived",
                    source_payload=combo_payload,
                    classification_override=leg_classification,
                ):
                    combo_prop_legs_discovered += 1

    db.flush()
    return {
        "processed": processed,
        "total_kalshi_markets_seen": total_seen,
        "supported_nba_props_seen": supported_nba_props,
        "supported_mlb_props_seen": supported_mlb_props,
        "unsupported_prop_category_counts": unsupported_prop_categories,
        "combo_prop_legs_discovered": combo_prop_legs_discovered,
        "combo_prop_legs_refreshed": combo_prop_legs_refreshed,
        "open_market_tickers": open_market_tickers,
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
            sports_summary = refresh_sports_data(
                db,
                provider=provider,
                major_provider=espn_client,
                niche_provider=niche_provider,
                sports=active_sports,
                lookback_days=settings.current_slate_lookback_days if current_slate_only else None,
                lookahead_days=settings.current_slate_lookahead_days if current_slate_only else None,
            )
            kalshi_summary = refresh_kalshi_markets(
                db,
                client=kalshi_client,
                include_standalone=True,
                refresh_combo_prop_tickers=not current_slate_only,
                discover_combo_props=False,
            )
            mapped_count = map_markets_to_events(db)
            current_watchlist_resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=True)
            current_watchlist_summary = warm_current_watchlist_prop_context(db, resolver=current_watchlist_resolver)
            target_market_ids = {market.id for market in current_watchlist_markets(db)} if current_slate_only else None
            resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=False)
            watchlist_summary = regenerate_watchlist(
                db,
                run_id=run.id,
                resolver=resolver,
                allowed_market_ids=target_market_ids,
                replace_all=not current_slate_only,
                capture_parlays=not current_slate_only,
            )
            if current_slate_only:
                shadow_prediction_count, shadow_parlay_prediction_count = 0, 0
            else:
                shadow_prediction_count, shadow_parlay_prediction_count = capture_shadow_artifacts(
                    db,
                    run_id=run.id,
                    candidates=[],
                )
            single_settlement_summary = settle_predictions(
                db,
                client=kalshi_client,
                open_market_tickers=set(kalshi_summary.get("open_market_tickers") or set()),
                sport_keys=set(active_sports) if current_slate_only else None,
            )
            parlay_settlement_summary = settle_parlay_predictions(db) if not current_slate_only else {
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
            kalshi_summary = refresh_kalshi_markets(
                db,
                client=kalshi_client,
                include_standalone=False,
                refresh_combo_prop_tickers=False,
                discover_combo_props=True,
            )
            mapped_count = map_markets_to_events(db)
            resolver = PropStatsResolver(db, espn_client=espn_client, allow_network=True)
            warm_summary = warm_prop_context_cache(db, resolver=resolver)
            watchlist_summary = regenerate_watchlist(
                db,
                run_id=run.id,
                resolver=resolver,
            )
            run.details, records = _build_watchlist_run_details(
                db,
                sports=sports,
                sports_summary=None,
                kalshi_summary=kalshi_summary,
                mapped_count=mapped_count,
                watchlist_summary=watchlist_summary,
                extra_details=warm_summary,
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
