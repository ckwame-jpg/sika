from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil, sqrt
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, load_only

from app.models import ModelFamilyRuntimeHealth, ParlayPrediction, Prediction, ShadowInference, ShadowParlayInference
from app.services.ml.shadow_modes import is_promotion_excluded_metadata
from app.services.model_families import FAMILY_DEFINITIONS, family_definition, parlay_family_key, single_family_key


MIN_PROMOTION_SHADOW_SAMPLES = 150
BRIER_DELTA_CONFIDENCE_Z = 1.64
BRIER_DELTA_ABSOLUTE_TOLERANCE = 0.005
BRIER_DELTA_RELATIVE_TOLERANCE = 0.02
STABILITY_DAYS_REQUIRED = 3
SETTLED_BINARY_OUTCOMES = {"won", "lost"}

# Walk-forward fold metrics are retained as diagnostics. Promotion itself
# uses paired current-lineage Brier deltas so a family is judged against
# the heuristic on the exact rows where shadow also made a prediction.
# Low-volume families auto-widen diagnostics to 2-week buckets so operators
# still get useful fold visibility when weekly buckets are too sparse.
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
    settled_at: datetime | None = None
    shadow_target: int | None = None
    shadow_market_price: float | None = None
    shadow_realized_pnl: float | None = None


@dataclass(frozen=True, slots=True)
class PromotionLineageIdentity:
    model_name: str
    model_version: str | None = None
    calibration_version: str | None = None
    feature_set_version: str | None = None
    artifact_signature: str | None = None
    artifact_path: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "calibration_version": self.calibration_version,
            "feature_set_version": self.feature_set_version,
            "artifact_signature": self.artifact_signature,
            "artifact_path": self.artifact_path,
        }


@dataclass(frozen=True, slots=True)
class PromotionMetrics:
    sample_count: int
    heuristic_brier: float
    shadow_brier: float
    heuristic_top_decile_roi: float
    shadow_top_decile_roi: float
    calibration_delta_mean: float = 0.0
    calibration_delta_standard_error: float = 0.0
    calibration_delta_upper_bound: float = 0.0
    calibration_tolerance: float = BRIER_DELTA_ABSOLUTE_TOLERANCE
    walk_forward_fold_count: int = 0
    walk_forward_window_days: int | None = None
    walk_forward_rows_per_fold: tuple[int, ...] = ()
    walk_forward_min_rows_per_fold: int = MIN_WALK_FORWARD_ROWS_PER_FOLD
    walk_forward_min_valid_folds: int = MIN_WALK_FORWARD_VALID_FOLDS
    insufficient_history: bool = False
    aggregate_heuristic_brier: float = 0.0
    aggregate_shadow_brier: float = 0.0
    worst_fold_heuristic_brier: float | None = None
    worst_fold_shadow_brier: float | None = None
    latest_settled_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        # Emit None for aggregate Brier when there are no samples — keeps
        # downstream consumers from mistaking ``brier_score([])==0.0`` for
        # a real zero-Brier signal.
        has_samples = self.sample_count > 0
        return {
            "sample_count": self.sample_count,
            "heuristic_brier": self.heuristic_brier,
            "shadow_brier": self.shadow_brier,
            "heuristic_top_decile_roi": self.heuristic_top_decile_roi,
            "shadow_top_decile_roi": self.shadow_top_decile_roi,
            "calibration_metric": "paired_brier_delta_upper_bound",
            "calibration_delta_mean": self.calibration_delta_mean if has_samples else None,
            "calibration_delta_standard_error": self.calibration_delta_standard_error if has_samples else None,
            "calibration_delta_upper_bound": self.calibration_delta_upper_bound if has_samples else None,
            "calibration_tolerance": self.calibration_tolerance if has_samples else None,
            "latest_settled_at": self.latest_settled_at.isoformat() if has_samples and self.latest_settled_at else None,
            "walk_forward": {
                "fold_count": self.walk_forward_fold_count,
                "window_days": self.walk_forward_window_days,
                "rows_per_fold": list(self.walk_forward_rows_per_fold),
                "min_rows_per_fold": self.walk_forward_min_rows_per_fold,
                "min_valid_folds": self.walk_forward_min_valid_folds,
                "insufficient_history": self.insufficient_history,
                "metric": "worst_fold_brier_diagnostic",
                "aggregate_heuristic_brier": self.aggregate_heuristic_brier if has_samples else None,
                "aggregate_shadow_brier": self.aggregate_shadow_brier if has_samples else None,
                "worst_fold_heuristic_brier": self.worst_fold_heuristic_brier,
                "worst_fold_shadow_brier": self.worst_fold_shadow_brier,
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


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def _non_heuristic_lineage_from_row(row: ModelFamilyRuntimeHealth) -> PromotionLineageIdentity | None:
    model_name = str(row.model_name or "").strip()
    if not model_name or model_name.startswith("heuristic"):
        return None
    metadata = dict(row.model_metadata or {})
    artifact_signature = str(metadata.get("artifact_signature") or "").strip() or None
    artifact_path = str(metadata.get("artifact_path") or "").strip() or None
    return PromotionLineageIdentity(
        model_name=model_name,
        model_version=row.model_version,
        calibration_version=row.calibration_version,
        feature_set_version=row.feature_set_version,
        artifact_signature=artifact_signature,
        artifact_path=artifact_path,
    )


def _lineage_metadata_matches(metadata: Any, lineage: PromotionLineageIdentity | None) -> bool:
    if lineage is None:
        return True
    row_metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
    if lineage.artifact_signature:
        return row_metadata.get("artifact_signature") == lineage.artifact_signature
    if lineage.artifact_path:
        return row_metadata.get("artifact_path") == lineage.artifact_path
    return True


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


def _apply_single_lineage_filter(statement, lineage: PromotionLineageIdentity | None):
    if lineage is None:
        return statement
    statement = statement.where(ShadowInference.model_name == lineage.model_name)
    if lineage.model_version is not None:
        statement = statement.where(ShadowInference.model_version == lineage.model_version)
    if lineage.calibration_version is not None:
        statement = statement.where(ShadowInference.calibration_version == lineage.calibration_version)
    if lineage.feature_set_version is not None:
        statement = statement.where(ShadowInference.feature_set_version == lineage.feature_set_version)
    return statement


def _apply_parlay_lineage_filter(statement, lineage: PromotionLineageIdentity | None):
    if lineage is None:
        return statement
    statement = statement.where(ShadowParlayInference.model_name == lineage.model_name)
    if lineage.model_version is not None:
        statement = statement.where(ShadowParlayInference.model_version == lineage.model_version)
    if lineage.calibration_version is not None:
        statement = statement.where(ShadowParlayInference.calibration_version == lineage.calibration_version)
    if lineage.feature_set_version is not None:
        statement = statement.where(ShadowParlayInference.feature_set_version == lineage.feature_set_version)
    return statement


def _opposite_binary_side(side: str) -> str:
    return "no" if side == "yes" else "yes"


def _shadow_target_for_sides(*, heuristic_side: str, shadow_side: str, target: int) -> int:
    if heuristic_side in {"yes", "no"} and shadow_side == _opposite_binary_side(heuristic_side):
        return 1 - target
    return target


def _shadow_market_price_for_sides(*, heuristic_side: str, shadow_side: str, market_price: float) -> float:
    if heuristic_side in {"yes", "no"} and shadow_side == _opposite_binary_side(heuristic_side):
        return round(1.0 - market_price, 4)
    return market_price


def _single_examples(
    db: Session,
    family_key: str,
    *,
    lineage: PromotionLineageIdentity | None = None,
    include_promotion_excluded: bool = False,
    only_promotion_excluded: bool = False,
) -> list[PromotionExample]:
    definition = family_definition(family_key)
    statement = (
        select(ShadowInference, Prediction)
        .options(
            load_only(
                ShadowInference.recommended_side,
                ShadowInference.fair_yes_price,
                ShadowInference.fair_no_price,
                ShadowInference.confidence,
                ShadowInference.captured_at,
                ShadowInference.model_name,
                ShadowInference.model_version,
                ShadowInference.calibration_version,
                ShadowInference.feature_set_version,
                ShadowInference.model_metadata,
            ),
            load_only(
                Prediction.sport_key,
                Prediction.market_family,
                Prediction.prediction_outcome,
                Prediction.suggested_price,
                Prediction.side,
                Prediction.fair_yes_price,
                Prediction.fair_no_price,
                Prediction.confidence,
                Prediction.realized_pnl,
                Prediction.settled_at,
                Prediction.captured_at,
            ),
        )
        .join(Prediction, ShadowInference.source_prediction_id == Prediction.id)
        .where(
            Prediction.prediction_outcome.in_(SETTLED_BINARY_OUTCOMES),
            ShadowInference.inference_scope == "single",
        )
    )
    statement = _apply_single_lineage_filter(statement, lineage)
    if definition.sport_scope in {"NBA", "MLB", "WNBA"}:
        statement = statement.where(Prediction.sport_key == definition.sport_scope)
    if family_key.endswith("_props"):
        statement = statement.where(Prediction.market_family == "player_prop")
    elif family_key.endswith("_singles"):
        statement = statement.where(or_(Prediction.market_family.is_(None), Prediction.market_family != "player_prop"))
    rows = db.execute(statement).all()
    examples: list[PromotionExample] = []
    for shadow, prediction in rows:
        excluded = is_promotion_excluded_metadata(shadow.model_metadata)
        if only_promotion_excluded and not excluded:
            continue
        if excluded and not include_promotion_excluded and not only_promotion_excluded:
            continue
        if not _lineage_metadata_matches(shadow.model_metadata, lineage):
            continue
        if single_family_key(prediction.sport_key, prediction.market_family) != family_key:
            continue
        target = 1 if prediction.prediction_outcome == "won" else 0
        market_price = _safe_float(prediction.suggested_price, default=0.5) or 0.5
        heuristic_side = str(prediction.side or "yes").strip().lower()
        shadow_side = str(shadow.recommended_side or prediction.side or "yes").strip().lower()
        shadow_target = _shadow_target_for_sides(
            heuristic_side=heuristic_side,
            shadow_side=shadow_side,
            target=target,
        )
        shadow_market_price = _shadow_market_price_for_sides(
            heuristic_side=heuristic_side,
            shadow_side=shadow_side,
            market_price=market_price,
        )
        heuristic_realized_pnl = _realized_pnl(target, market_price, prediction.realized_pnl)
        shadow_realized_pnl = (
            heuristic_realized_pnl
            if shadow_side == heuristic_side
            else _realized_pnl(shadow_target, shadow_market_price, None)
        )
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
                    fallback=shadow_market_price,
                ),
                market_price=market_price,
                realized_pnl=heuristic_realized_pnl,
                captured_at=shadow.captured_at or prediction.captured_at,
                settled_at=prediction.settled_at or shadow.captured_at or prediction.captured_at,
                shadow_target=shadow_target,
                shadow_market_price=shadow_market_price,
                shadow_realized_pnl=shadow_realized_pnl,
            )
        )
    return examples


def _parlay_examples(
    db: Session,
    family_key: str,
    *,
    lineage: PromotionLineageIdentity | None = None,
    include_promotion_excluded: bool = False,
    only_promotion_excluded: bool = False,
) -> list[PromotionExample]:
    definition = family_definition(family_key)
    statement = (
        select(ShadowParlayInference, ParlayPrediction)
        .options(
            load_only(
                ShadowParlayInference.combined_model_probability,
                ShadowParlayInference.captured_at,
                ShadowParlayInference.model_name,
                ShadowParlayInference.model_version,
                ShadowParlayInference.calibration_version,
                ShadowParlayInference.feature_set_version,
                ShadowParlayInference.model_metadata,
            ),
            load_only(
                ParlayPrediction.leg_count,
                ParlayPrediction.sport_scope,
                ParlayPrediction.participating_sports,
                ParlayPrediction.prediction_outcome,
                ParlayPrediction.combined_market_price,
                ParlayPrediction.combined_model_probability,
                ParlayPrediction.realized_pnl,
                ParlayPrediction.settled_at,
                ParlayPrediction.captured_at,
            ),
        )
        .join(ParlayPrediction, ShadowParlayInference.source_parlay_prediction_id == ParlayPrediction.id)
        .where(ParlayPrediction.prediction_outcome.in_(SETTLED_BINARY_OUTCOMES))
    )
    statement = _apply_parlay_lineage_filter(statement, lineage)
    if definition.leg_count is not None:
        statement = statement.where(ParlayPrediction.leg_count == definition.leg_count)
    if definition.sport_scope in {"NBA", "MLB"}:
        statement = statement.where(ParlayPrediction.sport_scope == definition.sport_scope)
    rows = db.execute(statement).all()
    examples: list[PromotionExample] = []
    for shadow, parlay in rows:
        excluded = is_promotion_excluded_metadata(shadow.model_metadata)
        if only_promotion_excluded and not excluded:
            continue
        if excluded and not include_promotion_excluded and not only_promotion_excluded:
            continue
        if not _lineage_metadata_matches(shadow.model_metadata, lineage):
            continue
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
                settled_at=parlay.settled_at or shadow.captured_at or parlay.captured_at,
            )
        )
    return examples


def paired_examples_for_family(
    db: Session,
    family_key: str,
    *,
    lineage: PromotionLineageIdentity | None = None,
    include_promotion_excluded: bool = False,
    only_promotion_excluded: bool = False,
) -> list[PromotionExample]:
    definition = family_definition(family_key)
    if definition.scope == "parlay":
        return _parlay_examples(
            db,
            family_key,
            lineage=lineage,
            include_promotion_excluded=include_promotion_excluded,
            only_promotion_excluded=only_promotion_excluded,
        )
    return _single_examples(
        db,
        family_key,
        lineage=lineage,
        include_promotion_excluded=include_promotion_excluded,
        only_promotion_excluded=only_promotion_excluded,
    )


def diagnostic_backfill_metrics_for_family(
    db: Session,
    family_key: str,
    *,
    lineage: PromotionLineageIdentity | None = None,
) -> PromotionMetrics:
    return metrics_for_examples(
        paired_examples_for_family(
            db,
            family_key,
            lineage=lineage,
            include_promotion_excluded=True,
            only_promotion_excluded=True,
        )
    )


def _example_probability(example: PromotionExample, *, model: str) -> float:
    return example.shadow_probability if model == "shadow" else example.heuristic_probability


def _example_target(example: PromotionExample, *, model: str) -> int:
    if model == "shadow" and example.shadow_target is not None:
        return example.shadow_target
    return example.target


def _example_market_price(example: PromotionExample, *, model: str) -> float:
    if model == "shadow" and example.shadow_market_price is not None:
        return example.shadow_market_price
    return example.market_price


def _example_realized_pnl(example: PromotionExample, *, model: str) -> float:
    if model == "shadow" and example.shadow_realized_pnl is not None:
        return example.shadow_realized_pnl
    return example.realized_pnl


def brier_score(examples: list[PromotionExample], *, model: str) -> float:
    if not examples:
        return 0.0
    return round(
        sum(
            (_example_probability(example, model=model) - _example_target(example, model=model)) ** 2
            for example in examples
        )
        / len(examples),
        6,
    )


def top_decile_roi(examples: list[PromotionExample], *, model: str) -> float:
    if not examples:
        return 0.0
    top_n = max(int(ceil(len(examples) * 0.10)), 1)
    selected = sorted(
        examples,
        key=lambda example: _example_probability(example, model=model) - _example_market_price(example, model=model),
        reverse=True,
    )[:top_n]
    return round(sum(_example_realized_pnl(example, model=model) for example in selected) / len(selected), 6)


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

    Unlike the training-side variant in ``apps/ml/ml/training.py``, this
    API-side diagnostic has no train slice — the heuristic and shadow
    probabilities are already-recorded observations, so every bucket with
    enough rows is a valid "fold" for worst-bucket visibility.
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


def _calibration_delta_stats(examples: list[PromotionExample]) -> tuple[float, float, float, float]:
    if not examples:
        return 0.0, 0.0, 0.0, BRIER_DELTA_ABSOLUTE_TOLERANCE

    deltas = [
        (_example_probability(example, model="shadow") - _example_target(example, model="shadow")) ** 2
        - (_example_probability(example, model="heuristic") - _example_target(example, model="heuristic")) ** 2
        for example in examples
    ]
    mean_delta = sum(deltas) / len(deltas)
    if len(deltas) <= 1:
        standard_error = 0.0
    else:
        variance = sum((delta - mean_delta) ** 2 for delta in deltas) / (len(deltas) - 1)
        standard_error = sqrt(variance / len(deltas))
    aggregate_heuristic = brier_score(examples, model="heuristic")
    tolerance = max(BRIER_DELTA_ABSOLUTE_TOLERANCE, BRIER_DELTA_RELATIVE_TOLERANCE * aggregate_heuristic)
    upper_bound = mean_delta + BRIER_DELTA_CONFIDENCE_Z * standard_error
    return round(mean_delta, 6), round(standard_error, 6), round(upper_bound, 6), round(tolerance, 6)


def metrics_for_examples(examples: list[PromotionExample]) -> PromotionMetrics:
    aggregate_heuristic = brier_score(examples, model="heuristic")
    aggregate_shadow = brier_score(examples, model="shadow")
    heuristic_roi = top_decile_roi(examples, model="heuristic")
    shadow_roi = top_decile_roi(examples, model="shadow")
    delta_mean, delta_standard_error, delta_upper_bound, delta_tolerance = _calibration_delta_stats(examples)

    buckets, fold_meta = _walk_forward_buckets(examples)
    insufficient = bool(fold_meta["insufficient_history"])

    worst_heuristic = None
    worst_shadow = None
    if not insufficient:
        worst_heuristic = round(max(_per_bucket_brier(examples, idx, model="heuristic") for idx in buckets), 6)
        worst_shadow = round(max(_per_bucket_brier(examples, idx, model="shadow") for idx in buckets), 6)
    settled_timestamps = [
        coerced
        for example in examples
        if (coerced := _coerce_utc(example.settled_at or example.captured_at)) is not None
    ]

    return PromotionMetrics(
        sample_count=len(examples),
        heuristic_brier=aggregate_heuristic,
        shadow_brier=aggregate_shadow,
        heuristic_top_decile_roi=heuristic_roi,
        shadow_top_decile_roi=shadow_roi,
        calibration_delta_mean=delta_mean,
        calibration_delta_standard_error=delta_standard_error,
        calibration_delta_upper_bound=delta_upper_bound,
        calibration_tolerance=delta_tolerance,
        walk_forward_fold_count=int(fold_meta["fold_count"]),
        walk_forward_window_days=fold_meta["window_days"],
        walk_forward_rows_per_fold=tuple(int(v) for v in fold_meta["rows_per_fold"]),
        walk_forward_min_rows_per_fold=int(fold_meta["min_rows_per_fold"]),
        walk_forward_min_valid_folds=int(fold_meta["min_valid_folds"]),
        insufficient_history=insufficient,
        aggregate_heuristic_brier=aggregate_heuristic,
        aggregate_shadow_brier=aggregate_shadow,
        worst_fold_heuristic_brier=worst_heuristic,
        worst_fold_shadow_brier=worst_shadow,
        latest_settled_at=max(settled_timestamps, default=None),
    )


def evaluate_promotion_gates(
    metrics: PromotionMetrics,
    *,
    previous_stability_days: int = 0,
    same_evaluation_date: bool = False,
    stability_increment_allowed: bool = True,
) -> PromotionGateResult:
    volume_passed = metrics.sample_count >= MIN_PROMOTION_SHADOW_SAMPLES
    calibration_passed = metrics.calibration_delta_upper_bound <= metrics.calibration_tolerance
    ranking_passed = metrics.shadow_top_decile_roi >= metrics.heuristic_top_decile_roi
    first_three_passed = volume_passed and calibration_passed and ranking_passed
    # ``same_evaluation_date`` is retained for older direct callers, but
    # normal family evaluation now gates counting with
    # ``stability_increment_allowed`` so a second same-day pass can count
    # only when new current-lineage settled evidence arrived.
    can_increment_stability = stability_increment_allowed and not same_evaluation_date
    stability_days = (
        previous_stability_days + 1
        if first_three_passed and can_increment_stability
        else previous_stability_days
        if first_three_passed
        else 0
    )
    stability_passed = stability_days >= STABILITY_DAYS_REQUIRED
    reasons: list[str] = []
    if not volume_passed:
        reasons.append(f"Need {MIN_PROMOTION_SHADOW_SAMPLES}+ settled shadow predictions.")
    if not calibration_passed:
        reasons.append("Shadow paired-Brier delta upper bound exceeded the promotion tolerance.")
    if not ranking_passed:
        reasons.append("Shadow top-decile ROI did not beat the heuristic ranking.")
    if first_three_passed and not can_increment_stability:
        reasons.append("Need newly settled current-lineage paired samples before counting another stability day.")
    elif first_three_passed and not stability_passed:
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


PROMOTION_GATE_VERSION = "paired_brier_delta_evidence_v2"


def _previous_metric_compatible(
    previous_payload: dict[str, Any],
    *,
    current_lineage: PromotionLineageIdentity | None,
) -> bool:
    """True when stored stability days were earned under the same gate and
    same model lineage currently being evaluated."""
    if previous_payload.get("gate_version") != PROMOTION_GATE_VERSION:
        return False
    if current_lineage is None:
        return True
    return previous_payload.get("lineage") == current_lineage.to_dict()


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _coerce_utc(parsed)


def _payload_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _has_new_countable_evidence(
    previous_payload: dict[str, Any],
    metrics: PromotionMetrics,
    *,
    previous_is_compatible: bool,
) -> bool:
    if metrics.sample_count <= 0:
        return False
    if not previous_is_compatible:
        return True
    previous_sample_count = _payload_int(previous_payload.get("last_counted_sample_count"))
    previous_latest = _parse_iso_datetime(previous_payload.get("last_counted_latest_settled_at"))
    if previous_sample_count <= 0:
        previous_metrics = dict(previous_payload.get("metrics") or {})
        previous_sample_count = _payload_int(previous_metrics.get("sample_count"))
        previous_latest = _parse_iso_datetime(previous_metrics.get("latest_settled_at"))
    if metrics.sample_count > previous_sample_count:
        return True
    if metrics.latest_settled_at is not None and (previous_latest is None or metrics.latest_settled_at > previous_latest):
        return True
    return False


def evaluate_family(db: Session, family_key: str, *, now: datetime | None = None) -> PromotionEvaluation:
    reference_now = now or _now_utc()
    row = _runtime_row(db, family_key)
    lineage = _non_heuristic_lineage_from_row(row)
    metrics = metrics_for_examples(paired_examples_for_family(db, family_key, lineage=lineage))
    previous_payload = dict(row.promotion_metrics or {})
    current_date = reference_now.date().isoformat()
    # Reset stability accumulation when the stored payload predates the
    # current gate or was earned by a different model lineage. A first
    # pass under the current gate/lineage may count immediately; later
    # passes need newly settled evidence.
    previous_is_compatible = _previous_metric_compatible(previous_payload, current_lineage=lineage)
    carryover_stability_days = int(row.promotion_stability_days or 0) if previous_is_compatible else 0
    counted_today = previous_is_compatible and previous_payload.get("last_counted_date") == current_date
    has_new_evidence = _has_new_countable_evidence(
        previous_payload,
        metrics,
        previous_is_compatible=previous_is_compatible,
    )
    gates = evaluate_promotion_gates(
        metrics,
        previous_stability_days=carryover_stability_days,
        stability_increment_allowed=has_new_evidence and not counted_today,
    )

    previous_mode = str(row.promotion_mode or "").strip().lower() or None
    lineage_changed_while_live = previous_mode == "ml" and not previous_is_compatible
    next_mode = "ml" if gates.promoted else "shadow" if lineage_changed_while_live else previous_mode
    if not gates.promoted and previous_mode not in {"ml", "shadow"}:
        next_mode = None

    row.promotion_mode = next_mode
    row.promotion_stability_days = gates.stability_days
    if gates.promoted:
        # The kill switch compares an aggregate rolling-50 Brier to this
        # baseline (apps/api/app/services/ml/kill_switch.py); store the
        # aggregate value so the comparison stays apples-to-apples.
        row.promotion_baseline_brier = metrics.aggregate_shadow_brier
    counted_date = previous_payload.get("last_counted_date") if previous_is_compatible else None
    counted_sample_count = previous_payload.get("last_counted_sample_count") if previous_is_compatible else None
    counted_latest_settled_at = previous_payload.get("last_counted_latest_settled_at") if previous_is_compatible else None
    counted_this_evaluation = gates.stability_days > carryover_stability_days
    if counted_this_evaluation:
        counted_date = current_date
        counted_sample_count = metrics.sample_count
        counted_latest_settled_at = metrics.latest_settled_at.isoformat() if metrics.latest_settled_at else None
    row.promotion_metrics = {
        "gate_version": PROMOTION_GATE_VERSION,
        "last_evaluation_date": current_date,
        "last_counted_date": counted_date,
        "last_counted_sample_count": counted_sample_count,
        "last_counted_latest_settled_at": counted_latest_settled_at,
        "lineage": lineage.to_dict() if lineage is not None else None,
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


def evaluate_family_if_due(db: Session, family_key: str, *, now: datetime | None = None) -> PromotionEvaluation | None:
    reference_now = now or _now_utc()
    row = _runtime_row(db, family_key)
    lineage = _non_heuristic_lineage_from_row(row)
    previous_payload = dict(row.promotion_metrics or {})
    current_date = reference_now.date().isoformat()
    if not _previous_metric_compatible(previous_payload, current_lineage=lineage):
        return evaluate_family(db, family_key, now=reference_now)
    if previous_payload.get("last_evaluation_date") != current_date:
        return evaluate_family(db, family_key, now=reference_now)
    metrics = metrics_for_examples(paired_examples_for_family(db, family_key, lineage=lineage))
    if not _has_new_countable_evidence(previous_payload, metrics, previous_is_compatible=True):
        return None
    return evaluate_family(db, family_key, now=reference_now)


def evaluate_all_families_if_due(db: Session, *, now: datetime | None = None) -> list[PromotionEvaluation]:
    reference_now = now or _now_utc()
    evaluations: list[PromotionEvaluation] = []
    for definition in FAMILY_DEFINITIONS:
        if definition.study_track != "active":
            continue
        evaluation = evaluate_family_if_due(db, definition.key, now=reference_now)
        if evaluation is not None:
            evaluations.append(evaluation)
    return evaluations
