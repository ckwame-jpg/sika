# Peer-review prompt for Codex — round 2 (post PR 6)

Paste this into Codex with read access to the repo and PR sika#11. Codex's
first-round notes are committed at `CODEX_REVIEW_NOTES.md` for reference.

---

## Context

You previously reviewed branch `claude/compassionate-keller-63679f` (PR
sika#11) covering PRs 1, 2a, 2b, 2c, 3, 4, 5. Your notes flagged 5 actual
bugs / behavioral gaps. I addressed all 5 in **PR 6 — commit `6e9cf65`**
on the same branch. This second-round review should:

1. Verify each PR 6 fix actually closes the original finding without
   regressing other PRs.
2. Scrutinize the new code paths (new helpers, new wiring, schema
   handling) for fresh bugs.
3. Confirm the deferred follow-ups stay cleanly punted.

Branch: `claude/compassionate-keller-63679f`
PR: <https://github.com/ckwame-jpg/sika/pull/11>
PR 6 commit: `6e9cf65`
Test totals after PR 6: **342 backend Python + 47 frontend vitest + 2 ML**
— all passing.

## What PR 6 changed, mapped to your prior findings

### Finding #3 — `_mlb_xstats_anchor_factor` typo

**Original:** both halves of the blend read `features.get("season_xba")`,
so the expected-stat half was always `1.0` and the factor only moved on
actual recent-vs-season AVG.

**Fix in `apps/api/app/services/heuristic_factors.py:130-156`:**
- Actual half: `recent_avg / season_avg` (unchanged).
- Expected half: `season_xba / season_avg` (this points the factor at
  season-level luck regression — when xBA exceeds AVG, the factor pulls
  the prediction up, etc.).
- Docstring rewritten to call out that recent-window xBA is not yet
  cached and the expected half therefore uses season-level xBA only.
- Two regression tests pin the math
  (`test_xstats_anchor_blends_actual_and_expected_ratios`,
  `test_xstats_anchor_factor_does_not_double_read_season_xba`).

**Verify:**
- Is the factor name still misleading? "xstats_anchor" implies recent
  expected stats. Consider whether the rename should land now even if
  the formula is correct.
- Should the actual half use a luck-regressed denominator
  (`season_xba` instead of `season_avg`) for purer regression? My
  current blend mixes "recent regression to season" with "season
  regression to expected" which is two different signals.

### Finding #1 — MLB lineup parser wrong schema

**Original:** `emit_lineup_features` walked
`game.teams.{home,away}.probableLineup`, but the real MLB Stats API
hydrate puts confirmed lineups under
`game.lineups.{homePlayers, awayPlayers}` as flat ordered arrays.

**Fix in `apps/api/app/services/mlb_advanced.py:584-636`:**
- Reads `game.lineups.{homePlayers, awayPlayers}` first as ordered
  arrays of person dicts (the real MLB Stats API shape).
- Falls back to the old `teams.{home,away}.probableLineup` shape so
  legacy fixtures keep working.
- Returns batting-order position from the array index (1-indexed).
- Three new tests cover both shapes + the empty-lineup case.

**Verify:**
- The new shape uses array index as `battingOrder` — MLB schedules
  may include slots like a designated hitter or bench that don't have
  an explicit batting-order field. Is index-as-position correct, or
  should we look for a nested `batting_order` field per player?
- The fallback path still uses the legacy schema. Is it worth keeping
  long-term, or should it be removed once any consumer is migrated?

### Finding #2 — MLB advanced wiring missing in scoring

**Original:** `_score_player_prop` MLB branch only emitted batter
sabermetrics + Statcast, park factors, and weather. Pitcher /
lineup / opponent-team emitters built in PR 2b were never called, so
the heuristic factors tested for `opposing_starter_*` and
`batting_order_position` were dead.

**Fix in `apps/api/app/services/scoring.py:1539-1622`:**
- New helper `_probable_pitcher_identity(event, role)` returns
  `(display_name, espn_athlete_id)` from ESPN's competitor `probables`
  list (next to the existing `_probable_pitcher_era`).
- MLB branch resolves the probable starter via
  `resolve_mlb_stats_player_id` → loads
  `mlb_pitcher_advanced_cache` and `mlb_statcast_pitcher_cache`
  (both cached-only on the read path) → emits via
  `emit_mlb_pitcher_features`.
- Lineup features pulled from `MlbLineupCache` via
  `load_lineup_for_event(db, event_id)`; resolves the prop
  subject's `mlb_stats_id` via the search-cache sidecar and
  emits `batting_order_position`.

**Verify:**
- The starter-resolution path imports `EspnPlayerSearchCache` and
  walks every NBA + MLB row twice (once for the prop subject, once
  inside `resolve_mlb_stats_player_id`). At ~100 markets / refresh
  this should be fine, but the linear scan is O(n_search_rows ×
  n_props). Consider an indexed sidecar field if the cache grows.
- Pitcher resolution uses `team_abbreviation =
  opponent_entry.participant.short_name`. ESPN's short_name is not
  always the MLB Stats abbreviation (e.g. "Rays" vs "TBR"). Falls
  back to name-only match in the resolver, but may resolve to the
  wrong starter for ambiguous names like "Sale".
- I cache-load pitcher Statcast inside the synchronous prop scoring
  path but never block on missing data (allow_network=False). Confirm
  this is the right behavior given the Statcast cache may be cold for
  starters that haven't been touched by the daily warm.

### Finding #4 — Game-winner scoring ignored advanced team context

**Original:** `_score_team_winner` used only win rate / score / rest /
workload / back-to-back. PR 5 made winner markets flow but predictions
were box-score-only.

**Fix in `apps/api/app/services/scoring.py:1063-1168`:**
- New `_winner_advanced_team_edge(db, event, left, right, features)`
  helper.
- NBA path: `find_nba_team_id_by_name` for both teams →
  `load_nba_team_gamelog(allow_network=False)` → reads
  `recent_5_avg.net_rating`. Edge = `(left_net - right_net) * 0.006`.
- MLB path: `_probable_pitcher_identity` → `resolve_mlb_stats_player_id`
  → `load_mlb_pitcher_advanced` for both starters; reads `season_avg.xfip`
  (with `fip` fallback). Edge = `(right_xfip - left_xfip) * 0.05`
  (lower xFIP favors the team facing the *other* starter).
- Edge clamps to `±6%` and is ADDED to `left_win_probability` before
  the final clamp, so it can't dominate the box-score signal.
- Reasons string appended when `|edge| ≥ 1%`.

**Verify:**
- The constants `0.006` (per NetRating point) and `0.05` (per run/9
  xFIP) are not calibrated against a holdout. Are they reasonable
  ballpark values, or should this be tuned? My intent was a soft nudge
  bounded by the ±6% clamp.
- `event.starts_at.year` is used as the season parameter. For NBA
  this is wrong in October-December (season starts in fall but is
  named by the END year). NBA `season_param(2024) → "2024-25"` formats
  correctly, but `find_nba_team_id_by_name` uses the raw year — is
  that the right key for `nba_team_advanced_cache`? (The cache is
  keyed by `season` int.)
- The MLB MOR helper reads `season_avg.xfip OR fip` as a fallback
  chain. xFIP is from a different field; FIP is the wrong metric to
  fall back to (xFIP regresses HR/FB to league average). Consider
  whether this fallback is misleading or if it's better than nothing.

### Finding #5 — `advanced_stats_warm` cron passed empty player ID lists

**Original:** without ID lists, only league-wide leaderboards refreshed;
per-player advanced caches stayed cold.

**Fix in `apps/api/app/services/refresh_jobs.py:737-798`:**
- When `nba_stats_player_ids` / `mlb_stats_player_ids` are not pinned
  by the caller, the dispatch now derives them from
  `EspnPlayerSearchCache.payload.{nba_stats_id, mlb_stats_id}` — the
  set of athletes that have appeared in real prop scoring (the
  resolver writes the sidecar on first match).
- Counts logged as `nba_stats_player_ids_warmed /
  mlb_stats_player_ids_warmed` in `job.details` for visibility.

**Verify:**
- The derivation runs `db.query(EspnPlayerSearchCache).filter(...).all()`
  for both sports; for a long-running deployment with thousands of
  search-cache rows, this could pull a lot. Worth chunking?
- The `payload` JSON column is read directly. If the search cache has
  a row for a player who has been traded / inactive, we'd warm a
  stale ID. Is that worth gating with a recency filter?

## Cross-cutting questions

1. **Test coverage for new code paths.** PR 6 added 7 tests in
   `test_pr6_review_fixes.py` plus the existing suites. Identify any
   new branch (the `_winner_advanced_team_edge` MLB path, the lineup
   sidecar lookup, the warm-cron derivation) that lacks a paired test
   you'd want to see before promotion.

2. **Game-winner advanced edge interaction with calibration.** The
   added `advanced_team_edge` is applied AFTER the box-score
   probability is computed but BEFORE confidence. Should confidence
   adjust when the edge is non-zero (i.e. higher confidence when
   advanced data agrees with box-score)? Today it doesn't.

3. **Deferred follow-ups still deferred?** Confirm none of these
   silently slipped in:
   - Driver attribution module (`feature_attribution.py`)
   - Median imputation in `dataset.py`
   - Sample-weighted training in `training.py`
   - Triggered v2-only retrain at ≥ 2,000 settled per family
   - Family-key v2 bump with v1 as `serving_fallback`
   - Backend `stats_query.py` extension for percentiles + categories

4. **Live verification status (unchanged from round 1):**
   - Player props still flowing (5,000 predictions, ~equal NBA/MLB
     split).
   - Game-winner predictions now flowing as of PR 5 (4 in watchlist
     including `Arizona vs Chicago C Winner?` at 20.3% edge).
   - Advanced team context fired on game-winner scoring after PR 6
     deploys; no production data on it yet because the
     `advanced_team_edge` paths are cached-only and the caches need
     to populate.

## What I want back

- 1-paragraph headline verdict per Codex finding (closed / partial /
  not closed) with the single most important issue per fix if any.
- Any new bugs introduced by PR 6 (not style preferences).
- Anything in "Verify" bullets that you've checked and is fine, so I
  can stop worrying about it.
- A green-light decision on the 5 closed findings so I can record
  this round as resolved.
