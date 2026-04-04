from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.clients.kalshi import KalshiPublicClient, parse_price_dollars
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
        existing = db.scalar(
            select(Prediction).where(
                Prediction.run_id == run_id,
                Prediction.market_id == market.id,
            )
        )
        if existing is not None:
            if recommendation is not None and existing.capture_scope != "recommendation":
                existing.capture_scope = "recommendation"
                existing.side = recommendation.side
                existing.action = action
                existing.suggested_price = suggested_price
                existing.edge = edge
                existing.confidence = confidence
                existing.selection_score = selection_score
                existing.invalidation = invalidation
                existing.rationale = rationale
                existing.reasons = list(signal.reasons or [])
                existing.features = dict(signal.features or {})
                existing.scoring_diagnostics = diagnostics
                existing.model_name = signal.model_name or MODEL_NAME
                existing.model_version = signal.model_version
                existing.calibration_version = signal.calibration_version
                existing.feature_set_version = signal.feature_set_version
                existing.model_metadata = dict(signal.model_metadata or {})
                existing.captured_at = captured_at
            return existing

    prediction = Prediction(
        run_id=run_id,
        event_id=event.id,
        market_id=market.id,
        ticker=market.ticker,
        sport_key=market.sport_key or event.sport_key,
        event_name=event.name,
        market_title=market.title,
        market_family=str(metadata.get("copilot_market_family") or "") or None,
        market_kind=str(metadata.get("copilot_market_kind") or "") or None,
        stat_key=str(metadata.get("copilot_stat_key") or "") or None,
        threshold=float(metadata.get("copilot_threshold")) if metadata.get("copilot_threshold") is not None else None,
        subject_name=str(metadata.get("copilot_subject_name") or "") or None,
        subject_team=str(metadata.get("copilot_subject_team") or "") or None,
        capture_scope=capture_scope,
        side=recommendation.side if recommendation else selected_side,
        action=action,
        suggested_price=suggested_price,
        fair_yes_price=signal.fair_yes_price,
        fair_no_price=signal.fair_no_price,
        edge=edge,
        confidence=confidence,
        selection_score=selection_score,
        model_name=signal.model_name or MODEL_NAME,
        model_version=signal.model_version,
        calibration_version=signal.calibration_version,
        feature_set_version=signal.feature_set_version,
        model_metadata=dict(signal.model_metadata or {}),
        invalidation=invalidation,
        rationale=rationale,
        reasons=list(signal.reasons or []),
        features=dict(signal.features or {}),
        scoring_diagnostics=diagnostics,
        market_status_at_capture=market.status,
        captured_at=captured_at,
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
) -> dict[str, int]:
    stmt = (
        select(Prediction)
        .options(joinedload(Prediction.market))
        .where(Prediction.settlement_status.in_(("pending", "unresolved")))
        .order_by(Prediction.captured_at.asc(), Prediction.id.asc())
    )
    if sport_keys:
        stmt = stmt.where(Prediction.sport_key.in_(tuple(sorted(sport_keys))))
    unsettled = db.scalars(stmt).all()

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
                latest_payload = client.get_market(prediction.ticker)
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
