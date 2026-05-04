# Sika UI Touch Test — Playwright Session Prompt

Paste this entire file into a new Claude Code session opened in `/Users/chris/Workspace/locked-in/github/sika`. The new session will run an extensive touch test of the sika web app after the PR3 merges.

---

## Context (don't skip — explains what changed)

The full advanced-stats stack landed across an internal **PR 1 → PR 5** sequence (all bundled into sika#11 `486dd83`), then **PR 3a-d** (#12-#15) layered on top. Touch test needs to cover both the **foundation** (data flowing through, components rendering) and the **PR 3 deltas** (per-stat gating, driver detail, percentile UI). Walk back through each layer:

### Foundation (PR 1 → PR 5, all in sika#11 / `486dd83`)

- **PR 1** — NBA advanced stats foundation: `nba_stats.py` client, advanced gamelog cache, league percentiles cache, `emit_nba_player_features` (TS%, USG%, ORtg/DRtg/Net Rtg, Pace, PIE, eFG%).
- **PR 2a** — NBA team-level: `NbaTeamGamelogCache`, opponent recent-form features (`opponent_pace_recent_5`, `opponent_def_rating_recent_5`, etc.), `find_nba_team_id_by_name` resolver, lineup advanced.
- **PR 2b** — MLB advanced stack: `mlb_stats.py` + `baseball_savant.py` clients, batter sabermetrics + Statcast caches, pitcher sabermetrics + Statcast, park factors, weather, lineup confirmations.
- **PR 2c** — NBA long-tail: hustle (deflections, screen assists), drives tracking, clutch splits, defender data — each writes a `*_data_complete` marker.
- **PR 4** — Frontend: `AdvancedMetricsGrid` component + the `WhyThisPrediction` panel — these are the React components PR 3b/3c later wire data into.
- **PR 5** — Game-winner market discovery: deep standalone Kalshi pull for moneyline markets + cron entry. Adds NBA/MLB winner recommendations to the surface.

### PR 3 deltas (live on `main` since 2026-05-03)

- **PR 3a** (scoring): predictions are now scored without double-counting overlapping proxies. User-visible: recommendations may have slightly different `expected` / `edge` / `confidence` than pre-PR3, but the page chrome is unchanged. Smoke test: recommendations still surface, no scoring crashes.
- **PR 3b** (driver attribution): the **"Why this prediction?"** panel (from PR 4) now reads from a server-built `_drivers` payload with curated labels and one-line detail strings ("Recent USG% 32% vs season 28%"). Falls back to the legacy `advanced_factors` derivation only for older predictions captured before this PR. File: `apps/web/components/markets/why-this-prediction.tsx`.
- **PR 3c** (Stats Assistant): the workspace now shows **advanced metrics** (TS%, USG%, ORtg, DRtg... for NBA; xBA, xwOBA, barrel rate, hard-hit rate for MLB batters) with **0-100 percentile bars** and a **basic vs advanced** grouping. The `AdvancedMetricsGrid` component is from PR 4; PR 3c is what populates its data. NBA percentile bars should be populated; MLB percentiles may be empty until the league percentiles writer ships (see `PR3_FOLLOWUP.md` P0).
- **PR 3d** (ML training): offline pipeline only — no UI surface.

### Why this matters for the touch test

The PR 3 deltas only work if the foundation is intact: e.g. the driver panel needs `advanced_factors` in features (PR 1/2 emit those); the percentile bars need league percentile cache rows (PR 1 wrote the NBA loader); game-winner recommendations need the PR 5 ingestion path. Test BOTH layers — a green PR 3 run on top of a broken foundation isn't a real ship signal.

Read `PR3_FOLLOWUP.md` for the post-PR3 state-of-main if you need it. The PR 1-5 work pre-dates that file.

## Your job

1. Start the dev server.
2. Drive the app via Playwright.
3. Walk through every checklist item below, marking each `[x]` once verified or `[ ] FAIL — <reason>` if broken.
4. Capture screenshots for any failure or visually-interesting state.
5. Report a final summary at the end.

## Setup

```bash
# Verify you're on main with PR3 merged
cd /Users/chris/Workspace/locked-in/github/sika
git fetch origin && git switch main && git pull --ff-only
git log --oneline -5      # expect 7eed105 (#15) at top through 1ddc456 (#12)

# Install web deps if needed
cd apps/web
npm install --silent

# Make sure Playwright is installed
npx playwright install chromium 2>&1 | tail -3

# Start the API in one background shell, web dev server in another.
# Run from repo root:
cd /Users/chris/Workspace/locked-in/github/sika
.venv/bin/python -m uvicorn apps.api.app.main:app --port 8000 --reload &
# Note the API base URL — confirm with: curl -s http://localhost:8000/health | head -3

cd apps/web
npm run dev &
# Default Next.js port: 3000. Confirm with: curl -s -o /dev/null -w "%{http_code}" http://localhost:3000
```

If the API needs DB seed data, check `apps/api/tests/conftest.py` for the schema and run any startup script the README references. If the local DB is empty, recommendations may not surface — note that as a setup limitation rather than a regression.

### Playwright execution

**Prefer the Playwright MCP tools** if they're available in your tool list (look for `mcp__*playwright*`). Their session model handles browser lifecycle automatically.

**If MCP isn't available**, use `npx playwright test` with an inline test file or `npx playwright codegen` for interactive walks. For each checklist item that needs UI inspection, write a focused short test (or use `page.evaluate` / `page.screenshot`).

Whatever you use, **always capture a screenshot for the "core surface" items** (marked ⭐ below) and on every failure.

---

## Checklist

Use this format when reporting:
```
[x] Item description — passed
[ ] Item description — FAIL: <what broke> (screenshot: path)
[~] Item description — SKIPPED: <why> (e.g. local DB empty)
```

### Setup verification

- [ ] API health endpoint returns 200 (`curl http://localhost:8000/health`)
- [ ] Web dev server responds at `http://localhost:3000`
- [ ] Browser console has no red errors on initial load
- [ ] No 4xx/5xx network failures in the initial page load (open DevTools network panel)

### Navigation smoke (every top-level route loads)

For each route, navigate, wait for content, verify no error boundary, screenshot the first paint:

- [ ] `/` (home / dashboard) — loads, no crash ⭐
- [ ] `/watchlist` (or whatever the recommendations list route is — discover from nav) — loads, lists at least one item if DB is seeded
- [ ] `/research/stats` (or wherever the Stats Assistant lives — look for `stats-workspace.tsx` import sites) — loads ⭐
- [ ] `/markets` (or equivalent) — loads
- [ ] `/events` (cosmic-reskin landing page from PR 6/7) — loads, no console errors
- [ ] Settings / Operator pages if they exist — load without 500
- [ ] Switching between routes via nav links does not throw any console error

### PR 1-2 — Advanced data ingestion sanity (foundation)

These cache reads back the whole stack. If any of these endpoints 500, the rest of the touch test is moot.

- [ ] API `/health` returns 200 (already in setup) — confirms DB connection
- [ ] API endpoint that returns operator settings or status (look for routes in `apps/api/app/api/routes.py`) responds 200 — confirms scheduler + refresh job state
- [ ] At least ONE recommendation in the DB has `features.advanced_data_complete == 1.0` OR `features.mlb_batter_data_complete == 1.0` (query DB directly: `select features from predictions where features::text like '%advanced_data_complete%' limit 5;`). If zero — note as setup limitation; advanced data hasn't been ingested locally
- [ ] DB has at least one row in `nba_advanced_gamelog_cache` for the current season (`select count(*) from nba_advanced_gamelog_cache where season=2026;`)
- [ ] DB has at least one row in `nba_league_percentiles_cache` for the current season — needed for PR 3c percentile bars to populate
- [ ] DB has at least one row in `mlb_batter_advanced_cache` for the current season
- [ ] DB has at least one row in `nba_team_gamelog_cache` (PR 2a) — opponent recent-form features depend on this
- [ ] (PR 2c) DB has at least one row in `nba_hustle_player_cache` OR `nba_tracking_cache` OR `nba_clutch_player_cache` — long-tail features ingested

**If the local DB is mostly empty:** document that as a setup limitation. The frontend touch tests below will SKIP data-dependent items rather than fail. Don't burn time trying to backfill — just note it in the report.

### PR 4 — Frontend component baseline (pre-PR-3 components render)

These components shipped with PR 4. PR 3b/3c change the data they consume but the components themselves should render even with the legacy data shape.

- [ ] `AdvancedMetricsGrid` is reachable — a Stats Assistant query for any NBA/MLB player produces the section header "Advanced" (`data-testid="sa-advanced-grid"`). Even with empty `metric_categories`, the component should render an empty state without crashing
- [ ] `WhyThisPrediction` panel is reachable — open ANY recommendation that has any `advanced_factors` in its features blob. Older predictions (pre-PR-3b) trigger the fallback derivation path. Confirm the panel renders with humanized labels (e.g. "Efficiency", "Opp Def") even without `_drivers`

### PR 5 — Game-winner market discovery

PR 5 added deep Kalshi standalone discovery for moneyline markets plus a cron job. User-visible: NBA/MLB winner markets show up in the watchlist.

- [ ] Watchlist (or recommendations list) includes at least one market with `market_kind == "winner"` or `market_family == "winner"` — confirm by clicking through and checking the URL/query/title. If only player-prop markets surface, PR 5 ingestion may be off — flag it
- [ ] At least one winner-market recommendation has `features.has_team_context == true` and `features.has_opponent_context == true` (PR 5's discovery should hydrate event context for these)
- [ ] (MLB-specific) at least one `first_five_winner` market exists if the MLB season is active

### PR 3b — Driver attribution UI ⭐

Find a **player-prop recommendation** with advanced data. If none exist in the local DB, note as a SKIP with reason. Otherwise:

- [ ] Click a player-prop recommendation to open its detail sheet (or navigate directly if there's a per-market URL)
- [ ] Locate the **"Why this prediction?"** panel (`data-testid="why-this-prediction"`)
- [ ] Confirm at least one driver row renders (`data-testid="why-driver-<key>"`)
- [ ] For each driver row, verify:
  - [ ] Direction arrow present (↑ for boost, ↓ for suppress)
  - [ ] Label is human-readable (e.g. "Quality of contact", NOT raw "quality_of_contact_factor")
  - [ ] Delta percentage shown (e.g. "+12.0%")
  - [ ] Color matches direction (green for ↑, red/rose for ↓)
- [ ] If any driver has a `detail` string, it renders below the row in smaller muted text (`data-testid="why-driver-<key>-detail"`) — examples: "Recent TS% 66.0% vs season 60.0%", "Season barrel rate: 14.0%"
- [ ] Maximum 3 driver rows visible (the `slice(0, 3)` cap)
- [ ] Open a recommendation that does NOT have advanced factors — panel should NOT render at all (returns `null`)
- [ ] (If you can find one) older prediction with `advanced_factors` but no `_drivers` field — should render the fallback derivation (humanized labels, no detail rows)
- [ ] No console errors with the panel open or while scrolling
- [ ] Screenshot the panel ⭐

### PR 3c — Stats Assistant advanced metrics + percentiles ⭐

The Stats Assistant takes a natural-language question. The UI is in `apps/web/components/stats/stats-workspace.tsx`. The component name is `StatsAnswer` for the result rendering.

For NBA:
- [ ] Submit a query like "Jalen Brunson stats this season" or "Jalen Brunson last 10 games"
- [ ] Verify the basic metric grid renders (`data-testid="sa-metric-points"`, etc.) for points/rebounds/assists/etc.
- [ ] Verify the **Advanced** section renders below the basic grid (`data-testid="sa-advanced-grid"`)
- [ ] At least one advanced row renders (`data-testid="sa-advanced-ts_pct"`, `sa-advanced-usg_pct`, etc.)
- [ ] Each advanced row shows: label (e.g. "TS%"), value, and a **percentile bar** with a number 0-100
- [ ] Bar color: red < 33, neutral 33-66, green > 66 — pick a row and confirm the color matches the percentile shown
- [ ] If the player has `def_rating` data, confirm its percentile is **inverted** (lower DRtg → higher percentile bar)
- [ ] Screenshot the advanced grid ⭐

For MLB:
- [ ] Submit a query like "Bryce Harper this season" (or any active MLB hitter)
- [ ] Verify basic metric grid renders (hits, AVG, OPS, etc.)
- [ ] Verify the Advanced section renders with batter sabermetrics + Statcast (xBA, xwOBA, barrel_rate, hard_hit_rate, woba, iso, etc.)
- [ ] **Expected gap**: percentile bars likely show "—" because the MLB league percentiles writer doesn't ship until P0 follow-up. Note this as ✅ expected behavior, not a failure
- [ ] If `strikeout_rate` is in the row set, the percentile (when populated) should be inverted (high K% = low percentile)
- [ ] Screenshot the MLB advanced grid ⭐

For unsupported sports (Soccer/Tennis/UFC):
- [ ] Submit a query for one (e.g. "Lionel Messi this season")
- [ ] Verify it still works — basic metrics render, no Advanced section, no console errors
- [ ] Confirm the response contains `metric_categories: {}` and `percentiles: {}` (check via DevTools network tab or `page.evaluate` on the fetch response)

### PR 3a — Indirect scoring verification

PR 3a's only user-visible effect is potentially-different prediction numbers. Hard to verify directly without a known baseline, but smoke:

- [ ] Open a player-prop recommendation, look at the rationale strings — they should NOT contain raw factor names like "efficiency factor: 1.05x" any more (those were replaced by the driver_reason_strings format like "Shooting efficiency +5.0%: Recent TS% ..."). Older predictions might still have the old format — note which version you saw.
- [ ] No recommendations show NaN, Infinity, or "undefined" anywhere in the prediction values
- [ ] Confidence values still in [0, 1] range
- [ ] Edge values still finite

### Network contract checks

For the same player-prop detail page used above, capture the API response and verify the contract at every layer:

**Foundation (PR 1-2):**
- [ ] For an NBA player-prop, `features.advanced_data_complete == 1.0` (PR 1 emitter fired)
- [ ] If the player has cached advanced data, features include some subset of: `recent_usage_pct`, `season_usage_pct`, `recent_true_shooting_pct`, `season_offensive_rating`, `season_defensive_rating`, `season_pace`, `season_pie`
- [ ] For an NBA prop with opponent team data cached: `features.opponent_team_data_complete == 1.0` and at least one of `opponent_pace_recent_5`, `opponent_def_rating_recent_5`, `opponent_pace_season` is present (PR 2a)
- [ ] (PR 2c) If the long-tail caches are populated for the player: at least one of `hustle_data_complete`, `drives_data_complete`, `clutch_data_complete`, `opponent_defender_data_complete` is `1.0`
- [ ] For an MLB batter prop: `features.mlb_batter_data_complete == 1.0` if cached; values like `season_woba`, `season_xba`, `season_barrel_rate`, `season_hard_hit_rate` populated (PR 2b)
- [ ] For an MLB prop with opposing-pitcher data: `features.pitcher_data_complete == 1.0`, plus `opposing_starter_xfip` / `opposing_starter_fip` / `opposing_starter_csw_pct` (PR 2b)
- [ ] For MLB outdoor games: `features.weather_data_complete == 1.0` plus temp/wind keys (PR 2b)
- [ ] For MLB games with a known venue: `features.park_data_complete == 1.0` plus `park_factor_hr`, `park_factor_runs` (PR 2b)

**PR 3 deltas:**
- [ ] Network response for the prediction includes `features._drivers` as an array (when advanced_factors fired)
- [ ] Each `_drivers` entry has `key`, `label`, `delta_pct` (number), `direction` (string), `detail` (string or null)
- [ ] For NBA props with USG% cached, `features.usage_factor_proxy_superseded == true` AND `features.usage_factor == 1.0` (PR 3a gate fired). If the prop's stat_key is `rebounds` (where the advanced replacement isn't wired), the supersede flag must be ABSENT and `usage_factor` must NOT be pinned to 1.0
- [ ] For Stats Assistant queries on NBA/MLB, response includes `summary.percentiles` (object) and `summary.metric_categories` (object)
- [ ] `summary.metric_categories` values are exactly `"basic"` or `"advanced"` (no other strings)
- [ ] Stats Assistant response for an NBA player includes advanced metric keys (`ts_pct`, `usg_pct`, etc.) in `summary.metrics` AND in `summary.metric_categories` tagged `"advanced"`

### Cross-cutting smoke

- [ ] Dark mode toggle (if present) — switch and verify the new advanced grids + driver panels still readable
- [ ] Responsive: resize viewport to ~375px wide. Stats grid + driver panel should still render reasonably (no horizontal overflow, no clipped numbers)
- [ ] Accessibility: percentile bars have `role="progressbar"` with `aria-valuenow`/`aria-valuemin`/`aria-valuemax` set (already in code at `advanced-metrics-grid.tsx:88-94`) — confirm present in DOM
- [ ] Reload any page mid-session — no hydration errors in console

### Performance / regression

- [ ] First contentful paint on the Stats Assistant after a query — should be < 3s on a warm cache
- [ ] No memory leaks visible after navigating between 5+ different recommendations (DevTools memory tab, or just observe the tab stays responsive)
- [ ] Network tab: no requests with `pending` status hanging > 30s

---

## Reporting

At the end, produce:

1. **Pass/fail summary** — count of `[x]` vs `[ ] FAIL` vs `[~] SKIP`
2. **Critical failures** — any item that breaks core flows (red ⭐ items)
3. **Notable observations** — UX issues that aren't checklist failures but worth flagging (slow renders, ugly fallback states, missing data)
4. **Setup limitations encountered** — empty DB, missing seed data, etc., that prevented some checks
5. **Screenshots** — list of paths, organized by section
6. **Recommendation**: SHIP / SHIP WITH CAVEATS / FIX REQUIRED

If you find a bug, capture:
- Screenshot
- Console error (if any)
- Network response (if relevant)
- Steps to reproduce
- Suspected root cause (code path / file)

## Bonus

If you have time, write a single Playwright spec file at `apps/web/tests/e2e/pr3-touch-test.spec.ts` that automates the highest-value checks (driver panel renders, stats advanced grid renders for NBA, response shape contracts). Future regressions then become CI failures instead of manual rediscoveries.

## Notes on the dev environment

- The local DB may not have realistic data. If the watchlist is empty, you can seed by running the API's refresh job, but that requires Kalshi credentials. If those aren't set up, document the gap and skip data-dependent checks rather than failing them.
- The frontend talks to the API at `http://localhost:8000` by default (or whatever `NEXT_PUBLIC_API_BASE_URL` is set to). Confirm the wire-up before reporting "stats query returns nothing" as a regression.
- Some advanced caches won't be populated unless the daily refresh job has run. Missing percentile bars on MLB are EXPECTED (P0 follow-up); missing on NBA suggests the NBA league percentiles cache row needs to be backfilled — check `select * from nba_league_percentiles_cache where season=2026;` (or whatever the current season is — `default_season_for_sport` in `apps/api/app/services/stats_query.py`).

Good luck.
