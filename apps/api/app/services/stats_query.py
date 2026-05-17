from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from typing import Any

from sqlalchemy.orm import Session

from app.clients.espn import EspnPublicClient


SUPPORTED_STATS_SPORTS = {"NBA", "NFL", "MLB", "WNBA", "SOCCER", "TENNIS", "UFC"}

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
_TENNIS_SET_RE = re.compile(r"(?P<left>\d+)-(?P<right>\d+)(?:\s*\((?P<tiebreak_left>\d+)-(?P<tiebreak_right>\d+)\))?")

_NBA_METRIC_LABELS = {
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
}

_METRIC_LABELS = {
    "NBA": _NBA_METRIC_LABELS,
    # WNBA shares NBA's stat set 1:1. Distinct dict reference (not a
    # mutation-shared alias) so future WNBA-only labels can land here.
    "WNBA": dict(_NBA_METRIC_LABELS),
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
    "SOCCER": {
        "appearances": "Apps",
        "starts": "Starts",
        "sub_appearances": "Sub Apps",
        "goals": "Goals",
        "assists": "Assists",
        "goal_contributions": "G+A",
        "goals_per_match": "Goals/Match",
        "assists_per_match": "Ast/Match",
        "shots": "Shots",
        "shots_on_target": "Shots On Target",
        "shots_per_match": "Shots/Match",
        "shots_on_target_per_match": "SOT/Match",
        "fouls_committed": "Fouls Comm.",
        "fouls_suffered": "Fouls Drawn",
        "offsides": "Offsides",
        "yellow_cards": "Yellow Cards",
        "red_cards": "Red Cards",
    },
    "TENNIS": {
        "sets_won": "Sets Won",
        "sets_lost": "Sets Lost",
        "games_won": "Games Won",
        "games_lost": "Games Lost",
        "straight_sets_wins": "Straight-Set Wins",
        "win_pct": "Win%",
        "titles": "Titles",
        "prize_money_usd": "Prize (USD)",
    },
    "UFC": {
        "ko_tko_wins": "KO/TKO Wins",
        "submission_wins": "Submission Wins",
        "decision_wins": "Decision Wins",
        "ko_tko_losses": "KO/TKO Losses",
        "submission_losses": "Submission Losses",
        "decision_losses": "Decision Losses",
        "finish_rate": "Finish%",
        "win_pct": "Win%",
        "avg_round": "Avg Round",
        "avg_fight_minutes": "Avg Fight Minutes",
        "title_fights": "Title Fights",
        "round": "Round",
        "fight_minutes": "Fight Minutes",
        "title_fight": "Title Fight",
        "finish": "Finish",
    },
}

_EXPLANATION_KEYS = {
    "NBA": ("points", "assists", "rebounds", "minutes"),
    "NFL": ("passing_yards", "passing_touchdowns", "rushing_yards", "qbr"),
    "MLB": ("batting_avg", "home_runs", "rbis", "ops"),
    "SOCCER": ("goals", "assists", "shots", "starts"),
}

# Display labels for advanced metric keys added by stats_summary_augment.
# Keep these in sync with the keys in stats_summary_augment._NBA_ADVANCED_KEYS
# and _MLB_BATTER_ADVANCED_KEYS.
_ADVANCED_METRIC_LABELS = {
    "NBA": {
        "ts_pct": "TS%",
        "efg_pct": "eFG%",
        "usg_pct": "USG%",
        "off_rating": "ORtg",
        "def_rating": "DRtg",
        "net_rating": "Net Rtg",
        "pie": "PIE",
        "pace": "Pace",
    },
    "MLB": {
        "woba": "wOBA",
        "iso": "ISO",
        "walk_rate": "BB%",
        "strikeout_rate": "K%",
        "wrc_plus": "wRC+",
        "babip": "BABIP",
        "xwoba": "xwOBA",
        "xba": "xBA",
        "xslg": "xSLG",
        "barrel_rate": "Barrel%",
        "hard_hit_rate": "Hard-Hit%",
        "exit_velocity_avg": "Exit Velo",
        "launch_angle_avg": "Launch Angle",
        "sweet_spot_rate": "Sweet Spot%",
    },
}


def _advanced_metric_labels(sport_key: str, metric_categories: dict[str, str]) -> dict[str, str]:
    """Return display labels for any "advanced"-tagged keys in
    ``metric_categories`` that have an entry in ``_ADVANCED_METRIC_LABELS``."""
    table = _ADVANCED_METRIC_LABELS.get(sport_key, {})
    return {key: table[key] for key, category in metric_categories.items() if category == "advanced" and key in table}


_NBA_STAT_LINE_SPEC = (
    ("points", "point", "points"),
    ("assists", "assist", "assists"),
    ("rebounds", "rebound", "rebounds"),
    ("minutes", "minute", "minutes"),
)

_STAT_LINE_SPECS = {
    "NBA": _NBA_STAT_LINE_SPEC,
    # WNBA mirrors NBA — same per-game metric shape, same stat-line phrasing.
    "WNBA": _NBA_STAT_LINE_SPEC,
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
    "SOCCER": (
        ("goals", "goal", "goals"),
        ("assists", "assist", "assists"),
        ("shots", "shot", "shots"),
        ("shots_on_target", "shot on target", "shots on target"),
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

    def query_team_history(
        self,
        team_name: str,
        sport_key: str = "NBA",
        n: int = 5,
        *,
        opponent: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Return the last ``n`` completed games for a team as a flat list of
        results (date, opponent, location, scores, W/L).

        Used by the trade-ticket pick-history strip for game-line picks. The
        endpoint deliberately avoids the regex-parsed natural-language path
        the player-prop queries go through — callers already have a clean
        ``team_name`` string from the selection model and the parser would
        only add fragility.

        Optional filters narrow the result set before clipping:
          - ``opponent``: case-insensitive substring match on the
            opponent's display name (e.g. ``"Pistons"`` matches both
            ``"Detroit Pistons"`` and a hypothetical short form). Picks
            from any sport this way.
          - ``location``: ``"home"`` or ``"away"`` to keep only games
            played at that location.
        """
        normalized_sport = sport_key.upper()
        team = self.espn_client.search_team(team_name, sport_key=normalized_sport)
        schedule = self.espn_client.fetch_team_schedule(normalized_sport, team["team_id"])
        results = _build_team_results(schedule, self_team_id=team["team_id"])
        results = _filter_team_results(results, opponent=opponent, location=location)
        return {
            "entity_id": team["team_id"],
            "team_name": team["display_name"],
            "sport_key": normalized_sport,
            "results": results[:n],
        }

    def query(
        self,
        question: str,
        sport_key: str = "NBA",
        season: int | None = None,
        *,
        db: Session | None = None,
        team_hint: str | None = None,
    ) -> dict[str, Any]:
        parsed = parse_stats_question(question, sport_key=sport_key, season=season)
        # Codex round-2 P2 on PR #24: same-name player disambiguation.
        # ``team_hint`` (forwarded from ``selection.subjectTeam`` in
        # the pick-history strip) is what bug #13's
        # ``search_player`` upgrade was built for. Without this
        # plumbing, prop picks for duplicate-name players (the
        # canonical "two John Smiths" case) silently chart the
        # wrong athlete's game logs.
        player = self.espn_client.search_player(
            parsed.player_name,
            sport_key=parsed.sport_key,
            team_hint=team_hint,
        )
        if parsed.sport_key == "SOCCER":
            return self._query_soccer(question, parsed, player)
        if parsed.sport_key == "TENNIS":
            return self._query_tennis(question, parsed, player)
        if parsed.sport_key == "UFC":
            return self._query_ufc(question, parsed, player)

        gamelog_payload = self.espn_client.fetch_player_gamelog(parsed.sport_key, player["athlete_id"], parsed.season)

        game_logs = _build_game_logs(parsed.sport_key, gamelog_payload)
        game_logs = _apply_filters(game_logs, parsed)
        if parsed.query_type == "last_n_games" and parsed.games_requested is not None:
            game_logs = game_logs[: parsed.games_requested]

        if not game_logs:
            raise LookupError(f"No {parsed.sport_key} game logs matched the query for {player['display_name']}")

        summary_metrics = _build_summary_metrics(parsed.sport_key, game_logs)
        # PR 3c: layer in advanced metrics + percentile ranks + categories.
        # Cache misses are graceful — basic metrics always survive.
        from app.services.stats_summary_augment import augment_summary_with_advanced

        summary_metrics, percentiles, metric_categories = augment_summary_with_advanced(
            db,
            sport_key=parsed.sport_key,
            player=player,
            season=parsed.season,
            summary_metrics=summary_metrics,
        )
        # Extend the metric_labels map with display labels for any newly
        # added advanced keys so the frontend can render their names.
        metric_labels = _METRIC_LABELS[parsed.sport_key]
        if metric_categories:
            metric_labels = {**metric_labels, **_advanced_metric_labels(parsed.sport_key, metric_categories)}
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
            "metric_labels": metric_labels,
            "summary": {
                "games": len(game_logs),
                "wins": sum(1 for item in game_logs if item.get("result") == "W"),
                "losses": sum(1 for item in game_logs if item.get("result") == "L"),
                "draws": sum(1 for item in game_logs if item.get("result") == "D"),
                "metrics": summary_metrics,
                "stat_line": _build_stat_line(parsed.sport_key, summary_metrics),
                "percentiles": percentiles,
                "metric_categories": metric_categories,
            },
            "game_logs": [_serialize_game_log(item) for item in game_logs],
            "explanation": _build_explanation(player["display_name"], parsed, summary_metrics, len(game_logs)),
            "source": "espn_public",
        }

    def _query_soccer(self, question: str, parsed: ParsedStatsQuery, player: dict[str, Any]) -> dict[str, Any]:
        if parsed.query_type == "season" and (parsed.split or parsed.opponent):
            raise ValueError("Soccer season queries do not support home/away or opponent filters yet")
        if parsed.query_type == "last_n_games" and (parsed.games_requested or 0) > 5:
            raise ValueError("Soccer beta currently supports up to the last 5 matches per player")

        overview_payload = self.espn_client.fetch_soccer_player_overview(
            player["athlete_id"],
            page_slug=player.get("page_slug"),
        )
        all_game_logs = _build_soccer_game_logs(overview_payload)
        filtered_game_logs = _apply_filters(all_game_logs, parsed)

        if parsed.query_type == "last_n_games":
            filtered_game_logs = filtered_game_logs[: parsed.games_requested]
            if not filtered_game_logs:
                raise LookupError(f"No SOCCER match logs matched the query for {player['display_name']}")

            summary_metrics = _soccer_summary_metrics(filtered_game_logs)
            coverage_note = "Soccer beta uses ESPN's public player overview and currently exposes up to the last 5 matches."
            return {
                "question": question,
                "sport_key": parsed.sport_key,
                "entity_name": player["display_name"],
                "entity_id": player["athlete_id"],
                "team_name": player.get("team_name"),
                "query_type": parsed.query_type,
                "season": parsed.season,
                "games_requested": parsed.games_requested,
                "games_analyzed": len(filtered_game_logs),
                "split": parsed.split,
                "opponent": parsed.opponent,
                "metric_labels": _METRIC_LABELS[parsed.sport_key],
                "summary": {
                    "games": len(filtered_game_logs),
                    "wins": sum(1 for item in filtered_game_logs if item.get("result") == "W"),
                    "losses": sum(1 for item in filtered_game_logs if item.get("result") == "L"),
                    "draws": sum(1 for item in filtered_game_logs if item.get("result") == "D"),
                    "metrics": summary_metrics,
                    "stat_line": _build_stat_line(parsed.sport_key, summary_metrics),
                },
                "game_logs": [_serialize_game_log(item) for item in filtered_game_logs],
                "explanation": _build_soccer_explanation(player["display_name"], parsed, summary_metrics, len(filtered_game_logs)),
                "coverage_note": coverage_note,
                "source": "espn_public_player_page",
            }

        season_logs = _filter_soccer_logs_for_current_context(all_game_logs, overview_payload)
        season_summary = _build_soccer_season_summary(overview_payload, season_logs)
        recent_logs = season_logs[:5]
        exact_results = season_summary["games"] <= len(recent_logs)
        return {
            "question": question,
            "sport_key": parsed.sport_key,
            "entity_name": player["display_name"],
            "entity_id": player["athlete_id"],
            "team_name": player.get("team_name"),
            "query_type": parsed.query_type,
            "season": parsed.season,
            "games_requested": parsed.games_requested,
            "games_analyzed": season_summary["games"],
            "split": parsed.split,
            "opponent": parsed.opponent,
            "metric_labels": _METRIC_LABELS[parsed.sport_key],
            "summary": {
                "games": season_summary["games"],
                "wins": sum(1 for item in recent_logs if item.get("result") == "W") if exact_results else None,
                "losses": sum(1 for item in recent_logs if item.get("result") == "L") if exact_results else None,
                "draws": sum(1 for item in recent_logs if item.get("result") == "D") if exact_results else None,
                "metrics": season_summary["metrics"],
                "stat_line": _build_stat_line(parsed.sport_key, season_summary["metrics"]),
            },
            "game_logs": [_serialize_game_log(item) for item in recent_logs],
            "explanation": _build_soccer_season_explanation(
                player["display_name"],
                season_summary["metrics"],
                season_summary["coverage_label"],
            ),
            "coverage_note": season_summary["coverage_note"],
            "source": "espn_public_player_page",
        }

    def _query_tennis(self, question: str, parsed: ParsedStatsQuery, player: dict[str, Any]) -> dict[str, Any]:
        if parsed.split:
            raise ValueError("Tennis queries do not support home/away splits")

        athlete_profile = self.espn_client.fetch_tennis_athlete_profile(player["athlete_id"])
        statistics_ref = ((athlete_profile.get("statistics") or {}).get("$ref"))
        event_log_ref = ((athlete_profile.get("eventLog") or {}).get("$ref"))
        if not statistics_ref or not event_log_ref:
            raise LookupError(f"Could not locate tennis refs for {player['display_name']}")

        statistics_payload = self.espn_client.fetch_json_ref(statistics_ref)
        event_log_payload = self.espn_client.fetch_json_ref(event_log_ref)
        all_game_logs = _build_tennis_game_logs(player["athlete_id"], event_log_payload, self.espn_client)
        filtered_game_logs = _apply_filters(all_game_logs, parsed)

        if parsed.query_type == "last_n_games":
            filtered_game_logs = filtered_game_logs[: parsed.games_requested]
            if not filtered_game_logs:
                raise LookupError(f"No TENNIS match logs matched the query for {player['display_name']}")

            wins = sum(1 for item in filtered_game_logs if item.get("result") == "W")
            losses = sum(1 for item in filtered_game_logs if item.get("result") == "L")
            summary_metrics = _tennis_summary_metrics(filtered_game_logs)
            return {
                "question": question,
                "sport_key": parsed.sport_key,
                "entity_name": player["display_name"],
                "entity_id": player["athlete_id"],
                "team_name": player.get("team_name"),
                "query_type": parsed.query_type,
                "season": parsed.season,
                "games_requested": parsed.games_requested,
                "games_analyzed": len(filtered_game_logs),
                "split": parsed.split,
                "opponent": parsed.opponent,
                "metric_labels": _METRIC_LABELS[parsed.sport_key],
                "summary": {
                    "games": len(filtered_game_logs),
                    "wins": wins,
                    "losses": losses,
                    "draws": 0,
                    "metrics": summary_metrics,
                    "stat_line": _build_tennis_summary_stat_line(wins, losses, summary_metrics),
                },
                "game_logs": [_serialize_game_log(item) for item in filtered_game_logs],
                "explanation": _build_tennis_explanation(player["display_name"], parsed, wins, losses, summary_metrics),
                "source": "espn_public_tennis_core",
            }

        if parsed.opponent:
            if not filtered_game_logs:
                raise LookupError(f"No TENNIS match logs matched the query for {player['display_name']}")
            wins = sum(1 for item in filtered_game_logs if item.get("result") == "W")
            losses = sum(1 for item in filtered_game_logs if item.get("result") == "L")
            summary_metrics = _tennis_summary_metrics(filtered_game_logs)
            coverage_note = "Tennis beta uses ESPN's public core tennis event log for opponent-filtered season queries."
            games_analyzed = len(filtered_game_logs)
            summary_games = len(filtered_game_logs)
        else:
            season_summary = _build_tennis_season_summary(statistics_payload, all_game_logs)
            wins = season_summary["wins"]
            losses = season_summary["losses"]
            summary_metrics = season_summary["metrics"]
            coverage_note = season_summary["coverage_note"]
            games_analyzed = season_summary["games"]
            summary_games = season_summary["games"]
            filtered_game_logs = all_game_logs

        return {
            "question": question,
            "sport_key": parsed.sport_key,
            "entity_name": player["display_name"],
            "entity_id": player["athlete_id"],
            "team_name": player.get("team_name"),
            "query_type": parsed.query_type,
            "season": parsed.season,
            "games_requested": parsed.games_requested,
            "games_analyzed": games_analyzed,
            "split": parsed.split,
            "opponent": parsed.opponent,
            "metric_labels": _METRIC_LABELS[parsed.sport_key],
            "summary": {
                "games": summary_games,
                "wins": wins,
                "losses": losses,
                "draws": 0,
                "metrics": summary_metrics,
                "stat_line": _build_tennis_summary_stat_line(wins, losses, summary_metrics),
            },
            "game_logs": [_serialize_game_log(item) for item in filtered_game_logs],
            "explanation": _build_tennis_explanation(player["display_name"], parsed, wins, losses, summary_metrics),
            "coverage_note": coverage_note,
            "source": "espn_public_tennis_core",
        }

    def _query_ufc(self, question: str, parsed: ParsedStatsQuery, player: dict[str, Any]) -> dict[str, Any]:
        if parsed.split:
            raise ValueError("UFC queries do not support home/away splits")

        history_payload = self.espn_client.fetch_mma_fighter_history(
            player["athlete_id"],
            page_slug=player.get("page_slug"),
        )
        all_fight_logs = _build_mma_fight_logs(history_payload)
        scoped_logs = [item for item in all_fight_logs if item["game_date"].year == parsed.season] if parsed.query_type == "season" else all_fight_logs
        scoped_logs = _apply_filters(scoped_logs, parsed)
        if parsed.query_type == "last_n_games" and parsed.games_requested is not None:
            scoped_logs = scoped_logs[: parsed.games_requested]

        if not scoped_logs:
            raise LookupError(f"No UFC fight logs matched the query for {player['display_name']}")

        wins = sum(1 for item in scoped_logs if item.get("result") == "W")
        losses = sum(1 for item in scoped_logs if item.get("result") == "L")
        draws = sum(1 for item in scoped_logs if item.get("result") == "D")
        summary_metrics = _mma_summary_metrics(scoped_logs)
        coverage_note = "UFC beta uses ESPN's public fighter history page. 'This season' is interpreted as the calendar year."
        return {
            "question": question,
            "sport_key": parsed.sport_key,
            "entity_name": player["display_name"],
            "entity_id": player["athlete_id"],
            "team_name": player.get("team_name"),
            "query_type": parsed.query_type,
            "season": parsed.season,
            "games_requested": parsed.games_requested,
            "games_analyzed": len(scoped_logs),
            "split": parsed.split,
            "opponent": parsed.opponent,
            "metric_labels": _METRIC_LABELS[parsed.sport_key],
            "summary": {
                "games": len(scoped_logs),
                "wins": wins,
                "losses": losses,
                "draws": draws,
                "metrics": summary_metrics,
                "stat_line": _build_mma_summary_stat_line(wins, losses, summary_metrics),
            },
            "game_logs": [_serialize_game_log(item) for item in scoped_logs],
            "explanation": _build_mma_explanation(player["display_name"], parsed, wins, losses, summary_metrics),
            "coverage_note": coverage_note,
            "source": "espn_public_mma_history_page",
        }


def parse_stats_question(question: str, sport_key: str = "NBA", season: int | None = None) -> ParsedStatsQuery:
    normalized_sport = sport_key.upper()
    if normalized_sport not in SUPPORTED_STATS_SPORTS:
        raise ValueError("Stats query currently supports NBA, NFL, MLB, Soccer, Tennis, and UFC only")

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
        "'Lionel Messi last 5 matches', 'Novak Djokovic last 5 matches', or 'Alex Pereira last 5 fights'."
    )


def default_season_for_sport(sport_key: str, reference_date: date | None = None) -> int:
    today = reference_date or date.today()
    if sport_key == "NBA":
        return today.year + 1 if today.month >= 10 else today.year
    if sport_key == "NFL":
        return today.year if today.month >= 8 else today.year - 1
    if sport_key == "MLB":
        return today.year if today.month >= 3 else today.year - 1
    # WNBA's regular season runs May → September of one calendar year
    # (no multi-year span like NBA's). Offseason references (Oct → Apr)
    # roll back to the previous season's calendar year, matching how
    # MLB handles its winter offseason.
    if sport_key == "WNBA":
        return today.year if today.month >= 5 else today.year - 1
    if sport_key == "SOCCER":
        return today.year
    if sport_key == "TENNIS":
        return today.year
    if sport_key == "UFC":
        return today.year
    return today.year


def _filter_team_results(
    results: list[dict[str, Any]],
    *,
    opponent: str | None = None,
    location: str | None = None,
) -> list[dict[str, Any]]:
    """Narrow a list of team results by opponent name (substring) and/or
    location. Returns ``results`` unchanged when both filters are None."""
    if not opponent and not location:
        return results
    opponent_needle = (opponent or "").strip().lower()
    location_value = (location or "").strip().lower() or None
    out: list[dict[str, Any]] = []
    for row in results:
        if location_value is not None and str(row.get("location") or "").lower() != location_value:
            continue
        if opponent_needle:
            opponent_haystack = " ".join(
                str(part or "").lower()
                for part in (row.get("opponent"), row.get("opponent_abbreviation"))
                if part
            )
            if opponent_needle not in opponent_haystack:
                continue
        out.append(row)
    return out


def _build_team_results(schedule_payload: dict[str, Any], *, self_team_id: str) -> list[dict[str, Any]]:
    """Extract completed games from an ESPN team schedule, most recent first.

    ESPN's ``/teams/{team_id}/schedule`` endpoint returns an ``events`` list
    that mixes completed games (with scores + winner flags) and upcoming
    games (no scores). We keep only the completed ones, sort by date
    descending, and normalize each to the shape ``TeamGameResultRead``
    expects on the schemas side.
    """
    out: list[dict[str, Any]] = []
    for event in schedule_payload.get("events") or []:
        competition = (event.get("competitions") or [{}])[0]
        status_type = (competition.get("status") or event.get("status") or {}).get("type") or {}
        # Codex round-3 P2 on PR #24: cancelled / postponed games also
        # have ``state == "post"`` but ship without scores. The previous
        # ``state == "post"`` fallback let them through, and the missing
        # scores fell back to ``0`` further down, so cancellations
        # surfaced in the strip as 0-0 losses. Require ESPN's explicit
        # ``completed`` flag (or the ``STATUS_FINAL`` terminal name as
        # an allow-listed fallback for payloads that omit it).
        status_name = str(status_type.get("name") or "").upper()
        is_completed = bool(status_type.get("completed")) or status_name == "STATUS_FINAL"
        if not is_completed:
            continue

        competitors = competition.get("competitors") or []
        self_side = next(
            (c for c in competitors if str(((c.get("team") or {}).get("id")) or "") == str(self_team_id)),
            None,
        )
        other_side = next(
            (c for c in competitors if str(((c.get("team") or {}).get("id")) or "") != str(self_team_id)),
            None,
        )
        if self_side is None or other_side is None:
            continue

        # PR #24 codex round-1 P2: ESPN's site/v2 shape returns
        # ``"score": "112"`` (scalar) on completed-game payloads,
        # not the dict form ``{"value": 112}``. ``.get("value")`` on
        # the scalar raises ``AttributeError`` and 500'd this whole
        # endpoint. ``_competitor_score`` accepts either shape.
        def _competitor_score(side: dict) -> int | None:
            raw = side.get("score")
            if isinstance(raw, dict):
                raw = raw.get("value")
            if raw in (None, ""):
                return 0
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return None

        self_score = _competitor_score(self_side)
        opp_score = _competitor_score(other_side)
        if self_score is None or opp_score is None:
            continue

        opponent_team = other_side.get("team") or {}
        winner_flag = self_side.get("winner")
        if winner_flag is True:
            result = "W"
        elif winner_flag is False:
            result = "L"
        else:
            result = "W" if self_score > opp_score else "L"

        out.append({
            "game_date": event.get("date"),
            "opponent": opponent_team.get("displayName") or opponent_team.get("shortDisplayName") or "",
            "opponent_abbreviation": opponent_team.get("abbreviation"),
            "location": "home" if str(self_side.get("homeAway") or "").lower() == "home" else "away",
            "team_score": self_score,
            "opp_score": opp_score,
            "result": result,
        })

    out.sort(key=lambda item: str(item.get("game_date") or ""), reverse=True)
    return out


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
    # WNBA shares NBA's ESPN gamelog payload shape exactly — same stat
    # names, same seasonTypes / categories / events nesting, same
    # made/attempted "11-19" string format. Reuse _build_nba_game_logs
    # but pass sport_key="WNBA" so each game entry's per-row
    # ``sport_key`` field is tagged correctly (cosmetic for the public
    # response today; the resolver consumes these dicts in PR 4 and
    # needs the right tag).
    if sport_key == "WNBA":
        return _build_nba_game_logs(payload, sport_key="WNBA")
    if sport_key == "SOCCER":
        return _build_soccer_game_logs(payload)
    raise ValueError(f"Unsupported stats sport: {sport_key}")


def _build_nba_game_logs(payload: dict[str, Any], *, sport_key: str = "NBA") -> list[dict[str, Any]]:
    """Parse ESPN's NBA-shaped gamelog payload.

    ``sport_key`` is threaded into the per-game entry's ``sport_key``
    field so WNBA (which reuses this parser — same payload shape)
    produces correctly-tagged game logs. Defaults to NBA for backward
    compat with the bare ``_build_nba_game_logs(payload)`` callers.
    """
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
                game_logs[event_id] = _build_game_entry(sport_key, metadata, event_id, raw_metrics, _nba_metrics_for_game(raw_metrics))

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


def _build_soccer_game_logs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    player_payload = _soccer_player_payload(payload)
    gmlg_sections = ((player_payload.get("gmlg") or {}).get("stats") or [])
    game_logs: list[dict[str, Any]] = []

    for section in gmlg_sections:
        headings = [item.get("data") or "" for item in section.get("headings") or []]
        for row in section.get("rows") or []:
            stat_values = row.get("stats") or []
            stat_map = {name: stat_values[index] if index < len(stat_values) else None for index, name in enumerate(headings)}
            team_score, opponent_score = _parse_score_pair(((row.get("res") or {}).get("score")))
            appearance_value = str(stat_map.get("APP") or "").strip()
            raw_metrics = {
                "appearances": 1.0 if appearance_value in {"Started", "Sub"} else 0.0,
                "starts": 1.0 if appearance_value == "Started" else 0.0,
                "sub_appearances": 1.0 if appearance_value == "Sub" else 0.0,
                "goals": _parse_number(stat_map.get("G")),
                "assists": _parse_number(stat_map.get("A")),
                "shots": _parse_number(stat_map.get("SH")),
                "shots_on_target": _parse_number(stat_map.get("ST")),
                "fouls_committed": _parse_number(stat_map.get("FC")),
                "fouls_suffered": _parse_number(stat_map.get("FA")),
                "offsides": _parse_number(stat_map.get("OF")),
                "yellow_cards": _parse_number(stat_map.get("YC")),
                "red_cards": _parse_number(stat_map.get("RC")),
                "team_goals_for": team_score,
                "team_goals_against": opponent_score,
            }
            opponent = row.get("opp") or {}
            game_logs.append(
                {
                    "sport_key": "SOCCER",
                    "game_id": str(row.get("id") or ""),
                    "game_date": _parse_datetime(row.get("dt")),
                    "competition": row.get("comp"),
                    "team_name": ((row.get("tm") or {}).get("name")),
                    "team_uid": ((row.get("tm") or {}).get("uid")),
                    "location": _soccer_location(opponent.get("atVs")),
                    "opponent": opponent.get("name") or opponent.get("abbr") or "Unknown",
                    "opponent_abbreviation": opponent.get("abbr"),
                    "result": ((row.get("res") or {}).get("abbr")),
                    "team_score": team_score,
                    "opponent_score": opponent_score,
                    "raw_metrics": raw_metrics,
                    "metrics": _soccer_metrics_for_game(raw_metrics),
                }
            )

    return sorted(game_logs, key=lambda item: item["game_date"], reverse=True)


def _soccer_player_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return (((payload.get("page") or {}).get("content") or {}).get("player") or {})


def _build_soccer_season_summary(payload: dict[str, Any], game_logs: list[dict[str, Any]]) -> dict[str, Any]:
    player_payload = _soccer_player_payload(payload)
    stats_block = ((player_payload.get("plyrHdr") or {}).get("statsBlck") or {})
    starts, sub_appearances = _parse_soccer_starts_block(stats_block)
    appearances = starts + sub_appearances
    stats_payload = player_payload.get("stats") or {}
    stats_key = _default_soccer_stats_key(stats_payload)
    stats_labels = ((stats_payload.get("splts") or {}).get("lbls") or [])
    stats_rows = ((stats_payload.get("splts") or {}).get("stats") or {})
    stats_row = _coerce_soccer_stats_row(stats_rows.get(stats_key) or next(iter(stats_rows.values()), []))
    stats_values = _map_soccer_season_values(stats_labels, stats_row)

    metrics = {
        "appearances": float(appearances),
        "starts": float(starts),
        "sub_appearances": float(sub_appearances),
        "goals": stats_values.get("G"),
        "assists": stats_values.get("A"),
        "goal_contributions": _sum_pair(stats_values.get("G"), stats_values.get("A")),
        "goals_per_match": _rate(stats_values.get("G") or 0.0, appearances),
        "assists_per_match": _rate(stats_values.get("A") or 0.0, appearances),
        "shots": stats_values.get("SH"),
        "shots_on_target": stats_values.get("ST"),
        "shots_per_match": _rate(stats_values.get("SH") or 0.0, appearances),
        "shots_on_target_per_match": _rate(stats_values.get("ST") or 0.0, appearances),
        "fouls_committed": stats_values.get("FC"),
        "fouls_suffered": stats_values.get("FA"),
        "offsides": stats_values.get("OF"),
        "yellow_cards": stats_values.get("YC"),
        "red_cards": stats_values.get("RC"),
    }
    coverage_label = stats_block.get("hdr") or (stats_row[0] if stats_row else "current competition")
    if appearances and len(game_logs) < appearances:
        coverage_note = (
            "Soccer beta uses ESPN's public player overview. Season totals reflect the current competition shown on the page, "
            "and detailed match logs are limited to the latest 5 available matches."
        )
    else:
        coverage_note = "Soccer beta uses ESPN's public player overview for the current competition shown on the page."

    return {
        "games": appearances,
        "metrics": metrics,
        "coverage_label": coverage_label,
        "coverage_note": coverage_note,
    }


def _build_tennis_game_logs(
    athlete_id: str,
    event_log_payload: dict[str, Any],
    espn_client: EspnPublicClient,
) -> list[dict[str, Any]]:
    event_items = (((event_log_payload.get("events") or {}).get("items")) or [])
    event_cache: dict[str, dict[str, Any]] = {}
    game_logs: list[dict[str, Any]] = []

    for item in event_items:
        if not item.get("played"):
            continue

        competition_ref = ((item.get("competition") or {}).get("$ref"))
        if not competition_ref:
            continue

        competition = espn_client.fetch_json_ref(competition_ref)
        if ((competition.get("type") or {}).get("type")) != "singles":
            continue

        competitors = competition.get("competitors") or []
        player_competitor = next((entry for entry in competitors if str(entry.get("id")) == str(athlete_id)), None)
        opponent_competitor = next((entry for entry in competitors if str(entry.get("id")) != str(athlete_id)), None)
        if not player_competitor or not opponent_competitor:
            continue

        opponent_name = opponent_competitor.get("name") or "Unknown"
        if _normalize_name(opponent_name) == "bye":
            continue

        event_ref = ((item.get("event") or {}).get("$ref"))
        event_payload: dict[str, Any] = {}
        if event_ref:
            if event_ref not in event_cache:
                event_cache[event_ref] = espn_client.fetch_json_ref(event_ref)
            event_payload = event_cache[event_ref]

        player_won = bool(player_competitor.get("winner"))
        scoreline_data = _tennis_scoreline_data(competition, player_competitor, opponent_competitor, player_won, espn_client)
        tournament_name = event_payload.get("shortName") or event_payload.get("name") or "Tennis Match"
        round_name = ((competition.get("round") or {}).get("description")) or _tennis_round_name_from_note(competition)

        raw_metrics = {
            "sets_won": scoreline_data["sets_won"],
            "sets_lost": scoreline_data["sets_lost"],
            "games_won": scoreline_data["games_won"],
            "games_lost": scoreline_data["games_lost"],
            "straight_sets_wins": 1.0 if player_won and scoreline_data["sets_lost"] == 0 and scoreline_data["sets_won"] > 0 else 0.0,
        }
        game_logs.append(
            {
                "sport_key": "TENNIS",
                "game_id": str(competition.get("id") or ""),
                "game_date": _parse_datetime(competition.get("date")),
                "competition": tournament_name,
                "team_name": None,
                "location": "neutral",
                "opponent": opponent_name,
                "opponent_abbreviation": None,
                "result": "W" if player_won else "L",
                "team_score": raw_metrics["sets_won"],
                "opponent_score": raw_metrics["sets_lost"],
                "raw_metrics": raw_metrics,
                "metrics": _tennis_metrics_for_game(raw_metrics),
                "stat_line": _build_tennis_match_stat_line(
                    "W" if player_won else "L",
                    opponent_name,
                    scoreline_data.get("display_scoreline"),
                    round_name,
                    tournament_name,
                ),
            }
        )

    return sorted(game_logs, key=lambda item: item["game_date"], reverse=True)


def _build_tennis_season_summary(statistics_payload: dict[str, Any], game_logs: list[dict[str, Any]]) -> dict[str, Any]:
    stats = _tennis_stats_map(statistics_payload)
    wins = int(stats.get("singlesWon") or sum(1 for item in game_logs if item.get("result") == "W"))
    losses = int(stats.get("singlesLost") or sum(1 for item in game_logs if item.get("result") == "L"))
    games = wins + losses
    metrics = _tennis_summary_metrics(game_logs)
    metrics["titles"] = float(stats.get("singlesTitles") or 0.0)
    metrics["prize_money_usd"] = float(stats.get("prize") or 0.0)

    if games > len(game_logs):
        coverage_note = (
            "Tennis beta uses ESPN's public core tennis refs. Season totals reflect the current singles record, "
            f"and detailed logs currently include {len(game_logs)} singles matches returned by ESPN."
        )
    else:
        coverage_note = "Tennis beta uses ESPN's public core tennis refs for singles season totals and match logs."

    return {
        "games": games,
        "wins": wins,
        "losses": losses,
        "metrics": metrics,
        "coverage_note": coverage_note,
    }


def _build_mma_fight_logs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    player_payload = _mma_player_payload(payload)
    fight_history = (player_payload.get("fghtHstr") or player_payload.get("ovrFghtHstr") or [])
    fight_logs: list[dict[str, Any]] = []

    for row in fight_history:
        result = row.get("rslt")
        method = row.get("dcsn") or "Unknown"
        method_bucket = _mma_method_bucket(method)
        raw_metrics = {
            "round": _parse_number(row.get("rnd")),
            "fight_minutes": _parse_minutes(row.get("htime")),
            "title_fight": 1.0 if row.get("ttlFght") else 0.0,
            "finish": 1.0 if method_bucket in {"ko_tko", "submission"} else 0.0,
        }
        fight_logs.append(
            {
                "sport_key": "UFC",
                "game_id": str(row.get("evntLnk") or row.get("evnt") or row.get("hdate") or ""),
                "game_date": _parse_datetime(row.get("hdate")),
                "competition": row.get("evnt"),
                "team_name": None,
                "location": "neutral",
                "opponent": row.get("opp") or "Unknown",
                "opponent_abbreviation": None,
                "result": result,
                "team_score": 1.0 if result == "W" else 0.0,
                "opponent_score": 1.0 if result == "L" else 0.0,
                "raw_metrics": raw_metrics,
                "metrics": _mma_metrics_for_fight(raw_metrics),
                "method_bucket": method_bucket,
                "stat_line": _build_mma_fight_stat_line(
                    result or "",
                    row.get("opp") or "Unknown",
                    method,
                    int(raw_metrics["round"]),
                    str(row.get("htime") or ""),
                    row.get("evnt"),
                ),
            }
        )

    return sorted(fight_logs, key=lambda item: item["game_date"], reverse=True)


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
    # WNBA mirrors NBA — same per-game raw_metrics shape, same summary aggregates.
    if sport_key == "WNBA":
        return _nba_summary_metrics(game_logs)
    if sport_key == "SOCCER":
        return _soccer_summary_metrics(game_logs)
    if sport_key == "TENNIS":
        return _tennis_summary_metrics(game_logs)
    if sport_key == "UFC":
        return _mma_summary_metrics(game_logs)
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


def _soccer_metrics_for_game(raw: dict[str, float]) -> dict[str, float | None]:
    appearances = raw["appearances"] or 1.0
    return {
        "appearances": raw["appearances"],
        "starts": raw["starts"],
        "sub_appearances": raw["sub_appearances"],
        "goals": raw["goals"],
        "assists": raw["assists"],
        "goal_contributions": raw["goals"] + raw["assists"],
        "goals_per_match": _rate(raw["goals"], appearances),
        "assists_per_match": _rate(raw["assists"], appearances),
        "shots": raw["shots"],
        "shots_on_target": raw["shots_on_target"],
        "shots_per_match": _rate(raw["shots"], appearances),
        "shots_on_target_per_match": _rate(raw["shots_on_target"], appearances),
        "fouls_committed": raw["fouls_committed"],
        "fouls_suffered": raw["fouls_suffered"],
        "offsides": raw["offsides"],
        "yellow_cards": raw["yellow_cards"],
        "red_cards": raw["red_cards"],
    }


def _soccer_summary_metrics(game_logs: list[dict[str, Any]]) -> dict[str, float | None]:
    raw_totals = _sum_raw_metrics(game_logs)
    appearances = int(raw_totals["appearances"])
    return {
        "appearances": float(appearances),
        "starts": raw_totals["starts"],
        "sub_appearances": raw_totals["sub_appearances"],
        "goals": raw_totals["goals"],
        "assists": raw_totals["assists"],
        "goal_contributions": raw_totals["goals"] + raw_totals["assists"],
        "goals_per_match": _rate(raw_totals["goals"], appearances),
        "assists_per_match": _rate(raw_totals["assists"], appearances),
        "shots": raw_totals["shots"],
        "shots_on_target": raw_totals["shots_on_target"],
        "shots_per_match": _rate(raw_totals["shots"], appearances),
        "shots_on_target_per_match": _rate(raw_totals["shots_on_target"], appearances),
        "fouls_committed": raw_totals["fouls_committed"],
        "fouls_suffered": raw_totals["fouls_suffered"],
        "offsides": raw_totals["offsides"],
        "yellow_cards": raw_totals["yellow_cards"],
        "red_cards": raw_totals["red_cards"],
    }


def _tennis_metrics_for_game(raw: dict[str, float]) -> dict[str, float | None]:
    total_sets = raw["sets_won"] + raw["sets_lost"]
    return {
        "sets_won": raw["sets_won"],
        "sets_lost": raw["sets_lost"],
        "games_won": raw["games_won"],
        "games_lost": raw["games_lost"],
        "straight_sets_wins": raw["straight_sets_wins"],
        "win_pct": 100.0 if total_sets > 0 and raw["sets_won"] > raw["sets_lost"] else 0.0,
    }


def _tennis_summary_metrics(game_logs: list[dict[str, Any]]) -> dict[str, float | None]:
    raw_totals = _sum_raw_metrics(game_logs)
    wins = sum(1 for item in game_logs if item.get("result") == "W")
    return {
        "sets_won": raw_totals["sets_won"],
        "sets_lost": raw_totals["sets_lost"],
        "games_won": raw_totals["games_won"],
        "games_lost": raw_totals["games_lost"],
        "straight_sets_wins": raw_totals["straight_sets_wins"],
        "win_pct": _percentage(wins, len(game_logs)),
    }


def _mma_metrics_for_fight(raw: dict[str, float]) -> dict[str, float | None]:
    return {
        "round": raw["round"],
        "fight_minutes": round(raw["fight_minutes"], 1),
        "title_fight": raw["title_fight"],
        "finish": raw["finish"],
    }


def _mma_summary_metrics(fight_logs: list[dict[str, Any]]) -> dict[str, float | None]:
    wins = [item for item in fight_logs if item.get("result") == "W"]
    losses = [item for item in fight_logs if item.get("result") == "L"]
    raw_totals = _sum_raw_metrics(fight_logs)
    return {
        "ko_tko_wins": float(sum(1 for item in wins if item.get("method_bucket") == "ko_tko")),
        "submission_wins": float(sum(1 for item in wins if item.get("method_bucket") == "submission")),
        "decision_wins": float(sum(1 for item in wins if item.get("method_bucket") == "decision")),
        "ko_tko_losses": float(sum(1 for item in losses if item.get("method_bucket") == "ko_tko")),
        "submission_losses": float(sum(1 for item in losses if item.get("method_bucket") == "submission")),
        "decision_losses": float(sum(1 for item in losses if item.get("method_bucket") == "decision")),
        "finish_rate": _percentage(raw_totals["finish"], len(fight_logs)),
        "win_pct": _percentage(len(wins), len(fight_logs)),
        "avg_round": _round_average(raw_totals["round"], len(fight_logs)),
        "avg_fight_minutes": _round_average(raw_totals["fight_minutes"], len(fight_logs)),
        "title_fights": raw_totals["title_fight"],
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

    if parsed.sport_key in {"NBA", "WNBA"}:
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
    if parsed.sport_key == "SOCCER":
        return _build_soccer_explanation(player_name, parsed, summary_metrics, games_analyzed)
    if parsed.sport_key == "TENNIS":
        wins = int(round(((summary_metrics.get("win_pct") or 0.0) / 100.0) * games_analyzed))
        losses = max(games_analyzed - wins, 0)
        return _build_tennis_explanation(player_name, parsed, wins, losses, summary_metrics)
    if parsed.sport_key == "UFC":
        wins = int(round(((summary_metrics.get("win_pct") or 0.0) / 100.0) * games_analyzed))
        losses = max(games_analyzed - wins, 0)
        return _build_mma_explanation(player_name, parsed, wins, losses, summary_metrics)
    return (
        f"{player_name} posted a .{int((summary_metrics['batting_avg'] or 0) * 1000):03d} batting average, "
        f"{int(summary_metrics['home_runs'] or 0)} home runs, {int(summary_metrics['rbis'] or 0)} RBI, "
        f"and a {summary_metrics['ops']:.3f} OPS over {scope}."
    )


def _build_soccer_explanation(
    player_name: str,
    parsed: ParsedStatsQuery,
    summary_metrics: dict[str, float | None],
    games_analyzed: int,
) -> str:
    scope = f"the last {games_analyzed} matches"
    if parsed.split:
        scope += " at home" if parsed.split == "home" else " away"
    if parsed.opponent:
        scope += f" against {parsed.opponent}"
    return (
        f"{player_name} made {int(summary_metrics['appearances'] or 0)} appearances, "
        f"scored {int(summary_metrics['goals'] or 0)} goals, added {int(summary_metrics['assists'] or 0)} assists, "
        f"and took {int(summary_metrics['shots'] or 0)} shots over {scope}."
    )


def _build_soccer_season_explanation(
    player_name: str,
    summary_metrics: dict[str, float | None],
    coverage_label: str,
) -> str:
    return (
        f"{player_name} has {int(summary_metrics['appearances'] or 0)} appearances "
        f"({int(summary_metrics['starts'] or 0)} starts, {int(summary_metrics['sub_appearances'] or 0)} as a substitute), "
        f"{int(summary_metrics['goals'] or 0)} goals, {int(summary_metrics['assists'] or 0)} assists, "
        f"and {int(summary_metrics['shots'] or 0)} shots in {coverage_label}."
    )


def _build_tennis_explanation(
    player_name: str,
    parsed: ParsedStatsQuery,
    wins: int,
    losses: int,
    summary_metrics: dict[str, float | None],
) -> str:
    if parsed.query_type == "last_n_games":
        scope = f"the last {wins + losses} matches"
    else:
        scope = f"the {parsed.season} season"
    if parsed.opponent:
        scope += f" against {parsed.opponent}"

    titles = int(summary_metrics.get("titles") or 0)
    titles_clause = f" and won {titles} title" + ("s" if titles != 1 else "") if parsed.query_type == "season" and titles else ""
    return (
        f"{player_name} went {wins}-{losses} with a {int(summary_metrics['sets_won'] or 0)}-"
        f"{int(summary_metrics['sets_lost'] or 0)} set edge and {int(summary_metrics['games_won'] or 0)}-"
        f"{int(summary_metrics['games_lost'] or 0)} games won over {scope}{titles_clause}."
    )


def _build_tennis_summary_stat_line(wins: int, losses: int, summary_metrics: dict[str, float | None]) -> str:
    base = (
        f"{wins}-{losses} record, {int(summary_metrics['sets_won'] or 0)}-{int(summary_metrics['sets_lost'] or 0)} in sets, "
        f"{int(summary_metrics['games_won'] or 0)}-{int(summary_metrics['games_lost'] or 0)} in games"
    )
    titles = int(summary_metrics.get("titles") or 0)
    if titles:
        suffix = " title" if titles == 1 else " titles"
        return f"{base}, {titles}{suffix}"
    return base


def _build_mma_explanation(
    player_name: str,
    parsed: ParsedStatsQuery,
    wins: int,
    losses: int,
    summary_metrics: dict[str, float | None],
) -> str:
    if parsed.query_type == "last_n_games":
        scope = f"the last {wins + losses} fights"
    else:
        scope = f"the {parsed.season} calendar year"
    if parsed.opponent:
        scope += f" against {parsed.opponent}"
    return (
        f"{player_name} went {wins}-{losses} with {int(summary_metrics['ko_tko_wins'] or 0)} KO/TKO wins, "
        f"a {summary_metrics['finish_rate']:.1f}% finish rate, and {summary_metrics['avg_round']:.1f} average rounds over {scope}."
    )


def _build_mma_summary_stat_line(wins: int, losses: int, summary_metrics: dict[str, float | None]) -> str:
    parts = [f"{wins}-{losses} record"]
    if summary_metrics.get("ko_tko_wins"):
        parts.append(f"{int(summary_metrics['ko_tko_wins'])} KO/TKO wins")
    if summary_metrics.get("submission_wins"):
        parts.append(f"{int(summary_metrics['submission_wins'])} submission wins")
    if not summary_metrics.get("ko_tko_wins") and not summary_metrics.get("submission_wins") and summary_metrics.get("decision_wins"):
        parts.append(f"{int(summary_metrics['decision_wins'])} decision wins")
    parts.append(f"{summary_metrics['finish_rate']:.1f}% finish rate")
    parts.append(f"{summary_metrics['avg_round']:.1f} avg rounds")
    return ", ".join(parts)


def _build_mma_fight_stat_line(
    result: str,
    opponent: str,
    method: str,
    round_number: int,
    time_value: str,
    event_name: str | None,
) -> str:
    parts = [f"{result} vs {opponent}", method]
    if round_number > 0:
        parts.append(f"R{round_number} {time_value}".strip())
    elif time_value:
        parts.append(time_value)
    prefix = ", ".join(part for part in parts if part)
    if event_name:
        return f"{prefix} ({event_name})"
    return prefix


def _mma_player_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return (((payload.get("page") or {}).get("content") or {}).get("player") or {})


def _mma_method_bucket(method: str | None) -> str:
    lowered = (method or "").lower()
    if "ko" in lowered:
        return "ko_tko"
    if "sub" in lowered:
        return "submission"
    if "dec" in lowered:
        return "decision"
    return "other"


def _build_tennis_match_stat_line(
    result: str,
    opponent: str,
    display_scoreline: str | None,
    round_name: str | None,
    tournament_name: str | None,
) -> str:
    details = [detail for detail in (round_name, tournament_name) if detail]
    prefix = f"{result} vs {opponent}"
    if display_scoreline:
        prefix += f", {display_scoreline}"
    if details:
        prefix += f" ({', '.join(details)})"
    return prefix


def _tennis_round_name_from_note(competition: dict[str, Any]) -> str | None:
    note_text = (((competition.get("notes") or [{}])[0]).get("type")) or ""
    return note_text.split(" - ", 1)[0].strip() or None


def _tennis_stats_map(statistics_payload: dict[str, Any]) -> dict[str, float]:
    stats: dict[str, float] = {}
    categories = (((statistics_payload.get("splits") or {}).get("categories")) or [])
    for category in categories:
        for stat in category.get("stats") or []:
            stats[stat.get("name") or ""] = _parse_number(stat.get("value"))
    return stats


def _tennis_scoreline_data(
    competition: dict[str, Any],
    player_competitor: dict[str, Any],
    opponent_competitor: dict[str, Any],
    player_won: bool,
    espn_client: EspnPublicClient,
) -> dict[str, float | str | None]:
    note_text = (((competition.get("notes") or [{}])[0]).get("text")) or ""
    parsed = _parse_tennis_scoreline(note_text, player_won)
    if parsed:
        return parsed
    return _tennis_scoreline_from_linescores(player_competitor, opponent_competitor, espn_client)


def _parse_tennis_scoreline(note_text: str, player_won: bool) -> dict[str, float | str | None] | None:
    matches = list(_TENNIS_SET_RE.finditer(note_text or ""))
    if not matches:
        return None

    parts: list[str] = []
    sets_won = 0.0
    sets_lost = 0.0
    games_won = 0.0
    games_lost = 0.0

    for match in matches:
        left = _parse_number(match.group("left"))
        right = _parse_number(match.group("right"))
        tiebreak_left = match.group("tiebreak_left")
        tiebreak_right = match.group("tiebreak_right")
        if not player_won:
            left, right = right, left
            tiebreak_left, tiebreak_right = tiebreak_right, tiebreak_left

        games_won += left
        games_lost += right
        if left > right:
            sets_won += 1
        elif right > left:
            sets_lost += 1

        if tiebreak_left and tiebreak_right:
            parts.append(f"{int(left)}-{int(right)} ({tiebreak_left}-{tiebreak_right})")
        else:
            parts.append(f"{int(left)}-{int(right)}")

    return {
        "display_scoreline": " ".join(parts),
        "sets_won": sets_won,
        "sets_lost": sets_lost,
        "games_won": games_won,
        "games_lost": games_lost,
    }


def _tennis_scoreline_from_linescores(
    player_competitor: dict[str, Any],
    opponent_competitor: dict[str, Any],
    espn_client: EspnPublicClient,
) -> dict[str, float | str | None]:
    player_ref = ((player_competitor.get("linescores") or {}).get("$ref"))
    opponent_ref = ((opponent_competitor.get("linescores") or {}).get("$ref"))
    if not player_ref or not opponent_ref:
        return {
            "display_scoreline": None,
            "sets_won": 0.0,
            "sets_lost": 0.0,
            "games_won": 0.0,
            "games_lost": 0.0,
        }

    player_payload = espn_client.fetch_json_ref(player_ref)
    opponent_payload = espn_client.fetch_json_ref(opponent_ref)
    player_linescores = {int(item.get("period") or 0): _parse_number(item.get("value")) for item in player_payload.get("items") or []}
    opponent_linescores = {int(item.get("period") or 0): _parse_number(item.get("value")) for item in opponent_payload.get("items") or []}
    periods = sorted(set(player_linescores) | set(opponent_linescores))
    if not periods:
        return {
            "display_scoreline": None,
            "sets_won": 0.0,
            "sets_lost": 0.0,
            "games_won": 0.0,
            "games_lost": 0.0,
        }

    parts: list[str] = []
    sets_won = 0.0
    sets_lost = 0.0
    games_won = 0.0
    games_lost = 0.0
    for period in periods:
        player_games = player_linescores.get(period, 0.0)
        opponent_games = opponent_linescores.get(period, 0.0)
        games_won += player_games
        games_lost += opponent_games
        if player_games > opponent_games:
            sets_won += 1
        elif opponent_games > player_games:
            sets_lost += 1
        parts.append(f"{int(player_games)}-{int(opponent_games)}")

    return {
        "display_scoreline": " ".join(parts),
        "sets_won": sets_won,
        "sets_lost": sets_lost,
        "games_won": games_won,
        "games_lost": games_lost,
    }


def _parse_soccer_starts_block(stats_block: dict[str, Any]) -> tuple[int, int]:
    for value in stats_block.get("vals") or []:
        if value.get("lbl") != "START (SUB)":
            continue
        raw_value = str(value.get("val") or "").strip()
        match = re.match(r"(?P<starts>\d+)\s*\((?P<subs>\d+)\)", raw_value)
        if match:
            return int(match.group("starts")), int(match.group("subs"))
    return 0, 0


def _default_soccer_stats_key(stats_payload: dict[str, Any]) -> str | None:
    team_id = stats_payload.get("tm")
    competition_id = stats_payload.get("lg")
    if team_id and competition_id:
        return f"{competition_id}-{team_id}"
    return None


def _filter_soccer_logs_for_current_context(game_logs: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    player_payload = _soccer_player_payload(payload)
    stats_payload = player_payload.get("stats") or {}
    stats_rows = ((stats_payload.get("splts") or {}).get("stats") or {})
    stats_key = _default_soccer_stats_key(stats_payload)
    stats_row = _coerce_soccer_stats_row(stats_rows.get(stats_key) or next(iter(stats_rows.values()), []))
    competition_name = _soccer_competition_name_from_row(stats_row)
    team_id = str(stats_payload.get("tm") or "")

    filtered = [
        item
        for item in game_logs
        if _soccer_log_matches_team(item, team_id) and _soccer_competition_matches(item.get("competition"), competition_name)
    ]
    return filtered or game_logs


def _coerce_soccer_stats_row(value: Any) -> list[Any]:
    if value and isinstance(value, list) and isinstance(value[0], list):
        return value[0]
    return value or []


def _soccer_competition_name_from_row(stats_row: list[Any]) -> str:
    if not stats_row:
        return ""
    label = str(stats_row[0] or "")
    label = re.sub(r"^\d{4}(?:-\d{2,4})?\s+", "", label)
    return label.strip()


def _soccer_log_matches_team(game_log: dict[str, Any], team_id: str) -> bool:
    team_uid = str(((game_log.get("team_uid")) or ""))
    return bool(team_id) and team_uid.endswith(f"~t:{team_id}")


def _soccer_competition_matches(game_competition: str | None, expected_competition: str) -> bool:
    if not expected_competition:
        return True
    return _normalize_name(game_competition) == _normalize_name(expected_competition)


def _map_soccer_season_values(stats_labels: list[dict[str, Any]], stats_row: list[Any]) -> dict[str, float]:
    values: dict[str, float] = {}
    if not stats_row:
        return values
    metric_values = stats_row[1:]
    for index, label in enumerate(stats_labels):
        if index >= len(metric_values):
            break
        values[label.get("data") or f"metric_{index}"] = _parse_number(metric_values[index])
    return values


def _soccer_location(at_vs: str | None) -> str:
    if at_vs == "@":
        return "home"
    if at_vs == "vs":
        return "away"
    return "neutral"


def _parse_score_pair(value: Any) -> tuple[float, float]:
    raw = str(value or "0-0").strip()
    if "-" not in raw:
        return 0.0, 0.0
    left, right = raw.split("-", 1)
    return _parse_number(left), _parse_number(right)


def _sum_pair(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(left + right, 1)


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
        "goals_per_match",
        "assists_per_match",
        "shots_per_match",
        "shots_on_target_per_match",
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
