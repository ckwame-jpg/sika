from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from typing import Any

from app.clients.espn import EspnPublicClient


SUPPORTED_STATS_SPORTS = {"NBA", "NFL", "MLB"}

_QUESTION_PREFIX_RE = re.compile(r"^(?:what(?:'s| is| are)|show me|give me|tell me)\s+", re.IGNORECASE)
_QUESTION_SUFFIX_RE = re.compile(r"[?.!]+\s*$")
_WHITESPACE_RE = re.compile(r"\s+")
_OPPONENT_FILTER_RE = re.compile(
    r"\b(?:vs\.?|versus|against)\s+(?P<opponent>[a-z0-9 .'\-]+?)(?=(?:\s+(?:in|over|for|this|last|games?|season|stats))|$)",
    re.IGNORECASE,
)
_LAST_N_PATTERNS = (
    re.compile(r"^(?P<player>.+?)'?s stats(?: in| over)? the last (?P<n>\d+) (?:games?|matches?|fights?)$", re.IGNORECASE),
    re.compile(r"^(?P<player>.+?)'?s stats last (?P<n>\d+) (?:games?|matches?|fights?)$", re.IGNORECASE),
    re.compile(r"^(?P<player>.+?)'?s last (?P<n>\d+) (?:games?|matches?|fights?)$", re.IGNORECASE),
    re.compile(r"^(?P<player>.+?) last (?P<n>\d+) (?:games?|matches?|fights?)$", re.IGNORECASE),
)
_SEASON_PATTERNS = (
    re.compile(r"^(?P<player>.+?)'?s stats this season$", re.IGNORECASE),
    re.compile(r"^(?P<player>.+?) this season$", re.IGNORECASE),
    re.compile(r"^(?P<player>.+?)'?s season stats$", re.IGNORECASE),
    re.compile(r"^(?P<player>.+?) season stats$", re.IGNORECASE),
)
_METRIC_LABELS = {
    "NBA": {
        "minutes": "Minutes",
        "points": "Points",
        "rebounds": "Rebounds",
        "assists": "Assists",
        "made_threes": "3PM",
        "steals": "Steals",
        "blocks": "Blocks",
        "turnovers": "Turnovers",
        "field_goal_pct": "FG%",
        "three_point_pct": "3P%",
        "free_throw_pct": "FT%",
    },
    "NFL": {
        "completions": "Completions",
        "passing_attempts": "Pass Attempts",
        "passing_yards": "Pass Yards",
        "completion_pct": "Comp%",
        "yards_per_pass_attempt": "YPA",
        "passing_touchdowns": "Pass TD",
        "interceptions": "INT",
        "sacks": "Sacks",
        "qbr": "QBR",
        "rushing_attempts": "Rush Attempts",
        "rushing_yards": "Rush Yards",
        "yards_per_rush_attempt": "YPC",
        "rushing_touchdowns": "Rush TD",
    },
    "MLB": {
        "at_bats": "AB",
        "hits": "Hits",
        "runs": "Runs",
        "home_runs": "HR",
        "rbis": "RBI",
        "walks": "BB",
        "strikeouts": "SO",
        "total_bases": "TB",
        "batting_avg": "AVG",
        "on_base_pct": "OBP",
        "slugging_pct": "SLG",
        "ops": "OPS",
    },
}

_EXPLANATION_KEYS = {
    "NBA": ("points", "assists", "rebounds", "minutes"),
    "NFL": ("passing_yards", "passing_touchdowns", "rushing_yards", "qbr"),
    "MLB": ("batting_avg", "home_runs", "rbis", "ops"),
}

_STAT_LINE_SPECS = {
    "NBA": (
        ("points", "point", "points"),
        ("assists", "assist", "assists"),
        ("rebounds", "rebound", "rebounds"),
        ("minutes", "minute", "minutes"),
    ),
    "NFL": (
        ("passing_yards", "pass yard", "pass yards"),
        ("passing_touchdowns", "pass TD", "pass TD"),
        ("rushing_yards", "rush yard", "rush yards"),
        ("qbr", "QBR", "QBR"),
    ),
    "MLB": (
        ("hits", "hit", "hits"),
        ("home_runs", "HR", "HR"),
        ("rbis", "RBI", "RBI"),
        ("ops", "OPS", "OPS"),
    ),
}


@dataclass(slots=True)
class ParsedStatsQuery:
    sport_key: str
    player_name: str
    query_type: str
    season: int
    games_requested: int | None = None
    split: str | None = None
    opponent: str | None = None


class StatsQueryService:
    def __init__(self, espn_client: EspnPublicClient | None = None):
        self.espn_client = espn_client or EspnPublicClient()

    def query(self, question: str, sport_key: str = "NBA", season: int | None = None) -> dict[str, Any]:
        parsed = parse_stats_question(question, sport_key=sport_key, season=season)
        player = self.espn_client.search_player(parsed.player_name, sport_key=parsed.sport_key)
        gamelog_payload = self.espn_client.fetch_player_gamelog(parsed.sport_key, player["athlete_id"], parsed.season)

        game_logs = _build_game_logs(parsed.sport_key, gamelog_payload)
        game_logs = _apply_filters(game_logs, parsed)
        if parsed.query_type == "last_n_games" and parsed.games_requested is not None:
            game_logs = game_logs[: parsed.games_requested]

        if not game_logs:
            raise LookupError(f"No {parsed.sport_key} game logs matched the query for {player['display_name']}")

        summary_metrics = _build_summary_metrics(parsed.sport_key, game_logs)
        return {
            "question": question,
            "sport_key": parsed.sport_key,
            "entity_name": player["display_name"],
            "entity_id": player["athlete_id"],
            "team_name": player.get("team_name"),
            "query_type": parsed.query_type,
            "season": parsed.season,
            "games_requested": parsed.games_requested,
            "games_analyzed": len(game_logs),
            "split": parsed.split,
            "opponent": parsed.opponent,
            "metric_labels": _METRIC_LABELS[parsed.sport_key],
            "summary": {
                "games": len(game_logs),
                "wins": sum(1 for item in game_logs if item.get("result") == "W"),
                "losses": sum(1 for item in game_logs if item.get("result") == "L"),
                "draws": sum(1 for item in game_logs if item.get("result") == "D"),
                "metrics": summary_metrics,
                "stat_line": _build_stat_line(parsed.sport_key, summary_metrics),
            },
            "game_logs": [_serialize_game_log(item) for item in game_logs],
            "explanation": _build_explanation(player["display_name"], parsed, summary_metrics, len(game_logs)),
            "source": "espn_public",
        }

def parse_stats_question(question: str, sport_key: str = "NBA", season: int | None = None) -> ParsedStatsQuery:
    normalized_sport = sport_key.upper()
    if normalized_sport not in SUPPORTED_STATS_SPORTS:
        raise ValueError("Stats query currently supports NBA, NFL, and MLB only")

    cleaned = _normalize_question(question)
    if not cleaned:
        raise ValueError("Question is required")

    split = None
    lowered = cleaned.lower()
    if "at home" in lowered or re.search(r"\bhome\b", lowered):
        split = "home"
        cleaned = re.sub(r"\bat home\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bhome\b", "", cleaned, flags=re.IGNORECASE)
    elif "on the road" in lowered or re.search(r"\b(?:away|road)\b", lowered):
        split = "away"
        cleaned = re.sub(r"\bon the road\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(?:away|road)\b", "", cleaned, flags=re.IGNORECASE)

    opponent = None
    opponent_match = _OPPONENT_FILTER_RE.search(cleaned)
    if opponent_match:
        opponent = _clean_phrase(opponent_match.group("opponent"))
        cleaned = _OPPONENT_FILTER_RE.sub("", cleaned)

    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()

    for pattern in _LAST_N_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            games_requested = int(match.group("n"))
            if games_requested <= 0:
                raise ValueError("Games requested must be positive")
            return ParsedStatsQuery(
                sport_key=normalized_sport,
                player_name=_clean_phrase(match.group("player")),
                query_type="last_n_games",
                season=season or default_season_for_sport(normalized_sport),
                games_requested=games_requested,
                split=split,
                opponent=opponent,
            )

    for pattern in _SEASON_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            return ParsedStatsQuery(
                sport_key=normalized_sport,
                player_name=_clean_phrase(match.group("player")),
                query_type="season",
                season=season or default_season_for_sport(normalized_sport),
                split=split,
                opponent=opponent,
            )

    raise ValueError(
        "Unsupported stats query. Try 'Jalen Brunson last 10 games', 'Patrick Mahomes this season', "
        "or 'Aaron Judge last 10 games'."
    )


def default_season_for_sport(sport_key: str, reference_date: date | None = None) -> int:
    today = reference_date or date.today()
    if sport_key == "NBA":
        return today.year + 1 if today.month >= 10 else today.year
    if sport_key == "NFL":
        return today.year if today.month >= 8 else today.year - 1
    if sport_key == "MLB":
        return today.year if today.month >= 3 else today.year - 1
    if sport_key == "SOCCER":
        return today.year
    if sport_key == "TENNIS":
        return today.year
    return today.year


def _normalize_question(question: str) -> str:
    cleaned = _QUESTION_SUFFIX_RE.sub("", question.strip())
    cleaned = _QUESTION_PREFIX_RE.sub("", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _clean_phrase(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def _build_game_logs(sport_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if sport_key == "NBA":
        return _build_nba_game_logs(payload)
    if sport_key == "NFL":
        return _build_nfl_game_logs(payload)
    if sport_key == "MLB":
        return _build_mlb_game_logs(payload)
    raise ValueError(f"Unsupported stats sport: {sport_key}")


def _build_nba_game_logs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stat_names = payload.get("names") or []
    event_metadata = payload.get("events") or {}
    game_logs: dict[str, dict[str, Any]] = {}

    for season_type in payload.get("seasonTypes") or []:
        for category in season_type.get("categories") or []:
            for event_stats in category.get("events") or []:
                event_id = str(event_stats.get("eventId") or "")
                if not event_id:
                    continue
                metadata = event_metadata.get(event_id) or {}
                stats = event_stats.get("stats") or []
                stat_map = {name: stats[index] if index < len(stats) else None for index, name in enumerate(stat_names)}
                raw_metrics = {
                    "minutes": _parse_minutes(stat_map.get("minutes")),
                    "points": _parse_number(stat_map.get("points")),
                    "rebounds": _parse_number(stat_map.get("totalRebounds")),
                    "assists": _parse_number(stat_map.get("assists")),
                    "steals": _parse_number(stat_map.get("steals")),
                    "blocks": _parse_number(stat_map.get("blocks")),
                    "turnovers": _parse_number(stat_map.get("turnovers")),
                    "field_goals_made": _parse_made_attempted(stat_map.get("fieldGoalsMade-fieldGoalsAttempted"))[0],
                    "field_goals_attempted": _parse_made_attempted(stat_map.get("fieldGoalsMade-fieldGoalsAttempted"))[1],
                    "three_points_made": _parse_made_attempted(stat_map.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))[0],
                    "three_points_attempted": _parse_made_attempted(stat_map.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))[1],
                    "free_throws_made": _parse_made_attempted(stat_map.get("freeThrowsMade-freeThrowsAttempted"))[0],
                    "free_throws_attempted": _parse_made_attempted(stat_map.get("freeThrowsMade-freeThrowsAttempted"))[1],
                }
                game_logs[event_id] = _build_game_entry("NBA", metadata, event_id, raw_metrics, _nba_metrics_for_game(raw_metrics))

    return sorted(game_logs.values(), key=lambda item: item["game_date"], reverse=True)


def _build_nfl_game_logs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stat_names = payload.get("names") or []
    event_metadata = payload.get("events") or {}
    game_logs: dict[str, dict[str, Any]] = {}

    for season_type in payload.get("seasonTypes") or []:
        for category in season_type.get("categories") or []:
            for event_stats in category.get("events") or []:
                event_id = str(event_stats.get("eventId") or "")
                if not event_id:
                    continue
                metadata = event_metadata.get(event_id) or {}
                stats = event_stats.get("stats") or []
                stat_map = {name: stats[index] if index < len(stats) else None for index, name in enumerate(stat_names)}
                raw_metrics = {
                    "completions": _parse_number(stat_map.get("completions")),
                    "passing_attempts": _parse_number(stat_map.get("passingAttempts")),
                    "passing_yards": _parse_number(stat_map.get("passingYards")),
                    "passing_touchdowns": _parse_number(stat_map.get("passingTouchdowns")),
                    "interceptions": _parse_number(stat_map.get("interceptions")),
                    "sacks": _parse_number(stat_map.get("sacks")),
                    "qbr": _parse_number(stat_map.get("adjQBR")),
                    "rushing_attempts": _parse_number(stat_map.get("rushingAttempts")),
                    "rushing_yards": _parse_number(stat_map.get("rushingYards")),
                    "rushing_touchdowns": _parse_number(stat_map.get("rushingTouchdowns")),
                }
                game_logs[event_id] = _build_game_entry("NFL", metadata, event_id, raw_metrics, _nfl_metrics_for_game(raw_metrics))

    return sorted(game_logs.values(), key=lambda item: item["game_date"], reverse=True)


def _build_mlb_game_logs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stat_names = payload.get("names") or []
    event_metadata = payload.get("events") or {}
    game_logs: dict[str, dict[str, Any]] = {}

    for season_type in payload.get("seasonTypes") or []:
        for category in season_type.get("categories") or []:
            for event_stats in category.get("events") or []:
                event_id = str(event_stats.get("eventId") or "")
                if not event_id:
                    continue
                metadata = event_metadata.get(event_id) or {}
                stats = event_stats.get("stats") or []
                stat_map = {name: stats[index] if index < len(stats) else None for index, name in enumerate(stat_names)}
                raw_metrics = {
                    "at_bats": _parse_number(stat_map.get("atBats")),
                    "runs": _parse_number(stat_map.get("runs")),
                    "hits": _parse_number(stat_map.get("hits")),
                    "doubles": _parse_number(stat_map.get("doubles")),
                    "triples": _parse_number(stat_map.get("triples")),
                    "home_runs": _parse_number(stat_map.get("homeRuns")),
                    "rbis": _parse_number(stat_map.get("RBIs")),
                    "walks": _parse_number(stat_map.get("walks")),
                    "hit_by_pitch": _parse_number(stat_map.get("hitByPitch")),
                    "strikeouts": _parse_number(stat_map.get("strikeouts")),
                }
                game_logs[event_id] = _build_game_entry("MLB", metadata, event_id, raw_metrics, _mlb_metrics_for_game(raw_metrics))

    return sorted(game_logs.values(), key=lambda item: item["game_date"], reverse=True)


def _build_game_entry(
    sport_key: str,
    metadata: dict[str, Any],
    event_id: str,
    raw_metrics: dict[str, float],
    metrics: dict[str, float | None],
) -> dict[str, Any]:
    location = "home" if metadata.get("atVs") == "vs" else "away"
    team_score = metadata.get("homeTeamScore") if location == "home" else metadata.get("awayTeamScore")
    opponent_score = metadata.get("awayTeamScore") if location == "home" else metadata.get("homeTeamScore")
    opponent = metadata.get("opponent") or {}
    return {
        "sport_key": sport_key,
        "game_id": event_id,
        "game_date": _parse_datetime(metadata.get("gameDate")),
        "location": location,
        "opponent": opponent.get("displayName") or opponent.get("abbreviation") or "Unknown",
        "opponent_abbreviation": opponent.get("abbreviation"),
        "result": metadata.get("gameResult"),
        "team_score": _parse_number(team_score),
        "opponent_score": _parse_number(opponent_score),
        "raw_metrics": raw_metrics,
        "metrics": metrics,
    }


def _apply_filters(game_logs: list[dict[str, Any]], parsed: ParsedStatsQuery) -> list[dict[str, Any]]:
    filtered = game_logs
    if parsed.split:
        filtered = [item for item in filtered if item["location"] == parsed.split]
    if parsed.opponent:
        normalized_opponent = _normalize_name(parsed.opponent)
        filtered = [
            item
            for item in filtered
            if normalized_opponent in _normalize_name(item["opponent"])
            or normalized_opponent == _normalize_name(item.get("opponent_abbreviation"))
        ]
    return filtered


def _build_summary_metrics(sport_key: str, game_logs: list[dict[str, Any]]) -> dict[str, float | None]:
    if sport_key == "NBA":
        return _nba_summary_metrics(game_logs)
    if sport_key == "NFL":
        return _nfl_summary_metrics(game_logs)
    if sport_key == "MLB":
        return _mlb_summary_metrics(game_logs)
    raise ValueError(f"Unsupported stats sport: {sport_key}")


def _nba_metrics_for_game(raw: dict[str, float]) -> dict[str, float | None]:
    return {
        "minutes": round(raw["minutes"], 1),
        "points": raw["points"],
        "rebounds": raw["rebounds"],
        "assists": raw["assists"],
        "made_threes": raw["three_points_made"],
        "steals": raw["steals"],
        "blocks": raw["blocks"],
        "turnovers": raw["turnovers"],
        "field_goal_pct": _percentage(raw["field_goals_made"], raw["field_goals_attempted"]),
        "three_point_pct": _percentage(raw["three_points_made"], raw["three_points_attempted"]),
        "free_throw_pct": _percentage(raw["free_throws_made"], raw["free_throws_attempted"]),
    }


def _nba_summary_metrics(game_logs: list[dict[str, Any]]) -> dict[str, float | None]:
    count = len(game_logs)
    raw_totals = _sum_raw_metrics(game_logs)
    return {
        "minutes": _round_average(raw_totals["minutes"], count),
        "points": _round_average(raw_totals["points"], count),
        "rebounds": _round_average(raw_totals["rebounds"], count),
        "assists": _round_average(raw_totals["assists"], count),
        "made_threes": _round_average(raw_totals["three_points_made"], count),
        "steals": _round_average(raw_totals["steals"], count),
        "blocks": _round_average(raw_totals["blocks"], count),
        "turnovers": _round_average(raw_totals["turnovers"], count),
        "field_goal_pct": _percentage(raw_totals["field_goals_made"], raw_totals["field_goals_attempted"]),
        "three_point_pct": _percentage(raw_totals["three_points_made"], raw_totals["three_points_attempted"]),
        "free_throw_pct": _percentage(raw_totals["free_throws_made"], raw_totals["free_throws_attempted"]),
    }


def _nfl_metrics_for_game(raw: dict[str, float]) -> dict[str, float | None]:
    return {
        "completions": raw["completions"],
        "passing_attempts": raw["passing_attempts"],
        "passing_yards": raw["passing_yards"],
        "completion_pct": _percentage(raw["completions"], raw["passing_attempts"]),
        "yards_per_pass_attempt": _rate(raw["passing_yards"], raw["passing_attempts"]),
        "passing_touchdowns": raw["passing_touchdowns"],
        "interceptions": raw["interceptions"],
        "sacks": raw["sacks"],
        "qbr": round(raw["qbr"], 1),
        "rushing_attempts": raw["rushing_attempts"],
        "rushing_yards": raw["rushing_yards"],
        "yards_per_rush_attempt": _rate(raw["rushing_yards"], raw["rushing_attempts"]),
        "rushing_touchdowns": raw["rushing_touchdowns"],
    }


def _nfl_summary_metrics(game_logs: list[dict[str, Any]]) -> dict[str, float | None]:
    count = len(game_logs)
    raw_totals = _sum_raw_metrics(game_logs)
    return {
        "completions": _round_average(raw_totals["completions"], count),
        "passing_attempts": _round_average(raw_totals["passing_attempts"], count),
        "passing_yards": _round_average(raw_totals["passing_yards"], count),
        "completion_pct": _percentage(raw_totals["completions"], raw_totals["passing_attempts"]),
        "yards_per_pass_attempt": _rate(raw_totals["passing_yards"], raw_totals["passing_attempts"]),
        "passing_touchdowns": _round_average(raw_totals["passing_touchdowns"], count),
        "interceptions": _round_average(raw_totals["interceptions"], count),
        "sacks": _round_average(raw_totals["sacks"], count),
        "qbr": _round_average(raw_totals["qbr"], count),
        "rushing_attempts": _round_average(raw_totals["rushing_attempts"], count),
        "rushing_yards": _round_average(raw_totals["rushing_yards"], count),
        "yards_per_rush_attempt": _rate(raw_totals["rushing_yards"], raw_totals["rushing_attempts"]),
        "rushing_touchdowns": _round_average(raw_totals["rushing_touchdowns"], count),
    }


def _mlb_metrics_for_game(raw: dict[str, float]) -> dict[str, float | None]:
    return {
        "at_bats": raw["at_bats"],
        "hits": raw["hits"],
        "runs": raw["runs"],
        "home_runs": raw["home_runs"],
        "rbis": raw["rbis"],
        "walks": raw["walks"],
        "strikeouts": raw["strikeouts"],
        "total_bases": _total_bases(raw),
        "batting_avg": _decimal_rate(raw["hits"], raw["at_bats"]),
        "on_base_pct": _decimal_rate(raw["hits"] + raw["walks"] + raw["hit_by_pitch"], raw["at_bats"] + raw["walks"] + raw["hit_by_pitch"]),
        "slugging_pct": _decimal_rate(_total_bases(raw), raw["at_bats"]),
        "ops": _decimal_sum(
            _decimal_rate(raw["hits"] + raw["walks"] + raw["hit_by_pitch"], raw["at_bats"] + raw["walks"] + raw["hit_by_pitch"]),
            _decimal_rate(_total_bases(raw), raw["at_bats"]),
        ),
    }


def _mlb_summary_metrics(game_logs: list[dict[str, Any]]) -> dict[str, float | None]:
    raw_totals = _sum_raw_metrics(game_logs)
    at_bats = raw_totals["at_bats"]
    on_base_denominator = at_bats + raw_totals["walks"] + raw_totals["hit_by_pitch"]
    batting_avg = _decimal_rate(raw_totals["hits"], at_bats)
    on_base_pct = _decimal_rate(raw_totals["hits"] + raw_totals["walks"] + raw_totals["hit_by_pitch"], on_base_denominator)
    slugging_pct = _decimal_rate(_total_bases(raw_totals), at_bats)
    return {
        "at_bats": raw_totals["at_bats"],
        "hits": raw_totals["hits"],
        "runs": raw_totals["runs"],
        "home_runs": raw_totals["home_runs"],
        "rbis": raw_totals["rbis"],
        "walks": raw_totals["walks"],
        "strikeouts": raw_totals["strikeouts"],
        "total_bases": _total_bases(raw_totals),
        "batting_avg": batting_avg,
        "on_base_pct": on_base_pct,
        "slugging_pct": slugging_pct,
        "ops": _decimal_sum(on_base_pct, slugging_pct),
    }


def _serialize_game_log(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "game_id": item["game_id"],
        "game_date": item["game_date"],
        "competition": item.get("competition"),
        "team_name": item.get("team_name"),
        "location": item["location"],
        "opponent": item["opponent"],
        "opponent_abbreviation": item["opponent_abbreviation"],
        "result": item["result"],
        "team_score": item["team_score"],
        "opponent_score": item["opponent_score"],
        "metrics": item["metrics"],
        "stat_line": item.get("stat_line") or _build_stat_line(item.get("sport_key"), item["metrics"]),
    }


def _build_explanation(
    player_name: str,
    parsed: ParsedStatsQuery,
    summary_metrics: dict[str, float | None],
    games_analyzed: int,
) -> str:
    if parsed.query_type == "last_n_games":
        scope = f"the last {games_analyzed} games"
    else:
        scope = f"the {parsed.season} season"

    if parsed.split:
        scope += " at home" if parsed.split == "home" else " on the road"
    if parsed.opponent:
        scope += f" against {parsed.opponent}"

    if parsed.sport_key == "NBA":
        return (
            f"{player_name} averaged {summary_metrics['points']:.1f} points, {summary_metrics['assists']:.1f} assists, "
            f"{summary_metrics['rebounds']:.1f} rebounds, and {summary_metrics['minutes']:.1f} minutes over {scope}."
        )
    if parsed.sport_key == "NFL":
        return (
            f"{player_name} averaged {summary_metrics['passing_yards']:.1f} passing yards, "
            f"{summary_metrics['passing_touchdowns']:.1f} passing touchdowns, {summary_metrics['rushing_yards']:.1f} rushing yards, "
            f"and a {summary_metrics['qbr']:.1f} QBR over {scope}."
        )
    return (
        f"{player_name} posted a .{int((summary_metrics['batting_avg'] or 0) * 1000):03d} batting average, "
        f"{int(summary_metrics['home_runs'] or 0)} home runs, {int(summary_metrics['rbis'] or 0)} RBI, "
        f"and a {summary_metrics['ops']:.3f} OPS over {scope}."
    )


def _build_stat_line(sport_key: str | None, metrics: dict[str, float | None]) -> str | None:
    if not sport_key or sport_key not in _STAT_LINE_SPECS:
        return None

    parts: list[str] = []
    for metric_key, singular_label, plural_label in _STAT_LINE_SPECS[sport_key]:
        value = metrics.get(metric_key)
        if value is None:
            continue
        label = singular_label if _uses_singular_stat_label(value) else plural_label
        parts.append(f"{_format_stat_line_value(metric_key, value)} {label}")

    return ", ".join(parts) or None


def _format_stat_line_value(metric_key: str, value: float | None) -> str:
    if value is None:
        return ""

    decimal_keys = {
        "batting_avg",
        "on_base_pct",
        "slugging_pct",
        "ops",
    }
    single_decimal_keys = {
        "minutes",
        "field_goal_pct",
        "three_point_pct",
        "free_throw_pct",
        "completion_pct",
        "yards_per_pass_attempt",
        "yards_per_rush_attempt",
        "qbr",
    }

    if metric_key in decimal_keys:
        return f"{value:.3f}"
    if metric_key in single_decimal_keys:
        return f"{value:.1f}"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _uses_singular_stat_label(value: float) -> bool:
    return abs(value - 1.0) < 1e-9


def _sum_raw_metrics(game_logs: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for item in game_logs:
        for key, value in item["raw_metrics"].items():
            totals[key] = totals.get(key, 0.0) + value
    return totals


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    return float(str(value).replace(",", ""))


def _parse_minutes(value: Any) -> float:
    raw = str(value or "0").strip()
    if ":" not in raw:
        return _parse_number(raw)
    minutes, seconds = raw.split(":", 1)
    return _parse_number(minutes) + (_parse_number(seconds) / 60.0)


def _parse_made_attempted(value: Any) -> tuple[float, float]:
    raw = str(value or "0-0").strip()
    if "-" not in raw:
        return 0.0, 0.0
    made, attempted = raw.split("-", 1)
    return _parse_number(made), _parse_number(attempted)


def _percentage(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100.0, 1)


def _decimal_rate(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _decimal_sum(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(left + right, 3)


def _rate(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 1)


def _round_average(total: float, count: int) -> float:
    if count <= 0:
        return 0.0
    return round(total / count, 1)


def _total_bases(raw: dict[str, float]) -> float:
    singles = raw["hits"] - raw["doubles"] - raw["triples"] - raw["home_runs"]
    return singles + (2.0 * raw["doubles"]) + (3.0 * raw["triples"]) + (4.0 * raw["home_runs"])


def _normalize_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())
