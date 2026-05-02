# Peer-review prompt for Codex

Paste the block below into Codex (or another reviewing agent) along with read access to the repo and the PR diff at `https://github.com/ckwame-jpg/sika/pull/11`.

---

## Context

You are peer-reviewing a 7-commit change against the sika repository (Kalshi sports copilot). The work was split across six logical PRs (1, 2a, 2b, 2c, 3, 4, 5) that all sit on a single branch (`claude/compassionate-keller-63679f`) targeting `main`. The user’s goal: make the heuristic + ML model use advanced NBA/MLB stats end-to-end (ingestion → cache → scoring → ML feature dict → UI), and verify both **game-winner** and **player-prop** predictions are flowing.

Branch: `claude/compassionate-keller-63679f`
PR: <https://github.com/ckwame-jpg/sika/pull/11>
Total diff: 38 files changed, ~7,337 insertions.
Test totals after the branch lands: **335 backend Python tests + 47 frontend vitest tests + 2 ML tests** — all passing.

## Commit-by-commit summary (what to scrutinize per PR)

### `7c3e14f` — PR 1: NBA advanced-stats foundation
- New `apps/api/app/clients/_rate_limit.py` with a process-singleton TokenBucket registry and `parse_retry_after`.
- New `apps/api/app/clients/nba_stats.py` — `NbaStatsClient` for `stats.nba.com`. Header rotation (4 Chrome UAs), required `x-nba-stats-token` / `Origin` / `Referer`, exponential 429 backoff (10s→30s→90s→300s) honoring `Retry-After`. Bucket `rps=0.6, burst=2`.
- 3 cache tables in `models.py`: `nba_advanced_gamelog_cache`, `nba_team_advanced_cache`, `nba_league_percentiles_cache`.
- `apps/api/app/services/advanced_stats.py` orchestrator: `load_nba_advanced` / `load_nba_team_advanced` / `load_nba_league_percentiles`, circuit breaker + daily IP cap (both persisted via `OperatorSetting`), `emit_nba_player_features`, `warm_nba_advanced_for_athletes`.
- New `advanced_stats_warm` refresh-job kind + 05:15 daily cron.
- Resolver wiring in `scoring.py` so `_score_player_prop` writes new feature keys to `predictions.features` when the player is mapped.

**Scrutinize:**
- Does the rate limiter actually serialize concurrent callers, or could two threads race past `acquire()`?
- Is the circuit-breaker reset logic safe across process restarts? (`_record_nba_success` resets but only after a successful fetch; what happens if the breaker trips and the process restarts before any success?)
- Look for SQLAlchemy session-leak patterns in `_operator_get` / `_operator_set`.
- Is `season_param(2024) == "2024-25"` correct for all NBA seasons? (special-case for `season_param(1999)` → `"1999-00"`).

### `62da1f1` — PR 2a: NBA team gamelog + lineup advanced + player-ID resolution
- Adds `fetch_team_advanced_gamelog`, `fetch_lineup_advanced`, `fetch_boxscore_advanced`, `fetch_common_all_players` to `NbaStatsClient`.
- 4 new caches: `nba_team_gamelog_cache`, `nba_lineup_advanced_cache`, `nba_boxscore_advanced_cache`, `nba_player_roster_cache`.
- `resolve_nba_stats_player_id`: name + team match against the cached `commonallplayers` roster, persists the resolved ID back to `EspnPlayerSearchCache.payload["nba_stats_id"]` so subsequent calls are O(1).
- `find_nba_team_id_by_name` for opponent-team lookups.
- `_score_player_prop` emits `opponent_off_rating_recent_5`, `opponent_def_rating_recent_5`, `opponent_pace_recent_5`, `opponent_form_delta_*` when the opposing team’s gamelog is cached.

**Scrutinize:**
- Name-normalization in `_normalize_name` — is the matching deterministic enough to not mis-resolve common names like “Anthony Davis”?
- The resolver sets `nba_stats_id` on the *first* matching `EspnPlayerSearchCache` row. Could there be multiple rows for the same `athlete_id`? If so, only one gets updated.
- The warm function auto-derives all 30 NBA team IDs from `nba_team_advanced_cache` — is the ordering deterministic enough that the rate-limiter doesn’t starve other callers if 30 calls fire in a tight loop?

### `220d72d` — PR 2b: MLB stack
- New `apps/api/app/clients/mlb_stats.py` (`MlbStatsClient`) — sabermetrics, hitting advanced, pitcher sabermetrics, splits (vs L/R, day/night, home/away), gameLog, schedule with lineups + probable-pitcher + weather hydration, venue metadata, team roster, injury report, player search. No auth, bucket `rps=5`.
- New `apps/api/app/clients/baseball_savant.py` (`BaseballSavantClient`) — Statcast batter/pitcher events via CSV, percentile leaderboards, sprint speed, OAA, pitch arsenal. Bucket `rps=2`.
- New `apps/api/app/clients/weather.py` (`WeatherClient`) — OpenWeatherMap when `OPENWEATHER_API_KEY` is set, NWS fallback otherwise. Both code paths normalize to a common payload shape.
- 12 new MLB cache tables: `mlb_batter_advanced`, `mlb_pitcher_advanced`, `mlb_statcast_batter`, `mlb_statcast_pitcher`, `mlb_player_splits`, `mlb_team_gamelog`, `mlb_bullpen_state`, `mlb_lineup`, `mlb_weather`, `mlb_player_roster`, `mlb_league_percentiles`, `mlb_injury_report`.
- New data asset `apps/api/app/data/park_factors.json` — curated FanGraphs 5-year park factors keyed by MLB venue ID.
- `apps/api/app/services/mlb_advanced.py` orchestrator with batter/pitcher/Statcast loaders, splits, park factors (file-backed), weather (with dome short-circuit), lineup, ID resolver, and feature emitters.
- `_score_player_prop` MLB branch emits batter sabermetrics + Statcast features, plus park factors via `event.raw_data.venue_id` and weather (cached only — no live fetch on read).
- 3 new refresh-job kinds: `weather_refresh`, `lineup_refresh`, `advanced_stats_audit` (placeholders that wire in PR 2c follow-up).

**Scrutinize:**
- The Savant CSV aggregator (`_aggregate_statcast_batter_events`) classifies a barrel via `launch_speed_angle == "6"`. Is that the right Statcast classification value, or is barrel a separate boolean column?
- `emit_lineup_features` walks `payload["raw"]["dates"][i]["games"][j]["teams"][side]["probableLineup"]` — is that schema actually emitted by the MLB Stats API hydrate, or did I guess it? Confirm against MLB Stats `/schedule?hydrate=lineups` real responses.
- `_mlb_xstats_anchor_factor` reads `recent_xba` from `season_xba` (typo): the recent value falls back to season because a per-game xBA cache isn’t populated. That’s safe (factor = 1.0 when missing) but odd. Should the loader cache rolling-window xBA explicitly?
- `WeatherClient` falls back to NWS on *any* exception from OpenWeather, including auth/permission errors. Could a misconfigured API key cause silent fall-through to NWS without surfacing the misconfig?

### `8e69858` — PR 2c: NBA long-tail endpoints
- Extends `NbaStatsClient` with `fetch_hustle_stats_player`, `fetch_player_tracking(pt_measure_type=…)`, `fetch_player_clutch`, `fetch_player_defense_dashboard(defense_category=…)`.
- 5 new caches: `nba_hustle_player_cache`, `nba_tracking_cache`, `nba_clutch_player_cache`, `nba_player_defense_cache`, `nba_injury_report_cache` (last is scaffolded for ESPN ingestion in a follow-up).
- `apps/api/app/services/nba_long_tail.py` with loaders + `emit_nba_hustle_features`, `emit_nba_drives_features`, `emit_nba_clutch_features`, `emit_nba_player_defense_features`.
- `_score_player_prop` NBA branch now also emits the prop subject’s hustle/drives/clutch features when the player has an `nba_stats_id` mapping.
- `warm_nba_advanced_for_athletes` extended to refresh all four leaderboards on the same daily cron.

**Scrutinize:**
- `_cache_or_fetch` in `nba_long_tail.py` imports private helpers from `advanced_stats.py` (e.g. `_increment_daily_count`, `_record_nba_failure`). That’s a smell — should those be public?
- Drives features are emitted from raw NBA Stats column names (`DRIVES`, `DRIVE_FGA`, …). If the Stats API renames a column, we silently drop the feature. Is there value in a contract test that pins the expected column set?

### `a8b55f6` — PR 3: Heuristic uses advanced stats as primary inputs + ML feature_set_version v2
- New `apps/api/app/services/heuristic_factors.py` with stat-keyed gating and 5 NBA + 10 MLB factors (efficiency, opp_def, opp_recent_form, pace_advanced, usage_advanced; xstats_anchor, quality_of_contact, starter_advanced, k_rate, pitcher_dominance, park HR/runs/singles, weather, lineup). All factors clamp to `[0.85, 1.15]`.
- `_score_player_prop` runs `compute_advanced_factors(sport, stat_key, features)` after the existing factor block, multiplies `expected`, recomputes `probability_yes`, and appends per-factor reason strings (only when `|delta| ≥ 2%`).
- `apps/ml/ml/training.py` and `apps/ml/ml/cli.py` default `feature_set_version` bumped to `public-feature-set-v2`.

**Scrutinize:**
- The `_mlb_weather_factor` formula combines temperature delta and wind component multiplicatively — is the magnitude (`(temp - 75) * 0.10 / 30`, plus `cos(wind_dir) * wind_speed * 0.005`) tuned against any published study, or is it a guess? Note: I clamped to `[0.85, 1.15]` so the worst-case error is bounded but the slope could still be calibrated wrong.
- `_mlb_xstats_anchor_factor` blends `recent_avg / season_avg` with `recent_xba / season_xba` 50/50 — should the weight depend on sample size?
- `compute_advanced_factors` filters `|value - 1.0| < 1e-4` as no-op factors and drops them. Is that threshold tight enough for downstream attribution, or could a 1.0001 factor accidentally appear in `signal.reasons` rounding to "1.00x"?
- I bumped `feature_set_version` but **did not** bump family keys (`nba_props_v1` → `_v2`). The deferred PR 3b is supposed to handle the family-key bump + `serving_fallback` plumbing + median imputation + sample-weighted training + triggered v2-only retrain. Confirm the version-bump-without-family-bump is safe (i.e. `predictions.feature_set_version` is just a label and doesn’t key any routing).

### `c91d455` — PR 4: UI advanced-metrics-grid + Why this prediction
- New `apps/web/components/stats/advanced-metrics-grid.tsx` rendering metrics tagged `"advanced"` from `summary.metric_categories` with horizontal percentile bars (red < 33, neutral 33–66, green > 66) plus tooltips.
- New `apps/web/components/markets/why-this-prediction.tsx` rendering top-3 drivers from `signal.features.advanced_factors`, with direction arrow and a magnitude bar.
- Wired into `stats-workspace.tsx` (basic metrics in the existing grid, advanced below) and `market-detail-sheet.tsx` (panel below the existing rationale block).
- Contracts: `StatsSummaryRead` gains optional `percentiles` and `metric_categories`. `SignalSnapshotRead.features` stays `Record<string, unknown>`; the WhyThisPrediction component does the runtime guard at the type boundary.

**Scrutinize:**
- The vitest test for WhyThisPrediction asserts `+12.0%` on a factor of 1.12 — is the rounding (`toFixed(1)`) consistent with how the backend rounds factor values to 4 decimals?
- The percentile bar uses `aria-progressbar` with `aria-valuenow`/`min`/`max`. Is that the right ARIA pattern for a "league percentile" semantic, or should it be a `meter`?
- Backend `stats_query.py` is **not yet** populating `percentiles` or `metric_categories` — both are optional in the contract. Is the UI’s graceful-fallback behavior correct (advanced grid hidden when the backend returns nothing)?

### `b38f423` — PR 5: deep paginate + market_discovery job + cron
- `KalshiPublicClient.list_markets` was clipping at `limit` after pagination, so `max_pages > 1` was a silent no-op. Fix: `iter_market_pages` now treats `limit` as per-page and `max_pages` as depth; `list_markets` returns up to `limit * max_pages` markets.
- New `market_discovery` refresh-job kind that runs `refresh_kalshi_markets(include_standalone=True)` and immediately calls `map_markets_to_events` so newly-persisted markets get their `event_id` linkage before the next slate refresh.
- Twice-daily cron (09:00 + 16:00 default-tz) registered in `start_scheduler`.
- New `POST /ops/jobs/market-discovery` HTTP endpoint for manual triggering.

**Scrutinize:**
- The `iter_market_pages` change removed the `remaining` accumulator. That’s intentional (the old behavior was wrong) but verify there’s no caller relying on `markets[:limit]` truncation behavior.
- The cron times are hard-coded to default timezone (`America/Chicago` per `Settings.default_timezone`). Could that miss European MLB / NBA markets that list earlier?
- `map_markets_to_events` is called inside the same DB transaction as `refresh_kalshi_markets`. If the mapper is slow, we hold a long write lock. Worth profiling.

## Cross-cutting questions

1. **Memory rule compliance.** Per the user’s `feedback_main_branch_workflow` memory: direct commits to `main` are guarded; feature-branch + PR is required. Confirm none of the commits land directly on `main`.
2. **No new SQLAlchemy migrations.** The existing pattern is `Base.metadata.create_all()` at boot. Confirm every new model in this branch is reachable from `models.py` so it gets picked up at boot.
3. **Test coverage.** Backend new tests:
   - `test_rate_limit.py` — 11
   - `test_nba_stats_client.py` — 12 (PR 1: 7, PR 2a: 5)
   - `test_advanced_stats.py` — 19 (PR 1) + 11 (PR 2a) = 30
   - `test_mlb_stats_client.py` — 5
   - `test_baseball_savant_client.py` — 5
   - `test_weather_client.py` — 3
   - `test_mlb_advanced.py` — 18
   - `test_nba_long_tail.py` — 11
   - `test_heuristic_factors.py` — 25
   - `test_kalshi_client.py` — +2 regression tests in PR 5
   - `test_market_discovery_job.py` — 2
   - Frontend: `advanced-metrics-grid.test.tsx` (4), `why-this-prediction.test.tsx` (4), `stats-workspace.test.tsx` extended.
   Identify any factor / loader / emitter that lacks a paired test.
4. **Deferred follow-ups acknowledged in commit messages.** Verify the items below are cleanly punted, not silently broken:
   - Driver attribution module (`feature_attribution.py`)
   - Median imputation in `dataset.py`
   - Sample-weighted training in `training.py`
   - Triggered v2-only retrain at ≥ 2,000 settled per family
   - Family-key v2 bump with v1 as `serving_fallback`
   - Backend `stats_query.py` extension to populate `percentiles` + `metric_categories` from the league percentile cache
5. **Live verification.** Manual one-shot ingestion (Fix A in PR 5) showed:
   - Recommendation count went 110 → 174 after the slate refresh saw the new winner markets.
   - Watchlist now contains 4 winner items including a real MLB game_winner ("Arizona vs Chicago C Winner?", edge 20.3%, confidence 43%).
   - Confirm the production cron schedule (`09:00, 16:00` default-tz) won’t introduce unwanted load against Kalshi — the discovery job pulls up to 5,000 markets per run.

## What I want back

- A 1-paragraph headline verdict per PR (ship / hold / needs-changes) + the single most important issue per PR if any.
- A list of any actual bugs (not style, not preferences) you found.
- Anything in the “Scrutinize” bullets that I called out but is fine, so I can stop worrying about it.
- Concrete suggestions for the deferred follow-ups (PR 3b/4b) — especially around v2-family-key bump, sample-weighted training implementation in HGB, and median imputation that survives an artifact reload.
