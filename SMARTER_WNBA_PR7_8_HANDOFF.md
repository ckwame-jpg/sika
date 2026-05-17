# Smarter WNBA — PR 7-8 handoff

**Session date written:** 2026-05-17 (after WNBA PR 6 merged at [sika#188](https://github.com/ckwame-jpg/sika/pull/188)).
**Author:** the session that finished 6 of 8 WNBA PRs (#177, #178, #181, #183, #184, #188).

This doc + `SMARTER_WNBA_PROMPT_PR7_8.md` (spawn prompt) get a fresh Claude session from "merged PR 6" to "merged PR 8". Read the spawn prompt first, then this doc only if something is unclear.

---

## TL;DR

- **PR 6 shipped — WNBA is now live by default.** A fresh sika deployment running the default refresh cycle fetches WNBA events from ESPN, persists KXWNBA Kalshi markets, scores them via the WNBA branch wired in PR 4, and surfaces them in the trade desk + `/product/freshness`.
- **Both remaining PRs are MVP+1.** Operator-experience improvements, not blockers. Ship if there's session budget; defer if not.
- **PR 7 (WNBA injury endpoint)** generalizes Smarter #17's NBA injury-suppression policy so WNBA gets the same OUT/DOUBTFUL handling. ~half-day, LOW risk. Touches the scoring kernel and adds a new cache table.
- **PR 8 (operator UX polish)** updates operator-facing strings that hardcoded "NBA/MLB" and verifies the readiness panel renders WNBA correctly. ~half-day, NONE risk. Frontend changes — use the `/frontend-design` skill family.
- **Verify Kalshi WNBA per-game prop coverage at first slate refresh.** PR 6 shipped the infrastructure with `kxwnbagame` series + NBA-pattern prop slugs. SMARTER_WNBA_PREP.md §1 / §7 flagged that per-game props weren't broadly live mid-May 2026; hit Kalshi's events API to confirm. If the slug is wrong, the `KALSHI_*` dicts in `apps/api/app/api/routes.py` + `apps/api/app/services/trade_desk.py` are the surface to update.

---

## What's already shipped (don't redo)

| # | PR | Scope |
|---|---|---|
| 169 | Arch #5 | Feature freshness layer — feature_groups + per-group PENALIZE / SUPPRESS / IGNORE policy |
| 173 | Arch #5 follow-up 1 | `events_fresh_at` threaded once per batch |
| 175 | Arch #5 follow-up 2 | Smarter #16 / #17 consolidated into SUPPRESS policy registry |
| 177 | WNBA PR 1 | Sport scaffolding — `SportKey`, ESPN URLs, team map, CSS token, cache TTLs |
| 178 | WNBA PR 2 | Market mapping — classifier WNBA branch, alias dict, combo-prefix gate |
| 181 | WNBA PR 3 | Gamelog + stats query — `_build_game_logs` dispatch, season rollover, stat-line specs |
| 183 | WNBA PR 4 | Scoring kernel — `_score_player_prop` WNBA branch, `wnba_props` / `wnba_singles` family registration |
| 184 | WNBA PR 5 | Training pipeline — `_DEFAULT_SERVE_FAMILY_KEYS` includes WNBA families |
| 188 | WNBA PR 6 | **`enabled_sports` flipped to include WNBA + Kalshi/Odds API/adapter wiring + `CURRENT_WATCHLIST_SPORTS` expansion + ml/promotion per-sport gate widening** |

**Baseline test counts after PR 6 + #186 (Smarter #22 freshness badge) + #187:** apps/api **1680 passed, 4 skipped**; apps/ml **255**; apps/web **176** (4 pre-existing tsc errors in `apps/web/test/fixtures/trade-fixtures.ts` from sika#180 — unrelated, vitest itself green).

---

## Cross-PR learnings worth carrying forward

These caught me on PR 6 and will likely recur:

1. **Flipping a `CURRENT_WATCHLIST_SPORTS`-style global gate has wider test blast radius than the registry / Kalshi changes themselves.** Adding WNBA cascaded into 5+ test-fixture updates: `/product/freshness` scope tests (×3), `/sports/availability` test, `test_persist_current_slate_snapshots_appends_new_rows_per_call`. When PR 7 adds WNBA to any new gate, grep for sibling tests that pin the pre-change set BEFORE running the full suite.
2. **Codex (gpt-5.5 default) hung again with a 651-line diff prompt.** Per the original handoff: "If codex hangs / rate-limits, fall back to the `python-reviewer` or `typescript-reviewer` subagent." The fallback worked cleanly on PR 6 and caught a real Medium (dead `_parlay_examples` WNBA gate). Skip the codex round; go straight to the reviewer subagent.
3. **Rebase mid-session.** `main` moved twice during PR 6 (Smarter #22 PR A + tuning playbook). The rebase was clean but DID change baselines (#186 added 8 api tests + 13 web tests). Always re-run all three suites post-rebase and re-confirm baselines before push.
4. **Reviewer feedback on PR 6 flagged a latent bug worth noting:** `apps/api/app/services/ml/walk_forward.py:358` (`_build_parlay_predicate`) gates only `{"NBA", "MLB"}` — a future `wnba_parlay_*` family with `sport_scope="WNBA"` would fall through to the bare `leg_filter` branch and silently over-include mixed-sport rows. Not introduced by PR 6, gated by a family that doesn't exist yet. Worth adding a `# TODO: add WNBA when wnba_parlay_* family ships` when that PR lands — NOT in PR 7 or 8.

---

## PR 7 — WNBA injury endpoint (~half day, LOW risk)

Generalize Smarter #17's `fetch_nba_injury_report` so WNBA gets the same OUT/DOUBTFUL suppression behavior the NBA path already has.

### Scope (in implementation order)

1. **`apps/api/app/clients/espn.py::fetch_nba_injury_report`** — generalize to `fetch_injury_report(sport_key: str = "NBA")`. ESPN's WNBA injuries page is `https://www.espn.com/wnba/injuries` (same scraped HTML pattern as NBA per `SMARTER_WNBA_PREP.md` §3). Keep the existing `fetch_nba_injury_report` as a thin wrapper for backwards compat — Smarter #17's caller already uses the NBA function name.
2. **New `apps/api/app/services/wnba_injury_report.py`** — mirror `nba_injury_report.py`. New `WnbaInjuryReportCache` model. **Recommendation:** keep separate from `NbaInjuryReportCache` rather than unifying with a `sport_key` column — minimum blast radius (per `SMARTER_WNBA_PREP.md` §4). Trade-off documented; can dedupe in a later PR.
3. **`apps/api/app/services/scoring/__init__.py`** — `_score_player_prop` WNBA branch (line ~1300, from PR 4) needs a parallel `emit_to_group("wnba_injury", ...)` call. Smarter #17's gate in `_single_scoring_adjustments` checks `family_key == "nba_props"` — generalize to `family_key in {"nba_props", "wnba_props"}` (or refactor the registered `nba_injury_suppress_when` callback into a sport-parametric form).
4. **`apps/api/app/services/refresh_jobs.py`** — add WNBA injury refresh entry mirroring the NBA one (`grep nba_injury_refresh` to find the dispatch).
5. **`apps/api/app/services/feature_groups.py`** — add `wnba_injury` SUPPRESS policy entry mirroring `nba_injury`.
6. **Drift guard sweep:** if you generalize ESPN injury fetching, verify there's no apps/ml mirror of `_NBA_INJURY_*` constants that needs syncing (`grep -ri nba_injury apps/ml/`).

### Tests

Mirror `test_nba_injury_suppression.py` patterns. New file: `apps/api/tests/test_wnba_injury_suppression.py`. Pin:
- ESPN HTML fixture parses into expected OUT / DOUBTFUL / DAY-TO-DAY rows.
- `WnbaInjuryReportCache` row created on first fetch, refreshed on TTL expiry.
- A WNBA prop where the player is OUT triggers the SUPPRESS path (recommendation hidden, not penalized).
- A WNBA prop where the player is DOUBTFUL applies the policy's suppress threshold.
- `family_key == "wnba_props"` correctly dispatches through the generalized gate.

### Reviewer prompt focus

- Pattern 1 (state-machine): is the new `WnbaInjuryReportCache` table migrated cleanly? Tests pass without a migration (SQLAlchemy creates tables in test DB) but production deploys will break.
- Pattern 2 (cross-component): if `fetch_injury_report` is generalized, did the existing `fetch_nba_injury_report` wrapper preserve the original signature for Smarter #17's caller?
- Pattern 9 (cross-scope): the `family_key == "nba_props"` check — easy to miss when widening to include `wnba_props`.

---

## PR 8 — Operator UX polish (~half day, NONE risk)

Update operator-facing strings that hardcoded "NBA/MLB". These became misleading when PR 6 added WNBA to `CURRENT_WATCHLIST_SPORTS`. Strings are functional (the trade desk still works); just stale.

### Scope

1. **`apps/api/app/services/trade_desk.py:70`** — `PRODUCT_SLATE_NO_CANDIDATES_REASON = "Current NBA/MLB events exist, but no current Kalshi markets are mapped to them."`. Two options:
   - Static update to `"Current NBA/MLB/WNBA events exist..."`, OR
   - Dynamic — build the sport list from `sorted(CURRENT_WATCHLIST_SPORTS)` at module load.
   Dynamic is cleaner but cascades through 5 test fixtures that pin the literal string. **Recommendation:** static update for minimum blast radius; revisit dynamic when the next sport gets added.
2. **`apps/api/app/services/ingestion/warming.py:89`** — duplicate static string. Update in lockstep.
3. **`apps/web/lib/health-status.ts:187-188`** — `"Current NBA/MLB slate refresh is queued."` / `"Refreshing the current NBA/MLB slate in background."` → `"NBA/MLB/WNBA slate"` (static) or refactor to dynamic.
4. **`apps/web/components/ops/mappings-desk.tsx:44`** — `SPORT_PRESETS = ["all", "NBA", "MLB"]` operator-tool filter list. Add `"WNBA"`. Verify this controls a sport tab in the mappings-desk admin tool — if so, adding WNBA makes WNBA mappings discoverable.
5. **Test fixtures that pin the literal strings** (must update in lockstep):
   - `apps/api/tests/test_trade_desk.py:583, 736, 753`
   - `apps/api/tests/test_api.py:1260`
   - `apps/web/components/trade/trade-desk.test.tsx:137, 143, 160`
   - `apps/web/components/layout/product-freshness-banner.test.tsx:32, 44`
6. **Readiness panel `interval_models` tile:** verify it renders empty WNBA rows correctly (PR 5 wired the manifest auto-include). Check `apps/web/components/ops/readiness-panel.tsx` or equivalent — a WNBA row with zero data should render the same "insufficient_history" treatment as NBA does on a fresh deployment.
7. **Tailwind `--sport-wnba` CSS variable:** added in PR 1; confirm it renders by opening the trade desk in a dev server and looking for the WNBA sport pill — should have its own color, not NBA's.

### Tests

- Update all string-pinning test fixtures to match the new copy.
- Add a regression test pinning that `PRODUCT_SLATE_NO_CANDIDATES_REASON` mentions "WNBA" (catch future copy drift).
- Add a web test asserting the `health-status` banner mentions WNBA.

### Frontend workflow

Per the original handoff: "Frontend changes use the `/frontend-design` skill family." Use it for the readiness panel + mappings-desk verification. The Tailwind `--sport-wnba` token check belongs in `apps/web/tailwind.config.ts` (check existing `--sport-nba` for the pattern).

---

## State of branches

After PR 6 merged into main (sika#188 at commit `72d1821`), no work-in-progress remains. Next session should branch fresh:

```bash
git fetch origin main
git checkout -b claude/smarter-wnba-pr-7-injury origin/main
# ... PR 7 work, merge ...
git fetch origin main
git checkout -b claude/smarter-wnba-pr-8-ux-polish origin/main
# ... PR 8 work, merge ...
```

---

## Workflow requirements (unchanged from PRs 1-6)

- **Worktree, not main.** Branch `claude/<topic>` off `origin/main` per PR.
- **One PR per scope.** PRs 7 and 8 are independent — don't bundle.
- **TDD-ish ordering.** Run the failing test, watch it red, implement, watch it green.
- **9-point self-review before push** (full list at the end of `SMARTER_WNBA_HANDOFF.md`).
- **Reviewer subagent in preference to codex.** Codex has hung 5× across the last two sessions; `python-reviewer` (or `typescript-reviewer` for apps/web) is responsive and catches real Mediums.
- **Squash-merge with admin:** `gh pr merge <N> --squash --admin --body ""`.
- **Rebase if main moves mid-session.** Re-run all three suites post-rebase before push.

---

## Baseline test counts (verify before push)

| Suite | Post-PR-6 baseline |
|---|---|
| apps/api | **1680** (4 skipped) |
| apps/ml | **255** |
| apps/web | **176** (4 pre-existing tsc errors from sika#180 — vitest green) |

Run before push:
- `cd apps/api && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `cd apps/ml && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `cd apps/web && npx tsc --noEmit && npx vitest run`

---

## Useful prior-art commit references

- **PR 6 (sika#188)** — `72d1821` — full WNBA-by-default wiring. Patterns to reuse: `monkeypatch.setattr(settings, "enabled_sports", [...])` in `test_market_filtering.py` for the defense-in-depth gate test; new `test_wnba_scaffolding.py` tests pin Kalshi constants in BOTH routes.py and trade_desk.py.
- **PR 4's HIGH catch** (sika#183) — codex caught `_prop_value_from_raw` not handling WNBA. The pattern (widen `{NBA}` → `{NBA, WNBA}`) recurred several times in PR 6's sweep too — expect at least one more in PR 7's injury-gate work.
- **`_player_prop_participation_gate`** in PR 4 — canonical "WNBA shares NBA basketball semantics" pattern for minutes-based gates, FGA/usage proxies, etc.

---

## Cross-package drift guards (still in place)

- `_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME` — apps/api + apps/ml, validated by `test_team_abbreviation_map_matches_apps_api_canonical_source`. PR 1 added WNBA to both copies; no action needed in PR 7 or 8 unless you add another team to the apps/api map.
- `INTERVAL_COVERAGE_*` — apps/api + apps/ml. Not WNBA-relevant.
- **Kalshi constants duplicated** in `apps/api/app/api/routes.py` + `apps/api/app/services/trade_desk.py` (Bug #30 design smell). PR 6 updated both sides for WNBA. PRs 7 and 8 don't touch these; the dedupe is a separate PR.

---

## What NOT to do

- **Do NOT add WNBA to `parlay_enabled_sports` in PR 7 or 8.** `parlay_family_key` still has no WNBA-specific families. WNBA combos would silently land in `mixed_parlay_*` and pollute mixed-family calibration. A separate WNBA-parlay-family PR follows once Smarter #28 backtest data justifies one.
- **Do NOT add WNBA to `_parlay_examples` per-sport gate in `apps/api/app/services/ml/promotion.py:224`.** PR 6 reviewer caught this as dead code (no `wnba_parlay_*` family exists). Add when the WNBA parlay PR ships.
- **Do NOT update `walk_forward._build_parlay_predicate:358` in PR 7 or 8.** Same reason. Latent bug there (silent over-inclusion when a `sport_scope="WNBA"` parlay family is added later) — fix in the WNBA parlay PR with a sibling test, not as a drive-by.
- **Do NOT skip the reviewer subagent for PR 7** — the new `WnbaInjuryReportCache` model + cross-PR `family_key` gate are exactly the surface where the reviewer catches real bugs.

---

## Verification at first WNBA slate refresh (carryover from PR 6)

Two things to verify before assuming Kalshi day-1 parity. Both were flagged in the PR 6 body:

1. **Kalshi `kxwnbagame` series slug.** PR 6 used the NBA naming pattern (`kxnbagame` → `kxwnbagame`). Hit Kalshi's events API for the `kxwnbagame` series at first WNBA slate refresh to confirm it returns markets. If empty, look up the actual WNBA-game series slug from Kalshi's UI and update both `apps/api/app/api/routes.py:KALSHI_EVENT_SERIES` and `apps/api/app/services/trade_desk.py:KALSHI_EVENT_SERIES`.
2. **WNBA prop stat slugs.** PR 6 mirrored NBA's (`player-points`, `player-rebounds`, etc.) — Kalshi has signaled NBA-parity for WNBA props, but actual slugs may differ at roll-out. Same dicts hold the answers.

If Kalshi still has no per-game WNBA props at first refresh, PR 6 still ships value (infrastructure is ready) but the day-1 watchlist won't carry WNBA props. Tell the operator; no code change needed.
