"""Canonical ML family registry — single source of truth.

Bug #41: a parallel registry used to live at ``apps/ml/ml/families.py``
with ``_v1``-suffixed keys (e.g. ``nba_singles_v1``) and a
``required_feature_groups`` field that nothing actually consumed. It
duplicated the logical family list defined here and drifted (the
``parlay_4_6_leg_combiner`` family added below was never mirrored
there). That registry has been deleted; all runtime metadata —
serving, readiness, kill-switch, promotion, and shadow capture — flows
through ``FAMILY_DEFINITIONS``. Training artifacts may still carry
``_v1`` suffixes in their packaged ``family_key``; the manifest's
``serves_family_key`` field maps each artifact to the runtime key
documented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


StudyTrack = Literal["active", "heuristic_only"]


@dataclass(frozen=True, slots=True)
class ModelFamilyDefinition:
    key: str
    label: str
    scope: str
    sport_scope: str
    leg_count: int | None = None
    study_track: StudyTrack = "heuristic_only"


FAMILY_DEFINITIONS: tuple[ModelFamilyDefinition, ...] = (
    ModelFamilyDefinition(key="nba_singles", label="NBA singles", scope="single", sport_scope="NBA", study_track="active"),
    ModelFamilyDefinition(key="mlb_singles", label="MLB singles", scope="single", sport_scope="MLB", study_track="active"),
    ModelFamilyDefinition(key="nba_props", label="NBA props", scope="single", sport_scope="NBA", study_track="active"),
    ModelFamilyDefinition(key="mlb_props", label="MLB props", scope="single", sport_scope="MLB", study_track="active"),
    # WNBA families — same shape as NBA. ``study_track="active"`` means
    # the training pipeline picks them up automatically (PR 5 will add
    # them to ``_DEFAULT_SERVE_FAMILY_KEYS`` so they're served in the
    # weekly retrain workflow output). Until WNBA settled rows accumulate
    # (needs PR 6 + several weeks of games), the readiness panel will
    # surface them as ``insufficient_history``.
    ModelFamilyDefinition(key="wnba_singles", label="WNBA singles", scope="single", sport_scope="WNBA", study_track="active"),
    ModelFamilyDefinition(key="wnba_props", label="WNBA props", scope="single", sport_scope="WNBA", study_track="active"),
    ModelFamilyDefinition(
        key="nba_parlay_2leg",
        label="NBA 2-leg parlays",
        scope="parlay",
        sport_scope="NBA",
        leg_count=2,
        study_track="active",
    ),
    # 3-leg + 4-6-leg parlay families intentionally stay
    # ``study_track="heuristic_only"`` (the default) — bug #42 flagged the
    # apparent inconsistency, but per-family settled volume at these leg
    # counts is too low to clear bug #20's walk-forward floor in any
    # reasonable window, so promoting them to "active" would just leave
    # them stuck at ``insufficient_history`` forever. The keys exist for
    # accounting/UI grouping; ML promotion only applies to singles +
    # 2-leg parlays today.
    ModelFamilyDefinition(key="nba_parlay_3leg", label="NBA 3-leg parlays", scope="parlay", sport_scope="NBA", leg_count=3),
    ModelFamilyDefinition(
        key="mlb_parlay_2leg",
        label="MLB 2-leg parlays",
        scope="parlay",
        sport_scope="MLB",
        leg_count=2,
        study_track="active",
    ),
    ModelFamilyDefinition(key="mlb_parlay_3leg", label="MLB 3-leg parlays", scope="parlay", sport_scope="MLB", leg_count=3),
    ModelFamilyDefinition(
        key="mixed_parlay_2leg",
        label="Mixed 2-leg parlays",
        scope="parlay",
        sport_scope="MIXED",
        leg_count=2,
        study_track="active",
    ),
    ModelFamilyDefinition(key="mixed_parlay_3leg", label="Mixed 3-leg parlays", scope="parlay", sport_scope="MIXED", leg_count=3),
    ModelFamilyDefinition(key="parlay_4_6_leg_combiner", label="4-6 leg parlay combiner", scope="parlay", sport_scope="MIXED"),
)

FAMILY_DEFINITION_BY_KEY = {item.key: item for item in FAMILY_DEFINITIONS}


def family_definition(key: str) -> ModelFamilyDefinition:
    return FAMILY_DEFINITION_BY_KEY.get(
        key,
        ModelFamilyDefinition(key=key, label=key.replace("_", " "), scope="unknown", sport_scope="UNKNOWN"),
    )


# Smarter #28 — per-family ``_quality_tier`` calibration.
#
# The kernel previously hardcoded one set of thresholds (selected-side
# probability ≥ 0.36, context coverage ≥ 0.72, adjusted confidence ≥
# 0.58, total penalty ≤ 0.09) for the "high" tier on the heuristic
# path. Different families have meaningfully different settled-data
# quality (NBA props are high-variance and rarely clear the 0.72 ctx
# floor; MLB game lines fail to disambiguate at the 0.36 prob floor)
# so a single set understates the calibration cost for one family and
# overstates it for the other.
#
# This dataclass + override map provide the mechanism. The
# initial overrides dict is intentionally empty so today's behavior is
# preserved exactly; operators tune values per-family from backtest
# results without changing the call site.
@dataclass(frozen=True, slots=True)
class QualityTierThresholds:
    # Heuristic-path "high" tier — ALL conditions must hold.
    high_selected_side_probability: float = 0.36
    high_context_coverage: float = 0.72
    high_adjusted_confidence: float = 0.58
    high_total_penalty: float = 0.09
    # Heuristic-path "low" tier — ANY condition triggers low.
    low_selected_side_probability: float = 0.20
    low_context_coverage: float = 0.45
    low_adjusted_confidence: float = 0.40
    low_total_penalty: float = 0.18
    # ML-served path uses a coarser ladder (calibrated probability +
    # context coverage; no penalty term because penalties don't
    # accumulate on the ML branch).
    ml_high_context_coverage: float = 0.75
    ml_high_adjusted_confidence: float = 0.60
    ml_medium_context_coverage: float = 0.50


DEFAULT_QUALITY_TIER_THRESHOLDS = QualityTierThresholds()

# Per-family overrides. Empty by design — adding an entry here tunes
# that family's tier semantics without touching scoring code.
QUALITY_TIER_THRESHOLDS_BY_FAMILY: dict[str, QualityTierThresholds] = {}


def quality_tier_thresholds_for(family_key: str) -> QualityTierThresholds:
    """Return per-family thresholds, falling back to the shared defaults.

    Unknown family keys (e.g. one-off scopes that don't appear in
    ``FAMILY_DEFINITIONS``) also get the defaults — no special case.
    """
    return QUALITY_TIER_THRESHOLDS_BY_FAMILY.get(family_key, DEFAULT_QUALITY_TIER_THRESHOLDS)


# Smarter #30 — per-family ``watchlist_min_edge`` tuning mechanism.
#
# The watchlist suppression floor (``Settings.watchlist_min_edge``,
# default 0.03) is the bar a scored recommendation must clear to land
# on the watchlist. A single floor is too aggressive for high-variance
# NBA props (where edge realization is noisy and a 0.03 floor filters
# real signal) and too lenient for tight MLB game lines (where a 0.03
# floor lets through every barely-mispriced market).
#
# Like Smarter #28's ``QUALITY_TIER_THRESHOLDS_BY_FAMILY``, this ships
# the **mechanism only** — the override registry is intentionally empty
# so today's behavior is preserved exactly. Operators populate from
# backtest results (Smarter #2 produces the per-family Brier + hit-rate
# data needed to tune these) without touching scoring code.
#
# Consumers:
# - ``scoring/__init__.py:_single_scoring_adjustments`` (the main
#   ``min_edge`` suppression check).
# - ``scoring/monotonicity.py`` (post-clamp bug #9 floor check — both
#   the recommendation and prediction paths).
WATCHLIST_MIN_EDGE_OVERRIDES: dict[str, float] = {}


def watchlist_min_edge_for(family_key: str, default: float) -> float:
    """Return the per-family watchlist edge floor, falling back to the
    operator-set ``Settings.watchlist_min_edge`` default.

    Unknown family keys (e.g. parlay scopes that don't have a tuned
    floor) get the default. The ``default`` argument is required (not
    a module-level constant) so the caller always passes the live
    operator setting — overrides represent deltas off that baseline,
    not absolute floors that would silently outlive a config change.
    """
    return WATCHLIST_MIN_EDGE_OVERRIDES.get(family_key, default)


# Smarter #19 — per-family monotonic constraints for HGBC training.
# The actual registry lives in ``ml_features.monotonic`` (shared
# package) because ``apps/ml/ml/training.py`` can't import from
# ``apps/api``. Re-export here so operators see the per-family
# mechanism alongside the other per-family knobs.
from ml_features.monotonic import (  # noqa: E402 — re-export at module bottom
    MONOTONIC_CONSTRAINTS_BY_FAMILY,
    monotonic_constraints_for,
)


def single_family_key(sport_key: str | None, market_family: str | None) -> str:
    sport = (sport_key or "").upper()
    family = (market_family or "").lower()
    if family == "player_prop":
        if sport == "NBA":
            return "nba_props"
        if sport == "MLB":
            return "mlb_props"
        if sport == "WNBA":
            return "wnba_props"
    if sport == "NBA":
        return "nba_singles"
    if sport == "MLB":
        return "mlb_singles"
    if sport == "WNBA":
        return "wnba_singles"
    return f"{sport.lower()}_singles" if sport else "unknown_singles"


def parlay_family_key(leg_count: int, participating_sports: list[str] | tuple[str, ...] | set[str]) -> str:
    sports = sorted({(sport or "").upper() for sport in participating_sports if sport})
    if leg_count >= 4:
        return "parlay_4_6_leg_combiner"
    if sports == ["NBA"]:
        return f"nba_parlay_{leg_count}leg"
    if sports == ["MLB"]:
        return f"mlb_parlay_{leg_count}leg"
    return f"mixed_parlay_{leg_count}leg"
