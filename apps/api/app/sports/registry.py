from app.config import get_settings
from app.sports.head_to_head import HeadToHeadSportAdapter
from app.sports.team import TeamSportAdapter


def build_registry() -> dict[str, object]:
    settings = get_settings()
    return {
        "NBA": TeamSportAdapter("NBA", "Basketball"),
        "NFL": TeamSportAdapter("NFL", "American Football"),
        "MLB": TeamSportAdapter("MLB", "Baseball"),
        "SOCCER": TeamSportAdapter("SOCCER", "Soccer", league_whitelist=settings.soccer_leagues),
        "TENNIS": HeadToHeadSportAdapter("TENNIS", "Tennis"),
        "UFC": HeadToHeadSportAdapter("UFC", "Mixed Martial Arts"),
    }


ADAPTERS = build_registry()
