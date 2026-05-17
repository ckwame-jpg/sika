# Punch list state snapshot — 2026-05-17

`SIKA_PUNCH_LIST.md` checkboxes drifted behind merged work. This is the reconciled state — drop into the main punch list at your pace, or use this as the authoritative open-items list until you do.

**Last refreshed:** 2026-05-17 (early session — after PRs #167-#181 landed; supersedes [`PUNCH_LIST_STATE_2026_05_16.md`](PUNCH_LIST_STATE_2026_05_16.md)).

## Headline

**Three of the four "truly-open" items from the 2026-05-16 snapshot shipped between then and now**:

| Prior open item | Status as of 2026-05-17 | Evidence |
|---|---|---|
| Smarter #21 phase 2d (consumer + UI band) | **shipped** | [sika#179](https://github.com/ckwame-jpg/sika/pull/179) (scoring kernel consumer) + [sika#180](https://github.com/ckwame-jpg/sika/pull/180) (trade-ticket UI band) |
| Architecture #5 (feature freshness layer) | **shipped (WIP base + 2 follow-ups)** | [sika#169](https://github.com/ckwame-jpg/sika/pull/169) base + [sika#173](https://github.com/ckwame-jpg/sika/pull/173) `events_fresh_at` threading + [sika#175](https://github.com/ckwame-jpg/sika/pull/175) SUPPRESS policy registry consolidation |
| Smarter #13 phase 2b-2 (BR referee fetcher wiring) | **shipped** | [sika#174](https://github.com/ckwame-jpg/sika/pull/174) — operator-supplied BR URL pattern unblocked the deferred refresh-job wiring |
| Smarter #28 + #30 override registries | **mechanism-only (unchanged)** | Still awaiting Smarter #2 backtest output to populate; not a code task |

Plus the WNBA sport-expansion plan progressed: 3 of 8 PRs landed ([sika#177](https://github.com/ckwame-jpg/sika/pull/177) scaffolding, [sika#178](https://github.com/ckwame-jpg/sika/pull/178) market support, [sika#181](https://github.com/ckwame-jpg/sika/pull/181) gamelog + stats query branch).

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
| #22 (feature freshness SLAs) | **unblocked** | Architecture #5 prerequisite shipped ([sika#169](https://github.com/ckwame-jpg/sika/pull/169)). Next step: layer SLA policies on top of `feature_groups`/`check_freshness` already in scoring. |
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

After the 2026-05-17 reconciliation:

1. **Smarter #28 + #30 override tuning** — mechanism shipped; populating the override registries needs Smarter #2 backtest output, not code. **Not a coding task** — runs when the operator generates fresh backtest data.
2. **Smarter #22 (feature freshness SLAs)** — Architecture #5 prerequisite shipped, so this is now unblocked. Multi-PR design pass needed to define per-group SLAs on top of the freshness layer. **Not started; no urgency.**
3. **WNBA sport expansion** — 3 of 8 PRs shipped ([sika#177](https://github.com/ckwame-jpg/sika/pull/177), [sika#178](https://github.com/ckwame-jpg/sika/pull/178), [sika#181](https://github.com/ckwame-jpg/sika/pull/181)). 5 PRs remaining per `SMARTER_WNBA_PREP.md`. **In progress.**
4. **Smarter #21 phase 2d coverage-band expansion** — out of the 7 currently-trained stat keys, only 2 are in the `ok` coverage band that actually swaps probability. As more games settle and the operator re-runs `train-intervals`, more keys should migrate into `ok` and start consuming intervals automatically. **No code change required; ops cadence.**

Nothing on this list blocks the active NBA + MLB ship target. WNBA expansion continues per its own 8-PR plan.

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
