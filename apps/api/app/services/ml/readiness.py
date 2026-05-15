from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only, selectinload

from app.models import ParlayPrediction, Prediction, ShadowInference, ShadowParlayInference
from app.services.clv import average_clv
from app.services.ml.promotion import MIN_PROMOTION_SHADOW_SAMPLES, STABILITY_DAYS_REQUIRED
from app.services.ml.runtime import read_family_runtime, shadow_capture_blocker
from app.services.ml.study_progress import (
    MIN_SETTLED_FOR_PROMOTION_REVIEW,
    MIN_SETTLED_FOR_REVIEW,
    MIN_SHADOW_COVERAGE,
    SETTLED_OUTCOMES,
    history_ready_for_shadow,
    retained_study_cutoff,
    shadow_coverage_ratio,
    shadow_coverage_ready,
    walk_forward_history_ready,
)
from app.services.model_families import FAMILY_DEFINITIONS, family_definition, parlay_family_key, single_family_key
from app.services.operator_settings import (
    effective_ml_serving_mode,
    effective_narrator_enabled,
    effective_pick_history_default_n,
    effective_sportsbook_disagreement_min_book_count,
    effective_sportsbook_disagreement_threshold,
)
from app.services.predictions import compute_settlement_aging

# Sample size for diagnostic aggregations (buckets, rates, recent-row averages).
# Headline counts come from SQL aggregation and are unaffected by this limit.
READINESS_ROW_LIMIT = 5_000


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return number


def _average_metric(values: list[float]) -> float | None:
    finite_values = [value for value in values if isfinite(value)]
    if not finite_values:
        return None
    return round(sum(finite_values) / len(finite_values), 4)


def _runtime_mode(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"heuristic", "shadow", "ml"} else "heuristic"


def _runtime_health(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"healthy", "degraded", "unavailable"} else "unavailable"


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


def _single_shadow_fallback_key(run_id: int | None, market_id: int | None, ticker: str) -> tuple[int | None, int | None, str]:
    return run_id, market_id, ticker


def _parlay_shadow_fallback_key(run_id: int | None, leg_count: int, leg_tickers: list[str] | tuple[str, ...]) -> tuple[int | None, int, tuple[str, ...]]:
    return run_id, leg_count, tuple(str(ticker) for ticker in leg_tickers)


def _single_shadow_coverage_count(predictions: list[Prediction], shadows: list[ShadowInference]) -> tuple[int, int]:
    linked_prediction_ids = {int(item.source_prediction_id) for item in shadows if item.source_prediction_id is not None}
    fallback_keys = {
        _single_shadow_fallback_key(item.run_id, item.market_id, item.ticker)
        for item in shadows
        if item.source_prediction_id is None
    }
    covered = 0
    backlog = 0
    for prediction in predictions:
        if prediction.id in linked_prediction_ids or _single_shadow_fallback_key(prediction.run_id, prediction.market_id, prediction.ticker) in fallback_keys:
            covered += 1
        else:
            backlog += 1
    return covered, backlog


def _parlay_shadow_coverage_count(predictions: list[ParlayPrediction], shadows: list[ShadowParlayInference]) -> tuple[int, int]:
    linked_prediction_ids = {int(item.source_parlay_prediction_id) for item in shadows if item.source_parlay_prediction_id is not None}
    fallback_keys = {
        _parlay_shadow_fallback_key(item.run_id, item.leg_count, item.leg_tickers or [])
        for item in shadows
        if item.source_parlay_prediction_id is None
    }
    covered = 0
    backlog = 0
    for prediction in predictions:
        if prediction.id in linked_prediction_ids or _parlay_shadow_fallback_key(
            prediction.run_id,
            prediction.leg_count,
            [leg.ticker for leg in prediction.legs],
        ) in fallback_keys:
            covered += 1
        else:
            backlog += 1
    return covered, backlog


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


# -- Smarter #1: per-family reliability-curve buckets --------------------------
#
# Aggregate Brier hides bucket-level miscalibration (e.g. 8-point over-
# confidence in the 60-70% band). The buckets below partition settled
# predictions by the model's P(YES) and surface the gap between predicted
# probability and observed YES rate, per bucket. Consumed by the readiness
# panel's reliability curve.
#
# Note on scope: we recompute live from the per-family row sample
# (``READINESS_ROW_LIMIT`` predictions max). A precomputed
# ``prediction_calibration_buckets`` table would be needed only if the row
# sample stops being representative — bug #19's retention archive now keeps
# every settled prediction, so longer windows can be served on demand by
# widening the readiness query rather than by adding a new table.

CALIBRATION_BUCKETS_BOUNDARIES: list[tuple[str, float, float]] = [
    ("0-10%", 0.0, 0.1),
    ("10-20%", 0.1, 0.2),
    ("20-30%", 0.2, 0.3),
    ("30-40%", 0.3, 0.4),
    ("40-50%", 0.4, 0.5),
    ("50-60%", 0.5, 0.6),
    ("60-70%", 0.6, 0.7),
    ("70-80%", 0.7, 0.8),
    ("80-90%", 0.8, 0.9),
    ("90-100%", 0.9, 1.0),
]


def _did_yes_happen(row: Any) -> bool | None:
    """Return whether the model's predicted outcome actually occurred for
    ``row``.

    For single predictions the predicted probability is P(YES), and the
    picked side is a separate signal: a NO-side prediction that ``won``
    means YES did NOT happen (we have to invert the outcome relative to the
    probability). For parlay predictions there is no row-level ``side`` —
    ``combined_model_probability`` is already the joint probability of the
    chosen leg combination, so ``outcome == "won"`` means the predicted
    joint event happened directly.

    Returns ``None`` for push / cancelled / pending / rows whose ``side`` is
    set to anything other than ``"yes"`` / ``"no"`` — those rows cannot
    inform calibration.
    """

    outcome = str(getattr(row, "prediction_outcome", None) or "").lower()
    if outcome not in ("won", "lost"):
        return None
    raw_side = getattr(row, "side", None)
    if raw_side is None:
        # Parlay rows have no per-row ``side`` column. ``combined_model_probability``
        # already encodes the joint probability for the chosen combination, so
        # ``outcome == "won"`` matches the YES axis directly.
        return outcome == "won"
    side = str(raw_side or "").lower()
    if side == "yes":
        return outcome == "won"
    if side == "no":
        return outcome == "lost"
    return None


def _predicted_yes_probability(row: Any) -> float | None:
    """Return the model's predicted P(YES) for this prediction.

    Single predictions store this in ``fair_yes_price``; parlay predictions
    use ``combined_model_probability`` (the joint probability across all
    legs). NaN / infinite / unparseable values yield ``None``. Finite but
    out-of-range values (``< 0`` or ``> 1``) also yield ``None`` — they
    cannot inform calibration and silently dropping them prevents a
    malformed model output from corrupting bucket averages.
    """

    raw = _safe_float(getattr(row, "fair_yes_price", None))
    if raw is None:
        raw = _safe_float(getattr(row, "combined_model_probability", None))
    if raw is None:
        return None
    if raw < 0.0 or raw > 1.0:
        return None
    return raw


def _calibration_buckets(rows: list[Any]) -> list[dict[str, Any]]:
    """Bucket rows by predicted P(YES) and report observed vs. predicted.

    Each row contributes ``(predicted_yes_probability, did_yes_happen)``.
    Rows without a decided yes/no outcome (push, cancelled, pending) or
    without a parseable predicted probability are skipped — they cannot
    inform calibration. Per bucket we report:

    - ``label`` (e.g. ``"60-70%"``)
    - ``settled_count`` — rows in the bucket with both a probability and a
      decided outcome
    - ``avg_predicted`` — mean of in-bucket predicted probabilities
    - ``actual_yes_rate`` — fraction of in-bucket rows where YES actually
      happened
    - ``miscalibration`` — ``avg_predicted - actual_yes_rate``. Positive means
      the model was over-confident in YES inside this band; negative means
      under-confident. ``None`` when the bucket is empty.

    The boundaries are lower-inclusive / upper-exclusive (``[lower, upper)``)
    except the final bucket, which is fully inclusive so a perfect-confidence
    row (``p == 1.0``) is still bucketed.
    """

    last_upper = CALIBRATION_BUCKETS_BOUNDARIES[-1][2]
    results: list[dict[str, Any]] = []
    for label, lower, upper in CALIBRATION_BUCKETS_BOUNDARIES:
        is_last_bucket = upper == last_upper
        in_bucket: list[tuple[float, bool]] = []
        for row in rows:
            proba = _predicted_yes_probability(row)
            if proba is None:
                continue
            if proba < lower:
                continue
            if is_last_bucket:
                if proba > upper:
                    continue
            else:
                if proba >= upper:
                    continue
            yes = _did_yes_happen(row)
            if yes is None:
                continue
            in_bucket.append((proba, yes))
        count = len(in_bucket)
        if count == 0:
            results.append(
                {
                    "label": label,
                    "settled_count": 0,
                    "avg_predicted": None,
                    "actual_yes_rate": None,
                    "miscalibration": None,
                }
            )
            continue
        avg_predicted = sum(proba for proba, _ in in_bucket) / count
        yes_count = sum(1 for _, yes in in_bucket if yes)
        actual_rate = yes_count / count
        results.append(
            {
                "label": label,
                "settled_count": count,
                "avg_predicted": round(avg_predicted, 4),
                "actual_yes_rate": round(actual_rate, 4),
                "miscalibration": round(avg_predicted - actual_rate, 4),
            }
        )
    return results


def _single_outcome_counts(db: Session, *, cutoff: datetime) -> dict[str, dict[str, dict[str, int]]]:
    """Per-family per-scope per-outcome counts of single predictions in the readiness window.

    Returned shape: ``{family_key: {capture_scope: {outcome: count}}}``.
    """
    capture_scope = func.coalesce(Prediction.capture_scope, "recommendation").label("scope")
    outcome = func.coalesce(Prediction.prediction_outcome, "pending").label("outcome")
    stmt = (
        select(
            Prediction.sport_key,
            Prediction.market_family,
            capture_scope,
            outcome,
            func.count(Prediction.id),
        )
        .where(Prediction.captured_at >= cutoff)
        .group_by(Prediction.sport_key, Prediction.market_family, capture_scope, outcome)
    )
    by_family: dict[str, dict[str, dict[str, int]]] = {}
    for sport_key, market_family, scope, outcome_value, count in db.execute(stmt).all():
        family_key = single_family_key(sport_key, market_family)
        scope_bucket = by_family.setdefault(family_key, {}).setdefault(str(scope), {})
        scope_bucket[str(outcome_value)] = scope_bucket.get(str(outcome_value), 0) + int(count or 0)
    return by_family


def _single_shadow_match_counts(db: Session, *, cutoff: datetime) -> dict[str, int]:
    """Per-family count of in-window predictions that have a matching shadow inference."""
    matched_ids = (
        select(ShadowInference.source_prediction_id)
        .where(ShadowInference.captured_at >= cutoff)
        .where(ShadowInference.source_prediction_id.is_not(None))
    )
    stmt = (
        select(
            Prediction.sport_key,
            Prediction.market_family,
            func.count(Prediction.id),
        )
        .where(Prediction.captured_at >= cutoff)
        .where(Prediction.id.in_(matched_ids))
        .group_by(Prediction.sport_key, Prediction.market_family)
    )
    by_family: dict[str, int] = {}
    for sport_key, market_family, count in db.execute(stmt).all():
        family_key = single_family_key(sport_key, market_family)
        by_family[family_key] = by_family.get(family_key, 0) + int(count or 0)
    return by_family


def _parlay_outcome_counts(db: Session, *, cutoff: datetime) -> dict[str, dict[str, int]]:
    """Per-family per-outcome counts for parlay predictions in the readiness window.

    Group key uses ``sport_scope`` (the same fallback the row-iterator uses when
    ``participating_sports`` is empty) so we can aggregate without unnesting JSON.
    """
    outcome = func.coalesce(ParlayPrediction.prediction_outcome, "pending").label("outcome")
    stmt = (
        select(
            ParlayPrediction.leg_count,
            ParlayPrediction.sport_scope,
            outcome,
            func.count(ParlayPrediction.id),
        )
        .where(ParlayPrediction.captured_at >= cutoff)
        .group_by(ParlayPrediction.leg_count, ParlayPrediction.sport_scope, outcome)
    )
    by_family: dict[str, dict[str, int]] = {}
    for leg_count, sport_scope, outcome_value, count in db.execute(stmt).all():
        family_key = parlay_family_key(int(leg_count), [sport_scope or "MIXED"])
        bucket = by_family.setdefault(family_key, {})
        bucket[str(outcome_value)] = bucket.get(str(outcome_value), 0) + int(count or 0)
    return by_family


def _parlay_shadow_match_counts(db: Session, *, cutoff: datetime) -> dict[str, int]:
    matched_ids = (
        select(ShadowParlayInference.source_parlay_prediction_id)
        .where(ShadowParlayInference.captured_at >= cutoff)
        .where(ShadowParlayInference.source_parlay_prediction_id.is_not(None))
    )
    stmt = (
        select(
            ParlayPrediction.leg_count,
            ParlayPrediction.sport_scope,
            func.count(ParlayPrediction.id),
        )
        .where(ParlayPrediction.captured_at >= cutoff)
        .where(ParlayPrediction.id.in_(matched_ids))
        .group_by(ParlayPrediction.leg_count, ParlayPrediction.sport_scope)
    )
    by_family: dict[str, int] = {}
    for leg_count, sport_scope, count in db.execute(stmt).all():
        family_key = parlay_family_key(int(leg_count), [sport_scope or "MIXED"])
        by_family[family_key] = by_family.get(family_key, 0) + int(count or 0)
    return by_family


def _scope_total(scope_counts: dict[str, int]) -> int:
    return sum(scope_counts.values())


def _scope_settled(scope_counts: dict[str, int]) -> int:
    return sum(scope_counts.get(outcome, 0) for outcome in SETTLED_OUTCOMES)


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
    db: Session,
    family_key: str,
    scope: str,
    study_track: str,
    desired_mode: str,
    settled_predictions: int,
    shadow_predictions: int,
    shadow_coverage_ratio: float,
) -> tuple[str, str]:
    if study_track != "active":
        return "heuristic_only", "This family is not in the active ML study track and stays on the heuristic path."
    if desired_mode == "ml":
        return "serving", "This family is configured to serve ML. Runtime health below shows whether it is currently falling back."
    if not history_ready_for_shadow(settled_predictions):
        return "insufficient_history", (
            f"This family is in the active ML study track. Only {settled_predictions} settled predictions are available; "
            f"need {MIN_SETTLED_FOR_REVIEW} before review."
        )
    if shadow_predictions == 0:
        blocker = shadow_capture_blocker(family_key, scope=scope, db=db)
        if blocker:
            return "shadow_not_started", blocker
        return "shadow_not_started", (
            "This family has enough settled history and is shadow-eligible, but no shadow samples have been recorded yet."
        )
    if not shadow_coverage_ready(shadow_coverage_ratio):
        return "shadowing", f"Shadow coverage is {shadow_coverage_ratio:.0%}; need at least {MIN_SHADOW_COVERAGE:.0%} before review."
    # Bug #20 walk-forward gate needs ≥200 settled rows spread across ≥8
    # weekly folds; advancing to ``ready_for_review`` before that floor
    # is operator-misleading — the gate will keep returning
    # ``insufficient_history`` until enough weeks accumulate. Hold the
    # status at ``history_accumulating`` until the per-family settled
    # count clears the walk-forward floor.
    if not walk_forward_history_ready(settled_predictions):
        return "history_accumulating", (
            f"Shadow coverage is high, but only {settled_predictions} settled predictions are available. "
            f"The walk-forward promotion gate needs ≥{MIN_SETTLED_FOR_PROMOTION_REVIEW} rows "
            f"across ≥8 weeks before it can evaluate. Keep collecting settled history; "
            f"the status will advance to ``ready_for_review`` once the floor is cleared."
        )
    return "ready_for_review", (
        "Settled history and shadow coverage are high enough for a promotion review. This does not enable live ML serving until desired mode is set to ml."
    )


def _summary_for_family(
    db: Session,
    family_key: str,
    single_predictions: list[Prediction],
    parlay_predictions: list[ParlayPrediction],
    shadow_singles: list[ShadowInference],
    shadow_parlays: list[ShadowParlayInference],
    *,
    single_outcome_counts: dict[str, dict[str, dict[str, int]]] | None = None,
    parlay_outcome_counts: dict[str, dict[str, int]] | None = None,
    single_shadow_match_counts: dict[str, int] | None = None,
    parlay_shadow_match_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    definition = family_definition(family_key)
    scope = definition.scope
    runtime = read_family_runtime(db, family_key, scope=scope)
    all_predictions: list[Any] = single_predictions if scope == "single" else parlay_predictions
    shadows: list[Any] = shadow_singles if scope == "single" else shadow_parlays
    if scope == "single":
        coverage_predictions = [row for row in all_predictions if getattr(row, "capture_scope", "recommendation") == "coverage"]
        predictions = [row for row in all_predictions if getattr(row, "capture_scope", "recommendation") != "coverage"]
        # Headline counts come from SQL aggregation (`single_outcome_counts`) so they
        # stay accurate beyond the diagnostic row sample. The row lists above remain
        # the basis for averages, buckets, and feature-rate diagnostics.
        scope_counts = (single_outcome_counts or {}).get(family_key, {})
        rec_counts = scope_counts.get("recommendation", {})
        cov_counts = scope_counts.get("coverage", {})
        total_predictions = _scope_total(rec_counts)
        settled_count = _scope_settled(rec_counts)
        pending_count = rec_counts.get("pending", 0)
        wins = rec_counts.get("won", 0)
        losses = rec_counts.get("lost", 0)
        pushes = rec_counts.get("push", 0)
        cancelled = rec_counts.get("cancelled", 0)
        coverage_total = _scope_total(cov_counts)
        coverage_settled_count = _scope_settled(cov_counts)
        coverage_pending_count = cov_counts.get("pending", 0)
        gate_settled_predictions = settled_count + coverage_settled_count
        gate_total = total_predictions + coverage_total
        covered_shadow_predictions = (single_shadow_match_counts or {}).get(family_key, 0)
        shadow_backlog_predictions = max(gate_total - covered_shadow_predictions, 0)
        shadow_backlog_parlays = 0
    else:
        coverage_predictions = []
        predictions = all_predictions
        family_outcomes = (parlay_outcome_counts or {}).get(family_key, {})
        total_predictions = _scope_total(family_outcomes)
        settled_count = _scope_settled(family_outcomes)
        pending_count = family_outcomes.get("pending", 0)
        wins = family_outcomes.get("won", 0)
        losses = family_outcomes.get("lost", 0)
        pushes = family_outcomes.get("push", 0)
        cancelled = family_outcomes.get("cancelled", 0)
        coverage_total = 0
        coverage_settled_count = 0
        coverage_pending_count = 0
        gate_settled_predictions = settled_count
        gate_total = total_predictions
        covered_shadow_predictions = (parlay_shadow_match_counts or {}).get(family_key, 0)
        shadow_backlog_parlays = max(gate_total - covered_shadow_predictions, 0)
        shadow_backlog_predictions = 0
    edges = [value for row in predictions if (value := _safe_float(getattr(row, "edge", None))) is not None]
    confidences = [value for row in predictions if (value := _safe_float(getattr(row, "confidence", None))) is not None]
    pnls = [value for row in predictions if (value := _safe_float(getattr(row, "realized_pnl", None))) is not None]
    desired_mode = _runtime_mode(runtime.desired_mode)
    effective_mode = _runtime_mode(runtime.effective_mode)
    runtime_health = _runtime_health(runtime.runtime_health)
    shadow_ratio = shadow_coverage_ratio(total_predictions=gate_total, shadow_predictions=covered_shadow_predictions)
    readiness_status, why_not_ready = _readiness_status(
        db=db,
        family_key=family_key,
        scope=scope,
        study_track=definition.study_track,
        desired_mode=desired_mode,
        settled_predictions=gate_settled_predictions,
        shadow_predictions=covered_shadow_predictions,
        shadow_coverage_ratio=shadow_ratio,
    )
    feature_rates, missing_rates, top_failures = _rates_from_diagnostics(predictions)
    settled_rows = [row for row in predictions if getattr(row, "prediction_outcome", None) in SETTLED_OUTCOMES]
    last_settled_at = max((row.settled_at for row in settled_rows if row.settled_at is not None), default=None)
    last_shadow_capture_at = max((row.captured_at for row in shadows if row.captured_at is not None), default=None)

    return {
        "family_key": family_key,
        "label": definition.label,
        "scope": definition.scope,
        "sport_scope": definition.sport_scope,
        "leg_count": definition.leg_count,
        "study_track": definition.study_track,
        "readiness_status": readiness_status,
        "why_not_ready": why_not_ready,
        "runtime": {
            "family_key": family_key,
            "desired_mode": desired_mode,
            "effective_mode": effective_mode,
            "runtime_health": runtime_health,
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
            "promotion_mode": runtime.promotion_mode,
            "promotion_stability_days": runtime.promotion_stability_days,
            "promotion_baseline_brier": runtime.promotion_baseline_brier,
            "promotion_metrics": dict(runtime.promotion_metrics or {}),
            "promotion_updated_at": runtime.promotion_updated_at,
        },
        "total_predictions": total_predictions,
        "settled_predictions": settled_count,
        "pending_predictions": pending_count,
        "coverage_predictions": coverage_total,
        "coverage_settled_predictions": coverage_settled_count,
        "coverage_pending_predictions": coverage_pending_count,
        "shadow_predictions": covered_shadow_predictions,
        "shadow_coverage_ratio": shadow_ratio,
        "shadow_backlog_predictions": shadow_backlog_predictions,
        "shadow_backlog_parlays": shadow_backlog_parlays,
        "last_shadow_capture_at": last_shadow_capture_at,
        "won_predictions": wins,
        "lost_predictions": losses,
        "push_predictions": pushes,
        "cancelled_predictions": cancelled,
        "average_edge": _average_metric(edges),
        "average_confidence": _average_metric(confidences),
        "average_realized_pnl": _average_metric(pnls),
        # Smarter #3 — signed mean closing-line value across settled
        # predictions in this family's row sample. Singles only: parlays
        # don't carry a per-row ``closing_line_value`` because closing
        # prices are per-market and a parlay covers multiple. ``None``
        # when no settled row has a CLV yet (fresh deploy or all rows
        # captured before Smarter #3 shipped).
        "average_clv": average_clv(predictions) if scope == "single" else None,
        "last_settled_at": last_settled_at,
        "confidence_buckets": _confidence_buckets(predictions),
        "edge_buckets": _edge_buckets(predictions),
        "calibration_buckets": _calibration_buckets(predictions),
        "feature_coverage_rates": feature_rates,
        "missing_context_rates": missing_rates,
        "top_failure_reasons": top_failures,
        "last_validation_failure": runtime.last_error,
        "last_fallback_event_at": runtime.last_error_at if runtime.fallback_active else None,
    }


def build_model_readiness_summary(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    serving_mode = effective_ml_serving_mode(db)
    cutoff = retained_study_cutoff()
    single_predictions = db.scalars(
        select(Prediction)
        .options(
            load_only(
                Prediction.id,
                Prediction.run_id,
                Prediction.market_id,
                Prediction.ticker,
                Prediction.sport_key,
                Prediction.market_family,
                Prediction.capture_scope,
                # Smarter #1 (codex pattern-2 catch): ``side`` and
                # ``fair_yes_price`` are consumed by ``_calibration_buckets``
                # → ``_did_yes_happen`` / ``_predicted_yes_probability``.
                # Without them in the projection SQLAlchemy lazy-loads each
                # one per row — a query storm proportional to
                # ``READINESS_ROW_LIMIT`` on every readiness fetch.
                Prediction.side,
                Prediction.fair_yes_price,
                Prediction.edge,
                Prediction.confidence,
                Prediction.scoring_diagnostics,
                Prediction.prediction_outcome,
                Prediction.realized_pnl,
                # Smarter #3: consumed by ``average_clv``. Same lazy-load
                # storm gotcha as the smarter-#1 ``fair_yes_price`` /
                # ``side`` additions — must be in the projection.
                Prediction.closing_line_value,
                Prediction.settled_at,
                Prediction.captured_at,
            )
        )
        .where(Prediction.captured_at >= cutoff)
        .order_by(Prediction.captured_at.desc(), Prediction.id.desc())
        .limit(READINESS_ROW_LIMIT)
    ).all()
    parlay_predictions = db.scalars(
        select(ParlayPrediction)
        .options(
            load_only(
                ParlayPrediction.id,
                ParlayPrediction.run_id,
                ParlayPrediction.sport_scope,
                ParlayPrediction.leg_count,
                ParlayPrediction.participating_sports,
                # Smarter #1 (codex pattern-2 catch): joint probability is
                # consumed by ``_calibration_buckets`` →
                # ``_predicted_yes_probability`` for parlay rows. Not in the
                # projection → SQLAlchemy lazy-loads per row.
                ParlayPrediction.combined_model_probability,
                ParlayPrediction.edge,
                ParlayPrediction.confidence,
                ParlayPrediction.prediction_outcome,
                ParlayPrediction.realized_pnl,
                ParlayPrediction.settled_at,
                ParlayPrediction.captured_at,
            ),
            selectinload(ParlayPrediction.legs),
        )
        .where(ParlayPrediction.captured_at >= cutoff)
        .order_by(ParlayPrediction.captured_at.desc(), ParlayPrediction.id.desc())
        .limit(READINESS_ROW_LIMIT)
    ).all()
    shadow_singles = db.scalars(
        select(ShadowInference)
        .options(
            load_only(
                ShadowInference.source_prediction_id,
                ShadowInference.run_id,
                ShadowInference.market_id,
                ShadowInference.ticker,
                ShadowInference.sport_key,
                ShadowInference.market_family,
                ShadowInference.model_metadata,
                ShadowInference.captured_at,
            )
        )
        .where(ShadowInference.captured_at >= cutoff)
        .order_by(ShadowInference.captured_at.desc(), ShadowInference.id.desc())
        .limit(READINESS_ROW_LIMIT)
    ).all()
    shadow_parlays = db.scalars(
        select(ShadowParlayInference)
        .options(
            load_only(
                ShadowParlayInference.source_parlay_prediction_id,
                ShadowParlayInference.run_id,
                ShadowParlayInference.sport_scope,
                ShadowParlayInference.leg_count,
                ShadowParlayInference.participating_sports,
                ShadowParlayInference.leg_tickers,
                ShadowParlayInference.model_metadata,
                ShadowParlayInference.captured_at,
            )
        )
        .where(ShadowParlayInference.captured_at >= cutoff)
        .order_by(ShadowParlayInference.captured_at.desc(), ShadowParlayInference.id.desc())
        .limit(READINESS_ROW_LIMIT)
    ).all()

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

    single_counts = _single_outcome_counts(db, cutoff=cutoff)
    parlay_counts = _parlay_outcome_counts(db, cutoff=cutoff)
    single_shadow_matches = _single_shadow_match_counts(db, cutoff=cutoff)
    parlay_shadow_matches = _parlay_shadow_match_counts(db, cutoff=cutoff)

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
                single_outcome_counts=single_counts,
                parlay_outcome_counts=parlay_counts,
                single_shadow_match_counts=single_shadow_matches,
                parlay_shadow_match_counts=parlay_shadow_matches,
            )
        )

    # Smarter #26 — settlement aging buckets. Aggregated here once per
    # readiness build so the operator UI doesn't need a separate fetch.
    # ``now`` is plumbed through for tests that need a fixed clock —
    # the bucket boundaries depend on hours-past-close, so a real-wall-
    # clock comparison against pre-seeded close_times drifts over time.
    aging = compute_settlement_aging(db, now=now)

    return {
        "generated_at": _now_utc(),
        "ml_serving_mode": serving_mode,
        "shadow_enabled": serving_mode in {"shadow", "ml"},
        "auto_promotion_enabled": serving_mode == "ml",
        "min_settled_for_review": MIN_SETTLED_FOR_REVIEW,
        "min_settled_for_promotion_review": MIN_SETTLED_FOR_PROMOTION_REVIEW,
        "min_shadow_coverage": MIN_SHADOW_COVERAGE,
        "min_promotion_shadow_samples": MIN_PROMOTION_SHADOW_SAMPLES,
        "promotion_stability_days_required": STABILITY_DAYS_REQUIRED,
        "pick_history_default_n": effective_pick_history_default_n(db),
        "families": families,
        "settlement_aging": {
            "bucket_0_to_1h": aging.bucket_0_to_1h,
            "bucket_1_to_6h": aging.bucket_1_to_6h,
            "bucket_6_to_24h": aging.bucket_6_to_24h,
            "bucket_beyond_24h": aging.bucket_beyond_24h,
            "total_pending_past_close": aging.total_pending_past_close,
        },
        "narrator_enabled": effective_narrator_enabled(db),
        # Smarter #18 — sportsbook disagreement knobs. Surfaced here so
        # the readiness-panel UI can show the current effective values
        # alongside the existing thresholds. The PATCH endpoint at
        # ``/ops/models/readiness/settings`` writes these via
        # ``set_sportsbook_disagreement_*``.
        "sportsbook_disagreement_threshold": effective_sportsbook_disagreement_threshold(db),
        "sportsbook_disagreement_min_book_count": effective_sportsbook_disagreement_min_book_count(db),
    }


def build_model_readiness_detail(db: Session, family_key: str) -> dict[str, Any] | None:
    definition = family_definition(family_key)
    if definition.key != family_key:
        return None
    cutoff = retained_study_cutoff()

    if definition.scope == "single":
        single_predictions = [
            prediction
            for prediction in db.scalars(
                select(Prediction)
                .options(
                    load_only(
                        Prediction.id,
                        Prediction.run_id,
                        Prediction.market_id,
                        Prediction.ticker,
                        Prediction.sport_key,
                        Prediction.market_family,
                        Prediction.capture_scope,
                        # Smarter #1 + #3 (codex pattern-2 catch on the
                        # DETAIL path — same N+1 storm as the summary path
                        # had pre-fix). ``side`` / ``fair_yes_price`` feed
                        # calibration buckets; ``closing_line_value`` feeds
                        # ``average_clv``. Missing them here lazy-loads
                        # every row.
                        Prediction.side,
                        Prediction.fair_yes_price,
                        Prediction.edge,
                        Prediction.confidence,
                        Prediction.scoring_diagnostics,
                        Prediction.prediction_outcome,
                        Prediction.realized_pnl,
                        Prediction.closing_line_value,
                        Prediction.settled_at,
                        Prediction.captured_at,
                    )
                )
                .where(Prediction.captured_at >= cutoff)
                .order_by(Prediction.captured_at.desc(), Prediction.id.desc())
                .limit(READINESS_ROW_LIMIT)
            ).all()
            if _single_prediction_family_key(prediction) == family_key
        ]
        shadow_singles = [
            item
            for item in db.scalars(
                select(ShadowInference)
                .options(
                    load_only(
                        ShadowInference.source_prediction_id,
                        ShadowInference.run_id,
                        ShadowInference.market_id,
                        ShadowInference.ticker,
                        ShadowInference.sport_key,
                        ShadowInference.market_family,
                        ShadowInference.model_metadata,
                        ShadowInference.captured_at,
                    )
                )
                .where(ShadowInference.captured_at >= cutoff)
                .order_by(ShadowInference.captured_at.desc(), ShadowInference.id.desc())
                .limit(READINESS_ROW_LIMIT)
            ).all()
            if _shadow_single_family_key(item) == family_key
        ]
        return _summary_for_family(
            db,
            family_key,
            single_predictions,
            [],
            shadow_singles,
            [],
            single_outcome_counts=_single_outcome_counts(db, cutoff=cutoff),
            single_shadow_match_counts=_single_shadow_match_counts(db, cutoff=cutoff),
        )

    parlay_predictions = [
        prediction
        for prediction in db.scalars(
            select(ParlayPrediction)
            .options(
                load_only(
                    ParlayPrediction.id,
                    ParlayPrediction.run_id,
                    ParlayPrediction.sport_scope,
                    ParlayPrediction.leg_count,
                    ParlayPrediction.participating_sports,
                    # Smarter #1 (codex pattern-2 catch on the DETAIL path).
                    # Joint probability feeds calibration buckets via
                    # ``_predicted_yes_probability``.
                    ParlayPrediction.combined_model_probability,
                    ParlayPrediction.edge,
                    ParlayPrediction.confidence,
                    ParlayPrediction.prediction_outcome,
                    ParlayPrediction.realized_pnl,
                    ParlayPrediction.settled_at,
                    ParlayPrediction.captured_at,
                ),
                selectinload(ParlayPrediction.legs),
            )
            .where(ParlayPrediction.captured_at >= cutoff)
            .order_by(ParlayPrediction.captured_at.desc(), ParlayPrediction.id.desc())
            .limit(READINESS_ROW_LIMIT)
        ).all()
        if _parlay_prediction_family_key(prediction) == family_key
    ]
    shadow_parlays = [
        item
        for item in db.scalars(
            select(ShadowParlayInference)
            .options(
                load_only(
                    ShadowParlayInference.source_parlay_prediction_id,
                    ShadowParlayInference.run_id,
                    ShadowParlayInference.sport_scope,
                    ShadowParlayInference.leg_count,
                    ShadowParlayInference.participating_sports,
                    ShadowParlayInference.leg_tickers,
                    ShadowParlayInference.model_metadata,
                    ShadowParlayInference.captured_at,
                )
            )
            .where(ShadowParlayInference.captured_at >= cutoff)
            .order_by(ShadowParlayInference.captured_at.desc(), ShadowParlayInference.id.desc())
            .limit(READINESS_ROW_LIMIT)
        ).all()
        if _shadow_parlay_family_key(item) == family_key
    ]
    return _summary_for_family(
        db,
        family_key,
        [],
        parlay_predictions,
        [],
        shadow_parlays,
        parlay_outcome_counts=_parlay_outcome_counts(db, cutoff=cutoff),
        parlay_shadow_match_counts=_parlay_shadow_match_counts(db, cutoff=cutoff),
    )
