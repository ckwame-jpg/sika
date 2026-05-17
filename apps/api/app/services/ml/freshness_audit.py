"""Smarter #22 PR B prep — empirical calibration audit per stale feature group.

The tuning playbook (`SMARTER_22_TUNING_PLAYBOOK.md`) asks operators
to observe the freshness badge across a few scoring sessions and
journal which stale groups correlated with bad picks. This module
auto-captures that signal: it joins settled `Prediction` rows with the
`scoring_diagnostics["freshness_stale_groups"]` (Smarter #22 PR A,
sika#186) and `scoring_diagnostics["feature_groups"]` (Architecture
#5, sika#169) JSON, then aggregates per group:

- Count of stale-bucket vs fresh-bucket settled predictions
- Average predicted YES probability per bucket
- Actual YES hit rate per bucket
- Calibration miss per bucket = ``|avg_predicted - hit_rate|``
- Delta = ``stale_miss - fresh_miss`` (positive ⇒ staleness hurts)

A positive delta is the tuning signal the playbook prescribes for
promoting a group from `IGNORE` to `PENALIZE` in
`FEATURE_GROUP_POLICIES`.

## Apples-to-apples bucketing

For each unique `group_key` ever seen in the audit window, the
"fresh" bucket counts ONLY predictions where the group was emitted
AND not in the stale list. Predictions where the group wasn't
emitted at all (e.g., MLB-only group on an NBA recommendation) are
excluded from both buckets — otherwise the fresh-bucket size would
balloon with irrelevant rows that depress the calibration baseline.

## NO-side outcome inversion

`Prediction.fair_yes_price` is always P(YES). A NO-side prediction
that "won" means YES did NOT happen — so the YES hit rate must
invert. Reuses the same logic as `readiness._did_yes_happen` for
consistency with the calibration histogram on the readiness panel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Prediction
from app.schemas import FreshnessAuditRowRead


logger = logging.getLogger(__name__)


DEFAULT_WINDOW_DAYS = 30
# Mirror ``readiness.READINESS_ROW_LIMIT``. At 100 predictions/day ×
# 30 days = 3 000 rows the audit fits well under the limit; the cap
# matters for unusual backfills or operators with very high volume.
# Ordering by settled_at DESC keeps the most-recent window preferred
# if the cap clips.
AUDIT_ROW_LIMIT = 5_000


def _did_yes_happen(side: str | None, outcome: str | None) -> bool | None:
    """Return whether the YES side of this single prediction happened,
    from the perspective of comparing against ``fair_yes_price``.

    Returns:
    - True if YES happened (yes-side won, or no-side lost)
    - False if YES didn't happen (yes-side lost, or no-side won)
    - None for push / cancelled / unknown side (cannot inform
      calibration)

    Intentionally DIVERGES from ``readiness._did_yes_happen`` in one
    way: this audit excludes parlays entirely. Parlay predictions
    have ``side=None`` and use ``combined_model_probability`` (the
    joint probability of the picked leg combination) as their model
    output — not ``fair_yes_price``. Auditing them on the same
    calibration axis as singles would mix two different probability
    scales. They get a separate calibration story; this audit is
    single-only.
    """
    if side not in {"yes", "no"} or outcome not in {"won", "lost"}:
        return None
    yes_side_won = side == "yes" and outcome == "won"
    no_side_lost = side == "no" and outcome == "lost"
    if yes_side_won or no_side_lost:
        return True
    return False


def _stale_group_keys(diagnostics: dict) -> set[str]:
    """Pull the set of group_keys flagged stale on this prediction.

    Defensive — drops malformed payloads (non-list, entries without
    a string ``group_key``) so a single bad row doesn't 500 the
    whole audit. Logs at debug level so persistent diagnostics drift
    is observable when an operator runs with ``--log-level=debug``."""
    raw = diagnostics.get("freshness_stale_groups")
    if raw is None:
        return set()
    if not isinstance(raw, list):
        logger.debug(
            "freshness_audit.skip_malformed_stale_groups: "
            "type=%s value=%r",
            type(raw).__name__, raw,
        )
        return set()
    out: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = entry.get("group_key")
        if isinstance(key, str) and key:
            out.add(key)
    return out


def _emitted_group_keys(diagnostics: dict) -> set[str]:
    """Pull the set of group_keys this prediction emitted at all
    (stale or fresh). Drawn from ``feature_groups`` — the structured
    half of Architecture #5's persistence."""
    raw = diagnostics.get("feature_groups")
    if not isinstance(raw, dict):
        return set()
    return {key for key in raw.keys() if isinstance(key, str)}


def _settled_predictions_in_window(
    db: Session, *, window_days: int, now: datetime,
) -> Iterable[Prediction]:
    """Yield settled predictions whose ``settled_at`` falls in the
    last ``window_days``. Outcomes restricted to ``won`` / ``lost``
    (push and cancelled cannot inform calibration). Uses a server-
    side filter so the audit scales with newer rows, not the full
    settlement history."""
    cutoff = now - timedelta(days=window_days)
    stmt = (
        select(Prediction)
        .where(Prediction.settled_at >= cutoff)
        .where(Prediction.prediction_outcome.in_(["won", "lost"]))
        .order_by(Prediction.settled_at.desc())
        .limit(AUDIT_ROW_LIMIT)
    )
    return db.scalars(stmt).all()


def compute_freshness_audit(
    db: Session, *, window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[FreshnessAuditRowRead]:
    """Compute the per-group calibration audit. See module docstring
    for the bucketing + inversion semantics.

    Returns rows sorted by ``calibration_delta`` descending so the
    most-actionable signals (biggest staleness penalty) are at the
    top of the operator's view. Empty list when no settled
    predictions in the window have any persisted freshness
    diagnostics — the common pre-PR-A state.
    """
    effective_now = now if now is not None else datetime.now(timezone.utc)

    # Accumulators per group_key. The ``predicted`` and ``actual``
    # parallel lists support computing avg_predicted + hit_rate by a
    # second pass; we keep them tiny (one float each per prediction)
    # to avoid a second DB round-trip.
    stale: dict[str, dict[str, list[float]]] = {}
    fresh: dict[str, dict[str, list[float]]] = {}

    for prediction in _settled_predictions_in_window(
        db, window_days=window_days, now=effective_now,
    ):
        diagnostics = prediction.scoring_diagnostics
        if not isinstance(diagnostics, dict):
            continue
        yes_happened = _did_yes_happen(prediction.side, prediction.prediction_outcome)
        if yes_happened is None:
            continue
        predicted_prob = prediction.fair_yes_price
        if predicted_prob is None:
            continue
        try:
            predicted = float(predicted_prob)
        except (TypeError, ValueError):
            continue
        if predicted < 0.0 or predicted > 1.0:
            # Out-of-range model output cannot inform calibration;
            # silently skip rather than corrupt bucket averages.
            continue

        # ``check_freshness`` iterates over ``feature_groups`` and only
        # ever appends groups it finds there into the persisted stale
        # list — so ``stale_keys`` is a subset of ``emitted_keys`` by
        # construction. We rely on that invariant here: if a stale key
        # somehow appeared without a matching ``feature_groups`` entry
        # (e.g., a future code path that wrote ``freshness_stale_groups``
        # bypassing the layer), it should NOT count toward stale_count
        # because the prediction didn't actually use the group's data.
        # Reviewer P1 — removed a defensive union here for that reason.
        stale_keys = _stale_group_keys(diagnostics)
        emitted_keys = _emitted_group_keys(diagnostics)
        actual = 1.0 if yes_happened else 0.0

        for group_key in emitted_keys:
            bucket = stale if group_key in stale_keys else fresh
            entry = bucket.setdefault(
                group_key, {"predicted": [], "actual": []},
            )
            entry["predicted"].append(predicted)
            entry["actual"].append(actual)

    all_keys = sorted(set(stale.keys()) | set(fresh.keys()))
    rows: list[FreshnessAuditRowRead] = []
    for group_key in all_keys:
        stale_data = stale.get(group_key) or {"predicted": [], "actual": []}
        fresh_data = fresh.get(group_key) or {"predicted": [], "actual": []}
        stale_avg_pred = _safe_mean(stale_data["predicted"])
        fresh_avg_pred = _safe_mean(fresh_data["predicted"])
        stale_hit = _safe_mean(stale_data["actual"])
        fresh_hit = _safe_mean(fresh_data["actual"])
        stale_miss = abs(stale_avg_pred - stale_hit)
        fresh_miss = abs(fresh_avg_pred - fresh_hit)
        rows.append(
            FreshnessAuditRowRead(
                group_key=group_key,
                stale_count=len(stale_data["predicted"]),
                fresh_count=len(fresh_data["predicted"]),
                stale_avg_predicted=round(stale_avg_pred, 4),
                fresh_avg_predicted=round(fresh_avg_pred, 4),
                stale_hit_rate=round(stale_hit, 4),
                fresh_hit_rate=round(fresh_hit, 4),
                stale_calibration_miss=round(stale_miss, 4),
                fresh_calibration_miss=round(fresh_miss, 4),
                calibration_delta=round(stale_miss - fresh_miss, 4),
            )
        )
    rows.sort(key=lambda row: row.calibration_delta, reverse=True)
    return rows


def _safe_mean(values: list[float]) -> float:
    """Mean with empty-list → 0.0 fallback so the schema's
    non-nullable float fields always have a value."""
    if not values:
        return 0.0
    return sum(values) / len(values)
