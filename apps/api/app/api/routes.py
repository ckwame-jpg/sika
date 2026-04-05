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
    RefreshJob,
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
    RefreshJobRead,
    RecommendationRead,
    RunDetailRead,
    RunRead,
    RunSummaryCounts,
    SignalSnapshotRead,
    SportAvailabilityRead,
    SportRead,
    StatsQueryRead,
    StatsQueryRequest,
    TradeDeskEventRead,
    TradeDeskGameLineRead,
    TradeDeskPlayerPropRead,
    TradeDeskResponse,
    TradeDeskStatGroupRead,
    TradeDeskThresholdRead,
    WatchlistDiagnosticsRead,
    WatchlistCoverageRowRead,
)
from app.services.market_history import build_market_history
from app.services.ml.readiness import build_model_readiness_detail, build_model_readiness_summary
from app.services.orders import cancel_demo_order, close_paper_position, create_demo_order, create_paper_position
from app.services.parlays import settle_parlay_predictions
from app.services.predictions import settle_predictions
from app.services.refresh_jobs import enqueue_refresh_job, get_refresh_job
from app.services.scheduler import get_refresh_runtime_state
from app.services.stats_query import StatsQueryService
from app.services.watchlist_coverage import (
    CURRENT_WATCHLIST_SPORTS,
    current_watchlist_markets,
    latest_prediction_by_market_id,
    latest_recommendation_by_market_id,
    latest_snapshot_by_market_id,
)
from app.sports.base import alias_tokens

router = APIRouter()


def _serialize_refresh_job(item: RefreshJob | None) -> RefreshJobRead | None:
    if item is None:
        return None
    return RefreshJobRead(
        id=item.id,
        kind=item.kind,
        scope=item.scope,
        reason=item.reason,
        status=item.status,
        run_id=item.run_id,
        error_message=item.error_message,
        details=dict(item.details or {}),
        queued_at=item.queued_at,
        started_at=item.started_at,
        finished_at=item.finished_at,
    )


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


TERMINAL_EVENT_STATUSES = frozenset({"completed", "cancelled"})


def _visible_sports() -> list[str]:
    return [sport.upper() for sport in get_settings().enabled_sports if sport.upper() != "UFC"]


def _sport_order(sport_key: str | None) -> int:
    sport_map = {sport: index for index, sport in enumerate(_visible_sports())}
    return sport_map.get((sport_key or "").upper(), len(sport_map))


def _latest_successful_refresh_at(db: Session) -> datetime | None:
    return db.scalar(
        select(Run.finished_at)
        .where(Run.kind == "refresh", Run.status == "completed", Run.finished_at.is_not(None))
        .order_by(Run.finished_at.desc(), Run.id.desc())
        .limit(1)
    )


def _sport_availability_rows(db: Session) -> list[SportAvailabilityRead]:
    visible_sports = _visible_sports()
    now = datetime.now(timezone.utc)
    recent_window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    event_counts = {
        sport_key: int(count or 0)
        for sport_key, count in db.execute(
            select(Event.sport_key, func.count(Event.id))
            .where(
                Event.sport_key.in_(tuple(visible_sports)),
                Event.status.notin_(tuple(TERMINAL_EVENT_STATUSES)),
                Event.starts_at >= recent_window_start,
            )
            .group_by(Event.sport_key)
        ).all()
    }
    recommendation_counts = {
        sport_key: int(count or 0)
        for sport_key, count in db.execute(
            select(Market.sport_key, func.count(Recommendation.id))
            .join(Market, Recommendation.market_id == Market.id)
            .where(Market.sport_key.in_(tuple(visible_sports)))
            .group_by(Market.sport_key)
        ).all()
    }
    last_refresh_at = _latest_successful_refresh_at(db)
    rows: list[SportAvailabilityRead] = []
    for sport_key in visible_sports:
        rows.append(
            SportAvailabilityRead(
                sport_key=sport_key,
                availability_mode="live" if sport_key in CURRENT_WATCHLIST_SPORTS else "research_only",
                events_count=event_counts.get(sport_key, 0),
                recommendations_count=recommendation_counts.get(sport_key, 0),
                last_refresh_at=last_refresh_at,
            )
        )
    return rows


def _participant_token_score(lookup_text: str, display_name: str, short_name: str | None = None) -> float:
    left_tokens = alias_tokens(lookup_text)
    right_tokens = alias_tokens(display_name, short_name)
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    if not shared:
        return 0.0
    strong_shared = [token for token in shared if len(token) >= 4]
    return min(1.0, (len(shared) * 0.15) + (len(strong_shared) * 0.2))


def _trade_desk_market_matches_event(market: Market) -> bool:
    if market.event is None:
        return False
    raw_data = market.raw_data or {}
    if str(raw_data.get("copilot_market_family") or "") == "player_prop":
        return True
    lookup_text = " ".join(
        part
        for part in [
            market.title,
            market.subtitle or "",
            str(raw_data.get("copilot_source_market_title") or ""),
            market.event_ticker or "",
        ]
        if part
    )
    participant_matches = 0
    for entry in market.event.participants:
        participant = entry.participant
        if _participant_token_score(lookup_text, participant.display_name, participant.short_name) >= 0.15:
            participant_matches += 1
    return participant_matches >= 2


def _kalshi_market_url(ticker: str) -> str:
    return f"https://kalshi.com/markets/{ticker}"


def _game_line_projected_label(market: Market, recommendation: Recommendation) -> str | None:
    raw_data = market.raw_data or {}
    diagnostics = dict(recommendation.scoring_diagnostics or {})
    market_kind = str(raw_data.get("copilot_market_kind") or "")
    threshold = raw_data.get("copilot_threshold")
    direction = str(raw_data.get("copilot_direction") or "over").lower()
    subject_name = str(raw_data.get("copilot_subject_name") or "").strip()
    selected_side = recommendation.side.lower()

    if market_kind in {"game_winner", "first_five_winner"}:
        return str(diagnostics.get("selected_subject_name") or subject_name or "").strip() or None
    if market_kind == "spread":
        if not subject_name:
            return None
        if threshold is None:
            return subject_name
        if selected_side == "yes":
            return f"{subject_name} -{float(threshold):g}"
        return f"{subject_name} +{float(threshold):g}"
    if market_kind == "total":
        selected_direction = direction if selected_side == "yes" else ("under" if direction == "over" else "over")
        if threshold is None:
            return selected_direction.title()
        return f"{selected_direction.title()} {float(threshold):g}"
    return str(diagnostics.get("selected_subject_name") or "").strip() or None


def _game_line_display_label(market: Market, recommendation: Recommendation) -> str:
    raw_data = market.raw_data or {}
    market_kind = str(raw_data.get("copilot_market_kind") or "")
    if market_kind in {"game_winner", "first_five_winner"}:
        projected = _game_line_projected_label(market, recommendation)
        if projected:
            return f"{projected} to win"
    return str(
        dict(recommendation.scoring_diagnostics or {}).get("display_market_title")
        or raw_data.get("copilot_display_market_title")
        or market.title
    )


def _thresholds_are_monotonic(thresholds: list[TradeDeskThresholdRead]) -> bool:
    previous_probability: float | None = None
    for threshold in sorted(thresholds, key=lambda item: item.threshold):
        probability = float(threshold.probability_yes)
        if previous_probability is not None and probability > previous_probability + 1e-9:
            return False
        previous_probability = probability
    return True


def _serialize_recommendation(
    item: Recommendation,
    market: Market,
    event_name: str | None = None,
) -> RecommendationRead:
    diagnostics = dict(item.scoring_diagnostics or {})
    event = item.event
    return RecommendationRead(
        id=item.id,
        ticker=market.ticker,
        sport_key=market.sport_key,
        market_title=market.title,
        event_name=event_name or (event.name if event else market.title),
        starts_at=event.starts_at if event else None,
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
    stmt = select(Prediction)
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
    stmt = stmt.order_by(Prediction.captured_at.desc(), Prediction.id.desc())
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


def _aggregate_prediction_summary(
    db: Session,
    *,
    sport: str | None = None,
    market_family: str | None = None,
    stat_key: str | None = None,
    outcome: str | None = None,
    captured_from: date | None = None,
    captured_to: date | None = None,
) -> PredictionSummaryRead:
    base_stmt = _prediction_stmt(
        sport=sport,
        market_family=market_family,
        stat_key=stat_key,
        outcome=outcome,
        captured_from=captured_from,
        captured_to=captured_to,
    ).order_by(None)
    subquery = base_stmt.subquery()

    totals = db.execute(
        select(
            func.count(subquery.c.id),
            func.avg(subquery.c.edge),
            func.avg(subquery.c.confidence),
            func.avg(subquery.c.realized_pnl),
        )
    ).one()
    total_predictions = int(totals[0] or 0)

    by_outcome_rows = db.execute(
        select(subquery.c.prediction_outcome, func.count(subquery.c.id))
        .group_by(subquery.c.prediction_outcome)
    ).all()
    by_outcome = {str(key or "pending"): int(count or 0) for key, count in by_outcome_rows}

    by_sport_rows = db.execute(
        select(subquery.c.sport_key, func.count(subquery.c.id))
        .group_by(subquery.c.sport_key)
    ).all()
    by_sport = {str(key or "UNKNOWN"): int(count or 0) for key, count in by_sport_rows}

    by_market_family_rows = db.execute(
        select(subquery.c.market_family, func.count(subquery.c.id))
        .group_by(subquery.c.market_family)
    ).all()
    by_market_family = {str(key or "unknown"): int(count or 0) for key, count in by_market_family_rows}

    settled_predictions = sum(by_outcome.get(name, 0) for name in ("won", "lost", "push", "cancelled"))
    pending_predictions = by_outcome.get("pending", 0)
    unresolved_predictions = by_outcome.get("unresolved", 0)
    won_predictions = by_outcome.get("won", 0)
    lost_predictions = by_outcome.get("lost", 0)
    push_predictions = by_outcome.get("push", 0)
    cancelled_predictions = by_outcome.get("cancelled", 0)
    win_loss_total = won_predictions + lost_predictions

    return PredictionSummaryRead(
        total_predictions=total_predictions,
        settled_predictions=settled_predictions,
        pending_predictions=pending_predictions,
        unresolved_predictions=unresolved_predictions,
        won_predictions=won_predictions,
        lost_predictions=lost_predictions,
        push_predictions=push_predictions,
        cancelled_predictions=cancelled_predictions,
        win_rate=round(won_predictions / win_loss_total, 4) if win_loss_total else None,
        loss_rate=round(lost_predictions / win_loss_total, 4) if win_loss_total else None,
        average_edge=round(float(totals[1]), 4) if totals[1] is not None else None,
        average_confidence=round(float(totals[2]), 4) if totals[2] is not None else None,
        average_realized_pnl=round(float(totals[3]), 4) if totals[3] is not None else None,
        by_sport=by_sport,
        by_market_family=by_market_family,
        by_outcome=by_outcome,
    )


def _aggregate_parlay_prediction_summary(
    db: Session,
    *,
    sport_scope: str,
    leg_count: int | None,
) -> ParlayPredictionSummaryRead:
    subquery = _parlay_prediction_stmt(sport_scope=sport_scope, leg_count=leg_count).order_by(None).subquery()
    totals = db.execute(
        select(
            func.count(subquery.c.id),
            func.avg(subquery.c.edge),
            func.avg(subquery.c.confidence),
            func.avg(subquery.c.realized_pnl),
        )
    ).one()
    total_predictions = int(totals[0] or 0)

    by_outcome_rows = db.execute(
        select(subquery.c.prediction_outcome, func.count(subquery.c.id))
        .group_by(subquery.c.prediction_outcome)
    ).all()
    by_outcome = {str(key or "pending"): int(count or 0) for key, count in by_outcome_rows}

    by_scope_rows = db.execute(
        select(subquery.c.sport_scope, func.count(subquery.c.id))
        .group_by(subquery.c.sport_scope)
    ).all()
    by_sport_scope = {str(key or "MIXED"): int(count or 0) for key, count in by_scope_rows}

    by_leg_count_rows = db.execute(
        select(subquery.c.leg_count, func.count(subquery.c.id))
        .group_by(subquery.c.leg_count)
    ).all()
    by_leg_count = {str(key): int(count or 0) for key, count in by_leg_count_rows}

    settled_predictions = sum(by_outcome.get(name, 0) for name in ("won", "lost", "push", "cancelled"))
    pending_predictions = by_outcome.get("pending", 0)
    unresolved_predictions = by_outcome.get("unresolved", 0)
    won_predictions = by_outcome.get("won", 0)
    lost_predictions = by_outcome.get("lost", 0)
    push_predictions = by_outcome.get("push", 0)
    cancelled_predictions = by_outcome.get("cancelled", 0)
    win_loss_total = won_predictions + lost_predictions

    return ParlayPredictionSummaryRead(
        total_predictions=total_predictions,
        settled_predictions=settled_predictions,
        pending_predictions=pending_predictions,
        unresolved_predictions=unresolved_predictions,
        won_predictions=won_predictions,
        lost_predictions=lost_predictions,
        push_predictions=push_predictions,
        cancelled_predictions=cancelled_predictions,
        win_rate=round(won_predictions / win_loss_total, 4) if win_loss_total else None,
        loss_rate=round(lost_predictions / win_loss_total, 4) if win_loss_total else None,
        average_edge=round(float(totals[1]), 4) if totals[1] is not None else None,
        average_confidence=round(float(totals[2]), 4) if totals[2] is not None else None,
        average_realized_pnl=round(float(totals[3]), 4) if totals[3] is not None else None,
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
        active_refresh_job=RefreshJobRead.model_validate(runtime["active_refresh_job"]) if runtime["active_refresh_job"] else None,
        latest_refresh_job=RefreshJobRead.model_validate(runtime["latest_refresh_job"]) if runtime["latest_refresh_job"] else None,
        active_prop_refresh_job=RefreshJobRead.model_validate(runtime["active_prop_refresh_job"]) if runtime["active_prop_refresh_job"] else None,
        latest_prop_refresh_job=RefreshJobRead.model_validate(runtime["latest_prop_refresh_job"]) if runtime["latest_prop_refresh_job"] else None,
    )


@router.get("/sports", response_model=list[SportRead])
def list_sports(db: Session = Depends(get_db)) -> list[SportRead]:
    visible = _visible_sports()
    items = db.scalars(select(Sport).where(Sport.key.in_(tuple(visible)))).all()
    by_key = {item.key: item for item in items}
    return [SportRead.model_validate(by_key[key]) for key in visible if key in by_key]


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


def _build_trade_desk_response(db: Session, sport: str | None = None) -> TradeDeskResponse:
    normalized_sport = sport.upper() if sport else None
    availability_rows = sorted(_sport_availability_rows(db), key=lambda item: _sport_order(item.sport_key))
    if normalized_sport and normalized_sport not in CURRENT_WATCHLIST_SPORTS:
        research_rows = [
            row for row in availability_rows if row.sport_key == normalized_sport and row.availability_mode == "research_only"
        ]
        return TradeDeskResponse(events=[], research_sports=research_rows)

    markets = current_watchlist_markets(db, sport=normalized_sport if normalized_sport in CURRENT_WATCHLIST_SPORTS else None)
    market_ids = [market.id for market in markets]
    latest_recommendations = latest_recommendation_by_market_id(db, market_ids)

    event_buckets: dict[int, dict[str, object]] = {}
    for market in markets:
        recommendation = latest_recommendations.get(market.id)
        if recommendation is None or market.event is None:
            continue
        if recommendation.edge <= 0:
            continue
        if not _trade_desk_market_matches_event(market):
            continue

        raw_data = market.raw_data or {}
        family = str(raw_data.get("copilot_market_family") or "")
        bucket = event_buckets.setdefault(
            market.event.id,
            {
                "event": market.event,
                "game_lines": [],
                "props": {},
            },
        )

        if family == "player_prop":
            if recommendation.side.lower() != "yes":
                continue
            subject_name = str(raw_data.get("copilot_subject_name") or "").strip()
            subject_team = str(raw_data.get("copilot_subject_team") or "").strip() or None
            stat_key = str(raw_data.get("copilot_stat_key") or "").strip()
            threshold = raw_data.get("copilot_threshold")
            selected_probability = dict(recommendation.scoring_diagnostics or {}).get("selected_side_probability")
            if not subject_name or not stat_key or threshold is None or selected_probability is None:
                continue

            player_map = bucket["props"]
            assert isinstance(player_map, dict)
            player_key = (subject_name, subject_team)
            stat_map = player_map.setdefault(player_key, {})
            assert isinstance(stat_map, dict)
            thresholds = stat_map.setdefault(stat_key, [])
            assert isinstance(thresholds, list)
            thresholds.append(
                TradeDeskThresholdRead(
                    ticker=market.ticker,
                    threshold=float(threshold),
                    probability_yes=float(selected_probability),
                    selected_side=recommendation.side,
                    selected_side_probability=float(selected_probability),
                    entry_price=recommendation.suggested_price,
                    edge=recommendation.edge,
                    confidence=recommendation.confidence,
                    kalshi_url=_kalshi_market_url(market.ticker),
                )
            )
            continue

        if family not in {"winner", "game_line"}:
            continue

        selected_probability = dict(recommendation.scoring_diagnostics or {}).get("selected_side_probability")
        if selected_probability is None:
            continue
        game_lines = bucket["game_lines"]
        assert isinstance(game_lines, list)
        game_lines.append(
            TradeDeskGameLineRead(
                ticker=market.ticker,
                market_title=market.title,
                display_label=_game_line_display_label(market, recommendation),
                sport_key=market.sport_key,
                market_kind=str(raw_data.get("copilot_market_kind") or ""),
                selected_side=recommendation.side,
                projected_side_label=_game_line_projected_label(market, recommendation),
                selected_side_probability=float(selected_probability),
                entry_price=recommendation.suggested_price,
                edge=recommendation.edge,
                confidence=recommendation.confidence,
                kalshi_url=_kalshi_market_url(market.ticker),
            )
        )

    events: list[TradeDeskEventRead] = []
    game_line_order = {"game_winner": 0, "first_five_winner": 0, "spread": 1, "total": 2}
    for bucket in event_buckets.values():
        event = bucket["event"]
        game_lines = bucket["game_lines"]
        props = bucket["props"]
        assert isinstance(event, Event)
        assert isinstance(game_lines, list)
        assert isinstance(props, dict)

        player_props: list[TradeDeskPlayerPropRead] = []
        for (subject_name, subject_team), stat_map in props.items():
            assert isinstance(stat_map, dict)
            stat_groups: list[TradeDeskStatGroupRead] = []
            best_edge = 0.0
            best_win_prob: float | None = None
            for stat_key, thresholds in stat_map.items():
                assert isinstance(thresholds, list)
                thresholds.sort(key=lambda item: item.threshold)
                if not _thresholds_are_monotonic(thresholds):
                    continue
                best_index = max(range(len(thresholds)), key=lambda index: thresholds[index].edge)
                thresholds[best_index].is_best = True
                best_edge = max(best_edge, thresholds[best_index].edge)
                group_win_prob = max((threshold.probability_yes for threshold in thresholds), default=0.0)
                best_win_prob = group_win_prob if best_win_prob is None else max(best_win_prob, group_win_prob)
                stat_groups.append(
                    TradeDeskStatGroupRead(
                        stat_key=str(stat_key),
                        thresholds=thresholds,
                    )
                )
            if stat_groups:
                player_props.append(
                    TradeDeskPlayerPropRead(
                        subject_name=str(subject_name),
                        subject_team=subject_team,
                        stat_groups=stat_groups,
                        best_edge=best_edge,
                        best_win_prob=best_win_prob,
                    )
                )

        player_props.sort(key=lambda item: (-item.best_edge, item.subject_name.lower()))
        sorted_game_lines = sorted(
            game_lines,
            key=lambda item: (
                game_line_order.get(item.market_kind, 99),
                -item.edge,
                item.display_label.lower(),
            ),
        )
        if not sorted_game_lines and not player_props:
            continue
        events.append(
            TradeDeskEventRead(
                event_id=event.id,
                event_name=event.name,
                event_status=event.status,
                starts_at=event.starts_at,
                sport_key=event.sport_key,
                game_lines=sorted_game_lines,
                player_props=player_props,
            )
        )

    events.sort(
        key=lambda item: (
            _sport_order(item.sport_key),
            item.starts_at or datetime.max.replace(tzinfo=timezone.utc),
            item.event_name.lower(),
        )
    )

    research_sports = [
        row
        for row in availability_rows
        if row.availability_mode == "research_only" and (normalized_sport is None or row.sport_key == normalized_sport)
    ]
    return TradeDeskResponse(events=events, research_sports=research_sports)


@router.get("/sports/availability", response_model=list[SportAvailabilityRead])
def list_sport_availability(db: Session = Depends(get_db)) -> list[SportAvailabilityRead]:
    return sorted(_sport_availability_rows(db), key=lambda item: _sport_order(item.sport_key))


@router.get("/trade-desk", response_model=TradeDeskResponse)
def get_trade_desk(
    sport: str | None = None,
    db: Session = Depends(get_db),
) -> TradeDeskResponse:
    return _build_trade_desk_response(db, sport=sport)


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
        active_refresh_job=RefreshJobRead.model_validate(runtime["active_refresh_job"]) if runtime["active_refresh_job"] else None,
        latest_refresh_job=RefreshJobRead.model_validate(runtime["latest_refresh_job"]) if runtime["latest_refresh_job"] else None,
        active_prop_refresh_job=RefreshJobRead.model_validate(runtime["active_prop_refresh_job"]) if runtime["active_prop_refresh_job"] else None,
        latest_prop_refresh_job=RefreshJobRead.model_validate(runtime["latest_prop_refresh_job"]) if runtime["latest_prop_refresh_job"] else None,
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
        _serialize_recommendation(item, item.market)
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
    return _aggregate_prediction_summary(
        db,
        sport=sport,
        market_family=market_family,
        stat_key=stat_key,
        outcome=outcome,
        captured_from=captured_from,
        captured_to=captured_to,
    )


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
    return _aggregate_parlay_prediction_summary(db, sport_scope=scope, leg_count=validated_leg_count)


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
    market_ids = [market.id for market in markets]
    latest_snapshots = latest_snapshot_by_market_id(db, market_ids)
    latest_recommendations = latest_recommendation_by_market_id(db, market_ids)
    payload: list[MarketListRead] = []
    for market in markets:
        raw_data = market.raw_data or {}
        if not raw_data.get("copilot_market_kind"):
            continue
        latest_snapshot = latest_snapshots.get(market.id)
        latest_recommendation = latest_recommendations.get(market.id)
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


@router.post("/jobs/refresh", response_model=JobRefreshResponse, status_code=202)
def refresh_jobs(db: Session = Depends(get_db)) -> JobRefreshResponse:
    job, _created = enqueue_refresh_job(
        db,
        kind="refresh",
        scope="current_slate",
        reason="manual",
    )
    db.commit()
    return JobRefreshResponse(
        job_id=job.id,
        kind=job.kind,
        scope=job.scope,
        status=job.status,
    )


@router.get("/jobs/{job_id}", response_model=RefreshJobRead)
def refresh_job_detail(job_id: int, db: Session = Depends(get_db)) -> RefreshJobRead:
    job = get_refresh_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Refresh job not found")
    return _serialize_refresh_job(job)  # type: ignore[return-value]


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
