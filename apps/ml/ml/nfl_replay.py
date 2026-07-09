"""Smarter NFL PR 9 — 2025-season walk-forward replay.

Replays a full NFL season through the SAME pure pricing functions the
serving path uses (``ml_features.nfl_pricing``) and reports calibration
(Brier / ECE), so the go/no-go decision for flipping NFL live (PR 10)
rests on measured numbers, not vibes.

Three tracks:

1. **Winner** — for each regular-season game in week N, the internal
   margin comes from EPA ratings built ONLY from weeks < N (weeks 1–4
   shrink toward the prior season, mirroring serving), the consensus
   anchor is the CLOSING line from nfldata games.csv (the stand-in for
   the live Odds API anchor), and the blended probability uses the
   production blend weights. Baseline: the de-vigged closing moneyline
   — the sharpest public benchmark; matching it within a few Brier
   points is success for a consensus-anchored model.
2. **Spread ladder** — kernel-grid prices at closing ±0.5/±1.5/±2.5
   thresholds vs realized covers, bucketed near/away from key numbers.
3. **Props** — Normal-tail vs Poisson Brier on yardage stats, using
   walk-forward player means/sds from nflverse weekly stats. This is
   the empirical verdict on the variance=mean pathology.

Leakage notes (documented, deliberate):
- The margin grid (2011–2025) includes the eval season's rows — a
  distribution-SHAPE reuse, not per-game information. The
  spread-conditional margin shape is stable era to era; the blend /
  distribution-class comparisons this replay decides are unaffected.
- Per-game inputs are strictly walk-forward (weeks < N only); the
  leakage-guard test pins that.

Usage (local files skip the download):
    python -m ml.nfl_replay --season 2025 \
        --games /path/games.csv --team-stats /path/stats_team_week_2025.csv \
        --player-stats /path/stats_player_week_2025.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import urllib.request
from collections import defaultdict
from typing import Any, Iterable

from ml_features.nfl_pricing import (
    blend_line,
    blend_probability,
    nfl_margin_yes_probability,
    nfl_total_yes_probability,
    nfl_win_probability,
    normal_tail_yes_probability,
)

GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
TEAM_STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"
PLAYER_STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"

HOME_FIELD_ADVANTAGE = 1.7  # mirror scoring.nfl_game_model
RATING_SHRINK_GAMES = 4.0
CONSENSUS_LINE_WEIGHT = 0.70

from ml_features.nfl_pricing import NFL_STAT_SD_PRIORS, nfl_prop_sd  # noqa: E402


def _fetch_csv(url: str) -> list[dict[str, str]]:
    with urllib.request.urlopen(url) as response:  # noqa: S310 — pinned nflverse URLs
        text = response.read().decode()
    return list(csv.DictReader(io.StringIO(text)))


def _read_csv(path: str | None, url: str) -> list[dict[str, str]]:
    if path:
        return list(csv.DictReader(io.StringIO(open(path).read())))
    return _fetch_csv(url)


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def brier(pairs: list[tuple[float, float]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")


def ece(pairs: list[tuple[float, float]], bins: int = 10) -> float:
    if not pairs:
        return float("nan")
    buckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for p, y in pairs:
        buckets[min(int(p * bins), bins - 1)].append((p, y))
    total = len(pairs)
    out = 0.0
    for rows in buckets.values():
        avg_p = sum(p for p, _ in rows) / len(rows)
        avg_y = sum(y for _, y in rows) / len(rows)
        out += (len(rows) / total) * abs(avg_p - avg_y)
    return out


def devig_moneyline(home_ml: float | None, away_ml: float | None) -> float | None:
    """American moneylines → de-vigged home win probability."""
    def implied(ml: float) -> float:
        return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)

    if home_ml is None or away_ml is None:
        return None
    raw_home, raw_away = implied(home_ml), implied(away_ml)
    total = raw_home + raw_away
    return raw_home / total if total > 0 else None


# -----------------------------------------------------------------------------
# Walk-forward team ratings (points/game scale, mirrors serving math)

def build_ratings_through_week(
    team_rows: list[dict[str, str]], max_week_exclusive: int
) -> dict[str, dict[str, float]]:
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"epa": 0.0, "plays": 0.0, "games": 0.0})
    for row in team_rows:
        if row.get("season_type") != "REG":
            continue
        week = _num(row.get("week")) or 0
        if week >= max_week_exclusive:
            continue
        team = row.get("team") or ""
        epa = (_num(row.get("passing_epa")) or 0.0) + (_num(row.get("rushing_epa")) or 0.0)
        plays = (
            (_num(row.get("attempts")) or 0.0)
            + (_num(row.get("carries")) or 0.0)
            + (_num(row.get("sacks_suffered")) or 0.0)
        )
        agg[team]["epa"] += epa
        agg[team]["plays"] += plays
        agg[team]["games"] += 1.0
    out: dict[str, dict[str, float]] = {}
    for team, acc in agg.items():
        if acc["plays"] <= 0:
            continue
        epa_per_play = acc["epa"] / acc["plays"]
        plays_pg = acc["plays"] / acc["games"]
        out[team] = {"rating": epa_per_play * plays_pg, "games": acc["games"]}
    return out


def prior_season_ratings(games_rows: list[dict[str, str]], season: int) -> dict[str, float]:
    """Prior-season net points/game from results — the shrink target.
    (Serving uses prior EPA ratings; results-based net points is the
    equivalent scale and avoids a second bulk download.)"""
    pf: dict[str, float] = defaultdict(float)
    pa: dict[str, float] = defaultdict(float)
    games: dict[str, float] = defaultdict(float)
    for row in games_rows:
        if row.get("game_type") != "REG" or row.get("season") != str(season - 1):
            continue
        hs, as_ = _num(row.get("home_score")), _num(row.get("away_score"))
        if hs is None or as_ is None:
            continue
        home, away = row.get("home_team") or "", row.get("away_team") or ""
        pf[home] += hs; pa[home] += as_; games[home] += 1
        pf[away] += as_; pa[away] += hs; games[away] += 1
    return {
        team: (pf[team] - pa[team]) / games[team]
        for team in games
        if games[team] > 0
    }


def internal_margin(
    home: str,
    away: str,
    ratings: dict[str, dict[str, float]],
    priors: dict[str, float],
) -> float:
    def strength(team: str) -> float:
        current = ratings.get(team)
        prior = priors.get(team, 0.0)
        if current is None:
            return prior
        weight = min(current["games"] / RATING_SHRINK_GAMES, 1.0)
        return weight * current["rating"] + (1.0 - weight) * prior

    return strength(home) - strength(away) + HOME_FIELD_ADVANTAGE


# -----------------------------------------------------------------------------
# Tracks

def run_winner_and_spread_tracks(
    games_rows: list[dict[str, str]],
    team_rows: list[dict[str, str]],
    season: int,
    blend_weights: Iterable[float] = (0.5, 0.6, 0.7, 0.8),
) -> dict[str, Any]:
    season_games = [
        row for row in games_rows
        if row.get("season") == str(season) and row.get("game_type") == "REG"
        and _num(row.get("result")) is not None
    ]
    ratings_by_week: dict[int, dict[str, dict[str, float]]] = {}
    priors = prior_season_ratings(games_rows, season)

    winner_pairs: dict[str, list[tuple[float, float]]] = {
        "internal": [], "closing_ml": [], "blended_production": [],
    }
    weight_grid: dict[float, list[tuple[float, float]]] = {w: [] for w in blend_weights}
    spread_pairs: list[tuple[float, float]] = []
    spread_key_pairs: list[tuple[float, float]] = []
    total_pairs: list[tuple[float, float]] = []

    for row in season_games:
        week = int(_num(row.get("week")) or 0)
        if week not in ratings_by_week:
            ratings_by_week[week] = build_ratings_through_week(team_rows, week)
        ratings = ratings_by_week[week]
        home, away = row.get("home_team") or "", row.get("away_team") or ""
        margin = _num(row.get("result"))
        spread_line = _num(row.get("spread_line"))
        total_line = _num(row.get("total_line"))
        total_actual = _num(row.get("total"))
        home_won = 1.0 if margin is not None and margin > 0 else (0.5 if margin == 0 else 0.0)

        mu_internal = internal_margin(home, away, ratings, priors)
        p_internal = nfl_win_probability(mu_internal)
        winner_pairs["internal"].append((p_internal, home_won))

        p_closing = devig_moneyline(_num(row.get("home_moneyline")), _num(row.get("away_moneyline")))
        if p_closing is not None:
            winner_pairs["closing_ml"].append((p_closing, home_won))
            p_blend, _w = blend_probability(p_internal, p_closing, 5)
            winner_pairs["blended_production"].append((p_blend, home_won))
            for weight in blend_weights:
                weight_grid[weight].append(
                    (weight * p_closing + (1 - weight) * p_internal, home_won)
                )

        if spread_line is not None and margin is not None:
            consensus_margin = -(-spread_line)  # games.csv spread_line is home-positive already
            mu_blend = blend_line(mu_internal, spread_line, weight=CONSENSUS_LINE_WEIGHT)
            for offset in (-2.5, -1.5, -0.5, 0.5, 1.5, 2.5):
                threshold = spread_line + offset
                p_cover = nfl_margin_yes_probability(mu_blend, threshold)
                outcome = 1.0 if margin > threshold else 0.0
                spread_pairs.append((p_cover, outcome))
                if abs(abs(threshold) - 3.0) <= 0.5 or abs(abs(threshold) - 7.0) <= 0.5:
                    spread_key_pairs.append((p_cover, outcome))
            _ = consensus_margin

        if total_line is not None and total_actual is not None:
            p_over = nfl_total_yes_probability(total_line, total_line - 0.5, direction="over")
            # price the ladder around the closing total with mu = the line
            for offset in (-3.5, 0.5, 3.5):
                threshold = total_line + offset
                p = nfl_total_yes_probability(total_line, threshold, direction="over")
                total_pairs.append((p, 1.0 if total_actual > threshold else 0.0))
            _ = p_over

    report: dict[str, Any] = {"games_evaluated": len(season_games)}
    for name, pairs in winner_pairs.items():
        report[f"winner_{name}"] = {
            "n": len(pairs), "brier": round(brier(pairs), 5), "ece": round(ece(pairs), 5),
        }
    report["winner_blend_grid"] = {
        str(weight): round(brier(pairs), 5) for weight, pairs in weight_grid.items()
    }
    report["spread_ladder"] = {
        "n": len(spread_pairs), "brier": round(brier(spread_pairs), 5),
        "ece": round(ece(spread_pairs), 5),
    }
    report["spread_key_numbers"] = {
        "n": len(spread_key_pairs), "brier": round(brier(spread_key_pairs), 5),
        "ece": round(ece(spread_key_pairs), 5),
    }
    report["total_ladder"] = {
        "n": len(total_pairs), "brier": round(brier(total_pairs), 5),
        "ece": round(ece(total_pairs), 5),
    }
    return report


def _shrunk_sd(stat_key: str, values: list[float]) -> float:
    return nfl_prop_sd(stat_key, values)


def _poisson_tail(expected: float, threshold: float) -> float:
    lam = max(expected, 0.01)
    cutoff = max(int(round(threshold)) - 1, 0)
    term = math.exp(-lam)
    cumulative = term
    for k in range(1, cutoff + 1):
        term *= lam / k
        cumulative += term
    return min(max(1.0 - cumulative, 0.01), 0.99)


def run_props_track(
    player_rows: list[dict[str, str]], season: int, min_history: int = 4
) -> dict[str, Any]:
    by_player: dict[tuple[str, str], list[tuple[int, dict[str, str]]]] = defaultdict(list)
    for row in player_rows:
        if row.get("season_type") != "REG":
            continue
        week = int(_num(row.get("week")) or 0)
        by_player[(row.get("player_id") or "", row.get("position") or "")].append((week, row))

    positions_for_stat = {
        "passing_yards": {"QB"},
        "rushing_yards": {"RB"},
        "receiving_yards": {"WR", "TE"},
    }
    report: dict[str, Any] = {}
    fitted_sds: dict[str, float] = {}
    for stat_key, positions in positions_for_stat.items():
        normal_pairs: list[tuple[float, float]] = []
        poisson_pairs: list[tuple[float, float]] = []
        demeaned: list[float] = []
        for (_pid, position), games in by_player.items():
            if position not in positions:
                continue
            games = sorted(games, key=lambda pair: pair[0])
            values = [(_num(row.get(stat_key)) or 0.0) for _week, row in games]
            player_values = [v for v in values]
            if len(player_values) >= 6:
                mean = sum(player_values) / len(player_values)
                demeaned.extend(v - mean for v in player_values)
            for index in range(min_history, len(games)):
                history = values[:index]
                if sum(history) / len(history) < 15.0:
                    continue  # sub-package players — never a listed prop
                recent10 = history[-10:]
                recent3 = history[-3:]
                expected = (
                    (sum(recent10) / len(recent10)) * 0.55
                    + (sum(history) / len(history)) * 0.30
                    + (sum(recent3) / len(recent3)) * 0.15
                )
                actual = values[index]
                threshold = max(round(expected / 5.0) * 5.0, 5.0) + 0.5
                sd = _shrunk_sd(stat_key, recent10)
                outcome = 1.0 if actual >= threshold else 0.0
                normal_pairs.append(
                    (normal_tail_yes_probability(expected, sd, threshold), outcome)
                )
                poisson_pairs.append((_poisson_tail(expected, threshold), outcome))
        if demeaned:
            variance = sum(v * v for v in demeaned) / len(demeaned)
            fitted_sds[stat_key] = round(math.sqrt(variance), 2)
        report[stat_key] = {
            "n": len(normal_pairs),
            "normal_brier": round(brier(normal_pairs), 5),
            "poisson_brier": round(brier(poisson_pairs), 5),
            "normal_ece": round(ece(normal_pairs), 5),
        }
    report["fitted_sd_by_stat"] = fitted_sds
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--games", help="local games.csv")
    parser.add_argument("--team-stats", help="local stats_team_week csv")
    parser.add_argument("--player-stats", help="local stats_player_week csv")
    args = parser.parse_args()

    games_rows = _read_csv(args.games, GAMES_URL)
    team_rows = _read_csv(args.team_stats, TEAM_STATS_URL.format(season=args.season))
    player_rows = _read_csv(args.player_stats, PLAYER_STATS_URL.format(season=args.season))

    report = {
        "season": args.season,
        **run_winner_and_spread_tracks(games_rows, team_rows, args.season),
        "props": run_props_track(player_rows, args.season),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
