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

## Window honesty

The scan runs newest-first under ``AUDIT_ROW_LIMIT``. When settled
volume is high enough that the cap clips the nominal window, the
returned ``meta`` reports it (``row_limit_hit`` +
``effective_window_start``) so the UI can label the real window
instead of silently narrowing — the original 5k cap shrank "30d"
to ~30h at MLB volume and hid every stale-bucket sample.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Prediction
from app.schemas import FreshnessAuditMetaRead, FreshnessAuditRowRead


logger = logging.getLogger(__name__)


DEFAULT_WINDOW_DAYS = 30
# Newest-first cap on settled rows the audit scans. Sized from
# measured volume: at MLB peak the live DB settles ~15-20k singles a
# day (a 30-day window held ~77k won/lost rows on 2026-07-21), so
# 250k leaves ~3× headroom before clipping. The original 5k cap was
# sized for ~100/day and silently shrank the "30d" window to ~30h —
# clipping out 100% of the stale-bucket evidence. If this cap ever
# clips again, the result's ``meta.row_limit_hit`` flips true and
# ``effective_window_start`` reports the real (shorter) window so
# the UI labels it honestly instead of narrowing in silence.
# Ordering by settled_at DESC keeps the most-recent window preferred
# when the cap clips.
AUDIT_ROW_LIMIT = 250_000
# Batch size for the streaming column select. Keeps memory flat while
# scanning up to ``AUDIT_ROW_LIMIT`` rows (server-side cursor on
# Postgres; plain batching on SQLite).
AUDIT_FETCH_BATCH = 1_000


@dataclass
class FreshnessAuditResult:
    """Audit rows plus the window-honesty sidecar the UI renders."""

    rows: list[FreshnessAuditRowRead] = field(default_factory=list)
    meta: FreshnessAuditMetaRead = field(default_factory=FreshnessAuditMetaRead)


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


def _settled_rows_in_window(
    db: Session, *, cutoff: datetime, row_limit: int,
) -> Iterable[Any]:
    """Stream ``(settled_at, side, prediction_outcome, fair_yes_price,
    scoring_diagnostics)`` tuples for settled predictions whose
    ``settled_at`` falls after ``cutoff``, newest first. Outcomes
    restricted to ``won`` / ``lost`` (push and cancelled cannot inform
    calibration).

    Column-tuple select rather than full ORM rows: the audit only
    needs five columns and ``scoring_diagnostics`` is by far the
    heaviest, so materializing ``Prediction`` instances at the 250k
    cap would waste both memory and identity-map bookkeeping.
    ``yield_per`` batches the fetch (server-side cursor on Postgres,
    plain batching on SQLite) so memory stays flat regardless of how
    much of the cap the window fills."""
    stmt = (
        select(
            Prediction.settled_at,
            Prediction.side,
            Prediction.prediction_outcome,
            Prediction.fair_yes_price,
            Prediction.scoring_diagnostics,
        )
        .where(Prediction.settled_at >= cutoff)
        .where(Prediction.prediction_outcome.in_(["won", "lost"]))
        .order_by(Prediction.settled_at.desc())
        .limit(row_limit)
        .execution_options(yield_per=AUDIT_FETCH_BATCH)
    )
    return db.execute(stmt)


def compute_freshness_audit(
    db: Session, *, window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    row_limit: int = AUDIT_ROW_LIMIT,
) -> FreshnessAuditResult:
    """Compute the per-group calibration audit. See module docstring
    for the bucketing + inversion semantics.

    Returns ``FreshnessAuditResult``: ``rows`` sorted by
    ``calibration_delta`` descending so the most-actionable signals
    (biggest staleness penalty) are at the top of the operator's
    view — empty when no settled predictions in the window have any
    persisted freshness diagnostics (the common pre-PR-A state) —
    plus ``meta`` reporting how much of the nominal window the row
    cap actually covered.
    """
    effective_now = now if now is not None else datetime.now(timezone.utc)
    cutoff = effective_now - timedelta(days=window_days)
    started = time.perf_counter()

    # Running-sum accumulators per group_key. Only the per-bucket
    # means are ever consumed, so ``count / predicted_sum /
    # actual_sum`` keeps memory flat at the 250k-row cap where the
    # parallel-list accumulators this replaced would not.
    stale: dict[str, dict[str, float]] = {}
    fresh: dict[str, dict[str, float]] = {}

    rows_scanned = 0
    oldest_settled_at: datetime | None = None

    for (
        settled_at, side, outcome, fair_yes_price, diagnostics,
    ) in _settled_rows_in_window(db, cutoff=cutoff, row_limit=row_limit):
        rows_scanned += 1
        # Newest-first ordering means the last row seen is the oldest
        # actually included — the honest window edge when the cap clips.
        oldest_settled_at = settled_at
        if not isinstance(diagnostics, dict):
            continue
        yes_happened = _did_yes_happen(side, outcome)
        if yes_happened is None:
            continue
        if fair_yes_price is None:
            continue
        try:
            predicted = float(fair_yes_price)
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
                group_key,
                {"count": 0.0, "predicted_sum": 0.0, "actual_sum": 0.0},
            )
            entry["count"] += 1
            entry["predicted_sum"] += predicted
            entry["actual_sum"] += actual

    all_keys = sorted(set(stale.keys()) | set(fresh.keys()))
    rows: list[FreshnessAuditRowRead] = []
    for group_key in all_keys:
        stale_data = stale.get(group_key)
        fresh_data = fresh.get(group_key)
        stale_avg_pred = _bucket_mean(stale_data, "predicted_sum")
        fresh_avg_pred = _bucket_mean(fresh_data, "predicted_sum")
        stale_hit = _bucket_mean(stale_data, "actual_sum")
        fresh_hit = _bucket_mean(fresh_data, "actual_sum")
        stale_miss = abs(stale_avg_pred - stale_hit)
        fresh_miss = abs(fresh_avg_pred - fresh_hit)
        rows.append(
            FreshnessAuditRowRead(
                group_key=group_key,
                stale_count=int(stale_data["count"]) if stale_data else 0,
                fresh_count=int(fresh_data["count"]) if fresh_data else 0,
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

    row_limit_hit = row_limit > 0 and rows_scanned >= row_limit
    meta = FreshnessAuditMetaRead(
        window_days=window_days,
        row_limit=row_limit,
        rows_scanned=rows_scanned,
        row_limit_hit=row_limit_hit,
        # When the cap clipped, the honest window starts at the oldest
        # row actually scanned; otherwise the full nominal window fit.
        effective_window_start=(
            oldest_settled_at
            if row_limit_hit and oldest_settled_at is not None
            else cutoff
        ),
    )

    duration_ms = (time.perf_counter() - started) * 1000.0
    # Perf guardrail — the readiness summary endpoint already runs
    # 20-30s; this line is how we notice the audit creeping toward
    # the web client's 60s timeout before operators do.
    logger.info(
        "freshness_audit.completed rows_scanned=%d groups=%d "
        "row_limit_hit=%s duration_ms=%.0f",
        rows_scanned, len(all_keys), row_limit_hit, duration_ms,
    )
    return FreshnessAuditResult(rows=rows, meta=meta)


def _bucket_mean(entry: dict[str, float] | None, sum_key: str) -> float:
    """Mean with empty-bucket → 0.0 fallback so the schema's
    non-nullable float fields always have a value."""
    if not entry or not entry["count"]:
        return 0.0
    return entry[sum_key] / entry["count"]
