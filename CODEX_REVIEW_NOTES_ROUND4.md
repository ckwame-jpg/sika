# Codex Review Notes - Round 4

Review target: PR 8 commit `07d1d9b` on `claude/compassionate-keller-63679f`.

Verdict: Green-light on the five round-3 functional gaps. PR 8 fixes the production wiring issues I called out in round 3. I do not see a merge-blocking new bug. There are two caveats worth tracking: the new scoring test is a real scoring integration test but not literally a producer-to-scorer test, and the Savant warm path can be heavier than the prompt describes because it warms sidecar batters too, not only probable starters.

I did not rerun the test suite in this pass; this is a static review against the PR 8 code and tests.

## Round-3 Item Status

### 1. Lineup cache identity: closed

The original bug was producer/consumer key disagreement: `lineup_refresh` wrote `event_id=str(gamePk)` while scoring read `event_id=str(event.id)`.

PR 8 fixes that. `lineup_refresh` now builds an active MLB event index and writes lineup rows under the matched app event primary key in `apps/api/app/services/refresh_jobs.py:995-1017`. Scoring still reads `load_lineup_for_event(db, event_id=str(event.id))` in `apps/api/app/services/scoring.py:1771`, so the keys now line up.

The matcher threshold is acceptable for normal MLB names, including the Cubs/Sox concern. `Cubs` is length 4, `White` is length 5, and full city names also contribute strong tokens. A Red Sox matchup still has `Boston` plus the opponent token, so it should clear the `>=2` strong-token gate.

The doubleheader/yesterday-rematch concern is mostly handled by the time window and closest-time tie-break. A same-team doubleheader inside the 3-hour window would still choose the closest `starts_at`, which is the right behavior as long as both app events have distinct start times. A stale same-team rematch on a different day is rejected by the `dt > 3h` check.

Caveat: `_match_mlb_event` uses aggregate event tokens, not participant-pair tokens. That means a schedule game can theoretically match a wrong active event if the correct event is missing and the wrong event shares enough city/opponent tokens. Example class: same-city teams plus a shared opponent. I would not block PR 8 on this, but the more precise version would store home/away participant token sets in the index and require one side-level match per team.

### 2. Warm cron pitcher coverage: closed, with cadence caveat

The default warm path no longer relies only on `EspnPlayerSearchCache` sidecars. `advanced_stats_warm` fetches today's MLB schedule, extracts `teams.{home,away}.probablePitcher.id`, merges those IDs with explicit `details.pitcher_ids` and sidecar IDs, then passes the union into `warm_mlb_advanced_for_athletes` in `apps/api/app/services/refresh_jobs.py:902-943`.

The starter resolver write-back gap is also fixed. `_probable_pitcher_identity` now returns `(display_name, espn_athlete_id)` in `apps/api/app/services/scoring.py:919-940`, and both starter-resolution paths pass that ID into `resolve_mlb_stats_player_id` in `apps/api/app/services/scoring.py:1164-1176` and `apps/api/app/services/scoring.py:1730-1747`. That lets successful starter resolutions persist `mlb_stats_id` sidecars when ESPN provides an athlete ID.

The 05:15 warm cron should catch many MLB probable starters because starters are often listed the prior day or early morning. It will not catch every case: TBD starters, openers, late scratches, and some doubleheader decisions can appear later. I would add a second MLB advanced warm tick around the existing 15:00 lineup refresh, or have `lineup_refresh` enqueue/pass discovered probable IDs into a lightweight pitcher-only warm. That is a coverage improvement, not a reason to hold this PR.

The duplicate schedule fetch is acceptable. One fetch at 05:15 for warm and another at 11:00/15:00 for lineup refresh is small enough that a shared cache is optional.

### 3. Statcast pitcher producer: closed, with volume/timeout risk

PR 8 now passes `BaseballSavantClient()` into `warm_mlb_advanced_for_athletes` from `advanced_stats_warm` in `apps/api/app/services/refresh_jobs.py:938-943`, so `mlb_statcast_pitcher_cache` has a scheduled producer. The dedicated test proving `MlbStatcastPitcherCache` is written when a Savant client is supplied is a good regression.

Rate-limit caveat: the prompt frames this as "every probable starter triggers a Savant CSV fetch." In the current function, supplying `savant` also warms batter Statcast for every `mlb_stats_player_ids` sidecar, not just pitcher Statcast for `merged_pitcher_ids`. That can be much larger than the probable-starter set as the sidecar list grows.

The shared Savant bucket at `rps=2` is conservative enough for a normal full slate of probable starters. The operational risk is wall-clock time: Baseball Savant requests have `timeout=30s` and retries/backoff, while generic refresh-job timeout logic gives non-special job kinds the default maintenance budget plus grace. If the sidecar list grows large or Savant is slow, `advanced_stats_warm` can become a long job. I would either scope this PR's Savant warm to pitcher IDs only, add a higher timeout/budget for `advanced_stats_warm`, or batch/requeue it like other long-running refresh jobs.

This is not the old "no producer" bug; the producer now exists.

### 4. True scoring integration: closed for scoring, test claim is overstated

`test_score_player_prop_emits_opposing_starter_and_batting_order_features` is a real `_score_player_prop` integration test. It seeds the subject, starter roster resolution, pitcher sabermetrics, pitcher Statcast, lineup cache, and gamelog cache, runs scoring with `allow_network=False`, and asserts the resulting feature dict includes `opposing_starter_xfip`, `opposing_starter_csw_pct`, `opposing_starter_avg_fastball_velo`, `pitcher_data_complete`, and `batting_order_position`.

Important nuance: this specific scoring test manually seeds `MlbLineupCache(event_id=str(event.id))`. It would not by itself fail against pre-PR-8 code, because pre-PR-8 scoring also read `event.id`. The pre-PR-8 failure was in the producer writing `gamePk`. The separate `test_lineup_refresh_persists_under_app_event_id_round_trip` catches the producer key fix.

Together, the two tests cover the old bug well enough: one proves producer writes the app key, the other proves scoring consumes that key into features. If you want the literal end-to-end proof, combine them by using `_match_mlb_event`/`load_lineup_for_event` to write the lineup row and then immediately running `_score_player_prop` in the same test.

### 5. Per-game lineup persistence hardening: closed

The per-game block is now wrapped in `try/except` in `apps/api/app/services/refresh_jobs.py:1002-1028`. A malformed game increments `lineups_failed`, logs the gamePk, and allows the rest of the slate to continue. Job details now include `lineups_warmed`, `lineups_unmatched`, and `lineups_failed` in `apps/api/app/services/refresh_jobs.py:1030-1037`.

The test reproduces the branch behavior rather than running the full worker, but the branch code is straightforward and the regression is adequate for the original issue.

## Deferred Follow-ups

The explicitly deferred items still look deferred and should stay out of this PR:

- `feature_attribution.py` driver attribution.
- Median imputation in the ML dataset path.
- Sample-weighted training.
- Triggered v2-only retrain at `>= 2,000` settled advanced rows.
- `nba_props_v2` / `mlb_props_v2` family-key migration with v1 serving fallback.
- Backend `stats_query.py` percentile/category population for the UI grid.

## Recommended Follow-ups

1. Add a late-day pitcher-only warm or enqueue one from `lineup_refresh` so same-day TBD starters get pitcher caches before scoring.

2. Limit the PR 8 Savant warm to pitcher Statcast, or explicitly raise/batch the `advanced_stats_warm` job budget before sidecar lists get large.

3. Tighten `_match_mlb_event` later with participant-level token checks: require each MLB Stats team to match one app participant, instead of relying on aggregate event token overlap.

4. Optional test cleanup: add one combined producer-to-scorer test if you want the test suite to prove the exact old failure in a single assertion path.

## Merge Guidance

I would merge PR 8 from a functional correctness standpoint. The five round-3 issues are closed enough for the advanced-stats rollout to proceed. The remaining items are operational hardening and test precision, not blockers.
