# Codex Review Notes - Round 3

Review target: PR 7 commit `044cda4` on `claude/compassionate-keller-63679f`.

Verdict: PR 7 closes the NWS user-agent issue, the NBA season-key bug, the MLB cache-status precedence issue, and the prop-subject resolver ID threading issue. I would not mark the full round-2 set as "all closed" yet. The MLB starter warm path is still partial, and the new lineup producer appears to write under a different event key than scoring reads, which means lineup features can still stay empty in production.

I did not run the test suite in this pass; this is a static code review against the PR 7 diff and current branch contents.

## Finding Status

### Round-2 #1 - Pitcher caches read but no warm path: still partial

PR 7 adds `pitcher_ids` to `warm_mlb_advanced_for_athletes`, and explicit pitcher IDs do warm `mlb_pitcher_advanced_cache`. That part is real.

The default scheduled path still depends on `mlb_player_ids` derived from `EspnPlayerSearchCache.payload["mlb_stats_id"]` sidecars in `apps/api/app/services/refresh_jobs.py:763-777`. The comment says probable starters are already covered by sidecars, but the starter resolution paths still call `resolve_mlb_stats_player_id(..., espn_athlete_id=None, ...)` in `apps/api/app/services/scoring.py:1165-1172` and `apps/api/app/services/scoring.py:1729-1736`. The resolver only writes back to `EspnPlayerSearchCache` when `espn_athlete_id` is present in `apps/api/app/services/mlb_advanced.py:831-837`. So a starter who has only appeared as an opposing probable pitcher, not as a prop subject, still will not create a sidecar row and will not be discovered by the default warm dispatch.

There is a second partial: scoring reads both `load_mlb_pitcher_advanced` and `load_mlb_statcast_pitcher` cached-only in `apps/api/app/services/scoring.py:1738-1749`. The warm function only warms Statcast pitcher data if a `savant` client is supplied in `apps/api/app/services/mlb_advanced.py:1068-1071`, but `advanced_stats_warm` calls `warm_mlb_advanced_for_athletes` without one in `apps/api/app/services/refresh_jobs.py:784-789`. That leaves `mlb_statcast_pitcher_cache` without a scheduled producer, so `opposing_starter_whiff_pct`, `opposing_starter_csw_pct`, and `opposing_starter_avg_fastball_velo` remain empty unless manually seeded or warmed by another path.

Idempotence is fine. Warming the same MLB Stats ID as batter and pitcher writes to separate cache tables, and the warm function dedups per list.

### Round-2 #2 - Lineup cache producer: not fully closed

PR 7 adds a real `lineup_refresh` branch, but the producer and consumer appear to use different cache keys.

The producer persists each lineup row with `event_id=str(game_pk)` from MLB Stats API `gamePk` in `apps/api/app/services/refresh_jobs.py:831-842`. The scoring consumer reads with `event_id=str(event.id)` in `apps/api/app/services/scoring.py:1760`. `Event.id` is the app database primary key, not MLB Stats `gamePk` (`apps/api/app/models.py:43-47`). Unless another ingestion path guarantees `event.id == gamePk`, these rows will not meet. The test in `apps/api/tests/test_pr7_review_fixes.py` reproduces the producer loop and then reads the same synthetic `gamePk` key, so it does not catch the production key mismatch.

The per-game envelope shape itself is acceptable. Wrapping one game in `{"dates": [{"games": [game]}]}` matches the parser path and the duplicated envelope is a small cost.

The per-game loop has no inner try/except. One malformed game payload can fail the whole `lineup_refresh` job after the current point in the loop. I would not hold merge solely for that if the key mismatch is fixed, but it is worth hardening while this branch is open.

The 11:00 and 15:00 cron is probably adequate for some slates, but a later afternoon tick would materially improve posted-lineup coverage. The key mismatch is the merge-blocking concern.

### Round-2 #3 - NBA winner edge season key: closed

`_winner_advanced_team_edge` now uses `default_season_for_sport("NBA", event.starts_at.date())`, so October-December NBA games key to the ending season year. The regression test seeds season `2026` for an October 22, 2025 event and exercises the edge helper. This closes the original NBA season-key bug.

MLB season convention also looks fine. `default_season_for_sport("MLB", date)` returns the same year for March through December and prior year for January/February, so April regular-season games and October postseason games key to the same MLB season.

### Round-2 #4 - Scoring coverage tests: still partial

PR 7 adds useful regression tests, but they are still mostly helper-level. The lineup test does not invoke the actual refresh worker and does not prove scoring reads the row the producer writes. There is still no MLB prop scoring integration with a realistic `Event`, seeded pitcher cache, seeded lineup cache, and `_score_player_prop` asserting that pitcher and lineup features appear in `predictions.features`.

This matters because both remaining gaps above would be caught by an integration test that uses the same event identity as production scoring.

### Round-2 #5 - Warm cron empty ID lists: still partial

This folds into #1. Explicit `pitcher_ids` now work, but the default cron still warms pitchers from the batter/prop-subject sidecar list, which does not necessarily contain starter-only pitchers.

## Copilot Findings

### Copilot #1 - NWS user-agent hardcoded email: closed

`apps/api/app/clients/weather.py` now reads the NWS user-agent from settings, with a safe no-email fallback. `rg` only finds `chris@example.com` in the round-3 prompt text, not app code. This is closed.

### Copilot #3 - MLB cache-status precedence: closed

`_merge_cache_status` in `apps/api/app/services/scoring.py:440-464` uses an explicit priority table, so partial misses no longer collapse to `hit`. Treating `missing_id` with the same priority as `miss` is acceptable for the current single status field. Letting unknown statuses bubble as highest priority is also acceptable because it exposes typos instead of hiding them.

### Copilot #4 - Linear EspnPlayerSearchCache scan in scoring: closed for prop subjects

`ResolvedPropSubject` now carries `nba_stats_id` and `mlb_stats_id`, and `_load_advanced` returns resolved IDs for the resolver to thread through. `_score_player_prop` uses `resolved.mlb_stats_id` for lineup features instead of rescanning the search cache. This closes the linear scan issue for prop-subject paths.

This does not close the starter-sidecar coverage issue. The starter resolution paths still pass `espn_athlete_id=None`, so those resolved IDs are not persisted into `EspnPlayerSearchCache`.

## Deferred Follow-ups

These still look deferred, not silently half-implemented:

- No `feature_attribution.py` module.
- No median-imputation work in the ML dataset path beyond existing mentions.
- No visible `sample_weight` training implementation.
- No triggered v2-only retrain gate at `>= 2,000` settled advanced rows per family.
- No `nba_props_v2` / `mlb_props_v2` family key migration with v1 serving fallback.
- Frontend contract/type support for `percentiles` and `metric_categories` exists, but backend `stats_query.py` does not appear to populate the full UI percentile/category summary yet.

One caveat: `feature_set_version` is already `public-feature-set-v2` in `apps/ml/ml/training.py`, so that part is no longer purely deferred. I did not see the rest of the PR 3 training safeguards that were supposed to accompany it.

## Recommended Fix Plan

1. Fix lineup cache identity first. Either make `lineup_refresh` map MLB `gamePk` to the app `Event.id` before writing, or change scoring to read a stable MLB game key that is stored on the app event. The key used by `load_lineup_for_event` must be identical in the producer and consumer.

2. Derive warm `pitcher_ids` from actual current-slate probable starters, not only `EspnPlayerSearchCache` sidecars. When ESPN exposes a probable-pitcher athlete ID, pass it into `resolve_mlb_stats_player_id` so the sidecar write-back path can persist it.

3. Add a scheduled Statcast pitcher producer. The simplest version is passing a `BaseballSavantClient` into `warm_mlb_advanced_for_athletes` from `advanced_stats_warm`, with the same rate/circuit controls as the batter Statcast path. If that is too expensive, add a smaller pitcher-only Statcast warm for current-slate starters.

4. Add one true scoring regression: create an MLB event, seed the exact cache keys the refresh jobs would write, run the scoring path, and assert `opposing_starter_*` plus `batting_order_position` features appear. That test should fail today if the producer writes `gamePk` and scoring reads `event.id`.

5. Wrap per-game lineup persistence in a small try/except so one bad game payload records a failure count and the rest of the slate still warms.

## Merge Guidance

Green-light the closed Copilot fixes and the NBA season fix. Do not mark PR 7 as fully closing all round-2 items until the lineup key mismatch and default starter-warm coverage are fixed.
