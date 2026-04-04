from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ModelFamily:
    family_key: str
    target_scope: str
    sports: tuple[str, ...]
    market_types: tuple[str, ...]
    required_feature_groups: tuple[str, ...] = field(default_factory=tuple)


SINGLE_LEG_FAMILIES = (
    ModelFamily(
        family_key="nba_singles_v1",
        target_scope="single",
        sports=("NBA",),
        market_types=("winner",),
        required_feature_groups=("opponent_defense", "rest_travel", "injury_status", "kalshi_context"),
    ),
    ModelFamily(
        family_key="mlb_singles_v1",
        target_scope="single",
        sports=("MLB",),
        market_types=("winner", "first_five_winner"),
        required_feature_groups=("probable_starters", "bullpen_context", "park_factors", "weather", "kalshi_context"),
    ),
    ModelFamily(
        family_key="nba_props_v1",
        target_scope="single",
        sports=("NBA",),
        market_types=("player_prop",),
        required_feature_groups=("player_form", "opponent_defense", "injury_status", "rest_travel", "kalshi_context"),
    ),
    ModelFamily(
        family_key="mlb_props_v1",
        target_scope="single",
        sports=("MLB",),
        market_types=("player_prop",),
        required_feature_groups=("player_form", "probable_starters", "bullpen_context", "park_factors", "weather", "kalshi_context"),
    ),
)


DIRECT_PARLAY_FAMILIES = (
    ModelFamily(
        family_key="nba_parlay_2leg_v1",
        target_scope="parlay",
        sports=("NBA",),
        market_types=("winner", "player_prop"),
        required_feature_groups=("leg_context", "correlation_features", "kalshi_context"),
    ),
    ModelFamily(
        family_key="nba_parlay_3leg_v1",
        target_scope="parlay",
        sports=("NBA",),
        market_types=("winner", "player_prop"),
        required_feature_groups=("leg_context", "correlation_features", "kalshi_context"),
    ),
    ModelFamily(
        family_key="mlb_parlay_2leg_v1",
        target_scope="parlay",
        sports=("MLB",),
        market_types=("winner", "first_five_winner", "player_prop"),
        required_feature_groups=("leg_context", "correlation_features", "kalshi_context"),
    ),
    ModelFamily(
        family_key="mlb_parlay_3leg_v1",
        target_scope="parlay",
        sports=("MLB",),
        market_types=("winner", "first_five_winner", "player_prop"),
        required_feature_groups=("leg_context", "correlation_features", "kalshi_context"),
    ),
    ModelFamily(
        family_key="mixed_parlay_2leg_v1",
        target_scope="parlay",
        sports=("NBA", "MLB"),
        market_types=("winner", "first_five_winner", "player_prop"),
        required_feature_groups=("leg_context", "cross_sport_features", "correlation_features", "kalshi_context"),
    ),
    ModelFamily(
        family_key="mixed_parlay_3leg_v1",
        target_scope="parlay",
        sports=("NBA", "MLB"),
        market_types=("winner", "first_five_winner", "player_prop"),
        required_feature_groups=("leg_context", "cross_sport_features", "correlation_features", "kalshi_context"),
    ),
)
