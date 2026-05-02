# Codex Review Prompt - Round 6

You are reviewing **PR 10** on branch `claude/compassionate-keller-63679f`
(sika repo). PR 10 is the follow-up to your round-5 review
(`CODEX_REVIEW_NOTES_ROUND5.md`).

Your round-5 verdict was: **partially closed**. PR 9 fixed the public
API split between batter / pitcher Savant clients but missed the
operational bound — the worker built `merged_pitcher_ids` from
`explicit + sidecar + probable` *before* checking `pitchers_only`, so a
"pitchers-only" job still ran pitcher Statcast over every sidecar
batter. The endpoint moved (batter Statcast → pitcher Statcast) but the
fanout was preserved.

PR 10 closes all three round-5 findings:

## What changed

### Round-5 #1 — pitcher list now bounded by mode

In `apps/api/app/services/refresh_jobs.py`, the `advanced_stats_warm`
branch now builds the pitcher list **after** the `pitchers_only` check
and per mode:

- **Daily** (no flag): `details.pitcher_ids ∪ sidecar_mlb_player_ids ∪
  probable_pitcher_ids` (unchanged — the sidecar backstop matters here
  for two-way players or starters that have already shown up as a prop
  subject).
- **`pitchers_only=True`**: `details.pitcher_ids ∪ probable_pitcher_ids`
  only. The sidecar batter list is **excluded**, so the late-day tick is
  bounded to actual probable starters.

A new `mlb_pitcher_ids_warmed` job-detail metric exposes the warmed
list size for observability.

### Round-5 #3 — coalesce now unions, doesn't overwrite

The `lineup_refresh` enqueue path used to overwrite
`warm_job.details["pitcher_ids"]` with the latest tick's list. PR 10
unions prior + new IDs so a partial earlier schedule fetch (11:00) plus
a more-complete later one (15:00) end with the union of both, not just
the latest. Logic lives in
`apps/api/app/services/refresh_jobs.py` inside the `lineup_refresh`
branch.

### Round-5 #2 — true worker-branch test

`apps/api/tests/test_pr10_review_fixes.py::test_pitchers_only_worker_branch_actually_skips_sidecar_pitcher_calls`
seeds an `EspnPlayerSearchCache` row with an MLB sidecar batter, queues
an `advanced_stats_warm` job with `pitchers_only=True` and an explicit
`pitcher_ids=["543037"]`, monkeypatches `SessionLocal` to the test DB
(matches the pattern in `tests/test_refresh_jobs_timeout.py`), and
calls `process_refresh_job_queue_once()`. Assertions:

- The seeded sidecar batter ID (`592450`) is **NOT** in the pitcher
  list passed to `warm_mlb_advanced_for_athletes`.
- The explicit `543037` is preserved.
- NBA warming is skipped (no `warm_nba_advanced_for_athletes` calls).
- `mlb_stats_player_ids` arg is empty (batter side zeroed).
- `savant_pitcher` is supplied; `savant_batter` is not.
- Job moves to `completed` and records `mlb_pitcher_ids_warmed`.

This is the regression you specifically asked for to prove the operational
bound is enforced in the worker, not just on paper.

## What I want from this review

A focused verdict per round-5 finding:
- #1 (sidecar leak): **closed** / **partially closed with a specific
  gap** / **regressed**.
- #2 (paper test): **closed** / **partially closed**.
- #3 (coalesce overwrite): **closed** / **partially closed**.

Round-4 caveat #3 (participant-level token matching in
`_match_mlb_event`) remains intentionally deferred — please don't
re-open unless something has visibly broken.

## Out of scope (still deferred — same list)

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

- `pytest apps/api/tests/test_pr10_review_fixes.py` — 4/4 pass
  (including the worker-branch integration test)
- `pytest apps/api/tests/test_pr9_review_fixes.py
   tests/test_pr8_review_fixes.py tests/test_pr7_review_fixes.py
   tests/test_mlb_advanced.py tests/test_scoring.py
   tests/test_advanced_stats.py tests/test_refresh_jobs_timeout.py` —
  91/91 pass
- Full API suite (`pytest apps/api`) — **365/365 pass**

## Merge expectation

If round 6 returns a clean **closed** verdict on all three round-5
findings, this is the merge candidate. The functional issues have been
green-lit since round 4; PR 10 is the final operational hardening pass.
