# Codex Review Notes - Round 5

Review target: PR 9 commit `52002af` on `claude/compassionate-keller-63679f`.

Verdict: not quite the merge candidate yet. PR 9 fixes the API split between batter and pitcher Savant clients, and it adds the late-day enqueue mechanism, but the `pitchers_only` worker path still merges the full MLB sidecar list into `pitcher_ids`. That means the "small probable-starter set" job can still fan out over every sidecar player, just through pitcher warming instead of batter Statcast.

I did not rerun the test suite in this pass; this is a static review against the PR 9 code and tests.

## Caveat Status

### Round-4 Caveat #1 - Savant warm overscope: partially closed

The function-level split is good. `warm_mlb_advanced_for_athletes` now accepts `savant_pitcher` and `savant_batter`, while preserving `savant` as a back-compat shorthand in `apps/api/app/services/mlb_advanced.py:1008-1043`. Passing `savant_pitcher` only does not call `load_mlb_statcast_batter`; the loop only uses `batter_savant` in `apps/api/app/services/mlb_advanced.py:1048-1065`.

The daily cron also passes `savant_pitcher=BaseballSavantClient()` rather than the old broad `savant=` argument in `apps/api/app/services/refresh_jobs.py:952-958`. So the exact batter-Statcast overscope from round 4 is fixed.

The remaining gap is that the worker still builds `merged_pitcher_ids` from `details.pitcher_ids + mlb_player_ids + probable_pitcher_ids` before checking `pitchers_only` in `apps/api/app/services/refresh_jobs.py:919-927`. Because `mlb_player_ids` is the full sidecar-derived prop-subject list, `savant_pitcher` can still run pitcher Statcast for every sidecar player in `apps/api/app/services/mlb_advanced.py:1067-1084`. That keeps the same wall-clock risk alive as the sidecar list grows, even though the endpoint moved from batter Statcast to pitcher Statcast.

### Round-4 Caveat #2 - Late-day pitcher catch: partially closed

The enqueue mechanism exists and is directionally right. `lineup_refresh` extracts probable starter IDs from the schedule it already fetched, then enqueues an `advanced_stats_warm` job with `scope="lineup_refresh_pitchers"` and `pitchers_only=True` in `apps/api/app/services/refresh_jobs.py:1045-1070`.

The worker does skip NBA warming and batter warming when `pitchers_only` is true by setting `effective_mlb_player_ids = []` in `apps/api/app/services/refresh_jobs.py:935-946`. That closes part of the cost problem.

But it does not actually limit the pitcher side to the late-day probable IDs. The same `merged_pitcher_ids` sidecar merge described above is still passed into `warm_mlb_advanced_for_athletes` at `apps/api/app/services/refresh_jobs.py:952-958`. So a late-day "pitcher-only" job may still warm pitcher sabermetrics and pitcher Statcast for every sidecar MLB player, not just the probable-starter set discovered by lineup refresh.

There is also a smaller coalescing mismatch: the prompt says the second tick merges pitcher IDs into the queued row. The code overwrites `warm_job.details["pitcher_ids"]` with the latest `late_day_pitcher_ids` in `apps/api/app/services/refresh_jobs.py:1065-1069`. That is probably fine if each schedule fetch is complete, but it is not a union merge.

### Round-4 Caveat #3 - Matcher tightening: unchanged as expected

No new finding here. PR 9 intentionally leaves matcher precision out of scope.

## Test Coverage Notes

`test_warm_mlb_advanced_savant_pitcher_only_skips_batter_statcast` is a useful function-level test for the Savant split.

`test_advanced_stats_warm_pitchers_only_skips_batter_warming` does not actually execute the worker branch. It reads `job.details`, asserts the flag exists, and stops in `apps/api/tests/test_pr9_review_fixes.py:161-201`. That does not prove the real worker skips sidecars, and it misses the current bug where sidecar IDs still flow into `merged_pitcher_ids`.

`test_lineup_refresh_pitcher_warm_coalesces_existing_queue` proves one queued job is reused, but it models a superset overwrite rather than verifying a true union merge in the real branch.

## Recommended Fix

Move the `pitchers_only` decision before `merged_pitcher_ids` is built, or build separate lists:

- Normal daily warm: include explicit `details.pitcher_ids`, sidecar `mlb_player_ids`, and schedule probable IDs if that is still desired.
- `pitchers_only=True`: include only explicit `details.pitcher_ids` plus current schedule probable IDs. Do not include sidecar `mlb_player_ids`.

Then add a regression where `EspnPlayerSearchCache` contains a sidecar MLB batter, the job has `pitchers_only=True`, and a stub warm function/client proves that the sidecar ID is not included in the pitcher warm list.

For coalescing, either truly union old and new `pitcher_ids`, or update the prompt/tests to say "latest schedule payload wins."

## Merge Guidance

Do not treat PR 9 as fully closed yet. The public API split is good, and the late-day enqueue is in place, but the operational bound that motivated PR 9 is still not enforced in the worker.
