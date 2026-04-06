from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.clients.kalshi import KalshiPublicClient, parse_price_dollars
from app.config import get_settings
from app.models import Event, Market, Prediction, Recommendation, SignalSnapshot
from app.services.ml.lineage import HEURISTIC_SINGLE_MODEL
from app.services.market_support import parse_market_datetime

MODEL_NAME = HEURISTIC_SINGLE_MODEL.model_name
OPEN_MARKET_STATUSES = {"open", "active", "paused", "initialized"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.lower() if text else None


def _round_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 4)


def _market_settlement_value(payload: dict[str, Any]) -> float | None:
    for field in ("settlement_value_dollars", "settlement_value"):
        parsed = parse_price_dollars(payload.get(field))
        if parsed is not None:
            return parsed
    return None


def _prediction_payout(side: str, settlement_value: float) -> float:
    return settlement_value if side == "yes" else 1.0 - settlement_value


def _coverage_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().default_timezone)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coverage_day_window(captured_at: datetime) -> tuple[datetime, datetime]:
    local_tz = _coverage_timezone()
    captured_local = _coerce_utc(captured_at).astimezone(local_tz)
    local_day_start = datetime.combine(captured_local.date(), time.min, tzinfo=local_tz)
    local_day_end = local_day_start + timedelta(days=1)
    return local_day_start.astimezone(timezone.utc), local_day_end.astimezone(timezone.utc)


@dataclass(slots=True)
class PredictionSettlement:
    settlement_status: str
    prediction_outcome: str
    market_result: str | None
    winning_side: str | None
    settlement_value: float | None
    settled_at: datetime | None
    realized_pnl: float | None
    settlement_notes: str | None


def _session_predictions(db: Session) -> list[Prediction]:
    seen_ids: set[int] = set()
    predictions: list[Prediction] = []
    for collection in (db.identity_map.values(), db.new):
        for item in collection:
            if not isinstance(item, Prediction):
                continue
            identity = id(item)
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            predictions.append(item)
    return predictions


def _apply_prediction_capture(
    prediction: Prediction,
    *,
    run_id: int | None,
    event: Event,
    market: Market,
    recommendation: Recommendation | None,
    signal: SignalSnapshot,
    metadata: dict[str, Any],
    capture_scope: str,
    diagnostics: dict[str, Any],
    selected_side: str,
    action: str,
    suggested_price: float,
    edge: float,
    confidence: float,
    selection_score: float | None,
    invalidation: str | None,
    rationale: str,
    captured_at: datetime,
    reset_settlement: bool = False,
) -> Prediction:
    prediction.run_id = run_id
    prediction.event_id = event.id
    prediction.market_id = market.id
    prediction.ticker = market.ticker
    prediction.sport_key = market.sport_key or event.sport_key
    prediction.event_name = event.name
    prediction.market_title = market.title
    prediction.market_family = str(metadata.get("copilot_market_family") or "") or None
    prediction.market_kind = str(metadata.get("copilot_market_kind") or "") or None
    prediction.stat_key = str(metadata.get("copilot_stat_key") or "") or None
    prediction.threshold = float(metadata.get("copilot_threshold")) if metadata.get("copilot_threshold") is not None else None
    prediction.subject_name = str(metadata.get("copilot_subject_name") or "") or None
    prediction.subject_team = str(metadata.get("copilot_subject_team") or "") or None
    prediction.capture_scope = capture_scope
    prediction.side = recommendation.side if recommendation else selected_side
    prediction.action = action
    prediction.suggested_price = suggested_price
    prediction.fair_yes_price = signal.fair_yes_price
    prediction.fair_no_price = signal.fair_no_price
    prediction.edge = edge
    prediction.confidence = confidence
    prediction.selection_score = selection_score
    prediction.model_name = signal.model_name or MODEL_NAME
    prediction.model_version = signal.model_version
    prediction.calibration_version = signal.calibration_version
    prediction.feature_set_version = signal.feature_set_version
    prediction.model_metadata = dict(signal.model_metadata or {})
    prediction.invalidation = invalidation
    prediction.rationale = rationale
    prediction.reasons = list(signal.reasons or [])
    prediction.features = dict(signal.features or {})
    prediction.scoring_diagnostics = diagnostics
    prediction.market_status_at_capture = market.status
    prediction.captured_at = captured_at
    if reset_settlement:
        prediction.settlement_status = "pending"
        prediction.prediction_outcome = "pending"
        prediction.market_result = None
        prediction.winning_side = None
        prediction.settlement_value = None
        prediction.settled_at = None
        prediction.realized_pnl = None
        prediction.settlement_source = None
        prediction.settlement_notes = None
    return prediction


def capture_prediction(
    db: Session,
    *,
    run_id: int | None,
    event: Event,
    market: Market,
    recommendation: Recommendation | None,
    signal: SignalSnapshot,
    metadata: dict[str, Any],
    capture_scope: str = "recommendation",
) -> Prediction:
    diagnostics = dict(recommendation.scoring_diagnostics or signal.scoring_diagnostics or {}) if recommendation else dict(signal.scoring_diagnostics or {})
    selected_side = str(diagnostics.get("selected_side") or "yes")
    suggested_price_value = diagnostics.get("suggested_price")
    if suggested_price_value is None:
        suggested_price_value = signal.fair_yes_price if selected_side == "yes" else signal.fair_no_price
    suggested_price = recommendation.suggested_price if recommendation else float(suggested_price_value or 0.0)
    action = recommendation.action if recommendation else "buy"
    invalidation = recommendation.invalidation if recommendation else (diagnostics.get("invalidation") or None)
    rationale = recommendation.rationale if recommendation else "; ".join(str(reason) for reason in list(signal.reasons or []))
    edge = recommendation.edge if recommendation else signal.edge
    confidence = recommendation.confidence if recommendation else signal.confidence
    selection_score = recommendation.selection_score if recommendation else signal.selection_score
    captured_at = recommendation.captured_at if recommendation else signal.captured_at

    if run_id is not None:
        existing = next(
            (
                item
                for item in _session_predictions(db)
                if item.run_id == run_id and item.market_id == market.id
            ),
            None,
        )
        if existing is None:
            existing = db.scalar(
                select(Prediction).where(
                    Prediction.run_id == run_id,
                    Prediction.market_id == market.id,
                )
            )
        if existing is not None:
            if recommendation is not None or existing.capture_scope == "coverage":
                _apply_prediction_capture(
                    existing,
                    run_id=run_id,
                    event=event,
                    market=market,
                    recommendation=recommendation,
                    signal=signal,
                    metadata=metadata,
                    capture_scope="recommendation" if recommendation is not None else capture_scope,
                    diagnostics=diagnostics,
                    selected_side=selected_side,
                    action=action,
                    suggested_price=suggested_price,
                    edge=edge,
                    confidence=confidence,
                    selection_score=selection_score,
                    invalidation=invalidation,
                    rationale=rationale,
                    captured_at=captured_at,
                    reset_settlement=recommendation is None,
                )
            return existing

    if capture_scope == "coverage":
        coverage_window_start, coverage_window_end = _coverage_day_window(captured_at)
        sampled = next(
            (
                item
                for item in _session_predictions(db)
                if item.market_id == market.id
                and item.capture_scope == "coverage"
                and item.captured_at is not None
                and coverage_window_start <= _coerce_utc(item.captured_at) < coverage_window_end
            ),
            None,
        )
        if sampled is None:
            sampled = db.scalar(
                select(Prediction)
                .where(
                    Prediction.market_id == market.id,
                    Prediction.capture_scope == "coverage",
                    Prediction.captured_at >= coverage_window_start,
                    Prediction.captured_at < coverage_window_end,
                )
                .order_by(Prediction.captured_at.desc(), Prediction.id.desc())
                .limit(1)
            )
        if sampled is not None:
            return _apply_prediction_capture(
                sampled,
                run_id=run_id,
                event=event,
                market=market,
                recommendation=None,
                signal=signal,
                metadata=metadata,
                capture_scope="coverage",
                diagnostics=diagnostics,
                selected_side=selected_side,
                action=action,
                suggested_price=suggested_price,
                edge=edge,
                confidence=confidence,
                selection_score=selection_score,
                invalidation=invalidation,
                rationale=rationale,
                captured_at=captured_at,
                reset_settlement=True,
            )

    prediction = _apply_prediction_capture(
        Prediction(),
        run_id=run_id,
        event=event,
        market=market,
        recommendation=recommendation,
        signal=signal,
        metadata=metadata,
        capture_scope=capture_scope,
        diagnostics=diagnostics,
        selected_side=selected_side,
        action=action,
        suggested_price=suggested_price,
        edge=edge,
        confidence=confidence,
        selection_score=selection_score,
        invalidation=invalidation,
        rationale=rationale,
        captured_at=captured_at,
        reset_settlement=capture_scope == "coverage",
    )
    db.add(prediction)
    return prediction


def resolve_prediction_settlement(prediction: Prediction, payload: dict[str, Any]) -> PredictionSettlement | None:
    market_status = _normalized_text(payload.get("status"))
    market_result = _normalized_text(payload.get("result"))
    settlement_value = _market_settlement_value(payload)
    settled_at = parse_market_datetime(payload.get("settlement_ts"))

    if market_result in OPEN_MARKET_STATUSES or market_status in OPEN_MARKET_STATUSES:
        return None

    if market_result in {"yes", "no"}:
        winning_side = market_result
        binary_value = 1.0 if winning_side == "yes" else 0.0
        pnl = _prediction_payout(prediction.side, settlement_value if settlement_value is not None else binary_value) - prediction.suggested_price
        return PredictionSettlement(
            settlement_status="settled",
            prediction_outcome="won" if prediction.side == winning_side else "lost",
            market_result=market_result,
            winning_side=winning_side,
            settlement_value=_round_or_none(settlement_value if settlement_value is not None else binary_value),
            settled_at=settled_at or _now_utc(),
            realized_pnl=round(pnl, 4),
            settlement_notes=None,
        )

    if market_result in {"void", "cancelled", "canceled"}:
        return PredictionSettlement(
            settlement_status="cancelled",
            prediction_outcome="cancelled",
            market_result=market_result,
            winning_side=None,
            settlement_value=_round_or_none(settlement_value),
            settled_at=settled_at or _now_utc(),
            realized_pnl=0.0,
            settlement_notes="Kalshi reported a void/cancelled result.",
        )

    if settlement_value is not None:
        if settlement_value >= 0.999:
            winning_side = "yes"
            pnl = _prediction_payout(prediction.side, 1.0) - prediction.suggested_price
            return PredictionSettlement(
                settlement_status="settled",
                prediction_outcome="won" if prediction.side == winning_side else "lost",
                market_result=market_result or winning_side,
                winning_side=winning_side,
                settlement_value=1.0,
                settled_at=settled_at or _now_utc(),
                realized_pnl=round(pnl, 4),
                settlement_notes="Derived binary result from Kalshi settlement value.",
            )
        if settlement_value <= 0.001:
            winning_side = "no"
            pnl = _prediction_payout(prediction.side, 0.0) - prediction.suggested_price
            return PredictionSettlement(
                settlement_status="settled",
                prediction_outcome="won" if prediction.side == winning_side else "lost",
                market_result=market_result or winning_side,
                winning_side=winning_side,
                settlement_value=0.0,
                settled_at=settled_at or _now_utc(),
                realized_pnl=round(pnl, 4),
                settlement_notes="Derived binary result from Kalshi settlement value.",
            )

        pnl = _prediction_payout(prediction.side, settlement_value) - prediction.suggested_price
        return PredictionSettlement(
            settlement_status="settled",
            prediction_outcome="push",
            market_result=market_result or "fair_market_price",
            winning_side=None,
            settlement_value=_round_or_none(settlement_value),
            settled_at=settled_at or _now_utc(),
            realized_pnl=round(pnl, 4),
            settlement_notes="Non-binary settlement value treated as a push/fair-market-price resolution.",
        )

    if market_status in OPEN_MARKET_STATUSES or market_status is None:
        return None

    return PredictionSettlement(
        settlement_status="unresolved",
        prediction_outcome="unresolved",
        market_result=market_result,
        winning_side=None,
        settlement_value=None,
        settled_at=None,
        realized_pnl=None,
        settlement_notes="Market is no longer open but no settlement result is available yet.",
    )


def settle_predictions(
    db: Session,
    *,
    client: KalshiPublicClient | None = None,
    open_market_tickers: set[str] | None = None,
    sport_keys: set[str] | None = None,
    latest_only_per_key: bool = False,
) -> dict[str, int]:
    stmt = (
        select(Prediction)
        .options(joinedload(Prediction.market))
        .where(Prediction.settlement_status.in_(("pending", "unresolved")))
        .order_by(Prediction.captured_at.desc(), Prediction.id.desc())
    )
    if sport_keys:
        stmt = stmt.where(Prediction.sport_key.in_(tuple(sorted(sport_keys))))
    unsettled = db.scalars(stmt).all()
    if latest_only_per_key:
        latest_unsettled: list[Prediction] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for prediction in unsettled:
            dedupe_key = (prediction.ticker, prediction.capture_scope or "recommendation", prediction.side)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            latest_unsettled.append(prediction)
        unsettled = latest_unsettled

    if not unsettled:
        return {
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

    client = client or KalshiPublicClient()
    summary = {
        "processed": len(unsettled),
        "updated": 0,
        "won": 0,
        "lost": 0,
        "push": 0,
        "cancelled": 0,
        "pending": 0,
        "unresolved": 0,
        "errors": 0,
    }
    open_market_tickers = set(open_market_tickers or set())
    payload_cache: dict[str, dict[str, Any]] = {}

    for prediction in unsettled:
        market = prediction.market
        if open_market_tickers and prediction.ticker in open_market_tickers:
            summary["pending"] += 1
            continue

        payload = dict(market.raw_data or {}) if market else {}
        settlement_source = "stored_market_raw_data"
        should_refresh = market is not None and (
            not open_market_tickers
            or prediction.ticker not in open_market_tickers
        )

        if should_refresh:
            try:
                latest_payload = payload_cache.get(prediction.ticker)
                if latest_payload is None:
                    latest_payload = client.get_market(prediction.ticker)
                    payload_cache[prediction.ticker] = latest_payload
            except Exception:
                summary["errors"] += 1
                if prediction.settlement_status == "pending":
                    summary["pending"] += 1
                else:
                    summary["unresolved"] += 1
                continue
            if latest_payload:
                payload = {**payload, **latest_payload}
                settlement_source = "kalshi_get_market"
                if market is not None:
                    market.status = latest_payload.get("status") or market.status
                    close_time = parse_market_datetime(latest_payload.get("close_time"))
                    if close_time is not None:
                        market.close_time = close_time
                    market.raw_data = {**(market.raw_data or {}), **latest_payload}

        resolution = resolve_prediction_settlement(prediction, payload)
        if resolution is None:
            summary["pending"] += 1
            continue

        previous_outcome = prediction.prediction_outcome
        previous_status = prediction.settlement_status

        prediction.settlement_status = resolution.settlement_status
        prediction.prediction_outcome = resolution.prediction_outcome
        prediction.market_result = resolution.market_result
        prediction.winning_side = resolution.winning_side
        prediction.settlement_value = resolution.settlement_value
        prediction.settled_at = resolution.settled_at
        prediction.realized_pnl = resolution.realized_pnl
        prediction.settlement_source = settlement_source
        prediction.settlement_notes = resolution.settlement_notes

        if (previous_status, previous_outcome) != (prediction.settlement_status, prediction.prediction_outcome):
            summary["updated"] += 1

        if prediction.prediction_outcome in summary:
            summary[prediction.prediction_outcome] += 1
        elif prediction.prediction_outcome == "won":
            summary["won"] += 1
        elif prediction.prediction_outcome == "lost":
            summary["lost"] += 1
        elif prediction.prediction_outcome == "push":
            summary["push"] += 1
        elif prediction.prediction_outcome == "cancelled":
            summary["cancelled"] += 1
        else:
            summary["unresolved"] += 1

    db.flush()
    return summary
