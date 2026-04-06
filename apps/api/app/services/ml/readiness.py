from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ParlayPrediction, Prediction, ShadowInference, ShadowParlayInference
from app.services.ml.runtime import read_family_runtime
from app.services.model_families import FAMILY_DEFINITIONS, family_definition, parlay_family_key, single_family_key

MIN_SETTLED_FOR_REVIEW = 40
MIN_SHADOW_COVERAGE = 0.75


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _single_prediction_family_key(prediction: Prediction) -> str:
    return single_family_key(prediction.sport_key, prediction.market_family)


def _parlay_prediction_family_key(prediction: ParlayPrediction) -> str:
    return parlay_family_key(prediction.leg_count, prediction.participating_sports or [prediction.sport_scope])


def _shadow_single_family_key(item: ShadowInference) -> str:
    metadata = dict(item.model_metadata or {})
    if metadata.get("family_key"):
        return str(metadata["family_key"])
    return single_family_key(item.sport_key, item.market_family)


def _shadow_parlay_family_key(item: ShadowParlayInference) -> str:
    metadata = dict(item.model_metadata or {})
    if metadata.get("family_key"):
        return str(metadata["family_key"])
    return parlay_family_key(item.leg_count, item.participating_sports or [item.sport_scope])


def _bucket_rows(rows: list[Any], *, value_getter, buckets: list[tuple[str, float, float | None]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for label, lower, upper in buckets:
        selected = []
        for row in rows:
            value = value_getter(row)
            if value is None:
                continue
            if value < lower:
                continue
            if upper is not None and value >= upper:
                continue
            selected.append(row)
        won = sum(1 for row in selected if getattr(row, "prediction_outcome", None) == "won")
        lost = sum(1 for row in selected if getattr(row, "prediction_outcome", None) == "lost")
        push = sum(1 for row in selected if getattr(row, "prediction_outcome", None) == "push")
        cancelled = sum(1 for row in selected if getattr(row, "prediction_outcome", None) == "cancelled")
        realized = [float(row.realized_pnl) for row in selected if getattr(row, "realized_pnl", None) is not None]
        decisions = won + lost
        results.append(
            {
                "label": label,
                "total_count": len(selected),
                "won_count": won,
                "lost_count": lost,
                "push_count": push,
                "cancelled_count": cancelled,
                "win_rate": round(won / decisions, 4) if decisions else None,
                "average_realized_pnl": round(sum(realized) / len(realized), 4) if realized else None,
            }
        )
    return results


def _confidence_buckets(rows: list[Any]) -> list[dict[str, Any]]:
    return _bucket_rows(
        rows,
        value_getter=lambda row: getattr(row, "confidence", None),
        buckets=[
            ("<50%", 0.0, 0.5),
            ("50-59%", 0.5, 0.6),
            ("60-69%", 0.6, 0.7),
            ("70-79%", 0.7, 0.8),
            ("80%+", 0.8, None),
        ],
    )


def _edge_buckets(rows: list[Any]) -> list[dict[str, Any]]:
    return _bucket_rows(
        rows,
        value_getter=lambda row: getattr(row, "edge", None),
        buckets=[
            ("<0.05", 0.0, 0.05),
            ("0.05-0.09", 0.05, 0.10),
            ("0.10-0.14", 0.10, 0.15),
            ("0.15+", 0.15, None),
        ],
    )


def _rates_from_diagnostics(rows: list[Any]) -> tuple[dict[str, float], dict[str, float], dict[str, int]]:
    feature_hits: dict[str, int] = {}
    missing_hits: dict[str, int] = {}
    failure_hits: dict[str, int] = {}
    total_rows = max(len(rows), 1)
    for row in rows:
        diagnostics = dict(getattr(row, "scoring_diagnostics", None) or {})
        for key, value in dict(diagnostics.get("feature_flags") or {}).items():
            feature_hits[key] = feature_hits.get(key, 0) + (1 if value else 0)
        for key in list(diagnostics.get("missing_context") or []):
            missing_hits[key] = missing_hits.get(key, 0) + 1
        if getattr(row, "prediction_outcome", None) != "lost":
            continue
        for key, value in dict(diagnostics.get("penalties") or {}).items():
            if float(value or 0.0) > 0:
                failure_hits[key] = failure_hits.get(key, 0) + 1
        for key in list(diagnostics.get("missing_context") or []):
            failure_hits[key] = failure_hits.get(key, 0) + 1

    feature_rates = {key: round(value / total_rows, 4) for key, value in sorted(feature_hits.items())}
    missing_rates = {key: round(value / total_rows, 4) for key, value in sorted(missing_hits.items())}
    top_failures = dict(sorted(failure_hits.items(), key=lambda item: (-item[1], item[0]))[:5])
    return feature_rates, missing_rates, top_failures


def _readiness_status(
    *,
    desired_mode: str,
    total_predictions: int,
    settled_predictions: int,
    shadow_predictions: int,
    shadow_coverage_ratio: float,
) -> tuple[str, str]:
    if desired_mode == "ml":
        return "serving", "This family is configured to serve ML. Runtime health below shows whether it is currently falling back."
    if total_predictions and settled_predictions < MIN_SETTLED_FOR_REVIEW:
        return "insufficient_history", f"Only {settled_predictions} settled predictions are available; need {MIN_SETTLED_FOR_REVIEW} before review."
    if desired_mode in {"shadow", "ml"} and shadow_predictions == 0:
        return "shadow_not_started", "Shadow is configured but this family has not recorded any shadow inferences yet."
    if shadow_predictions > 0 and shadow_coverage_ratio < MIN_SHADOW_COVERAGE:
        return "shadowing", f"Shadow coverage is {shadow_coverage_ratio:.0%}; need at least {MIN_SHADOW_COVERAGE:.0%} before review."
    if shadow_predictions > 0 and settled_predictions >= MIN_SETTLED_FOR_REVIEW:
        return "ready_for_review", "Settled history and shadow coverage are high enough for a promotion review."
    return "heuristic_only", "This family is still operating on the heuristic path without active ML shadow coverage."


def _summary_for_family(
    db: Session,
    family_key: str,
    single_predictions: list[Prediction],
    parlay_predictions: list[ParlayPrediction],
    shadow_singles: list[ShadowInference],
    shadow_parlays: list[ShadowParlayInference],
) -> dict[str, Any]:
    definition = family_definition(family_key)
    scope = definition.scope
    runtime = read_family_runtime(db, family_key, scope=scope)
    all_predictions: list[Any] = single_predictions if scope == "single" else parlay_predictions
    shadows: list[Any] = shadow_singles if scope == "single" else shadow_parlays
    if scope == "single":
        coverage_predictions = [row for row in all_predictions if getattr(row, "capture_scope", "recommendation") == "coverage"]
        predictions = [row for row in all_predictions if getattr(row, "capture_scope", "recommendation") != "coverage"]
    else:
        coverage_predictions = []
        predictions = all_predictions
    total_predictions = len(predictions)
    settled = [row for row in predictions if getattr(row, "prediction_outcome", None) in {"won", "lost", "push", "cancelled"}]
    pending = [row for row in predictions if getattr(row, "prediction_outcome", None) == "pending"]
    coverage_settled = [
        row for row in coverage_predictions if getattr(row, "prediction_outcome", None) in {"won", "lost", "push", "cancelled"}
    ]
    coverage_pending = [row for row in coverage_predictions if getattr(row, "prediction_outcome", None) == "pending"]
    wins = sum(1 for row in predictions if getattr(row, "prediction_outcome", None) == "won")
    losses = sum(1 for row in predictions if getattr(row, "prediction_outcome", None) == "lost")
    pushes = sum(1 for row in predictions if getattr(row, "prediction_outcome", None) == "push")
    cancelled = sum(1 for row in predictions if getattr(row, "prediction_outcome", None) == "cancelled")
    edges = [float(getattr(row, "edge", 0.0)) for row in predictions]
    confidences = [float(getattr(row, "confidence", 0.0)) for row in predictions]
    pnls = [float(row.realized_pnl) for row in predictions if getattr(row, "realized_pnl", None) is not None]
    shadow_coverage_ratio = round(min(len(shadows) / total_predictions, 1.0), 4) if total_predictions else 0.0
    readiness_status, why_not_ready = _readiness_status(
        desired_mode=runtime.desired_mode,
        total_predictions=total_predictions,
        settled_predictions=len(settled),
        shadow_predictions=len(shadows),
        shadow_coverage_ratio=shadow_coverage_ratio,
    )
    feature_rates, missing_rates, top_failures = _rates_from_diagnostics(predictions)
    last_settled_at = max((row.settled_at for row in settled if row.settled_at is not None), default=None)

    return {
        "family_key": family_key,
        "label": definition.label,
        "scope": definition.scope,
        "sport_scope": definition.sport_scope,
        "leg_count": definition.leg_count,
        "readiness_status": readiness_status,
        "why_not_ready": why_not_ready,
        "runtime": {
            "family_key": family_key,
            "desired_mode": runtime.desired_mode,
            "effective_mode": runtime.effective_mode,
            "runtime_health": runtime.runtime_health,
            "fallback_active": runtime.fallback_active,
            "consecutive_failures": runtime.consecutive_failures,
            "last_check_at": runtime.last_check_at,
            "last_success_at": runtime.last_success_at,
            "last_error": runtime.last_error,
            "last_error_at": runtime.last_error_at,
            "artifact_path": runtime.artifact_path,
            "model_name": runtime.lineage.model_name,
            "model_version": runtime.lineage.model_version,
            "calibration_version": runtime.lineage.calibration_version,
            "feature_set_version": runtime.lineage.feature_set_version,
            "model_metadata": dict(runtime.lineage.model_metadata or {}),
        },
        "total_predictions": total_predictions,
        "settled_predictions": len(settled),
        "pending_predictions": len(pending),
        "coverage_predictions": len(coverage_predictions),
        "coverage_settled_predictions": len(coverage_settled),
        "coverage_pending_predictions": len(coverage_pending),
        "shadow_predictions": len(shadows),
        "shadow_coverage_ratio": shadow_coverage_ratio,
        "won_predictions": wins,
        "lost_predictions": losses,
        "push_predictions": pushes,
        "cancelled_predictions": cancelled,
        "average_edge": round(sum(edges) / len(edges), 4) if edges else None,
        "average_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
        "average_realized_pnl": round(sum(pnls) / len(pnls), 4) if pnls else None,
        "last_settled_at": last_settled_at,
        "confidence_buckets": _confidence_buckets(predictions),
        "edge_buckets": _edge_buckets(predictions),
        "feature_coverage_rates": feature_rates,
        "missing_context_rates": missing_rates,
        "top_failure_reasons": top_failures,
        "last_validation_failure": runtime.last_error,
        "last_fallback_event_at": runtime.last_error_at if runtime.fallback_active else None,
    }


def build_model_readiness_summary(db: Session) -> dict[str, Any]:
    single_predictions = db.scalars(select(Prediction)).all()
    parlay_predictions = db.scalars(select(ParlayPrediction)).all()
    shadow_singles = db.scalars(select(ShadowInference)).all()
    shadow_parlays = db.scalars(select(ShadowParlayInference)).all()

    singles_by_family: dict[str, list[Prediction]] = {}
    for prediction in single_predictions:
        singles_by_family.setdefault(_single_prediction_family_key(prediction), []).append(prediction)

    parlays_by_family: dict[str, list[ParlayPrediction]] = {}
    for prediction in parlay_predictions:
        parlays_by_family.setdefault(_parlay_prediction_family_key(prediction), []).append(prediction)

    shadow_singles_by_family: dict[str, list[ShadowInference]] = {}
    for item in shadow_singles:
        shadow_singles_by_family.setdefault(_shadow_single_family_key(item), []).append(item)

    shadow_parlays_by_family: dict[str, list[ShadowParlayInference]] = {}
    for item in shadow_parlays:
        shadow_parlays_by_family.setdefault(_shadow_parlay_family_key(item), []).append(item)

    families = []
    for definition in FAMILY_DEFINITIONS:
        families.append(
            _summary_for_family(
                db,
                definition.key,
                singles_by_family.get(definition.key, []),
                parlays_by_family.get(definition.key, []),
                shadow_singles_by_family.get(definition.key, []),
                shadow_parlays_by_family.get(definition.key, []),
            )
        )

    return {
        "generated_at": _now_utc(),
        "families": families,
    }


def build_model_readiness_detail(db: Session, family_key: str) -> dict[str, Any] | None:
    definition = family_definition(family_key)
    if definition.key != family_key:
        return None

    if definition.scope == "single":
        single_predictions = [
            prediction
            for prediction in db.scalars(select(Prediction)).all()
            if _single_prediction_family_key(prediction) == family_key
        ]
        shadow_singles = [
            item
            for item in db.scalars(select(ShadowInference)).all()
            if _shadow_single_family_key(item) == family_key
        ]
        return _summary_for_family(
            db,
            family_key,
            single_predictions,
            [],
            shadow_singles,
            [],
        )

    parlay_predictions = [
        prediction
        for prediction in db.scalars(select(ParlayPrediction)).all()
        if _parlay_prediction_family_key(prediction) == family_key
    ]
    shadow_parlays = [
        item
        for item in db.scalars(select(ShadowParlayInference)).all()
        if _shadow_parlay_family_key(item) == family_key
    ]
    return _summary_for_family(
        db,
        family_key,
        [],
        parlay_predictions,
        [],
        shadow_parlays,
    )
