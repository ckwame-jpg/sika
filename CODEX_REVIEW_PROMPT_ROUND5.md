# Codex Review Prompt - Round 5

You are reviewing **PR 9** on branch `claude/compassionate-keller-63679f`
(sika repo). PR 9 is the operational follow-up to your round-4 review
(`CODEX_REVIEW_NOTES_ROUND4.md`).

Your round-4 verdict was a clean green-light on the five round-3
functional gaps closed by PR 8, with three operational caveats:

1. **Savant warm overscope** — passing `savant` into
   `warm_mlb_advanced_for_athletes` fanned out batter Statcast for every
   `mlb_stats_player_ids` sidecar, not just probable starters. Sidecar
   list grows over time → wall-clock risk.
2. **Late-day pitcher catch** — 05:15 daily warm tick misses TBD
   starters and late scratches; you suggested either a second cron or
   having `lineup_refresh` enqueue a pitcher-only warm.
3. **Matcher tightening** — `_match_mlb_event` uses aggregate event
   tokens; defensive but uncommon. **Out of scope for PR 9** — keep
   deferred.

PR 9 closes #1 and #2.

## What changed

### Round-4 #1 — Savant pitcher/batter split

`warm_mlb_advanced_for_athletes` in
`apps/api/app/services/mlb_advanced.py` now accepts
`savant_pitcher` and `savant_batter` as separate kwargs. The legacy
`savant` kwarg is preserved as a back-compat shorthand that fills both
when the specific kwargs are not supplied. The cron in
`apps/api/app/services/refresh_jobs.py` now passes
`savant_pitcher=BaseballSavantClient()` only — batter Statcast for the
sidecar list is no longer triggered by the daily tick.

Two regression tests in `apps/api/tests/test_pr9_review_fixes.py`:
- `test_warm_mlb_advanced_savant_pitcher_only_skips_batter_statcast` —
  proves `savant_pitcher` only fires the pitcher loop.
- `test_warm_mlb_advanced_back_compat_savant_kwarg_still_warms_both` —
  proves the legacy `savant=` shorthand still warms both halves so
  pre-PR-9 callers don't regress.

### Round-4 #2 — Late-day pitcher warm enqueue

`lineup_refresh` now extracts probable-starter IDs from the schedule it
just fetched (`_extract_probable_pitcher_ids` reused from PR 8) and
enqueues an `advanced_stats_warm` job with
`scope="lineup_refresh_pitchers"` and `details.pitchers_only=True`.

The `advanced_stats_warm` worker honors `details.pitchers_only` by
skipping the NBA warm-pass and zeroing out the batter sidecar list
before invoking `warm_mlb_advanced_for_athletes` — so the late-day tick
costs only the small probable-starter set. Job details now include
`pitcher_warm_enqueued`, `late_day_pitcher_ids_seen`, and `pitchers_only`.

`enqueue_refresh_job` already coalesces by `(kind, scope)` so the 11:00
and 15:00 `lineup_refresh` ticks don't pile up duplicate warm jobs;
the second tick refreshes `details.pitcher_ids` on the queued row.

Three regression tests:
- `test_lineup_refresh_enqueues_pitcher_only_advanced_stats_warm` —
  end-to-end: walks a schedule, matches the sika event, writes the
  lineup row under `event.id`, and asserts a queued
  `advanced_stats_warm` job carrying the probable-starter IDs and the
  `pitchers_only` flag.
- `test_lineup_refresh_pitcher_warm_coalesces_existing_queue` — proves
  back-to-back enqueues coalesce to a single queued job with merged
  pitcher IDs.
- `test_advanced_stats_warm_pitchers_only_skips_batter_warming` —
  proves `pitchers_only=True` short-circuits batter and NBA warming.

## What I want from this review

A focused verdict per round-4 caveat:
- Caveat #1 (Savant scope): **closed** / **partially closed with a
  specific gap** / **regressed**.
- Caveat #2 (late-day pitcher catch): same scale.
- Caveat #3 (matcher tightening): unchanged on purpose. Don't open it
  as a new finding unless something has visibly broken.

If you spot something unrelated that's clearly a bug (not a polish),
flag it as "new" rather than mixing it into the round-4 follow-up
status.

## Out of scope (still deferred — same list as round 4)

- `feature_attribution.py` driver attribution module
- Median imputation in the ML dataset path
- Sample-weighted training (`sample_weight` in HGB candidate fits)
- Triggered v2-only retrain at `>= 2,000` settled advanced rows
- `nba_props_v2` / `mlb_props_v2` family-key migration with v1 serving
  fallback
- Backend `stats_query.py` populating `percentiles` /
  `metric_categories` for the UI grid (frontend contract + types
  already shipped in PR 4)

## Verification I ran locally

- `pytest apps/api/tests/test_pr9_review_fixes.py` — 5/5 pass
- `pytest apps/api/tests/test_pr8_review_fixes.py
   tests/test_pr7_review_fixes.py tests/test_mlb_advanced.py
   tests/test_scoring.py tests/test_advanced_stats.py` — 71/71 pass
- Full API suite (`pytest apps/api`) — 361/361 pass

## Merge expectation

If you reach a clean **closed** verdict on caveats #1 and #2, this is
the merge candidate. The round-3 functional issues have been green-lit
since round 4; PR 9's only role is operational hardening on top of that
verdict.
