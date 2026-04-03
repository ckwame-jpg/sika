from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from math import prod
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import (
    Event,
    Market,
    ParlayPrediction,
    ParlayPredictionLeg,
    ParlayRecommendation,
    ParlayRecommendationLeg,
    Prediction,
    Recommendation,
    SignalSnapshot,
)
from app.services.predictions import OPEN_MARKET_STATUSES


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def american_odds_from_probability(probability: float) -> str:
    value = max(0.01, min(0.99, probability))
    if value >= 0.5:
        odds = -round((value / (1 - value)) * 100)
    else:
        odds = round(((1 - value) / value) * 100)
    return f"{odds:+d}"


@dataclass(slots=True)
class ParlayCandidateInput:
    event: Event
    market: Market
    recommendation: Recommendation
    signal: SignalSnapshot
    prediction: Prediction
    metadata: dict[str, Any]


@dataclass(slots=True)
class GeneratedParlay:
    candidates: tuple[ParlayCandidateInput, ...]
    leg_count: int
    sport_scope: str
    participating_sports: list[str]
    combined_market_price: float
    combined_model_probability: float
    american_odds: str
    edge: float
    confidence: float
    invalidation: str
    rationale: str


def _selected_model_probability(candidate: ParlayCandidateInput) -> float:
    if candidate.recommendation.side == "yes":
        return float(candidate.signal.fair_yes_price)
    return float(candidate.signal.fair_no_price)


def _sport_scope(participating_sports: list[str]) -> str:
    if len(participating_sports) == 1:
        return participating_sports[0]
    return "MIXED"


def clear_active_parlay_watchlist(db: Session) -> None:
    db.query(ParlayRecommendationLeg).delete()
    db.query(ParlayRecommendation).delete()
    db.flush()


def _eligible_candidates(candidates: list[ParlayCandidateInput]) -> list[ParlayCandidateInput]:
    settings = get_settings()
    enabled_sports = {sport.upper() for sport in settings.parlay_enabled_sports}
    eligible: list[ParlayCandidateInput] = []
    for candidate in candidates:
        sport_key = (candidate.market.sport_key or candidate.event.sport_key or "").upper()
        if sport_key not in enabled_sports:
            continue
        if candidate.market.status not in OPEN_MARKET_STATUSES:
            continue
        if candidate.recommendation.status != "active":
            continue
        if candidate.event.status in {"completed", "cancelled"}:
            continue
        if candidate.recommendation.suggested_price <= 0:
            continue
        if _selected_model_probability(candidate) <= 0:
            continue
        eligible.append(candidate)

    eligible.sort(
        key=lambda item: (item.recommendation.edge, item.recommendation.confidence, item.recommendation.captured_at),
        reverse=True,
    )
    return eligible[: settings.parlay_candidate_pool_size]


def _combo_is_valid(combo: tuple[ParlayCandidateInput, ...]) -> bool:
    tickers = {candidate.market.ticker for candidate in combo}
    event_ids = {candidate.event.id for candidate in combo}
    return len(tickers) == len(combo) and len(event_ids) == len(combo)


def _build_generated_parlays(candidates: list[ParlayCandidateInput]) -> list[GeneratedParlay]:
    settings = get_settings()
    eligible = _eligible_candidates(candidates)
    if len(eligible) < settings.parlay_min_legs:
        return []

    generated: list[GeneratedParlay] = []
    max_legs = min(settings.parlay_max_legs, len(eligible))
    for leg_count in range(settings.parlay_min_legs, max_legs + 1):
        for combo in combinations(eligible, leg_count):
            if not _combo_is_valid(combo):
                continue

            combined_market_price = round(prod(candidate.recommendation.suggested_price for candidate in combo), 4)
            combined_model_probability = round(prod(_selected_model_probability(candidate) for candidate in combo), 4)
            edge = round(combined_model_probability - combined_market_price, 4)
            if edge <= 0:
                continue

            participating_sports = sorted({(candidate.market.sport_key or candidate.event.sport_key or "UNKNOWN").upper() for candidate in combo})
            confidence = round(min(candidate.recommendation.confidence for candidate in combo), 4)
            american_odds = american_odds_from_probability(combined_market_price)
            rationale = " + ".join(
                f"{candidate.market.title} ({candidate.recommendation.side.upper()} {american_odds_from_probability(candidate.recommendation.suggested_price)})"
                for candidate in combo
            )
            invalidation = (
                "Cancel if any leg closes, is suspended, or the captured entry meaningfully drifts before execution."
            )
            generated.append(
                GeneratedParlay(
                    candidates=combo,
                    leg_count=leg_count,
                    sport_scope=_sport_scope(participating_sports),
                    participating_sports=participating_sports,
                    combined_market_price=combined_market_price,
                    combined_model_probability=combined_model_probability,
                    american_odds=american_odds,
                    edge=edge,
                    confidence=confidence,
                    invalidation=invalidation,
                    rationale=rationale,
                )
            )

    generated.sort(key=lambda item: (-item.edge, -item.confidence, item.leg_count))
    return generated[: settings.parlay_max_output]


def _recommendation_leg_from_candidate(
    parlay_id: int,
    leg_index: int,
    candidate: ParlayCandidateInput,
) -> ParlayRecommendationLeg:
    metadata = candidate.metadata or {}
    return ParlayRecommendationLeg(
        parlay_recommendation_id=parlay_id,
        leg_index=leg_index,
        event_id=candidate.event.id,
        market_id=candidate.market.id,
        ticker=candidate.market.ticker,
        sport_key=candidate.market.sport_key or candidate.event.sport_key,
        event_name=candidate.event.name,
        market_title=candidate.market.title,
        market_family=str(metadata.get("copilot_market_family") or "") or None,
        market_kind=str(metadata.get("copilot_market_kind") or "") or None,
        stat_key=str(metadata.get("copilot_stat_key") or "") or None,
        threshold=float(metadata.get("copilot_threshold")) if metadata.get("copilot_threshold") is not None else None,
        subject_name=str(metadata.get("copilot_subject_name") or "") or None,
        subject_team=str(metadata.get("copilot_subject_team") or "") or None,
        side=candidate.recommendation.side,
        action=candidate.recommendation.action,
        suggested_price=candidate.recommendation.suggested_price,
        fair_yes_price=candidate.signal.fair_yes_price,
        fair_no_price=candidate.signal.fair_no_price,
        edge=candidate.recommendation.edge,
        confidence=candidate.recommendation.confidence,
    )


def _prediction_leg_from_candidate(
    parlay_id: int,
    leg_index: int,
    candidate: ParlayCandidateInput,
) -> ParlayPredictionLeg:
    metadata = candidate.metadata or {}
    return ParlayPredictionLeg(
        parlay_prediction_id=parlay_id,
        leg_index=leg_index,
        source_prediction_id=candidate.prediction.id,
        event_id=candidate.event.id,
        market_id=candidate.market.id,
        ticker=candidate.market.ticker,
        sport_key=candidate.market.sport_key or candidate.event.sport_key,
        event_name=candidate.event.name,
        market_title=candidate.market.title,
        market_family=str(metadata.get("copilot_market_family") or "") or None,
        market_kind=str(metadata.get("copilot_market_kind") or "") or None,
        stat_key=str(metadata.get("copilot_stat_key") or "") or None,
        threshold=float(metadata.get("copilot_threshold")) if metadata.get("copilot_threshold") is not None else None,
        subject_name=str(metadata.get("copilot_subject_name") or "") or None,
        subject_team=str(metadata.get("copilot_subject_team") or "") or None,
        side=candidate.recommendation.side,
        action=candidate.recommendation.action,
        suggested_price=candidate.recommendation.suggested_price,
        fair_yes_price=candidate.signal.fair_yes_price,
        fair_no_price=candidate.signal.fair_no_price,
        edge=candidate.recommendation.edge,
        confidence=candidate.recommendation.confidence,
    )


def capture_parlay_artifacts(
    db: Session,
    *,
    run_id: int | None,
    candidates: list[ParlayCandidateInput],
) -> tuple[int, int]:
    generated = _build_generated_parlays(candidates)
    if not generated:
        return 0, 0

    recommendation_count = 0
    prediction_count = 0
    captured_at = _now_utc()
    for item in generated:
        recommendation = ParlayRecommendation(
            run_id=run_id,
            leg_count=item.leg_count,
            sport_scope=item.sport_scope,
            participating_sports=item.participating_sports,
            status="active",
            combined_market_price=item.combined_market_price,
            combined_model_probability=item.combined_model_probability,
            american_odds=item.american_odds,
            edge=item.edge,
            confidence=item.confidence,
            invalidation=item.invalidation,
            rationale=item.rationale,
            captured_at=captured_at,
        )
        db.add(recommendation)
        db.flush()
        for leg_index, candidate in enumerate(item.candidates, start=1):
            db.add(_recommendation_leg_from_candidate(recommendation.id, leg_index, candidate))
        recommendation_count += 1

        prediction = ParlayPrediction(
            run_id=run_id,
            leg_count=item.leg_count,
            sport_scope=item.sport_scope,
            participating_sports=item.participating_sports,
            combined_market_price=item.combined_market_price,
            combined_model_probability=item.combined_model_probability,
            american_odds=item.american_odds,
            edge=item.edge,
            confidence=item.confidence,
            rationale=item.rationale,
            invalidation=item.invalidation,
            captured_at=captured_at,
        )
        db.add(prediction)
        db.flush()
        for leg_index, candidate in enumerate(item.candidates, start=1):
            db.add(_prediction_leg_from_candidate(prediction.id, leg_index, candidate))
        prediction_count += 1

    db.flush()
    return recommendation_count, prediction_count


def settle_parlay_predictions(db: Session) -> dict[str, int]:
    unsettled = db.scalars(
        select(ParlayPrediction)
        .options(joinedload(ParlayPrediction.legs).joinedload(ParlayPredictionLeg.source_prediction))
        .where(ParlayPrediction.settlement_status.in_(("pending", "unresolved")))
        .order_by(ParlayPrediction.captured_at.asc(), ParlayPrediction.id.asc())
    ).unique().all()

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
    if not unsettled:
        return summary

    for parlay in unsettled:
        source_predictions = [leg.source_prediction for leg in parlay.legs if leg.source_prediction is not None]
        if len(source_predictions) != len(parlay.legs):
            parlay.settlement_status = "unresolved"
            parlay.prediction_outcome = "unresolved"
            parlay.settlement_notes = "One or more source leg predictions are missing."
            summary["updated"] += 1
            summary["unresolved"] += 1
            continue

        outcomes = [prediction.prediction_outcome for prediction in source_predictions]
        statuses = [prediction.settlement_status for prediction in source_predictions]
        if any(outcome == "lost" for outcome in outcomes):
            parlay.settlement_status = "settled"
            parlay.prediction_outcome = "lost"
            parlay.settlement_value = 0.0
            parlay.realized_pnl = round(-parlay.combined_market_price, 4)
            parlay.settled_at = _now_utc()
            parlay.settlement_notes = "At least one leg settled as a loss."
            summary["updated"] += 1
            summary["lost"] += 1
            continue

        if outcomes and all(outcome == "won" for outcome in outcomes):
            parlay.settlement_status = "settled"
            parlay.prediction_outcome = "won"
            parlay.settlement_value = 1.0
            parlay.realized_pnl = round(1.0 - parlay.combined_market_price, 4)
            parlay.settled_at = _now_utc()
            parlay.settlement_notes = "Every leg settled as a win."
            summary["updated"] += 1
            summary["won"] += 1
            continue

        if any(outcome in {"cancelled", "push"} for outcome in outcomes):
            parlay.settlement_status = "cancelled"
            parlay.prediction_outcome = "cancelled"
            parlay.settlement_value = None
            parlay.realized_pnl = 0.0
            parlay.settled_at = _now_utc()
            parlay.settlement_notes = "At least one leg cancelled or pushed, so the parlay was cancelled."
            summary["updated"] += 1
            summary["cancelled"] += 1
            continue

        if any(status == "unresolved" or outcome == "unresolved" for status, outcome in zip(statuses, outcomes, strict=False)):
            if parlay.settlement_status != "unresolved" or parlay.prediction_outcome != "unresolved":
                parlay.settlement_status = "unresolved"
                parlay.prediction_outcome = "unresolved"
                parlay.settlement_notes = "One or more legs left the open state without a final settlement result."
                parlay.settled_at = None
                parlay.realized_pnl = None
                summary["updated"] += 1
            summary["unresolved"] += 1
            continue

        if any(status == "pending" or outcome == "pending" for status, outcome in zip(statuses, outcomes, strict=False)):
            summary["pending"] += 1
            continue

        parlay.settlement_status = "unresolved"
        parlay.prediction_outcome = "unresolved"
        parlay.settlement_notes = "Parlay settlement did not match a recognized terminal pattern."
        summary["updated"] += 1
        summary["unresolved"] += 1

    db.flush()
    return summary
