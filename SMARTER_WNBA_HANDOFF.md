# Smarter — WNBA support — session handoff

You are picking up sport expansion to WNBA for the sika sports trading copilot. Today only NBA + MLB are active ship targets. WNBA's 2026 season is already live (started May 8; 140-day regular season) — every week without coverage is missed picks you can't backfill.

## Read first, code second

**Three docs:**

0. [`SIKA_SESSION_RULES.md`](SIKA_SESSION_RULES.md) — durable patterns that bit previous sessions. **Highest-leverage to read first** — it includes the "research, don't fabricate" rule, the BR 403 reality, codex review status, worktree/contracts quirk, and the punch-list status-truth pointer.
1. [`SMARTER_WNBA_PREP.md`](SMARTER_WNBA_PREP.md) — the canonical research + execution doc. Contains:
   - Business context (season timing, volume expectations, star players)
   - WNBA team list with ESPN abbreviations
   - All data source endpoints (ESPN, Kalshi, basketball-reference, RefMetrics, The Odds API, stats.wnba.com)
   - 77-touchpoint code inventory across apps/api (53) + apps/ml (12) + apps/web (10) + packages (2), categorized high-reuse / medium / low
   - 7 open design decisions with recommendations
   - 8-PR execution sequence (this doc is the execution brief for those PRs)
   - 10-item risks/gotchas list
   - Day-1 operator checklist

2. [`PUNCH_LIST_STATE_2026_05_16.md`](PUNCH_LIST_STATE_2026_05_16.md) — confirms the punch list is current; no other work blocks WNBA.
3. [`SMARTER_13_BR_REFEREE_NEXT.md`](SMARTER_13_BR_REFEREE_NEXT.md) — the operator-blocked URL is now verified (`/referees/{end_year}_register.html`). What's still needed from the operator + the small PR to ship the BR fetcher wiring is documented there.

Skim them. They have every URL, every file:line touchpoint, every gotcha. Don't re-derive.

## Goal

Ship 6 PRs that bring WNBA to parity with NBA on the player-prop surface. After PRs 1-6 land + ~3-4 weeks of WNBA games settle, `python -m ml.cli train-intervals --family-key wnba_props --stat-key points` produces real interval models with **zero new code** — the Smarter #21 phase 2b pipeline is already sport-agnostic.

**MVP scope (PRs 1-6):**
- Tier 1 prop categories: points, rebounds, assists, made_threes, PRA combos
- Heuristic factors only (no advanced stats — see D2 in prep doc)
- All 15 teams including expansion (Toronto Tempo + Portland Fire)

**Out of MVP scope** (defer to MVP+1 or later):
- WNBA advanced stats (stats.wnba.com integration)
- WNBA referee tendencies (RefMetrics scraper)
- WNBA long-tail features (hustle, drives, clutch)
- WNBA parlay families
- Tier 2 props (steals, blocks)
- Phase 2d consumer + UI band for interval models (separate handoff in [sika#160](https://github.com/ckwame-jpg/sika/pull/160))

## 8-PR execution sequence

Each PR is self-contained, TDD-ordered, codex-reviewable. **Don't bundle.** See prep doc §6 for detailed scope per PR.

| PR | Scope | Effort | Risk |
|---|---|---|---|
| 1 | Sport scaffolding (allowlists, team maps, ESPN URLs, TS types, contracts regen) | ½ day | none |
| 2 | Market mapping (`WNBA_PROP_ALIASES`, title regex, `KXWNBA` ticker prefix) | ½ day | low (verify Kalshi title phrasing) |
| 3 | Gamelog parsing + stats query (`_build_game_logs` WNBA branch, season rollover) | ½ day | low |
| 4 | Scoring kernel WNBA branch + heuristic profiles | 1 day | medium (largest PR) |
| 5 | Training pipeline registration (`_DEFAULT_SERVE_FAMILY_KEYS`) | ½ day | none |
| 6 | Kalshi WNBA discovery + ingestion (`KALSHI_*` constants, sport adapter, refresh job) | ½ day | medium (verify Kalshi per-game prop coverage at first slate refresh) |
| 7 (MVP+1) | WNBA injury endpoint | ½ day | low |
| 8 (MVP+1) | Operator UX polish (banner copy, readiness panel) | ½ day | none |

**Critical path total:** ~3 sessions, 12-15 hours wall-clock.

## Workflow requirements (non-negotiable)

You're operating from a sika worktree. Workflow mirrors what shipped Smarter #21 phase 2b across PRs #154-#166.

### Per-PR loop

1. **Branch off `origin/main`**: `git checkout -b claude/smarter-wnba-pr-N-<scope> origin/main`
2. **TDD ordering**: write failing tests first, watch them RED, implement, GREEN, run full suite.
3. **Self-review with the 9-point checklist before push** (see below).
4. **Codex review** if it's responsive (recent sessions hit timeouts — manual review against the 9-point list was sufficient).
5. **Push + PR via `gh pr create`** with scope/contract/rollback in the body.
6. **Admin-merge**: `gh pr merge <N> --squash --admin --body ""`.
7. **Reset to `origin/main`** between PRs: `git checkout -b claude/smarter-wnba-pr-(N+1)-<scope> origin/main`.

### 9-point self-review (apply before every push)

1. Does the test fail without the change and pass with it?
2. Are types narrow (no `Any`, no `dict[str, Any]` at boundaries)?
3. Are inputs validated at the boundary and errors surfaced explicitly?
4. Any silent fallback that could mask a real bug?
5. Does the on-disk / API contract match what existing sports established?
6. Are imports / re-exports preserved for backward compat?
7. Did I touch only files this PR requires?
8. Is the PR description specific about scope, contract, and rollback?
9. Did codex (or the reviewer subagent) flag anything I haven't addressed?

### Codex review

Run if codex is responsive:

```bash
codex exec --skip-git-repo-check --sandbox read-only "$(cat <<'EOF'
Review [PR description] in sika repo. CONTEXT [...]. FILES CHANGED [...].
REVIEW FOCUS — flag P1 / P2 issues only: [9-point checklist].
Reply <300 words. End with APPROVE or REQUEST CHANGES.

DIFF FOLLOWS:
EOF
)$(git diff --staged)" > /tmp/codex.log 2>&1
```

**The default model is `gpt-5.5`** — `gpt-5-codex` errors on ChatGPT accounts. If codex hangs / rate-limits (it did 4× in the last session), fall back to the `python-reviewer` or `typescript-reviewer` subagent with the same prompt.

**Address every P1 before merging.** Reply to P2s in the PR description if you choose not to fix them.

### Frontend changes use the `/frontend-design` skill family

PR 1 + PR 8 touch apps/web. Use `frontend-design:frontend-design` skill for component / tile work. The Tailwind `--sport-wnba` token addition belongs in `apps/web/tailwind.config.ts` (or wherever sport color tokens currently live — check existing `--sport-nba` for the pattern).

## Cross-package drift guards (already in place)

Two cross-package duplications have drift-guard tests. Adding WNBA to one side WILL fail CI until the other side is synced. This is by design.

1. **`_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME`** — lives in both `apps/api/app/clients/espn.py` and `apps/ml/ml/interval_dataset.py`. PR #165 added a test that parses the apps/api source via `ast.literal_eval` and asserts the apps/ml copy agrees. **Add the WNBA team map to BOTH sides in PR 1.**
2. **`INTERVAL_COVERAGE_*` constants** — duplicated between `apps/api/app/services/ml/interval_status.py` and `apps/ml/ml/cli.py`. Not WNBA-relevant but worth knowing the pattern exists.

## Baselines that must stay green

| Suite | Baseline | After WNBA PRs (rough estimate) |
|---|---|---|
| `apps/api` | 1,556 | +20-40 (per-PR tests for WNBA branches + drift guards) |
| `apps/ml` | 247 | +10-20 (mostly fixture updates + new sport tests) |
| `apps/web` | 153 | +5-10 (sport pill / readiness panel) |

Run before push:
- `cd apps/api && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `cd apps/ml && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `cd apps/web && npx tsc --noEmit && npx vitest run`

## File layout you should aim for

```
apps/api/
  app/config.py                            # PR 1 — WNBA TTL settings + allowlists
  app/clients/espn.py                      # PR 1 — ESPN URLs + team abbrev map
  app/services/market_support.py           # PR 2 — WNBA_PROP_ALIASES + title regex
  app/services/stats_query.py              # PR 3 — _build_game_logs WNBA dispatch
  app/services/scoring/__init__.py         # PR 4 — WNBA scoring branch
  app/services/scoring/resolver.py         # PR 4 — WNBA heuristic profiles
  app/services/model_families.py           # PR 4 — wnba_props + wnba_singles families
  app/services/refresh_jobs.py             # PR 6 — WNBA refresh registration
  app/api/routes.py                        # PR 6 — Kalshi WNBA URLs
  app/sports/registry.py                   # PR 6 — WNBA sport adapter

apps/ml/
  ml/dataset.py                            # PR 1 — _family_key WNBA branch, sport_is_wnba one-hot
  ml/interval_dataset.py                   # PR 1 + 3 — team abbrev map, _WNBA_STAT_TO_RAW
  ml/cli.py                                # PR 5 — _DEFAULT_SERVE_FAMILY_KEYS

apps/web/
  lib/types.ts                             # PR 1 — SportKey + SPORT_LABELS
  lib/utils.ts                             # PR 1 — SPORT_OPTIONS
  lib/sport-tints.ts                       # PR 1 — SPORT_TINTS
  tailwind.config.ts                       # PR 1 — --sport-wnba token
  lib/health-status.ts                     # PR 8 — banner copy

packages/contracts/
  generated/api.d.ts                       # PR 1 — auto-regenerated
  openapi.json                             # PR 1 — auto-regenerated

(plus matching tests/ for every code file touched)
```

## Two snapshot quirks the previous session hit

1. **Worktree vs repo-root contracts.** When you regenerate contracts in a worktree (`npm run contracts:generate`), the new `api.d.ts` lands in the worktree. The worktree's `npm` workspaces symlink resolves to **repo-root** `packages/contracts` for type-checking — so you also need to copy the regenerated `api.d.ts` + `openapi.json` to the repo root for local `tsc` to pick them up. CI / production aren't affected; this is local-dev only.

2. **Pulling main into the worktree after admin-merging.** The repo-root main and the worktree's `origin/main` ref drift if you don't re-fetch after each merge. Pattern that works: after `gh pr merge --admin`, do `git fetch origin main && git checkout -b claude/<next-pr-scope> origin/main` from the worktree. Don't try to fast-forward an existing branch — branch fresh from `origin/main` each PR.

## Open design decisions (recommendations baked into the prep doc)

See prep doc §5 for full reasoning. Short version:

| Decision | Recommendation |
|---|---|
| D1 — Cache table topology | Parallel WNBA tables (mirror NBA pattern; smaller blast radius) |
| D2 — Advanced-stats client generalization | **Skip for MVP**; ship without; promote based on real evidence |
| D3 — Prop categories | **Tier 1 only** for MVP; steals + blocks as MVP+1 |
| D4 — Heuristic profile values | **Start with NBA values**; refine after Smarter #2 backtest |
| D5 — Manifest topology | Add `wnba_props` + `wnba_singles` to `_DEFAULT_SERVE_FAMILY_KEYS` |
| D6 — Lineup gate | Same Smarter #16 "suppress, don't penalize" policy |
| D7 — Toronto/Portland cold start | Flag both; small confidence penalty for first ~15 games |

## What NOT to do

- **Don't touch the on-disk contract** in `apps/ml/ml/interval_training.py` or `apps/api/app/services/ml/artifact_loader.py`. Smarter #21 phase 2c is shipped and serving; changing the layout breaks both ends.
- **Don't change phase 2b's `train-intervals` CLI behavior.** It's sport-agnostic by design. Once WNBA settled rows accumulate, just point it at `--family-key wnba_props`.
- **Don't skip the cross-package drift guard.** When you update `_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME` in apps/api, the test in apps/ml will fail until you sync the apps/ml copy. That's the safeguard working.
- **Don't bundle PRs.** Each PR has independent scope, independent tests, independent rollback. Reviewing six small PRs is faster than one large one.
- **Don't ship phase 2d consumer / UI band as part of WNBA work.** That's separate ([sika#160](https://github.com/ckwame-jpg/sika/pull/160) handoff), needs coverage-status gating design first, and benefits from accumulating more cross-sport interval calibration evidence before committing.
- **Don't add WNBA advanced stats / referee scraper / long-tail features in MVP.** Out of scope by D2/D3.

## Day-1 verification after PRs 1-6 land

From the prep doc §8:

1. **Verify Kalshi WNBA market discovery.** `GET /ops/markets?sport_key=WNBA` should show events. If only milestones/futures appear, document the per-game prop gap as a Kalshi-side limitation.
2. **Verify ESPN WNBA gamelog cache populates.** After 24-48h, `python -m ml.cli inspect-intervals --manifest-path manifests/current.json --family-key wnba_props` should show `no_gamelog` skip count dropping.
3. **Verify readiness panel "Prediction Intervals" tile shows WNBA families.** Will report `insufficient_samples` until ~30 settled WNBA predictions exist. That's expected.
4. **Eyeball first 10 WNBA recommendations.** `expected_stat_output` in plausible range (points 5-30, rebounds 2-12), `confidence` not stuck at floor, `quality_tier` not always `low`, `scoring_diagnostics.recent_games` populated.
5. **After ~3 weeks of WNBA games settle:** run `train-intervals` for `wnba_props/points` and check empirical coverage. Target 0.75-0.85 like NBA.

## Punch-list pointer

- [`SIKA_PUNCH_LIST.md`](SIKA_PUNCH_LIST.md) — the big roadmap. Checkboxes are stale; trust the state snapshot below.
- [`PUNCH_LIST_STATE_2026_05_16.md`](PUNCH_LIST_STATE_2026_05_16.md) — reconciled open-items list as of late 2026-05-16. None block WNBA.

The full session-by-session handoff history (architecture context, conventions, decisions log) is in `SESSION_HANDOFF_2026_05_15.md` and earlier. Read if anything in this doc references a pattern you don't immediately recognize.
