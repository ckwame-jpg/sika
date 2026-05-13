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


def single_family_key(sport_key: str | None, market_family: str | None) -> str:
    sport = (sport_key or "").upper()
    family = (market_family or "").lower()
    if family == "player_prop":
        if sport == "NBA":
            return "nba_props"
        if sport == "MLB":
            return "mlb_props"
    if sport == "NBA":
        return "nba_singles"
    if sport == "MLB":
        return "mlb_singles"
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
