import re
from datetime import datetime
from typing import Any


UNSUPPORTED_MARKET_KEYWORDS = ("CROSSCATEGORY", "MULTIGAME", "PARLAY", "SAMEGAME", "SGP", "COMBO", "EXTENDED")
UNSUPPORTED_WINNER_CONTEXTS = (
    "half winner",
    "quarter winner",
    "period winner",
    "inning winner",
    "innings winner",
    "first 5 innings",
    "first five innings",
    "set winner",
    "round winner",
)
# Iteration order is load-bearing here — ``infer_market_sport_key``
# returns the FIRST sport whose hints match, and ``"NBA"`` is a
# substring of ``"WNBA"`` (so a ``KXWNBA…`` ticker would otherwise
# classify as NBA). Keep WNBA above NBA so the more-specific match
# wins. If a third sport with a token that contains an existing sport
# code lands (e.g. some hypothetical ``NBAG``), apply the same rule:
# the longer / more-specific code goes first.
SUPPORTED_SPORT_HINTS = {
    "WNBA": ("WNBA",),
    "NBA": ("NBA",),
    "NFL": ("NFL",),
    "MLB": ("MLB",),
    "SOCCER": ("SOCCER", "MLS", "EPL", "UEFA", "UCL", "FIFA", "EURO", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1"),
    "TENNIS": ("TENNIS", "ATP", "WTA", "ITF", "CHALLENGER"),
}
SUPPORTED_COMBO_PROP_FAMILIES = {
    "NBA": frozenset({"PTS", "REB", "AST", "PR", "PA", "RA", "PRA", "3PM", "STL", "BLK", "TOV"}),
    "MLB": frozenset({"H", "HIT", "HITS", "R", "RUN", "RUNS", "RBI", "RBIS", "HR", "BB", "TB", "HRR"}),
    # WNBA mirrors NBA's box-score stat set — points / rebounds / assists /
    # made threes / steals / blocks / turnovers all map to the same ESPN
    # gamelog payload keys (PR 3 will share _build_nba_game_logs).
    "WNBA": frozenset({"PTS", "REB", "AST", "PR", "PA", "RA", "PRA", "3PM", "STL", "BLK", "TOV"}),
}
BLOCKED_COMBO_LEG_FAMILY_PREFIXES = {
    "NBA": ("GAME", "SPREAD", "TOTAL", "WINNER", "1H", "2H", "1Q", "2Q", "3Q", "4Q"),
    "MLB": ("GAME", "SPREAD", "TOTAL", "WINNER", "F5"),
    # WNBA has the same 4-quarter game structure as NBA, plus 1H/2H halves
    # for live-betting splits — block the same prefixes from being treated
    # as player-prop combo legs.
    "WNBA": ("GAME", "SPREAD", "TOTAL", "WINNER", "1H", "2H", "1Q", "2Q", "3Q", "4Q"),
}
KNOWN_UNSUPPORTED_COMBO_PROP_FAMILIES = {
    "NBA": frozenset(),
    "MLB": frozenset({"SO", "IP", "OUTS", "ER"}),
    "WNBA": frozenset(),
}

PLAYER_PROP_TITLE_RE = re.compile(
    r"^(?P<subject>.+?):\s*(?P<threshold>\d+(?:\.\d+)?)\+\s+(?P<phrase>.+?)(?:\?)?$",
    re.IGNORECASE,
)
GAME_SPREAD_TITLE_RE = re.compile(
    r"^(?P<subject>.+?)\s+wins\s+by\s+over\s+(?P<threshold>\d+(?:\.\d+)?)\s+(?P<unit>points|runs)(?:\?)?$",
    re.IGNORECASE,
)
GAME_TOTAL_TITLE_RE = re.compile(
    r"^(?P<direction>over|under)\s+(?P<threshold>\d+(?:\.\d+)?)\s+(?P<unit>points|runs)\s+scored(?:\?)?$",
    re.IGNORECASE,
)
TICKER_TEAM_HINT_RE = re.compile(r"-(?P<segment>[A-Z0-9']+)-\d+(?:\.\d+)?$")
NBA_PROP_ALIASES = {
    "points": "points",
    "point": "points",
    "rebounds": "rebounds",
    "rebound": "rebounds",
    "assists": "assists",
    "assist": "assists",
    "made threes": "made_threes",
    "made three": "made_threes",
    "threes": "made_threes",
    "three": "made_threes",
    "three pointers made": "made_threes",
    "three pointer made": "made_threes",
    "three pointers": "made_threes",
    "three pointer": "made_threes",
    "3 pointers": "made_threes",
    "3 pointer": "made_threes",
    "3-pointers": "made_threes",
    "3-pointer": "made_threes",
    "steals": "steals",
    "steal": "steals",
    "blocks": "blocks",
    "block": "blocks",
    "turnovers": "turnovers",
    "turnover": "turnovers",
}
MLB_PROP_ALIASES = {
    "hits": "hits",
    "hit": "hits",
    "runs": "runs",
    "run": "runs",
    "home runs": "home_runs",
    "home run": "home_runs",
    "rbis": "rbis",
    "rbi": "rbis",
    "walks": "walks",
    "walk": "walks",
    "strikeouts": "strikeouts",
    "strikeout": "strikeouts",
    "total bases": "total_bases",
    "total base": "total_bases",
}
# WNBA prop stat phrases mirror NBA's — same box-score stat set, same
# Kalshi prop title phrasing (verified for "<player>: <N>+ points" /
# "rebounds" / "assists" / "made threes" patterns at PR 2 time). Kept as
# a distinct dict (rather than aliased to NBA_PROP_ALIASES) so future
# WNBA-only colloquialisms (e.g. league-specific shorthand) can land
# without touching the NBA dict.
WNBA_PROP_ALIASES = dict(NBA_PROP_ALIASES)
PROP_COMPONENT_ORDER = {
    "NBA": {
        "points": 0,
        "rebounds": 1,
        "assists": 2,
        "made_threes": 3,
        "steals": 4,
        "blocks": 5,
        "turnovers": 6,
    },
    "MLB": {
        "hits": 0,
        "runs": 1,
        "rbis": 2,
        "home_runs": 3,
        "walks": 4,
        "strikeouts": 5,
        "total_bases": 6,
    },
    "WNBA": {
        "points": 0,
        "rebounds": 1,
        "assists": 2,
        "made_threes": 3,
        "steals": 4,
        "blocks": 5,
        "turnovers": 6,
    },
}


def parse_market_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def market_anchor_time(payload: dict) -> datetime | None:
    for field in ("expected_expiration_time", "close_time", "latest_expiration_time", "open_time"):
        parsed = parse_market_datetime(payload.get(field))
        if parsed is not None:
            return parsed
    return None


def infer_yes_label(payload: dict) -> str | None:
    yes_sub_title = str(payload.get("yes_sub_title") or "").strip()
    if yes_sub_title:
        yes_sub_title = re.sub(
            r"\s+wins?(?:\s+first\s+5\s+innings|\s+first\s+five\s+innings|\s+the\s+game)?$",
            "",
            yes_sub_title,
            flags=re.IGNORECASE,
        ).strip()
        return yes_sub_title

    title = str(payload.get("title") or "").strip()
    beat_match = re.match(r"will\s+(.+?)\s+beat\s+.+\?$", title, flags=re.IGNORECASE)
    if beat_match:
        return beat_match.group(1).strip()
    return None


def infer_market_sport_key(payload: dict) -> str | None:
    lookup = " ".join(
        str(payload.get(field) or "").upper()
        for field in ("event_ticker", "ticker", "series_ticker", "primary_participant_key")
    )
    for sport_key, hints in SUPPORTED_SPORT_HINTS.items():
        if any(hint in lookup for hint in hints):
            return sport_key
    return None


def combo_leg_metadata_prefilter(payload: dict[str, Any]) -> dict[str, Any]:
    market_ticker = str(payload.get("market_ticker") or payload.get("ticker") or "").strip().upper()
    event_ticker = str(payload.get("event_ticker") or "").strip().upper()
    sport_key = infer_market_sport_key({"ticker": market_ticker, "event_ticker": event_ticker})
    family_code = _combo_leg_family_code(market_ticker, sport_key)

    result: dict[str, Any] = {
        "supported": False,
        "sport_key": sport_key,
        "market_family_code": family_code,
        "reason": None,
    }

    if not market_ticker:
        result["reason"] = "missing_identity"
        return result
    if sport_key not in {"NBA", "MLB", "WNBA"}:
        result["reason"] = "unsupported_sport"
        return result
    if not family_code:
        result["reason"] = "unsupported_market"
        return result
    if _combo_leg_family_is_blocked(sport_key, family_code):
        result["reason"] = "unsupported_market_family"
        return result
    if family_code in KNOWN_UNSUPPORTED_COMBO_PROP_FAMILIES.get(sport_key, frozenset()):
        result["reason"] = "unsupported_prop_category"
        return result
    if family_code in SUPPORTED_COMBO_PROP_FAMILIES.get(sport_key, frozenset()):
        result["supported"] = True
        return result
    if not _combo_leg_has_player_prop_shape(market_ticker):
        result["reason"] = "unsupported_market_shape"
        return result

    result["supported"] = True
    return result


def classify_market_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ticker = str(payload.get("ticker") or "").upper()
    title = str(payload.get("title") or "").strip()
    lowered_title = title.lower()
    event_ticker = str(payload.get("event_ticker") or "").upper()
    sport_key = infer_market_sport_key(payload)

    result: dict[str, Any] = {
        "supported": False,
        "sport_key": sport_key,
        "metadata": None,
        "reason": None,
        "prop_category": None,
    }

    if not ticker or not title or not event_ticker:
        result["reason"] = "missing_identity"
        return result
    if payload.get("mve_collection_ticker") or payload.get("mve_selected_legs"):
        result["reason"] = "mve_market"
        return result
    if any(keyword in ticker for keyword in UNSUPPORTED_MARKET_KEYWORDS):
        result["reason"] = "unsupported_combo"
        return result
    if sport_key is None:
        result["reason"] = "unsupported_sport"
        return result

    winner_metadata = _winner_market_metadata(payload, sport_key, lowered_title)
    if winner_metadata:
        result["supported"] = True
        result["metadata"] = winner_metadata
        return result

    game_line_metadata = _game_line_metadata(payload, sport_key)
    if game_line_metadata:
        result["supported"] = True
        result["metadata"] = game_line_metadata
        return result

    prop_result = _player_prop_metadata(payload, sport_key)
    if prop_result:
        if prop_result.get("unsupported_reason"):
            result["reason"] = prop_result["unsupported_reason"]
            result["prop_category"] = prop_result.get("prop_category")
            return result
        result["supported"] = True
        result["metadata"] = prop_result
        return result

    result["reason"] = "unsupported_market"
    return result


def market_metadata(payload: dict[str, Any]) -> dict[str, Any] | None:
    classification = classify_market_payload(payload)
    return classification.get("metadata")


def infer_supported_market_kind(payload: dict) -> str | None:
    metadata = market_metadata(payload)
    if not metadata:
        return None
    return str(metadata.get("copilot_market_kind") or "")


def _winner_market_metadata(payload: dict[str, Any], sport_key: str, lowered_title: str) -> dict[str, Any] | None:
    yes_label = infer_yes_label(payload)
    if not yes_label or yes_label.lower() == "tie":
        return None

    if "first 5 innings winner?" in lowered_title or "first five innings winner?" in lowered_title:
        if sport_key != "MLB":
            return None
        return {
            "copilot_market_family": "winner",
            "copilot_market_kind": "first_five_winner",
            "copilot_direction": "yes",
            "copilot_subject_name": yes_label,
        }

    if "winner?" in lowered_title:
        if any(context in lowered_title for context in UNSUPPORTED_WINNER_CONTEXTS):
            return None
        return {
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "copilot_direction": "yes",
            "copilot_subject_name": yes_label,
        }

    if lowered_title.startswith("will ") and " beat " in lowered_title:
        return {
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "copilot_direction": "yes",
            "copilot_subject_name": yes_label,
        }

    return None


def _game_line_metadata(payload: dict[str, Any], sport_key: str) -> dict[str, Any] | None:
    if sport_key not in {"NBA", "NFL", "MLB", "WNBA", "SOCCER"}:
        return None

    title = str(payload.get("title") or "").strip()
    spread_match = GAME_SPREAD_TITLE_RE.match(title)
    if spread_match:
        threshold = float(spread_match.group("threshold"))
        subject_name = spread_match.group("subject").strip()
        unit = spread_match.group("unit").lower()
        return {
            "copilot_market_family": "game_line",
            "copilot_market_kind": "spread",
            "copilot_stat_key": f"margin_{unit}",
            "copilot_threshold": threshold,
            "copilot_direction": "over",
            "copilot_subject_name": subject_name,
            "copilot_unit": unit,
            "copilot_display_market_title": f"{subject_name} wins by over {threshold:g} {unit}",
            "copilot_display_line_label": f"{subject_name} -{threshold:g}",
        }

    total_match = GAME_TOTAL_TITLE_RE.match(title)
    if total_match:
        threshold = float(total_match.group("threshold"))
        direction = total_match.group("direction").lower()
        unit = total_match.group("unit").lower()
        return {
            "copilot_market_family": "game_line",
            "copilot_market_kind": "total",
            "copilot_stat_key": f"total_{unit}",
            "copilot_threshold": threshold,
            "copilot_direction": direction,
            "copilot_unit": unit,
            "copilot_display_market_title": f"{direction.title()} {threshold:g} {unit} scored",
            "copilot_display_line_label": f"{direction.title()} {threshold:g}",
        }

    return None


def _player_prop_metadata(payload: dict[str, Any], sport_key: str) -> dict[str, Any] | None:
    if sport_key not in {"NBA", "MLB", "WNBA"}:
        return None

    title = str(payload.get("title") or "").strip()
    match = PLAYER_PROP_TITLE_RE.match(title)
    if not match:
        return None

    subject_name = match.group("subject").strip()
    threshold = float(match.group("threshold"))
    raw_phrase = match.group("phrase").strip()
    prop_category = _slugify_prop_category(raw_phrase)
    component_keys = _component_stat_keys(sport_key, raw_phrase)
    if not component_keys:
        return {
            "unsupported_reason": "unsupported_prop_category",
            "prop_category": prop_category,
        }

    if sport_key == "MLB" and not _is_supported_mlb_batter_prop(payload):
        return {
            "unsupported_reason": "unsupported_prop_category",
            "prop_category": prop_category,
        }

    stat_key = _combined_stat_key(sport_key, component_keys)
    if not stat_key:
        return {
            "unsupported_reason": "unsupported_prop_category",
            "prop_category": prop_category,
        }

    team_hint = _subject_team_hint(payload)
    return {
        "copilot_market_family": "player_prop",
        "copilot_market_kind": "player_prop",
        "copilot_stat_key": stat_key,
        "copilot_component_stat_keys": component_keys,
        "copilot_threshold": threshold,
        "copilot_direction": "over",
        "copilot_subject_name": subject_name,
        "copilot_subject_team": team_hint,
        "copilot_requires_lineup": _requires_lineup_confirmation(payload),
    }


_SPORT_KEY_TO_COMBO_TICKER_PREFIX = {
    "NBA": "KXNBA",
    "MLB": "KXMLB",
    "WNBA": "KXWNBA",
}


def _combo_leg_family_code(market_ticker: str, sport_key: str | None) -> str | None:
    prefix = _SPORT_KEY_TO_COMBO_TICKER_PREFIX.get(sport_key or "")
    if prefix is None:
        return None
    if not market_ticker.startswith(prefix):
        return None
    family_code, _separator, _rest = market_ticker[len(prefix) :].partition("-")
    return family_code or None


def _combo_leg_family_is_blocked(sport_key: str, family_code: str) -> bool:
    return any(family_code.startswith(prefix) for prefix in BLOCKED_COMBO_LEG_FAMILY_PREFIXES.get(sport_key, ()))


def _combo_leg_has_player_prop_shape(market_ticker: str) -> bool:
    parts = market_ticker.split("-")
    return len(parts) >= 4 and len(parts[-2]) > 4


_PROP_ALIASES_BY_SPORT = {
    "NBA": NBA_PROP_ALIASES,
    "MLB": MLB_PROP_ALIASES,
    "WNBA": WNBA_PROP_ALIASES,
}


def _component_stat_keys(sport_key: str, raw_phrase: str) -> list[str] | None:
    aliases = _PROP_ALIASES_BY_SPORT.get(sport_key)
    if aliases is None:
        return None
    components: list[str] = []
    for part in raw_phrase.split("+"):
        normalized = _normalize_prop_component(part)
        stat_key = aliases.get(normalized)
        if not stat_key:
            return None
        if stat_key not in components:
            components.append(stat_key)
    return components or None


def _normalize_prop_component(value: str) -> str:
    lowered = value.lower().strip(" ?")
    replacements = {
        "3-pointers made": "made threes",
        "3-pointers": "made threes",
        "3 pointer": "made threes",
        "3 pointers": "made threes",
        "three-pointers made": "made threes",
        "three-pointers": "made threes",
        "three pointer": "made threes",
        "three pointers": "made threes",
        "three point field goals made": "made threes",
        "three point field goals": "made threes",
        "rbi's": "rbis",
    }
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def _combined_stat_key(sport_key: str, component_keys: list[str]) -> str | None:
    if not component_keys:
        return None
    if len(component_keys) == 1:
        return component_keys[0]

    order_map = PROP_COMPONENT_ORDER[sport_key]
    try:
        ordered = sorted(component_keys, key=lambda key: order_map[key])
    except KeyError:
        return None
    return "_".join(ordered)


def _requires_lineup_confirmation(payload: dict[str, Any]) -> bool:
    rules = " ".join(str(payload.get(field) or "") for field in ("rules_secondary", "rules_primary")).lower()
    return "starting lineup" in rules or "plate appearance" in rules


def _is_supported_mlb_batter_prop(payload: dict[str, Any]) -> bool:
    rules = " ".join(str(payload.get(field) or "") for field in ("rules_secondary", "rules_primary")).lower()
    if "plate appearance" in rules or "starting lineup" in rules:
        return True
    if "innings pitched" in rules or "batters faced" in rules or "recorded outs" in rules:
        return False
    return False


def _subject_team_hint(payload: dict[str, Any]) -> str | None:
    match = TICKER_TEAM_HINT_RE.search(str(payload.get("ticker") or ""))
    if not match:
        return None
    segment = match.group("segment")
    if len(segment) < 3:
        return segment
    return segment[:3]


def _slugify_prop_category(value: str) -> str:
    normalized = _normalize_prop_component(value)
    normalized = normalized.replace("+", " plus ")
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
