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

# Bug #20 — walk-forward worst-fold gating. The Brier headline reported
# on ``PromotionMetrics`` is the worst per-week-bucket value, not the
# aggregate; one favourable stretch should not be enough to promote.
# Low-volume families (game-winner markets at ~30 settled picks/week)
# auto-widen to 2-week buckets so each bucket clears the row floor.
# A family below the fold floor stays "insufficient history" and never
# promotes regardless of the aggregate Brier.
MIN_WALK_FORWARD_ROWS_PER_FOLD = 25
MIN_WALK_FORWARD_VALID_FOLDS = 8
WALK_FORWARD_WEEK_SIZES_DAYS = (7, 14)


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
    walk_forward_fold_count: int = 0
    walk_forward_window_days: int | None = None
    walk_forward_rows_per_fold: tuple[int, ...] = ()
    walk_forward_min_rows_per_fold: int = MIN_WALK_FORWARD_ROWS_PER_FOLD
    walk_forward_min_valid_folds: int = MIN_WALK_FORWARD_VALID_FOLDS
    insufficient_history: bool = False
    aggregate_heuristic_brier: float = 0.0
    aggregate_shadow_brier: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "heuristic_brier": self.heuristic_brier,
            "shadow_brier": self.shadow_brier,
            "heuristic_top_decile_roi": self.heuristic_top_decile_roi,
            "shadow_top_decile_roi": self.shadow_top_decile_roi,
            "walk_forward": {
                "fold_count": self.walk_forward_fold_count,
                "window_days": self.walk_forward_window_days,
                "rows_per_fold": list(self.walk_forward_rows_per_fold),
                "min_rows_per_fold": self.walk_forward_min_rows_per_fold,
                "min_valid_folds": self.walk_forward_min_valid_folds,
                "insufficient_history": self.insufficient_history,
                "metric": "worst_fold_brier",
                "aggregate_heuristic_brier": self.aggregate_heuristic_brier,
                "aggregate_shadow_brier": self.aggregate_shadow_brier,
            },
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


def _walk_forward_buckets(
    examples: list[PromotionExample],
    *,
    min_rows_per_fold: int = MIN_WALK_FORWARD_ROWS_PER_FOLD,
    min_valid_folds: int = MIN_WALK_FORWARD_VALID_FOLDS,
) -> tuple[list[list[int]], dict[str, Any]]:
    """Bucket examples by capture week (or fortnight) for walk-forward Brier.

    Returns ``(buckets, meta)``. ``buckets`` is a list of index lists —
    each inner list is the positions of ``examples`` that fall in one
    weekly (or biweekly) window. ``meta`` records the windowing choice
    and whether the fold floor was cleared.

    Unlike the training-side variant in ``apps/ml/ml/training.py``,
    the API-side gate has no train slice — the heuristic and shadow
    probabilities are already-recorded observations, so every bucket
    with enough rows is a valid "fold" for the worst-Brier check.
    """
    if not examples:
        return [], {
            "fold_count": 0,
            "window_days": None,
            "min_rows_per_fold": min_rows_per_fold,
            "min_valid_folds": min_valid_folds,
            "rows_per_fold": [],
            "insufficient_history": True,
        }

    timestamps: list[datetime] = []
    for example in examples:
        captured = example.captured_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        timestamps.append(captured)

    order = sorted(range(len(examples)), key=lambda i: timestamps[i])
    earliest = timestamps[order[0]]

    best_attempt: tuple[list[list[int]], dict[str, Any]] | None = None
    for window_days in WALK_FORWARD_WEEK_SIZES_DAYS:
        bucket_to_indices: dict[int, list[int]] = {}
        for position in order:
            offset_seconds = (timestamps[position] - earliest).total_seconds()
            bucket = int(offset_seconds // (window_days * 86400.0))
            bucket_to_indices.setdefault(bucket, []).append(position)

        buckets = [
            bucket_to_indices[bucket_id]
            for bucket_id in sorted(bucket_to_indices)
            if len(bucket_to_indices[bucket_id]) >= min_rows_per_fold
        ]
        meta = {
            "fold_count": len(buckets),
            "window_days": window_days,
            "min_rows_per_fold": min_rows_per_fold,
            "min_valid_folds": min_valid_folds,
            "rows_per_fold": [len(bucket) for bucket in buckets],
            "insufficient_history": len(buckets) < min_valid_folds,
        }
        if len(buckets) >= min_valid_folds:
            return buckets, meta
        if best_attempt is None or len(buckets) > len(best_attempt[0]):
            best_attempt = (buckets, meta)

    assert best_attempt is not None
    buckets, meta = best_attempt
    return buckets, {**meta, "insufficient_history": True}


def _per_bucket_brier(examples: list[PromotionExample], indices: list[int], *, model: str) -> float:
    fold_examples = [examples[i] for i in indices]
    return brier_score(fold_examples, model=model)


def metrics_for_examples(examples: list[PromotionExample]) -> PromotionMetrics:
    aggregate_heuristic = brier_score(examples, model="heuristic")
    aggregate_shadow = brier_score(examples, model="shadow")
    heuristic_roi = top_decile_roi(examples, model="heuristic")
    shadow_roi = top_decile_roi(examples, model="shadow")

    buckets, fold_meta = _walk_forward_buckets(examples)
    insufficient = bool(fold_meta["insufficient_history"])

    if insufficient:
        # Fall back to the aggregate so the runtime dashboard still
        # reflects something useful — the gate fails via the
        # ``insufficient_history`` flag regardless of these numbers.
        heuristic_brier = aggregate_heuristic
        shadow_brier = aggregate_shadow
    else:
        heuristic_brier = round(max(_per_bucket_brier(examples, idx, model="heuristic") for idx in buckets), 6)
        shadow_brier = round(max(_per_bucket_brier(examples, idx, model="shadow") for idx in buckets), 6)

    return PromotionMetrics(
        sample_count=len(examples),
        heuristic_brier=heuristic_brier,
        shadow_brier=shadow_brier,
        heuristic_top_decile_roi=heuristic_roi,
        shadow_top_decile_roi=shadow_roi,
        walk_forward_fold_count=int(fold_meta["fold_count"]),
        walk_forward_window_days=fold_meta["window_days"],
        walk_forward_rows_per_fold=tuple(int(v) for v in fold_meta["rows_per_fold"]),
        walk_forward_min_rows_per_fold=int(fold_meta["min_rows_per_fold"]),
        walk_forward_min_valid_folds=int(fold_meta["min_valid_folds"]),
        insufficient_history=insufficient,
        aggregate_heuristic_brier=aggregate_heuristic,
        aggregate_shadow_brier=aggregate_shadow,
    )


def evaluate_promotion_gates(
    metrics: PromotionMetrics,
    *,
    previous_stability_days: int = 0,
    same_evaluation_date: bool = False,
) -> PromotionGateResult:
    volume_passed = metrics.sample_count >= MIN_PROMOTION_SHADOW_SAMPLES
    # Bug #20 — calibration is gated on the *worst-fold* walk-forward
    # Brier. If walk-forward couldn't assemble the per-family minimum
    # (8 folds × ≥25 rows, with 2-week fall-back), the comparison is
    # untrustworthy and we fail calibration regardless of the headline
    # numbers — the family stays in shadow until enough history piles
    # up.
    calibration_passed = (
        not metrics.insufficient_history
        and metrics.shadow_brier <= metrics.heuristic_brier * BRIER_NOISE_BAND
    )
    ranking_passed = metrics.shadow_top_decile_roi >= metrics.heuristic_top_decile_roi
    first_three_passed = volume_passed and calibration_passed and ranking_passed
    stability_days = previous_stability_days if same_evaluation_date else (previous_stability_days + 1 if first_three_passed else 0)
    stability_passed = stability_days >= STABILITY_DAYS_REQUIRED
    reasons: list[str] = []
    if not volume_passed:
        reasons.append(f"Need {MIN_PROMOTION_SHADOW_SAMPLES}+ settled shadow predictions.")
    if metrics.insufficient_history:
        reasons.append(
            f"Need {MIN_WALK_FORWARD_VALID_FOLDS}+ walk-forward folds with "
            f"≥{MIN_WALK_FORWARD_ROWS_PER_FOLD} settled rows each "
            f"(got {metrics.walk_forward_fold_count})."
        )
    elif not calibration_passed:
        reasons.append("Worst-fold shadow Brier did not clear the heuristic Brier noise band.")
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
