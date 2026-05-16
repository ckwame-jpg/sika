# Smarter #21 phase 2d — session handoff

You are picking up Smarter #21 (conformal prediction intervals on prop expected values) in the sika repo. **Phase 2b is shipped** ([sika#154](https://github.com/ckwame-jpg/sika/pull/154) — dataset extraction, [sika#158](https://github.com/ckwame-jpg/sika/pull/158) — train-intervals CLI). **Phase 2d** — scoring kernel consumer + UI band — is what's left.

## Prerequisite — confirm interval artifacts exist in prod

The original phase 2b handoff said:

> **Don't ship phase 2d before phase 2b populates real interval artifacts.** Without populated intervals, the consumer wiring is dead code that complicates rollback.

**As of 2026-05-16 this prerequisite is satisfied** — the operator ran `train-intervals` for 7 stat keys and the inspect-intervals CLI confirms artifacts exist. Live coverage results (recorded so you can ground the gating design in real evidence, not theory):

```
FAMILY     STAT         SAMPLES  COVERAGE  STATUS
mlb_props  hits         84       0.964     bad
mlb_props  home_runs    62       0.952     bad
mlb_props  total_bases  59       0.915     warn
nba_props  assists      82       0.878     ok
nba_props  made_threes  71       0.972     bad
nba_props  points       126      0.786     ok
nba_props  rebounds     84       0.952     bad
```

**Key signal for the gating design: 2/7 ok, 1/7 warn, 4/7 bad.** Most low-sample-size stat keys over-cover (interval too wide). High-volume keys (points 126 samples / assists 82) calibrate correctly. This means the consumer **must** gate on `coverage_status` — naively consuming all intervals would ship worse projections than the Poisson approximation for 4/7 stat keys.

The inspect-intervals CLI lives at `python -m ml.cli inspect-intervals --manifest-path manifests/current.json`; it's the read path you'll want during PR 3 design + verification.

Re-run the inspect CLI before starting PR 3 — coverage may have improved since this snapshot as more games settle.

## What's already shipped

| PR | What | Key files |
|---|---|---|
| Phase 1 (long shipped) | Pure-math primitives | `apps/ml/ml/quantile_regression.py` |
| [sika#133](https://github.com/ckwame-jpg/sika/pull/133) (phase 2a) | Sidecar I/O contract | `apps/ml/ml/interval_training.py` |
| [sika#140](https://github.com/ckwame-jpg/sika/pull/140) (phase 2c) | Serve-time loader | `apps/api/app/services/ml/artifact_loader.py` — `SklearnArtifact.interval_models`, `_load_sidecar_interval_models`, `apply_interval_models`. Pinned by 11 tests in `apps/api/tests/test_artifact_loader_intervals.py`. |
| [sika#154](https://github.com/ckwame-jpg/sika/pull/154) (phase 2b dataset) | DB-side dataset extraction | `apps/ml/ml/interval_dataset.py` — `build_interval_training_rows`, `IntervalDatasetExtract`. 31 tests covering team-hint disambiguation, MLB doubleheader anchoring, asymmetric game-match window, skip taxonomy. |
| [sika#158](https://github.com/ckwame-jpg/sika/pull/158) (phase 2b CLI) | `train-intervals` CLI subcommand | `apps/ml/ml/cli.py` — `_train_intervals` + `_load_feature_spec`. 8 tests pinning the apply / dry-run / insufficient-samples decision tree. |
| [sika#163](https://github.com/ckwame-jpg/sika/pull/163) (operator visibility CLI) | `inspect-intervals` CLI for operator visibility | `apps/ml/ml/cli.py` — `_inspect_intervals` + `_collect_interval_artifacts` + `_format_inspect_intervals_table` + `_classify_coverage`. Output is a table or JSON; bands coverage into `ok` / `warn` / `bad` / `unknown` against the 80% target. 13 tests. |
| [sika#164](https://github.com/ckwame-jpg/sika/pull/164) (operator visibility UI) | Readiness panel `interval_models` section (API + web) | `apps/api/app/services/ml/interval_status.py:collect_interval_model_status` + `apps/api/app/schemas.py:IntervalModelStatusRead` + `apps/web/components/predictions/interval-models-badge.tsx`. 8 + 5 tests. Same `coverage_status` banding as the CLI (cross-package drift guard pins them). |
| [sika#165](https://github.com/ckwame-jpg/sika/pull/165) (resolver fix) | Bare cache row accepted when team_name matches hint | `apps/ml/ml/interval_dataset.py:_build_athlete_resolver` + `_team_hint_matches_subtitle` (+ ESPN team abbreviation map, drift-guarded against `apps/api/app/clients/espn.py`). Found because the strict policy from #154 rejected 100% of rows when run against the live DB. |
| [sika#166](https://github.com/ckwame-jpg/sika/pull/166) (dedupe fix) | Dedupe shared-artifact listings + metadata family attribution | `apps/ml/ml/cli.py:_collect_interval_artifacts` + `apps/api/app/services/ml/interval_status.py:collect_interval_model_status`. Found because sika's `global_v1` artifact serves 4 manifest families; the listings were 4×-reported. Both surfaces (CLI + readiness panel) attribute rows by metadata's `family_key` now, not by manifest's `serves_family_key`. |

## On-disk contract (don't change it)

Phase 2a established this layout. Phase 2b writes to it; phase 2c reads from it. Drift here breaks both ends silently.

```
<artifact_dir>/interval_models/<stat_key>/p10.joblib
<artifact_dir>/interval_models/<stat_key>/p50.joblib
<artifact_dir>/interval_models/<stat_key>/p90.joblib
<artifact_dir>/interval_models/<stat_key>/metadata.json
```

Each joblib is a fitted `GradientBoostingRegressor(loss="quantile", alpha=<q>)`. Metadata is `{family_key, stat_key, quantiles, sample_size, empirical_coverage, window_start, window_end, trained_at}`. The cache-key fingerprint in `_sidecar_fingerprint` walks this subdirectory; adding/replacing files automatically invalidates the artifact cache.

## Phase 2d scope (this session)

### PR 3 — Scoring kernel consumer

In `apps/api/app/services/scoring/__init__.py:_score_player_prop` (around the Poisson approximation), after the feature vector is built:

```python
artifact = ...  # the served family's SklearnArtifact (from ml.runtime)
intervals = apply_interval_models(artifact, stat_key, feature_vector)
if intervals is not None:
    p10, p50, p90 = intervals
    # Use CDF lookup against the interval to compute over/under probability.
    # Surface intervals + chosen probability in scoring_diagnostics.
```

**CDF distribution choice** — the handoff suggests a triangular distribution on (p10, p50, p90) as the cheap-and-defensible default. Integrate the triangular distribution above (over) or below (under) the threshold to get the probability. A future phase can refine to piecewise-linear or KDE when more quantiles are trained.

**Surface in diagnostics**:
```python
scoring_diagnostics["prediction_interval"] = {
    "p10": p10,
    "p50": p50,
    "p90": p90,
    "source": "interval_model_v1",
    "yes_probability_from_interval": <computed>,
    "yes_probability_from_poisson": <existing point-estimate value>,
    "delta": <interval - poisson>,
}
```

Persist both so the operator can A/B inspect interval vs Poisson per prop without re-running.

**Gating** — **the load-bearing design decision for this PR.** Two gates, both required:

1. `apply_interval_models` returns non-None. Stat keys without trained intervals continue to use the Poisson approximation — no behavior change for them. This is the rollback-safety gate (revert PR removes the consumer; Poisson still works).
2. **`metadata.coverage_status == "ok"`.** This is NEW — wasn't in the original handoff. The 2026-05-16 demo proved 4/7 stat keys land in `bad` (over-covering) or `warn` (edge) coverage. Naively consuming bad intervals = worse than Poisson. Solutions to consider:
   - **Strict (recommended for first ship):** only consume when `coverage_status == "ok"`. The other 4-5 stat keys keep using Poisson, transparent to the operator. As more games settle and coverage migrates to `ok`, more stat keys naturally activate.
   - **Lenient:** consume `ok` + `warn`. Risks shipping slightly-miscalibrated intervals. Requires explaining the choice in scoring_diagnostics.
   - **Weighted:** blend interval-derived YES probability with the Poisson value, weighted by 1 - |coverage - 0.80|. Cleanest in theory; harder to operator-explain.

The metadata for this gate is already loaded — `SklearnArtifact.interval_models` is keyed by stat_key but the per-stat metadata.json (with `empirical_coverage`) is read at artifact-load time too. May need a small helper on the artifact loader to expose `coverage_status` per stat key without re-reading metadata.

This is the open design pass to do **before** writing PR 3's code. Sketch the gating policy in the PR description; the answer affects how many lines of code change.

**Tests** (apps/api side):
- Happy path: a SklearnArtifact with interval models loaded + a prop with that stat key → `scoring_diagnostics["prediction_interval"]` populated, YES probability derived from CDF.
- Fallback path: same artifact, prop with a stat key NOT in `interval_models` → no `prediction_interval` diagnostic, Poisson path used (existing behavior unchanged).
- CDF math: synthetic triangular distribution → known integrals (P(X > threshold) hand-computed).
- Edge cases: threshold below p10 (P(over) ≈ 1.0), threshold above p90 (P(over) ≈ 0.0), threshold exactly at p50 (P(over) ≈ 0.5).

### PR 4 — Trade-ticket UI band

In `apps/web/components/trade/trade-ticket.tsx` (or wherever the prop projection is rendered):

- When `recommendation.scoring_diagnostics.prediction_interval` is present, render a horizontal band showing the [p10, p90] range with a tick at p50 and another at the threshold. Color based on which side the threshold falls on (over → green-leaning, under → red-leaning).
- Fall back to the existing point-estimate display when intervals are absent.

**Use the `/frontend-design` skill family for the UI changes** (the user explicitly asked for this in prior sessions).

Refer to existing pricing displays + sparkline patterns in `apps/web/components/trade/` for the visual idiom. New component lives at `apps/web/components/trade/prediction-interval-band.tsx`.

**Type-safe schema**: `apps/web/lib/types.ts` will need a `PredictionInterval` interface matching the backend `scoring_diagnostics["prediction_interval"]` shape.

**Tests** (apps/web side):
- Band renders when `prediction_interval` is present.
- Band falls back to point-estimate when `prediction_interval` is absent.
- Threshold tick positioned correctly relative to the [p10, p90] range.
- Over vs under coloring respects which side the threshold falls on.

## Required workflow (non-negotiable)

You are operating in a sika worktree. Branch off `origin/main` for each PR; commit on the feature branch; push + open PR via `gh`; merge with `gh pr merge --squash --admin --body ""`; reset to `origin/main` between PRs.

**TDD ordering**:
1. Write failing tests first.
2. Implement.
3. Confirm tests pass.
4. Run full suite — `apps/api`: `pytest --tb=short -q`; `apps/web`: `npx vitest run`.
5. Run codex review (see below).
6. Address findings; re-run tests.
7. Push + PR + merge.

### Codex review

Run `codex exec` after each meaningful PR:

```bash
codex exec --skip-git-repo-check --sandbox read-only "$(cat <<'EOF'
Review [PR description] in the sika repo.

CONTEXT
[what changed, why it matters]

FILES CHANGED
[list]

REVIEW FOCUS — flag P1 / P2 issues only:
[9-point checklist below]

REPORT under 400 words. End with "APPROVE" or "REQUEST CHANGES".
EOF
)"
```

The default model is `gpt-5.5` (codex on a ChatGPT account doesn't support `gpt-5-codex`). If codex hangs / rate-limits, fall back to the `python-reviewer` or `typescript-reviewer` subagent with the same prompt.

**9-point self-review (apply before every push):**
1. Does the test fail without the change and pass with it?
2. Are types narrow (no `Any`, no `dict[str, Any]` at boundaries)?
3. Are inputs validated at the boundary and errors surfaced explicitly?
4. Any silent fallback that could mask a real bug?
5. Does the on-disk / API contract match what phase 2a and 2c established?
6. Are imports / re-exports preserved for backward compat?
7. Did I touch only files this phase requires?
8. Is the PR description specific about scope, contract, and rollback?
9. Did codex (or the reviewer subagent) flag anything I haven't addressed?

Address every P1 before merging. Reply to P2s in the PR description if you choose not to fix them.

## File layout you should aim for

```
apps/api/app/services/
  scoring/__init__.py        # Phase 2d PR 3 — _score_player_prop consumer
  ml/artifact_loader.py      # (already exists — phase 2c loader; do NOT modify)

apps/api/app/
  schemas.py                 # Phase 2d PR 3 — PredictionInterval Pydantic schema
                             # if surfacing on RecommendationRead

apps/web/components/trade/
  prediction-interval-band.tsx  # Phase 2d PR 4 — new component
  trade-ticket.tsx              # Phase 2d PR 4 — mount the band

apps/web/lib/types.ts           # Phase 2d PR 4 — PredictionInterval interface
```

## What NOT to do

- **Don't change the on-disk contract** in `apps/ml/ml/interval_training.py` or `apps/api/app/services/ml/artifact_loader.py:_load_sidecar_interval_models`. Both are shipped and serving.
- **Don't refactor scoring.py** to consume intervals everywhere at once. The Poisson approximation is correct for stat keys without intervals; the consumer must be gated on `apply_interval_models` returning non-None.
- **Don't bump existing tests' golden values** unless they're testing a stat key that now has intervals — for stat keys without intervals, behavior is unchanged.
- **Don't ship PR 3 + PR 4 in a single bundle.** PR 3 (kernel consumer) and PR 4 (UI band) are independent surfaces with independent test surfaces. Reviewing them separately is easier.

## Baseline tests must stay green

Updated counts after the 2026-05-16 session shipped 4 more PRs (#163, #164, #165, #166):

- `apps/api`: **1,560 baseline → expected ~1,575 after phase 2d** (10-15 new tests for the consumer + CDF math + coverage gating).
- `apps/ml`: **247 (after PRs #163, #165, #166)** — phase 2d doesn't touch apps/ml; should stay 247.
- `apps/web`: **153 baseline → expected ~160 after PR 4** (5-7 new vitest tests for the band).

## Open design decisions you'll need to make

1. **Coverage-status gating policy** (NEW — see PR 3 §Gating above). Strict / lenient / weighted. Affects the consumer's branch logic. Decide before writing tests.
2. **CDF distribution choice** — triangular (cheap, defensible default) vs. piecewise-linear with more quantiles (more accurate, requires more training data). Triangular is the right starting point.
3. **YES probability resolution** — replace the Poisson value entirely when intervals are available, OR ship both side-by-side with a feature flag? Recommend: when `prediction_interval` is present AND coverage_status passes the gate, use the interval-derived YES probability for `fair_yes_price`. Keep the Poisson value in `scoring_diagnostics["prediction_interval"]["yes_probability_from_poisson"]` for A/B inspection.
4. **UI band height / width** — match the existing sparkline patterns. The frontend-design skill family will have opinions here.
5. **Color scheme for over vs under** — green/red is the obvious choice; check `apps/web` design tokens for the canonical green/red (e.g. `outcome-pill` tones `settled` / `lost` reuse here too).

## Lessons from the 2026-05-16 session (read first)

Specifically applies to phase 2d. Compounds with the broader `SIKA_SESSION_RULES.md` rules.

1. **Cross-package drift guards work.** PRs #165 + #166 added `_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME` + `INTERVAL_COVERAGE_*` constants duplicated across apps/api + apps/ml, with `ast.literal_eval`-based drift-guard tests that fail CI when copies diverge. For phase 2d: the same pattern applies if you duplicate any consumer-side classification constants (e.g. CDF-band labels) between scoring and the readiness panel.
2. **Don't fabricate facts.** Per `SIKA_SESSION_RULES.md`, when you need a URL / config value / threshold from outside the codebase, research it (WebSearch / WebFetch / `gh` CLI) before writing it in a doc, commit message, or PR body. Mistakes cost trust + clarification rounds.
3. **Codex hung 4× in the 2026-05-16 session.** Manual self-review against the 9-point checklist was the documented fallback. If codex is responsive when you ship phase 2d, prefer it; if not, the python-reviewer / typescript-reviewer subagents are the next step. The model `gpt-5-codex` errors on ChatGPT accounts — default to `gpt-5.5` or omit `--model`.
4. **Real data informs real design.** The 2026-05-16 inspect-intervals run produced 7 trained stat keys; 4 landed in `bad` coverage. That's the evidence base for the gating decision in PR 3. Re-run `inspect-intervals` before starting design — coverage may have improved.
5. **Worktree vs repo-root contracts gotcha.** When PR 4 regenerates contracts (`npm run contracts:generate`), the new files land in the worktree's `packages/contracts/`. The worktree's `npm` workspaces symlink to the repo-root `packages/contracts/`, so copy the regenerated `api.d.ts` + `openapi.json` to the repo root before running local `tsc`. CI/prod are unaffected.

## Punch-list pointer

The smarter roadmap lives in `SIKA_PUNCH_LIST.md` (repo root, with status banner at top pointing to `PUNCH_LIST_STATE_2026_05_16.md`). Smarter #21 is the section labeled "Conformal prediction intervals on prop expected values."

Read-first docs in this order:
1. **`SIKA_SESSION_RULES.md`** — durable patterns / behaviors from prior sessions (research-first rule + others).
2. **This doc** — phase 2d execution brief.
3. **`PUNCH_LIST_STATE_2026_05_16.md`** — current open items list (confirms nothing else blocks phase 2d).
4. `SMARTER_21_PHASE_2B_HANDOFF.md` — historical context for phase 2b; everything flagged "deferred" there is shipped.
