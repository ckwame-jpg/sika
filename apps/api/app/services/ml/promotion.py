from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import ModelFamilyRuntimeHealth, ParlayPrediction, Prediction, ShadowInference, ShadowParlayInference
from app.services.model_families import FAMILY_DEFINITIONS, family_definition, parlay_family_key, single_family_key


MIN_PROMOTION_SHADOW_SAMPLES = 150
BRIER_NOISE_BAND = 1.02
STABILITY_DAYS_REQUIRED = 3
SETTLED_BINARY_OUTCOMES = {"won", "lost"}


@dataclass(frozen=True, slots=True)
class PromotionExample:
    target: int
    heuristic_probability: float
    shadow_probability: float
    market_price: float
    realized_pnl: float
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class PromotionMetrics:
    sample_count: int
    heuristic_brier: float
    shadow_brier: float
    heuristic_top_decile_roi: float
    shadow_top_decile_roi: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "heuristic_brier": self.heuristic_brier,
            "shadow_brier": self.shadow_brier,
            "heuristic_top_decile_roi": self.heuristic_top_decile_roi,
            "shadow_top_decile_roi": self.shadow_top_decile_roi,
        }


@dataclass(frozen=True, slots=True)
class PromotionGateResult:
    volume_passed: bool
    calibration_passed: bool
    ranking_passed: bool
    stability_passed: bool
    promoted: bool
    reasons: list[str]
    stability_days: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "volume_passed": self.volume_passed,
            "calibration_passed": self.calibration_passed,
            "ranking_passed": self.ranking_passed,
            "stability_passed": self.stability_passed,
            "promoted": self.promoted,
            "reasons": list(self.reasons),
            "stability_days": self.stability_days,
        }


@dataclass(frozen=True, slots=True)
class PromotionEvaluation:
    family_key: str
    metrics: PromotionMetrics
    gates: PromotionGateResult
    promotion_mode: str | None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:
        return default
    return min(max(result, 0.0), 1.0)


def _runtime_row(db: Session, family_key: str) -> ModelFamilyRuntimeHealth:
    row = db.scalar(select(ModelFamilyRuntimeHealth).where(ModelFamilyRuntimeHealth.family_key == family_key))
    if row is None:
        row = ModelFamilyRuntimeHealth(family_key=family_key)
        db.add(row)
        db.flush()
    return row


def _selected_single_probability(
    *,
    side: str | None,
    fair_yes_price: float | None,
    fair_no_price: float | None,
    confidence: float | None,
    fallback: float,
) -> float:
    normalized_side = (side or "yes").lower()
    if normalized_side == "no":
        direct = _safe_float(fair_no_price)
        if direct is not None:
            return direct
        yes_price = _safe_float(fair_yes_price)
        if yes_price is not None:
            return 1.0 - yes_price
    direct = _safe_float(fair_yes_price)
    if direct is not None:
        return direct
    confidence_value = _safe_float(confidence)
    return confidence_value if confidence_value is not None else fallback


def _realized_pnl(target: int, market_price: float, value: float | None) -> float:
    if value is not None:
        return float(value)
    return round(1.0 - market_price, 4) if target == 1 else round(-market_price, 4)


def _single_examples(db: Session, family_key: str) -> list[PromotionExample]:
    definition = family_definition(family_key)
    statement = (
        select(ShadowInference, Prediction)
        .join(Prediction, ShadowInference.source_prediction_id == Prediction.id)
        .where(
            Prediction.prediction_outcome.in_(SETTLED_BINARY_OUTCOMES),
            ShadowInference.inference_scope == "single",
        )
    )
    if definition.sport_scope in {"NBA", "MLB"}:
        statement = statement.where(Prediction.sport_key == definition.sport_scope)
    if family_key.endswith("_props"):
        statement = statement.where(Prediction.market_family == "player_prop")
    elif family_key.endswith("_singles"):
        statement = statement.where(or_(Prediction.market_family.is_(None), Prediction.market_family != "player_prop"))
    rows = db.execute(statement).all()
    examples: list[PromotionExample] = []
    for shadow, prediction in rows:
        if single_family_key(prediction.sport_key, prediction.market_family) != family_key:
            continue
        target = 1 if prediction.prediction_outcome == "won" else 0
        market_price = _safe_float(prediction.suggested_price, default=0.5) or 0.5
        examples.append(
            PromotionExample(
                target=target,
                heuristic_probability=_selected_single_probability(
                    side=prediction.side,
                    fair_yes_price=prediction.fair_yes_price,
                    fair_no_price=prediction.fair_no_price,
                    confidence=prediction.confidence,
                    fallback=market_price,
                ),
                shadow_probability=_selected_single_probability(
                    side=shadow.recommended_side or prediction.side,
                    fair_yes_price=shadow.fair_yes_price,
                    fair_no_price=shadow.fair_no_price,
                    confidence=shadow.confidence,
                    fallback=market_price,
                ),
                market_price=market_price,
                realized_pnl=_realized_pnl(target, market_price, prediction.realized_pnl),
                captured_at=shadow.captured_at or prediction.captured_at,
            )
        )
    return examples


def _parlay_examples(db: Session, family_key: str) -> list[PromotionExample]:
    definition = family_definition(family_key)
    statement = (
        select(ShadowParlayInference, ParlayPrediction)
        .join(ParlayPrediction, ShadowParlayInference.source_parlay_prediction_id == ParlayPrediction.id)
        .where(ParlayPrediction.prediction_outcome.in_(SETTLED_BINARY_OUTCOMES))
    )
    if definition.leg_count is not None:
        statement = statement.where(ParlayPrediction.leg_count == definition.leg_count)
    if definition.sport_scope in {"NBA", "MLB"}:
        statement = statement.where(ParlayPrediction.sport_scope == definition.sport_scope)
    rows = db.execute(statement).all()
    examples: list[PromotionExample] = []
    for shadow, parlay in rows:
        candidate_key = parlay_family_key(parlay.leg_count, parlay.participating_sports or [parlay.sport_scope])
        if candidate_key != family_key:
            continue
        target = 1 if parlay.prediction_outcome == "won" else 0
        market_price = _safe_float(parlay.combined_market_price, default=0.5) or 0.5
        examples.append(
            PromotionExample(
                target=target,
                heuristic_probability=_safe_float(parlay.combined_model_probability, default=market_price) or market_price,
                shadow_probability=_safe_float(shadow.combined_model_probability, default=market_price) or market_price,
                market_price=market_price,
                realized_pnl=_realized_pnl(target, market_price, parlay.realized_pnl),
                captured_at=shadow.captured_at or parlay.captured_at,
            )
        )
    return examples


def paired_examples_for_family(db: Session, family_key: str) -> list[PromotionExample]:
    definition = family_definition(family_key)
    if definition.scope == "parlay":
        return _parlay_examples(db, family_key)
    return _single_examples(db, family_key)


def brier_score(examples: list[PromotionExample], *, model: str) -> float:
    if not examples:
        return 0.0
    probabilities = [
        example.shadow_probability if model == "shadow" else example.heuristic_probability
        for example in examples
    ]
    return round(
        sum((probability - example.target) ** 2 for probability, example in zip(probabilities, examples, strict=True))
        / len(examples),
        6,
    )


def top_decile_roi(examples: list[PromotionExample], *, model: str) -> float:
    if not examples:
        return 0.0
    probability = (
        (lambda example: example.shadow_probability)
        if model == "shadow"
        else (lambda example: example.heuristic_probability)
    )
    top_n = max(int(ceil(len(examples) * 0.10)), 1)
    selected = sorted(examples, key=lambda example: probability(example) - example.market_price, reverse=True)[:top_n]
    return round(sum(example.realized_pnl for example in selected) / len(selected), 6)


def metrics_for_examples(examples: list[PromotionExample]) -> PromotionMetrics:
    return PromotionMetrics(
        sample_count=len(examples),
        heuristic_brier=brier_score(examples, model="heuristic"),
        shadow_brier=brier_score(examples, model="shadow"),
        heuristic_top_decile_roi=top_decile_roi(examples, model="heuristic"),
        shadow_top_decile_roi=top_decile_roi(examples, model="shadow"),
    )


def evaluate_promotion_gates(
    metrics: PromotionMetrics,
    *,
    previous_stability_days: int = 0,
    same_evaluation_date: bool = False,
) -> PromotionGateResult:
    volume_passed = metrics.sample_count >= MIN_PROMOTION_SHADOW_SAMPLES
    calibration_passed = metrics.shadow_brier <= metrics.heuristic_brier * BRIER_NOISE_BAND
    ranking_passed = metrics.shadow_top_decile_roi >= metrics.heuristic_top_decile_roi
    first_three_passed = volume_passed and calibration_passed and ranking_passed
    stability_days = previous_stability_days if same_evaluation_date else (previous_stability_days + 1 if first_three_passed else 0)
    stability_passed = stability_days >= STABILITY_DAYS_REQUIRED
    reasons: list[str] = []
    if not volume_passed:
        reasons.append(f"Need {MIN_PROMOTION_SHADOW_SAMPLES}+ settled shadow predictions.")
    if not calibration_passed:
        reasons.append("Shadow Brier did not clear the heuristic Brier noise band.")
    if not ranking_passed:
        reasons.append("Shadow top-decile ROI did not beat the heuristic ranking.")
    if first_three_passed and not stability_passed:
        reasons.append(f"Need {STABILITY_DAYS_REQUIRED} consecutive passing daily evaluations.")
    return PromotionGateResult(
        volume_passed=volume_passed,
        calibration_passed=calibration_passed,
        ranking_passed=ranking_passed,
        stability_passed=stability_passed,
        promoted=first_three_passed and stability_passed,
        reasons=reasons,
        stability_days=stability_days,
    )


def evaluate_family(db: Session, family_key: str, *, now: datetime | None = None) -> PromotionEvaluation:
    reference_now = now or _now_utc()
    row = _runtime_row(db, family_key)
    metrics = metrics_for_examples(paired_examples_for_family(db, family_key))
    previous_payload = dict(row.promotion_metrics or {})
    current_date = reference_now.date().isoformat()
    gates = evaluate_promotion_gates(
        metrics,
        previous_stability_days=int(row.promotion_stability_days or 0),
        same_evaluation_date=previous_payload.get("last_evaluation_date") == current_date,
    )

    previous_mode = str(row.promotion_mode or "").strip().lower() or None
    next_mode = "ml" if gates.promoted else previous_mode
    if not gates.promoted and previous_mode not in {"ml", "shadow"}:
        next_mode = None

    row.promotion_mode = next_mode
    row.promotion_stability_days = gates.stability_days
    if gates.promoted:
        row.promotion_baseline_brier = metrics.shadow_brier
    row.promotion_metrics = {
        "last_evaluation_date": current_date,
        "metrics": metrics.to_dict(),
        "gates": gates.to_dict(),
    }
    row.promotion_updated_at = reference_now
    db.flush()
    return PromotionEvaluation(
        family_key=family_key,
        metrics=metrics,
        gates=gates,
        promotion_mode=next_mode,
    )


def evaluate_all_families(db: Session, *, now: datetime | None = None) -> list[PromotionEvaluation]:
    reference_now = now or _now_utc()
    evaluations: list[PromotionEvaluation] = []
    for definition in FAMILY_DEFINITIONS:
        if definition.study_track != "active":
            continue
        evaluations.append(evaluate_family(db, definition.key, now=reference_now))
    return evaluations
