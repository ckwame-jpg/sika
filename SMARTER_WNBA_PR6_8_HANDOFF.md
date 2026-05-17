# Smarter WNBA — PR 6-8 handoff

**Session date written:** 2026-05-17 (afternoon, right after WNBA PR 5 merged).
**Author:** the previous Claude session, finishing 5 of 8 WNBA PRs (#177, #178, #181, #183, #184) + the Arch #5 stack (#169, #173, #175) before context ran out.

This doc + `SMARTER_WNBA_PROMPT_PR6.md` (spawn prompt) get a fresh Claude session from "merged PR 5" to "merged PR 8". Read the spawn prompt first, then this doc only if something is unclear.

---

## TL;DR

- **Critical path remaining:** PR 6 only (Kalshi WNBA discovery + sport adapter + flip `enabled_sports`). PRs 7 + 8 are MVP+1 — ship them if there's session budget, defer if not.
- **PR 6 was started but NOT pushed.** Two trivial edits on a local branch (`claude/smarter-wnba-pr-6-kalshi-adapter`) are explained in `## State of branches` below. The next session can either continue that branch or branch fresh — both work.
- **Verify Kalshi WNBA per-game prop coverage at first slate refresh.** `SMARTER_WNBA_PREP.md` §1 + §7 flag that per-game props weren't broadly live mid-May 2026; only milestones (`kxwnba40pts`) + futures (`kxwnbamvp`, `kxwnbaseries`) were confirmed. Without per-game props on Kalshi, PR 6 still ships value (the infrastructure is ready when Kalshi adds them) but the day-1 watchlist won't carry WNBA props yet.

---

## What's already shipped (don't redo)

| # | PR | Scope |
|---|---|---|
| 169 | Arch #5 | Feature freshness layer — feature_groups + per-group PENALIZE / SUPPRESS / IGNORE policy |
| 173 | Arch #5 follow-up 1 | `events_fresh_at` threaded once per batch (retired per-market `upstream_health` read) |
| 175 | Arch #5 follow-up 2 | Smarter #16 / #17 consolidated into SUPPRESS policy registry |
| 177 | WNBA PR 1 | Sport scaffolding — `SportKey` literal, ESPN URLs, team abbreviation map (15 teams), `--sport-wnba` CSS token, 4 cache TTL settings. WNBA deliberately kept OUT of default `enabled_sports` / `parlay_enabled_sports` until PR 6 lands the adapter. |
| 178 | WNBA PR 2 | Market mapping — `SUPPORTED_SPORT_HINTS` (WNBA above NBA — substring-precedence comment), `WNBA_PROP_ALIASES`, `PROP_COMPONENT_ORDER`, `KXWNBA` combo prefix. Defense-in-depth `enabled_sports` gate at `_persist_market_payload_records` (so the classifier change doesn't accidentally start persisting WNBA markets before PR 6). |
| 181 | WNBA PR 3 | Gamelog parsing + stats query — `_build_game_logs` dispatches WNBA → `_build_nba_game_logs(payload, sport_key="WNBA")`, `default_season_for_sport` with month-boundary rollover, `_METRIC_LABELS` / `_STAT_LINE_SPECS` mirror NBA, web `defaultSeasonForSport` mirrors backend rollover. apps/ml `_parse_gamelog_entries` allowlist + `_direct_lookup` refactor. Stats Assistant dropdown gate removed. |
| 183 | WNBA PR 4 | Scoring kernel + family registration — `_score_player_prop` WNBA branch emits `wnba_workload` (sport-agnostic gamelog reader, PENALIZE -3% / 24h). `SINGLE_HEURISTIC_PROFILES` + `FAMILY_DEFINITIONS` get `wnba_props` / `wnba_singles`. `single_family_key` dispatches WNBA. **HIGH catch from codex:** `_prop_value_from_raw` was widened from `{NBA}` → `{NBA, WNBA}` (otherwise made_threes / combo props would silently score expected=0). `_gamelog_ttl`, `home_advantage`, `walk_forward._build_single_predicate` all updated. |
| 184 | WNBA PR 5 | Training pipeline — `_DEFAULT_SERVE_FAMILY_KEYS` in `apps/ml/ml/cli.py` adds `wnba_props,wnba_singles`. Manifest auto-includes WNBA. |

**Baseline test counts after all 5 PRs:** apps/api 1666 · apps/ml 255 · apps/web 153 (1 pre-existing tsc error in `test/fixtures/trade-fixtures.ts` from sika#180, unrelated).

---

## State of branches

After PR 5 merged into main (sika#184 at commit `091e422`), I started PR 6 in a new branch:

```
claude/smarter-wnba-pr-6-kalshi-adapter  (off origin/main, 0 commits, 2 uncommitted edits)
```

The uncommitted edits:

1. **`apps/api/app/sports/registry.py`** — added `"WNBA": TeamSportAdapter("WNBA", "Basketball")` to `build_registry()`. Comment explains the dependency on PR 1's ESPN URL constants + team map.
2. **`apps/api/app/services/ingestion/__init__.py`** — added `"WNBA": "WNBA"` to `SPORT_LABELS` and `"WNBA"` to `PUBLIC_MAJOR_SPORTS`.

**Recommendation for the next session:** discard the in-flight edits and branch fresh off `origin/main`. The edits are 10 lines total — easier to re-apply with full focus than to inherit a half-baked branch. Run:

```bash
git checkout main
git branch -D claude/smarter-wnba-pr-6-kalshi-adapter
git fetch origin main
git checkout -b claude/smarter-wnba-pr-6-kalshi-adapter origin/main
```

---

## PR 6 — Kalshi WNBA discovery + sport adapter (~half day, MEDIUM risk)

This is the only critical-path PR left. After it lands, WNBA is fully wired (assuming Kalshi has WNBA props on the wire).

### Scope (in implementation order)

1. **`apps/api/app/sports/registry.py`** — register `"WNBA": TeamSportAdapter("WNBA", "Basketball")` in `build_registry()`. Uses ESPN's `/wnba/` URLs (PR 1) + the WNBA team map (PR 1).
2. **`apps/api/app/services/ingestion/__init__.py`** — add `"WNBA"` to `PUBLIC_MAJOR_SPORTS` (set) and `"WNBA": "WNBA"` to `SPORT_LABELS` (dict).
3. **`apps/api/app/api/routes.py`** — register WNBA Kalshi constants:
   - `KALSHI_SPORT_CATEGORY_ROOTS` — add `"WNBA": "https://kalshi.com/category/sports/basketball/pro-basketball-w"` (verify the actual slug on Kalshi).
   - `KALSHI_EVENT_SERIES` — add `"WNBA": ("kxwnbagame", "professional-basketball-game")`.
   - `KALSHI_PROP_CATEGORY_SLUGS` — add a `"WNBA"` block mirroring NBA's (`player-points`, `player-rebounds`, etc.). May need to verify per-stat slugs against actual Kalshi WNBA listings.
4. **`apps/api/app/services/trade_desk.py`** — there's a duplicate of the Kalshi constants from routes.py here (Bug #30 design smell, documented in prep doc §4). Update both sides — the duplication exists today; PR 6 isn't the right place to dedupe.
5. **`apps/api/app/clients/the_odds_api.py:31`** — add `"WNBA": "basketball_wnba"` to `SPORT_KEY_TO_ODDS_API_SPORT` (lets Smarter #18 sportsbook consensus pick up WNBA when an Odds API key is configured).
6. **`apps/api/app/services/refresh_jobs.py`** — find the `refresh_sports_data(["NBA", "MLB"])` call site (`grep refresh_sports_data` will show the entry) and add `"WNBA"`. Also check for any sport-keyed dispatches that gate on NBA/MLB.
7. **`apps/api/app/config.py`** — flip the defaults Chris asked me NOT to flip in PR 1:
   - `enabled_sports`: add `"WNBA"`.
   - `parlay_enabled_sports`: leave as `["NBA", "MLB"]` — WNBA parlays still fall into the mixed-family bucket because `parlay_family_key` doesn't have WNBA-specific families. Document this in the commit message; a separate WNBA-parlay-family PR can follow if Smarter #28 backtest data justifies one.
8. **Tests:**
   - Update `apps/api/tests/test_wnba_scaffolding.py::test_settings_default_enabled_sports_do_not_include_wnba_yet` → rename + invert the assertion (`assert "WNBA" in settings.enabled_sports`).
   - Update `apps/api/tests/test_market_filtering.py::test_persist_market_payload_records_skips_wnba_when_not_in_enabled_sports` — that test pins the safe default WAS off; once WNBA is enabled by default, the test needs to assert the OPPOSITE (with WNBA in enabled_sports, payloads persist). Alternative: leave the test in place by overriding `settings.enabled_sports` to `{NBA, MLB}` inside the test.
   - Add `test_sport_adapter_registry_includes_wnba` pinning `ADAPTERS["WNBA"]` exists and is a `TeamSportAdapter`.
   - Add a smoke test calling `refresh_sports_data` with `["WNBA"]` and asserting no `KeyError`.

### Risk: Kalshi per-game prop coverage

Per `SMARTER_WNBA_PREP.md` §1, as of mid-May 2026 Kalshi had only milestones (`kxwnba40pts`) + futures (`kxwnbamvp`, `kxwnbaseries`) live for WNBA — NOT per-game player props. The `KALSHI_EVENT_SERIES["WNBA"]` slug above is a guess based on the NBA pattern (`kxnbagame` → `kxwnbagame`). **At first slate refresh after PR 6 ships:**

- Hit Kalshi's events API for the `kxwnbagame` series and confirm it returns markets.
- If empty, look up the actual WNBA-game series slug from Kalshi's UI.
- If Kalshi still doesn't have per-game WNBA props in mid-May 2026 → mid-June 2026, PR 6 still ships value (the infrastructure is ready) but the day-1 watchlist won't carry WNBA props. Tell the operator; no code change needed.

### Codex review prompt for PR 6

Mirror the prompts used on PRs 2-5. Specifically flag:

- Pattern 1 (state-machine): flipping `enabled_sports` default means existing operators upgrading sika get WNBA automatically. Any persisted state that assumes only NBA/MLB/etc.? Specifically `current_slate_lookback_days` / `current_slate_lookahead_days` / `WATCHLIST_SCORE_BATCH_SIZE` — these apply globally.
- Pattern 2 (cross-component): the routes.py Kalshi constants are duplicated in trade_desk.py — confirm both sides are updated.
- Pattern 9 (cross-scope): any other `if sport_key in {NBA, MLB}` checks I missed? (PR 4 + PR 5 cleaned up several; one more sweep is cheap.)

---

## PR 7 (MVP+1) — WNBA injury endpoint (~half day, LOW risk)

Generalize Smarter #17's `fetch_nba_injury_report` to `fetch_injury_report(sport_key)` so WNBA gets the same OUT/DOUBTFUL suppression NBA does. ESPN's WNBA injury report is at `https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/news/injuries` (verify).

Touchpoints:

- `apps/api/app/clients/espn.py::fetch_nba_injury_report` → generalize.
- New `WnbaInjuryReportCache` model + loader (`apps/api/app/services/wnba_injury_report.py`, mirroring `nba_injury_report.py`).
- `_score_player_prop` WNBA branch (PR 4) — add a parallel `emit_to_group("wnba_injury", ...)` call.
- Smarter #17's bespoke gate in `_single_scoring_adjustments` checks `family_key == "nba_props"`; generalize to include `wnba_props` (or refactor the registered `nba_injury_suppress_when` callback into a sport-parametric form).
- Refresh job: add WNBA injury refresh entry mirroring the NBA one.

Tests: mirror `test_nba_injury_suppression.py` patterns. Add a `wnba_injury` SUPPRESS policy entry in `feature_groups.py`.

---

## PR 8 (MVP+1) — Operator UX polish (~half day, NONE risk)

- `apps/web/lib/health-status.ts:187-188` — hardcoded `"NBA/MLB slate"` text in operator banner. Update to `"NBA/MLB/WNBA slate"` or refactor to dynamic.
- Readiness panel's `interval_models` tile auto-picks up `wnba_props` rows from the manifest (PR 5 wired this). If the panel renders empty WNBA rows distinctly, verify the empty-state copy matches NBA's "insufficient_history" treatment.
- Operator settings page `visible_sports` default — confirm WNBA appears if applicable.
- Web fixture for WNBA readiness payload renders correctly.

---

## Workflow requirements (unchanged from PRs 1-5)

- **Worktree, not main.** Branch `claude/<topic>` off `origin/main` per PR.
- **One PR per scope.** Don't bundle. (PR 6 is the only multi-file PR remaining.)
- **TDD-ish ordering.** Run the failing test, watch it red, implement, watch it green.
- **9-point self-review.** Same list as PRs 2-5.
- **Codex review** via `codex exec --skip-git-repo-check --sandbox read-only "<focused prompt>"` (no `--model gpt-5-codex` — errors on ChatGPT accounts; default model works fine).
- **Frontend changes** use `/frontend-design` skill family.
- **Squash-merge with admin** — `gh pr merge <N> --squash --admin --body ""`.

---

## Baseline test counts (verify after each PR)

| Suite | Post-PR-5 baseline |
|---|---|
| apps/api | **1666** (4 skipped) |
| apps/ml | **255** |
| apps/web | **153** (1 pre-existing tsc error from sika#180's prediction-interval band fixture — unrelated; vitest itself green) |

Run before push:
- `cd apps/api && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `cd apps/ml && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `cd apps/web && npx tsc --noEmit && npx vitest run`

---

## Useful prior-art commit references

- **PR 4's HIGH catch** (codex caught `_prop_value_from_raw` not handling WNBA): the diff that fixed it is in commit `cd49fa9` on the merged PR 4 branch. The pattern (widen `{NBA}` → `{NBA, WNBA}`) recurs across the WNBA work; do another sweep during PR 6 review.
- **PR 2's substring-precedence comment** in `SUPPORTED_SPORT_HINTS` — load-bearing reminder for any future ticker-prefix work.
- **PR 4's `_player_prop_participation_gate`** is the canonical "WNBA shares NBA basketball semantics" pattern — minutes-based gates, FGA/usage proxies, etc.

---

## Cross-package drift guards (still in place)

- `_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME` — apps/api + apps/ml, validated by `test_team_abbreviation_map_matches_apps_api_canonical_source` (PR #165). PR 1 added WNBA to both copies; no action needed in PR 6 unless you add another team to the apps/api map.
- `INTERVAL_COVERAGE_*` — apps/api + apps/ml, not WNBA-relevant but worth knowing.

---

## What NOT to do

- **Do NOT add WNBA to `parlay_enabled_sports` in PR 6** — `parlay_family_key` has no WNBA-specific families. WNBA combos would silently land in `mixed_parlay_*` and pollute mixed-family calibration. Document in the PR description.
- **Do NOT change PR 1 / PR 4's NBA-defaults-on-WNBA pattern** without operator backtest data. The handoff says "NBA values as the starting point"; Smarter #28's tuning data is the right justification for divergence.
- **Do NOT skip the codex review for PR 6** even though PRs 1-5 caught the easy stuff. The Kalshi constants + enabled_sports flip are exactly the cross-component contract surface codex was best at on prior PRs.
