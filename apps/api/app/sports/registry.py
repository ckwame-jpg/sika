from app.sports.team import TeamSportAdapter


def build_registry() -> dict[str, object]:
    return {
        "NBA": TeamSportAdapter("NBA", "Basketball"),
        "NFL": TeamSportAdapter("NFL", "American Football"),
        "MLB": TeamSportAdapter("MLB", "Baseball"),
    }


ADAPTERS = build_registry()
