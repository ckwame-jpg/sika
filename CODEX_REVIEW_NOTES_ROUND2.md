# Codex Review Notes — Round 2

Target: `claude/compassionate-keller-63679f` / PR `sika#11`  
Round-2 focus: PR 6 commit `6e9cf65`, which claims to address the first Codex review findings.

I attempted to run `../../../.venv/bin/pytest apps/api/tests/test_pr6_review_fixes.py`, but this local venv is missing `joblib`, so the test process failed during app import before collecting tests. The notes below are based on source inspection.

## Closure Verdicts

- **Finding #1, MLB lineup parser schema: closed for parsing, partial for production flow.** `emit_lineup_features()` now reads the real `game.lineups.homePlayers` / `awayPlayers` schedule shape and falls back to the legacy fixture shape. Index-as-batting-order is reasonable for the observed MLB Stats API arrays. However, I still do not see any production path that writes MLB schedule lineups into `MlbLineupCache`; `lineup_refresh` remains a placeholder, so the parser fix only matters when something manually populates the cache.
- **Finding #2, MLB advanced wiring missing in scoring: partial.** `_score_player_prop` now calls the pitcher and lineup emitters, so the wiring exists. The important remaining issue is cache population: pitcher advanced/statcast caches are read with `allow_network=False`, but `warm_mlb_advanced_for_athletes()` still only warms batter advanced/statcast batter data, not `mlb_pitcher_advanced_cache` or `mlb_statcast_pitcher_cache`. Lineup reads have the same problem because `MlbLineupCache` is not populated by a scheduler path.
- **Finding #3, `_mlb_xstats_anchor_factor` typo: closed.** The double-read bug is fixed, and the docstring now states that the factor uses season-level xBA rather than rolling xBA. I do not think the name needs to block PR 6 as long as the docstring stays explicit.
- **Finding #4, game-winner advanced team context: partial.** `_winner_advanced_team_edge()` is now wired and bounded, but the NBA path uses `event.starts_at.year` as the cache season key. That will miss October-December NBA games if the rest of the app keys NBA seasons by ending year via `default_season_for_sport()`. The MLB path also depends on pitcher caches that PR 6 still does not warm.
- **Finding #5, `advanced_stats_warm` empty ID lists: partial.** The refresh job now derives NBA/MLB IDs from `EspnPlayerSearchCache` sidecars, so the original empty-list problem is improved. It still does not solve PR 6's new starter-pitcher requirement because starter resolution passes `espn_athlete_id=None`, does not write search-cache sidecars, and the MLB warm function never warms pitcher caches.

## New Bugs / Remaining Behavioral Gaps

1. **PR 6 reads pitcher caches that no warm path populates.**
   - Code: `apps/api/app/services/scoring.py:1702-1731`, `apps/api/app/services/mlb_advanced.py:1006-1049`
   - Scoring loads `load_mlb_pitcher_advanced(... allow_network=False)` and `load_mlb_statcast_pitcher(... allow_network=False)`.
   - `warm_mlb_advanced_for_athletes()` declares pitcher counters, but only calls `load_mlb_batter_advanced()` and `load_mlb_statcast_batter()`.
   - Impact: `opposing_starter_*` features and MLB game-winner xFIP edge will usually remain no-ops unless caches are manually seeded.

2. **MLB lineup cache still has no real producer.**
   - Code: `apps/api/app/services/scoring.py:1733-1752`, `apps/api/app/services/refresh_jobs.py:794-797`
   - Scoring reads `load_lineup_for_event(db, event_id=...)` cached-only.
   - The `lineup_refresh` job is still a placeholder and `MlbStatsClient.fetch_schedule()` is not wired into a cache-warming flow.
   - Impact: `batting_order_position` and `lineup_factor` are still effectively absent in production.

3. **NBA game-winner advanced edge can miss fall-season cache keys.**
   - Code: `apps/api/app/services/scoring.py:1100-1106`
   - The helper uses `event.starts_at.year` for `find_nba_team_id_by_name()` and `load_nba_team_gamelog()`.
   - For October-December NBA games, the app's NBA default season convention is ending year, while `event.starts_at.year` is the starting calendar year.
   - Impact: advanced team edge will fail to find warm caches for early-season NBA games unless the cache key convention is different from `default_season_for_sport()`.

4. **PR 6 tests do not exercise the new scoring paths end-to-end.**
   - Code: `apps/api/tests/test_pr6_review_fixes.py`
   - The warm derivation test re-implements the query shape instead of executing the refresh-job branch.
   - There is no test that scores an MLB prop with seeded pitcher + lineup caches and asserts `opposing_starter_xfip`, `batting_order_position`, and `advanced_factors` appear.
   - There is no test for `_winner_advanced_team_edge()` NBA season selection or MLB xFIP edge.

## Verify Bullets That Look Fine

- **Lineup array index:** using the `homePlayers` / `awayPlayers` array index as batting-order position is reasonable for the real schedule hydrate shape I checked. If MLB adds explicit order fields later, prefer those, but index fallback is fine.
- **Legacy lineup fallback:** keeping the old `teams.{home,away}.probableLineup` fallback is harmless and keeps fixtures/mocks working.
- **`xstats_anchor` naming:** not worth blocking PR 6. The formula is now documented clearly enough, though a future rolling-xBA implementation should revisit the name.
- **Cached-only scoring reads:** the scoring path should stay cached-only. The missing piece is warm/population coverage, not making synchronous scoring fetch Statcast or MLB Stats live.
- **Deferred follow-ups remain deferred:** I did not find `feature_attribution.py`, median imputation, sample-weighted training, v2-only retrain trigger, v2 family-key fallback plumbing, or backend `stats_query.py` percentile/category population added in PR 6.

## Green-Light Decision

Do **not** record all five round-1 findings as fully resolved yet.

Green-light as resolved:
- Finding #1 parser bug, with the caveat that cache population is separate.
- Finding #3 xstats typo.

Record as partial / still needs follow-up:
- Finding #2 MLB pitcher and lineup scoring, because the readers are wired but cache producers are missing.
- Finding #4 game-winner advanced context, because NBA season selection and MLB pitcher cache population can prevent it from firing.
- Finding #5 warm cron, because it now derives batter/player sidecars but does not warm the new starter-pitcher caches PR 6 depends on.

## Recommended PR 6 Follow-Ups

1. Update `warm_mlb_advanced_for_athletes()` to call `load_mlb_pitcher_advanced()` and `load_mlb_statcast_pitcher()` for supplied MLB player IDs, or add a separate starter-specific warm list derived from current MLB events' probable pitchers.
2. Make starter resolution pass an ESPN athlete ID when available and persist the sidecar mapping, or store a dedicated starter-ID cache so future warm jobs can pick it up.
3. Implement `lineup_refresh` enough to fetch MLB schedules and call `load_lineup_for_event(..., schedule_payload=...)` for upcoming events.
4. Replace `event.starts_at.year` in the NBA winner helper with the repo's NBA season resolver, e.g. `default_season_for_sport("NBA", event.starts_at.date())`, if cache keys follow the existing ending-year convention.
5. Add integration tests for seeded-cache MLB prop scoring and game-winner advanced edge paths; the current PR 6 tests cover helpers, not the production scoring behavior.

