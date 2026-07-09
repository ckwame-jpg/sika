#!/usr/bin/env python3
"""Build ``packages/ml-features/ml_features/data/nfl_margin_distribution.json``.

Smarter NFL PR 5 — precomputes the kernel-weighted CONDITIONAL margin
model that prices NFL winner + spread markets.

Why conditional: NFL margins concentrate on key numbers relative to the
closing spread — historically (2011–2025 REG) a game lined at -3 lands
on exactly 3 about 10% of the time, so P(cover -2.5) − P(cover -3.5) is
~10 probability points where a Normal predicts ~3. The model therefore
estimates ``P(home margin > t | projected margin mu)`` as a
Gaussian-kernel-weighted empirical tail over all historical games,
kernel distance measured in closing-spread space (bandwidth 2.0 pts).
Validation against raw spread buckets: kernel gap at mu=3 across
[2.5, 3.5] = 0.090 vs bucket ground truth 0.100 (Normal: 0.030).

The dataset is home/away-symmetrized (each game contributes (m, s) and
(-m, -s)) so pricing is orientation-neutral and win_prob(0) == 0.5
exactly. Ties get half-mass in the win-probability column.

Output grid: mu ∈ [-21, 21] step 0.5; tail columns at thresholds
±0.5 … ±35.5 step 1 (Kalshi thresholds are X.5). Runtime linear
interpolation across both axes lives in ``ml_features.nfl_pricing``
(linear interp at integer t equals the correct P(m > t) + ½·P(m = t)
tie treatment by construction).

Usage:
    python scripts/build_nfl_margin_distribution.py [--games /path/to/games.csv]

Without ``--games`` the script downloads nfldata's games.csv (the same
source the runtime nflverse client uses). Re-run after each season and
commit the refreshed JSON.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import urllib.request
from pathlib import Path

GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
SEASON_MIN, SEASON_MAX = 2011, 2025
KERNEL_BANDWIDTH = 2.0
MU_MIN, MU_MAX, MU_STEP = -21.0, 21.0, 0.5
THRESHOLD_MAX = 35.5  # columns at ±0.5 … ±35.5

OUT_PATH = (
    Path(__file__).resolve().parents[1]
    / "packages" / "ml-features" / "ml_features" / "data" / "nfl_margin_distribution.json"
)


def load_games(path: str | None) -> list[tuple[int, float]]:
    if path:
        text = Path(path).read_text()
    else:
        with urllib.request.urlopen(GAMES_URL) as response:  # noqa: S310 — pinned nflverse URL
            text = response.read().decode()
    rows: list[tuple[int, float]] = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            season = int(row["season"])
        except (KeyError, ValueError):
            continue
        if not (SEASON_MIN <= season <= SEASON_MAX):
            continue
        if row.get("game_type") != "REG":
            continue
        result = row.get("result")
        spread = row.get("spread_line")
        if result in ("", None) or spread in ("", None):
            continue
        rows.append((int(float(result)), float(spread)))
    return rows


def build_grid(games: list[tuple[int, float]]) -> dict:
    # Symmetrize: orientation-neutral pricing, win_prob(0) == 0.5.
    data = games + [(-m, -s) for m, s in games]

    mu_values = [round(MU_MIN + i * MU_STEP, 1) for i in range(int((MU_MAX - MU_MIN) / MU_STEP) + 1)]
    thresholds = [round(-THRESHOLD_MAX + i, 1) for i in range(int(2 * THRESHOLD_MAX) + 1)]

    tails: list[list[float]] = []
    win_probs: list[float] = []
    for mu in mu_values:
        weights = [math.exp(-((s - mu) ** 2) / (2 * KERNEL_BANDWIDTH**2)) for _, s in data]
        total_weight = sum(weights)
        row: list[float] = []
        for t in thresholds:
            tail = sum(w for (m, _), w in zip(data, weights) if m > t)
            row.append(round(tail / total_weight, 5))
        tails.append(row)
        win = sum(w for (m, _), w in zip(data, weights) if m > 0)
        win += 0.5 * sum(w for (m, _), w in zip(data, weights) if m == 0)
        win_probs.append(round(win / total_weight, 5))

    return {
        "_metadata": {
            "source": f"nfldata games.csv, {SEASON_MIN}-{SEASON_MAX} regular season, symmetrized",
            "games": len(games),
            "kernel_bandwidth_points": KERNEL_BANDWIDTH,
            "builder": "scripts/build_nfl_margin_distribution.py",
            "semantics": "tails[i][j] = P(home margin > thresholds[j] | projected margin == mu_values[i])",
        },
        "mu_values": mu_values,
        "thresholds": thresholds,
        "tails": tails,
        "win_probs": win_probs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", help="Path to a local games.csv (skips download)")
    args = parser.parse_args()

    games = load_games(args.games)
    if len(games) < 2000:
        raise SystemExit(f"suspiciously few games ({len(games)}) — refusing to build")
    grid = build_grid(games)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(grid) + "\n")
    print(f"wrote {OUT_PATH} ({len(games)} games, {len(grid['mu_values'])} mu rows)")


if __name__ == "__main__":
    main()
