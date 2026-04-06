from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ParlayPrediction, Prediction
from app.services.model_families import family_definition, parlay_family_key, single_family_key

SETTLED_OUTCOMES = ("won", "lost", "push", "cancelled")
MIN_SETTLED_FOR_REVIEW = 40
MIN_SHADOW_COVERAGE = 0.75

_SETTLED_COUNTS_CACHE_KEY = "ml_study_settled_counts_by_family"


def is_active_study_family(family_key: str) -> bool:
    return family_definition(family_key).study_track == "active"


def history_ready_for_shadow(settled_predictions: int) -> bool:
    return settled_predictions >= MIN_SETTLED_FOR_REVIEW


def shadow_coverage_ratio(*, total_predictions: int, shadow_predictions: int) -> float:
    if total_predictions <= 0:
        return 0.0
    return round(min(shadow_predictions / total_predictions, 1.0), 4)


def shadow_coverage_ready(ratio: float) -> bool:
    return ratio >= MIN_SHADOW_COVERAGE


def retained_study_window_days(*, settings=None) -> int:
    current_settings = settings or get_settings()
    return max(1, min(current_settings.prediction_retention_days, current_settings.shadow_inference_retention_days))


def retained_study_cutoff(*, now: datetime | None = None, settings=None) -> datetime:
    reference_now = now or datetime.now(timezone.utc)
    return reference_now - timedelta(days=retained_study_window_days(settings=settings))


def settled_prediction_counts_by_family(db: Session) -> dict[str, int]:
    cached = db.info.get(_SETTLED_COUNTS_CACHE_KEY)
    if isinstance(cached, dict):
        return cached

    counts: dict[str, int] = {}

    single_rows = db.execute(
        select(Prediction.sport_key, Prediction.market_family).where(
            Prediction.prediction_outcome.in_(SETTLED_OUTCOMES),
            or_(Prediction.capture_scope.is_(None), Prediction.capture_scope != "coverage"),
        )
    ).all()
    for sport_key, market_family in single_rows:
        family_key = single_family_key(sport_key, market_family)
        counts[family_key] = counts.get(family_key, 0) + 1

    parlay_rows = db.execute(
        select(
            ParlayPrediction.leg_count,
            ParlayPrediction.participating_sports,
            ParlayPrediction.sport_scope,
        ).where(ParlayPrediction.prediction_outcome.in_(SETTLED_OUTCOMES))
    ).all()
    for leg_count, participating_sports, sport_scope in parlay_rows:
        family_key = parlay_family_key(leg_count, participating_sports or [sport_scope])
        counts[family_key] = counts.get(family_key, 0) + 1

    db.info[_SETTLED_COUNTS_CACHE_KEY] = counts
    return counts


def settled_prediction_count_for_family(db: Session, family_key: str) -> int:
    return int(settled_prediction_counts_by_family(db).get(family_key, 0))
