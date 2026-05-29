from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ModelFamilyRuntimeHealth
from app.services.ml.promotion import _non_heuristic_lineage_from_row, brier_score, paired_examples_for_family
from app.services.model_families import FAMILY_DEFINITIONS


ROLLING_SAMPLE_SIZE = 50
BRIER_DEMOTION_MULTIPLIER = 1.05
RUNTIME_UNAVAILABLE_MINUTES = 15


@dataclass(frozen=True, slots=True)
class KillSwitchResult:
    family_key: str
    demoted: bool
    reason: str | None
    rolling_sample_count: int = 0
    rolling_shadow_brier: float | None = None
    baseline_brier: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "family_key": self.family_key,
            "demoted": self.demoted,
            "reason": self.reason,
            "rolling_sample_count": self.rolling_sample_count,
            "rolling_shadow_brier": self.rolling_shadow_brier,
            "baseline_brier": self.baseline_brier,
        }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _runtime_bad_long_enough(row: ModelFamilyRuntimeHealth, *, now: datetime) -> bool:
    if row.runtime_health not in {"unavailable", "degraded"}:
        return False
    if row.last_error_at is None:
        return False
    error_at = row.last_error_at
    if error_at.tzinfo is None:
        error_at = error_at.replace(tzinfo=timezone.utc)
    return error_at <= now - timedelta(minutes=RUNTIME_UNAVAILABLE_MINUTES)


def _demote(row: ModelFamilyRuntimeHealth, *, now: datetime, reason: str, details: dict[str, Any]) -> KillSwitchResult:
    row.promotion_mode = "shadow"
    row.desired_mode = "shadow"
    row.effective_mode = "heuristic" if row.runtime_health in {"unavailable", "degraded"} else "shadow"
    row.fallback_active = False
    row.promotion_stability_days = 0
    row.promotion_updated_at = now
    row.promotion_metrics = {
        **dict(row.promotion_metrics or {}),
        "kill_switch": {
            "demoted_at": now.isoformat(),
            "reason": reason,
            **details,
        },
    }
    return KillSwitchResult(
        family_key=row.family_key,
        demoted=True,
        reason=reason,
        rolling_sample_count=int(details.get("rolling_sample_count") or 0),
        rolling_shadow_brier=details.get("rolling_shadow_brier"),
        baseline_brier=details.get("baseline_brier"),
    )


def evaluate_family(db: Session, family_key: str, *, now: datetime | None = None) -> KillSwitchResult:
    reference_now = now or _now_utc()
    row = db.scalar(select(ModelFamilyRuntimeHealth).where(ModelFamilyRuntimeHealth.family_key == family_key))
    if row is None or row.promotion_mode != "ml":
        return KillSwitchResult(family_key=family_key, demoted=False, reason=None)

    if _runtime_bad_long_enough(row, now=reference_now):
        result = _demote(
            row,
            now=reference_now,
            reason="runtime_unavailable",
            details={"runtime_health": row.runtime_health, "last_error": row.last_error},
        )
        db.flush()
        return result

    baseline_brier = row.promotion_baseline_brier
    if baseline_brier is None or baseline_brier <= 0:
        return KillSwitchResult(family_key=family_key, demoted=False, reason=None)

    lineage = _non_heuristic_lineage_from_row(row)
    examples = sorted(
        paired_examples_for_family(db, family_key, lineage=lineage),
        key=lambda example: example.captured_at,
        reverse=True,
    )[:ROLLING_SAMPLE_SIZE]
    if len(examples) < ROLLING_SAMPLE_SIZE:
        return KillSwitchResult(
            family_key=family_key,
            demoted=False,
            reason=None,
            rolling_sample_count=len(examples),
            baseline_brier=baseline_brier,
        )

    rolling_brier = brier_score(examples, model="shadow")
    if rolling_brier > baseline_brier * BRIER_DEMOTION_MULTIPLIER:
        result = _demote(
            row,
            now=reference_now,
            reason="rolling_brier_regression",
            details={
                "rolling_sample_count": len(examples),
                "rolling_shadow_brier": rolling_brier,
                "baseline_brier": baseline_brier,
            },
        )
        db.flush()
        return result

    return KillSwitchResult(
        family_key=family_key,
        demoted=False,
        reason=None,
        rolling_sample_count=len(examples),
        rolling_shadow_brier=rolling_brier,
        baseline_brier=baseline_brier,
    )


def evaluate_all_families(db: Session, *, now: datetime | None = None) -> list[KillSwitchResult]:
    reference_now = now or _now_utc()
    results: list[KillSwitchResult] = []
    for definition in FAMILY_DEFINITIONS:
        if definition.study_track != "active":
            continue
        results.append(evaluate_family(db, definition.key, now=reference_now))
    return results
