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


def study_track_for_family(key: str) -> StudyTrack:
    return family_definition(key).study_track


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
