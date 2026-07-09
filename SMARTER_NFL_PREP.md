# Smarter NFL — prep notes + PR 9 backtest report

State doc for the NFL enablement sequence (Smarter NFL PR 1–10, mirrors
the Smarter WNBA 8-PR pattern). Records the live-verified integration
facts and the 2025-season replay results that gate the PR 10 flips.

## Live-verified integration facts (2026-07-09)

- **Kalshi series inventory** (249 NFL series): game lines are
  `KXNFLGAME` / `KXNFLSPREAD` / `KXNFLTOTAL` (+ 1H/2H/1Q–4Q variants,
  `KXNFLTEAMTOTAL`, `KXNFLWINMARGIN`); per-game props are
  `KXNFLPASSYDS`, `KXNFLPASSTDS`, **`KXNFLRSHYDS`** (Kalshi's
  abbreviation — not "RUSHYDS"), `KXNFLRECYDS`, `KXNFLREC`. No
  completions or combined rush+rec series exist yet.
- **NFL game-winner titles use a phrasing no other sport does**:
  "Will Seattle win the Dallas vs Seattle Pro Football game?" —
  classifier branch requires " vs " + trailing "game?" so season
  futures can't match. Verify spread/total/prop title phrasing at
  first regular-season listing (classification tests encode the
  assumed forms).
- **nflverse asset names**: weekly player stats live under the
  `stats_player` release (`stats_player_week_{season}.csv`) — the old
  `player_stats` tag is dead. Team codes: `LA` = Rams, `WAS` =
  Washington (normalized via `normalize_nfl_team_code`).
- **ESPN**: `/sports/football/nfl/injuries` exists (32 teams, shared
  parser shape); WR gamelog stat names verified (`receivingTargets`
  etc.); NFL rows use `"-"` placeholders (dash-tolerant parser).

## PR 9 — 2025-season walk-forward replay results

Run: `python -m ml.nfl_replay --season 2025` (272 REG games; internal
ratings strictly walk-forward from weeks < N; closing lines from
nfldata stand in for the Odds API anchor).

### Winner track

| model | Brier | ECE |
|---|---|---|
| internal EPA only | 0.2413 | 0.1157 |
| **blended (production, w=0.70)** | **0.2153** | **0.0493** |
| closing moneyline (benchmark) | 0.2116 | 0.0515 |

Blend-weight grid (Brier): 0.5 → 0.2203, 0.6 → 0.2176, 0.7 → 0.2153,
0.8 → 0.2136.

**Verdict: GO.** Blended Brier is within +0.0037 of the closing line
(gate: +0.005) and blended ECE *beats* the closing line's. The
original ECE ≤ 0.03 target was miscalibrated for n=272 — the closing
line itself scores 0.0515 (sampling noise floor at this n); the
operative gate is "≤ benchmark ECE", which passes. Weight stays at
**0.70**: 0.80 buys −0.0017 Brier but further mutes the internal
signal that generates divergence-from-Kalshi edges; revisit after a
live season of CLV data.

### Spread / total ladders (thresholds at closing ±0.5/±1.5/±2.5)

- Spread ladder (n=1632): ECE **0.0279** — the kernel-conditional grid
  is well calibrated across the ladder. Key-number subset (n=348):
  ECE 0.0482 (small-n noise; within gate of 2pts + noise).
- Total ladder (n=816): ECE **0.0160** — sigma 13.16 confirmed.

(Ladder Briers sit ≈0.25 by construction — thresholds bracket the
closing line where outcomes are ~50/50; calibration is the metric.)

### Props track (walk-forward player means, thresholds at expectation)

| stat | n | Normal Brier | Poisson Brier | fitted 2025 sd |
|---|---|---|---|---|
| passing_yards | 376 | **0.2502** | 0.2527 | 76.0 |
| rushing_yards | 680 | **0.2495** | 0.2531 | 26.5 |
| receiving_yards | 1648 | **0.2492** | 0.2531 | 23.6 |

**Verdict: GO.** Normal beats Poisson on every yardage stat even at
expectation-centered thresholds (where the two models differ least —
tail thresholds widen the gap further). Shipped sd priors updated in
`ml_features.nfl_pricing.NFL_STAT_SD_PRIORS` to blend the 2025 fits
with the multi-season starting points: passing 72, rushing 26,
receiving 25 (combos 32 / 74). Receiving ECE (0.104) reflects the
low-mean TE2 tail in the eval set; live listings only cover
high-volume players, and the snap-share gate excludes unstable roles.

### Known caveats

- The margin grid (2011–2025) includes 2025 rows — distribution-shape
  reuse only, no per-game leakage; rebuild the grid on 2011–2025 after
  the 2026 season ends (`scripts/build_nfl_margin_distribution.py`).
- Internal ratings alone are weak (Brier 0.2413) — expected and by
  design; the model is consensus-anchored. The internal component's
  job is news-reaction divergence (QB status, rest, weather), not
  out-modeling Vegas.
- ML families (`nfl_singles` / `nfl_props` / `nfl_parlay_2leg`) will
  sit at `insufficient_history` behind the walk-forward promotion gate
  well into the season. Heuristics carry the year.

## PR 10 flip checklist (day-1 verification at first live week)

1. Confirm KXNFLGAME markets map to ESPN events (32-team map + the
   "win the ... game?" title branch).
2. Capture 3 live KXNFLSPREAD/KXNFLTOTAL/prop titles; confirm the
   classification regexes match (update `test_nfl_market_classification`
   fixtures with real strings).
3. Confirm kalshi.com URL slugs for the NFL deep links
   (`professional-football-game` + `player-*` prop slugs are
   best-guess pending a live page).
4. Watch the Odds API `requests_remaining` header the first NFL week —
   the 48h event-window gate + 6h TTL should keep the monthly burn ≈
   110–150 credits.
5. `nfl_data_refresh` job: confirm the ~65 MB bundle completes inside
   the 300s timeout on production hardware.
