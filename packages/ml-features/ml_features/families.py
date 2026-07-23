from __future__ import annotations


SUPPORTED_SPORTS: tuple[str, ...] = ("NBA", "MLB", "WNBA", "NFL")

DEFAULT_SERVE_FAMILY_KEYS: tuple[str, ...] = (
    "mlb_props",
    "nba_props",
    "wnba_props",
    "nfl_props",
    "mlb_singles",
    "nba_singles",
    "wnba_singles",
    "nfl_singles",
)


def single_family_key(sport_key: str | None, market_family: str | None) -> str:
    """Map a single-market row to its canonical model family."""
    sport = (sport_key or "").upper()
    family = (market_family or "").lower()
    if family == "player_prop":
        if sport == "NBA":
            return "nba_props"
        if sport == "MLB":
            return "mlb_props"
        if sport == "WNBA":
            return "wnba_props"
        if sport == "NFL":
            return "nfl_props"
    if sport == "NBA":
        return "nba_singles"
    if sport == "MLB":
        return "mlb_singles"
    if sport == "WNBA":
        return "wnba_singles"
    return f"{sport.lower()}_singles" if sport else "unknown_singles"


def sport_one_hot(sport_key: str | None) -> dict[str, float]:
    """Return the training-time sport indicators for one prediction row."""
    sport = (sport_key or "").upper()
    return {
        "sport_is_nba": 1.0 if sport == "NBA" else 0.0,
        "sport_is_mlb": 1.0 if sport == "MLB" else 0.0,
        "sport_is_wnba": 1.0 if sport == "WNBA" else 0.0,
        "sport_is_nfl": 1.0 if sport == "NFL" else 0.0,
    }


__all__ = [
    "DEFAULT_SERVE_FAMILY_KEYS",
    "SUPPORTED_SPORTS",
    "single_family_key",
    "sport_one_hot",
]
