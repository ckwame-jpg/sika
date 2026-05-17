# Punch list state snapshot — 2026-05-17

`SIKA_PUNCH_LIST.md` checkboxes drifted behind merged work. This is the reconciled state — drop into the main punch list at your pace, or use this as the authoritative open-items list until you do.

**Last refreshed:** 2026-05-17 EOD (after PRs #182-#205 landed; supersedes the early-2026-05-17 snapshot of this file).

## Headline

**WNBA sport-expansion is COMPLETE (8 of 8 PRs merged).** The 2026 WNBA season started 2026-05-08 and runs through October; a fresh sika deployment now fetches WNBA events from ESPN, persists KXWNBA Kalshi markets, scores them with the WNBA branch + workload + injury features wired, and surfaces them in the trade desk + `/product/freshness` with the right operator-facing copy.

| Prior open item | Status as of 2026-05-17 EOD | Evidence |
|---|---|---|
| WNBA sport expansion (was 3/8) | **COMPLETE (8/8)** | [sika#183](https://github.com/ckwame-jpg/sika/pull/183) scoring kernel, [sika#184](https://github.com/ckwame-jpg/sika/pull/184) training pipeline, [sika#188](https://github.com/ckwame-jpg/sika/pull/188) enabled-by-default wiring, [sika#192](https://github.com/ckwame-jpg/sika/pull/192) injury endpoint, [sika#193](https://github.com/ckwame-jpg/sika/pull/193) operator UX polish |
| Smarter #22 feature freshness SLAs | **PR A shipped + freshness audit panel shipped** | [sika#186](https://github.com/ckwame-jpg/sika/pull/186) operator-facing badge surfaces stale-group + confidence-delta diagnostics on the trade ticket; [sika#190](https://github.com/ckwame-jpg/sika/pull/190) freshness calibration audit panel. PR B (policy registry expansion) gated on operator observation per [`SMARTER_22_TUNING_PLAYBOOK.md`](SMARTER_22_TUNING_PLAYBOOK.md). |
| Smarter #28 + #30 override registries | **mechanism-only (unchanged)** | Still awaiting Smarter #2 backtest output to populate; not a code task |
| Smarter #21 phase 2d coverage-band expansion | **unchanged (ops cadence)** | 2 of 7 trained stat keys in `ok` band; more migrate as games settle |

Earlier in the day (covered in the prior snapshot): Smarter #21 phase 2d shipped, Architecture #5 + follow-ups shipped, Smarter #13 phase 2b-2 shipped, WNBA PRs 1-3 shipped.

## Section 1 — Bugs & Issues

No changes since 2026-05-16. All HIGH, MEDIUM, and LOW items remain shipped per the [prior snapshot](PUNCH_LIST_STATE_2026_05_16.md#section-1--bugs--issues).

**Still genuinely open in Section 1:** **None.**

## Make Sika Smarter — open items

| Item | Status | Notes |
|---|---|---|
| #2 (walk-forward backtest) | shipped | `apps/ml/ml/backtest.py` |
| #4 (MLB venue → weather pipeline) | shipped | via Bug #4 fix |
| #7 (MLB park × weather HR interaction) | shipped | `apps/api/app/services/heuristic_factors.py` |
| #8 (correlation-aware parlay engine) | shipped | [sika#141](https://github.com/ckwame-jpg/sika/pull/141) |
| #13 phase 2b-2 (BR referee fetcher wiring) | **shipped (2026-05-17)** | [sika#174](https://github.com/ckwame-jpg/sika/pull/174) — operator unblocked the URL pattern; `BasketballReferenceClient.fetch_referee_season_stats` now wired into the deferred refresh job |
| #21 (quantile-regression intervals) | **COMPLETE (2026-05-17)** | Phase 2a (sidecar contract) → 2b ([sika#154](https://github.com/ckwame-jpg/sika/pull/154), [sika#158](https://github.com/ckwame-jpg/sika/pull/158) dataset + train-intervals CLI) → 2c ([sika#140](https://github.com/ckwame-jpg/sika/pull/140) serve-time loader) → 2d ([sika#179](https://github.com/ckwame-jpg/sika/pull/179) scoring consumer + [sika#180](https://github.com/ckwame-jpg/sika/pull/180) UI band). Strict gating: only stat keys with `coverage_status == "ok"` (2 of 7 currently — NBA points + assists) swap probability_yes; the rest stay on Poisson. UI band surfaces for all trained stat keys regardless of coverage so operators can A/B inspect. |
| #22 (feature freshness SLAs) | **PR A shipped; PR B gated on operator observation** | [sika#186](https://github.com/ckwame-jpg/sika/pull/186) trade-ticket freshness badge; [sika#190](https://github.com/ckwame-jpg/sika/pull/190) freshness calibration audit panel. Architecture #5 prerequisite shipped earlier as [sika#169](https://github.com/ckwame-jpg/sika/pull/169). PR B (policy registry expansion) gated per [`SMARTER_22_TUNING_PLAYBOOK.md`](SMARTER_22_TUNING_PLAYBOOK.md). |
| #25 (market mapping confidence + override) | shipped | [sika#134](https://github.com/ckwame-jpg/sika/pull/134) |
| #28 (per-family `quality_tier` calibration) | shipped (mechanism); awaiting tuning data | empty registry until Smarter #2 backtest results inform per-family overrides |
| #30 (per-family `watchlist_min_edge` tuning) | shipped (mechanism); awaiting tuning data | [sika#146](https://github.com/ckwame-jpg/sika/pull/146) — empty `WATCHLIST_MIN_EDGE_OVERRIDES` registry, default fallback to `settings.watchlist_min_edge`; populate from Smarter #2 results |
| #31 (LLM narrator) | shipped | [sika#94](https://github.com/ckwame-jpg/sika/pull/94) |
| #32 (drawdown brake on demo trading) | shipped | [sika#144](https://github.com/ckwame-jpg/sika/pull/144) |

## Architecture / Rewrite Candidates

| Item | Status |
|---|---|
| #1 (Owner / public API boundary) | deferred indefinitely (local-only deploy) |
| #2 (Refresh job runner) | shipped (retires bugs #10, #11, #22) |
| #3 (ML training & promotion pipeline) | shipped (retires bug #21; underpins Smarter #2, #20, #21) |
| #4 (Market / player resolution service) | shipped (retires bugs #13, #17; feeds Smarter #25) |
| #5 (Feature freshness layer) | **shipped (WIP base + 2 follow-ups)** — [sika#169](https://github.com/ckwame-jpg/sika/pull/169) ships the freshness layer; [sika#173](https://github.com/ckwame-jpg/sika/pull/173) threads `events_fresh_at` once per batch (O(N) → O(1) per-batch reads); [sika#175](https://github.com/ckwame-jpg/sika/pull/175) consolidates Smarter #16/#17 into a unified SUPPRESS policy registry on top of the freshness layer. Future SLA policies (Smarter #22) layer on top. |
| #6 (Contract / type ownership) | shipped — Bug #40 phase 1-10 |

### Also-worth-tracking single-PR refactors

| Item | Status |
|---|---|
| R1 (split `scoring.py`) | shipped (R1 phases 1-4 — PRs #135-#137) |
| R2 (decompose `ingestion.py`) | shipped (R2 phases 1-3 — PRs #138, #139) |
| R3 (latest_by_max_id helper) | shipped (Bug #8 fix) |
| R4 (consolidate `features.py`) | shipped (Bug #29 fix) |

## Truly-open items (the short list)

After the 2026-05-17 EOD reconciliation:

1. **Smarter #28 + #30 override tuning** — mechanism shipped; populating the override registries needs Smarter #2 backtest output, not code. **Not a coding task** — runs when the operator generates fresh backtest data.
2. **Smarter #22 PR B (policy registry expansion)** — PR A shipped ([sika#186](https://github.com/ckwame-jpg/sika/pull/186)) + freshness audit panel ([sika#190](https://github.com/ckwame-jpg/sika/pull/190)). PR B gated on operator observation per [`SMARTER_22_TUNING_PLAYBOOK.md`](SMARTER_22_TUNING_PLAYBOOK.md). **Operator-cadence, not coding-blocked.**
3. **Smarter #21 phase 2d coverage-band expansion** — out of the 7 currently-trained stat keys, only 2 are in the `ok` coverage band that actually swaps probability. As more games settle and the operator re-runs `train-intervals`, more keys should migrate into `ok` and start consuming intervals automatically. **No code change required; ops cadence.**
4. **WNBA day-1 verification** (carryover from PR 6): confirm Kalshi's `kxwnbagame` series slug returns markets and that the NBA-pattern WNBA prop stat slugs match at first live KXWNBA prop. Update [`apps/api/app/api/routes.py:KALSHI_EVENT_SERIES`](apps/api/app/api/routes.py) + [`apps/api/app/services/trade_desk.py:KALSHI_EVENT_SERIES`](apps/api/app/services/trade_desk.py) if either is wrong. **Operational, not coding-blocked.**

**Sport expansion next:** **NFL.** The 2026 NFL season kicks off ~2026-09-04 (~3.5 months out). An 8-PR sequence like WNBA's would eat ~3-4 weeks, but NFL is structurally messier than WNBA (separate stat lines for passing / rushing / receiving instead of reusing basketball semantics, fewer games per player per week so the backtest window matters more). Recommended start window: now-to-mid-June, leaving runway for tuning against last season's data before kickoff. The Smarter #28 follow-up (WNBA-parlay family + walk-forward predicate widening at [`walk_forward.py:358`](apps/api/app/services/ml/walk_forward.py)) can land in parallel once backtest data justifies a `wnba_parlay_*` family.

Nothing on this list blocks the active NBA + MLB + WNBA ship target.

## Recently shipped PRs (since [PUNCH_LIST_STATE_2026_05_16.md](PUNCH_LIST_STATE_2026_05_16.md))

| PR | Item | Brief |
|---|---|---|
| [#167](https://github.com/ckwame-jpg/sika/pull/167) | docs | Late-2026-05-16 PUNCH_LIST_STATE refresh |
| [#168](https://github.com/ckwame-jpg/sika/pull/168) | docs | Comprehensive WNBA support prep doc |
| [#169](https://github.com/ckwame-jpg/sika/pull/169) | Arch #5 | Feature freshness layer (WIP base) |
| [#170](https://github.com/ckwame-jpg/sika/pull/170) | docs | WNBA session handoff + spawn prompt; SIKA_PUNCH_LIST status banner |
| [#171](https://github.com/ckwame-jpg/sika/pull/171) | docs | Amend Smarter #21 handoff with phase 2d parallelism note |
| [#172](https://github.com/ckwame-jpg/sika/pull/172) | docs | Research-first rule + verified Smarter #13 BR referee URL |
| [#173](https://github.com/ckwame-jpg/sika/pull/173) | Arch #5 | Thread `events_fresh_at` once per batch |
| [#174](https://github.com/ckwame-jpg/sika/pull/174) | Smarter #13 | BR referee tendency fetcher wired (phase 2b-2 COMPLETE) |
| [#175](https://github.com/ckwame-jpg/sika/pull/175) | Arch #5 | Consolidate Smarter #16/#17 into SUPPRESS policy registry |
| [#176](https://github.com/ckwame-jpg/sika/pull/176) | docs | Refresh Smarter #21 phase 2d handoff + spawn prompt |
| [#177](https://github.com/ckwame-jpg/sika/pull/177) | WNBA | Sport scaffolding (PR 1 of 8) |
| [#178](https://github.com/ckwame-jpg/sika/pull/178) | WNBA | Market support (PR 2 of 8) |
| [#179](https://github.com/ckwame-jpg/sika/pull/179) | Smarter #21 phase 2d | Scoring kernel interval-model consumer (PR 3) |
| [#180](https://github.com/ckwame-jpg/sika/pull/180) | Smarter #21 phase 2d | Trade-ticket prediction-interval band (PR 4 — phase 2d COMPLETE) |
| [#181](https://github.com/ckwame-jpg/sika/pull/181) | WNBA | Gamelog + stats query branch (PR 3 of 8) |
| [#182](https://github.com/ckwame-jpg/sika/pull/182) | docs | WNBA PR 4 spawn prompt + handoff refresh |
| [#183](https://github.com/ckwame-jpg/sika/pull/183) | WNBA | Scoring kernel WNBA branch — `_score_player_prop` + `wnba_props` / `wnba_singles` family registration (PR 4 of 8) |
| [#184](https://github.com/ckwame-jpg/sika/pull/184) | WNBA | Training pipeline — `_DEFAULT_SERVE_FAMILY_KEYS` includes WNBA families (PR 5 of 8) |
| [#185](https://github.com/ckwame-jpg/sika/pull/185) | docs | WNBA PR 6-8 handoff + spawn prompt |
| [#186](https://github.com/ckwame-jpg/sika/pull/186) | Smarter #22 PR A | Trade-ticket freshness badge — operator-facing stale-group + confidence-delta diagnostics |
| [#187](https://github.com/ckwame-jpg/sika/pull/187) | docs | Smarter #22 tuning playbook + punch-list cross-link |
| [#188](https://github.com/ckwame-jpg/sika/pull/188) | WNBA | `enabled_sports` flipped to include WNBA + Kalshi / Odds API / adapter wiring + `CURRENT_WATCHLIST_SPORTS` expansion + ml/promotion per-sport gate widening (PR 6 of 8) |
| [#189](https://github.com/ckwame-jpg/sika/pull/189) | docs | WNBA PR 7-8 handoff + spawn prompt (post-PR-6 refresh) |
| [#190](https://github.com/ckwame-jpg/sika/pull/190) | Smarter #22 | Freshness calibration audit panel (Smarter #22 PR B prep) |
| [#191](https://github.com/ckwame-jpg/sika/pull/191) | docs | Point Smarter #22 playbook at the audit panel |
| [#192](https://github.com/ckwame-jpg/sika/pull/192) | WNBA | WNBA injury endpoint + suppression gate — new `WnbaInjuryReportCache`, parallel `wnba_injury` SUPPRESS-policy entry, scoring kernel emit, `wnba_injury_refresh` job kind + cron (PR 7 of 8) |
| [#193](https://github.com/ckwame-jpg/sika/pull/193) | WNBA | Operator UX polish — `PRODUCT_SLATE_NO_CANDIDATES_REASON` + health-status banners updated to "NBA/MLB/WNBA"; mappings-desk `SPORT_PRESETS` widened; missing `wnba` badge variant added (PR 8 of 8 — **WNBA sequence complete**) |
| [#199](https://github.com/ckwame-jpg/sika/pull/199) | web refactor | Mappings desk redesign with `cosmos-chip` + mobile polish |
| [#200](https://github.com/ckwame-jpg/sika/pull/200) | web refactor | Add `text-2xs` + `text-3xs` Tailwind utilities |
| [#201](https://github.com/ckwame-jpg/sika/pull/201) | web refactor | Add `bg-surface-soft` + `border-surface-soft` tokens |
| [#202](https://github.com/ckwame-jpg/sika/pull/202) | web | `EmptyState` + `LoadingState` primitives |
| [#203](https://github.com/ckwame-jpg/sika/pull/203) | web refactor | Add `focus-visible:ring-focus` to bare buttons |
| [#204](https://github.com/ckwame-jpg/sika/pull/204) | docs | Mark resolved drift items in `DESIGN_SYSTEM.md` |
| [#205](https://github.com/ckwame-jpg/sika/pull/205) | web refactor | JSDoc Badge / outcome-pill + delete orphaned tokens |
