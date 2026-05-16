"""Architecture #5 — feature freshness layer.

Before this module: each feature emitter dumped its outputs into a
flat ``features: dict[str, Any]`` via ``features.update(emit_X(...))``.
Scoring read from that dict with no idea whether a value was fresh
from a refresh 5 min ago, served from a 6-hour-stale cache, or
defaulted to zero because the cache missed entirely. The result was
inconsistent ``*_data_complete`` markers and "missing context"
penalties operators couldn't explain.

This module replaces the flat dict with a **group-level snapshot**
structure: every emitter's outputs land in a ``FeatureGroupSnapshot``
that carries ``fresh_at`` / ``source`` / ``completeness`` metadata
alongside the values. The scoring kernel still reads from a flat
``features`` dict — but that dict is now **derived** from the
feature_groups via :func:`features_view`, not maintained separately.
This is the "pure A" shape per the Architecture #5 design discussion:
single source of truth in-process, with the persistence layer
dual-writing for backward compat with historical prediction rows + the
ML training pipeline.

## The freshness policy registry

Each group is mapped to a :class:`FeatureGroupPolicy` describing what
scoring should do when the group is stale (older than its TTL):

- ``SUPPRESS`` — drop the recommendation entirely. Used for groups
  where stale data is actively misleading (the policy registry today
  uses bespoke kernel paths for these; see notes below).
- ``PENALIZE`` — reduce confidence by a configurable delta. Used for
  groups where stale data is wrong-but-not-catastrophic (weather,
  bullpen rest, workload).
- ``IGNORE`` — no scoring impact; surface in diagnostics only. Used
  for season-stable aggregates (park factors, player season stats,
  referee tendencies), groups gated by bespoke kernel logic
  (nba_injury / mlb_lineup / mlb_starter), and toggle-gated advisory
  groups (sportsbook_consensus).

Unknown groups fall through to ``DEFAULT_POLICY`` (IGNORE) so adding
a new emitter without a registry entry doesn't accidentally gate
scoring — the operator has to opt in.

## Bespoke paths preserved by design

Three groups have pre-existing custom suppression logic in the scoring
kernel that already does the right thing:

- **nba_injury** — Smarter #17 phase 1 suppresses on OUT/DOUBTFUL with
  a fresh report. The kernel checks ``injury_report_is_fresh`` (12h
  window) before acting.
- **mlb_lineup** — Smarter #16 suppresses on confirmed-and-scratched
  with the ``player_not_in_starting_lineup`` reason.
- **mlb_starter** — the kernel has its own
  ``has_probable_starter_context`` missing-context gate plus the
  ``opposing_starter_*`` factor degrades gracefully on partial data.

For these groups the policy is ``IGNORE`` — the bespoke gate stays
authoritative. Consolidating the bespoke paths into the unified
registry is a meaningful follow-up but explicitly out of scope for
the initial Architecture #5 ship per the design discussion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class FeatureGroupSeverity(str, Enum):
    """How the scoring kernel responds when a group is stale."""

    SUPPRESS = "suppress"
    PENALIZE = "penalize"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class FeatureGroupPolicy:
    """Per-group freshness policy. Operators tune by editing
    :data:`FEATURE_GROUP_POLICIES`; scoring reads via
    :func:`policy_for_group`."""

    severity: FeatureGroupSeverity
    ttl: timedelta
    # Only meaningful when ``severity == PENALIZE``. Confidence delta
    # applied when the group is stale; should be negative.
    penalty_confidence_delta: float = 0.0


@dataclass(frozen=True, slots=True)
class FeatureGroupSnapshot:
    """The freshness-aware contents of one feature group.

    Replaces the flat ``features.update({...})`` pattern. ``values``
    carries the actual feature key-value pairs (same shape they had
    before the migration). The metadata fields describe where the
    data came from, when it was last refreshed, and how complete the
    group is — so the kernel can make per-group freshness decisions
    and operators can audit provenance.

    Frozen + slots: snapshots are immutable per scoring pass. Emitters
    construct a new snapshot per call; the kernel reads but never
    mutates.
    """

    group_key: str
    values: dict[str, Any] = field(default_factory=dict)
    # When the underlying data was last refreshed. ``None`` for
    # derived feature groups (interaction terms, schedule-density
    # signals) that don't have an externally-refreshed cache; these
    # are never flagged stale.
    fresh_at: datetime | None = None
    # Free-form operator-facing source label (e.g.
    # ``"load_weather"`` / ``"NbaInjuryReportCache"``). Used for
    # diagnostics; not read by scoring.
    source: str = ""
    # 0.0 = no expected values populated; 1.0 = every expected key
    # populated. Derived from the emitter's input availability;
    # surfaced in diagnostics, not used for gating (the existing
    # ``*_data_complete`` markers in ``values`` remain the kernel's
    # gating signal).
    completeness: float = 0.0


# Default policy: ignore. Adding a new emitter without an explicit
# registry entry never accidentally gates scoring; operators opt in.
DEFAULT_POLICY = FeatureGroupPolicy(
    severity=FeatureGroupSeverity.IGNORE,
    ttl=timedelta(days=365),
)


# Per-group freshness policy. Operators tune by editing this dict.
# The registry intentionally lists only groups with non-default
# behavior — IGNORE-policy groups (the vast majority) fall through to
# ``DEFAULT_POLICY``.
FEATURE_GROUP_POLICIES: dict[str, FeatureGroupPolicy] = {
    # PENALIZE: weather drifts but doesn't binary-flip. -5% confidence
    # when the cached forecast is older than 6h.
    "mlb_weather": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=6),
        penalty_confidence_delta=-0.05,
    ),
    # PENALIZE: bullpen rest is computed from schedule density.
    # Stale data means we missed a game in the window.
    "mlb_bullpen": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=4),
        penalty_confidence_delta=-0.05,
    ),
    # PENALIZE: workload windows change daily. -3% if no fresh game
    # log fetch in 24h.
    "nba_workload": FeatureGroupPolicy(
        severity=FeatureGroupSeverity.PENALIZE,
        ttl=timedelta(hours=24),
        penalty_confidence_delta=-0.03,
    ),
    # IGNORE groups (registry entries omitted; fall through to
    # ``DEFAULT_POLICY``). Documented here for operator visibility:
    #
    # Bespoke gates already authoritative:
    # - nba_injury — Smarter #17 phase 1 suppression on OUT/DOUBTFUL
    #   with fresh report
    # - mlb_lineup — Smarter #16 suppression on confirmed-and-scratched
    # - mlb_starter — has_probable_starter_context missing-context gate
    #
    # Season-stable / aggregates:
    # - mlb_park (park factors stable across season)
    # - mlb_batter (season + statcast aggregates)
    # - mlb_platoon (season splits)
    # - nba_advanced (player advanced; recent + season)
    # - nba_opponent_team (opponent advanced)
    # - nba_referee (per-referee season stats)
    # - nba_hustle / nba_drives / nba_clutch / nba_player_defense
    #   (long-tail season aggregates)
    #
    # Toggle-gated advisory:
    # - sportsbook_consensus (consumed via separate diagnostics dict;
    #   suppression toggle is operator-controlled and OFF by default)
}


def policy_for_group(group_key: str) -> FeatureGroupPolicy:
    """Look up a group's freshness policy.

    Unknown ``group_key`` falls through to :data:`DEFAULT_POLICY`
    (IGNORE, 365-day TTL) so a new emitter without a registry entry
    never accidentally gates scoring.
    """
    return FEATURE_GROUP_POLICIES.get(group_key, DEFAULT_POLICY)


def features_view(
    feature_groups: dict[str, FeatureGroupSnapshot],
) -> dict[str, Any]:
    """Derive the flat ``features`` dict the scoring kernel expects.

    This is the core of Architecture #5's pure-A shape: emitters
    write to ``feature_groups`` (single source of truth); the kernel
    reads via this derived view. Existing read sites in
    ``scoring/__init__.py`` and ``heuristic_factors.py`` (~141
    ``features.get(...)`` calls combined) keep their shape unchanged.

    Last-group-wins on key conflicts. In practice groups don't share
    keys — the emitter migration enforces disjoint key namespaces per
    group — but the test suite pins this with an explicit overlap
    check.
    """
    flat: dict[str, Any] = {}
    for snapshot in feature_groups.values():
        flat.update(snapshot.values)
    return flat


@dataclass(frozen=True, slots=True)
class FreshnessAssessment:
    """One group's freshness check result.

    Built by :func:`check_freshness`; consumed by the scoring kernel
    when applying per-group policy. Surfaced into diagnostics so
    operators can audit which groups were stale at scoring time.
    """

    group_key: str
    severity: FeatureGroupSeverity
    is_stale: bool
    # Time since ``fresh_at``; ``None`` when the snapshot opted out
    # of the freshness check by leaving ``fresh_at`` unset.
    age: timedelta | None
    # Nonzero only when ``severity == PENALIZE`` and ``is_stale``.
    confidence_delta: float


def check_freshness(
    feature_groups: dict[str, FeatureGroupSnapshot],
    *,
    now: datetime,
) -> list[FreshnessAssessment]:
    """Evaluate every group's freshness against its policy.

    Returns one assessment per group present in ``feature_groups``.
    Groups whose ``fresh_at`` is ``None`` are treated as
    not-applicable (``is_stale=False``, no penalty) — emitters that
    don't have an externally-refreshed cache (derived features,
    schedule-density computations) opt out of the freshness check by
    leaving ``fresh_at`` unset.

    Naive ``fresh_at`` datetimes are coerced to UTC before the age
    arithmetic — matches the convention every other freshness check
    in the codebase follows (SQLite drops tz on DateTime columns; the
    coercion keeps Postgres + SQLite output identical).
    """
    assessments: list[FreshnessAssessment] = []
    for group_key, snapshot in feature_groups.items():
        policy = policy_for_group(group_key)
        fresh_at = snapshot.fresh_at
        if fresh_at is None:
            assessments.append(
                FreshnessAssessment(
                    group_key=group_key,
                    severity=policy.severity,
                    is_stale=False,
                    age=None,
                    confidence_delta=0.0,
                )
            )
            continue
        if fresh_at.tzinfo is None:
            fresh_at = fresh_at.replace(tzinfo=timezone.utc)
        age = now - fresh_at
        is_stale = age > policy.ttl
        confidence_delta = (
            policy.penalty_confidence_delta
            if is_stale and policy.severity is FeatureGroupSeverity.PENALIZE
            else 0.0
        )
        assessments.append(
            FreshnessAssessment(
                group_key=group_key,
                severity=policy.severity,
                is_stale=is_stale,
                age=age,
                confidence_delta=confidence_delta,
            )
        )
    return assessments


def serialize_feature_groups(
    feature_groups: dict[str, FeatureGroupSnapshot],
) -> dict[str, dict[str, Any]]:
    """JSON-friendly representation for persistence.

    Persistence dual-writes the flat ``features`` dict (derived view,
    backward compat with historical rows + the ML training
    vectorizer) AND the nested ``feature_groups`` map (the
    Architecture #5 source of truth). This function produces the
    nested half.

    Datetimes serialize as ISO 8601 strings; everything else passes
    through. The reader path lives in :func:`deserialize_feature_groups`.
    """
    result: dict[str, dict[str, Any]] = {}
    for group_key, snapshot in feature_groups.items():
        result[group_key] = {
            "values": dict(snapshot.values),
            "fresh_at": (
                snapshot.fresh_at.isoformat() if snapshot.fresh_at else None
            ),
            "source": snapshot.source,
            "completeness": snapshot.completeness,
        }
    return result


def deserialize_feature_groups(
    raw: dict[str, dict[str, Any]] | None,
) -> dict[str, FeatureGroupSnapshot]:
    """Reverse of :func:`serialize_feature_groups`.

    Tolerant of missing fields so prediction rows written before
    Architecture #5 shipped (which won't have ``feature_groups`` at
    all) round-trip as an empty dict. The reader side at the scoring
    kernel handles "no feature_groups" by falling back to the flat
    ``features`` dict on the same row.
    """
    if not raw:
        return {}
    result: dict[str, FeatureGroupSnapshot] = {}
    for group_key, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        fresh_at_raw = payload.get("fresh_at")
        fresh_at: datetime | None
        if isinstance(fresh_at_raw, str) and fresh_at_raw:
            try:
                fresh_at = datetime.fromisoformat(fresh_at_raw)
            except ValueError:
                fresh_at = None
        else:
            fresh_at = None
        result[group_key] = FeatureGroupSnapshot(
            group_key=group_key,
            values=dict(payload.get("values") or {}),
            fresh_at=fresh_at,
            source=str(payload.get("source") or ""),
            completeness=float(payload.get("completeness") or 0.0),
        )
    return result


__all__ = [
    "FeatureGroupSeverity",
    "FeatureGroupPolicy",
    "FeatureGroupSnapshot",
    "FreshnessAssessment",
    "DEFAULT_POLICY",
    "FEATURE_GROUP_POLICIES",
    "policy_for_group",
    "features_view",
    "check_freshness",
    "serialize_feature_groups",
    "deserialize_feature_groups",
]
