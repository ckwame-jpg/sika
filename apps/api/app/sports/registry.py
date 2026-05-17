from app.sports.head_to_head import HeadToHeadSportAdapter
from app.sports.team import TeamSportAdapter


def build_registry() -> dict[str, object]:
    return {
        "NBA": TeamSportAdapter("NBA", "Basketball"),
        "NFL": TeamSportAdapter("NFL", "American Football"),
        "MLB": TeamSportAdapter("MLB", "Baseball"),
        "TENNIS": HeadToHeadSportAdapter("TENNIS", "Tennis"),
        # Smarter WNBA PR 6 — depends on PR 1's ESPN ``/wnba/`` URL constants
        # and 15-team WNBA abbreviation map. The provider name is "Basketball"
        # (matches NBA) because ESPN's WNBA scoreboard payload shape is
        # identical to NBA's — ``TeamSportAdapter.normalize_event`` shares
        # the basketball normalization path for both.
        "WNBA": TeamSportAdapter("WNBA", "Basketball"),
    }


ADAPTERS = build_registry()
