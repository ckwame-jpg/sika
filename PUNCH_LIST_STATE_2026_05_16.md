# Punch list state snapshot — 2026-05-16

`SIKA_PUNCH_LIST.md` checkboxes drifted behind the actual shipped work. This is the reconciled state as of today — drop in to the main punch list at your pace, or use this as the authoritative open-items list until you do.

## Section 1 — Bugs & Issues

### HIGH

All HIGH bugs in the main punch list with `[ ]` are **shipped** — verified against code references in this repo:

| Bug | Status | Evidence |
|---|---|---|
| #4 (MLB park & weather inactive) | shipped | `apps/api/app/services/scoring/__init__.py:1208` wires `mlb_park_coords` → `load_weather(lat=…, lon=…, game_time_utc=…)`; venue normalization emits `venue_id` |
| #6 (`/positions` Kalshi calls uncached) | shipped | `apps/api/app/api/routes.py:1441` — Kalshi account snapshot cached ~30s; `force=True` query param bypasses |
| #7 (monotonicity clamp doesn't recompute edge) | shipped | `apps/api/app/services/scoring/monotonicity.py:90` recomputes `recommendation.edge` after clamping |
| #8 (`latest_*_by_market_id` uses `max(id)`) | shipped | `apps/api/app/services/watchlist_coverage.py:265` — new `_latest_per_market_by_captured_at` window function |
| #9 (monotonicity leaves recs active below floor) | shipped | `apps/api/app/services/scoring/monotonicity.py:107-132` — suppression with `monotonicity_edge_below_min` reason |
| #10 (timed-out workers commit after timeout) | shipped | `apps/api/app/services/refresh_jobs.py:847` — `WorkerCancelledError` + `before_commit` hook |
| #11 (refresh-job singleton claim race) | shipped | `_claim_next_job` uses `pg_advisory_xact_lock` |
| #12 (settlement only latest per ticker) | shipped | `apps/api/app/services/predictions.py:540-555` — removed `latest_only_per_key` toggle |
| #13 (ESPN player search ignores team hint) | shipped | `team_hint` flow with `team_hint` cache-key inclusion |

### MEDIUM

All MEDIUM bugs with `[ ]` are **shipped** except as noted:

| Bug | Status | Evidence |
|---|---|---|
| #14 (unbounded list endpoints) | shipped | `Query(ge=1, le=N)` on every list endpoint in `routes.py` |
| #15 (paper/demo schemas accept any string) | shipped | `apps/api/app/schemas.py:830-907` — `LowercaseSide`/`LowercaseAction`/`LowercaseTimeInForce` Literal types |
| #16 (training imputation leakage) | shipped | per-fold median in `_fold_feature_spec` |
| #17 (market mapping low-confidence) | shipped | `market_mapping.py` persists score + candidates; ops override endpoint |
| #18 (Kalshi market discovery cap 5K) | shipped | `max_pages` default raised 5 → 50 |
| #19 (runtime retention deletes ML data) | shipped | two-tier prediction retention; long-lived archive |
| #20 (single-split eval, optimistic gate) | shipped | walk-forward eval in `apps/ml/ml/promotion.py`; worst-fold Brier gate |
| #21 (weekly retrain inside API worker) | shipped | `.github/workflows/ml-retrain.yml` — moved to GitHub Actions |
| #22 (transient Kalshi requeue forever) | shipped | `transient_attempts` counter + exp backoff + dead-letter |
| #23 (unbounded all-row scans) | shipped | `Market.raw_data["…"].as_string()` SQL filter |
| #24 (SWR cache-key bugs) | shipped | `serializeQuery` sorts keys; `tradeDesk()` normalizes "all sports" |
| #25 (sequential paged Kalshi) | shipped | parallelized chunks |
| #26 (sidebar/starfield drain) | shipped | `visibilitychange` pause + pointer detach |
| #27 (settlement counter inflation) | shipped | only count rows whose state changed |
| #28 (`/positions` no pagination) | shipped | bounded `Query` + `paper_truncated`/`demo_truncated` flags |
| #29 (two copies of `features.py`) | shipped | `packages/ml-features` shared module |
| #30 (duplicate `visible_sports`/`SPORT_TINTS`) | shipped | shared `sport-tints.ts` import |
| #31 (`submit_demo_order` non-transactional) | shipped | outbox pattern |
| #32 (`_prune_current_slate_snapshots` branch split) | shipped | `maintenance.py:365` — empty-table / non-empty branches |

### LOW

| Bug | Status | Evidence |
|---|---|---|
| #33 (daemon-thread cancellation gap) | shipped (covered by #10) | `WorkerCancelledError` flows through every worker commit |
| #34 (`/watchlist` Python filter after limit) | shipped | post-filter limit semantics fixed |
| #35 (`triggerRefreshAndRevalidate` no AbortSignal) | shipped | `AbortController` wired into refresh poll |
| #36 (hydration mismatch on first paint) | shipped | first-mount migration tracked separately |
| #37 (`randomWalk` sparkline drifts) | shipped | real captured prices from the API |
| #38 (sidebar drag pointer hijacking) | n/a — no sidebar drag exists today; covered by #26 starfield fix |
| #39 (contract drift check not portable) | shipped | portable check in CI |
| #41 (stale ML family registry) | shipped | `apps/ml/ml/families.py` deleted; `FAMILY_DEFINITIONS` single source |
| #42 (`parlay_4_6_leg_combiner` heuristic-only) | wontfix-by-design | `apps/api/app/services/model_families.py:48-55` — 3-leg + 4-6-leg parlays intentionally stay `heuristic_only` because per-family settled volume can't clear bug #20's walk-forward floor |
| #43 (`_session_predictions` O(N)) | shipped | per-session index with O(1) lookup |
| #44 (probable-pitcher hydrate fragile) | shipped | fallback in place when ESPN omits |
| #45 (`triggerPredictionSettlement` cache invalidation) | shipped | watchlist/trade-desk cache invalidation wired |
| #46 (`_account_error_message` masks detail) | shipped | surfaces structured detail to operator |
| #47 (startup refresh swallowed) | shipped | non-blocking enqueue + visible failure |
| #48 (Kalshi private-key path hardcoded) | shipped | `apps/api/app/config.py:20` defaults to `Path.home() / ".config" / "kalshi" / "kalshi-demo.key"` — not user-specific |

### Still genuinely OPEN in Section 1

- **Bug #40** — Web contracts split between generated OpenAPI types and hand-written mirrors (`apps/web/lib/types.ts` is 881 lines of hand-written types; `packages/contracts/generated/api.d.ts` is the 126KB generated source). MEDIUM effort. Full migration is one-endpoint-family-at-a-time, not a single PR. See Architecture rewrite #6.

That's the **only** truly open Section 1 item.

## Make Sika Smarter — open items

| Item | Status | Notes |
|---|---|---|
| #2 (walk-forward backtest) | shipped | `apps/ml/ml/backtest.py` |
| #4 (MLB venue → weather pipeline) | shipped | via Bug #4 fix |
| #7 (MLB park × weather HR interaction) | shipped | `apps/api/app/services/heuristic_factors.py:432` — `_mlb_park_weather_hr_interaction` wired into `home_runs` + `total_bases` via `_MLB_FACTORS_BY_STAT` (12 tests in `test_mlb_park_weather_interaction.py`) |
| #8 (correlation-aware parlay engine) | shipped | [sika#141](https://github.com/ckwame-jpg/sika/pull/141) phase 3 |
| #22 (feature freshness SLAs) | blocked | depends on Architecture #5 (open) |
| #25 (market mapping confidence + override) | shipped | [sika#134](https://github.com/ckwame-jpg/sika/pull/134) |
| #30 (per-family `watchlist_min_edge` tuning) | shipped | [sika#146](https://github.com/ckwame-jpg/sika/pull/146) — mechanism-only ship (empty `WATCHLIST_MIN_EDGE_OVERRIDES` registry, default fallback to `settings.watchlist_min_edge`) |
| #32 (drawdown brake on demo trading) | shipped | [sika#144](https://github.com/ckwame-jpg/sika/pull/144) — drawdown brake snapshot on `GET /positions` |
| #31 (LLM narrator) | shipped, **pending operator UI eyeball** | branch `claude/smarter-31-llm-narrator` pushed but PR unmerged — operator needs to spot-check the toggle + rendered narration |
| #21 phase 2b/d (quantile-regression intervals — training pipeline + UI band) | handed off | see [SMARTER_21_PHASE_2B_HANDOFF.md](SMARTER_21_PHASE_2B_HANDOFF.md) + [SMARTER_21_PHASE_2B_PROMPT.md](SMARTER_21_PHASE_2B_PROMPT.md) |

## Architecture / Rewrite Candidates

| Item | Status |
|---|---|
| #1 (Owner / public API boundary) | deferred indefinitely (local-only deploy) |
| #2 (Refresh job runner) | shipped (retires bugs #10, #11, #22) |
| #3 (ML training & promotion pipeline) | shipped (retires bug #21; underpins Smarter #2, #20) |
| #4 (Market / player resolution service) | shipped (retires bugs #13, #17; feeds Smarter #25) |
| #5 (Feature freshness layer) | OPEN — prerequisite for Smarter #22, #23 |
| #6 (Contract / type ownership) | OPEN — retires bugs #39, #40 (only #40 still open) |

### Also-worth-tracking single-PR refactors

| Item | Status |
|---|---|
| R1 (split `scoring.py`) | shipped (R1 phase 1-4 — PRs #135-#137) |
| R2 (decompose `ingestion.py`) | shipped (R2 phase 1-3 — PRs #138, #139) |
| R3 (latest_by_max_id helper) | shipped (Bug #8 fix) |
| R4 (consolidate `features.py`) | shipped (Bug #29 fix) |

## Recommended next bets

The clean small-effort items are all shipped. What remains needs either a strategy decision or a fresh session:

1. **Bug #40 / Architecture #6** — Web contracts migration. `apps/web/lib/types.ts` is 881 lines of hand-written types that mirror `packages/contracts/generated/api.d.ts` (the OpenAPI-generated 126KB source). The migration is one endpoint family at a time, and each migration needs verification that every consumer surface (components, hooks, page handlers) still type-checks against the generated `Schema[…]` references. Recommend tackling read-only endpoint families first (`/health`, `/predictions/summary`) before mutating ones.
2. **Smarter #21 phase 2b/d** — Already handed off in [SMARTER_21_PHASE_2B_PROMPT.md](SMARTER_21_PHASE_2B_PROMPT.md); needs its own session.
3. **Architecture #5 — Feature freshness layer** — Prerequisite for Smarter #22. Not started; needs a design pass to define the `FeatureGroupSnapshot` shape and a migration order (MLB weather/park/starter + NBA injury/rest first, then sportsbook/referee features). L effort across multiple PRs.
4. **Smarter #13 phase 2b-2** — BR referee tendency fetcher wiring is deferred pending operator manual validation of the basketball-reference URL + table layout (the public BR site returns 403 to anonymous fetches; operators need to supply the working URL pattern from their environment).
5. **Smarter #31** — LLM narrator branch is shipped but unmerged; needs operator UI eyeball.

## Recently shipped PRs (since last roll-up)

| PR | Item | Brief |
|---|---|---|
| [#134](https://github.com/ckwame-jpg/sika/pull/134) | Smarter #25 | Operator review queue for fuzzy market→event mappings |
| [#135](https://github.com/ckwame-jpg/sika/pull/135) | R1 phase 2 | Extract resolver from scoring kernel |
| [#136](https://github.com/ckwame-jpg/sika/pull/136) | R1 phase 3 | Extract heuristics helpers from scoring kernel |
| [#137](https://github.com/ckwame-jpg/sika/pull/137) | R1 phase 4 | Extract orchestration from scoring kernel |
| [#138](https://github.com/ckwame-jpg/sika/pull/138) | R2 phase 2 | Extract cycle runners from ingestion kernel |
| [#139](https://github.com/ckwame-jpg/sika/pull/139) | R2 phase 3 | Extract warming + batch-selection helpers |
| [#140](https://github.com/ckwame-jpg/sika/pull/140) | Smarter #21 phase 2c | Serve-time loader for prop interval models |
| [#141](https://github.com/ckwame-jpg/sika/pull/141) | Smarter #8 phase 3 | parlays.py blends empirical correlation |
| [#142](https://github.com/ckwame-jpg/sika/pull/142) | Smarter #9 phase 3 | Kelly sizing diagnostics on scored recommendations |
| [#143](https://github.com/ckwame-jpg/sika/pull/143) | Smarter #21 phase 2b/d | Session handoff + spawn prompt |
| [#144](https://github.com/ckwame-jpg/sika/pull/144) | Smarter #32 | Drawdown brake snapshot on `/positions` |
| [#145](https://github.com/ckwame-jpg/sika/pull/145) | docs | This punch list state snapshot |
| [#146](https://github.com/ckwame-jpg/sika/pull/146) | Smarter #30 | Per-family `watchlist_min_edge` tuning mechanism |
