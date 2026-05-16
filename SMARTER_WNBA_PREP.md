# Smarter Sika — WNBA support prep

Comprehensive research + execution plan for adding WNBA as a first-class sport alongside NBA + MLB.

**Source research date:** 2026-05-16. WNBA season started 2026-05-08 (8 days into a 140-day regular season as of doc write).

---

## TL;DR

- **Effort: 5-7 PRs (~3-5 sessions).** Most of the work is config additions + new entries in allowlist maps — the existing infrastructure is sport-agnostic.
- **ESPN is a drop-in.** Every NBA endpoint works with `/wnba/` swapped in; payload schemas are identical.
- **Kalshi WNBA per-game player props are NOT yet broadly live** (mid-May 2026). Only milestones (`kxwnba40pts`) + futures (`kxwnbamvp`, `kxwnbaseries`) confirmed. **Verify per-game prop coverage at season tip-off before promising day-1 parity.**
- **Three real risks** worth designing around upfront: (1) WNBA doesn't mandate pre-tip lineup submission (RotoWire often confirms post-tipoff), (2) overseas-play data gap leaves ~6mo of recent player form invisible to WNBA stats sources, (3) name collisions within WNBA (4 players named "Kelsey") — sika's `(sport_key, query_normalized)` cache key partitioning handles cross-league but team-hint disambiguation must work intra-WNBA (already fixed in PR #165, this generalizes for free).
- **Cold-start window:** May 8 → ~end of May 2026 will have elevated cache misses. Expansion teams (Toronto Tempo + Portland Fire) have zero prior data.
- **No external blockers** — every data source is reachable today.

---

## 1. Business context

### Season + schedule
- **2026 regular season:** May 8 → Sept 24 (140 days).
- **Playoffs:** start Sept 27 (best-of-3 → best-of-5 → best-of-7 Finals).
- **Two interruptions to plan for:**
  - **All-Star break:** Jul 24-25 (United Center, Chicago).
  - **FIBA Women's World Cup break:** ~2 weeks early Sept — WNBA's equivalent of an Olympic break. Models trained on continuous play will have a discontinuity here.
- **15 teams** (was 13 in 2025; Toronto Tempo + Portland Fire are 2026 expansion). Connecticut Sun is a lame-duck Uncasville season — Houston relocation begins 2027.
- **44 games per team × 15 = 330 games.** League record.
- **Game cadence:** every day of the week during peak (no Tue/Wed/Fri/Sat-only pattern). ION Friday doubleheaders + USA Wednesday primetime are the broadcast anchors.
- **Tip-off times:** mixed. 7-10pm ET weekday primetime; 1pm / 2pm / 4pm / 8pm ET weekend slots. **Cache-warming jobs must read schedule per-date, not assume "all games at 7pm ET".**

### Volume expectations
- **~3-5 games/day** in regular season vs NBA's ~5-12 in-season.
- **~150 player-prop markets/day peak** (rough order-of-magnitude): 12-player rosters × ~5 prop-relevant players × 5 prop types × 5 games.
- **~25-35% of NBA daily volume.**
- ~180 league-wide rotation players vs NBA's ~450.

### Star players driving prop volume
Top ~20 names cover ~80% of expected handle. Priority for cache warming:
- Caitlin Clark (IND) — by far #1 in betting handle league-wide
- A'ja Wilson (LV)
- Breanna Stewart (NY)
- Sabrina Ionescu (NY)
- Paige Bueckers (DAL)
- Angel Reese (CHI)
- Napheesa Collier (MIN)

### Urgency
**Season is happening NOW.** Every week without WNBA support is missed picks you can't backfill. NBA playoffs + WNBA peak + MLB peak overlap from now through October.

---

## 2. WNBA team list with ESPN abbreviations

Verified against `espn.com/wnba/teams`:

| Team | ESPN abbr | URL slug | Kalshi (inferred) |
|---|---|---|---|
| Atlanta Dream | ATL | atlanta-dream | `atl` |
| Chicago Sky | CHI | chicago-sky | `chi` |
| Connecticut Sun | CON | connecticut-sun | `con` |
| Indiana Fever | IND | indiana-fever | `ind` |
| New York Liberty | NY | new-york-liberty | `nyl` (likely padded) |
| Toronto Tempo | TOR | toronto-tempo | `tor` |
| Washington Mystics | WSH | washington-mystics | `wsh` |
| Dallas Wings | DAL | dallas-wings | `dal` |
| Golden State Valkyries | GS | golden-state-valkyries | `gsv` (likely padded) |
| Las Vegas Aces | LV | las-vegas-aces | `las` or `lvg` |
| Los Angeles Sparks | LA | los-angeles-sparks | `las` or `laa` |
| Minnesota Lynx | MIN | minnesota-lynx | `min` |
| Phoenix Mercury | PHX | phoenix-mercury | `phx` |
| Portland Fire | POR | portland-fire | `por` |
| Seattle Storm | SEA | seattle-storm | `sea` |

**Critical gotcha:** ESPN uses 2-letter codes for `NY`, `LV`, `LA`, `GS`. Kalshi historically pads to 3 letters with team-distinctive suffix. **Must verify against actual Kalshi WNBA game tickers once live** — don't ship a hardcoded mapping based on guesses for these four. Mitigation in code: the existing `_team_hint_matches_subtitle` helper does substring + abbreviation lookup, so even with imperfect mapping the existing resilience kicks in.

---

## 3. Data sources

### ESPN — drop-in copy
**All endpoints work by swapping `/nba/` → `/wnba/`. Schemas verified identical to NBA.**

| Endpoint | URL pattern |
|---|---|
| Scoreboard | `https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard` |
| Player gamelog | `https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba/athletes/{athleteId}/gamelog` |
| Player search | `https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/...` |
| Teams | `https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams[/:team]` |
| News | `https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/news` |
| Injuries | `https://www.espn.com/wnba/injuries` (scraped page, same as NBA pattern) |
| Team schedule | `https://site.web.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule` |

The `wehoop` R package implements the same endpoints; cross-reference for any gotchas.

### Kalshi WNBA — partial coverage
**Ticker prefix: `KXWNBA`** (confirmed in observed URLs).

Confirmed live market families (mid-May 2026):
- `kxwnba40pts` — milestone scoring (e.g. `kxwnba40pts-26cclark22` = season 2026, Caitlin Clark)
- `kxwnbaseries` — series winner (playoffs)
- `kxwnbamvp` — season MVP futures

**NOT yet broadly live:** per-game player props for points/rebounds/assists/threes. Kalshi has signaled WNBA parity with the NBA prop framework that launched late 2025, but observable market coverage as of season day 8 is thin. **Verification step: hit Kalshi's events API directly at first WNBA slate refresh** to see what families ship live. Sika can pre-ship the infrastructure on the assumption per-game props will land mid-season.

Ticker encoding pattern (inferred from `kxnbagame-26apr17gswphx`):
- Games: `kx{league}{family}-{yy}{mmm}{dd}{away}{home}`
- Player milestones: `kx{league}{family}-{yy}c{player_initial+name}{number}`
- The `26` prefix is season year

Per-trade cap: $10K on basketball props (matches NBA). Settlement: same-day after final whistle.

### basketball-reference WNBA
URL pattern verified:
- Player page: `https://www.basketball-reference.com/wnba/players/{first-letter}/{playerid}w.html` — **note trailing `w`** on every WNBA player ID (`clarkca02w`, `hammobe01w`).
- Regular-season gamelog: `/wnba/players/c/clarkca02w/gamelog/`
- Playoffs gamelog: `/wnba/players/c/clarkca02w/gamelog-playoffs/`

Same rate-limit + cache-TTL considerations as BR NBA (confirmed 403s/timeouts during research).

### stats.wnba.com
Mirrors stats.nba.com structure. Categories: Player (Traditional, Advanced, Misc, Scoring, Usage, Shooting, Career), Team (Traditional, Advanced, Four Factors, Misc, Opponent, Shooting, Lineups, On/Off Court), Clutch, shot charts.

**No documented public API.** Same unofficial JSON endpoints as stats.nba.com, requires NBA-style headers (`User-Agent`, `Referer`, `x-nba-stats-*`) or returns 403. **If sika has a stats.nba.com client, generalize it; otherwise treat as a future enhancement.**

### RefMetrics — only viable WNBA referee source
ESPN/BR/WNBA do **not** publish ref data. Scrape `refmetrics.com/wnba/referee-assignments-today` for daily assignments + `refmetrics.com/wnba/foul-leaders` for tendency stats. **Thin scraper, same caching pattern as NBA refs.** Pre-empt the basketball-reference 403 problem (which blocks Smarter #13 phase 2b-2) by validating RefMetrics works from your environment first.

### The Odds API — WNBA supported
- Sport key: `basketball_wnba`
- Confirmed markets: `h2h`, `spreads`, `totals`, `player_points`, `player_rebounds` (others — `player_assists`, `player_threes`, `_alternate` lines — supported but not enumerated on the docs page)
- Free tier: current odds + scores
- Historical odds: paid plan (back to May 2022 for featured, May 2023 for player props)

### Other notable sources
- **Her Hoop Stats** (`herhoopstats.com`) — best dedicated WNBA stats site; per-100, on/off, lineup data with cleaner taxonomy than BR. Paid tier for API access.
- **wehoop R package** — open-source ESPN WNBA wrapper; useful as reference even if sika doesn't depend on R.
- **DraftKings inline odds** in ESPN scoreboard JSON — same pattern as NBA, free entry point.

### Lineup confirmations — major operational gotcha
**WNBA does NOT mandate pre-tip lineup submission.** RotoWire (`rotowire.com/wnba/lineups.php`) is the standard source but **expect lower confirmation rates than NBA — starters sometimes confirmed only at tipoff.**

Sika's `copilot_requires_lineup` flag works the same way for WNBA, but the operator should expect more `pending_lineup` recommendations near tip-off than they see for NBA. The Smarter #16 "suppress, don't penalize" policy works correctly here — the suppression simply fires more often.

---

## 4. Code touchpoint inventory

Based on parallel deep searches of `apps/api`, `apps/ml`, `apps/web`, `packages/contracts`, and `packages/ml-features`.

### apps/api (53 touchpoints)

**High-reuse (just add "WNBA" to a set / dict / string) — 34:**

| File | Detail |
|---|---|
| `app/config.py:63` | `parlay_enabled_sports` — add `"WNBA"` |
| `app/config.py:86` | `enabled_sports` — add `"WNBA"` |
| `app/config.py:49, 106-117, 154` | TTL settings: `wnba_prop_gamelog_cache_minutes`, `wnba_advanced_cache_minutes`, `wnba_team_advanced_cache_minutes`, `wnba_referee_assignments_cache_minutes`. Mirror NBA defaults (30 min, 240 min) |
| `app/services/market_support.py:19-34` | `STAT_KEY_PROPS_BY_SPORT`, `ALLOWED_PROP_STATS`, `ALLOWED_GAME_LINES`, `BLOCKED_COMBO_LEG_FAMILY_PREFIXES` — add WNBA rows mirroring NBA |
| `app/services/market_support.py:174, 341, 388` | `_player_prop_metadata`, `_game_line_metadata`, `_combo_leg_family_code` sport allowlist sets — add `"WNBA"` |
| `app/clients/espn.py:11, 20, 24, 29, 106` | `ESPN_SEARCH_SLUGS`, `ESPN_SCOREBOARD_URLS`, `ESPN_GAMELOG_URLS`, `ESPN_TEAM_SCHEDULE_URLS`, `ESPN_LEAGUE_NAMES` — add WNBA entries with `/wnba/` URLs |
| `app/services/refresh_jobs.py:956` | `refresh_sports_data` sports list — add `"WNBA"` |
| `app/services/refresh_jobs.py:61-62, 300-302` | `WNBA_INJURY_REFRESH_WORKER_TIMEOUT_SECONDS`, dispatch branches |
| `app/services/scoring/__init__.py` | Home-advantage sport allowlist — add `"WNBA"` |
| `app/api/routes.py:207, 214, 218` | `KALSHI_MARKET_URLS`, `KALSHI_EVENT_SERIES`, `KALSHI_PROP_CATEGORY_SLUGS` — add WNBA URLs (verify with Kalshi) |
| `app/services/trade_desk.py:32, 39, 43` | Duplicate of routes.py Kalshi constants (Bug #30 design smell — consider unifying as part of this work) |
| `app/clients/the_odds_api.py:31` | `SPORT_KEY_TO_ODDS_API_SPORT` — add `"WNBA": "basketball_wnba"` |
| `app/sports/registry.py:9` | `ADAPTERS` dict — add `"WNBA": TeamSportAdapter("WNBA", "Basketball")` |

**Medium-reuse (copy NBA pattern with small changes) — 15:**

| File | Detail |
|---|---|
| `app/services/stats_query.py:764-773` | `_build_game_logs` dispatch — `if sport_key == "WNBA": return _build_nba_game_logs(payload)` (WNBA ESPN payload shape matches NBA — full reuse of `_build_nba_game_logs`) |
| `app/services/stats_query.py:566-617, 628-641` | `parse_stats_question` sport validation, `default_season_for_sport` season rollover (WNBA: calendar year, May-Sept) |
| `app/services/scoring/__init__.py` | `_compute_team_strength`, `_player_role_stable` — add WNBA branches mirroring NBA logic |
| `app/services/scoring/resolver.py` | Add `wnba_props` + `wnba_singles` heuristic profiles to `SINGLE_HEURISTIC_PROFILES` (likely mirror NBA values; refine after backtest) |
| `app/services/market_support.py:51` | `WNBA_PROP_ALIASES` dict — copy `NBA_PROP_ALIASES` (likely identical) |
| `app/services/market_support.py:94, 407` | `_player_prop_metadata` + `_player_prop_aliases` — add WNBA branch pointing to `WNBA_PROP_ALIASES` (could also reuse `NBA_PROP_ALIASES` since stats are the same) |
| `app/models.py:471-773` | Parallel WNBA cache tables: `WnbaAdvancedGamelogCache`, `WnbaInjuryReportCache`, `WnbaTeamAdvancedCache`. **Decision point:** unify with NBA tables (add `sport_key` column) OR keep separate. Recommend separate for minimal blast radius. |
| `app/services/refresh_jobs.py:1131` | `EspnPlayerSearchCache` warming loop — add WNBA parallel loop |
| `app/clients/espn.py:128` | `fetch_nba_injury_report` — generalize to `fetch_injury_report(sport_key="NBA")` OR create `fetch_wnba_injury_report` mirror. Generalize is cleaner. |
| `app/services/model_families.py:35-65, 185-209` | `FAMILY_DEFINITIONS` — add `wnba_singles`, `wnba_props`, optional `wnba_parlay_*`. Mirror NBA structure. |
| `app/api/routes.py:755` | Parlay sport_scope validation error message — include WNBA |

**Low-reuse (genuinely sport-specific or skipped) — 4:**

| File | Detail |
|---|---|
| Long-tail features (`nba_long_tail.py`) | Hustle, drives, clutch, opponent-defender — **SKIP for WNBA** (stats.nba.com-style endpoints exist at stats.wnba.com but require separate client generalization; defer to phase 2) |
| NBA referee tendencies (Smarter #13) | RefMetrics is the only WNBA source. **Separate scraper PR**, not part of MVP. |
| Advanced stats (`advanced_stats.py`) | NBA Stats client is NBA-only. **Either generalize the client or skip WNBA advanced stats initially.** Most factor surfaces (workload, injuries, lineups) work without it. |
| NBA Statcast (`nba_*.py`) | NBA-Stats specific. Skip. |

### apps/ml (12 touchpoints)

**High-reuse — 8:**
- `ml/dataset.py:24-36` — `_family_key` sport switch: add WNBA branch (`wnba_props`, `wnba_singles`)
- `ml/dataset.py:67-68` — `_enrich_prediction_features` one-hot: add `sport_is_wnba`
- `ml/dataset.py:100` — `_prepare_frame` sport allowlist filter: expand to `{"NBA", "MLB", "WNBA"}`
- `ml/cli.py:32` — `_DEFAULT_SERVE_FAMILY_KEYS`: add `wnba_props,wnba_singles`
- `ml/cli.py:136-151` — `_family_key_for_row` mirror of `dataset._family_key`
- `ml/interval_dataset.py:672` — `_parse_gamelog_entries` sport allowlist: add `"WNBA"`
- `ml/training.py` — generic over family keys; no explicit changes

**Medium-reuse — 3:**
- `ml/interval_dataset.py:102-121` — `_WNBA_STAT_TO_RAW` dict; likely identical to `_NBA_STAT_TO_RAW`
- `ml/interval_dataset.py:885-912` — `_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME` — add `"WNBA"` entry with all 15 teams (drift guard in PR #165 will catch divergence from apps/api copy)
- `ml/interval_dataset.py:692-695` — `_nba_raw_metrics_from_stat_map` selector — add WNBA branch (likely reuses NBA parser)

**Low-reuse — 1:**
- Test fixtures — add `_player_search_row("WNBA", "Caitlin Clark", ...)` patterns to existing test files. ~5-10 new test cases.

### apps/web (10 touchpoints)

**High-reuse — 7:**
- `lib/types.ts:5` — `SportKey` literal union: add `| "WNBA"`
- `lib/types.ts:7-13` — `SPORT_LABELS`: add `WNBA: "WNBA"`
- `lib/utils.ts:85-91` — `SPORT_OPTIONS` array: add WNBA entry with color class `text-sport-wnba`
- `lib/sport-tints.ts:9-16` — `SPORT_TINTS`: add `wnba: "var(--sport-wnba)"`. **Need to define `--sport-wnba` CSS variable in Tailwind config.**
- `components/events/events-feed.tsx`, `components/trade/trade-desk.tsx`, `components/markets/market-detail-sheet.tsx` — generic over sport; pick up WNBA automatically once types update
- `components/stats/stats-workspace.tsx` — dynamic from `SPORT_LABELS`; WNBA appears in dropdown automatically

**Low-reuse — 3:**
- `lib/health-status.ts:187-188` — hardcoded "NBA/MLB slate" text in operator banner — update copy or refactor to dynamic
- Web tests — add WNBA fixture variants
- Tailwind config — define `--sport-wnba` color token

### packages/contracts + packages/ml-features

- **packages/contracts** — auto-regenerates from FastAPI OpenAPI after API schema update. Run `npm run contracts:generate`.
- **packages/ml-features** — `family_one_hot_keys` is dynamic; `MONOTONIC_CONSTRAINTS_BY_FAMILY` is empty by design. No changes needed for MVP.

### Total touchpoint count

| Layer | Count | High-reuse | Medium | Low |
|---|---|---|---|---|
| apps/api | 53 | 34 | 15 | 4 |
| apps/ml | 12 | 8 | 3 | 1 |
| apps/web | 10 | 7 | 0 | 3 |
| packages | 2 | 2 (auto) | 0 | 0 |
| **Total** | **77** | **51** | **18** | **8** |

**66% of touchpoints are just string additions.** The remaining 26% (medium + low) is the actual engineering.

---

## 5. Open design decisions

### D1 — Cache table topology
**Decision:** parallel WNBA cache tables (`WnbaAdvancedGamelogCache`, `WnbaInjuryReportCache`) or unified table with `sport_key` discriminator?

**Recommend:** **parallel tables.** Mirrors the existing NBA pattern (no migration of existing data, smaller blast radius, separate refresh cadences). The duplication cost is ~6 model definitions, each ~10 lines. The unification refactor can come later as a Smarter R-track item if it ever becomes painful.

### D2 — Advanced-stats client generalization
**Decision:** generalize `NbaStatsClient` + `BasketballReferenceClient` to take `sport_key`, OR skip WNBA advanced stats entirely for MVP?

**Recommend:** **skip for MVP.** Most factor surfaces (workload, injuries, lineups, refs) deliver signal without advanced stats. Generalizing the NBA Stats client requires header handling for `stats.wnba.com` and per-method URL parameterization — that's a 2-3 day side-quest. Ship WNBA without advanced stats, see how many props clear `quality_tier`, then promote advanced-stats generalization based on real evidence rather than premature optimization.

### D3 — Prop categories — Tier 1 only or include steals/blocks?
**Recommend:** **ship Tier 1 first (points, rebounds, assists, made_threes, PRA combos). Add steals + blocks as a Tier-2 follow-up.**

Steals on WNBA are higher-volume than NBA (slower pace, smaller rosters → more minutes for starters → more chances). The Odds API supports `player_steals` for WNBA. Worth a separate PR once Tier 1 is proven.

### D4 — Heuristic profile values
**Recommend:** **start with NBA values for `wnba_props` + `wnba_singles`** profiles in `SINGLE_HEURISTIC_PROFILES`. Refine after backtest (Smarter #2) once ~4 weeks of WNBA settled predictions exist.

### D5 — Manifest topology
**Recommend:** add `wnba_props` + `wnba_singles` to `_DEFAULT_SERVE_FAMILY_KEYS` so the weekly retrain workflow picks them up automatically. Initial WNBA artifacts will have insufficient_history for ~3-4 weeks; the `serving_mode="shadow"` gate handles this gracefully (no consumer change required).

### D6 — Lineup gate
**Recommend:** **same Smarter #16 "suppress, don't penalize" policy** as NBA. WNBA's looser lineup-confirmation cadence means the suppression fires more often near tip-off — operators will see more `pending_lineup` rows, which is correct behavior. Document this in the readiness panel.

### D7 — Toronto Tempo + Portland Fire cold start
**Recommend:** flag both teams in the heuristic profile (e.g. `team_history_insufficient: True`) and apply a small confidence penalty on ALL recommendations involving them for the first ~15 games. Re-evaluate mid-June.

---

## 6. Recommended PR sequence

Each PR is self-contained, TDD-ordered, and codex-reviewable.

### PR 1 — Sport scaffolding (~half day)
**Scope:**
- Add `"WNBA"` to every sport allowlist (config.py, market_support.py allowlists, scoring sport-key gates).
- Add WNBA team abbreviation maps to `apps/api/app/clients/espn.py:ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME` AND `apps/ml/ml/interval_dataset.py:_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME`. PR #165's drift guard test will fail until both are in sync.
- Add ESPN URL constants for WNBA (scoreboard, gamelog, schedule, league name, search slug).
- Add WNBA TTL settings to `Settings`.
- Add `"WNBA"` to the apps/web `SportKey` literal + `SPORT_LABELS` + `SPORT_OPTIONS` + `SPORT_TINTS` + Tailwind `--sport-wnba` token.
- Regenerate `packages/contracts/openapi.json` + `generated/api.d.ts`.

**Tests:**
- Drift guard between apps/api + apps/ml abbreviation maps (already exists from PR #165 — will auto-fail if maps disagree).
- Unit test: `default_season_for_sport("WNBA", date(2026, 7, 15))` returns 2026.

**Risk:** none. Pure config + allowlist additions. No behavior change for existing NBA/MLB flows.

### PR 2 — Market mapping (~half day)
**Scope:**
- `WNBA_PROP_ALIASES` dict mirroring `NBA_PROP_ALIASES` (or reuse — they're likely byte-identical).
- `PROP_COMPONENT_ORDER["WNBA"]` mirroring NBA's order (points→rebounds→assists→made_threes→steals→blocks→turnovers).
- WNBA prop title regex (same as NBA pattern; verify Kalshi WNBA props use same English phrasing).
- WNBA game-line title regex.
- `_combo_leg_family_code` returns `"KXWNBA"` prefix.

**Tests:**
- Title parsing for sample Kalshi WNBA market titles ("Caitlin Clark: 20+ points").
- Component-stat-key resolution for WNBA combos.

**Risk:** low. Verifying Kalshi market titles against the regex may surface phrasing differences.

### PR 3 — Gamelog parsing + stats query (~half day)
**Scope:**
- `_build_game_logs` dispatches `"WNBA"` → `_build_nba_game_logs` (verified payload shape matches).
- `_WNBA_STAT_TO_RAW` mapping in `apps/ml/ml/interval_dataset.py` (likely identical to NBA).
- `season_for_captured_at("WNBA", captured_at)` returns calendar year.
- `default_season_for_sport("WNBA", ref_date)` returns calendar year.
- `_parse_gamelog_entries` sport allowlist expanded.

**Tests:**
- Cached WNBA gamelog payload → expected stat extraction.
- Season rollover at year boundary (Jan 2027 captured_at for a Sept-Oct 2026 game).

**Risk:** low. The ESPN payload schema match is verified.

### PR 4 — Scoring kernel WNBA branch (~day)
**Scope:**
- Add `wnba_props` + `wnba_singles` to `SINGLE_HEURISTIC_PROFILES` (NBA values as starting point).
- Add WNBA arm to `_score_player_prop` (mostly delegates to existing NBA logic).
- Skip long-tail features for WNBA (data unavailable; documented).
- Add `wnba_props` + `wnba_singles` to `FAMILY_DEFINITIONS`.

**Tests:**
- End-to-end: WNBA player_prop market → recommendation with expected fields populated.
- Verify no long-tail features in WNBA prediction's `scoring_diagnostics`.

**Risk:** medium. Largest PR. Heuristic profile values may need tuning post-shadow; ship with NBA defaults.

### PR 5 — Training pipeline registration (~half day, mostly config)
**Scope:**
- Add `wnba_props,wnba_singles` to `_DEFAULT_SERVE_FAMILY_KEYS` in `apps/ml/ml/cli.py`.
- Manifest output includes WNBA families.
- Weekly retrain workflow (`.github/workflows/ml-retrain.yml`) picks them up automatically.

**Tests:**
- Manifest generation includes `wnba_props` + `wnba_singles` entries.
- Until WNBA settled rows exist, families report `insufficient_history` (already-shipped readiness panel pattern).

**Risk:** none. Existing training infra handles new families gracefully.

### PR 6 — Kalshi WNBA discovery + ingestion (~half day)
**Scope:**
- Kalshi WNBA category URL + event series in `routes.py:KALSHI_*` constants.
- Sport adapter registry (`app/sports/registry.py`) — add `"WNBA"`.
- `refresh_sports_data(["NBA", "MLB", "WNBA"])`.
- ESPN player search cache warming loop for WNBA.

**Tests:**
- Verify Kalshi WNBA category URL is reachable (live or VCR-recorded).
- Refresh job invocation picks up WNBA markets.

**Risk:** medium. **Kalshi WNBA per-game prop coverage is the open question** — if per-game props aren't live yet, the refresh job sees only milestones/futures, which is fine but limits day-1 prop coverage.

### PR 7 — WNBA injury endpoint (~half day, optional MVP+1)
**Scope:**
- Generalize `fetch_nba_injury_report` to `fetch_injury_report(sport_key)`.
- New `WnbaInjuryReportCache` model + loader.
- Wire into WNBA scoring path.

**Tests:**
- Cached WNBA injury payload → expected feature emission.

**Risk:** low. Mirrors Smarter #17 NBA pattern.

### PR 8 — Operator UX polish (~half day, optional MVP+1)
**Scope:**
- `apps/web/lib/health-status.ts` banner copy: "NBA/MLB/WNBA slate refresh".
- Readiness panel "Prediction Intervals" tile now includes `wnba_props` rows (auto, no code change — the panel reads what's in the manifest).
- Add `wnba` to operator settings page visible-sports default if applicable.

**Tests:**
- Web fixture for WNBA readiness payload renders correctly.

**Risk:** none.

### Total estimated effort

- **Critical path (PRs 1-6):** ~3 sessions, 12-15 hours wall-clock.
- **MVP+1 (PRs 7-8):** +1 session.
- **Once 3-4 weeks of WNBA games settle:** `python -m ml.cli train-intervals --family-key wnba_props --stat-key points` produces real interval models with zero new code.

---

## 7. Risks + gotchas (worth designing around upfront)

1. **WNBA doesn't mandate pre-tip lineup submission.** RotoWire often confirms starters post-tipoff. The Smarter #16 "suppress, don't penalize" policy works correctly but fires more often. **Document in the operator readiness panel.**
2. **Overseas-play data gap.** Many WNBA players play EuroLeague / Korea / Australia during offseason. Their stats.wnba.com + BR pages won't reflect 6+ months of recent games. Features keyed on "games played in last N days" will undercount form. **Mitigation:** add a "cold-start indicator" feature for the first 4 weeks of season; suppress recommendations with low confidence early-season.
3. **Mid-season roster turnover (hardship + 7-day contracts).** WNBA rosters churn weekly. Cache invalidation on roster moves matters more than NBA.
4. **Intra-WNBA name collisions.** 4 players named "Kelsey", multiple "Erica"s, "Kayla"s. **Sika's existing `(sport_key, query_normalized)` cache partitioning handles cross-league fine, and PR #165's team-hint disambiguation handles intra-WNBA.** Already solved by recent work.
5. **FIBA Women's World Cup break early Sept 2026.** ~2-week competitive interruption. Models trained on continuous play will have a discontinuity. **Mitigation:** add a `post_international_break` feature flag; expect first 3-5 post-break games to have less reliable form features.
6. **Expansion teams (Toronto Tempo + Portland Fire) have zero prior team-level data.** Player features carry over via athlete_id; team-chemistry / lineup features don't. Apply a confidence penalty for first 15 games.
7. **ESPN 2-letter codes (NY, LV, LA, GS) vs Kalshi 3-letter conventions.** Abbreviation normalization layer needs explicit mapping, not regex/truncation. **The existing `_team_hint_matches_subtitle` substring fallback handles this, but verify against actual Kalshi WNBA tickers at season tip-off.**
8. **stats.wnba.com requires NBA-style headers.** If sika has a stats.nba.com client today, generalizing it should be straightforward; if WNBA advanced stats are scoped out for MVP (see D2), this is deferred.
9. **basketball-reference rate-limiting** is aggressive (confirmed 403s during research). Same exponential backoff + cache TTL as NBA path.
10. **Kalshi per-game player props for WNBA are not yet broadly live.** Sika should ship the infrastructure assuming they will be, but **verify at first WNBA slate refresh after PR 6 ships**.

---

## 8. Day-1 operator checklist

Once PRs 1-6 land:

1. **Verify Kalshi WNBA market discovery.** Hit `/ops/markets?sport_key=WNBA` and confirm the refresh job produced events. If only milestones/futures appear, document the per-game prop gap as a Kalshi-side limitation.
2. **Verify ESPN WNBA gamelog cache populates.** After 24-48h, run `python -m ml.cli inspect-intervals --manifest-path manifests/current.json --family-key wnba_props` — expect `no_gamelog` skip counts to drop as cache warms.
3. **Verify readiness panel "Prediction Intervals" tile shows WNBA families.** Will report `insufficient_samples` until ~30 settled WNBA predictions exist. That's expected and is the correct UX state.
4. **Check `/health` upstream sources** for any new WNBA-specific source (ESPN scoreboard records under existing `espn_scoreboard` key; no new source needed unless RefMetrics or stats.wnba.com lands).
5. **Eyeball first 10 WNBA recommendations** for sanity. Look for:
   - `expected_stat_output` in a plausible range (points: 5-30 typical; rebounds: 2-12).
   - `confidence` not stuck at floor (suggests features are populating).
   - `quality_tier` not always `low` (suggests heuristic factors are firing).
   - `scoring_diagnostics.recent_games` populated (suggests gamelog cache resolved).
6. **After ~3 weeks of WNBA games settle:** run `train-intervals` for `wnba_props/points` and check empirical coverage. Target 0.75-0.85 like NBA.

---

## 9. Items NOT in scope for MVP

- WNBA advanced stats (stats.wnba.com integration) — generalize NBA Stats client as Smarter follow-up
- WNBA referee tendencies (RefMetrics scraper) — separate PR, mirrors Smarter #13
- WNBA long-tail features (hustle, drives, clutch) — data unavailable from public endpoints
- WNBA parlay families (`wnba_parlay_2leg` etc.) — defer until single-prop coverage proves out
- Smarter #21 phase 2d consumer + UI band — handed off in #160, awaiting design pass on coverage gating (separate work, not WNBA-blocking)
- WNBA Tier 2 props (steals + blocks) — follow-up PR after Tier 1 proves
- Toronto Tempo + Portland Fire team-level advanced features — wait for ~15 games of data per team

---

## 10. Sources

- ESPN WNBA scoreboard API: https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard
- ESPN WNBA teams page: https://www.espn.com/wnba/teams
- Public ESPN API reference (GitHub): https://github.com/pseudo-r/Public-ESPN-API
- Kalshi WNBA milestone market example: https://kalshi.com/markets/kxwnba40pts/wnba-player-to-score-40-points/kxwnba40pts-26cclark22
- Kalshi WNBA MVP futures: https://kalshi.com/markets/kxwnbamvp/pro-womens-basketball-mvp
- The Odds API — WNBA: https://the-odds-api.com/sports/wnba-odds.html
- Basketball-Reference WNBA: https://www.basketball-reference.com/wnba/
- Caitlin Clark BR gamelog: https://www.basketball-reference.com/wnba/players/c/clarkca02w/gamelog-playoffs/
- stats.wnba.com: https://stats.wnba.com/
- RotoWire WNBA daily lineups: https://www.rotowire.com/wnba/lineups.php
- RefMetrics WNBA: https://www.refmetrics.com/wnba/referee-assignments-today
- ESPN WNBA injuries: https://www.espn.com/wnba/injuries
- WNBA 2026 schedule release: https://www.wnba.com/news/2026-schedule-release
- CBS Sports — WNBA 2026 44 games per team: https://www.cbssports.com/wnba/news/wnba-2026-schedule-release-30th-season-begin-may-8-44-games-per-team/
- 2026 WNBA season — Wikipedia: https://en.wikipedia.org/wiki/2026_WNBA_season
- wehoop sportsdataverse reference: https://wehoop.sportsdataverse.org/reference/index.html
