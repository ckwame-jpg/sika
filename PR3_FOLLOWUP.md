# PR3 Follow-Up — Notes for Future Sessions

Continues from `PR3_HANDOFF.md` (the original spec). PRs #12-#15 are merged into `main` as of 2026-05-03. This file captures what was deferred, design choices that might warrant revisit, and the next layer of work.

## Current state of `main`

Commits since sika#11:

```
7eed105 PR 3d: ML v2 training — median imputation, weighting, promotion gate (#15)
7a116ff PR 3c: stats query advanced metrics + league percentile ranks (#14)
caf6141 PR 3b: driver attribution (depends on #12) (#13)
1ddc456 PR 3a: heuristic factor audit — advanced primary, proxies fallback (#12)
486dd83 feat: advanced NBA/MLB stats — ingestion, scoring emit, UI, 6 rounds of polish (#11)
```

Test counts: API **417**, Web **52**, ML **21**.

What advanced stats now drive end-to-end:
- **Scoring**: advanced features emit before proxies; per-stat gates skip the proxy when (a) the source feature is present AND (b) the heuristic_factors module wires the replacement for that `stat_key`. No double-counting, no double-dropping.
- **Frontend**: `_drivers` is server-authoritative — empty list = "computed but nothing notable", absent field = "older prediction, derive from `advanced_factors`".
- **Stats Assistant**: NBA + MLB queries return per-metric `percentiles` (0-100) and `metric_categories` ("basic"/"advanced") when caches are warm.
- **ML training**: median imputation (skipping completeness markers + binary indicators), 3x sample weighting for advanced-complete rows (HGBC + LR pipeline both), `advanced_only_threshold=2000` auto-trigger, promotion gate (`time_brier < baseline` → `serving_mode="ml"`).

## Open follow-ups (prioritized)

### P0 — Real signal, no producer

**MLB league percentiles writer**

`apps/api/app/models.py:653` defines `MlbLeaguePercentilesCache`, and `apps/api/app/services/stats_summary_augment.py:_read_mlb_league_breakpoints` reads it. **No writer exists.** Until a writer is added, MLB stats query responses include the advanced metrics + categories but `percentiles` is always `{}`.

For the writer, mirror the NBA pattern in `apps/api/app/services/advanced_stats.py:load_nba_league_percentiles`:
- Pull league-wide batter sabermetrics + Statcast distributions
- Compute p10/p25/p50/p75/p90 for the keys in `_MLB_BATTER_ADVANCED_KEYS`
- Cache under `metric_key="advanced"`
- Add a daily refresh job entry alongside `advanced_stats_warm`

Sources to consider: `pybaseball`, the MLB Stats API league leaderboards, or rolling our own from `MlbBatterAdvancedCache` rows that already exist (good for kickoff but biased toward our existing player set).

### P1 — Product/UX decisions to make

**`_percentile_rank` clamping semantic**

`apps/api/app/services/stats_summary_augment.py:_percentile_rank` clamps below-p10 to `10` and above-p90 to `90` (NOT 0/100). A player in the bottom 5% of the league sees "10". Tests pin this. The reviewer flagged it as confusing; I left it as-is.

Two product calls to make:
1. Is the clamping semantic acceptable, or should it return `None` for out-of-range (UI shows `—`)?
2. Should the UI label distinguish "≤10" from "exactly 10"?

If we change the semantic, update both `_percentile_rank` AND the `test_percentile_rank_clamps_below_lowest_breakpoint` / `_above_` tests in lockstep.

**`babip` percentile direction**

`babip` is intentionally NOT in `_LOWER_IS_BETTER`. It's contextual luck rather than a one-directional skill metric. If the UI treats "high percentile = good" for every advanced metric uniformly, `babip` will mislead users (a high `babip` could be unsustainable luck). Either:
- Add `babip` to `_LOWER_IS_BETTER` with a comment explaining the regression-to-mean intent
- OR keep the raw rank but add UI affordance (tooltip: "BABIP is partially luck — high values often regress")

### P2 — ML training trade-offs documented in-code

**Calibrated path drops sample_weight for pipeline candidates**

`apps/ml/ml/training.py:_fit_estimator` cleanly routes `sample_weight` to LR pipeline via `<step>__sample_weight` in the non-calibrated branch. The calibrated branch (`cv >= 2 and len(y_train) >= 500`) still drops weights for pipelines because `CalibratedClassifierCV` doesn't propagate prefixed kwargs to the base estimator. Once production crosses 500 settled rows, the LR pipeline candidate goes back to training uniform while HGBC trains weighted — re-introducing the asymmetry.

Options when this becomes painful:
- Manually construct calibration (KFold + per-fold fit + manual sigmoid/isotonic) so we can pass weights at every step
- Drop the `StandardScaler` step and use a single LR estimator (no pipeline); accept the loss of feature scaling
- Switch to `Pipeline.set_fit_request(sample_weight=True)` per the sklearn 1.4+ metadata routing API (requires version bump)

**Median imputation uses the full dataset before train/test split**

Documented as `evaluation_imputation_caveat` in `training_metadata.json`. Magnitude is ~1/N per row. The structural fix (per-fold medians) means recomputing medians inside `_evaluate_candidates` for each split — non-trivial but the right move once N grows enough that the bias becomes meaningful for promotion-gate decisions.

**`advanced_only_threshold=2000` will silently flip behaviour**

When any single family crosses 2,000 advanced-complete settled rows, training auto-filters to advanced-only mode (sample weighting drops to uniform; dataset shrinks). Worth a Slack/dashboard signal when this fires for the first time so the team isn't surprised by a model behaviour change.

### P3 — Observability gaps

**Stats augmentation failure surface**

`augment_summary_with_advanced` logs to `app.services.stats_summary_augment` at WARNING level with `exc_info=True`. There's no metric/counter — failures don't show up in any dashboard. If MLB cache reads start failing in production, the only signal is logs grep. Worth a counter (e.g. `stats_augment_failures_total{sport,reason}`).

**`pace_factor_proxy_superseded` and friends — no consumer**

The `*_proxy_superseded` flags get written to `features` but nothing reads them. Useful for ad-hoc debugging via DB queries; if we want them in dashboards, need to expose via `/research/predictions/...` or a new diagnostic endpoint.

### P4 — Test gaps reviewers flagged but didn't block on

These are LOW-severity test improvements; add when touching the relevant code:

- **PR 3a**: Integration tests for `(MLB, doubles, starter_factor_advanced)`, `(NBA, assists, usage_factor_advanced)`, `(MLB, home_runs, starter_factor_advanced)` suppress paths. The unit-level `factor_applies` test covers the logic, but no integration test exercises the actual scoring path for these triplets.
- **PR 3b**: No test for `_drivers` round-trip through DB serialization (frontend deserializes `unknown` from a JSONB blob — a unit test can't cover this).
- **PR 3d**: No test for the calibrated-path weight drop on pipelines. Behaviour is deliberate but untested directly.
- **PR 3c**: No snapshot test confirming Soccer/Tennis/UFC responses include `percentiles: {}` and `metric_categories: {}` (relying on schema defaults).

### P5 — Code-style nits left in place

These came up in reviewer commentary; deliberately not fixed because none have behavioural impact:

- Lazy imports of `feature_attribution`, `heuristic_factors`, `stats_summary_augment` inside hot paths in `scoring.py` and `stats_query.py`. Documented as intentional (circular-dependency avoidance + test mocking ergonomics).
- `_detail_for_factor` is a 15-branch if/elif chain. Could be a dispatch dict but each handler reads different keys with different fallback logic — collapsing wouldn't simplify.
- `top_drivers` accepts `expected_baseline` / `expected_final` as reserved parameters that are `del`'d. Kept for future-attribution path that might weight drivers by absolute impact on `expected`.
- Pre-existing dead code: `apps/ml/ml/training.py:_fit_estimator` has an `else "sigmoid"` branch that's unreachable (outer guard already requires `len(y_train) >= 500`). Predates PR3, didn't touch.

## Sanity-check commands

```bash
cd /Users/chris/Workspace/locked-in/github/sika
git fetch origin
git log --oneline -5 origin/main          # confirm PR3 commits visible

# Test totals (run on main):
.venv/bin/python -m pytest apps/api -q                                       # 417 expected
cd apps/web && npx vitest run                                                # 52 expected
cd ../ml && PYTHONPATH=. ../../.venv/bin/python -m pytest tests -q           # 21 expected

# Quick scoring smoke (no DB):
.venv/bin/python -c "from app.services.heuristic_factors import factor_applies; print(factor_applies('NBA', 'made_threes', 'usage_factor_advanced'))"
# Expected: True
```

## Files that took non-trivial growth in PR3

Useful to know which files are now larger-than-they-were so future edits respect the structure:

| File | Pre-PR3 | After PR3 | Notes |
|------|---------|-----------|-------|
| `apps/api/app/services/scoring.py` | ~3094 | ~3225 | `_score_player_prop` is the function to look at |
| `apps/api/app/services/stats_query.py` | ~1723 | ~1799 | new `_ADVANCED_METRIC_LABELS` table near top |
| `apps/api/app/services/feature_attribution.py` | (new) | 290 | factor name → label + detail string lookup |
| `apps/api/app/services/stats_summary_augment.py` | (new) | 315 | per-sport advanced metric augmentation |
| `apps/ml/ml/training.py` | 257 | 605 | `_fit_estimator`, completeness mask, sample weights, promotion gate |

## Where to look for "what does this signal mean?"

When stat-key meanings are unclear or you need to know who emits what:

- **NBA emitters**: `apps/api/app/services/advanced_stats.py` (player + team), `apps/api/app/services/nba_long_tail.py` (hustle/drives/clutch/defense)
- **MLB emitters**: `apps/api/app/services/mlb_advanced.py` (batter sabermetrics, batter Statcast, pitcher sabermetrics, pitcher Statcast, park, weather, lineup)
- **Heuristic factor functions**: `apps/api/app/services/heuristic_factors.py` (`_NBA_FACTOR_FNS`, `_MLB_FACTOR_FNS`)
- **Per-stat gating tuples**: same file (`_NBA_FACTORS_BY_STAT`, `_MLB_FACTORS_BY_STAT`)
- **Driver labels + detail strings**: `apps/api/app/services/feature_attribution.py` (`_FACTOR_LABELS`, `_detail_for_factor`)
- **Advanced-stats summary mapping**: `apps/api/app/services/stats_summary_augment.py` (`_NBA_ADVANCED_KEYS`, `_MLB_BATTER_ADVANCED_KEYS`)
- **ML completeness markers**: `apps/ml/ml/training.py:ADVANCED_COMPLETENESS_MARKERS` (must stay in sync with API emitters; the `test_advanced_completeness_markers_match_api_emitters` scan-test enforces this)

## When you add a new advanced feature emitter

1. Decide if it carries a `*_data_complete` marker. If so, add the marker to `ADVANCED_COMPLETENESS_MARKERS` in `apps/ml/ml/training.py` (the scan-test will fail until you do).
2. If the feature should drive scoring, add a function in `apps/api/app/services/heuristic_factors.py` (`_NBA_FACTOR_FNS` or `_MLB_FACTOR_FNS`) and list it in the relevant per-stat tuple.
3. If you're adding a new advanced replacement for an existing proxy, also add a gate in `_score_player_prop` that uses `factor_applies(...)` to decide whether to suppress the proxy.
4. If the feature should appear in driver attribution, add an entry to `_FACTOR_LABELS` in `feature_attribution.py` AND a branch in `_detail_for_factor` that pulls its underlying numbers.
5. If it's a new Stats Assistant metric, extend `_NBA_ADVANCED_KEYS` / `_MLB_BATTER_ADVANCED_KEYS` and the corresponding `_ADVANCED_METRIC_LABELS` entry in `stats_query.py`.
6. If it has lower-is-better semantics, add to `_LOWER_IS_BETTER` in `stats_summary_augment.py`.

The chain is long but each link is small. Skipping any one of them silently degrades the user-facing surface — the scan-tests catch the marker case but nothing yet enforces the others.

## Decision log — things that were deliberately left as-is

| Decision | Why | Where to revisit |
|----------|-----|------------------|
| Calibrated path drops weights for pipelines | sklearn limitation, documented; non-blocking until N>500 per fit | `_fit_estimator` docstring |
| `_percentile_rank` clamps to 10/90 not 0/100 | Conservative — avoids implying we know the tail shape | Product call, see P1 |
| `babip` raw rank (not inverted) | Contextual luck, not skill | Product call, see P1 |
| `expected_before_advanced` field name preserved | Backward compat; meaning unchanged from pre-PR3a | Inline comment added |
| `_drivers` always written when `advanced_factors` fires | Server is authoritative; empty = "nothing notable" | Frontend test pins this contract |
| Lazy imports inside hot paths | Avoids circular deps; CPython caches modules so cost is negligible | Refactor only if profiling flags it |
