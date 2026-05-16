# Smarter #21 phase 2b/d — session handoff

You are picking up Smarter #21 (conformal prediction intervals on prop expected values) in the sika repo. The math + on-disk contract + serve-time loader are done. **Phase 2b** (training-pipeline integration) and **phase 2d** (scoring consumer + UI band) are what's left, and they're invasive enough to deserve a fresh session.

## Goal recap

Today the prop-stat output is a Poisson approximation around a point estimate (`_poisson_yes_probability(expected_value, threshold)` in `apps/api/app/services/scoring/__init__.py`). That's wrong for low-variance stat keys (Brunson at 27.0 with a tight band) and wrong for high-variance ones (a swing guard projecting 22 but ranging 10–34). Smarter #21 replaces the Poisson with a fitted quantile-regression interval (p10, p50, p90) per family + stat key. The over/under recommendation is then a CDF lookup against the actual fitted distribution.

## What's already shipped

Read these PRs before doing anything else — the contract is established:

| PR | What | Key files |
|---|---|---|
| Phase 1 (long shipped) | Pure-math primitives | `apps/ml/ml/quantile_regression.py` — `fit_quantile_regressor`, `fit_prediction_interval_models`, `compute_prediction_interval`, `empirical_coverage` |
| [sika#133](https://github.com/ckwame-jpg/sika/pull/133) (phase 2a) | Sidecar I/O contract | `apps/ml/ml/interval_training.py` — `train_prop_interval_models`, `load_interval_models`, `load_interval_metadata`, `interval_models_present`. **Read this first** — it defines the on-disk layout phase 2b writes to and phase 2c reads from. |
| [sika#140](https://github.com/ckwame-jpg/sika/pull/140) (phase 2c) | Serve-time loader | `apps/api/app/services/ml/artifact_loader.py` — `SklearnArtifact.interval_models`, `_load_sidecar_interval_models`, `apply_interval_models`. Pinned by 11 tests in `apps/api/tests/test_artifact_loader_intervals.py`. |

## On-disk contract (don't change it)

Phase 2a established this layout. Phase 2b writes to it; phase 2c reads from it. Drift here breaks both ends silently.

```
<artifact_dir>/interval_models/<stat_key>/p10.joblib
<artifact_dir>/interval_models/<stat_key>/p50.joblib
<artifact_dir>/interval_models/<stat_key>/p90.joblib
<artifact_dir>/interval_models/<stat_key>/metadata.json
```

Each joblib is a fitted `GradientBoostingRegressor(loss="quantile", alpha=<q>)`. Metadata is `{family_key, stat_key, quantiles, sample_size, empirical_coverage, window_start, window_end, trained_at}`. The cache-key fingerprint in `_sidecar_fingerprint` walks this subdirectory, so adding/replacing files automatically invalidates the artifact cache.

## Phase 2b scope — training pipeline integration

The single biggest gap: the existing classifier training has the binary YES/NO label per row, but quantile regression needs the **actual continuous stat output** (e.g., LeBron scored 28 points). That value lives in the ESPN gamelog cache; phase 2b's main job is the join.

### Dataset extraction

1. Walk settled `Prediction` rows where `market_family == "player_prop"`, `prediction_outcome in ("won", "lost")`, `subject_name` and `stat_key` set.
2. For each row: join to ESPN gamelog cache (`EspnPlayerGamelogCache` keyed by `sport_key`, `athlete_id`, `season`) and pull the actual stat value from the game played on or near `prediction.captured_at`. The existing helper at `apps/api/app/services/stats_query.py:_build_game_logs` extracts the per-game stat values; reuse its parsing rather than duplicating.
3. Map `subject_name` → `athlete_id` via `EspnPlayerSearchCache` (same lookup the live `PropStatsResolver` does). Skip rows that can't be resolved — log a count of skipped rows for the operator.
4. Yield `(features, continuous_target)` pairs. `features` should be the same vector the classifier sees so the trained regressor can be served with the same `feature_spec` (no separate feature pipeline).

The natural module shape: `apps/ml/ml/interval_dataset.py` with one public function `build_interval_training_rows(db, family_key, stat_key, *, lookback_days, min_samples)` → `tuple[np.ndarray, np.ndarray]` or `None` when there's not enough data.

### Training-CLI wiring

Add a new subcommand to `apps/ml/ml/cli.py`. Two options to consider:

- **Option A:** new `train-intervals --family-key X --stat-key Y --manifest-path Z` (independent, one stat key at a time)
- **Option B:** extend `train` to also fit intervals for a configurable list of stat keys (`--interval-stat-keys points,rebounds,assists`)

Option A is more focused and reviewable; pick that unless you find a reason. The command should:
1. Resolve the artifact directory the same way `_resolve_artifact_dir` does for recalibrate (mirrors the existing pattern).
2. Call `build_interval_training_rows` (above).
3. Call `train_prop_interval_models` from phase 2a to fit + persist.
4. Bump the manifest's `model_version` or a new `intervals_version` marker so phase 2c's cache invalidation triggers.

### Continuous stat-target picks

Defensible default: **NBA** — points, rebounds, assists, three_points_made, field_goals_made, PR, PA, RA, PRA. **MLB** — total_bases, hits, rbis, runs, walks, strikeouts. These are all integer counts with enough variance that the interval is more informative than a Poisson around a single point. Don't include binary stats (made/missed FT) — those should stay binary classification.

## Phase 2d scope — scoring consumer + UI

Once phase 2b ships and artifacts have populated `interval_models`, the scoring kernel + UI can consume them. **Don't ship 2d before 2b is producing data** — without populated intervals you'd be shipping dead code.

### Scoring kernel hook

In `apps/api/app/services/scoring/__init__.py:_score_player_prop` (around the Poisson approximation), after the feature vector is built:

```python
artifact = ...  # the served family's SklearnArtifact (from ml.runtime)
intervals = apply_interval_models(artifact, stat_key, feature_vector)
if intervals is not None:
    p10, p50, p90 = intervals
    # Use CDF lookup against the interval to compute over/under probability.
    # Surface intervals + chosen probability in scoring_diagnostics.
```

The CDF lookup is the tricky math. A reasonable first-cut: fit a triangular distribution on (p10, p50, p90) and integrate above/below the threshold. A future phase can refine to a piecewise-linear or kernel-density distribution from more quantiles.

Surface the result in `scoring_diagnostics["prediction_interval"]` = `{p10, p50, p90, source}` so the operator UI can render the band.

### Trade-ticket UI band

In `apps/web/components/trade/trade-ticket.tsx` (or wherever the prop projection is rendered):

- When `recommendation.scoring_diagnostics.prediction_interval` is present, render a horizontal band showing the [p10, p90] range with a tick at p50 and another at the threshold. Color the band based on which side the threshold falls on (over → green-leaning, under → red-leaning).
- Fall back to the existing point-estimate display when intervals are absent (most stat keys won't have intervals until phase 2b has been run for them).

Refer to the existing pricing displays + sparkline patterns in `apps/web/components/trade/` for the visual idiom. **Use the frontend skills for the UI changes** (the user explicitly asked for this in prior sessions).

## Open design decisions you'll need to make (or get input on)

1. **CDF distribution choice** — triangular (cheap, defensible default) vs. piecewise-linear with more quantiles (more accurate, requires more training data). Triangular is the right starting point.
2. **Manifest versioning** — bump `model_version` per interval retrain, or introduce a separate `intervals_version` field? Operators read `model_version` for the classifier; a separate field keeps the audit log cleaner.
3. **Training window** — same 30-day rolling window as recalibration (Smarter #20) is the natural default. Documented choice; deviate only with a reason.
4. **Stat-key allowlist source** — hardcoded in `cli.py` (simple), or a settings field (operator-tunable)? Start hardcoded; promote to a setting if operators ask.
5. **Manifest update sequence** — does phase 2b write the sidecar first then bump the manifest, or the reverse? Mirror Smarter #20's pattern (sidecar first, then version bump) so an interrupted run leaves the manifest pointing at the old version.

## Required workflow

You are operating in the `/Users/chris/Workspace/locked-in/github/sika/.claude/worktrees/kind-snyder-58d4c7/` worktree. Branch off `origin/main` for each PR; commit on the feature branch; push + open PR via `gh`; merge with `--admin --body ""`; reset to `origin/main` between PRs.

### Codex review is required

Run `codex exec` after each meaningful PR. Pattern from prior sessions:

```bash
codex exec --model gpt-5-codex --skip-git-repo-check --sandbox read-only "$(cat <<'EOF'
Review the [PR description] in the sika repo.

CONTEXT
[what changed, why it matters]

CHANGES (full diff inline)
[git diff]

REVIEW FOCUS — flag P1 / P2 issues only:
1. Correctness of the math / DB query / contract
2. ...

REPORT under 400 words. End with "APPROVE" or "REQUEST CHANGES".
EOF
)" 2>&1 | tail -100
```

**If codex hangs / rate-limits** (it has happened mid-session), kill it and fall back to the `python-reviewer` subagent with the same prompt. The subagent uses the same 9-point self-review checklist:

1. Gate placement (auth, settings, feature flags checked before work)
2. N+1 / eager loading (load_only / joinedload where reads need it)
3. Per-stat / per-family scope (gating doesn't leak across families)
4. Float-precision boundaries (IEEE-754 rounding when comparing thresholds)
5. Naive-UTC handling (SQLite drops tz info; coerce before subtracting)
6. Concurrent-upsert race (IntegrityError-as-update for unique-key inserts)
7. UI/operator surface symmetry (diagnostic emitted in both summary AND detail paths)
8. Empty-state UX (explanation tooltip when zero data is shown)
9. Cross-scope reuse safety (helpers moved to shared modules don't widen behavior)

Address every P1 before merging. Reply to P2s in the PR description if you choose not to fix them.

### TDD ordering

For each PR:
1. Write failing tests first (synthetic inputs covering happy path + edge cases)
2. Implement the production code
3. Confirm tests pass
4. Run the full suite (`apps/api`: `pytest --tb=short -q`; `apps/ml`: same; `apps/web`: `npx vitest run`) to confirm no regression
5. Codex / subagent review
6. Address findings; re-run tests
7. Push + PR + merge

### File layout you should aim for

```
apps/ml/ml/
  interval_dataset.py        # Phase 2b — DB-side dataset extraction
  cli.py                     # Phase 2b — new train-intervals subcommand
  interval_training.py       # (already exists — phase 2a contract)
  quantile_regression.py     # (already exists — phase 1 math)

apps/api/app/services/
  scoring/__init__.py        # Phase 2d — _score_player_prop interval consumer
  ml/artifact_loader.py      # (already exists — phase 2c loader)

apps/web/components/trade/
  prediction-interval-band.tsx  # Phase 2d — new component
  trade-ticket.tsx              # Phase 2d — mount the band
```

## What NOT to do

- **Don't change the on-disk contract** in `apps/ml/ml/interval_training.py` or `apps/api/app/services/ml/artifact_loader.py:_load_sidecar_interval_models`. Phase 2c's loader is shipped and serving (currently empty maps until 2b populates artifacts); changing the layout breaks both ends.
- **Don't ship phase 2d before phase 2b populates real interval artifacts.** Without populated intervals, the consumer wiring is dead code that complicates rollback. Phase 2b can ship without 2d; 2d depends on 2b.
- **Don't refactor scoring.py** to consume intervals everywhere at once. The Poisson approximation is correct for stat keys without intervals; the consumer must be gated on `apply_interval_models` returning non-None.
- **Don't bump every existing test's golden values** to accommodate intervals. Existing tests run with empty `interval_models` dicts; their behavior is unchanged.
- **Don't add ML training to the API process.** Bug #21 explicitly moved retrain to GitHub Actions (`.github/workflows/ml-retrain.yml`). Phase 2b's CLI command runs offline via the same workflow (or via local CLI invocation).

## Quick sanity checks before each commit

- `apps/api`: `cd apps/api && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `apps/ml`: `cd apps/ml && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `apps/web`: `cd apps/web && npx tsc --noEmit && npx vitest run`

Baseline as of this handoff: **1,529 apps/api tests pass**, **186 apps/ml tests pass**, **148 apps/web tests pass**.

## Punch-list pointer

The smarter roadmap lives in `SIKA_PUNCH_LIST.md` (repo root). Smarter #21 is the section labeled "Conformal prediction intervals on prop expected values."

The full session-by-session handoff history (architecture context, conventions, decisions log) is in `SESSION_HANDOFF_2026_05_15.md`. Read it if anything in this doc references a pattern you don't immediately recognize.
