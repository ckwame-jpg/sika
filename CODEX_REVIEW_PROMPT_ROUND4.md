# Codex Review Prompt - Round 4

You are reviewing **PR 8** on branch `claude/compassionate-keller-63679f` (sika repo).

PR 8 is the follow-up to your round-3 review (`CODEX_REVIEW_NOTES_ROUND3.md`).
Your round 3 verdict was: PR 7 closed the NWS UA + NBA season-key + cache-status
priority + prop-subject ID-threading items, but left these gaps:

1. **Lineup cache key mismatch** — producer wrote `event_id=str(gamePk)`,
   scoring read `event_id=str(event.id)`. The cache rows never met.
2. **Warm cron coverage gap** — default `pitcher_ids` came only from
   `EspnPlayerSearchCache` sidecars, so a starter who'd never been touched
   as a prop subject was never warmed.
3. **No Statcast pitcher producer** — `warm_mlb_advanced_for_athletes`
   only touched Statcast when a `BaseballSavantClient` was supplied, but
   the `advanced_stats_warm` cron didn't supply one.
4. **No true scoring integration test** — every regression seeded helpers
   in isolation; nothing exercised producer→consumer agreement on the
   actual feature dict.
5. **Per-game lineup persistence had no try/except** — one bad payload
   poisoned the rest of the slate.

PR 8 attempts to close all five plus the resolver write-back gap you
flagged inside finding #1 (starter resolution paths threading
`espn_athlete_id=None`).

## What to verify

For each item, please confirm whether PR 8 closes it cleanly, or call out
a remaining hole.

### Round-3 #1 — lineup cache identity

PR 8 introduces `_match_mlb_event(event_index, game)` in
`apps/api/app/services/refresh_jobs.py`. The matcher walks active sika
MLB events and resolves an MLB Stats schedule game to the sika `Event`
via:
- `sport_key == "MLB"`
- `Event.status != "completed"`
- start time within ±3h of `game.gameDate`
- ≥2 shared tokens of length ≥4 between event tokens (name +
  participant display/short names, via `alias_tokens`) and game tokens
  (home + away `team.name`)

The `lineup_refresh` branch (same file) now writes
`event_id=str(matched.id)` instead of `event_id=str(game_pk)`. The new
test
`tests/test_pr8_review_fixes.py::test_lineup_refresh_persists_under_app_event_id_round_trip`
exercises the round trip.

Please confirm the match thresholds aren't too aggressive (e.g. games
that should match but won't because tokens are too short — Cubs/Sox is
the obvious risk) and that the time window logic doesn't accidentally
fire on yesterday's same-team rematch. The current ±3h window assumes
no team plays a doubleheader within the same window — true for MLB
single-day schedules, but worth a sanity check.

### Round-3 #2 — warm cron pitcher coverage

`advanced_stats_warm` (in `apps/api/app/services/refresh_jobs.py`) now:
1. Pulls today's MLB schedule via `MlbStatsClient().fetch_schedule()`
   (already `hydrate=lineups,probablePitcher,weather,broadcasts`).
2. Calls `_extract_probable_pitcher_ids(schedule_payload)` to dedupe
   probable starters across the slate.
3. Merges that list with `details.pitcher_ids` and the sidecar-derived
   `mlb_player_ids`, then passes the union to
   `warm_mlb_advanced_for_athletes(pitcher_ids=...)`.

For the resolver write-back gap: PR 8 also threaded `espn_athlete_id`
through the two starter-resolution paths in
`apps/api/app/services/scoring.py`:
- `_winner_advanced_team_edge` MLB branch (lines ~1157–1190)
- `_score_player_prop` MLB branch (lines ~1726–1740)

The new test
`test_resolver_starter_path_persists_mlb_stats_id_sidecar_when_espn_id_provided`
proves the sidecar write-back fires once the ESPN ID is supplied.

Please call out two things:
- Is the schedule fetch for the warm cron likely to give us probable
  pitchers at 05:15 local, or is that too early for that day's slate?
  If too early, recommend a separate cadence.
- The warm path now calls `MlbStatsClient().fetch_schedule()` *and*
  `lineup_refresh` calls it independently a few hours later. Is that
  duplicate fetch acceptable, or worth a small shared cache?

### Round-3 #3 — Statcast pitcher producer

`advanced_stats_warm` now constructs a `BaseballSavantClient()` and
passes it to `warm_mlb_advanced_for_athletes(savant=...)`. The new test
`test_warm_mlb_advanced_warms_statcast_pitcher_cache_when_savant_provided`
asserts the row lands in `mlb_statcast_pitcher_cache`.

Please flag if there's a rate-limit risk: passing the Savant client
unconditionally means every probable starter triggers a Savant CSV
fetch on each warm tick. The shared bucket caps `rps=2` which should be
enough for a 30-game slate, but worth confirming.

### Round-3 #4 — true scoring integration

The new test
`test_score_player_prop_emits_opposing_starter_and_batting_order_features`
seeds the exact cache keys the refresh jobs would write — sika `Event`
with two participants and ESPN-shaped `probables` raw_data,
`MlbPitcherAdvancedCache` + `MlbStatcastPitcherCache` + `MlbPlayerRosterCache`
+ `MlbLineupCache` keyed by `event.id` + `EspnPlayerSearchCache` +
`EspnPlayerGamelogCache` for the prop subject — then runs
`_score_player_prop` and asserts the resulting feature dict contains
`opposing_starter_xfip`, `opposing_starter_csw_pct`,
`opposing_starter_avg_fastball_velo`, `pitcher_data_complete`, and
`batting_order_position`.

Please confirm the test is genuinely end-to-end (i.e. it would have
failed against the pre-PR-8 code because of the lineup key mismatch),
not just exercising helpers.

### Round-3 #5 — per-game lineup persistence hardening

The `lineup_refresh` per-game block is now wrapped in `try/except`,
incrementing `lineups_failed` on per-game errors and logging via
`logger.warning("lineup_refresh per-game failure (gamePk=%s): %s", ...)`.
The job's final `details` dict carries `lineups_warmed`,
`lineups_unmatched`, and `lineups_failed`. The new test
`test_lineup_refresh_continues_when_one_game_payload_is_malformed`
proves a malformed first game doesn't block a healthy second game.

## Out of scope for PR 8 (explicitly deferred follow-ups)

These remain deferred and you should NOT open new findings on them
unless something has visibly regressed:

- `feature_attribution.py` driver attribution module
- Median imputation in the ML dataset path
- Sample-weighted training (`sample_weight` in HGB candidate fits)
- Triggered v2-only retrain at `>= 2,000` settled advanced rows
- `nba_props_v2` / `mlb_props_v2` family-key migration with v1 serving
  fallback
- Backend `stats_query.py` populating `percentiles` / `metric_categories`
  for the UI grid (frontend contract + types are already there)

## Verification I ran locally before submitting

- `pytest apps/api/tests/test_pr8_review_fixes.py` — 7/7 pass
- `pytest apps/api/tests/test_pr7_review_fixes.py tests/test_mlb_advanced.py
   tests/test_scoring.py tests/test_advanced_stats.py
   tests/test_heuristic_factors.py` — 96/96 pass
- Full API suite (`pytest apps/api`) — 356/356 pass

## What I want from this review

A focused verdict per round-3 item: **closed**, **partially closed
with a specific gap**, or **regressed**. Don't re-open round-1 or
round-2 items unless they've visibly broken. If you spot something
unrelated that's clearly a bug (not a polish), call it out, but flag it
as "new" rather than mixing it into the round-3 follow-up status.

If you reach the same green-light verdict on all five items, please say
so explicitly — I'd like a clean signal before merging PR 8.
