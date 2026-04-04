from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import (
    DemoOrder,
    Event,
    EventParticipant,
    Market,
    MarketSnapshot,
    PaperPosition,
    ParlayPrediction,
    ParlayRecommendation,
    Prediction,
    Recommendation,
    Run,
    SignalSnapshot,
    Sport,
)
from app.schemas import (
    DemoOrderCreate,
    DemoOrderRead,
    EventParticipantRead,
    EventRead,
    HealthResponse,
    JobRefreshResponse,
    MarketDetailRead,
    MarketHistoryRead,
    MarketListRead,
    MarketSnapshotRead,
    ModelFamilyReadinessRead,
    ModelReadinessSummaryRead,
    PaperPositionCreate,
    PaperPositionExit,
    PaperPositionRead,
    ParlayPredictionRead,
    ParlayPredictionSummaryRead,
    ParlayRecommendationRead,
    PositionsRead,
    PredictionRead,
    PredictionSettlementResponse,
    PredictionSummaryRead,
    RecommendationRead,
    RunDetailRead,
    RunRead,
    RunSummaryCounts,
    SignalSnapshotRead,
    SportRead,
    StatsQueryRead,
    StatsQueryRequest,
    WatchlistDiagnosticsRead,
    WatchlistCoverageRowRead,
)
from app.services.market_history import build_market_history
from app.services.ml.readiness import build_model_readiness_detail, build_model_readiness_summary
from app.services.orders import cancel_demo_order, close_paper_position, create_demo_order, create_paper_position
from app.services.parlays import settle_parlay_predictions
from app.services.predictions import settle_predictions
from app.services.scheduler import get_refresh_runtime_state, queue_prop_refresh_if_due, run_refresh_cycle_now
from app.services.stats_query import StatsQueryService
from app.services.watchlist_coverage import (
    current_watchlist_markets,
    latest_prediction_by_market_id,
    latest_recommendation_by_market_id,
    latest_snapshot_by_market_id,
)

router = APIRouter()


def get_stats_query_service() -> StatsQueryService:
    return StatsQueryService()


def _serialize_event(event: Event) -> EventRead:
    participants = [
        EventParticipantRead(
            participant_id=entry.participant_id,
            display_name=entry.participant.display_name,
            role=entry.role,
            is_home=entry.is_home,
            score=entry.score,
            result=entry.result,
        )
        for entry in event.participants
    ]
    return EventRead(
        id=event.id,
        external_id=event.external_id,
        sport_key=event.sport_key,
        name=event.name,
        status=event.status,
        starts_at=event.starts_at,
        completed_at=event.completed_at,
        participants=participants,
        raw_data=event.raw_data or {},
    )


def _market_metadata_fields(market: Market | None) -> dict[str, str | float | None]:
    raw_data = (market.raw_data or {}) if market else {}
    return {
        "market_family": raw_data.get("copilot_market_family"),
        "market_kind": raw_data.get("copilot_market_kind"),
        "stat_key": raw_data.get("copilot_stat_key"),
        "threshold": raw_data.get("copilot_threshold"),
        "direction": raw_data.get("copilot_direction"),
        "subject_name": raw_data.get("copilot_subject_name"),
        "subject_team": raw_data.get("copilot_subject_team"),
    }


def _serialize_recommendation(item: Recommendation, market: Market, event_name: str) -> RecommendationRead:
    diagnostics = dict(item.scoring_diagnostics or {})
    return RecommendationRead(
        id=item.id,
        ticker=market.ticker,
        sport_key=market.sport_key,
        market_title=market.title,
        event_name=event_name,
        side=item.side,
        action=item.action,
        suggested_price=item.suggested_price,
        edge=item.edge,
        confidence=item.confidence,
        selected_side_probability=diagnostics.get("selected_side_probability"),
        source_type=diagnostics.get("source_type") or (market.raw_data or {}).get("copilot_source_type"),
        source_market_ticker=diagnostics.get("source_market_ticker") or (market.raw_data or {}).get("copilot_source_market_ticker"),
        source_market_title=diagnostics.get("source_market_title") or (market.raw_data or {}).get("copilot_source_market_title"),
        display_market_title=diagnostics.get("display_market_title") or (market.raw_data or {}).get("copilot_display_market_title") or market.title,
        source_badge_label=diagnostics.get("source_badge_label") or (market.raw_data or {}).get("copilot_source_badge_label"),
        context_coverage_score=diagnostics.get("context_coverage_score"),
        quality_tier=diagnostics.get("quality_tier"),
        model_name=item.model_name,
        model_version=item.model_version,
        calibration_version=item.calibration_version,
        feature_set_version=item.feature_set_version,
        invalidation=item.invalidation,
        rationale=item.rationale,
        captured_at=item.captured_at,
        **_market_metadata_fields(market),
    )


def _run_summary_counts(details: dict | None) -> RunSummaryCounts:
    payload = details or {}
    return RunSummaryCounts(
        sports_records_ingested=payload.get("sports_records_ingested") or {},
        total_kalshi_markets_seen=int(payload.get("total_kalshi_markets_seen") or 0),
        supported_markets_kept=int(payload.get("supported_markets_kept") or 0),
        supported_nba_props_seen=int(payload.get("supported_nba_props_seen") or 0),
        supported_mlb_props_seen=int(payload.get("supported_mlb_props_seen") or 0),
        mapped_markets=int(payload.get("mapped_markets") or 0),
        mapped_prop_markets=int(payload.get("mapped_prop_markets") or 0),
        recommendations_emitted=int(payload.get("recommendations_emitted") or 0),
        predictions_captured=int(payload.get("predictions_captured") or 0),
        parlay_recommendations_emitted=int(payload.get("parlay_recommendations_emitted") or 0),
        parlay_predictions_captured=int(payload.get("parlay_predictions_captured") or 0),
        prediction_settlement_updated=int(payload.get("prediction_settlement_updated") or 0),
        parlay_prediction_settlement_updated=int(payload.get("parlay_prediction_settlement_updated") or 0),
        prediction_outcomes=payload.get("prediction_outcomes") or {},
        parlay_prediction_outcomes=payload.get("parlay_prediction_outcomes") or {},
        unsupported_prop_category_counts=payload.get("unsupported_prop_category_counts") or {},
        heuristic_longshots_suppressed=int(payload.get("heuristic_longshots_suppressed") or 0),
        inverse_winner_duplicates_collapsed=int(payload.get("inverse_winner_duplicates_collapsed") or 0),
        combo_prop_candidates_emitted=int(payload.get("combo_prop_candidates_emitted") or 0),
        combo_prop_candidates_suppressed=int(payload.get("combo_prop_candidates_suppressed") or 0),
        critical_context_suppressed=int(payload.get("critical_context_suppressed") or 0),
        quality_tier_counts=payload.get("quality_tier_counts") or {},
        prop_subjects_warmed=int(payload.get("prop_subjects_warmed") or 0),
        player_search_cache_hits=int(payload.get("player_search_cache_hits") or 0),
        player_search_cache_misses=int(payload.get("player_search_cache_misses") or 0),
        gamelog_cache_hits=int(payload.get("gamelog_cache_hits") or 0),
        gamelog_cache_misses=int(payload.get("gamelog_cache_misses") or 0),
        stale_gamelog_fallbacks=int(payload.get("stale_gamelog_fallbacks") or 0),
        combo_prop_legs_discovered=int(payload.get("combo_prop_legs_discovered") or 0),
        combo_prop_legs_refreshed=int(payload.get("combo_prop_legs_refreshed") or 0),
        watchlist_counts_by_sport=payload.get("watchlist_counts_by_sport") or {},
        watchlist_counts_by_prop_category=payload.get("watchlist_counts_by_prop_category") or {},
        parlay_watchlist_counts_by_scope=payload.get("parlay_watchlist_counts_by_scope") or {},
        parlay_watchlist_counts_by_leg_count=payload.get("parlay_watchlist_counts_by_leg_count") or {},
    )


def _serialize_signal(item: SignalSnapshot) -> SignalSnapshotRead:
    return SignalSnapshotRead(
        captured_at=item.captured_at,
        model_name=item.model_name,
        model_version=item.model_version,
        calibration_version=item.calibration_version,
        feature_set_version=item.feature_set_version,
        confidence=item.confidence,
        fair_yes_price=item.fair_yes_price,
        fair_no_price=item.fair_no_price,
        edge=item.edge,
        reasons=list(item.reasons or []),
        features=dict(item.features or {}),
        scoring_diagnostics=dict(item.scoring_diagnostics or {}),
    )


def _serialize_prediction(item: Prediction) -> PredictionRead:
    diagnostics = dict(item.scoring_diagnostics or {})
    return PredictionRead(
        id=item.id,
        run_id=item.run_id,
        event_id=item.event_id,
        market_id=item.market_id,
        ticker=item.ticker,
        sport_key=item.sport_key,
        event_name=item.event_name,
        market_title=item.market_title,
        market_family=item.market_family,
        market_kind=item.market_kind,
        stat_key=item.stat_key,
        threshold=item.threshold,
        subject_name=item.subject_name,
        subject_team=item.subject_team,
        capture_scope=item.capture_scope or "recommendation",
        side=item.side,
        action=item.action,
        suggested_price=item.suggested_price,
        fair_yes_price=item.fair_yes_price,
        fair_no_price=item.fair_no_price,
        edge=item.edge,
        confidence=item.confidence,
        selected_side_probability=diagnostics.get("selected_side_probability"),
        source_type=diagnostics.get("source_type"),
        source_market_ticker=diagnostics.get("source_market_ticker"),
        source_market_title=diagnostics.get("source_market_title"),
        display_market_title=diagnostics.get("display_market_title") or item.market_title,
        source_badge_label=diagnostics.get("source_badge_label"),
        context_coverage_score=diagnostics.get("context_coverage_score"),
        quality_tier=diagnostics.get("quality_tier"),
        model_name=item.model_name,
        model_version=item.model_version,
        calibration_version=item.calibration_version,
        feature_set_version=item.feature_set_version,
        invalidation=item.invalidation,
        rationale=item.rationale,
        reasons=list(item.reasons or []),
        features=dict(item.features or {}),
        market_status_at_capture=item.market_status_at_capture,
        settlement_status=item.settlement_status,
        prediction_outcome=item.prediction_outcome,
        market_result=item.market_result,
        winning_side=item.winning_side,
        settlement_value=item.settlement_value,
        settled_at=item.settled_at,
        realized_pnl=item.realized_pnl,
        settlement_source=item.settlement_source,
        settlement_notes=item.settlement_notes,
        captured_at=item.captured_at,
    )


def _serialize_watchlist_coverage_row(
    market: Market,
    *,
    latest_snapshot: MarketSnapshot | None,
    latest_recommendation: Recommendation | None,
    latest_prediction: Prediction | None,
) -> WatchlistCoverageRowRead:
    prediction_payload = _serialize_prediction(latest_prediction) if latest_prediction else None
    recommendation_payload = (
        _serialize_recommendation(
            latest_recommendation,
            market,
            market.event.name if market.event else market.title,
        )
        if latest_recommendation and market.event
        else None
    )
    return WatchlistCoverageRowRead(
        ticker=market.ticker,
        event_id=market.event_id,
        event_name=market.event.name if market.event else None,
        event_status=market.event.status if market.event else None,
        starts_at=market.event.starts_at if market.event else None,
        sport_key=market.sport_key,
        market_title=market.title,
        coverage_status=(
            "recommendation"
            if latest_recommendation is not None
            else "prediction"
            if latest_prediction is not None
            else "market"
        ),
        prop_context_stale=bool((latest_prediction.features or {}).get("uses_stale_prop_context")) if latest_prediction else False,
        latest_snapshot=MarketSnapshotRead.model_validate(latest_snapshot) if latest_snapshot else None,
        latest_recommendation=recommendation_payload,
        latest_prediction=prediction_payload,
        **_market_metadata_fields(market),
    )


def _serialize_run(run: Run) -> RunRead:
    return RunRead(
        id=run.id,
        kind=run.kind,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        records_processed=run.records_processed,
        error_message=run.error_message,
        summary_counts=_run_summary_counts(run.details),
    )


def _prediction_stmt(
    *,
    sport: str | None = None,
    market_family: str | None = None,
    stat_key: str | None = None,
    outcome: str | None = None,
    captured_from: date | None = None,
    captured_to: date | None = None,
):
    stmt = select(Prediction).order_by(Prediction.captured_at.desc(), Prediction.id.desc())
    if sport:
        stmt = stmt.where(Prediction.sport_key == sport.upper())
    if market_family:
        stmt = stmt.where(Prediction.market_family == market_family)
    if stat_key:
        stmt = stmt.where(Prediction.stat_key == stat_key)
    if outcome:
        stmt = stmt.where(Prediction.prediction_outcome == outcome.lower())
    if captured_from:
        start = datetime.combine(captured_from, datetime.min.time(), tzinfo=timezone.utc)
        stmt = stmt.where(Prediction.captured_at >= start)
    if captured_to:
        end = datetime.combine(captured_to, datetime.max.time(), tzinfo=timezone.utc)
        stmt = stmt.where(Prediction.captured_at <= end)
    return stmt


def _normalized_parlay_sport_scope(value: str | None) -> str:
    scope = (value or "all").strip().lower()
    if scope not in {"all", "nba", "mlb"}:
        raise HTTPException(status_code=400, detail="sport_scope must be one of all, NBA, or MLB")
    return scope


def _validated_leg_count(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 2 or value > 6:
        raise HTTPException(status_code=400, detail="leg_count must be between 2 and 6")
    return value


def _parlay_recommendation_stmt(*, sport_scope: str, leg_count: int | None):
    stmt = (
        select(ParlayRecommendation)
        .options(selectinload(ParlayRecommendation.legs))
        .order_by(
            ParlayRecommendation.selection_score.desc().nullslast(),
            ParlayRecommendation.edge.desc(),
            ParlayRecommendation.confidence.desc(),
            ParlayRecommendation.captured_at.desc(),
        )
    )
    if sport_scope == "nba":
        stmt = stmt.where(ParlayRecommendation.sport_scope == "NBA")
    elif sport_scope == "mlb":
        stmt = stmt.where(ParlayRecommendation.sport_scope == "MLB")
    if leg_count is not None:
        stmt = stmt.where(ParlayRecommendation.leg_count == leg_count)
    return stmt


def _parlay_prediction_stmt(*, sport_scope: str, leg_count: int | None):
    stmt = (
        select(ParlayPrediction)
        .options(selectinload(ParlayPrediction.legs))
        .order_by(ParlayPrediction.captured_at.desc(), ParlayPrediction.id.desc())
    )
    if sport_scope == "nba":
        stmt = stmt.where(ParlayPrediction.sport_scope == "NBA")
    elif sport_scope == "mlb":
        stmt = stmt.where(ParlayPrediction.sport_scope == "MLB")
    if leg_count is not None:
        stmt = stmt.where(ParlayPrediction.leg_count == leg_count)
    return stmt


def _build_prediction_summary(predictions: list[Prediction]) -> PredictionSummaryRead:
    by_sport: dict[str, int] = {}
    by_market_family: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    settled_predictions = 0
    pending_predictions = 0
    unresolved_predictions = 0
    won_predictions = 0
    lost_predictions = 0
    push_predictions = 0
    cancelled_predictions = 0

    for prediction in predictions:
        sport_key = prediction.sport_key or "UNKNOWN"
        by_sport[sport_key] = by_sport.get(sport_key, 0) + 1
        family = prediction.market_family or "unknown"
        by_market_family[family] = by_market_family.get(family, 0) + 1

        outcome = prediction.prediction_outcome or "pending"
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        if outcome == "won":
            won_predictions += 1
            settled_predictions += 1
        elif outcome == "lost":
            lost_predictions += 1
            settled_predictions += 1
        elif outcome == "push":
            push_predictions += 1
            settled_predictions += 1
        elif outcome == "cancelled":
            cancelled_predictions += 1
            settled_predictions += 1
        elif outcome == "unresolved":
            unresolved_predictions += 1
        else:
            pending_predictions += 1

    win_loss_total = won_predictions + lost_predictions
    realized = [prediction.realized_pnl for prediction in predictions if prediction.realized_pnl is not None]
    edges = [prediction.edge for prediction in predictions]
    confidences = [prediction.confidence for prediction in predictions]
    return PredictionSummaryRead(
        total_predictions=len(predictions),
        settled_predictions=settled_predictions,
        pending_predictions=pending_predictions,
        unresolved_predictions=unresolved_predictions,
        won_predictions=won_predictions,
        lost_predictions=lost_predictions,
        push_predictions=push_predictions,
        cancelled_predictions=cancelled_predictions,
        win_rate=round(won_predictions / win_loss_total, 4) if win_loss_total else None,
        loss_rate=round(lost_predictions / win_loss_total, 4) if win_loss_total else None,
        average_edge=round(sum(edges) / len(edges), 4) if edges else None,
        average_confidence=round(sum(confidences) / len(confidences), 4) if confidences else None,
        average_realized_pnl=round(sum(realized) / len(realized), 4) if realized else None,
        by_sport=by_sport,
        by_market_family=by_market_family,
        by_outcome=by_outcome,
    )


def _build_parlay_prediction_summary(predictions: list[ParlayPrediction]) -> ParlayPredictionSummaryRead:
    by_sport_scope: dict[str, int] = {}
    by_leg_count: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    settled_predictions = 0
    pending_predictions = 0
    unresolved_predictions = 0
    won_predictions = 0
    lost_predictions = 0
    push_predictions = 0
    cancelled_predictions = 0

    for prediction in predictions:
        scope = prediction.sport_scope or "MIXED"
        by_sport_scope[scope] = by_sport_scope.get(scope, 0) + 1
        leg_key = str(prediction.leg_count)
        by_leg_count[leg_key] = by_leg_count.get(leg_key, 0) + 1
        outcome = prediction.prediction_outcome or "pending"
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        if outcome == "won":
            won_predictions += 1
            settled_predictions += 1
        elif outcome == "lost":
            lost_predictions += 1
            settled_predictions += 1
        elif outcome == "push":
            push_predictions += 1
            settled_predictions += 1
        elif outcome == "cancelled":
            cancelled_predictions += 1
            settled_predictions += 1
        elif outcome == "unresolved":
            unresolved_predictions += 1
        else:
            pending_predictions += 1

    win_loss_total = won_predictions + lost_predictions
    realized = [prediction.realized_pnl for prediction in predictions if prediction.realized_pnl is not None]
    edges = [prediction.edge for prediction in predictions]
    confidences = [prediction.confidence for prediction in predictions]
    return ParlayPredictionSummaryRead(
        total_predictions=len(predictions),
        settled_predictions=settled_predictions,
        pending_predictions=pending_predictions,
        unresolved_predictions=unresolved_predictions,
        won_predictions=won_predictions,
        lost_predictions=lost_predictions,
        push_predictions=push_predictions,
        cancelled_predictions=cancelled_predictions,
        win_rate=round(won_predictions / win_loss_total, 4) if win_loss_total else None,
        loss_rate=round(lost_predictions / win_loss_total, 4) if win_loss_total else None,
        average_edge=round(sum(edges) / len(edges), 4) if edges else None,
        average_confidence=round(sum(confidences) / len(confidences), 4) if confidences else None,
        average_realized_pnl=round(sum(realized) / len(realized), 4) if realized else None,
        by_sport_scope=by_sport_scope,
        by_leg_count=by_leg_count,
        by_outcome=by_outcome,
    )


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


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    runtime = get_refresh_runtime_state()
    return HealthResponse(
        status="ok",
        environment=settings.environment,
        scheduler_enabled=settings.scheduler_enabled,
        refresh_status=str(runtime["refresh_status"]),
        refresh_reason=str(runtime["refresh_reason"]),
        last_successful_refresh_at=runtime["last_successful_refresh_at"],
        data_stale=bool(runtime["data_stale"]),
        refresh_error_message=runtime["refresh_error_message"],
        prop_refresh_status=str(runtime["prop_refresh_status"]),
        prop_refresh_reason=str(runtime["prop_refresh_reason"]),
        last_prop_refresh_at=runtime["last_prop_refresh_at"],
        prop_data_stale=bool(runtime["prop_data_stale"]),
        prop_refresh_error_message=runtime["prop_refresh_error_message"],
    )


@router.get("/sports", response_model=list[SportRead])
def list_sports(db: Session = Depends(get_db)) -> list[SportRead]:
    return [SportRead.model_validate(item) for item in db.scalars(select(Sport).order_by(Sport.key)).all()]


@router.get("/events", response_model=list[EventRead])
def list_events(
    sport: str | None = None,
    day: date | None = None,
    db: Session = Depends(get_db),
) -> list[EventRead]:
    stmt = select(Event).options(selectinload(Event.participants).joinedload(EventParticipant.participant)).order_by(Event.starts_at)
    if sport:
        stmt = stmt.where(Event.sport_key == sport.upper())
    if day:
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        stmt = stmt.where(Event.starts_at >= start, Event.starts_at <= end)
    return [_serialize_event(item) for item in db.scalars(stmt).all()]


@router.get("/watchlist/diagnostics", response_model=WatchlistDiagnosticsRead)
def get_watchlist_diagnostics(db: Session = Depends(get_db)) -> WatchlistDiagnosticsRead:
    settings = get_settings()
    runtime = get_refresh_runtime_state()
    latest_refresh_run = db.scalar(
        select(Run)
        .where(Run.kind == "refresh")
        .order_by(Run.started_at.desc(), Run.id.desc())
        .limit(1)
    )
    serialized_run = _serialize_run(latest_refresh_run) if latest_refresh_run else None
    summary_counts = serialized_run.summary_counts if serialized_run else None
    current_recommendation_count = db.scalar(select(func.count()).select_from(Recommendation)) or 0

    return WatchlistDiagnosticsRead(
        status="ok",
        environment=settings.environment,
        scheduler_enabled=settings.scheduler_enabled,
        refresh_status=str(runtime["refresh_status"]),
        refresh_reason=str(runtime["refresh_reason"]),
        last_successful_refresh_at=runtime["last_successful_refresh_at"],
        data_stale=bool(runtime["data_stale"]),
        refresh_error_message=runtime["refresh_error_message"],
        prop_refresh_status=str(runtime["prop_refresh_status"]),
        prop_refresh_reason=str(runtime["prop_refresh_reason"]),
        last_prop_refresh_at=runtime["last_prop_refresh_at"],
        prop_data_stale=bool(runtime["prop_data_stale"]),
        prop_refresh_error_message=runtime["prop_refresh_error_message"],
        latest_refresh_run=serialized_run,
        latest_refresh_succeeded=(latest_refresh_run.status == "completed") if latest_refresh_run else None,
        latest_supported_markets_kept=summary_counts.supported_markets_kept if summary_counts else 0,
        latest_recommendations_emitted=summary_counts.recommendations_emitted if summary_counts else 0,
        latest_watchlist_counts_by_sport=summary_counts.watchlist_counts_by_sport if summary_counts else {},
        current_recommendation_count=int(current_recommendation_count),
        watchlist_min_edge=settings.watchlist_min_edge,
        watchlist_min_confidence=settings.watchlist_min_confidence,
    )


@router.get("/watchlist", response_model=list[RecommendationRead])
def get_watchlist(sport: str | None = None, limit: int = 25, db: Session = Depends(get_db)) -> list[RecommendationRead]:
    stmt = (
        select(Recommendation)
        .options(joinedload(Recommendation.market), joinedload(Recommendation.event))
        .order_by(
            Recommendation.selection_score.desc().nullslast(),
            Recommendation.edge.desc(),
            Recommendation.confidence.desc(),
        )
        .limit(limit)
    )
    if sport:
        stmt = stmt.join(Market, Recommendation.market_id == Market.id).where(Market.sport_key == sport.upper())
    recommendations = db.scalars(stmt).all()
    return [
        _serialize_recommendation(item, item.market, item.event.name)
        for item in recommendations
    ]


@router.get("/watchlist/coverage", response_model=list[WatchlistCoverageRowRead])
def get_watchlist_coverage(
    sport: str | None = None,
    limit: int = 250,
    db: Session = Depends(get_db),
) -> list[WatchlistCoverageRowRead]:
    normalized_sport = sport.upper() if sport else None
    markets = current_watchlist_markets(db, sport=normalized_sport)
    limited_markets = markets[: max(limit, 1)]
    market_ids = [market.id for market in limited_markets]
    latest_snapshots = latest_snapshot_by_market_id(db, market_ids)
    latest_recommendations = latest_recommendation_by_market_id(db, market_ids)
    latest_predictions = latest_prediction_by_market_id(db, market_ids)
    return [
        _serialize_watchlist_coverage_row(
            market,
            latest_snapshot=latest_snapshots.get(market.id),
            latest_recommendation=latest_recommendations.get(market.id),
            latest_prediction=latest_predictions.get(market.id),
        )
        for market in limited_markets
    ]


@router.get("/parlays/watchlist", response_model=list[ParlayRecommendationRead])
def get_parlay_watchlist(
    sport_scope: str = "all",
    leg_count: int | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[ParlayRecommendationRead]:
    scope = _normalized_parlay_sport_scope(sport_scope)
    validated_leg_count = _validated_leg_count(leg_count)
    items = db.scalars(_parlay_recommendation_stmt(sport_scope=scope, leg_count=validated_leg_count).limit(limit)).all()
    return [ParlayRecommendationRead.model_validate(item) for item in items]


@router.get("/models/readiness", response_model=ModelReadinessSummaryRead)
def model_readiness_summary(db: Session = Depends(get_db)) -> ModelReadinessSummaryRead:
    return ModelReadinessSummaryRead.model_validate(build_model_readiness_summary(db))


@router.get("/models/readiness/{family_key}", response_model=ModelFamilyReadinessRead)
def model_readiness_detail(family_key: str, db: Session = Depends(get_db)) -> ModelFamilyReadinessRead:
    payload = build_model_readiness_detail(db, family_key)
    if payload is None:
        raise HTTPException(status_code=404, detail="Unknown model family")
    return ModelFamilyReadinessRead.model_validate(payload)


@router.get("/predictions", response_model=list[PredictionRead])
def list_predictions(
    sport: str | None = None,
    market_family: str | None = None,
    stat_key: str | None = None,
    outcome: str | None = None,
    captured_from: date | None = None,
    captured_to: date | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[PredictionRead]:
    stmt = _prediction_stmt(
        sport=sport,
        market_family=market_family,
        stat_key=stat_key,
        outcome=outcome,
        captured_from=captured_from,
        captured_to=captured_to,
    ).limit(limit)
    predictions = db.scalars(stmt).all()
    return [_serialize_prediction(item) for item in predictions]


@router.get("/predictions/summary", response_model=PredictionSummaryRead)
def prediction_summary(
    sport: str | None = None,
    market_family: str | None = None,
    stat_key: str | None = None,
    outcome: str | None = None,
    captured_from: date | None = None,
    captured_to: date | None = None,
    db: Session = Depends(get_db),
) -> PredictionSummaryRead:
    predictions = db.scalars(
        _prediction_stmt(
            sport=sport,
            market_family=market_family,
            stat_key=stat_key,
            outcome=outcome,
            captured_from=captured_from,
            captured_to=captured_to,
        )
    ).all()
    return _build_prediction_summary(predictions)


@router.get("/parlays/predictions", response_model=list[ParlayPredictionRead])
def list_parlay_predictions(
    sport_scope: str = "all",
    leg_count: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[ParlayPredictionRead]:
    scope = _normalized_parlay_sport_scope(sport_scope)
    validated_leg_count = _validated_leg_count(leg_count)
    predictions = db.scalars(_parlay_prediction_stmt(sport_scope=scope, leg_count=validated_leg_count).limit(limit)).all()
    return [ParlayPredictionRead.model_validate(item) for item in predictions]


@router.get("/parlays/predictions/summary", response_model=ParlayPredictionSummaryRead)
def parlay_prediction_summary(
    sport_scope: str = "all",
    leg_count: int | None = None,
    db: Session = Depends(get_db),
) -> ParlayPredictionSummaryRead:
    scope = _normalized_parlay_sport_scope(sport_scope)
    validated_leg_count = _validated_leg_count(leg_count)
    predictions = db.scalars(_parlay_prediction_stmt(sport_scope=scope, leg_count=validated_leg_count)).all()
    return _build_parlay_prediction_summary(predictions)


@router.get("/positions", response_model=PositionsRead)
def get_positions(db: Session = Depends(get_db)) -> PositionsRead:
    paper_positions = db.scalars(select(PaperPosition).order_by(PaperPosition.opened_at.desc())).all()
    demo_orders = db.scalars(select(DemoOrder).order_by(DemoOrder.id.desc())).all()
    return PositionsRead(
        paper_positions=[PaperPositionRead.model_validate(item) for item in paper_positions],
        demo_orders=[DemoOrderRead.model_validate(item) for item in demo_orders],
    )


@router.get("/markets", response_model=list[MarketListRead])
def list_markets(
    sport: str | None = None,
    family: str | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[MarketListRead]:
    stmt = (
        select(Market)
        .options(joinedload(Market.event))
        .order_by(Market.close_time.desc().nullslast(), Market.id.desc())
        .limit(max(limit * 6, limit))
    )
    if sport:
        stmt = stmt.where(Market.sport_key == sport.upper())
    if family:
        stmt = stmt.where(Market.raw_data["copilot_market_family"].as_string() == family)
    if status:
        stmt = stmt.where(Market.status == status)
    if search:
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            Market.ticker.ilike(term)
            | Market.title.ilike(term)
            | Market.subtitle.ilike(term)
        )

    markets = db.scalars(stmt).all()
    payload: list[MarketListRead] = []
    for market in markets:
        raw_data = market.raw_data or {}
        if not raw_data.get("copilot_market_kind"):
            continue
        latest_snapshot = db.scalars(
            select(MarketSnapshot)
            .where(MarketSnapshot.market_id == market.id)
            .order_by(MarketSnapshot.captured_at.desc())
            .limit(1)
        ).first()
        latest_recommendation = db.scalars(
            select(Recommendation)
            .options(joinedload(Recommendation.event), joinedload(Recommendation.market))
            .where(Recommendation.market_id == market.id)
            .order_by(Recommendation.captured_at.desc())
            .limit(1)
        ).first()
        event_name = market.event.name if market.event else None
        payload.append(
            MarketListRead(
                ticker=market.ticker,
                title=market.title,
                subtitle=market.subtitle,
                sport_key=market.sport_key,
                status=market.status,
                close_time=market.close_time,
                event_name=event_name,
                latest_snapshot=MarketSnapshotRead.model_validate(latest_snapshot) if latest_snapshot else None,
                latest_recommendation=(
                    _serialize_recommendation(
                        latest_recommendation,
                        market,
                        event_name or market.title,
                    )
                    if latest_recommendation
                    else None
                ),
                **_market_metadata_fields(market),
            )
        )
        if len(payload) >= limit:
            break
    return payload


@router.get("/markets/{ticker}", response_model=MarketDetailRead)
def get_market_detail(ticker: str, db: Session = Depends(get_db)) -> MarketDetailRead:
    market = db.scalar(select(Market).options(joinedload(Market.event)).where(Market.ticker == ticker))
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")

    latest_snapshot = db.scalars(
        select(MarketSnapshot).where(MarketSnapshot.market_id == market.id).order_by(MarketSnapshot.captured_at.desc()).limit(1)
    ).first()
    latest_signal = db.scalars(
        select(SignalSnapshot).where(SignalSnapshot.market_id == market.id).order_by(SignalSnapshot.captured_at.desc()).limit(1)
    ).first()
    recommendations = db.scalars(
        select(Recommendation).where(Recommendation.market_id == market.id).order_by(Recommendation.captured_at.desc()).limit(10)
    ).all()

    event_payload = None
    if market.event_id:
        event = db.scalar(
            select(Event).options(selectinload(Event.participants).joinedload(EventParticipant.participant)).where(Event.id == market.event_id)
        )
        if event:
            event_payload = _serialize_event(event)

    return MarketDetailRead(
        ticker=market.ticker,
        title=market.title,
        subtitle=market.subtitle,
        sport_key=market.sport_key,
        **_market_metadata_fields(market),
        status=market.status,
        close_time=market.close_time,
        event=event_payload,
        latest_snapshot=MarketSnapshotRead.model_validate(latest_snapshot) if latest_snapshot else None,
        latest_signal=_serialize_signal(latest_signal) if latest_signal else None,
        recommendations=[
            _serialize_recommendation(item, market, event_payload.name if event_payload else market.title)
            for item in recommendations
        ],
    )


@router.get("/markets/{ticker}/history", response_model=MarketHistoryRead)
def get_market_history(ticker: str, range: str = "1D", db: Session = Depends(get_db)) -> MarketHistoryRead:
    market = db.scalar(select(Market).where(Market.ticker == ticker))
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    try:
        history = build_market_history(db, market, range_key=range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MarketHistoryRead.model_validate(history)


@router.get("/runs", response_model=list[RunRead])
def list_runs(kind: str | None = None, status: str | None = None, limit: int = 20, db: Session = Depends(get_db)) -> list[RunRead]:
    stmt = select(Run)
    if kind:
        stmt = stmt.where(Run.kind == kind)
    if status:
        stmt = stmt.where(Run.status == status)
    stmt = stmt.order_by(Run.started_at.desc()).limit(limit)
    runs = db.scalars(stmt).all()
    return [_serialize_run(item) for item in runs]


@router.get("/runs/{run_id}", response_model=RunDetailRead)
def get_run_detail(run_id: int, db: Session = Depends(get_db)) -> RunDetailRead:
    run = db.scalar(select(Run).where(Run.id == run_id))
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    serialized = _serialize_run(run)
    return RunDetailRead(**serialized.model_dump(), details=run.details or {})


@router.post("/paper-positions", response_model=PaperPositionRead)
def open_paper_position(payload: PaperPositionCreate, db: Session = Depends(get_db)) -> PaperPositionRead:
    position = create_paper_position(db, payload)
    db.commit()
    db.refresh(position)
    return PaperPositionRead.model_validate(position)


@router.post("/paper-positions/{position_id}/exit", response_model=PaperPositionRead)
def exit_paper_position(position_id: int, payload: PaperPositionExit, db: Session = Depends(get_db)) -> PaperPositionRead:
    position = close_paper_position(db, position_id, payload)
    db.commit()
    db.refresh(position)
    return PaperPositionRead.model_validate(position)


@router.post("/demo-orders", response_model=DemoOrderRead)
def submit_demo_order(payload: DemoOrderCreate, db: Session = Depends(get_db)) -> DemoOrderRead:
    order = create_demo_order(db, payload)
    db.commit()
    db.refresh(order)
    return DemoOrderRead.model_validate(order)


@router.post("/demo-orders/{order_id}/cancel", response_model=DemoOrderRead)
def cancel_order(order_id: int, db: Session = Depends(get_db)) -> DemoOrderRead:
    order = cancel_demo_order(db, order_id)
    db.commit()
    db.refresh(order)
    return DemoOrderRead.model_validate(order)


@router.post("/jobs/refresh", response_model=JobRefreshResponse)
def refresh_jobs() -> JobRefreshResponse:
    run = run_refresh_cycle_now(reason="manual")
    if run is None:
        raise HTTPException(status_code=409, detail="Refresh already in progress")
    queued_prop_refresh = queue_prop_refresh_if_due(reason="manual")
    return JobRefreshResponse(
        run_id=run.run_id,
        status=run.status,
        records_processed=run.records_processed,
        queued_prop_refresh=queued_prop_refresh,
    )


@router.post("/jobs/settle-predictions", response_model=PredictionSettlementResponse)
def settle_prediction_job(db: Session = Depends(get_db)) -> PredictionSettlementResponse:
    single_summary = settle_predictions(db)
    parlay_summary = settle_parlay_predictions(db)
    db.commit()
    return PredictionSettlementResponse(**_merge_settlement_summaries(single_summary, parlay_summary))


@router.post("/stats/query", response_model=StatsQueryRead)
def query_stats(
    payload: StatsQueryRequest,
    service: StatsQueryService = Depends(get_stats_query_service),
) -> StatsQueryRead:
    try:
        result = service.query(payload.question, sport_key=payload.sport_key, season=payload.season)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StatsQueryRead.model_validate(result)
