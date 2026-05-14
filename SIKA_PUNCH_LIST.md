# Sika Punch List — 2026-05-12

Merged from two independent audits (codex deep pass + claude deep pass, plus an earlier capped codex pass). Three suspect single-agent findings were verified against the actual source before inclusion (marked **✓ verified**).

## How to read this

- **Severity** — `CRITICAL` / `HIGH` / `MEDIUM` / `LOW`. When the two audits disagreed, the higher rating is preserved.
- **Effort** — rough sizing: `S` (≤30 min) / `M` (a few hours) / `L` (a day or two) / `XL` (multi-day).
- **Source** — `[both]` = both agents independently flagged; `[codex]` / `[claude]` = single-agent; `[✓]` = verified against source code before inclusion; `[user-surfaced]` = surfaced by user input rather than audit.
- **Status** — `[x]` in front = shipped via PR.
- Sport scope: NBA + MLB are the active ship target. NFL / Soccer / Tennis are planned later. UFC is removed from scope — see [Dead Code](#dead-code-to-remove).

---

## Section 1 — Bugs & Issues

### CRITICAL

> Bug #1 (anonymous mutating endpoints) was demoted — see [#48a](#48a-deferred--anonymous-mutating-endpoints). Numbering is preserved so cross-references stay valid.

- [x] **2. ML train target is "selected-side-won", but serving reads `predict_proba[:, 1]` as P(YES)** `[both — claude rated CRITICAL]` · effort: **M** · **shipped: PR #25 + #26**
  - **Where:** `apps/ml/ml/dataset.py` (label = selected-side outcome); `apps/api/app/services/scoring.py` and `apps/api/app/services/ml/shadow.py` (read `predict_proba[:, 1]` and treat as P(YES)); `apps/api/app/services/ml/runtime.py`.
  - **Why it matters:** As long as 100% of training examples are YES-side picks the bug is masked. The moment shadow promotion serves a NO-recommendation segment, the model returns P(selected-won) but the runtime stores/derives edge as P(YES). Recommendations on the NO side will be silently flipped. This is currently latent but ships-blocking before any promotion.
  - **Fix:** Either (a) standardize to YES probability everywhere — change the training target to YES-won, regenerate manifests; or (b) carry an explicit `target = "yes" | "selected_side"` flag in the manifest and have the runtime invert when the recommendation side is NO. (a) is simpler. Add a golden test where a NO-side prediction wins and verify serving still emits the correct YES probability.
  - **Resolution notes (PRs #25 + #26):** Implemented (a). `dataset.py` now uses an XNOR over `side` and `prediction_outcome`. Training manifests declare `metadata.target_type = "yes_won"`. Runtime validates the field and refuses legacy artifacts (parlay scope exempted — codex caught this on round 2). PnL fallback in `_metrics_for_predictions` derives from `prediction_outcome` (not target). Scoring confidence converts to selected-side probability in ML mode. Training-metric edge ranking uses selected-side probability. Model retrained on 2026-05-12 with all 4 active families.

---

### HIGH

- [x] **3. MLB strikeout prop applies "pitcher dominance" suppressor in the wrong direction** `[claude] [✓]` · effort: **S** · **shipped: PR #27**
  - **Where:** `apps/api/app/services/heuristic_factors.py:81` (`"strikeouts": ("k_rate_factor", "pitcher_dominance_factor")`) and `:193-201` (`_mlb_pitcher_dominance_factor` returns `0.30/csw` — < 1 when CSW is high).
  - **Why it matters:** For batter hits/HR/walks, a dominant pitcher *should* suppress expected output (correct). For batter strikeouts, a dominant pitcher should *raise* expected K's. The factor is currently multiplied as a suppressor, partially canceling the correctly-oriented `k_rate_factor`. Worst on the segment with the most predictable edge (high-CSW starters).
  - **Fix:** Remove `pitcher_dominance_factor` from the `"strikeouts"` tuple — `k_rate_factor` already captures the upward signal. Add a test: `compute_advanced_factors("MLB", "strikeouts", {"opposing_starter_csw_pct": 0.35, "opposing_starter_k_per_9": 11.0})` should yield ≥ 1.0.
  - **Resolution notes (PR #27):** Removed pitcher_dominance from the strikeouts tuple. Codex flagged a partial-cache regression (Statcast warm but sabermetrics not yet ingested → no signal at all). Added `_mlb_strikeout_dominance_factor` as a guarded fallback amplifier that fires only when K/9 is unavailable, using CSW or whiff. Quantified impact: a dominant pitcher (CSW=0.35, K/9=11.0) went from a 0.986× net multiplier (suppressive!) to 1.15× (the high-side clamp).

- [ ] **4. MLB park & weather features are wired but receive no real data** `[codex] [✓]` · effort: **M**
  - **Where:** `apps/api/app/clients/espn.py:180` (ESPN normalization emits no top-level `venue_id`); `apps/api/app/services/scoring.py:689` (`_event_venue_context` only emits name/city/state/indoor); `apps/api/app/services/scoring.py:1648` (`venue_id = (event.raw_data or {}).get("venue_id")` → always `None`); weather call uses `lat=None, lon=None, game_time_utc=None, allow_network=False`.
  - **Why it matters:** Park factors and weather pipelines are fully plumbed (helpers, feature emitters, factor functions in `_MLB_FACTORS_BY_STAT`) but `load_park_factors(None)` defaults to 1.0 and `load_weather(...)` with no coords/time returns empty. Every MLB prediction silently runs without park or weather signal — exactly the kind of edge source the user explicitly wants.
  - **Fix:** (1) Add `venue_id` to ESPN normalization at line 180 (`raw_event["competitions"][0]["venue"]["id"]`). (2) Persist `venue_lat`, `venue_lon`, `game_time_utc` in `_event_venue_context` so weather has real inputs. (3) Decide if `allow_network=True` is acceptable in the synchronous scoring path or if a `weather_refresh` job should pre-warm (see smarter backlog item 14).

- [x] **5. Parlay combined probability assumes independence even for same-game / same-team legs** `[claude]` · effort: **M** · **shipped: PR #31**
  - **Where:** `apps/api/app/services/parlays.py`. Dependence penalties currently only adjust *confidence*, not the combined probability used for edge.
  - **Why it matters (corrected direction):** A 3-leg parlay built from "LeBron over points + LeBron over rebounds + Lakers ML" has heavily correlated legs. The original audit framing said multiplying overstates the joint — that's only true for *negatively* correlated legs (mutually exclusive outcomes). For the parlays sika constructs (same player, same team, shared opponent — all positively correlated), probability theory guarantees `P(A∩B) ≥ P(A) × P(B)`, so multiplying as if independent **UNDERSTATES** the joint → understates edge → systematic **under-recommendation** of correlated parlays. Real edge from same-game-parlay opportunities (where Kalshi prices each leg independently, no SGP discount) gets filtered out before the watchlist ever shows it.
  - **Fix:** Replace the strict-product combiner with a correlation-aware combiner. Blend between the strict product (lower bound, independence) and the minimum leg probability (upper bound, since `P(A∩B) ≤ min(P(A), P(B))`). Correlation factor scales with shared-subject/team/opponent pair counts. Long term: see [Make-Sika-Smarter item 8](#make-sika-smarter-consolidated).
  - **Resolution notes (PR #31):** New `_correlation_adjusted_joint_probability` blends `independent + factor × (min_leg − independent)`. Per-pair weights: 0.70 for shared subject, 0.30 for same team, 0.20 for shared opponent. Hard cap on factor at 0.85. Codex round-1 caught a `max_leg` upper-bound bug (impossible since `P(A∩B) ≤ min`); round-2 fix re-anchored on `min_leg`. Codex round-2 also flagged "wrong direction" relative to the original punch-list framing — verified the framing was backwards (positive correlation understates, not overstates); standing by the math.

- [ ] **6. `GET /positions` makes 3+ live Kalshi calls per request, sync, blocking, uncached** `[claude]` · effort: **M**
  - **Where:** `apps/api/app/api/routes.py` portfolio routes.
  - **Why it matters:** The portfolio page polls `/positions` every ~15 seconds. Each request fans out to balance + open positions + fills against Kalshi. Synchronous, blocking, no cache. Adds 3× Kalshi RPM, makes the FastAPI worker block on remote latency, and any 429 cascades into UI errors.
  - **Fix:** Cache the three responses with a short TTL (5–10s) keyed by user/session; serve from cache and trigger a stale-while-revalidate refresh. Or coalesce in-flight requests so concurrent callers share one upstream call.

- [ ] **7. Trade-desk monotonicity clamp mutates probability in place but never recomputes edge** `[claude]` · effort: **S**
  - **Where:** `apps/web/components/trade/*` (monotonicity clamp logic) + the prediction objects it mutates.
  - **Why it matters:** When monotonicity adjusts a prop probability down, the displayed `edge` field was computed from the pre-clamp probability. UI shows stale edge → operator sees a more attractive trade than the model actually believes in.
  - **Fix:** Either recompute `edge = probability - implied_yes_price` after the clamp, or hide `edge` until the clamp recalculates it.

- [ ] **8. Watchlist `latest_*_by_market_id` helpers select by `max(id)` instead of `captured_at`** `[both — claude HIGH, codex MEDIUM]` · effort: **S**
  - **Where:** `apps/api/app/services/watchlist_coverage.py` (3 helpers); `apps/api/app/services/trade_desk.py`.
  - **Why it matters:** `max(id)` only equals "latest captured" while inserts are strictly monotonic by capture time. Out-of-order ingest (retry queue, backfill, concurrent writes) breaks the invariant and serves a not-actually-latest snapshot to scoring.
  - **Fix:** Order by `captured_at DESC, id DESC`. Best done as one window-function helper applied to all four call sites — see [Architecture rewrite #4](#also-worth-tracking-single-pr-refactors-smaller-than-full-rewrites).

- [ ] **9. Monotonicity repair leaves recommendations active when adjusted edge falls below the minimum floor** `[both]` · effort: **S**
  - **Where:** `apps/api/app/services/scoring.py` (`_enforce_prop_monotonicity` / `_apply_prediction_monotonicity`).
  - **Why it matters:** Monotonicity sometimes lowers probability enough that the implied edge drops below `watchlist_min_edge`, but the recommendation stays on the watchlist. Operators get picks the system would have filtered out if the lowered probability had been the original.
  - **Fix:** After monotonicity adjustment, recompute edge and drop the recommendation if `adjusted_edge < watchlist_min_edge`.

- [ ] **10. Timed-out refresh workers keep running and can commit domain writes after the job is marked failed** `[both]` · effort: **L**
  - **Where:** `apps/api/app/services/refresh_jobs.py:699` (`process_refresh_job_queue_once`).
  - **Why it matters:** `daemon=True` thread + `done_event` wait. On timeout the job row is flipped to `failed`, but the worker thread keeps running and writing to the same tables. Next tick can claim a *new* job that races with the stale writer. Partial product-visible state survives across jobs.
  - **Fix:** See [Architecture rewrite #3](#also-worth-tracking-single-pr-refactors-smaller-than-full-rewrites). Short-term band-aid: gate every per-phase commit in the worker on a `cancelled` flag set when the watcher times out.

- [ ] **11. Refresh-job singleton claiming is race-prone across workers** `[codex]` · effort: **M**
  - **Where:** `apps/api/app/services/refresh_jobs.py`.
  - **Why it matters:** Two workers (or worker + scheduler in dev/prod overlap) can both pass the "is anyone running?" check and claim the same job kind in parallel.
  - **Fix:** Use a DB-level advisory lock (Postgres `pg_advisory_xact_lock`) or a `SELECT … FOR UPDATE SKIP LOCKED` on the claim row. Add a concurrency test with two simulated sessions.

- [ ] **12. Settlement processes only the latest prediction per ticker/scope/side — older predictions stay `pending` forever** `[codex]` · effort: **M**
  - **Where:** `apps/api/app/services/predictions.py` settlement path.
  - **Why it matters:** Two predictions made at different times on the same ticker+side never both get settled. The earlier one is orphaned. Distorts hit rate, calibration data, and any retroactive PnL.
  - **Fix:** Settle every unresolved prediction for the ticker, not just the latest. Add a regression test with two stacked predictions.

- [ ] **13. Player prop resolution ignores team hints during ESPN player search** `[codex]` · effort: **S**
  - **Where:** ESPN player-search path used by prop scoring.
  - **Why it matters:** ESPN's first-result match can return the wrong "John Smith" when two same-name players exist across teams. Wrong-player attribution → wrong features → wrong edge.
  - **Fix:** Filter candidates by team abbreviation before returning. Log when the team hint changes the resolved athlete.

- [x] **49. NO-side recommendations aren't directly actionable on Kalshi** `[user-surfaced]` · effort: **M** · **shipped: PR #28**
  - **Where:** `apps/api/app/services/scoring.py:2236-2245` (side selection in `_build_scored_recommendation`); 252 historical NO-side recommendations in DB confirm this is a real, recurring pattern (NBA + MLB winner markets).
  - **Why it matters:** Kalshi only offers YES contracts — you cannot bet NO directly. When sika's scoring picks `side="no"` (which happens whenever `no_edge > yes_edge`), the resulting recommendation is surfaced in the UI as a pick the user can't trade. For game-winner markets the actionable equivalent is "YES on the opposite team" (a paired market with a related ticker); for player props the NO side has no clean YES counterpart.
  - **Impact:** Every NO recommendation is a wasted UI slot. Operator either ignores it or has to mentally translate, which defeats the point of having a copilot.
  - **Resolution notes (PR #28):** Shipped the simpler, more conservative resolution — suppress NO-side recommendations rather than synthesize a translated YES on the opposite market. The paired market's YES scoring runs independently and surfaces the actionable equivalent, so no signal is lost in the common winner-market case. Added `no_side_not_actionable_on_kalshi` suppression reason with a corresponding outcome counter so ops surfaces can attribute the suppression. `_dedupe_winner_recommendations` becomes a no-op for the paired-winner case (its only remaining role is combo-derived market dedup); could be retired in a follow-up.

---

### MEDIUM

- [ ] **14. Public list endpoints accept unbounded or negative limits** `[both]` · effort: **S**
  - **Where:** `apps/api/app/api/routes.py` — multiple list endpoints.
  - **Fix:** Add `Query(ge=1, le=…)` to every `limit` parameter.

- [ ] **15. Paper/demo schemas accept arbitrary strings; NO-side PnL is brittle to wrong-side `exit_price`** `[both] [✓ partial]` · effort: **S**
  - **Where:** `apps/api/app/schemas.py:622`; `apps/api/app/services/orders.py:13, 41, 46`.
  - **Why it matters:** Schemas accept any string for `side`, `action`, `time_in_force` and lowercase them without validating an enum. PnL formula `(exit - entry) * qty` is **correct** when `exit_price` is the same side as entry — but no schema or caller enforces that. Verified: web caller at `paper-positions-table.tsx:78` passes whatever the user types. Any future integration that mis-passes (e.g. enters YES close price for a NO position) will silently invert PnL.
  - **Fix:** `Literal["yes","no"]` / `Literal["buy","sell"]` / time-in-force enum. Document & enforce: `exit_price` is the same-side contract price. Or accept both prices and derive PnL from the side. Tests for YES + NO round-trip.

- [ ] **16. ML training imputation fits medians on the full dataset before train/test split** `[both]` · effort: **S**
  - **Where:** `apps/ml/ml/dataset.py` / `training.py`.
  - **Why it matters:** Holdout metrics leak full-dataset information; reported Brier/log-loss is optimistic. Promotion gate is downstream of this leak.
  - **Fix:** Compute imputation statistics inside the training fold only; apply to held-out fold without refitting.

- [ ] **17. Market mapping uses a low-confidence fuzzy best match with no ambiguity record or override** `[codex]` · effort: **M**
  - **Where:** `apps/api/app/services/market_mapping.py:54`.
  - **Why it matters:** Wrong Kalshi-ticker → sika-event mapping silently corrupts every downstream feature for that market. Doubleheaders, abbreviation collisions, postponed games are particularly vulnerable.
  - **Fix:** Persist mapping confidence + candidate list; require ≥ threshold; add an ops endpoint to override. See [Architecture rewrite #4 — market/player resolution service](#architecture--rewrite-candidates).

- [ ] **18. Kalshi market discovery caps default scans at 5,000 markets** `[codex]` · effort: **S**
  - **Where:** Kalshi market-discovery loop.
  - **Why it matters:** Buried NBA/MLB tickers past the 5K cap never get discovered.
  - **Fix:** Raise the cap with pagination, or paginate until exhausted with a hard wall-clock budget.

- [ ] **19. Runtime retention deletes the prediction history needed for learning, calibration, and promotion** `[codex]` · effort: **M**
  - **Where:** `apps/api/app/services/predictions.py` retention; runtime cleanup.
  - **Why it matters:** The same rows that the system would use to fit calibrators, compute reliability curves, or run a backtest get reaped. Eats its own training data. Cross-reference: the 2026-05-12 retrain only had 1,714 training rows after `dedupe_markets=True` collapsed 23,000+ settled predictions.
  - **Fix:** Two-tier retention: short TTL for product UI; longer (or infinite) archive for ML use. Move runtime cleanup off the ML-relevant tables.

- [ ] **20. Model evaluation is optimistic; promotion runs on thin, non-walk-forward evidence** `[codex]` · effort: **L**
  - **Where:** `apps/ml/ml/training.py`, promotion gate.
  - **Why it matters:** Single split + small sample + no time-aware fold = a number that passes promotion but does not generalize.
  - **Fix:** Walk-forward evaluation by date. Promotion gate consumes the worst-fold Brier, not the average. Minimum settled-row floors per family.

- [ ] **21. Weekly model retraining runs inside the API worker and likely skips in prod** `[codex]` · effort: **M**
  - **Where:** Scheduler's `_weekly_model_retrain_job`.
  - **Why it matters:** Retraining requires the ML sibling repo + writable artifact path. On Render (or any FastAPI-only deploy) the path isn't there, so the job silently no-ops.
  - **Fix:** Move retraining to a separate offline job runner (CI cron, GitHub Action, dedicated worker). API only serves manifests it didn't produce. See [Architecture rewrite #3 — ML training & promotion pipeline](#architecture--rewrite-candidates).

- [ ] **22. Transient Kalshi prop-refresh errors requeue indefinitely with no attempt cap or backoff** `[codex]` · effort: **S**
  - **Where:** Prop refresh job error path.
  - **Why it matters:** A persistent upstream issue churns the queue forever and drowns log signal.
  - **Fix:** Per-job `attempts` counter + exponential backoff + dead-letter after N tries.

- [ ] **23. Unbounded all-row scans in ingestion / watchlist paths** `[codex]` · effort: **S**
  - **Where:** `apps/api/app/services/ingestion.py:516`; `apps/api/app/services/market_mapping.py:54`; `apps/api/app/api/routes.py:1106`.
  - **Why it matters:** Loads every open market into memory to filter by `raw_data` JSON keys. O(all-open-markets) on every refresh tick.
  - **Fix:** Push the JSON filter into the SQL query (`Market.raw_data["copilot_market_family"].as_string() == "player_prop"`) and/or add an indexed column.

- [ ] **24. SWR cache-key bugs in the web app** `[claude]` · effort: **S**
  - **Where:** `apps/web/lib/api.ts` and key builders (`keys.predictions`, `keys.events`, `keys.tradeDesk`).
  - **Why it matters:** (a) Args serialized via `Object.entries` insertion order — same logical args produce different keys, causing duplicated fetches and cache misses. (b) `keys.tradeDesk(sport)` produces a different key than `keys.tradeDesk()` even when both mean "all sports."
  - **Fix:** Sort keys before serializing. Normalize "all sports" to a single canonical key.

- [ ] **25. `_remote_market_lookup` issues sequential paged Kalshi calls inside a request** `[claude]` · effort: **S**
  - **Where:** market-mapping remote lookup.
  - **Fix:** Parallelize the page fetches with a bounded gather, or precompute and cache.

- [ ] **26. Sidebar drag listener and starfield animation run unconditionally — battery / CPU drain** `[claude]` · effort: **S**
  - **Where:** `apps/web/components/layout/*`, starfield canvas component.
  - **Fix:** Pause the starfield on `document.visibilityState === "hidden"`. Attach the drag listener on pointerdown, detach on pointerup.

- [ ] **27. Settlement summary writes "updated" on unchanged unresolved rows** `[both]` · effort: **S**
  - **Where:** Settlement integration.
  - **Why it matters:** Inflates ops counters, masks actual settlement throughput.
  - **Fix:** Compare before/after; only count rows whose state changed.

- [ ] **28. `/positions` returns all paper positions and demo orders without pagination** `[claude]` · effort: **S**
  - **Fix:** Add `limit` + cursor pagination.

- [ ] **29. Two copies of `features.py` between `apps/api/app/services/ml/` and `apps/ml/ml/`** `[claude]` · effort: **M**
  - **Why it matters:** Train/serve skew risk — they can't drift today but the contract is implicit.
  - **Fix:** Single source of truth (shared package, symlink, or CI byte-equality check). See [Architecture rewrite R4](#also-worth-tracking-single-pr-refactors-smaller-than-full-rewrites).

- [ ] **30. Duplicated `visible_sports` / `_visible_sports` and `SPORT_TINTS` maps** `[claude]` · effort: **S**
  - **Where:** `apps/api/app/services/scoring.py` + `apps/web/components/*` mirrors.
  - **Fix:** Single source in the contracts package; web imports it.

- [ ] **31. `submit_demo_order` is not transactional with Kalshi side-effects** `[claude]` · effort: **M**
  - **Where:** `apps/api/app/services/orders.py`.
  - **Why it matters:** If the local DB write succeeds but the Kalshi call fails (or vice versa) the two states diverge silently.
  - **Fix:** Outbox pattern or compensating action. At minimum, log the divergence and surface a reconcile path.

- [ ] **32. `_prune_current_slate_snapshots` skips deletion when every scope row is a survivor** `[claude]` · effort: **S**
  - **Fix:** Always run the delete with the survivor set; let it no-op when survivor == all.

---

### LOW

- [ ] **33. `process_refresh_job_queue_once` daemon-thread cancellation gap** `[both]` (subset of #10; track separately for the cleanup phase) · effort: **S**
- [ ] **34. `/watchlist` filters in Python *after* limit — can return fewer than `limit`** `[codex]` · effort: **S**
- [ ] **35. `triggerRefreshAndRevalidate` polls for up to 40 min without an abort signal** `[claude]` · effort: **S**
- [ ] **36. Hydration mismatch on first paint when `PriceDisplayMode` is non-default** `[claude]` · effort: **S**
- [ ] **37. `randomWalk` sparkline drifts on re-render via `seed_from_string`** `[claude]` · effort: **S** (fake price history — replace with real captured prices)
- [ ] **38. Sidebar drag listener hijacks pointer events globally** `[claude]` · effort: **S**
- [ ] **39. Contract drift check is not portable; failed before comparing** `[codex]` · effort: **S**
- [ ] **40. Web contracts split between generated OpenAPI types and hand-written mirrors** `[both]` · effort: **M** (see [Architecture rewrite #6 — contract/type ownership](#architecture--rewrite-candidates))
- [ ] **41. Stale ML family registry duplicates the runtime registry** `[codex]` · effort: **S**
- [ ] **42. `parlay_4_6_leg_combiner` is registered but `study_track` is `heuristic_only`** `[claude]` · effort: **S**
- [ ] **43. `_session_predictions` walks all in-memory predictions per `capture_prediction` call** `[claude]` · effort: **S**
- [ ] **44. Probable-pitcher extraction relies on `hydrate=probablePitcher` being honored by ESPN** `[claude]` · effort: **S**
- [ ] **45. `triggerPredictionSettlement` does not invalidate caches keyed off `/watchlist/coverage` or `/trade-desk`** `[claude]` · effort: **S**
- [ ] **46. `_account_error_message` masks all Kalshi error details from the operator** `[claude]` · effort: **S**
- [ ] **47. Startup refresh failures are swallowed** `[codex]` · effort: **S**
- [ ] **48. Default Kalshi private-key path is hardcoded to a user-specific location** `[codex first-pass]` · effort: **S**

---

#### 48a. (Deferred) Anonymous mutating endpoints

Originally bug #1 (CRITICAL) in both audits. Demoted to **deferred** based on actual deployment topology:

- API binds to `127.0.0.1:8000` ([scripts/api-dev.sh](scripts/api-dev.sh)) — localhost only.
- No active Vercel or Render deployment. [`render.yaml`](render.yaml) exists but is not in use; user has no plan to enable it.
- Only the user (localhost) and Canaan (via Tailscale, read-only) can reach the API. Both are trusted.

**Risk surface that still exists:**
- If [`render.yaml`](render.yaml) is ever flipped on without auth-first, the API goes public the same day. Consider deleting `render.yaml` if Render is truly retired.
- If Tailscale Funnel or any other public proxy is ever pointed at this API, same exposure.

**When to revisit:** the moment a public deploy is on the table. Until then, this stays deferred. Owner-token gate sketch remains valid: `Depends(require_owner_token)` on `/ops/*`, `/demo-orders`, `/paper-positions`, `/portfolio/*`. Read-only endpoints stay open.

---

## Dead Code to Remove

UFC is removed from scope. Claude's audit enumerated ~15 specific files/lines across:

- [ ] **50. UFC ingestion / scoring paths in `apps/api`** (clients, services, sport detection)
- [ ] **51. UFC components / sport pills / route handlers in `apps/web`**
- [ ] **52. UFC entries in `packages/contracts` sport enums**
- [ ] **53. UFC fixtures in `apps/api/tests` and `apps/web/test`**
- [ ] **54. UFC sport tint / CSS in design tokens**

---

## Make Sika Smarter (consolidated)

Ordered by what would move the needle most given fixed engineering time. Each item: WHAT / WHY / WHERE / EFFORT / SPORT.

1. **[shipped]** **Per-family, per-price-bucket calibration tracking with reliability curves** — **WHY:** aggregate Brier hides bucket-level miscalibration (e.g. 8-point over-confidence in the 60–70% band). Without this, every other model upgrade is unverifiable. **WHERE:** `apps/api/app/services/ml/readiness.py`, `apps/web/components/predictions/model-readiness-panel.tsx`, new `prediction_calibration_buckets` table. **EFFORT:** M. **SPORT:** both. **Depends on:** bug #19 (retention archive). **Shipped:** Smarter #1 — 10 reliability buckets (0-10% … 90-100%) per family with `avg_predicted`, `actual_yes_rate`, and `miscalibration = avg_predicted - actual_yes_rate`. Computed live from the `READINESS_ROW_LIMIT` row sample (bug #19's retention archive keeps every settled prediction so the table-based approach was redeferred until the row sample stops being representative). Reliability curve rendered as inline SVG + tabular breakdown on the model-readiness panel. Codex-pattern-2 catch surfaced: `fair_yes_price` / `side` / `combined_model_probability` had to be added to the `load_only` projection or each row would trigger a per-attribute lazy SELECT.

2. **Walk-forward backtesting harness over historical Kalshi candlesticks** — **WHY:** the guardrail against train/serve skew, leakage, and models that only look good on random splits. Without it we can't safely tune `watchlist_min_edge` or compare scoring revisions. **WHERE:** new `apps/ml/ml/backtest.py`; pull `get_historical_market_candlesticks`; replay slates against the scoring kernel. **EFFORT:** XL. **SPORT:** both. **Depends on:** historical Kalshi data pull + learning archive.

3. **[shipped]** **Closing-line value tracking** — **WHY:** CLV is the gold standard for sharp performance. If recommendations consistently get closing-line beat, sika is sharp; if they move away, it's noise. Cheapest sanity check we don't currently have. **WHERE:** extend `apps/api/app/services/market_history.py`; track close-time prices in `market_snapshots`; compute `closing_line_value` per recommendation in `predictions.py` at settlement. Surface in readiness panel. **EFFORT:** L. **SPORT:** both. **Shipped:** Smarter #3 — new `apps/api/app/services/clv.py` module with `closing_yes_price_for_market` (latest `MarketSnapshot` mid-or-last before close), `compute_clv` (signed delta with YES/NO sign convention), `average_clv` (family aggregation). `Prediction.closing_yes_price` and `Prediction.closing_line_value` columns persisted at settlement (one-shot guarded — preserves the authoritative original close). `average_clv` surfaced as an "Avg CLV" tile on the readiness panel for singles only (parlay CLV deferred; per-leg composite requires a separate spec). Codex catches: Pattern 2 N+1 storm on both summary AND detail `load_only` paths; Pattern 1 idempotency guard for re-settlement; Pattern 8 UX disambiguation in the empty-state tooltip.

4. **MLB venue → park-factor → weather pipeline (real)** — **WHY:** today the pipeline is fully plumbed but inactive (see bug #4). Park and weather materially change HR, total bases, runs, first-five. **WHERE:** ESPN normalization (`venue_id`, lat/lon, game_time_utc), `mlb_advanced.py`, `scoring.py:1648`, new `weather_refresh` job that pre-warms. **EFFORT:** L. **SPORT:** MLB. **Depends on:** bug #4 fix.

5. **[shipped]** **MLB probable-starter handedness × batter platoon splits** — **WHY:** LHB vs LHP suppresses hits ~10–15%, bigger than most multipliers already applied. Today's `_mlb_starter_factor_advanced` ignores handedness. **WHERE:** extend `load_player_splits` to ingest `vL`/`vR` splits; new `batter_vs_starter_platoon_factor` feature gated on `hits`/`home_runs`/`total_bases`/`rbis`/`runs`. **EFFORT:** M. **SPORT:** MLB. **Shipped:** Smarter #5 — new `emit_mlb_platoon_features(starter_pitch_hand, splits_payload, season_ops)` in `mlb_advanced.py` walks `vsLeft`/`vsRight` rows from the cached splits payload, computes the ratio of vs-hand OPS to season OPS, clamps to `[0.80, 1.20]`. New `extract_pitch_hand_from_lineup(lineup_payload, pitcher_id)` reads `pitchHand.code` from the schedule's `probablePitcher` block. New `_mlb_batter_platoon_factor` factor in `heuristic_factors.py` consuming `batter_vs_starter_platoon_factor`, gated on `hits`/`home_runs`/`total_bases`/`rbis`/`runs`. Scoring path wires all three: resolve starter id (already done) → extract pitch hand from lineup payload → load splits cache (allow_network=False) → emit features. 43 new tests in `test_mlb_platoon_splits.py` covering normalize/split-row/emit/extract/factor/gating with clamp + missing-data + non-offense-stat scoping.

6. **[shipped, partial]** **MLB bullpen state and IP-to-bullpen edge** — **WHY:** game-line totals and 2nd-half props are highly sensitive to "tired bullpen on day 4 of a long road trip." `mlb_bullpen_state_cache` exists but isn't feature-wired. **WHERE:** new `team_bullpen_rest_index_recent_3` feature; wire into game totals + first-five scoring. **EFFORT:** M. **SPORT:** MLB. **Shipped:** Smarter #6 (Phase 1) — `count_team_games_in_window(db, participant_id, end_at, days=3)` in `mlb_advanced.py` queries the local `Event` + `EventParticipant` tables for completed games in the 3-day window (strict less-than `end_at` so same-day re-eval doesn't count the game being scored). `bullpen_rest_index_from_games` converts the count to `[0.0, 1.0]` (0 games → 1.0, 3+ → 0.0). `emit_mlb_bullpen_features` emits per-side indices + a `bullpen_rest_data_complete` flag. New `_mlb_opposing_bullpen_rest_factor` factor — linear ±5% multiplier centered at neutral. Gated on **batter offense stats** (`runs`, `rbis`) where bullpen rest matters most. Scoring path threads the opposing team's recent-game count into `opposing_bullpen_rest_index_3d`. **Phase 2 follow-up:** game-line totals + first-five scoring integration (those score via different paths from the batter-prop factor pipeline); per-game reliever-IP ingestion from MLB Stats boxscores for a more accurate signal than schedule density. 27 new tests in `test_mlb_bullpen_rest.py`.

7. **MLB park × weather interaction term for HR / total bases** — **WHY:** park and weather are currently independent multipliers; the interaction is real (Coors + 90°F + wind-out is a different beast than each alone). **WHERE:** `heuristic_factors.py` — new `_mlb_park_weather_hr_factor`. **EFFORT:** M. **SPORT:** MLB. **Depends on:** smarter #4 + bug #4.

8. **Correlation-aware parlay engine** — **WHY:** addresses bug #5 at the root. Estimate correlation among same-game props, team outcomes, and player props before ranking parlays. **WHERE:** `apps/api/app/services/parlays.py`, shadow parlay inference, ML parlay families. **EFFORT:** L. **SPORT:** both. **Depends on:** smarter #1 (calibration warehouse) + enough settled parlay/single history.

9. **Fractional Kelly position sizing with floor/ceiling, bankroll-aware, correlation-aware, drawdown brakes** — **WHY:** better probabilities don't translate to better results if stake sizing overexposes correlated edges. **WHERE:** new `recommendation.suggested_size_fraction = kelly * 0.25` with `min 0.005, max 0.02` of bankroll; per-event cap aggregating correlated legs; drawdown brake checks rolling 7-day PnL. Surface in `apps/web/components/trade/trade-ticket.tsx`. **EFFORT:** L. **SPORT:** both. **Depends on:** bankroll input.

10. **[shipped]** **NBA rest, travel, back-to-backs, schedule density** — **WHY:** NBA props and game outcomes are strongly affected by fatigue, travel, B2B. **WHERE:** event ingestion, participant features, `scoring.py`, ML feature emitters. **EFFORT:** M. **SPORT:** NBA. **Shipped:** Smarter #10 — `_schedule_context` now also returns `games_last_3`, `games_last_5`, `is_third_in_four` (3rd game in 4 nights — the classic NBA fatigue trigger), `is_fourth_in_six` (rarer but punishing), and `last_game_away`. Wired through the prop-features dict in `_score_player_prop` alongside `team_is_home`. Two new heuristic factors in `heuristic_factors.py`: `_nba_rest_factor` (4th-in-6 → 0.94, 3rd-in-4 → 0.96, ≥3 days rest → 1.02, deadband → 1.0; suppressors win over rest boost) and `_nba_travel_factor` (continuous road trip → 0.98, else 1.0 — Phase 1 only, Phase 2 will use venue-coord mileage à la Smarter #15). Gated on points/rebounds/assists/made_threes/three_points_made/field_goals_made/PA/PR/RA/PRA. Game-line winner path (`_score_team_winner`) untouched — still uses the existing binary `back_to_back_edge`. Drift guard + parametrized per-stat gating tests in `test_nba_schedule_density.py` (39 new). SQLite TZ caveat noted in fixtures: anchors are naive because SQLite drops tz info on read; in Postgres both sides stay aware. Code-reviewer pass: APPROVE with 2 MEDIUM info notes (deliberately-loose `>=` thresholds and the pre-existing fact that per-factor 0.85 clamps don't bound the product of factors).

11. **[shipped]** **NBA load-management / workload heuristic** — **WHY:** a star at 22% chance of "rest day" is the single biggest source of large NBA prop misses. Today only `back_to_back_edge` exists. **WHERE:** new `recent_workload_minutes_per_game`, `consecutive_games_played` features in `advanced_stats.py`; require *more than* lineup confirmation when workload is top-quartile. **EFFORT:** M. **SPORT:** NBA. **Shipped:** Smarter #11 — `emit_nba_workload_features(game_logs, *, window_games=5)` in `advanced_stats.py` emits `recent_workload_minutes_per_game` (mean MIN over last 5 games where the player saw the floor — DNPs filtered out of the mean), `consecutive_games_played` (streak count from most-recent game, breaks at first DNP; INACTIVE games are absent from the ESPN gamelog so don't break the streak), and `workload_data_complete`. Heuristic `_nba_workload_factor` gated on points/rebounds/assists/made_threes/three_points_made/field_goals_made/PR/PA/RA/PRA (skipped for steals/blocks/turnovers per Pattern 3 — those aren't fatigue-suppressed): ≥34 MPG → 0.96, ≤22 MPG → 1.03, deadband 1.0. Scoring kernel adds `workload_top_quartile_uncertainty` to `missing_context` when lineup IS confirmed AND MPG ≥34 — gated to `family_key == "nba_props"` for Pattern 9 cross-scope safety. Also added `workload_data_complete` AND a backfilled `bullpen_rest_data_complete` (PR #78 oversight — symmetry test had been failing on main) to `ADVANCED_COMPLETENESS_MARKERS`. Code-reviewer subagent independent pass: APPROVE, 0 CRITICAL/HIGH issues. 26 new tests cover edge cases (empty/all-DNP/single-game/rookie/streak-walks-full-log) plus full scoring-integration matrix (confirmed-in-lineup, scratch, pre-lineup, MLB-cross-scope). The `consecutive_games_played` feature is emitted for ML training Phase 2; no heuristic consumer yet.

12. **[shipped]** **NBA usage-rate × pace × opponent-defense interaction feature** — **WHY:** today these are independent multipliers capped at 0.85–1.15 — understates extreme combinations. Let the ML model learn the shape. **WHERE:** `heuristic_factors.py` add `_nba_interaction_factor`; emit a single uncapped feature. **EFFORT:** M. **SPORT:** NBA. **Shipped:** Smarter #12 — `emit_nba_interaction_term` in `advanced_stats.py` emits a single UNCAPPED feature `nba_offense_interaction_term` = `(usage / 0.25) × (pace / 100) × (drtg / 110)`. Centered so league-average inputs (~0.25 / 100 / 110) produce 1.0. NO heuristic factor consumes it — the handoff explicitly defers the multiplicative shape to the ML model (next retrain picks it up automatically via dynamic `build_feature_spec`; no allowlist in `dataset.py`). Drift guard in opposite direction: assert `nba_offense_interaction_term` is NOT in `_NBA_FACTORS_BY_STAT` / `_NBA_FACTOR_FNS`. Codex Pattern 9: emission gated to NBA scoring block only. **Important direction-fix:** the original handoff pseudocode had `110 / drtg` which inverted the established `_nba_opp_def_factor` convention (where elite defense suppresses output). Code-reviewer flagged this as HIGH; fixed to `drtg / 110` so elite defense (DRtg=100) yields ~0.91 (suppression) and weak defense (DRtg=120) yields ~1.09 (boost). 15 new tests including a direction-pin test asserting the convention. NOT added to `ADVANCED_COMPLETENESS_MARKERS` per the handoff (the underlying inputs already have their own markers).

13. **NBA referee tendencies for total-points / fouls / FT props** — **WHY:** ref crews are statistically distinct (Tony Brothers ~3 fewer fouls/game than Scott Foster). Right now no ref signal exists. **WHERE:** new `nba_referee_cache` from `official.nba.com/referee-assignments`; new `crew_chief_total_points_adj` feature. **EFFORT:** L. **SPORT:** NBA.

14. **Event-aware scheduler bursts** — **WHY:** a 5-min refresh is wasteful at 11am and inadequate at 7:55pm. Refresh velocity should scale inversely with time-to-tip/first-pitch. **WHERE:** `scheduler.py` — replace static `IntervalTrigger` for `live_refresh_due_check` with a dynamic trigger that bursts to every 60s within T-30min. **EFFORT:** M. **SPORT:** both.

15. **[shipped]** **MLB game-day morning weather pre-warm** — **WHY:** `weather_refresh` is declared as a placeholder; first prop scored on a game-day pays the latency cost. **WHERE:** implement `weather_refresh` to walk the MLB slate and pre-warm `MlbWeatherCache`. **EFFORT:** S. **SPORT:** MLB. **Shipped:** Smarter #15 — new `_MLB_PARK_COORDS` table + `mlb_park_coords(team_abbreviation)` resolver in `mlb_advanced.py`. `weather_refresh` job in `refresh_jobs.py` now fetches today's MLB schedule, matches each game to a sika `Event`, looks up home-park coordinates via `mlb_park_coords` (routing ESPN's 2-letter codes through `_ESPN_TO_PARK_FACTORS_TEAM_ALIAS`), and calls `load_weather(allow_network=True)`. Job details now carry `events_warmed`, `events_dome`, `events_missing_coords`, `games_unmatched`, `games_failed`, `schedule_failed` counters. Tropicana Field (TBR) is the only `is_dome=True` entry; retractable-roof parks stay open-air so the forecast is captured for the slate where the roof is open.

16. **[shipped]** **Lineup confirmation: suppress, don't penalize, when player not in starting lineup** — **WHY:** today `metadata["copilot_requires_lineup"]` only yields a 0.025 penalty — too lenient when the player has actually been scratched. **WHERE:** `_single_scoring_adjustments` — suppress (not penalize) when `requires_lineup AND lineup_confirmed AND player_not_in_starting_lineup`. **EFFORT:** M. **SPORT:** both. **Shipped:** Smarter #16 — `emit_lineup_features` (MLB) now distinguishes three states: pre-lineup (`{}`), confirmed-and-in (`{batting_order_position, lineup_data_complete: 1.0, player_in_starting_lineup: 1.0}`), and confirmed-and-scratched (`{lineup_data_complete: 1.0, player_in_starting_lineup: 0.0}`). Scoring suppresses with `player_not_in_starting_lineup` reason when the scratched signal fires; pre-lineup keeps the existing missing-context penalty; confirmed-in drops the penalty entirely. NBA stays on the penalty path (no per-player lineup emitter today — deliberate downscope). Codex catches: Pattern 1 reasons/invalidation text gating so the "only valid if confirmed" disclaimer doesn't fire for confirmed-in picks; Pattern 2 ML training markers updated to include `player_in_starting_lineup` for the original "batting-order present" semantic.

17. **Late-breaking injury news ingestion + stale-news gate** — **WHY:** ESPN injury reports update faster than rosters. Player ruled out 60 min before tip should auto-suppress every prop on them. **WHERE:** `advanced_stats.py` / `nba_long_tail.py` injury cache reader — check `report_updated_at`, suppress when `status in ("out","doubtful")`. **EFFORT:** M. **SPORT:** both.

18. **Sportsbook implied-probability sanity check** — **WHY:** sportsbook lines (vig-removed) are a strong external prior. Sika should know when it's disagreeing with broad market consensus and explain why. **WHERE:** new odds client/service; `scoring.py` diagnostics; recommendation filters. **EFFORT:** L. **SPORT:** both. **Provider chosen:** The Odds API (free tier). **API key** held in `apps/api/.env` (gitignored) — never commit to source.

19. **Per-family monotonic GBM** — **WHY:** monotonicity is enforced post-hoc by `_enforce_prop_monotonicity` — fragile. A monotonic GBM with feature direction constraints produces calibrated, monotonic predictions natively. **WHERE:** `apps/ml/ml/training.py`; tag direction per feature in `model_families.py`. **EFFORT:** L. **SPORT:** both.

20. **Per-family isotonic recalibration on rolling 30-day window** — **WHY:** markets drift seasonally (NBA prop markets sharpen over the year). Refit the calibrator monthly. **WHERE:** add a `recalibrate` step to retrain pipeline; refit isotonic on last 30 days; swap via manifest. **EFFORT:** M. **SPORT:** both.

21. **Conformal prediction intervals on prop expected values** — **WHY:** a single point estimate of `expected_stat_output` is fragile to player variability. Conformal intervals give `(p10, p50, p90)` → more honest Over/Under distribution than the current Poisson approximation. **WHERE:** train a quantile regressor in parallel with the GBM; package intervals in artifact. **EFFORT:** L. **SPORT:** both.

22. **Feature freshness SLAs (per group)** — **WHY:** prefer no pick to a pick built on stale lineup, starter, injury, or weather data. **WHERE:** new feature-group metadata: `fresh_at`, `expires_at`, `source`, stale severity. Scoring consumes a normalized bundle. **EFFORT:** M. **SPORT:** both. **Depends on:** [Architecture rewrite #5 — feature freshness layer](#architecture--rewrite-candidates).

23. **[shipped]** **Stale-data detection per upstream source (`/health` integration)** — **WHY:** ESPN scoreboard fails silently, Kalshi 429s, basketball-reference cache expires. Surface per-source freshness in `/health` + operator settings. **EFFORT:** M. **SPORT:** both. **Shipped:** Smarter #23 — new `apps/api/app/services/upstream_health.py` module with `UPSTREAM_SOURCES` canonical 8-source tuple + `UpstreamSourceHealth` frozen dataclass + `record_upstream_success` / `record_upstream_failure` / `get_upstream_health` API. State lives in existing `OperatorSetting` JSON blob (no migration). `last_success_at` / `last_failure_at` both preserved across transitions; `last_error` clears on success so stale messages don't linger after recovery. `is_stale()` defaults to 24h threshold; never-recorded sources are stale by definition (explicit signal). New `UpstreamSourceHealthRead` schema; `/health` response gains `upstream_sources: list[UpstreamSourceHealthRead]`. Wired NBA Stats via the existing centralized `_record_nba_success`/`_record_nba_failure` recorders (one place); `_record_nba_failure` signature gained a keyword-only `error: str | None` and all 7 call sites updated to pass `error=str(exc)`. **Out of scope (follow-up):** ESPN scoreboard, Kalshi markets, basketball-reference, MLB Stats — those sources appear in `/health` with `last_success_at=None` until a follow-up wires their respective loaders (each is a single-line addition at the failure-handling site). Code-reviewer pass: APPROVE, 0 CRITICAL/HIGH/MEDIUM, 1 LOW (alphabetization, fixed). 17 new tests in `test_upstream_health.py`.

24. **[shipped]** **Time-to-close metrics in the watchlist** — **WHY:** operators can't tell which recs are aging out. A T-4h recommendation @ 0.05 edge ≠ T-15min @ 0.05 edge. **WHERE:** add `time_to_close_minutes` to recommendation read schemas; sort/highlight in `apps/web/app/(product)/watchlist/page.tsx`. **EFFORT:** S. **SPORT:** both. **Shipped:** Smarter #24 — `time_to_close_minutes: int | None` field on `RecommendationRead`, `TradeDeskGameLineRead`, and `TradeDeskThresholdRead`. Shared `TimeToCloseBadge` component on game-line rows + on the player-prop card header. Red-bold when ≤30 min, muted under 24h, day-count when ≥24h, "closing" when ≤0, hidden when null. Codex Pattern 7 catch: initial version omitted the badge from prop rows — fix added it to the prop card summary so props get equal-weight urgency. Pattern 2 noted: helper duplicated between `routes.py` and `trade_desk.py` with an explicit agreement test until next refactor consolidates them.

25. **Market mapping confidence + ambiguous-match logging + manual override** — addresses bug #17. **WHERE:** `market_mapping.py` (store score, candidate list, evidence); `/ops/market-mapping/overrides` PATCH endpoint; operator panel in `apps/web/app/(ops)/`. **EFFORT:** M. **SPORT:** both.

26. **[shipped]** **Settled-outcome SLA aging buckets** — **WHY:** predictions stuck in `pending` when Kalshi settlement is delayed. Need aging buckets (0–1h, 1–6h, 6–24h, 24h+) and an alert when a bucket exceeds threshold. **WHERE:** `predictions.py` add settlement-aging endpoint; surface in readiness panel. **EFFORT:** M. **SPORT:** both. **Surfacing:** ops UI badge on the readiness panel for v1. **Shipped:** Smarter #26 — `compute_settlement_aging(db, *, now=None)` in `predictions.py` JOINs `Prediction` × `Market` for rows where `settlement_status='pending'` AND `Market.close_time < now`, then buckets by hours-past-close. Returns `SettlementAging` frozen dataclass with 4 non-overlapping buckets (0-1h, 1-6h, 6-24h, 24h+) plus `total_pending_past_close`. Markets without `close_time` are excluded (we don't know when they SHOULD have settled). New `SettlementAgingRead` schema; `ModelReadinessSummaryRead` gains `settlement_aging: SettlementAgingRead` field (defaults to all-zero so the field is always populated for the UI). Surfaces on `GET /ops/models/readiness`. SQLite TZ caveat: `close_time` comes back naive in tests; helper coerces to UTC for the subtraction. 15 new tests covering each bucket boundary, the no-close-time/future-close-time/already-settled exclusions, and the readiness-summary + endpoint surfaces. **Out of scope:** the UI badge component (frontend follow-up).

27. **[shipped]** **Train/serve feature-dictionary drift detection at inference** — **WHY:** if serving emits a feature key training never saw (or vice versa), the feature vector silently mis-aligns. **WHERE:** `ml/runtime.py:_run_artifact_inference` — log when `set(features.keys()) != set(feature_spec.ordered_keys)`. **EFFORT:** S. **SPORT:** both. **Shipped:** Smarter #27 — pure-function `_detect_feature_drift(features, ordered_keys)` in `apps/api/app/services/ml/runtime.py` returns `(serving_extra, serving_missing_unexpected)`. Wired into `_run_artifact_inference` right before `vectorize`; emits a structured WARNING log (with `feature_spec_version`, `artifact_dir`, `serving_extra`, `serving_missing` fields) when either set is non-empty. Filters out the known training-only enrichments added by `dataset.py` (`sport_is_nba`, `sport_is_mlb`, `suggested_price`, `heuristic_*`) and the `family_key` which `vectorize` reads from a separate slot — so the detector only WARNs on **unexpected** drift. Pure observability: inference output unchanged regardless of whether drift was detected (asserted in tests). 16 new tests in `test_feature_drift_detection.py` covering both the helper's diff logic and the integration log emission (no warn on clean schema, warn on extra key, warn on unexpected missing key, no warn on legitimate training-only misses, no warn on family_key, log carries artifact metadata).

28. **[shipped]** **Per-family `quality_tier` calibration** — **WHY:** `_quality_tier` thresholds (0.36 selected_side_prob, 0.72 context_coverage, 0.58 confidence) are constants. Different families should have different thresholds based on settled-data quality. **WHERE:** move into `model_families.py` overrides. **EFFORT:** M. **SPORT:** both. **Shipped:** Smarter #28 — new `QualityTierThresholds` frozen dataclass in `model_families.py` with all 11 thresholds (4 high + 4 low for the heuristic path + 3 for the ML path). `DEFAULT_QUALITY_TIER_THRESHOLDS` whose fields match the pre-PR constants exactly. Empty `QUALITY_TIER_THRESHOLDS_BY_FAMILY` registry — the PR ships the **mechanism only**; no family is tuned yet (operators populate from backtest analysis later). `_quality_tier` in `scoring.py` now takes a `family_key` parameter and reads thresholds via `quality_tier_thresholds_for(family_key)` with default fallback. 15 new tests pin the default values, verify all registered families resolve to defaults, exercise both tier classifications (heuristic + ML), and confirm lenient/strict overrides shift the tier output for the targeted family without affecting others. Code-reviewer pass: APPROVE, 0 issues at any severity (clean mechanism-only refactor with no behavior change).

29. **[shipped]** **NBA pre-game lineup-confirmation reliability tuning** — **WHY:** NBA lineup confirmation arrives ~10–15 min before tip; injury report ~30–90 min before. **WHERE:** make `nba_injury_report_cache_minutes` 15 during the hour before tip. **EFFORT:** S. **SPORT:** NBA. **Shipped:** Smarter #29 — pure-function policy helper `_effective_injury_report_ttl_minutes(now, event_start)` in `apps/api/app/services/nba_long_tail.py`. Returns 15-min TTL inside the final hour pre-tip (inclusive on both endpoints), coarse default (`nba_injury_report_cache_minutes`, currently 60) otherwise; falls back to default when either arg is missing and coerces naive datetimes to UTC (handoff trap #5). Helper is the WRITE-side input (`expires_at = now + ttl`); READ paths trust the persisted `expires_at` so the cache cannot flap when the relative distance to tip-off ticks past 3600s. No NBA injury-report loader exists yet — the helper ships ahead of Smarter #11 / a future loader so the policy lives in one place. Drift guard test asserts the near-tip TTL is strictly smaller than the coarse default.

30. **Backtest-driven `watchlist_min_edge` per-family tuning** — **WHY:** a fixed 0.03 floor is too aggressive for high-variance NBA props, too lenient for tight MLB game lines. **WHERE:** per-family edge floors in `Settings`; tune from backtest results. **EFFORT:** M. **SPORT:** both. **Depends on:** smarter #2.

31. **Grounded LLM narrator (when/if added) with verifier pass** — **WHY:** if a narrator LLM is wired, ground every claim in structured features and have a verifier reject unsupported claims. **WHERE:** future `apps/api/app/services/narrator.py`. **EFFORT:** L. **SPORT:** both.

32. **Drawdown brake on demo trading** — **WHY:** a losing streak should reduce position size automatically. **WHERE:** `orders.py` — check rolling PnL, reduce suggested size when 7-day PnL below threshold. **EFFORT:** M. **SPORT:** both. **Depends on:** smarter #9.

---

## Architecture / Rewrite Candidates

Six items, ordered roughly by ROI / urgency.

### 1. Owner / public API boundary `[codex]` · effort: **M** · deferred
- **Current:** one app exposes product, research, ops, paper trading, demo trading, account data. Security depends on deployment topology + CORS rather than explicit authorization.
- **Target:** split owner router from public router. Owner router has an auth dependency. Trading + account endpoints are owner-only. Tests cover unauthorized access.
- **Migration:** add `Depends(require_owner_token)`; apply to sensitive routers; update web client to attach token for owner pages; add 401/403 UI.
- **Status:** deferred indefinitely while deployment stays local-only over Tailscale. See [#48a](#48a-deferred--anonymous-mutating-endpoints).

### 2. Refresh job runner `[both]` · effort: **L**
- **Current:** DB queue + scheduler + daemon-thread timeout wrapper; domain writes and job state in same long transaction.
- **What's wrong:** race-prone singleton claim, non-cancellable jobs, late commits after timeout, infinite transient requeues.
- **Target:** DB advisory locks (or `SELECT … FOR UPDATE SKIP LOCKED`) for singleton claim; bounded idempotent job steps; per-job attempts/backoff/dead-letter; cancellation-aware commits (asyncio task with `wait_for` + cooperative phase checks, OR process-pool with kill semantics).
- **Migration:** add job lease/attempt fields; gate claim with DB lock; convert long jobs to batch steps; make timeout roll back side effects; concurrency tests with two sessions.
- **Payoff:** retires bugs #10, #11, #22 at the root.

### 3. ML training and promotion pipeline `[codex]` · effort: **XL**
- **Current:** API worker tries to run training from a sibling repo path and writes local artifacts; full-dataset preprocessing; single-split evaluation.
- **What's wrong:** prod retrain likely no-ops; artifacts not durable; evaluation optimistic; target semantics wrong (bug #2 — now fixed).
- **Target:** offline training job with immutable archived examples; walk-forward eval; per-family calibration; durable artifact registry; manifest review gate; API only serves manifests.
- **Migration:** keep target fix → build learning archive → move training to CI/job runner → publish manifests to storage → API loads versioned manifests → retire worker retrain.
- **Payoff:** prevents a promoted model from silently degrading picks; unblocks smarter #1, #2, #20.

### 4. Market / player resolution service `[codex]` · effort: **L**
- **Current:** fuzzy market mapping + ESPN first-result player search happen inline with limited diagnostics.
- **What's wrong:** ambiguous mappings assigned without confidence trail; player/team hints don't prevent wrong-athlete resolution.
- **Target:** dedicated resolution tables for markets and athletes with confidence, candidate lists, source evidence, override support, ambiguity queue.
- **Migration:** add mapping tables → backfill current mappings with confidence → scoring requires confirmed / high-confidence mapping → ops UI for review.
- **Payoff:** retires bugs #13, #17 and feeds smarter #25.

### 5. Feature freshness layer `[codex]` · effort: **L**
- **Current:** feature emitters pull from caches and raw event payloads with ad-hoc stale flags.
- **What's wrong:** scoring can't reliably know which feature groups are current, missing, neutral-defaulted, or stale.
- **Target:** feature groups with explicit source, freshness, TTL, completeness, severity. Scoring consumes a normalized bundle.
- **Migration:** start with MLB weather/park/starter and NBA injury/rest groups → expand to sportsbook + referee features.
- **Payoff:** prerequisite for smarter #22, #23; makes "missing context" penalties explainable.

### 6. Contract / type ownership `[both]` · effort: **M**
- **Current:** OpenAPI-generated contracts coexist with hand-written TypeScript mirrors; drift check fails on missing `.venv`.
- **Target:** generated DTOs for API responses; explicit web view models for UI state only; mandatory portable drift check in CI.
- **Migration:** make `contracts:check` portable → migrate endpoint families one-at-a-time → delete hand-written mirrors.
- **Payoff:** retires bugs #39, #40.

### Also worth tracking (single-PR refactors, smaller than full rewrites)

- [ ] **R1.** Split `apps/api/app/services/scoring.py` (3,173 lines, violates the 800-line max) into a `scoring/` package (kernel, adjustments, monotonicity, dedupe, orchestration, persistence, resolver). `[claude]` effort: **M**.
- [ ] **R2.** Decompose `apps/api/app/services/ingestion.py` (2,006 lines) into pipeline stages under `refresh/`. `[claude]` effort: **M–L**.
- [ ] **R3.** Replace 4 copies of the `latest_by_max_id` pattern with one window-function helper. `[claude]` effort: **S** (fixes bug #8 at the root).
- [ ] **R4.** Consolidate the two copies of `features.py` (bug #29) — shared package or CI byte-equality check. `[claude]` effort: **S–M**.

---

## Test Coverage Gaps

- [ ] Owner-token gate — unauthorized-access tests on every owner route. Belongs with #48a if it ever ships.
- [x] ML training: NO-side prediction wins → serving emits correct P(YES). Shipped with bug #2.
- [ ] Refresh worker: two-session concurrency test for singleton race (bug #11).
- [ ] Refresh worker: timeout cancellation — assert no DB writes occur after `done_event` fires (bug #10).
- [ ] Settlement: two stacked predictions on same ticker/scope/side both settle (bug #12).
- [ ] Player resolution: same-name across teams → team hint changes the resolved athlete (bug #13).
- [x] Heuristic factors: MLB strikeouts with high CSW pitcher yields factor ≥ 1.0. Shipped with bug #3.
- [ ] Scoring: park / weather features non-default when venue + lat/lon + game_time_utc are present (bug #4).
- [ ] Parlay combiner: correlated 3-leg parlay yields lower combined probability than the independent product (bug #5).
- [ ] Trade-desk: monotonicity adjustment recomputes edge (bug #7).
- [ ] Watchlist coverage helpers: out-of-order ingest still returns latest by `captured_at` (bug #8).
- [ ] Imputation leakage: training-fold median computed without holdout data (bug #16).
- [ ] Walk-forward eval scaffolding (groundwork for smarter #2).
- [ ] Contracts drift check runs in CI from a clean checkout (bug #39).

---

## Open Questions — answered

1. **Owner-token UX** — deferred indefinitely (no public deploy planned). Env-var-only when revisited.
2. **Retention archive scope** — keep every prediction forever in a separate `prediction_archive` table; move to object storage when row count gets painful.
3. **Bankroll input for Kelly sizing** — user setting in a new `account_settings` row; opt-in toggle to use Kalshi live balance.
4. **Sportsbook odds provider** — The Odds API (free tier). Key in `apps/api/.env` (gitignored).
5. **NBA referee data source** — scrape `official.nba.com/referee-assignments` at slate generation; cache results in `nba_referee_cache`.
6. **Walk-forward backtest data scope** — full historical Kalshi candlesticks for NBA + MLB.
7. **Settlement SLA alert channel** — ops UI badge on readiness panel for v1.
8. **`weather_refresh` provider** — keep current provider; activating the dead pipeline is the goal of bug #4. Provider swap is a separate decision.

---

## Verification notes

Three single-agent findings were verified against actual source before inclusion in the original audit:

- **Bug #3 (MLB strikeout pitcher dominance)** — confirmed real. Factor returns `0.30/csw` (suppressor) on `"strikeouts"`, which should be an amplifier. Code at `apps/api/app/services/heuristic_factors.py:81, 193`. **Shipped: PR #27.**
- **Bug #4 (MLB park & weather inactive)** — confirmed real. ESPN normalization emits no `venue_id`; scoring reads `venue_id=None`; weather called with no coordinates. Code at `apps/api/app/clients/espn.py:180`, `apps/api/app/services/scoring.py:1648`. **Next up.**
- **Bug #15 (paper NO PnL)** — partially confirmed. Formula `exit - entry` is **correct** if both prices are the same side, but the schema doesn't enforce that. Reframed as a contract/inversion-risk issue rather than "currently inverted." Code at `apps/api/app/services/orders.py:41`.

---

## Shipped roll-up

| PR | Bug | Sport | Brief |
|---|---|---|---|
| [#25](https://github.com/ckwame-jpg/sika/pull/25) | #2 | both | ML training target = YES-won (initial) |
| [#26](https://github.com/ckwame-jpg/sika/pull/26) | #2 | both | Runtime target_type gate + NO-side scoring confidence + retrained manifest |
| [#27](https://github.com/ckwame-jpg/sika/pull/27) | #3 | MLB | Strikeout pitcher_dominance direction + Statcast-only fallback |
| [#28](https://github.com/ckwame-jpg/sika/pull/28) | #49 | both | NO-side recommendations suppressed as not actionable on Kalshi |
| [#71](https://github.com/ckwame-jpg/sika/pull/71) | Smarter #1 | both | Per-family reliability-curve buckets on the readiness panel (avg_predicted vs. actual_yes_rate per 10% band) |
| [#72](https://github.com/ckwame-jpg/sika/pull/72) | Smarter #3 | both | Closing-line value: signed delta between entry and close price; surfaced as "Avg CLV" tile on the readiness panel |
| [#73](https://github.com/ckwame-jpg/sika/pull/73) | Smarter #24 | both | Time-to-close badge ("T-Xm") on game-line + prop rows; red-bold under 30 min |
| (pending PR) | Smarter #16 | MLB | Suppress (not penalize) MLB props when lineup is confirmed AND player isn't in starting lineup |
| (pending PR) | Smarter #29 | NBA | Dynamic NBA injury-report cache TTL: 15-min inside the final hour pre-tip, default otherwise. Pure-function policy helper; no loader change yet (prep for Smarter #11) |
| (pending PR) | Smarter #11 | NBA | NBA workload heuristic — `recent_workload_minutes_per_game` / `consecutive_games_played` emitters + `_nba_workload_factor` (±5% envelope) + `workload_top_quartile_uncertainty` missing-context entry on confirmed-in-lineup top-quartile rows |
| (pending PR) | Smarter #10 | NBA | NBA granular rest / travel / B2B factors — `_schedule_context` extended with `games_last_3/5`, `is_third_in_four`, `is_fourth_in_six`, `last_game_away`; new `_nba_rest_factor` (±6%) and `_nba_travel_factor` (±2% Phase 1, mileage-aware Phase 2 deferred) |
| (pending PR) | Smarter #12 | NBA | NBA offense × pace × defense interaction term — uncapped `nba_offense_interaction_term` ML feature, no heuristic factor (model learns the shape). Fix-on-merge: corrected the handoff's inverted DRtg direction to match `_nba_opp_def_factor` convention |
| (pending PR) | Smarter #27 | both | Train/serve feature-dictionary drift detection — `_detect_feature_drift` helper + WARN log in `_run_artifact_inference` when serving features and trained spec don't match, filtered against known legitimate misses (dataset-time enrichments, `family_key`). Pure observability, no behavior change |
| (pending PR) | Smarter #28 | both | Per-family `_quality_tier` calibration mechanism — `QualityTierThresholds` dataclass + empty `QUALITY_TIER_THRESHOLDS_BY_FAMILY` override registry + `quality_tier_thresholds_for(family_key)` lookup. Ships the mechanism only; defaults match pre-PR constants exactly so behavior is unchanged today |
| (pending PR) | Smarter #23 | both | Per-upstream-source freshness on `/health` — new `upstream_health.py` module + `record_upstream_success`/`record_upstream_failure`/`get_upstream_health` API + new `upstream_sources` field on `HealthResponse`. NBA Stats wired; ESPN/Kalshi/basketball-reference/MLB Stats appear with `last_success_at=None` (follow-up wirings) |
| (pending PR) | Smarter #26 | both | Settled-outcome SLA aging buckets — `compute_settlement_aging` + `SettlementAging` dataclass + `settlement_aging` field on `ModelReadinessSummaryRead`. Buckets pending predictions by hours-past-`market.close_time` (0-1h / 1-6h / 6-24h / 24h+). UI badge component is frontend follow-up |
