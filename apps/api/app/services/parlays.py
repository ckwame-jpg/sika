from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from math import prod
from typing import Any

from sqlalchemy import and_, or_, select
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
from app.services.ml.lineage import HEURISTIC_PARLAY_MODEL
from app.services.ml.runtime import run_serving_inference
from app.services.model_families import parlay_family_key
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
    selection_score: float
    invalidation: str
    rationale: str
    scoring_diagnostics: dict[str, Any]
    model_name: str
    model_version: str | None
    calibration_version: str | None
    feature_set_version: str | None
    model_metadata: dict[str, Any]


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


def _candidate_selection_score(candidate: ParlayCandidateInput) -> float:
    return float(candidate.recommendation.selection_score or candidate.recommendation.edge)


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
        key=lambda item: (_candidate_selection_score(item), item.recommendation.confidence, item.recommendation.captured_at),
        reverse=True,
    )
    return eligible[: settings.parlay_candidate_pool_size]


def _combo_is_valid(combo: tuple[ParlayCandidateInput, ...]) -> bool:
    tickers = {candidate.market.ticker for candidate in combo}
    event_ids = {candidate.event.id for candidate in combo}
    return len(tickers) == len(combo) and len(event_ids) == len(combo)


def _candidate_team_key(candidate: ParlayCandidateInput) -> str | None:
    return str(candidate.metadata.get("copilot_subject_team") or "").upper() or None


def _candidate_subject_key(candidate: ParlayCandidateInput) -> str | None:
    return str(candidate.metadata.get("copilot_subject_name") or "").strip().lower() or None


def _candidate_opponent_key(candidate: ParlayCandidateInput) -> str | None:
    team_key = _candidate_team_key(candidate)
    if not team_key:
        return None
    for participant in candidate.event.participants:
        participant_name = str(participant.participant.display_name or "").upper()
        participant_short = str(participant.participant.short_name or "").upper()
        if team_key and team_key in {participant_name, participant_short}:
            continue
        return participant_short or participant_name or None
    return None


def _count_correlation_pairs(combo: tuple[ParlayCandidateInput, ...]) -> dict[str, int]:
    """Pair counts that signal positive correlation between parlay legs.

    Used both by ``_parlay_diagnostics_for_combo`` (drives the confidence
    penalty) and by ``_correlation_adjusted_joint_probability`` (drives
    bug #5's joint probability lift).
    """
    teams = [_candidate_team_key(candidate) for candidate in combo]
    subjects = [_candidate_subject_key(candidate) for candidate in combo]
    opponents = [_candidate_opponent_key(candidate) for candidate in combo]
    same_team_pairs = 0
    shared_subject_pairs = 0
    shared_opponent_pairs = 0
    for left in range(len(combo)):
        for right in range(left + 1, len(combo)):
            if teams[left] and teams[left] == teams[right]:
                same_team_pairs += 1
            if subjects[left] and subjects[left] == subjects[right]:
                shared_subject_pairs += 1
            if opponents[left] and opponents[left] == opponents[right]:
                shared_opponent_pairs += 1
    return {
        "same_team": same_team_pairs,
        "shared_subject": shared_subject_pairs,
        "shared_opponent": shared_opponent_pairs,
    }


def _correlation_adjusted_joint_probability(
    combo: tuple[ParlayCandidateInput, ...],
    pairs: dict[str, int],
) -> float:
    """Joint probability of all legs hitting, corrected for correlation.

    Bug #5: the strict product of leg probabilities assumes independence.
    For the parlays sika constructs — same player, same team, shared
    opponent — that assumption is wrong in a specific direction: those
    legs are POSITIVELY correlated, and probability theory guarantees
    that for positive correlation ``P(A∩B) >= P(A) * P(B)``. The strict
    product UNDERSTATES the joint, so genuine same-game-parlay edges
    get filtered out before the user sees them.

    (Aside: the punch list framing said independence "overstates" the
    joint. That's only true for *negatively* correlated legs — mutually
    exclusive outcomes like "Lakers win + Thunder win". Sika's combo
    construction filters those out, so in practice every correlated
    parlay we see is positive correlation and needs to move UP.)

    Formula: blend between the strict product (lower bound, independence)
    and the minimum leg probability (upper bound, since ``P(A∩B) <=
    min(P(A), P(B))`` regardless of correlation direction). Correlation
    factor scales with the number of shared-subject/team/opponent pairs.
    For independent legs the factor is zero and this returns the product.
    """
    leg_probs = [_selected_model_probability(candidate) for candidate in combo]
    independent = prod(leg_probs)
    if len(leg_probs) <= 1:
        return float(independent)
    # Codex PR #31 P1: P(A∩B) ≤ min(P(A), P(B)) — the joint can never
    # exceed the weakest leg's probability. Anchoring the blend on
    # min_leg keeps the result mathematically valid.
    min_leg = min(leg_probs)
    total_pairs = len(leg_probs) * (len(leg_probs) - 1) // 2
    # Per-pair weights: same player on both legs (subject) is the strongest
    # positive-correlation signal, same team is moderate, shared opponent
    # is mild. Hard cap below 1.0 so the joint never reaches min_leg fully
    # (some idiosyncratic noise remains even on co-moving legs).
    weighted = (
        0.7 * pairs.get("shared_subject", 0)
        + 0.3 * pairs.get("same_team", 0)
        + 0.2 * pairs.get("shared_opponent", 0)
    ) / max(total_pairs, 1)
    correlation_factor = min(weighted, 0.85)
    return float(independent + correlation_factor * (min_leg - independent))


def _parlay_diagnostics_for_combo(
    combo: tuple[ParlayCandidateInput, ...],
    *,
    leg_count: int,
    sport_scope: str,
) -> tuple[float, dict[str, Any]]:
    confidences = [float(candidate.recommendation.confidence) for candidate in combo]
    pair_counts = _count_correlation_pairs(combo)
    same_team_pairs = pair_counts["same_team"]
    shared_subject_pairs = pair_counts["shared_subject"]
    shared_opponent_pairs = pair_counts["shared_opponent"]

    same_sport_penalty = 0.01 if sport_scope != "MIXED" and leg_count <= 3 else 0.0
    same_team_penalty = round(same_team_pairs * 0.04, 4)
    shared_subject_penalty = round(shared_subject_pairs * 0.05, 4)
    shared_opponent_penalty = round(shared_opponent_pairs * 0.03, 4)
    leg_count_penalty = round(max(0, leg_count - 2) * (0.04 if leg_count <= 3 else 0.06), 4)
    total_penalty = round(
        same_sport_penalty + same_team_penalty + shared_subject_penalty + shared_opponent_penalty + leg_count_penalty,
        4,
    )
    base_confidence = min(confidences) if leg_count >= 4 else sum(confidences) / max(len(confidences), 1)
    confidence_cap = 0.82 if leg_count >= 4 else 0.92
    confidence = max(min(round(base_confidence - total_penalty, 4), confidence_cap), 0.2)
    diagnostics = {
        "family_key": parlay_family_key(leg_count, [candidate.market.sport_key or candidate.event.sport_key or "UNKNOWN" for candidate in combo]),
        "confidence_semantics": "heuristic_reliability",
        "base_confidence": round(base_confidence, 4),
        "adjusted_confidence": confidence,
        "same_team_pairs": same_team_pairs,
        "shared_subject_pairs": shared_subject_pairs,
        "shared_opponent_pairs": shared_opponent_pairs,
        "penalties": {
            "same_sport": round(same_sport_penalty, 4),
            "same_team": same_team_penalty,
            "shared_subject": shared_subject_penalty,
            "shared_opponent": shared_opponent_penalty,
            "leg_count": leg_count_penalty,
        },
        "feature_flags": {
            "mixed_scope": sport_scope == "MIXED",
            "all_leg_scores_available": True,
        },
        "missing_context": [],
    }
    return confidence, diagnostics


def _parlay_selection_score(edge: float, confidence: float, diagnostics: dict[str, Any]) -> float:
    total_penalty = sum(float(value or 0.0) for value in dict(diagnostics.get("penalties") or {}).values())
    return round(max((edge * 0.7) + (confidence * 0.22) - (total_penalty * 0.55), 0.0), 4)


def _build_generated_parlays(db: Session, candidates: list[ParlayCandidateInput]) -> list[GeneratedParlay]:
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
            # Bug #5: lift the joint probability toward max_leg when legs
            # share subject/team/opponent. Independent legs are unchanged.
            pair_counts = _count_correlation_pairs(combo)
            combined_model_probability = round(
                _correlation_adjusted_joint_probability(combo, pair_counts), 4
            )
            participating_sports = sorted({(candidate.market.sport_key or candidate.event.sport_key or "UNKNOWN").upper() for candidate in combo})
            sport_scope = _sport_scope(participating_sports)
            confidence, diagnostics = _parlay_diagnostics_for_combo(
                combo,
                leg_count=leg_count,
                sport_scope=sport_scope,
            )
            active_lineage = HEURISTIC_PARLAY_MODEL
            runtime_decision = None
            ml_result, runtime_decision = run_serving_inference(
                db,
                family_key=str(diagnostics.get("family_key") or parlay_family_key(leg_count, participating_sports)),
                scope="parlay",
                features=diagnostics,
            )
            if ml_result is not None:
                combined_model_probability = round(ml_result.probability, 4)
                confidence = round(ml_result.confidence, 4)
                active_lineage = ml_result.lineage
                diagnostics = {
                    **diagnostics,
                    "confidence_semantics": "calibrated_probability",
                    "base_confidence": confidence,
                    "adjusted_confidence": confidence,
                    "penalties": {
                        "same_sport": 0.0,
                        "same_team": 0.0,
                        "shared_subject": 0.0,
                        "shared_opponent": 0.0,
                        "leg_count": 0.0,
                    },
                    "serving_mode": "ml",
                    "artifact_path": ml_result.artifact_path,
                }
            elif runtime_decision and runtime_decision.fallback_active and runtime_decision.last_error:
                diagnostics["fallback_reason"] = runtime_decision.last_error
                diagnostics["serving_mode"] = "heuristic_fallback"
            else:
                diagnostics["serving_mode"] = "heuristic"

            edge = round(combined_model_probability - combined_market_price, 4)
            if edge <= 0:
                continue

            selection_score = _parlay_selection_score(edge, confidence, diagnostics)
            american_odds = american_odds_from_probability(combined_market_price)
            rationale = " + ".join(
                f"{candidate.market.title} ({candidate.recommendation.side.upper()} {american_odds_from_probability(candidate.recommendation.suggested_price)})"
                for candidate in combo
            )
            if ml_result is not None:
                rationale = f"{rationale}; served by {ml_result.lineage.model_name}"
            invalidation = (
                "Cancel if any leg closes, is suspended, or the captured entry meaningfully drifts before execution."
            )
            model_metadata = dict(active_lineage.model_metadata or {})
            model_metadata.update(
                {
                    "family_key": diagnostics.get("family_key"),
                    "desired_mode": runtime_decision.desired_mode if runtime_decision else "heuristic",
                    "effective_mode": "ml" if ml_result is not None else "heuristic",
                    "runtime_health": runtime_decision.runtime_health if runtime_decision else "healthy",
                }
            )
            generated.append(
                GeneratedParlay(
                    candidates=combo,
                    leg_count=leg_count,
                    sport_scope=sport_scope,
                    participating_sports=participating_sports,
                    combined_market_price=combined_market_price,
                    combined_model_probability=combined_model_probability,
                    american_odds=american_odds,
                    edge=edge,
                    confidence=confidence,
                    selection_score=selection_score,
                    invalidation=invalidation,
                    rationale=rationale,
                    scoring_diagnostics={**diagnostics, "selection_score": selection_score},
                    model_name=active_lineage.model_name,
                    model_version=active_lineage.model_version,
                    calibration_version=active_lineage.calibration_version,
                    feature_set_version=active_lineage.feature_set_version,
                    model_metadata=model_metadata,
                )
            )

    generated.sort(key=lambda item: (-item.selection_score, -item.edge, -item.confidence, item.leg_count))
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
    generated = _build_generated_parlays(db, candidates)
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
            selection_score=item.selection_score,
            model_name=item.model_name,
            model_version=item.model_version,
            calibration_version=item.calibration_version,
            feature_set_version=item.feature_set_version,
            model_metadata=dict(item.model_metadata or {}),
            invalidation=item.invalidation,
            rationale=item.rationale,
            scoring_diagnostics=dict(item.scoring_diagnostics or {}),
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
            selection_score=item.selection_score,
            model_name=item.model_name,
            model_version=item.model_version,
            calibration_version=item.calibration_version,
            feature_set_version=item.feature_set_version,
            model_metadata=dict(item.model_metadata or {}),
            rationale=item.rationale,
            invalidation=item.invalidation,
            scoring_diagnostics=dict(item.scoring_diagnostics or {}),
            captured_at=captured_at,
        )
        db.add(prediction)
        db.flush()
        for leg_index, candidate in enumerate(item.candidates, start=1):
            db.add(_prediction_leg_from_candidate(prediction.id, leg_index, candidate))
        prediction_count += 1

    db.flush()
    return recommendation_count, prediction_count


def _empty_parlay_settlement_summary() -> dict[str, int]:
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


def _parlay_batch_cursor_filter(captured_at_field, id_field, cursor: dict[str, Any] | None):
    if not cursor:
        return None
    raw_captured_at = cursor.get("captured_at")
    raw_parlay_id = cursor.get("parlay_prediction_id")
    if not raw_captured_at or raw_parlay_id is None:
        return None
    cursor_captured_at = datetime.fromisoformat(str(raw_captured_at).replace("Z", "+00:00"))
    cursor_parlay_id = int(raw_parlay_id)
    return or_(
        captured_at_field > cursor_captured_at,
        and_(captured_at_field == cursor_captured_at, id_field > cursor_parlay_id),
    )


def _load_parlay_settlement_batch(
    db: Session,
    *,
    limit: int,
    cursor: dict[str, Any] | None = None,
) -> list[ParlayPrediction]:
    stmt = (
        select(ParlayPrediction)
        .options(joinedload(ParlayPrediction.legs).joinedload(ParlayPredictionLeg.source_prediction))
        .where(ParlayPrediction.settlement_status.in_(("pending", "unresolved")))
        .order_by(ParlayPrediction.captured_at.asc(), ParlayPrediction.id.asc())
    )
    cursor_filter = _parlay_batch_cursor_filter(ParlayPrediction.captured_at, ParlayPrediction.id, cursor)
    if cursor_filter is not None:
        stmt = stmt.where(cursor_filter)
    return db.scalars(stmt.limit(limit)).unique().all()


def _settle_parlay_rows(unsettled: list[ParlayPrediction]) -> dict[str, int]:
    summary = _empty_parlay_settlement_summary()
    summary["processed"] = len(unsettled)
    if not unsettled:
        return summary

    for parlay in unsettled:
        source_predictions = [leg.source_prediction for leg in parlay.legs if leg.source_prediction is not None]
        if len(source_predictions) != len(parlay.legs):
            # Bug #27: only count the row as updated when its
            # state actually changed. A parlay that's already
            # ``unresolved`` from a prior settlement pass would
            # otherwise inflate the operator-facing "updated"
            # counter on every cron tick.
            new_notes = "One or more source leg predictions are missing."
            if (
                parlay.settlement_status != "unresolved"
                or parlay.prediction_outcome != "unresolved"
                or parlay.settlement_notes != new_notes
            ):
                parlay.settlement_status = "unresolved"
                parlay.prediction_outcome = "unresolved"
                parlay.settlement_notes = new_notes
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

        # Bug #27: same state-change guard as the missing-source
        # branch above — a parlay that already failed to match a
        # recognized terminal pattern on a prior pass shouldn't
        # re-bump "updated" on every retry.
        new_notes = "Parlay settlement did not match a recognized terminal pattern."
        if (
            parlay.settlement_status != "unresolved"
            or parlay.prediction_outcome != "unresolved"
            or parlay.settlement_notes != new_notes
        ):
            parlay.settlement_status = "unresolved"
            parlay.prediction_outcome = "unresolved"
            parlay.settlement_notes = new_notes
            summary["updated"] += 1
        summary["unresolved"] += 1

    return summary


def settle_parlay_predictions_batch(
    db: Session,
    *,
    limit: int = 100,
    cursor: dict[str, Any] | None = None,
) -> tuple[dict[str, int], dict[str, Any] | None]:
    unsettled = _load_parlay_settlement_batch(db, limit=limit, cursor=cursor)
    if not unsettled:
        return _empty_parlay_settlement_summary(), None
    summary = _settle_parlay_rows(unsettled)
    db.flush()
    next_cursor = None
    if len(unsettled) >= limit:
        tail = unsettled[-1]
        next_cursor = {
            "captured_at": tail.captured_at.isoformat() if tail.captured_at is not None else None,
            "parlay_prediction_id": tail.id,
        }
    return summary, next_cursor


def settle_parlay_predictions(db: Session) -> dict[str, int]:
    combined = _empty_parlay_settlement_summary()
    cursor: dict[str, Any] | None = None
    while True:
        batch_summary, cursor = settle_parlay_predictions_batch(
            db,
            limit=100,
            cursor=cursor,
        )
        for key, value in batch_summary.items():
            combined[key] = combined.get(key, 0) + int(value or 0)
        if cursor is None:
            break
    return combined
