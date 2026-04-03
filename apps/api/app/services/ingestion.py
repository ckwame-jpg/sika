from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.espn import EspnPublicClient
from app.clients.kalshi import KalshiPublicClient, snapshot_from_market_payload
from app.clients.sports_data import TheSportsDBClient
from app.config import get_settings
from app.models import Event, EventParticipant, League, Market, MarketSnapshot, ParlayRecommendation, Participant, Recommendation, Run, Sport
from app.services.market_mapping import map_markets_to_events
from app.services.market_support import classify_market_payload, infer_market_sport_key, infer_supported_market_kind
from app.services.parlays import settle_parlay_predictions
from app.services.predictions import settle_predictions
from app.services.scoring import regenerate_watchlist
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
) -> dict[str, object]:
    settings = get_settings()
    seed_sports(db)
    major_provider = major_provider or EspnPublicClient()
    niche_provider = niche_provider or TheSportsDBClient()
    sports = list(sports or settings.enabled_sports)
    anchor_day = anchor_day or datetime.now(timezone.utc).date()
    major_start_day = anchor_day - timedelta(days=settings.lookback_days)
    major_end_day = anchor_day + timedelta(days=settings.lookahead_days)
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


def refresh_kalshi_markets(db: Session, client: KalshiPublicClient | None = None) -> dict[str, object]:
    client = client or KalshiPublicClient()
    processed = 0
    total_seen = 0
    supported_nba_props = 0
    supported_mlb_props = 0
    unsupported_prop_categories: dict[str, int] = {}
    open_market_tickers: set[str] = set()
    for payload in client.list_markets(status="open", limit=1000, mve_filter="exclude"):
        total_seen += 1
        classification = classify_market_payload(payload)
        metadata = classification.get("metadata")
        if not classification.get("supported") or not metadata:
            if classification.get("reason") == "unsupported_prop_category" and classification.get("sport_key") in {"NBA", "MLB"}:
                prop_category = str(classification.get("prop_category") or "unknown")
                unsupported_prop_categories[prop_category] = unsupported_prop_categories.get(prop_category, 0) + 1
            continue
        market_kind = str(metadata.get("copilot_market_kind") or "")
        market_sport_key = infer_market_sport_key(payload)
        ticker = payload.get("ticker")
        if not ticker:
            continue
        open_market_tickers.add(ticker)
        if metadata.get("copilot_market_family") == "player_prop":
            if market_sport_key == "NBA":
                supported_nba_props += 1
            if market_sport_key == "MLB":
                supported_mlb_props += 1
        market = db.scalar(select(Market).where(Market.ticker == ticker))
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
        market.raw_data = {**payload, **metadata, "copilot_market_kind": market_kind}

        snapshot = snapshot_from_market_payload(payload)
        db.add(MarketSnapshot(market_id=market.id, raw_data=payload, **snapshot))
        processed += 1
    db.flush()
    return {
        "processed": processed,
        "total_kalshi_markets_seen": total_seen,
        "supported_nba_props_seen": supported_nba_props,
        "supported_mlb_props_seen": supported_mlb_props,
        "unsupported_prop_category_counts": unsupported_prop_categories,
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


def run_refresh_cycle(
    db: Session,
    provider: TheSportsDBClient | None = None,
    major_provider: EspnPublicClient | None = None,
    niche_provider: TheSportsDBClient | None = None,
    public_client: KalshiPublicClient | None = None,
    sports: Iterable[str] | None = None,
) -> Run:
    run = Run(kind="refresh", status="running", details={"sports": list(sports or get_settings().enabled_sports)})
    db.add(run)
    db.flush()
    try:
        sports_summary = refresh_sports_data(
            db,
            provider=provider,
            major_provider=major_provider,
            niche_provider=niche_provider,
            sports=sports,
        )
        kalshi_summary = refresh_kalshi_markets(db, client=public_client)
        mapped_count = map_markets_to_events(db)
        recommendation_count, prediction_count, parlay_recommendation_count, parlay_prediction_count = regenerate_watchlist(
            db,
            run_id=run.id,
        )
        single_settlement_summary = settle_predictions(
            db,
            client=public_client,
            open_market_tickers=set(kalshi_summary.get("open_market_tickers") or set()),
        )
        parlay_settlement_summary = settle_parlay_predictions(db)
        settlement_summary = _merge_settlement_summaries(single_settlement_summary, parlay_settlement_summary)
        records = (
            int(sports_summary["processed"])
            + int(kalshi_summary["processed"])
            + mapped_count
            + recommendation_count
            + prediction_count
            + parlay_recommendation_count
            + parlay_prediction_count
            + int(settlement_summary["updated"])
        )
        watchlist_by_sport: dict[str, int] = {}
        watchlist_by_prop_category: dict[str, int] = {}
        parlay_watchlist_by_scope: dict[str, int] = {}
        parlay_watchlist_by_leg_count: dict[str, int] = {}
        mapped_prop_markets = 0
        prop_markets = db.scalars(select(Market).where(Market.raw_data.is_not(None))).all()
        for market in prop_markets:
            raw_data = market.raw_data or {}
            if raw_data.get("copilot_market_family") != "player_prop":
                continue
            if market.event_id:
                mapped_prop_markets += 1

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

        parlay_recommendations = db.scalars(select(ParlayRecommendation)).all()
        for parlay in parlay_recommendations:
            parlay_watchlist_by_scope[parlay.sport_scope] = parlay_watchlist_by_scope.get(parlay.sport_scope, 0) + 1
            leg_key = str(parlay.leg_count)
            parlay_watchlist_by_leg_count[leg_key] = parlay_watchlist_by_leg_count.get(leg_key, 0) + 1

        run.details = {
            "sports_requested": list(sports or get_settings().enabled_sports),
            "sports_records_ingested": sports_summary["sports_records_ingested"],
            "sports_fetch_errors": sports_summary["sports_fetch_errors"],
            "total_kalshi_markets_seen": kalshi_summary["total_kalshi_markets_seen"],
            "supported_markets_kept": kalshi_summary["processed"],
            "supported_nba_props_seen": kalshi_summary["supported_nba_props_seen"],
            "supported_mlb_props_seen": kalshi_summary["supported_mlb_props_seen"],
            "unsupported_prop_category_counts": kalshi_summary["unsupported_prop_category_counts"],
            "mapped_markets": mapped_count,
            "mapped_prop_markets": mapped_prop_markets,
            "recommendations_emitted": recommendation_count,
            "predictions_captured": prediction_count,
            "parlay_recommendations_emitted": parlay_recommendation_count,
            "parlay_predictions_captured": parlay_prediction_count,
            "prediction_settlement_updated": single_settlement_summary["updated"],
            "parlay_prediction_settlement_updated": parlay_settlement_summary["updated"],
            "prediction_outcomes": {
                "won": single_settlement_summary["won"],
                "lost": single_settlement_summary["lost"],
                "push": single_settlement_summary["push"],
                "cancelled": single_settlement_summary["cancelled"],
                "pending": single_settlement_summary["pending"],
                "unresolved": single_settlement_summary["unresolved"],
                "errors": single_settlement_summary["errors"],
            },
            "parlay_prediction_outcomes": {
                "won": parlay_settlement_summary["won"],
                "lost": parlay_settlement_summary["lost"],
                "push": parlay_settlement_summary["push"],
                "cancelled": parlay_settlement_summary["cancelled"],
                "pending": parlay_settlement_summary["pending"],
                "unresolved": parlay_settlement_summary["unresolved"],
                "errors": parlay_settlement_summary["errors"],
            },
            "watchlist_counts_by_sport": watchlist_by_sport,
            "watchlist_counts_by_prop_category": watchlist_by_prop_category,
            "parlay_watchlist_counts_by_scope": parlay_watchlist_by_scope,
            "parlay_watchlist_counts_by_leg_count": parlay_watchlist_by_leg_count,
        }
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
