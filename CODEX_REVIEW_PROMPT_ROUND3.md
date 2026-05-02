# Peer-review prompt for Codex — round 3 (post PR 7)

Paste this into Codex with read access to the repo. Round-1 and round-2
notes are committed at `CODEX_REVIEW_NOTES.md` and
`CODEX_REVIEW_NOTES_ROUND2.md` for reference.

---

## Context

You previously reviewed branch `claude/compassionate-keller-63679f` (PR
sika#11) twice. Round 1 found 5 actual bugs. PR 6 (commit `6e9cf65`)
addressed them; round 2 marked 2 as fully closed and 3 as partial,
plus added 1 new bug (PR 6 reads pitcher caches that no warm path
populates) and 3 follow-up gaps. Separately, GitHub Copilot's
auto-review flagged 3 real findings on the same PR (NWS user-agent
hardcoded, MLB cache-status precedence, linear search-cache scan).

**PR 7 (commit `044cda4`)** closes every actionable item from both
reviews. This third-round review should:

1. Verify each round-2 partial is now fully closed.
2. Verify each Copilot finding is fully closed.
3. Scrutinize the new code paths (warm dispatch with pitcher IDs, the
   real lineup_refresh implementation, threaded resolver IDs, cache-
   status precedence helper) for fresh bugs.
4. Confirm the deferred follow-ups are still cleanly punted.

Branch: `claude/compassionate-keller-63679f`
PR: <https://github.com/ckwame-jpg/sika/pull/11>
PR 7 commit: `044cda4`
Test totals after PR 7: **349 backend Python + 47 frontend vitest + 2 ML**
— all passing.

## What PR 7 changed, mapped to your prior findings

### Round-2 Finding #1 — pitcher caches read but no warm path

**Status going in: not closed.** ``_score_player_prop`` MLB branch loaded
``mlb_pitcher_advanced_cache`` and ``mlb_statcast_pitcher_cache`` with
``allow_network=False``, but no scheduled job ever wrote them, so
opposing-starter features stayed empty in production.

**Fix in `apps/api/app/services/mlb_advanced.py:1006-1075` and
`apps/api/app/services/refresh_jobs.py:790-810`:**
- `warm_mlb_advanced_for_athletes` gains a new `pitcher_ids: Iterable[str] | None = None` parameter. Each ID fires `load_mlb_pitcher_advanced` and (when a Savant client is supplied) `load_mlb_statcast_pitcher`. Per-list dedup; counters added to the summary dict.
- `advanced_stats_warm` dispatch defaults `pitcher_ids` to the same `mlb_player_ids` list it derives from `EspnPlayerSearchCache.payload["mlb_stats_id"]` sidecars. Reasoning: every starter the resolver has touched gets persisted as `mlb_stats_id` on its own search row, so the sidecar list already covers starters. Two-way players (Ohtani) are warmed in both halves; the warm function dedups internally.
- New regression test pins the pitcher-cache write end-to-end with a stub `MlbStatsClient`.

**Verify:**
- The "starters are already in the sidecar list" assumption — does it really hold? Check `_score_player_prop` MLB branch and `_winner_advanced_team_edge`: both call `resolve_mlb_stats_player_id` with `espn_athlete_id=None` for the starter. The resolver's write-back to `EspnPlayerSearchCache` requires a non-None `espn_athlete_id`. So a starter who has never been a *prop subject* (only a probable opposing pitcher) will not get a sidecar row, and the warm dispatch won't see them. Is this a real coverage gap, or an acceptable bootstrap-period artifact?
- Idempotence: warming the same `mlb_stats_id` as both batter and pitcher fires both loaders. The two loaders write to different cache tables (`mlb_batter_advanced_cache` vs `mlb_pitcher_advanced_cache`), so no row collision. Confirm.

### Round-2 Finding #2 — lineup cache has no producer

**Status going in: not closed.** `MlbLineupCache` was being read by
`emit_lineup_features` but never written.

**Fix in `apps/api/app/services/refresh_jobs.py:769-810` and
`apps/api/app/services/scheduler.py:280-298`:**
- `lineup_refresh` job is no longer a placeholder. The branch calls `MlbStatsClient.fetch_schedule(today)` (default hydrate `"lineups,probablePitcher,weather,broadcasts"`), iterates every game in the response, wraps each in a `{"dates":[{"games":[game]}]}` envelope, and persists via `load_lineup_for_event`. Per-event row in `mlb_lineup_cache`.
- New twice-daily cron (`hour="11,15"`) registered in `start_scheduler` to run ahead of typical first-pitch windows.
- Fetch-failure path logged + `schedule_fetch_failed: True` written to job details so operators can spot upstream outages.
- Regression test runs the per-game persist loop against a synthetic schedule payload, then confirms `emit_lineup_features` reads the persisted row correctly.

**Verify:**
- The cron times (11:00, 15:00) are in `default_timezone` (America/Chicago by default). For an East Coast operator on a 7:05 PM ET first-pitch (8:05 PM Central), the 15:00 Central run lands ~5h before first pitch and ~3h before lineups typically post. Adequate, or should there be a third tick later in the afternoon?
- The branch swallows individual game errors implicitly (no try/except around the per-game `load_lineup_for_event`). If one game's payload is malformed, the whole job stops at that point. Should this be more defensive?
- Each game's envelope wraps a single game in the schedule shape so `emit_lineup_features`'s walker (which iterates `dates[].games[]`) finds it. This works but means each cache row's payload duplicates the schedule envelope shape; small cost. Acceptable?

### Round-2 Finding #3 — NBA winner edge season key

**Status going in: partial.** `_winner_advanced_team_edge` used
`event.starts_at.year` as the cache key, which is wrong for
October-December NBA games — the season is named by the *ending* year
(2025-10-22 is part of the 2025-26 season, keyed `2026`).

**Fix in `apps/api/app/services/scoring.py:1108-1134`:**
- Both NBA and MLB branches now call `default_season_for_sport(sport_key, event.starts_at.date())`, which returns the ending year for NBA (≥October → +1) and the season-start year for MLB (≥March → year, else year-1).
- Regression test pins `default_season_for_sport("NBA", date(2025, 10, 22)) == 2026` and runs the edge helper against seeded `nba_team_advanced_cache` + `nba_team_gamelog_cache` rows under season=2026; the edge fires correctly with `event.starts_at = 2025-10-22`.

**Verify:**
- MLB convention: `default_season_for_sport("MLB", date(2025, 4, 1))` returns 2025. October MLB games (postseason) return 2025 too. Confirm this matches the cache-key convention used by the warm path.

### Round-2 Finding #4 — PR 6 tests don't exercise scoring end-to-end

**Status going in: partial.** Round-2 noted PR 6 had per-helper unit tests
but no integration that scored an MLB prop with seeded pitcher + lineup
caches.

**Fix in `apps/api/tests/test_pr7_review_fixes.py`:**
- `test_lineup_refresh_persists_per_event_payloads_via_load_lineup_for_event` reproduces the lineup_refresh dispatch's per-game loop against a synthetic schedule, persists 2 rows, then runs `emit_lineup_features` against one of them with a known `mlb_stats_id` and asserts `batting_order_position == 2.0`.
- `test_winner_advanced_team_edge_uses_default_season_for_sport` is a true integration test: seeds `nba_team_advanced_cache` (display-name → team_id mapping) and `nba_team_gamelog_cache` rows, builds a mock NBA event with `starts_at = 2025-10-22`, calls `_winner_advanced_team_edge` and asserts the edge value is 0.06 (10-NetRating gap × 0.006).

**Verify:**
- The lineup test reproduces the dispatch logic instead of running the actual `_execute_claimed_job` worker. Acceptable for keeping the test light, but does it leave a coverage hole on the schedule-fetch-failure / `schedule_fetch_failed` flag? Worth adding a separate test that monkey-patches the client to raise?
- No integration test scores an MLB prop end-to-end through `_score_player_prop` with seeded pitcher cache. Per-helper tests for `emit_mlb_pitcher_features` exist; the integration would catch wiring regressions inside `_score_player_prop`. Worth flagging but maybe acceptable given the size of the test setup.

### Round-2 Finding #5 — warm cron empty ID lists

**Status going in: partial.** The cron derived IDs from search-cache
sidecars but didn't warm pitcher caches PR 6 depended on.

**Fix:** addressed by Finding #1 above (pitcher_ids defaults to mlb_player_ids).

### Copilot Finding #1 — NWS user-agent hardcoded with placeholder email

**Fix in `apps/api/app/clients/weather.py:36-50` and `apps/api/app/config.py:111`:**
- Removed `_NWS_USER_AGENT = "sika-sports-copilot (chris@example.com)"` constant.
- Added `Settings.nws_user_agent: str = "sika-sports-copilot"` (plain product token, no email).
- New `_nws_user_agent()` reader strips and falls back to the safe default. Operators wanting NWS' contactable-UA convention can set `NWS_USER_AGENT="myorg (ops@example.com)"` via env.
- Regression test asserts the env override path and the empty-default fallback both work, plus that `@` is not present in the default.

**Verify:**
- Anyone forking the repo no longer carries Chris's contact info. Spot-check git history for any other places the email might appear; if so, follow up.

### Copilot Finding #3 — MLB cache status precedence

**Fix in `apps/api/app/services/scoring.py:455-475`:**
- New `_merge_cache_status(*statuses)` helper with an explicit priority dict (`stale > skipped > miss/missing_id > hit > dome/disabled`). Partial misses now propagate up as `miss` (or `stale` when applicable), never `hit`.
- `_load_mlb_advanced` calls `_merge_cache_status(saber_status, statcast_status)` instead of the ad-hoc if/elif.
- Test: `test_merge_cache_status_picks_most_degraded` covers `(hit, miss) → miss`, `(stale, miss) → stale`, `(hit, hit) → hit`.

**Verify:**
- The status `"missing_id"` is given the same priority as `"miss"` (priority 2). They convey different information — `"miss"` means we tried and got nothing, `"missing_id"` means we never even attempted because resolution failed. Should they have distinct priorities? In practice the helper's caller writes a single status field on the resolver, so the loss of distinction is fine. Confirm.
- The dict treats `"unknown"` statuses as priority 5 (highest, via `.get(s, 5)`). That means a typo in the source code that returns an unrecognized status would bubble up as the "winner". Defensive choice — surfaces typos rather than hiding them. OK?

### Copilot Finding #4 — linear EspnPlayerSearchCache scan in scoring

**Fix in `apps/api/app/services/scoring.py`:**
- `ResolvedPropSubject` gains `nba_stats_id: str | None` and `mlb_stats_id: str | None` fields.
- `_load_advanced`/`_load_nba_advanced`/`_load_mlb_advanced` now return `(payload, cache_status, resolved_ids: dict[str, str])` so the resolver can write the IDs onto `ResolvedPropSubject`.
- The two `db.query(EspnPlayerSearchCache).filter(...).all()` linear scans inside `_score_player_prop` (one for NBA long-tail, one for MLB lineup) are replaced with `resolved.nba_stats_id` / `resolved.mlb_stats_id` direct reads.
- Two regression tests confirm the threading.

**Verify:**
- Old test `test_resolve_nba_stats_player_id_writes_back_to_search_cache` still passes — the search-cache sidecar write is preserved (the resolver still writes back so future cache lookups are O(1)). Threading is on top of that, not a replacement.
- The new return shape `(payload, cache_status, resolved_ids)` is a breaking signature change on a private helper. No other callers. Confirm.

## Cross-cutting questions

1. **Test count.** PR 7 adds 7 tests (`test_pr7_review_fixes.py`). Combined with `test_pr6_review_fixes.py` (7 tests), the round-1/round-2/round-3 fix coverage is 14 dedicated regression tests, all keyed to specific finding numbers. Is there any code path on PR 7 that lacks a test that you'd hold a merge for?

2. **Integration vs unit balance.** PR 7's `test_lineup_refresh_persists_per_event_payloads_via_load_lineup_for_event` reproduces the dispatch logic without going through `_execute_claimed_job`. Is the trade-off (no full worker harness, but pinned dispatch logic + `emit_lineup_features` end-to-end) acceptable?

3. **Deferred follow-ups still deferred?** Confirm none of these silently slipped in:
   - Driver attribution module (`feature_attribution.py`)
   - Median imputation in `dataset.py`
   - Sample-weighted training in `training.py`
   - Triggered v2-only retrain at ≥ 2,000 settled per family
   - Family-key v2 bump with v1 as `serving_fallback`
   - Backend `stats_query.py` extension to populate `percentiles` + `metric_categories`

4. **Live verification.** Production behavior:
   - Player props still flowing (5,000 predictions, ~equal NBA/MLB split).
   - Game-winner predictions flowing as of PR 5 + PR 6.
   - Pitcher caches: will start populating on the next `advanced_stats_warm` cron (05:15 default-tz). Until that runs, MLB pitcher features remain empty in scoring.
   - Lineup cache: will start populating on the next `lineup_refresh` cron (11:00 + 15:00 default-tz). Until then, `batting_order_position` and `lineup_factor` remain empty.

## What I want back

- 1-paragraph headline verdict per finding (closed / still partial / not closed). I'm hoping for "all closed".
- Any new bugs PR 7 introduced (not style preferences).
- Anything in the "Verify" bullets that you've checked and is fine, so I can stop worrying about it.
- Specific call-outs on the remaining "Verify" items I marked uncertain (starter-not-in-sidecar coverage, lineup-fetch-failure resilience, MLB-postseason season key).
